"""Operații SQL pentru biblioteca medicală OrtoDocs.

Funcțiile din acest modul primesc o conexiune deja deschisă. Validarea
parametrilor și administrarea tranzacțiilor aparțin stratului services.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_diagnoses(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM diagnoses"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY sort_order, name_key, id"
    return [dict(row) for row in connection.execute(query).fetchall()]


def get_diagnosis(
    connection: sqlite3.Connection,
    diagnosis_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            "SELECT * FROM diagnoses WHERE id = ?",
            (diagnosis_id,),
        ).fetchone()
    )


def insert_diagnosis(
    connection: sqlite3.Connection,
    *,
    name: str,
    name_key: str,
    code: str | None,
    sort_order: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO diagnoses (name, name_key, code, sort_order)
        VALUES (?, ?, ?, ?)
        """,
        (name, name_key, code, sort_order),
    )
    return int(cursor.lastrowid)


def update_diagnosis(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    *,
    name: str,
    name_key: str,
    code: str | None,
) -> int:
    cursor = connection.execute(
        """
        UPDATE diagnoses
        SET name = ?, name_key = ?, code = ?
        WHERE id = ?
        """,
        (name, name_key, code, diagnosis_id),
    )
    return cursor.rowcount


def set_diagnosis_sort_order(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    sort_order: int,
) -> int:
    cursor = connection.execute(
        "UPDATE diagnoses SET sort_order = ? WHERE id = ?",
        (sort_order, diagnosis_id),
    )
    return cursor.rowcount


def set_diagnosis_active(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    is_active: bool,
) -> int:
    cursor = connection.execute(
        "UPDATE diagnoses SET is_active = ? WHERE id = ?",
        (int(is_active), diagnosis_id),
    )
    return cursor.rowcount


def list_interventions(
    connection: sqlite3.Connection,
    *,
    diagnosis_id: int | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM interventions WHERE 1 = 1"
    parameters: list[int] = []
    if diagnosis_id is not None:
        query += " AND diagnosis_id = ?"
        parameters.append(diagnosis_id)
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY sort_order, name_key, id"
    return [
        dict(row)
        for row in connection.execute(query, parameters).fetchall()
    ]


def get_intervention(
    connection: sqlite3.Connection,
    intervention_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            "SELECT * FROM interventions WHERE id = ?",
            (intervention_id,),
        ).fetchone()
    )


def insert_intervention(
    connection: sqlite3.Connection,
    *,
    diagnosis_id: int,
    name: str,
    name_key: str,
    code: str | None,
    sort_order: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO interventions (
            diagnosis_id, name, name_key, code, sort_order
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (diagnosis_id, name, name_key, code, sort_order),
    )
    return int(cursor.lastrowid)


def update_intervention(
    connection: sqlite3.Connection,
    intervention_id: int,
    *,
    name: str,
    name_key: str,
    code: str | None,
) -> int:
    cursor = connection.execute(
        """
        UPDATE interventions
        SET name = ?, name_key = ?, code = ?
        WHERE id = ?
        """,
        (name, name_key, code, intervention_id),
    )
    return cursor.rowcount


def set_intervention_sort_order(
    connection: sqlite3.Connection,
    intervention_id: int,
    sort_order: int,
) -> int:
    cursor = connection.execute(
        "UPDATE interventions SET sort_order = ? WHERE id = ?",
        (sort_order, intervention_id),
    )
    return cursor.rowcount


def set_intervention_active(
    connection: sqlite3.Connection,
    intervention_id: int,
    is_active: bool,
) -> int:
    cursor = connection.execute(
        "UPDATE interventions SET is_active = ? WHERE id = ?",
        (int(is_active), intervention_id),
    )
    return cursor.rowcount


def list_field_definitions(
    connection: sqlite3.Connection,
    *,
    variable_key: str | None = None,
    scope: str | None = None,
    current_only: bool = False,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM field_definitions WHERE 1 = 1"
    parameters: list[Any] = []
    if variable_key is not None:
        query += " AND variable_key = ?"
        parameters.append(variable_key)
    if scope is not None:
        query += " AND scope = ?"
        parameters.append(scope)
    if current_only:
        query += " AND is_current = 1"
    query += " ORDER BY variable_key, version"
    return [
        dict(row)
        for row in connection.execute(query, parameters).fetchall()
    ]


def get_field_definition(
    connection: sqlite3.Connection,
    field_definition_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            "SELECT * FROM field_definitions WHERE id = ?",
            (field_definition_id,),
        ).fetchone()
    )


def get_field_definition_version(
    connection: sqlite3.Connection,
    variable_key: str,
    version: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT * FROM field_definitions
            WHERE variable_key = ? AND version = ?
            """,
            (variable_key, version),
        ).fetchone()
    )


def get_latest_field_definition(
    connection: sqlite3.Connection,
    variable_key: str,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT * FROM field_definitions
            WHERE variable_key = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (variable_key,),
        ).fetchone()
    )


def insert_field_definition(
    connection: sqlite3.Connection,
    *,
    variable_key: str,
    version: int,
    label: str,
    data_type: str,
    scope: str,
    diagnosis_id: int | None,
    intervention_id: int | None,
    is_current: bool,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO field_definitions (
            variable_key, version, label, data_type, scope,
            diagnosis_id, intervention_id, is_current
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variable_key,
            version,
            label,
            data_type,
            scope,
            diagnosis_id,
            intervention_id,
            int(is_current),
        ),
    )
    return int(cursor.lastrowid)


def list_field_options(
    connection: sqlite3.Connection,
    field_definition_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT field_definition_id, option_value, option_label, sort_order
            FROM field_options
            WHERE field_definition_id = ?
            ORDER BY sort_order, option_value
            """,
            (field_definition_id,),
        ).fetchall()
    ]


def insert_field_option(
    connection: sqlite3.Connection,
    *,
    field_definition_id: int,
    option_value: str,
    option_label: str,
    sort_order: int,
) -> None:
    connection.execute(
        """
        INSERT INTO field_options (
            field_definition_id, option_value, option_label, sort_order
        )
        VALUES (?, ?, ?, ?)
        """,
        (field_definition_id, option_value, option_label, sort_order),
    )
