"""Operații SQL pentru pachetele și mapările șabloanelor OrtoDocs.

Funcțiile primesc o conexiune deschisă și nu gestionează tranzacții. Validarea
regulilor aplicației și coordonarea tranzacțiilor aparțin serviciilor.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT ts.*, i.diagnosis_id, i.is_active AS intervention_is_active,
                   d.is_active AS diagnosis_is_active
            FROM template_sets ts
            JOIN interventions i ON i.id = ts.intervention_id
            JOIN diagnoses d ON d.id = i.diagnosis_id
            WHERE ts.id = ?
            """,
            (template_set_id,),
        ).fetchone()
    )


def get_active_template_set(
    connection: sqlite3.Connection,
    intervention_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT * FROM template_sets
            WHERE intervention_id = ? AND status = 'active'
            """,
            (intervention_id,),
        ).fetchone()
    )


def list_template_sets(
    connection: sqlite3.Connection,
    *,
    intervention_id: int | None = None,
) -> list[dict[str, Any]]:
    """Listează rezumatele pachetelor fără a încărca documentele DOCX."""

    query = """
        SELECT ts.id, ts.intervention_id, i.diagnosis_id,
               d.name AS diagnosis_name, d.code AS diagnosis_code,
               d.is_active AS diagnosis_is_active,
               i.name AS intervention_name, i.code AS intervention_code,
               i.is_active AS intervention_is_active,
               ts.version_no, ts.status, ts.created_at, ts.activated_at,
               ts.retired_at,
               (SELECT COUNT(*) FROM templates t
                WHERE t.template_set_id = ts.id) AS document_count,
               ts.import_report_json
        FROM template_sets ts
        JOIN interventions i ON i.id = ts.intervention_id
        JOIN diagnoses d ON d.id = i.diagnosis_id
    """
    parameters: tuple[int, ...] = ()
    if intervention_id is not None:
        query += " WHERE ts.intervention_id = ?"
        parameters = (intervention_id,)
    query += """
        ORDER BY d.sort_order, d.name_key, d.id,
                 i.sort_order, i.name_key, i.id,
                 ts.version_no DESC, ts.id DESC
    """
    return [
        dict(row)
        for row in connection.execute(query, parameters).fetchall()
    ]


def get_next_template_set_version(
    connection: sqlite3.Connection,
    intervention_id: int,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(version_no), 0) + 1
        FROM template_sets
        WHERE intervention_id = ?
        """,
        (intervention_id,),
    ).fetchone()
    return int(row[0])


def insert_template_set(
    connection: sqlite3.Connection,
    *,
    intervention_id: int,
    version_no: int,
    import_report_json: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO template_sets (intervention_id, version_no, import_report_json)
        VALUES (?, ?, ?)
        """,
        (intervention_id, version_no, import_report_json),
    )
    return int(cursor.lastrowid)


def update_template_set_report(
    connection: sqlite3.Connection,
    template_set_id: int,
    import_report_json: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_sets
        SET import_report_json = ?
        WHERE id = ? AND status = 'draft'
        """,
        (import_report_json, template_set_id),
    )
    return cursor.rowcount


def list_templates(
    connection: sqlite3.Connection,
    template_set_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, template_set_id, document_type, source_sha256,
                   validation_status, validation_report_json, parser_version,
                   imported_at
            FROM templates
            WHERE template_set_id = ?
            ORDER BY document_type
            """,
            (template_set_id,),
        ).fetchall()
    ]


def get_template(
    connection: sqlite3.Connection,
    template_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT t.id, t.template_set_id, t.document_type, t.source_sha256,
                   t.validation_status, t.validation_report_json, t.parser_version,
                   t.imported_at, ts.status AS template_set_status,
                   ts.intervention_id, i.diagnosis_id
            FROM templates t
            JOIN template_sets ts ON ts.id = t.template_set_id
            JOIN interventions i ON i.id = ts.intervention_id
            WHERE t.id = ?
            """,
            (template_id,),
        ).fetchone()
    )


def get_template_source(
    connection: sqlite3.Connection,
    template_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT id, source_docx, source_sha256, parsed_content_json
            FROM templates
            WHERE id = ?
            """,
            (template_id,),
        ).fetchone()
    )


def insert_template(
    connection: sqlite3.Connection,
    *,
    template_set_id: int,
    document_type: str,
    source_docx: bytes,
    source_sha256: str,
    parsed_content_json: str,
    validation_report_json: str,
    parser_version: str,
    validation_status: str = "pending",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO templates (
            template_set_id, document_type, source_docx, source_sha256,
            parsed_content_json, validation_status, validation_report_json,
            parser_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            template_set_id,
            document_type,
            source_docx,
            source_sha256,
            parsed_content_json,
            validation_status,
            validation_report_json,
            parser_version,
        ),
    )
    return int(cursor.lastrowid)


def update_template_validation(
    connection: sqlite3.Connection,
    template_id: int,
    *,
    validation_status: str,
    validation_report_json: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE templates
        SET validation_status = ?, validation_report_json = ?
        WHERE id = ?
          AND template_set_id IN (
              SELECT id FROM template_sets WHERE status = 'draft'
          )
        """,
        (validation_status, validation_report_json, template_id),
    )
    return cursor.rowcount


def delete_draft_template(
    connection: sqlite3.Connection,
    template_id: int,
) -> int:
    connection.execute(
        "DELETE FROM template_variables WHERE template_id = ?",
        (template_id,),
    )
    cursor = connection.execute(
        """
        DELETE FROM templates
        WHERE id = ? AND template_set_id IN (
            SELECT id FROM template_sets WHERE status = 'draft'
        )
        """,
        (template_id,),
    )
    return cursor.rowcount


def list_template_variable_mappings(
    connection: sqlite3.Connection,
    template_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT tv.template_id, tv.variable_key, tv.field_definition_id,
                   tv.is_required, fd.version, fd.label, fd.data_type, fd.scope,
                   fd.diagnosis_id, fd.intervention_id, fd.is_current
            FROM template_variables tv
            JOIN field_definitions fd ON fd.id = tv.field_definition_id
            WHERE tv.template_id = ?
            ORDER BY tv.variable_key
            """,
            (template_id,),
        ).fetchall()
    ]


def get_template_variable_mapping(
    connection: sqlite3.Connection,
    template_id: int,
    variable_key: str,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT * FROM template_variables
            WHERE template_id = ? AND variable_key = ?
            """,
            (template_id, variable_key),
        ).fetchone()
    )


def insert_template_variable_mapping(
    connection: sqlite3.Connection,
    *,
    template_id: int,
    variable_key: str,
    field_definition_id: int,
    is_required: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO template_variables (
            template_id, variable_key, field_definition_id, is_required
        )
        VALUES (?, ?, ?, ?)
        """,
        (template_id, variable_key, field_definition_id, int(is_required)),
    )


def update_template_variable_mapping(
    connection: sqlite3.Connection,
    *,
    template_id: int,
    variable_key: str,
    field_definition_id: int,
    is_required: bool,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_variables
        SET field_definition_id = ?, is_required = ?
        WHERE template_id = ? AND variable_key = ?
        """,
        (field_definition_id, int(is_required), template_id, variable_key),
    )
    return cursor.rowcount


def delete_template_variable_mapping(
    connection: sqlite3.Connection,
    *,
    template_id: int,
    variable_key: str,
) -> int:
    cursor = connection.execute(
        """
        DELETE FROM template_variables
        WHERE template_id = ? AND variable_key = ?
        """,
        (template_id, variable_key),
    )
    return cursor.rowcount


def activate_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
    activated_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_sets
        SET status = 'active', activated_at = ?, retired_at = NULL
        WHERE id = ? AND status = 'draft'
        """,
        (activated_at, template_set_id),
    )
    return cursor.rowcount


def retire_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
    retired_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_sets
        SET status = 'retired', retired_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (retired_at, template_set_id),
    )
    return cursor.rowcount


def reject_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_sets
        SET status = 'rejected'
        WHERE id = ? AND status = 'draft'
        """,
        (template_set_id,),
    )
    return cursor.rowcount
