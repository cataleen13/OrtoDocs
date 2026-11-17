"""Operații SQL pentru configurarea, maparea și istoricul sincronizării DOCX.

Funcțiile primesc conexiunea SQLite de la apelant. Nu deschid conexiuni și nu
încep sau finalizează tranzacții; coordonarea tranzacțiilor aparține
stratului services.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


SYNC_FOLDER_SETTING_KEY = "template_sync_folder"


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_app_setting(
    connection: sqlite3.Connection,
    setting_key: str,
) -> str | None:
    """Returnează valoarea configurată în app_settings sau None dacă lipsește."""

    row = connection.execute(
        "SELECT value FROM app_settings WHERE setting_key = ?",
        (setting_key,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def upsert_app_setting(
    connection: sqlite3.Connection,
    setting_key: str,
    value: str,
) -> None:
    """Salvează sau actualizează o cheie de configurare în app_settings."""

    connection.execute(
        """
        INSERT INTO app_settings (setting_key, value)
        VALUES (?, ?)
        ON CONFLICT(setting_key) DO UPDATE SET value = excluded.value
        """,
        (setting_key, value),
    )


def delete_app_setting(
    connection: sqlite3.Connection,
    setting_key: str,
) -> None:
    """Elimină o cheie de configurare din app_settings."""

    connection.execute(
        "DELETE FROM app_settings WHERE setting_key = ?",
        (setting_key,),
    )


def get_sync_entity_by_key(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    catalog_key: str,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT entity_type, catalog_key, diagnosis_id, intervention_id,
                   created_at, updated_at
            FROM template_sync_entities
            WHERE entity_type = ? AND catalog_key = ?
            """,
            (entity_type, catalog_key),
        ).fetchone()
    )


def get_sync_entity_by_owner(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    owner_id: int,
) -> dict[str, Any] | None:
    if entity_type == "diagnosis":
        query = """
            SELECT entity_type, catalog_key, diagnosis_id, intervention_id,
                   created_at, updated_at
            FROM template_sync_entities
            WHERE entity_type = 'diagnosis' AND diagnosis_id = ?
        """
    else:
        query = """
            SELECT entity_type, catalog_key, diagnosis_id, intervention_id,
                   created_at, updated_at
            FROM template_sync_entities
            WHERE entity_type = 'intervention' AND intervention_id = ?
        """
    return _as_dict(connection.execute(query, (owner_id,)).fetchone())


def insert_sync_entity(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    catalog_key: str,
    diagnosis_id: int | None = None,
    intervention_id: int | None = None,
    timestamp: str | None = None,
) -> None:
    if timestamp is None:
        connection.execute(
            """
            INSERT INTO template_sync_entities (
                entity_type, catalog_key, diagnosis_id, intervention_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (entity_type, catalog_key, diagnosis_id, intervention_id),
        )
        return
    connection.execute(
        """
        INSERT INTO template_sync_entities (
            entity_type, catalog_key, diagnosis_id, intervention_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_type, catalog_key, diagnosis_id, intervention_id, timestamp, timestamp),
    )


def touch_sync_entity(
    connection: sqlite3.Connection,
    *,
    entity_type: str,
    catalog_key: str,
    updated_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE template_sync_entities
        SET updated_at = ?
        WHERE entity_type = ? AND catalog_key = ?
        """,
        (updated_at, entity_type, catalog_key),
    )
    return cursor.rowcount


def insert_sync_source(
    connection: sqlite3.Connection,
    *,
    template_version_id: int,
    catalog_key: str,
    relative_path: str,
    synced_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO template_sync_sources (
            template_version_id, catalog_key, relative_path, synced_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (template_version_id, catalog_key, relative_path, synced_at),
    )


def upsert_sync_source(
    connection: sqlite3.Connection,
    *,
    template_version_id: int,
    catalog_key: str,
    relative_path: str,
    synced_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO template_sync_sources (
            template_version_id, catalog_key, relative_path, synced_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(template_version_id) DO UPDATE SET
            catalog_key = excluded.catalog_key,
            relative_path = excluded.relative_path,
            synced_at = excluded.synced_at
        """,
        (template_version_id, catalog_key, relative_path, synced_at),
    )


def get_sync_source_for_version(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT template_version_id, catalog_key, relative_path, synced_at
            FROM template_sync_sources
            WHERE template_version_id = ?
            """,
            (template_version_id,),
        ).fetchone()
    )


def list_sync_sources_for_versions(
    connection: sqlite3.Connection,
    version_ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    if not version_ids:
        return {}
    placeholders = ", ".join("?" for _ in version_ids)
    rows = connection.execute(
        f"""
        SELECT template_version_id, catalog_key, relative_path, synced_at
        FROM template_sync_sources
        WHERE template_version_id IN ({placeholders})
        """,
        tuple(int(item) for item in version_ids),
    ).fetchall()
    return {int(row["template_version_id"]): dict(row) for row in rows}


def insert_sync_run(
    connection: sqlite3.Connection,
    *,
    folder_path: str,
    status: str,
    scanned_count: int,
    new_count: int,
    modified_count: int,
    unchanged_count: int,
    imported_count: int,
    activated_count: int,
    backup_path: str | None,
    summary_message: str,
    created_at: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO template_sync_runs (
            folder_path, status, scanned_count, new_count, modified_count,
            unchanged_count, imported_count, activated_count, backup_path,
            summary_message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            folder_path,
            status,
            scanned_count,
            new_count,
            modified_count,
            unchanged_count,
            imported_count,
            activated_count,
            backup_path,
            summary_message,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def get_latest_sync_run(
    connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    return _as_dict(
        connection.execute(
            """
            SELECT id, folder_path, status, scanned_count, new_count,
                   modified_count, unchanged_count, imported_count,
                   activated_count, backup_path, summary_message, created_at
            FROM template_sync_runs
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    )


def count_active_independent_templates(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT document_type, COUNT(*) AS total
        FROM independent_template_versions
        WHERE status = 'active'
        GROUP BY document_type
        """
    ).fetchall()
    counts = {
        "total": 0,
        "epicrisis_recommendations": 0,
        "operative_protocol": 0,
    }
    for row in rows:
        doc_type = str(row["document_type"])
        total = int(row["total"])
        if doc_type in counts:
            counts[doc_type] = total
        counts["total"] += total
    return counts
