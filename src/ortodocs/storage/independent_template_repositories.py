"""Operații SQL pentru versiunile independente ale șabloanelor OrtoDocs.

Funcțiile primesc conexiunea SQLite de la apelant. Nu deschid conexiuni și nu
încep sau finalizează tranzacții; validarea regulilor de business și coordonarea
tranzacțiilor aparțin stratului services.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


_OWNER_SELECT = """
    SELECT
        v.id,
        v.owner_type,
        v.diagnosis_id,
        v.intervention_id,
        v.document_type,
        CASE
            WHEN v.owner_type = 'diagnosis' THEN d.name
            ELSE i.name
        END AS owner_name,
        CASE
            WHEN v.owner_type = 'diagnosis' THEN d.code
            ELSE i.code
        END AS owner_code,
        CASE
            WHEN v.owner_type = 'diagnosis' THEN d.is_active
            ELSE i.is_active
        END AS owner_is_active,
        v.version_no,
        v.status,
        v.source_sha256,
        v.parser_report_json,
        v.parser_version,
        v.validation_status,
        v.warnings_confirmed,
        v.warnings_confirmed_at,
        v.patient_identification_reviewed,
        v.patient_identification_reviewed_at,
        v.created_at,
        v.imported_at,
        v.activated_at,
        v.retired_at,
        v.legacy_template_id
    FROM independent_template_versions AS v
    LEFT JOIN diagnoses AS d
        ON v.owner_type = 'diagnosis' AND d.id = v.diagnosis_id
    LEFT JOIN interventions AS i
        ON v.owner_type = 'intervention' AND i.id = v.intervention_id
"""


def list_independent_template_versions(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_type: str,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Listează versiuni fără a încărca BLOB-ul DOCX sau Rich Text-ul parsat."""

    query = _OWNER_SELECT + """
        WHERE v.owner_type = ?
          AND v.document_type = ?
          AND (
              (v.owner_type = 'diagnosis' AND v.diagnosis_id = ?)
              OR
              (v.owner_type = 'intervention' AND v.intervention_id = ?)
          )
    """
    parameters: list[Any] = [owner_type, document_type, owner_id, owner_id]
    if status is not None:
        query += " AND v.status = ?"
        parameters.append(status)
    query += " ORDER BY v.version_no DESC, v.id DESC"
    return [
        dict(row)
        for row in connection.execute(query, parameters).fetchall()
    ]


def get_independent_template_details(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any] | None:
    """Returnează metadatele și raportul parserului, fără DOCX-ul original."""

    return _as_dict(
        connection.execute(
            _OWNER_SELECT + " WHERE v.id = ?",
            (template_version_id,),
        ).fetchone()
    )


def get_independent_template_content(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any] | None:
    """Returnează conținutul complet al versiunii, inclusiv DOCX-ul original."""

    return _as_dict(
        connection.execute(
            """
            SELECT id, source_docx, source_sha256, parsed_content_json,
                   parser_report_json, parser_version
            FROM independent_template_versions
            WHERE id = ?
            """,
            (template_version_id,),
        ).fetchone()
    )


def get_next_independent_template_version(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_type: str,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(version_no), 0) + 1
        FROM independent_template_versions
        WHERE owner_type = ?
          AND document_type = ?
          AND (
              (owner_type = 'diagnosis' AND diagnosis_id = ?)
              OR
              (owner_type = 'intervention' AND intervention_id = ?)
          )
        """,
        (owner_type, document_type, owner_id, owner_id),
    ).fetchone()
    return int(row[0])


def insert_independent_template_version(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_type: str,
    version_no: int,
    source_docx: bytes,
    source_sha256: str,
    parsed_content_json: str,
    parser_report_json: str,
    parser_version: str,
    validation_status: str = "pending",
) -> int:
    """Inserează o versiune nouă în starea draft impusă de schema v2."""

    cursor = connection.execute(
        """
        INSERT INTO independent_template_versions (
            owner_type, diagnosis_id, intervention_id, document_type,
            version_no, status, source_docx, source_sha256,
            parsed_content_json, parser_report_json, parser_version,
            validation_status
        )
        VALUES (
            ?,
            CASE WHEN ? = 'diagnosis' THEN ? ELSE NULL END,
            CASE WHEN ? = 'intervention' THEN ? ELSE NULL END,
            ?, ?, 'draft', ?, ?, ?, ?, ?, ?
        )
        """,
        (
            owner_type,
            owner_type,
            owner_id,
            owner_type,
            owner_id,
            document_type,
            version_no,
            source_docx,
            source_sha256,
            parsed_content_json,
            parser_report_json,
            parser_version,
            validation_status,
        ),
    )
    return int(cursor.lastrowid)


def get_active_independent_template_version(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_type: str,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            _OWNER_SELECT
            + """
            WHERE v.owner_type = ?
              AND v.document_type = ?
              AND v.status = 'active'
              AND (
                  (v.owner_type = 'diagnosis' AND v.diagnosis_id = ?)
                  OR
                  (v.owner_type = 'intervention' AND v.intervention_id = ?)
              )
            """,
            (owner_type, document_type, owner_id, owner_id),
        ).fetchone()
    )


def update_independent_template_confirmations(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    warnings_confirmed: bool,
    warnings_confirmed_at: str | None,
    patient_identification_reviewed: bool,
    patient_identification_reviewed_at: str | None,
) -> int:
    """Actualizează confirmările unui draft; constrângerile timestamp-urilor
    sunt verificate de schema SQLite.
    """

    cursor = connection.execute(
        """
        UPDATE independent_template_versions
        SET warnings_confirmed = ?,
            warnings_confirmed_at = ?,
            patient_identification_reviewed = ?,
            patient_identification_reviewed_at = ?
        WHERE id = ? AND status = 'draft'
        """,
        (
            int(warnings_confirmed),
            warnings_confirmed_at,
            int(patient_identification_reviewed),
            patient_identification_reviewed_at,
            template_version_id,
        ),
    )
    return cursor.rowcount


def update_independent_template_validation(
    connection: sqlite3.Connection,
    template_version_id: int,
    validation_status: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE independent_template_versions
        SET validation_status = ?
        WHERE id = ? AND status = 'draft'
        """,
        (validation_status, template_version_id),
    )
    return cursor.rowcount


def activate_independent_template_version(
    connection: sqlite3.Connection,
    template_version_id: int,
    activated_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE independent_template_versions
        SET status = 'active', activated_at = ?, retired_at = NULL
        WHERE id = ? AND status = 'draft'
        """,
        (activated_at, template_version_id),
    )
    return cursor.rowcount


def retire_independent_template_version(
    connection: sqlite3.Connection,
    template_version_id: int,
    retired_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE independent_template_versions
        SET status = 'retired', retired_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (retired_at, template_version_id),
    )
    return cursor.rowcount


def reject_independent_template_version(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> int:
    cursor = connection.execute(
        """
        UPDATE independent_template_versions
        SET status = 'rejected'
        WHERE id = ? AND status = 'draft'
        """,
        (template_version_id,),
    )
    return cursor.rowcount
