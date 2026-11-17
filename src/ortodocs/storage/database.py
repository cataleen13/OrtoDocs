"""Conexiuni SQLite și operații tranzacționale de stocare OrtoDocs."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping


DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_SESSION_RETENTION = 10
GENERATION_DOCUMENT_TYPES = (
    "epicrisis_recommendations",
    "operative_protocol",
)
_UNSET = object()


class DatabaseError(RuntimeError):
    """Eroare de configurare sau operație la nivelul bazei de date."""


class DatabaseConfigurationError(DatabaseError):
    """Configurația locală a bazei de date nu este validă."""


class DatabaseConnectionError(DatabaseError):
    """Nu s-a putut deschide sau configura conexiunea SQLite."""


class DatabaseIntegrityError(DatabaseError):
    """O operație ar încălca o invariantă a datelor aplicației."""


def get_database_path() -> Path:
    """Returnează calea persistentă fără a crea directoare sau fișiere."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise DatabaseConfigurationError(
            "Variabila de mediu LOCALAPPDATA nu este definită."
        )
    return Path(local_app_data) / "OrtoDocs" / "ortodocs.db"


def connect_database(
    database_path: str | os.PathLike[str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Deschide o conexiune și activează/verifică cheile străine.

    Deschiderea explicită a unei conexiuni poate crea fișierul SQLite. Importul
    modulului nu apelează această funcție și nu creează date sau directoare.
    Tranzacțiile sunt controlate explicit de apelant.
    """

    if timeout <= 0:
        raise ValueError("timeout trebuie să fie pozitiv")

    raw_path = os.fspath(database_path) if database_path is not None else None
    if raw_path is None:
        path = get_database_path()
        raw_path = os.fspath(path)

    connection: sqlite3.Connection | None = None
    try:
        if raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(
            raw_path,
            timeout=timeout,
            autocommit=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or foreign_keys[0] != 1:
            raise DatabaseConfigurationError(
                "SQLite nu a activat PRAGMA foreign_keys pentru conexiune."
            )
        return connection
    except DatabaseConfigurationError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise DatabaseConnectionError(
            f"Nu s-a putut deschide/configura baza SQLite: {exc}"
        ) from exc


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Deschide o tranzacție de scriere sau un savepoint imbricat."""

    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys is None or foreign_keys[0] != 1:
        raise DatabaseConfigurationError(
            "Operația necesită PRAGMA foreign_keys = ON pe conexiune."
        )

    owns_transaction = not connection.in_transaction
    savepoint = f"ortodocs_write_{uuid.uuid4().hex}"
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")

    try:
        yield
        if owns_transaction:
            connection.execute("COMMIT")
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if owns_transaction:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        elif connection.in_transaction:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def switch_current_field_version(
    connection: sqlite3.Connection,
    variable_key: str,
    version: int,
) -> None:
    """Setează atomic versiunea curentă pentru o cheie de variabilă.

    Indexul unic parțial împiedică două versiuni curente. Tranzacția permite
    starea intermediară fără versiune curentă, apoi verifică exact una înainte
    de commit. Versiunile istorice și mapările șabloanelor nu sunt modificate.
    """

    if not variable_key:
        raise ValueError("variable_key nu poate fi gol")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version trebuie să fie un întreg pozitiv")

    with write_transaction(connection):
        target = connection.execute(
            """
            SELECT id
            FROM field_definitions
            WHERE variable_key = ? AND version = ?
            """,
            (variable_key, version),
        ).fetchone()
        if target is None:
            raise DatabaseIntegrityError(
                f"Nu există versiunea {version} pentru variabila {variable_key!r}."
            )

        connection.execute(
            "UPDATE field_definitions SET is_current = 0 WHERE variable_key = ?",
            (variable_key,),
        )
        connection.execute(
            "UPDATE field_definitions SET is_current = 1 WHERE id = ?",
            (target["id"],),
        )

        current_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM field_definitions
            WHERE variable_key = ? AND is_current = 1
            """,
            (variable_key,),
        ).fetchone()[0]
        if current_count != 1:
            raise DatabaseIntegrityError(
                f"Variabila {variable_key!r} trebuie să aibă exact o versiune curentă."
            )


def retain_latest_generation_sessions(
    connection: sqlite3.Connection,
    limit: int = DEFAULT_SESSION_RETENTION,
) -> int:
    """Elimină sesiunile complete mai vechi de limita cerută.

    Fiecare sesiune este trecută în starea `deleting` înainte de ștergere;
    documentele sunt eliminate de SQLite prin ON DELETE CASCADE. Operația
    este atomică și poate rula într-o tranzacție deja deschisă de apelant.
    Returnează numărul sesiunilor șterse.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit trebuie să fie un întreg pozitiv")

    removed = 0
    with write_transaction(connection):
        old_sessions = connection.execute(
            """
            SELECT id
            FROM generation_sessions
            WHERE status = 'complete' AND template_set_id IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT -1 OFFSET ?
            """,
            (limit,),
        ).fetchall()

        for row in old_sessions:
            session_id = row["id"]
            updated = connection.execute(
                """
                UPDATE generation_sessions
                SET status = 'deleting'
                WHERE id = ? AND status = 'complete' AND template_set_id IS NOT NULL
                """,
                (session_id,),
            ).rowcount
            if updated != 1:
                raise DatabaseIntegrityError(
                    f"Sesiunea {session_id} nu a putut fi pregătită pentru ștergere."
                )

            deleted = connection.execute(
                "DELETE FROM generation_sessions WHERE id = ?",
                (session_id,),
            ).rowcount
            if deleted != 1:
                raise DatabaseIntegrityError(
                    f"Sesiunea {session_id} nu a putut fi ștearsă."
                )
            removed += 1

    return removed


def save_generation_session(
    connection: sqlite3.Connection,
    *,
    diagnosis_id: int,
    intervention_id: int,
    template_set_id: int | None = None,
    diagnosis_label_snapshot: str,
    intervention_label_snapshot: str,
    documents: Mapping[str, tuple[int, str]],
    retention_limit: int | None | object = _UNSET,
    created_at: str | None = None,
) -> int:
    """Salvează atomic sesiunea și ambele documente (legacy sau independente V3).

    ``documents`` mapează fiecare tip de document la perechea
    ``(template_or_version_id, rich_text_json)``. Pentru sesiuni legacy
    (``template_set_id is not None``) aplică implicit retenția la 10 sesiuni;
    pentru sesiuni V3 independente (``template_set_id is None``) păstrează
    implicit istoricul complet fără ștergerea sesiunilor vechi.
    """

    if not isinstance(documents, Mapping) or set(documents) != set(
        GENERATION_DOCUMENT_TYPES
    ):
        raise ValueError(
            "documents trebuie să conțină exact cele două tipuri de documente"
        )
    if template_set_id is not None and (
        isinstance(template_set_id, bool)
        or not isinstance(template_set_id, int)
        or template_set_id < 1
    ):
        raise ValueError("template_set_id trebuie să fie un întreg pozitiv sau None")

    effective_retention: int | None
    if retention_limit is _UNSET:
        effective_retention = (
            DEFAULT_SESSION_RETENTION if template_set_id is not None else None
        )
    elif retention_limit is None:
        effective_retention = None
    elif (
        isinstance(retention_limit, bool)
        or not isinstance(retention_limit, int)
        or retention_limit < 1
    ):
        raise ValueError("retention_limit trebuie să fie un întreg pozitiv sau None")
    else:
        effective_retention = retention_limit

    if connection.in_transaction:
        raise DatabaseConfigurationError(
            "Salvarea sesiunii trebuie să înceapă în afara unei tranzacții."
        )

    with write_transaction(connection):
        if created_at is None:
            cursor = connection.execute(
                """
                INSERT INTO generation_sessions (
                    diagnosis_id, intervention_id, template_set_id,
                    diagnosis_label_snapshot, intervention_label_snapshot
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    diagnosis_id,
                    intervention_id,
                    template_set_id,
                    diagnosis_label_snapshot,
                    intervention_label_snapshot,
                ),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO generation_sessions (
                    diagnosis_id, intervention_id, template_set_id,
                    diagnosis_label_snapshot, intervention_label_snapshot,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnosis_id,
                    intervention_id,
                    template_set_id,
                    diagnosis_label_snapshot,
                    intervention_label_snapshot,
                    created_at,
                ),
            )
        session_id = int(cursor.lastrowid)

        for document_type in GENERATION_DOCUMENT_TYPES:
            template_ref_id, rich_text_json = documents[document_type]
            if template_set_id is None:
                connection.execute(
                    """
                    INSERT INTO generated_documents (
                        session_id, template_set_id, document_type,
                        template_id, independent_template_version_id, rich_text_json
                    )
                    VALUES (?, NULL, ?, NULL, ?, ?)
                    """,
                    (
                        session_id,
                        document_type,
                        template_ref_id,
                        rich_text_json,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO generated_documents (
                        session_id, template_set_id, document_type,
                        template_id, rich_text_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        template_set_id,
                        document_type,
                        template_ref_id,
                        rich_text_json,
                    ),
                )

        completed = connection.execute(
            "UPDATE generation_sessions SET status = 'complete' WHERE id = ?",
            (session_id,),
        ).rowcount
        if completed != 1:
            raise DatabaseIntegrityError(
                f"Sesiunea {session_id} nu a putut fi marcată completă."
            )

        document_count = connection.execute(
            "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if document_count != 2:
            raise DatabaseIntegrityError(
                f"Sesiunea {session_id} trebuie să conțină exact două documente."
            )

        if effective_retention is not None:
            retain_latest_generation_sessions(connection, effective_retention)

        retained_session = connection.execute(
            """
            SELECT gs.status, COUNT(gd.session_id)
            FROM generation_sessions gs
            LEFT JOIN generated_documents gd ON gd.session_id = gs.id
            WHERE gs.id = ?
            GROUP BY gs.id
            """,
            (session_id,),
        ).fetchone()
        if (
            retained_session is None
            or retained_session["status"] != "complete"
            or retained_session[1] != 2
        ):
            raise DatabaseIntegrityError(
                f"Sesiunea nouă {session_id} nu a rămas completă după retenție."
            )

    return session_id
