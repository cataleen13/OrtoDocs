"""Citire și editare pentru istoricul generărilor OrtoDocs."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from ortodocs.models.generation import GenerationHistoryItem, GenerationSessionDetail
from ortodocs.models.rich_document import RichDocument
from ortodocs.storage.database import write_transaction


GENERATION_DOCUMENT_TYPES = (
    "epicrisis_recommendations",
    "operative_protocol",
)
_DOCUMENT_TYPES = frozenset(GENERATION_DOCUMENT_TYPES)
_UTC_MILLISECOND_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)


class GenerationRepositoryError(RuntimeError):
    """Eroare de citire sau persistență pentru istoricul generării."""


class GenerationSessionNotFoundError(GenerationRepositoryError):
    """Sesiunea solicitată pentru editare nu există."""


class GenerationSessionStateError(GenerationRepositoryError):
    """Sesiunea nu este completă și nu poate fi editată."""


class GeneratedDocumentNotFoundError(GenerationRepositoryError):
    """Documentul solicitat lipsește din sesiune."""


class GenerationPersistenceError(GenerationRepositoryError):
    """Datele persistate nu respectă structura așteptată de aplicație."""


def _validate_connection(connection: sqlite3.Connection) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection trebuie să fie o conexiune sqlite3.Connection.")


def _validate_session_id(session_id: int) -> None:
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id < 1:
        raise ValueError("session_id trebuie să fie un întreg pozitiv.")


def _validate_edited_at(edited_at: str) -> None:
    if not isinstance(edited_at, str) or not _UTC_MILLISECOND_TIMESTAMP.fullmatch(edited_at):
        raise ValueError(
            "edited_at trebuie să fie un timestamp UTC ISO 8601 cu milisecunde și sufixul Z."
        )
    try:
        datetime.strptime(edited_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError("edited_at nu conține o dată și o oră valide.") from exc


def _serialize_document(document: RichDocument) -> str:
    if not isinstance(document, RichDocument):
        raise TypeError("document trebuie să fie un RichDocument.")
    try:
        serialized = document.to_json()
        # Modelele sunt mutabile; verificăm din nou structura înainte de salvare.
        RichDocument.from_json(serialized)
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"document nu este un RichDocument valid: {exc}") from exc
    return serialized


def list_generation_sessions(
    connection: sqlite3.Connection,
    limit: int = 10,
) -> tuple[GenerationHistoryItem, ...]:
    """Listează cele mai recente sesiuni complete, fără a încărca documentele."""

    _validate_connection(connection)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit trebuie să fie un întreg între 1 și 10.")

    rows = connection.execute(
        """
        SELECT
            id,
            diagnosis_id,
            intervention_id,
            template_set_id,
            diagnosis_label_snapshot,
            intervention_label_snapshot,
            created_at,
            status
        FROM generation_sessions
        WHERE status = 'complete'
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    items: list[GenerationHistoryItem] = []
    for row in rows:
        try:
            items.append(
                GenerationHistoryItem(
                    session_id=row[0],
                    diagnosis_id=row[1],
                    intervention_id=row[2],
                    template_set_id=row[3],
                    diagnosis_label_snapshot=row[4],
                    intervention_label_snapshot=row[5],
                    created_at=row[6],
                    status=row[7],
                )
            )
        except (TypeError, ValueError) as exc:
            raise GenerationPersistenceError(
                f"Sesiunea {row[0]} conține metadate invalide pentru istoric."
            ) from exc
    return tuple(items)


def get_generation_session(
    connection: sqlite3.Connection,
    session_id: int,
) -> GenerationSessionDetail | None:
    """Recuperează atomic sesiunea completă și ambele documente Rich Text."""

    _validate_connection(connection)
    _validate_session_id(session_id)

    rows = connection.execute(
        """
        SELECT
            gs.id,
            gs.diagnosis_id,
            gs.intervention_id,
            gs.template_set_id,
            gs.diagnosis_label_snapshot,
            gs.intervention_label_snapshot,
            gs.created_at,
            gs.status,
            gd.document_type,
            gd.template_id,
            gd.independent_template_version_id,
            gd.rich_text_json,
            gd.edited_at
        FROM generation_sessions AS gs
        LEFT JOIN generated_documents AS gd
            ON gd.session_id = gs.id
        WHERE gs.id = ?
        ORDER BY gd.document_type
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return None
    if rows[0][7] != "complete":
        return None

    history_row = rows[0]
    document_rows: dict[str, tuple[int, int | None, str, str]] = {}
    for row in rows:
        document_type = row[8]
        if document_type is None:
            continue
        if document_type not in _DOCUMENT_TYPES or document_type in document_rows:
            raise GenerationPersistenceError(
                f"Sesiunea {session_id} conține tipuri de documente invalide sau duplicate."
            )
        legacy_template_id = row[9]
        independent_version_id = row[10]
        if history_row[3] is None:
            if legacy_template_id is not None or independent_version_id is None:
                raise GenerationPersistenceError(
                    f"Documentul {document_type!r} din sesiunea {session_id} are referințe de șablon inconsistente."
                )
            effective_template_id = int(independent_version_id)
        else:
            if legacy_template_id is None or independent_version_id is not None:
                raise GenerationPersistenceError(
                    f"Documentul {document_type!r} din sesiunea {session_id} are referințe legacy inconsistente."
                )
            effective_template_id = int(legacy_template_id)
        document_rows[document_type] = (
            effective_template_id,
            int(independent_version_id) if independent_version_id is not None else None,
            row[11],
            row[12],
        )

    if set(document_rows) != _DOCUMENT_TYPES:
        raise GenerationPersistenceError(
            f"Sesiunea completă {session_id} nu conține exact ambele documente."
        )

    for document_type in GENERATION_DOCUMENT_TYPES:
        edited_at = document_rows[document_type][3]
        try:
            _validate_edited_at(edited_at)
        except ValueError as exc:
            raise GenerationPersistenceError(
                f"Documentul {document_type!r} din sesiunea {session_id} "
                f"are edited_at invalid: {exc}"
            ) from exc

    documents: dict[str, RichDocument] = {}
    for document_type in GENERATION_DOCUMENT_TYPES:
        _, _, rich_text_json, _ = document_rows[document_type]
        try:
            documents[document_type] = RichDocument.from_json(rich_text_json)
        except (RecursionError, TypeError, ValueError) as exc:
            raise GenerationPersistenceError(
                f"Documentul {document_type!r} din sesiunea {session_id} are JSON Rich Text invalid."
            ) from exc

    epicrisis_template_id, epicrisis_version_id, _, epicrisis_edited_at = document_rows[
        "epicrisis_recommendations"
    ]
    protocol_template_id, protocol_version_id, _, protocol_edited_at = document_rows[
        "operative_protocol"
    ]
    try:
        history_item = GenerationHistoryItem(
            session_id=history_row[0],
            diagnosis_id=history_row[1],
            intervention_id=history_row[2],
            template_set_id=history_row[3],
            diagnosis_label_snapshot=history_row[4],
            intervention_label_snapshot=history_row[5],
            created_at=history_row[6],
            status=history_row[7],
        )
        return GenerationSessionDetail(
            history_item=history_item,
            epicrisis_recommendations=documents["epicrisis_recommendations"],
            operative_protocol=documents["operative_protocol"],
            epicrisis_template_id=epicrisis_template_id,
            epicrisis_edited_at=epicrisis_edited_at,
            operative_template_id=protocol_template_id,
            operative_edited_at=protocol_edited_at,
            epicrisis_template_version_id=epicrisis_version_id,
            operative_template_version_id=protocol_version_id,
        )
    except (TypeError, ValueError) as exc:
        raise GenerationPersistenceError(
            f"Sesiunea {session_id} conține metadate invalide pentru detaliu."
        ) from exc


def update_generated_document(
    connection: sqlite3.Connection,
    session_id: int,
    document_type: str,
    document: RichDocument,
    edited_at: str,
) -> None:
    """Actualizează atomic doar documentul indicat dintr-o sesiune completă."""

    _validate_connection(connection)
    _validate_session_id(session_id)
    if not isinstance(document_type, str) or document_type not in _DOCUMENT_TYPES:
        raise ValueError("document_type nu este un tip de document OrtoDocs acceptat.")
    _validate_edited_at(edited_at)
    rich_text_json = _serialize_document(document)

    with write_transaction(connection):
        session = connection.execute(
            "SELECT status FROM generation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise GenerationSessionNotFoundError(
                f"Sesiunea de generare {session_id} nu există."
            )
        if session[0] != "complete":
            raise GenerationSessionStateError(
                f"Sesiunea {session_id} nu este completă și nu poate fi editată."
            )

        present_types = {
            row[0]
            for row in connection.execute(
                "SELECT document_type FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }
        if document_type not in present_types:
            raise GeneratedDocumentNotFoundError(
                f"Documentul {document_type!r} lipsește din sesiunea {session_id}."
            )
        if present_types != _DOCUMENT_TYPES:
            raise GenerationPersistenceError(
                f"Sesiunea completă {session_id} nu conține exact ambele documente."
            )

        updated = connection.execute(
            """
            UPDATE generated_documents
            SET rich_text_json = ?, edited_at = ?
            WHERE session_id = ? AND document_type = ?
            """,
            (rich_text_json, edited_at, session_id, document_type),
        ).rowcount
        if updated != 1:
            raise GeneratedDocumentNotFoundError(
                f"Documentul {document_type!r} lipsește din sesiunea {session_id}."
            )
