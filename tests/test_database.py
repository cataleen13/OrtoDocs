"""Teste pentru conexiunile, schema și operațiile tranzacționale SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Proiectul folosește layout-ul src/ și nu instalăm pachetul în mediul virtual.
SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.storage.database import (
    DatabaseIntegrityError,
    connect_database,
    get_database_path,
    retain_latest_generation_sessions,
    save_generation_session,
    switch_current_field_version,
    write_transaction,
)
from ortodocs.storage import migrations as migrations_module
from ortodocs.storage.migrations import (
    LATEST_SCHEMA_VERSION,
    MigrationError,
    MigrationIntegrityError,
    SCHEMA_V1_SQL,
    SCHEMA_V2_SQL,
    SCHEMA_V3_SQL,
    create_database_backup,
    UnversionedDatabaseError,
    UnsupportedDatabaseVersionError,
    initialize_database,
    migrate_connection,
)


EPICRISIS = "epicrisis_recommendations"
PROTOCOL = "operative_protocol"
RICH_TEXT = '{"schema_version":1,"paragraphs":[]}'


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "ortodocs-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def insert_diagnosis(
        self,
        name: str = "Fractură col femural",
        *,
        is_active: int = 1,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO diagnoses (name, name_key, is_active)
            VALUES (?, ?, ?)
            """,
            (name, name.casefold().replace(" ", "_"), is_active),
        )
        return int(cursor.lastrowid)

    def insert_intervention(
        self,
        diagnosis_id: int,
        name: str = "Proteza bipolară",
        *,
        is_active: int = 1,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO interventions
                (diagnosis_id, name, name_key, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (diagnosis_id, name, name.casefold().replace(" ", "_"), is_active),
        )
        return int(cursor.lastrowid)

    def insert_field(
        self,
        variable_key: str = "data_interventiei",
        version: int = 1,
        *,
        is_current: int | None = None,
    ) -> int:
        if is_current is None:
            is_current = 1 if version == 1 else 0
        cursor = self.connection.execute(
            """
            INSERT INTO field_definitions
                (variable_key, version, label, data_type, scope, is_current)
            VALUES (?, ?, ?, 'date', 'common', ?)
            """,
            (variable_key, version, variable_key.replace("_", " ").title(), is_current),
        )
        return int(cursor.lastrowid)

    def insert_template(
        self,
        template_set_id: int,
        document_type: str,
        *,
        validation_status: str = "pending",
    ) -> int:
        source = f"temporary-template-{template_set_id}-{document_type}".encode()
        source_hash = hashlib.sha256(source).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO templates (
                template_set_id, document_type, source_docx, source_sha256,
                parsed_content_json, validation_status, parser_version
            )
            VALUES (?, ?, ?, ?, '{"paragraphs":[]}', ?, 'test-parser-1')
            """,
            (template_set_id, document_type, source, source_hash, validation_status),
        )
        return int(cursor.lastrowid)

    def insert_valid_template(self, template_set_id: int, document_type: str) -> int:
        template_id = self.insert_template(
            template_set_id,
            document_type,
            validation_status="pending",
        )
        self.connection.execute(
            """
            UPDATE templates
            SET validation_status = 'valid', validation_report_json = '{}'
            WHERE id = ?
            """,
            (template_id,),
        )
        return template_id

    def insert_draft_set(self, intervention_id: int, version_no: int = 1) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO template_sets (intervention_id, version_no)
            VALUES (?, ?)
            """,
            (intervention_id, version_no),
        )
        return int(cursor.lastrowid)

    def add_valid_template_pair(self, template_set_id: int) -> dict[str, int]:
        return {
            EPICRISIS: self.insert_valid_template(template_set_id, EPICRISIS),
            PROTOCOL: self.insert_valid_template(template_set_id, PROTOCOL),
        }

    def activate_set(self, template_set_id: int) -> None:
        self.connection.execute(
            """
            UPDATE template_sets
            SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z'
            WHERE id = ?
            """,
            (template_set_id,),
        )

    def create_active_package(
        self,
        *,
        diagnosis_active: int = 1,
        intervention_active: int = 1,
    ) -> tuple[int, int, int, dict[str, int]]:
        diagnosis_id = self.insert_diagnosis(is_active=diagnosis_active)
        intervention_id = self.insert_intervention(
            diagnosis_id,
            is_active=intervention_active,
        )
        template_set_id = self.insert_draft_set(intervention_id)
        template_ids = self.add_valid_template_pair(template_set_id)
        self.activate_set(template_set_id)
        return diagnosis_id, intervention_id, template_set_id, template_ids

    def insert_building_session(
        self,
        diagnosis_id: int,
        intervention_id: int,
        template_set_id: int,
        *,
        created_at: str | None = None,
    ) -> int:
        if created_at is None:
            cursor = self.connection.execute(
                """
                INSERT INTO generation_sessions (
                    diagnosis_id, intervention_id, template_set_id,
                    diagnosis_label_snapshot, intervention_label_snapshot
                )
                VALUES (?, ?, ?, 'Test diagnosis', 'Test intervention')
                """,
                (diagnosis_id, intervention_id, template_set_id),
            )
        else:
            cursor = self.connection.execute(
                """
                INSERT INTO generation_sessions (
                    diagnosis_id, intervention_id, template_set_id,
                    diagnosis_label_snapshot, intervention_label_snapshot, created_at
                )
                VALUES (?, ?, ?, 'Test diagnosis', 'Test intervention', ?)
                """,
                (diagnosis_id, intervention_id, template_set_id, created_at),
            )
        return int(cursor.lastrowid)

    def insert_document(
        self,
        session_id: int,
        template_set_id: int,
        document_type: str,
        template_id: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO generated_documents (
                session_id, template_set_id, document_type,
                template_id, rich_text_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, template_set_id, document_type, template_id, RICH_TEXT),
        )

    def complete_session(
        self,
        diagnosis_id: int,
        intervention_id: int,
        template_set_id: int,
        template_ids: dict[str, int],
        *,
        created_at: str | None = None,
    ) -> int:
        documents = {
            EPICRISIS: (template_ids[EPICRISIS], RICH_TEXT),
            PROTOCOL: (template_ids[PROTOCOL], RICH_TEXT),
        }
        return save_generation_session(
            self.connection,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
            template_set_id=template_set_id,
            diagnosis_label_snapshot="Test diagnosis",
            intervention_label_snapshot="Test intervention",
            documents=documents,
            created_at=created_at,
        )

    def test_database_creation_and_schema_version(self) -> None:
        self.assertTrue(self.database_path.is_file())
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, LATEST_SCHEMA_VERSION)

        table_names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "diagnoses",
                "interventions",
                "field_definitions",
                "field_options",
                "template_sets",
                "templates",
                "template_variables",
                "generation_sessions",
                "generated_documents",
                "app_settings",
            }.issubset(table_names)
        )

    def test_foreign_keys_are_enabled_on_every_connection(self) -> None:
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            1,
        )
        second_connection = connect_database(self.database_path)
        try:
            self.assertEqual(
                second_connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
        finally:
            second_connection.close()

    def test_database_path_uses_localappdata_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            local_app_data = Path(temporary_directory) / "LocalAppData"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                database_path = get_database_path()
            self.assertEqual(
                database_path,
                local_app_data / "OrtoDocs" / "ortodocs.db",
            )
            self.assertFalse(local_app_data.exists())

    def test_initialization_is_idempotent_and_preserves_data(self) -> None:
        diagnosis_id = self.insert_diagnosis("Diagnostic păstrat")
        initialize_database(self.database_path)
        row = self.connection.execute(
            "SELECT name FROM diagnoses WHERE id = ?",
            (diagnosis_id,),
        ).fetchone()
        self.assertEqual(row["name"], "Diagnostic păstrat")
        self.assertEqual(
            self.connection.execute("PRAGMA user_version").fetchone()[0],
            LATEST_SCHEMA_VERSION,
        )

    def test_refuses_database_with_newer_version(self) -> None:
        self.connection.execute("PRAGMA user_version = 99")
        self.connection.close()
        with self.assertRaises(UnsupportedDatabaseVersionError):
            initialize_database(self.database_path)

    def test_refuses_unversioned_database_with_existing_tables(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy_connection = connect_database(legacy_path)
        legacy_connection.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)")
        legacy_connection.close()
        with self.assertRaises(UnversionedDatabaseError):
            initialize_database(legacy_path)

    def test_failed_migration_rolls_back_partial_schema(self) -> None:
        rollback_path = Path(self.temp_dir.name) / "rollback.db"
        rollback_connection = connect_database(rollback_path)
        broken_migration = """
            CREATE TABLE partial_table (id INTEGER PRIMARY KEY);
            CREATE TABL invalid_statement (id INTEGER);
        """
        try:
            with patch.object(
                migrations_module,
                "MIGRATIONS",
                {0: (1, broken_migration)},
            ):
                with self.assertRaises(MigrationError):
                    migrate_connection(rollback_connection)

            self.assertFalse(rollback_connection.in_transaction)
            self.assertEqual(
                rollback_connection.execute("PRAGMA user_version").fetchone()[0],
                0,
            )
            self.assertIsNone(
                rollback_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'partial_table'"
                ).fetchone()
            )
        finally:
            rollback_connection.close()

    def test_write_transaction_rolls_back_on_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            with write_transaction(self.connection):
                self.insert_diagnosis("Diagnostic temporar")
                raise KeyboardInterrupt

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0],
            0,
        )

    def test_migration_rolls_back_on_keyboard_interrupt(self) -> None:
        rollback_path = Path(self.temp_dir.name) / "keyboard-interrupt.db"
        rollback_connection = connect_database(rollback_path)
        try:
            with patch.object(
                migrations_module,
                "_verify_schema_objects",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_connection(rollback_connection)

            self.assertFalse(rollback_connection.in_transaction)
            self.assertEqual(
                rollback_connection.execute("PRAGMA user_version").fetchone()[0],
                0,
            )
            self.assertIsNone(
                rollback_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'diagnoses'"
                ).fetchone()
            )
        finally:
            rollback_connection.close()

    def test_sqlite_backup_contains_a_consistent_database_copy(self) -> None:
        diagnosis_id = self.insert_diagnosis("Diagnostic pentru backup")
        backup_path = create_database_backup(self.connection, self.database_path)
        self.assertTrue(backup_path.is_file())
        backup_connection = connect_database(backup_path)
        try:
            self.assertEqual(
                backup_connection.execute("PRAGMA user_version").fetchone()[0],
                LATEST_SCHEMA_VERSION,
            )
            self.assertEqual(
                backup_connection.execute(
                    "SELECT name FROM diagnoses WHERE id = ?",
                    (diagnosis_id,),
                ).fetchone()[0],
                "Diagnostic pentru backup",
            )
            self.assertEqual(
                backup_connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            backup_connection.close()

    def test_foreign_key_check_and_integrity_check(self) -> None:
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(migrate_connection(self.connection), LATEST_SCHEMA_VERSION)

    def test_initialized_schema_requires_all_protection_triggers(self) -> None:
        self.connection.execute("DROP TRIGGER generation_session_insert_guard")
        with self.assertRaisesRegex(MigrationIntegrityError, "triggere"):
            migrate_connection(self.connection)

    def test_initialized_schema_requires_essential_unique_index(self) -> None:
        self.connection.execute("DROP INDEX ux_field_definitions_one_current")
        with self.assertRaisesRegex(MigrationIntegrityError, "indexul"):
            migrate_connection(self.connection)

    def test_initialized_schema_requires_active_package_unique_index(self) -> None:
        self.connection.execute(
            "DROP INDEX ux_template_sets_one_active_per_intervention"
        )
        with self.assertRaisesRegex(MigrationIntegrityError, "indexul"):
            migrate_connection(self.connection)

    def test_diagnoses_interventions_and_foreign_key_deletion_rules(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO interventions (diagnosis_id, name, name_key) VALUES (999, 'x', 'x')"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM diagnoses WHERE id = ?", (diagnosis_id,))
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT id FROM interventions WHERE id = ?",
                (intervention_id,),
            ).fetchone()
        )

    def test_duplicate_diagnosis_and_intervention_names_are_rejected(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        diagnosis_name_key = self.connection.execute(
            "SELECT name_key FROM diagnoses WHERE id = ?",
            (diagnosis_id,),
        ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO diagnoses (name, name_key) VALUES ('Dublură', ?)",
                (diagnosis_name_key,),
            )
        self.insert_intervention(diagnosis_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO interventions (diagnosis_id, name, name_key)
                VALUES (?, 'Altă etichetă', ?)
                """,
                (
                    diagnosis_id,
                    self.connection.execute(
                        "SELECT name_key FROM interventions WHERE diagnosis_id = ?",
                        (diagnosis_id,),
                    ).fetchone()[0],
                ),
            )

    def test_field_versions_are_unique_and_can_be_switched_back(self) -> None:
        first_id = self.insert_field(version=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_field(version=1)
        second_id = self.insert_field(version=2)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE field_definitions SET is_current = 1 WHERE id = ?",
                (second_id,),
            )

        switch_current_field_version(self.connection, "data_interventiei", 2)
        current = self.connection.execute(
            """
            SELECT id, version
            FROM field_definitions
            WHERE variable_key = 'data_interventiei' AND is_current = 1
            """
        ).fetchall()
        self.assertEqual([(row["id"], row["version"]) for row in current], [(second_id, 2)])

        switch_current_field_version(self.connection, "data_interventiei", 1)
        current = self.connection.execute(
            """
            SELECT id, version
            FROM field_definitions
            WHERE variable_key = 'data_interventiei' AND is_current = 1
            """
        ).fetchall()
        self.assertEqual([(row["id"], row["version"]) for row in current], [(first_id, 1)])

    def test_missing_target_field_version_rolls_back_without_changing_current(self) -> None:
        first_id = self.insert_field(version=1)
        self.insert_field(version=2)
        with self.assertRaises(DatabaseIntegrityError):
            switch_current_field_version(self.connection, "data_interventiei", 99)
        current = self.connection.execute(
            "SELECT id FROM field_definitions WHERE variable_key = ? AND is_current = 1",
            ("data_interventiei",),
        ).fetchone()
        self.assertEqual(current["id"], first_id)

    def test_template_insert_is_pending_then_can_be_validated(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        template_set_id = self.insert_draft_set(intervention_id)
        template_id = self.insert_template(template_set_id, EPICRISIS)
        self.assertEqual(
            self.connection.execute(
                "SELECT validation_status FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()[0],
            "pending",
        )
        self.connection.execute(
            "UPDATE templates SET validation_status = 'valid' WHERE id = ?",
            (template_id,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT validation_status FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()[0],
            "valid",
        )

    def test_direct_template_insert_as_valid_is_rejected(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        template_set_id = self.insert_draft_set(intervention_id)
        source = b"temporary docx placeholder"
        source_hash = hashlib.sha256(source).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_status, parser_version
                )
                VALUES (?, ?, ?, ?, '{"paragraphs":[]}', 'valid', 'test-parser-1')
                """,
                (template_set_id, EPICRISIS, source, source_hash),
            )

    def test_direct_active_template_set_insert_is_rejected(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO template_sets (
                    intervention_id, version_no, status, activated_at
                )
                VALUES (?, 1, 'active', '2026-01-01T00:00:00.000Z')
                """,
                (intervention_id,),
            )

    def test_valid_template_pair_can_activate(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        template_set_id = self.insert_draft_set(intervention_id)
        self.add_valid_template_pair(template_set_id)
        self.activate_set(template_set_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "active",
        )

    def test_second_active_package_for_same_intervention_is_rejected(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id)
        first_set = self.insert_draft_set(intervention_id, version_no=1)
        self.add_valid_template_pair(first_set)
        self.activate_set(first_set)

        second_set = self.insert_draft_set(intervention_id, version_no=2)
        self.add_valid_template_pair(second_set)
        with self.assertRaises(sqlite3.IntegrityError):
            self.activate_set(second_set)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (first_set,),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (second_set,),
            ).fetchone()[0],
            "draft",
        )

    def test_activation_is_rejected_for_inactive_diagnosis(self) -> None:
        diagnosis_id = self.insert_diagnosis(is_active=0)
        intervention_id = self.insert_intervention(diagnosis_id)
        template_set_id = self.insert_draft_set(intervention_id)
        self.add_valid_template_pair(template_set_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.activate_set(template_set_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "draft",
        )

    def test_activation_is_rejected_for_inactive_intervention(self) -> None:
        diagnosis_id = self.insert_diagnosis()
        intervention_id = self.insert_intervention(diagnosis_id, is_active=0)
        template_set_id = self.insert_draft_set(intervention_id)
        self.add_valid_template_pair(template_set_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.activate_set(template_set_id)

    def test_session_insert_is_rejected_for_inactive_diagnosis(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        self.connection.execute(
            "UPDATE diagnoses SET is_active = 0 WHERE id = ?",
            (diagnosis_id,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "active",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_building_session(diagnosis_id, intervention_id, template_set_id)

        self.connection.execute(
            "UPDATE diagnoses SET is_active = 1 WHERE id = ?",
            (diagnosis_id,),
        )
        session_id = self.complete_session(
            diagnosis_id,
            intervention_id,
            template_set_id,
            template_ids,
        )
        self.assertGreater(session_id, 0)

    def test_session_insert_is_rejected_for_inactive_intervention(self) -> None:
        diagnosis_id, intervention_id, template_set_id, _ = self.create_active_package()
        self.connection.execute(
            "UPDATE interventions SET is_active = 0 WHERE id = ?",
            (intervention_id,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_building_session(diagnosis_id, intervention_id, template_set_id)

    def test_session_requires_two_correctly_associated_documents(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        session_id = self.insert_building_session(diagnosis_id, intervention_id, template_set_id)
        self.insert_document(session_id, template_set_id, EPICRISIS, template_ids[EPICRISIS])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE generation_sessions SET status = 'complete' WHERE id = ?",
                (session_id,),
            )

        # Tipul diferă de documentul deja inserat, astfel încât testăm cheia
        # străină compusă și nu cheia primară (session_id, document_type).
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
            self.insert_document(session_id, template_set_id, PROTOCOL, template_ids[EPICRISIS])

        self.insert_document(session_id, template_set_id, PROTOCOL, template_ids[PROTOCOL])
        self.connection.execute(
            "UPDATE generation_sessions SET status = 'complete' WHERE id = ?",
            (session_id,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            2,
        )

    def test_completed_document_content_can_be_edited(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        session_id = self.complete_session(
            diagnosis_id,
            intervention_id,
            template_set_id,
            template_ids,
        )
        updated_json = '{"schema_version":1,"paragraphs":[{"runs":[]}]}'
        self.connection.execute(
            """
            UPDATE generated_documents
            SET rich_text_json = ?, edited_at = '2026-02-01T12:00:00.000Z'
            WHERE session_id = ? AND document_type = ?
            """,
            (updated_json, session_id, EPICRISIS),
        )
        row = self.connection.execute(
            """
            SELECT rich_text_json, edited_at
            FROM generated_documents
            WHERE session_id = ? AND document_type = ?
            """,
            (session_id, EPICRISIS),
        ).fetchone()
        self.assertEqual(row["rich_text_json"], updated_json)
        self.assertEqual(row["edited_at"], "2026-02-01T12:00:00.000Z")

    def test_app_settings_value_update_refreshes_timestamp(self) -> None:
        old_timestamp = "2000-01-01T00:00:00.000Z"
        self.connection.execute(
            """
            INSERT INTO app_settings (setting_key, value, updated_at)
            VALUES ('clipboard_format', 'rtf', ?)
            """,
            (old_timestamp,),
        )
        self.connection.execute(
            "UPDATE app_settings SET value = 'unicode' WHERE setting_key = 'clipboard_format'"
        )
        row = self.connection.execute(
            "SELECT value, updated_at FROM app_settings WHERE setting_key = 'clipboard_format'"
        ).fetchone()
        self.assertEqual(row["value"], "unicode")
        self.assertNotEqual(row["updated_at"], old_timestamp)

    def test_completed_documents_cannot_be_deleted_individually(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        session_id = self.complete_session(
            diagnosis_id,
            intervention_id,
            template_set_id,
            template_ids,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM generated_documents WHERE session_id = ? AND document_type = ?",
                (session_id, EPICRISIS),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            2,
        )

    def test_deleting_session_cascades_to_both_documents(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        session_id = self.complete_session(
            diagnosis_id,
            intervention_id,
            template_set_id,
            template_ids,
        )
        self.connection.execute(
            "UPDATE generation_sessions SET status = 'deleting' WHERE id = ?",
            (session_id,),
        )
        self.connection.execute("DELETE FROM generation_sessions WHERE id = ?", (session_id,))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generation_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()[0],
            0,
        )

    def test_retention_keeps_exactly_ten_complete_sessions_and_twenty_documents(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session_ids = []
        for index in range(12):
            created_at = (base_time + timedelta(seconds=index)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            session_ids.append(
                self.complete_session(
                    diagnosis_id,
                    intervention_id,
                    template_set_id,
                    template_ids,
                    created_at=created_at,
                )
            )

        # Generarea prin stratul de persistență aplică retenția în aceeași
        # tranzacție după fiecare sesiune nouă.
        self.assertEqual(retain_latest_generation_sessions(self.connection, limit=10), 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generation_sessions WHERE status = 'complete'"
            ).fetchone()[0],
            10,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            20,
        )
        remaining_ids = [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM generation_sessions ORDER BY created_at, id"
            )
        ]
        self.assertEqual(remaining_ids, session_ids[2:])

    def test_generation_rolls_back_if_first_document_insert_fails(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        existing_id = self.complete_session(
            diagnosis_id,
            intervention_id,
            template_set_id,
            template_ids,
        )
        self.connection.execute(
            """
            CREATE TRIGGER fail_first_generation_document
            BEFORE INSERT ON generated_documents
            BEGIN
                SELECT RAISE(ABORT, 'injected failure after session insert');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
            )

        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT id FROM generation_sessions ORDER BY id"
            )],
            [existing_id],
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            2,
        )

    def test_generation_rolls_back_if_second_document_insert_fails(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        self.connection.execute(
            """
            CREATE TRIGGER fail_second_generation_document
            BEFORE INSERT ON generated_documents
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'injected second document failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "second document"):
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )

    def test_generation_rolls_back_if_marking_session_complete_fails(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        self.connection.execute(
            """
            CREATE TRIGGER fail_generation_completion
            BEFORE UPDATE OF status ON generation_sessions
            WHEN NEW.status = 'complete'
            BEGIN
                SELECT RAISE(ABORT, 'injected completion failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "completion failure"):
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )

    def test_generation_and_retention_roll_back_if_retention_fails(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        existing_ids = [
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
                created_at=(base_time + timedelta(seconds=index)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            )
            for index in range(10)
        ]
        self.connection.execute(
            """
            CREATE TRIGGER fail_session_retention
            BEFORE DELETE ON generation_sessions
            WHEN OLD.status = 'deleting'
            BEGIN
                SELECT RAISE(ABORT, 'injected retention failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retention failure"):
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
                created_at="2026-01-02T00:00:00.000000Z",
            )

        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT id FROM generation_sessions ORDER BY id"
            )],
            existing_ids,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generation_sessions WHERE status = 'complete'"
            ).fetchone()[0],
            10,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            20,
        )

    def test_generation_rolls_back_if_retention_would_remove_new_session(self) -> None:
        diagnosis_id, intervention_id, template_set_id, template_ids = self.create_active_package()
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        existing_ids = [
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
                created_at=(base_time + timedelta(seconds=index)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            )
            for index in range(10)
        ]

        with self.assertRaisesRegex(DatabaseIntegrityError, "nu a rămas completă"):
            self.complete_session(
                diagnosis_id,
                intervention_id,
                template_set_id,
                template_ids,
                created_at="2025-01-01T00:00:00.000000Z",
            )

        self.assertEqual(
            [row[0] for row in self.connection.execute(
                "SELECT id FROM generation_sessions ORDER BY id"
            )],
            existing_ids,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            20,
        )


class IndependentTemplateMigrationTests(unittest.TestCase):
    """Teste pentru migrarea aditivă v1 → v2 pe baze temporare."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "ortodocs-v1.db"
        self.connection = connect_database(self.database_path)
        migrations_module._apply_migration(self.connection, 1, SCHEMA_V1_SQL)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_diagnosis(self, name: str, *, active: int = 1) -> int:
        cursor = self.connection.execute(
            "INSERT INTO diagnoses (name, name_key, is_active) VALUES (?, ?, ?)",
            (name, name.casefold().replace(" ", "_"), active),
        )
        return int(cursor.lastrowid)

    def add_intervention(
        self,
        diagnosis_id: int,
        name: str,
        *,
        active: int = 1,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO interventions (diagnosis_id, name, name_key, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (diagnosis_id, name, name.casefold().replace(" ", "_"), active),
        )
        return int(cursor.lastrowid)

    def add_active_legacy_set(
        self,
        intervention_id: int,
        *,
        version_no: int,
        epicrisis: bytes,
        protocol: bytes | None = None,
        patient_reviewed: bool = True,
        epicrisis_warning: bool = False,
        protocol_warning: bool = False,
        protocol_validation_status: str = "valid",
        source_hash_overrides: dict[str, str] | None = None,
        activate: bool = True,
    ) -> tuple[int, dict[str, int], dict[str, bytes]]:
        activated_at = f"2026-01-{version_no:02d}T10:00:00.000Z"
        package_report = {
            "patient_identification_review": {
                "confirmed": patient_reviewed,
                "confirmed_at": activated_at if patient_reviewed else None,
            }
        }
        set_cursor = self.connection.execute(
            """
            INSERT INTO template_sets (intervention_id, version_no, import_report_json)
            VALUES (?, ?, ?)
            """,
            (intervention_id, version_no, json.dumps(package_report)),
        )
        template_set_id = int(set_cursor.lastrowid)
        source_bytes = {
            EPICRISIS: epicrisis,
            PROTOCOL: protocol or f"protocol-{intervention_id}-{version_no}".encode(),
        }
        template_ids: dict[str, int] = {}
        for document_type, source_docx in source_bytes.items():
            parser_status = "valid_with_warnings" if (
                epicrisis_warning if document_type == EPICRISIS else protocol_warning
            ) else "valid"
            warning_confirmed = parser_status == "valid_with_warnings"
            report = {
                "import_report": {
                    "status": parser_status,
                    "variables": {},
                    "paragraph_count": 1,
                },
                "warning_confirmation": {
                    "required": parser_status == "valid_with_warnings",
                    "confirmed": warning_confirmed,
                    "confirmed_at": activated_at if warning_confirmed else None,
                },
                "validation": {"status": "valid", "issues": []},
            }
            source_sha256 = (
                (source_hash_overrides or {}).get(document_type)
                or hashlib.sha256(source_docx).hexdigest()
            )
            template_cursor = self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_status, validation_report_json,
                    parser_version, imported_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, 'docx-parser-test', ?)
                """,
                (
                    template_set_id,
                    document_type,
                    source_docx,
                    source_sha256,
                    RICH_TEXT,
                    json.dumps(report),
                    activated_at,
                ),
            )
            template_id = int(template_cursor.lastrowid)
            validation_status = (
                protocol_validation_status
                if document_type == PROTOCOL
                else "valid"
            )
            self.connection.execute(
                "UPDATE templates SET validation_status = ? WHERE id = ?",
                (validation_status, template_id),
            )
            template_ids[document_type] = template_id

        if activate:
            self.connection.execute(
                """
                UPDATE template_sets
                SET status = 'active', activated_at = ?
                WHERE id = ?
                """,
                (activated_at, template_set_id),
            )
        return template_set_id, template_ids, source_bytes

    def snapshot_legacy_data(self) -> dict[str, list[tuple[object, ...]]]:
        tables = (
            "diagnoses",
            "interventions",
            "field_definitions",
            "field_options",
            "template_sets",
            "templates",
            "template_variables",
            "generation_sessions",
            "generated_documents",
            "app_settings",
        )
        return {
            table: [
                tuple(row)
                for row in self.connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                )
            ]
            for table in tables
        }

    def assert_hash_mismatch_rolls_back(
        self,
        *,
        template_id: int,
        document_type: str,
    ) -> None:
        before = self.snapshot_legacy_data()
        with self.assertRaisesRegex(
            MigrationError,
            rf"documentul legacy {template_id} \({document_type}\)",
        ):
            migrate_connection(self.connection)

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='independent_template_versions'"
            ).fetchone()
        )
        self.assertEqual(self.snapshot_legacy_data(), before)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def add_independent_draft(
        self,
        *,
        owner_type: str,
        owner_id: int,
        document_type: str,
        version_no: int,
    ) -> int:
        source_docx = f"draft-{owner_type}-{owner_id}-{version_no}".encode()
        cursor = self.connection.execute(
            """
            INSERT INTO independent_template_versions (
                owner_type, diagnosis_id, intervention_id, document_type,
                version_no, source_docx, source_sha256, parsed_content_json,
                parser_report_json, parser_version, validation_status,
                patient_identification_reviewed,
                patient_identification_reviewed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test-parser', 'valid', 1, ?)
            """,
            (
                owner_type,
                owner_id if owner_type == "diagnosis" else None,
                owner_id if owner_type == "intervention" else None,
                document_type,
                version_no,
                source_docx,
                hashlib.sha256(source_docx).hexdigest(),
                RICH_TEXT,
                '{"status":"valid","variables":{}}',
                "2026-01-01T00:00:00.000Z",
            ),
        )
        return int(cursor.lastrowid)

    def test_migrates_active_protocols_and_one_identical_epicrisis_per_diagnosis(self) -> None:
        diagnosis_id = self.add_diagnosis("Fractură comună")
        first_intervention = self.add_intervention(diagnosis_id, "Intervenția A")
        second_intervention = self.add_intervention(diagnosis_id, "Intervenția B")
        shared_epicrisis = b"same-original-epicrisis-docx"
        first_set, first_templates, first_sources = self.add_active_legacy_set(
            first_intervention,
            version_no=1,
            epicrisis=shared_epicrisis,
            protocol=b"protocol-A",
            protocol_warning=True,
        )
        second_set, second_templates, second_sources = self.add_active_legacy_set(
            second_intervention,
            version_no=1,
            epicrisis=shared_epicrisis,
            protocol=b"protocol-B",
        )
        original_diagnoses = self.connection.execute(
            "SELECT id, name, is_active FROM diagnoses ORDER BY id"
        ).fetchall()
        original_interventions = self.connection.execute(
            "SELECT id, diagnosis_id, name, is_active FROM interventions ORDER BY id"
        ).fetchall()

        self.assertEqual(migrate_connection(self.connection), LATEST_SCHEMA_VERSION)
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT id, name, is_active FROM diagnoses ORDER BY id"
            )],
            [tuple(row) for row in original_diagnoses],
        )
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT id, diagnosis_id, name, is_active FROM interventions ORDER BY id"
            )],
            [tuple(row) for row in original_interventions],
        )
        migrated = self.connection.execute(
            """
            SELECT owner_type, diagnosis_id, intervention_id, document_type,
                   version_no, status, source_docx, source_sha256,
                   parsed_content_json, parser_report_json, parser_version,
                   warnings_confirmed, warnings_confirmed_at,
                   patient_identification_reviewed,
                   patient_identification_reviewed_at, imported_at,
                   activated_at, legacy_template_id
            FROM independent_template_versions
            ORDER BY owner_type, diagnosis_id, intervention_id
            """
        ).fetchall()
        diagnosis_templates = [row for row in migrated if row["owner_type"] == "diagnosis"]
        intervention_templates = [row for row in migrated if row["owner_type"] == "intervention"]

        self.assertEqual(len(diagnosis_templates), 1)
        self.assertEqual(diagnosis_templates[0]["diagnosis_id"], diagnosis_id)
        self.assertIsNone(diagnosis_templates[0]["intervention_id"])
        self.assertEqual(diagnosis_templates[0]["document_type"], EPICRISIS)
        self.assertEqual(diagnosis_templates[0]["version_no"], 1)
        self.assertEqual(diagnosis_templates[0]["status"], "active")
        self.assertEqual(diagnosis_templates[0]["source_docx"], shared_epicrisis)
        self.assertEqual(
            diagnosis_templates[0]["source_sha256"],
            hashlib.sha256(shared_epicrisis).hexdigest(),
        )
        self.assertEqual(diagnosis_templates[0]["parsed_content_json"], RICH_TEXT)
        self.assertEqual(
            json.loads(diagnosis_templates[0]["parser_report_json"])["status"],
            "valid",
        )
        self.assertEqual(diagnosis_templates[0]["parser_version"], "docx-parser-test")
        self.assertEqual(
            diagnosis_templates[0]["legacy_template_id"],
            first_templates[EPICRISIS],
        )

        self.assertEqual(len(intervention_templates), 2)
        by_owner = {row["intervention_id"]: row for row in intervention_templates}
        self.assertEqual(set(by_owner), {first_intervention, second_intervention})
        for intervention_id, template_id, source_bytes in (
            (first_intervention, first_templates[PROTOCOL], first_sources[PROTOCOL]),
            (second_intervention, second_templates[PROTOCOL], second_sources[PROTOCOL]),
        ):
            row = by_owner[intervention_id]
            self.assertEqual(row["document_type"], PROTOCOL)
            self.assertIsNone(row["diagnosis_id"])
            self.assertEqual(row["version_no"], 1)
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["source_docx"], source_bytes)
            self.assertEqual(
                row["source_sha256"], hashlib.sha256(source_bytes).hexdigest()
            )
            self.assertEqual(row["legacy_template_id"], template_id)
            self.assertEqual(row["patient_identification_reviewed"], 1)
            self.assertTrue(row["patient_identification_reviewed_at"])
            self.assertTrue(row["imported_at"])
            self.assertTrue(row["activated_at"])
            if intervention_id == first_intervention:
                self.assertEqual(row["warnings_confirmed"], 1)
                self.assertTrue(row["warnings_confirmed_at"])

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM template_sets WHERE id IN (?, ?)",
                (first_set, second_set),
            ).fetchone()[0],
            2,
        )

    def test_epicrises_with_same_stored_hash_but_different_bytes_abort_migration(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic cu hash epicriză inconsistent")
        first_intervention = self.add_intervention(diagnosis_id, "Intervenția A")
        second_intervention = self.add_intervention(diagnosis_id, "Intervenția B")
        first_epicrisis = b"epicrisis-original-A"
        second_epicrisis = b"epicrisis-original-B"
        reused_hash = hashlib.sha256(first_epicrisis).hexdigest()
        self.add_active_legacy_set(
            first_intervention,
            version_no=1,
            epicrisis=first_epicrisis,
        )
        _, second_templates, _ = self.add_active_legacy_set(
            second_intervention,
            version_no=1,
            epicrisis=second_epicrisis,
            source_hash_overrides={EPICRISIS: reused_hash},
        )

        self.assert_hash_mismatch_rolls_back(
            template_id=second_templates[EPICRISIS],
            document_type=EPICRISIS,
        )

    def test_protocol_with_mismatched_stored_hash_aborts_migration(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic cu hash protocol inconsistent")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenția A")
        protocol = b"protocol-original"
        actual_hash = hashlib.sha256(protocol).hexdigest()
        incorrect_hash = "0" * 64 if actual_hash != "0" * 64 else "1" * 64
        _, templates, _ = self.add_active_legacy_set(
            intervention_id,
            version_no=1,
            epicrisis=b"epicrisis-valid",
            protocol=protocol,
            source_hash_overrides={PROTOCOL: incorrect_hash},
        )

        self.assert_hash_mismatch_rolls_back(
            template_id=templates[PROTOCOL],
            document_type=PROTOCOL,
        )

    def test_different_epicrises_are_not_selected_and_legacy_sources_remain_intact(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic cu variante")
        first_intervention = self.add_intervention(diagnosis_id, "Intervenția A")
        second_intervention = self.add_intervention(diagnosis_id, "Intervenția B")
        first_set, first_templates, first_sources = self.add_active_legacy_set(
            first_intervention,
            version_no=1,
            epicrisis=b"epicrisis-version-A",
        )
        second_set, second_templates, second_sources = self.add_active_legacy_set(
            second_intervention,
            version_no=1,
            epicrisis=b"epicrisis-version-B",
        )

        migrate_connection(self.connection)
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM independent_template_versions
                WHERE owner_type = 'diagnosis' AND diagnosis_id = ?
                """,
                (diagnosis_id,),
            ).fetchone()[0],
            0,
        )
        for template_set_id, template_ids, source_bytes in (
            (first_set, first_templates, first_sources),
            (second_set, second_templates, second_sources),
        ):
            for document_type, template_id in template_ids.items():
                original = self.connection.execute(
                    """
                    SELECT template_set_id, document_type, source_docx, source_sha256
                    FROM templates WHERE id = ?
                    """,
                    (template_id,),
                ).fetchone()
                self.assertEqual(original["template_set_id"], template_set_id)
                self.assertEqual(original["document_type"], document_type)
                self.assertEqual(original["source_docx"], source_bytes[document_type])
                self.assertEqual(
                    original["source_sha256"],
                    hashlib.sha256(source_bytes[document_type]).hexdigest(),
                )

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM template_sets WHERE status = 'active'"
            ).fetchone()[0],
            2,
        )

    def test_contexts_without_valid_templates_do_not_receive_invented_rows(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic fără șabloane")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenție fără șabloane")
        migrate_connection(self.connection)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM independent_template_versions"
            ).fetchone()[0],
            0,
        )
        self.assertIsNotNone(
            self.connection.execute("SELECT id FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT id FROM interventions WHERE id = ?", (intervention_id,)
            ).fetchone()
        )

    def test_only_valid_legacy_documents_are_migrated(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic cu document invalid")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenție")
        template_set_id, template_ids, _ = self.add_active_legacy_set(
            intervention_id,
            version_no=1,
            epicrisis=b"valid-epicrisis",
            protocol_validation_status="invalid",
            activate=False,
        )
        migrate_connection(self.connection)
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM independent_template_versions
                WHERE owner_type = 'intervention' AND intervention_id = ?
                """,
                (intervention_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT validation_status FROM templates WHERE id = ?",
                (template_ids[PROTOCOL],),
            ).fetchone()[0],
            "invalid",
        )

    def test_owner_document_pairing_foreign_keys_versions_and_active_uniqueness(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic pentru constrângeri")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenție pentru constrângeri")
        migrate_connection(self.connection)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json,
                    parser_report_json, parser_version
                ) VALUES ('diagnosis', ?, 'operative_protocol', 1, X'01', ?, ?, ?, 'test')
                """,
                (diagnosis_id, "0" * 64, RICH_TEXT, '{"status":"valid"}'),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json,
                    parser_report_json, parser_version
                ) VALUES ('diagnosis', 99999, ?, 1, X'01', ?, ?, ?, 'test')
                """,
                (EPICRISIS, "0" * 64, RICH_TEXT, '{"status":"valid"}'),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, intervention_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json,
                    parser_report_json, parser_version
                ) VALUES ('intervention', ?, 'epicrisis_recommendations', 1,
                          X'01', ?, ?, ?, 'test')
                """,
                (intervention_id, "0" * 64, RICH_TEXT, '{"status":"valid"}'),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json,
                    parser_report_json, parser_version,
                    patient_identification_reviewed
                ) VALUES ('diagnosis', ?, ?, 1, X'01', ?, ?, ?, 'test', 1)
                """,
                (diagnosis_id, EPICRISIS, "0" * 64, RICH_TEXT, '{"status":"valid"}'),
            )

        first_diagnosis_version = self.add_independent_draft(
            owner_type="diagnosis",
            owner_id=diagnosis_id,
            document_type=EPICRISIS,
            version_no=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_independent_draft(
                owner_type="diagnosis",
                owner_id=diagnosis_id,
                document_type=EPICRISIS,
                version_no=1,
            )
        second_diagnosis_version = self.add_independent_draft(
            owner_type="diagnosis",
            owner_id=diagnosis_id,
            document_type=EPICRISIS,
            version_no=2,
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", first_diagnosis_version),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE independent_template_versions SET status = 'active', activated_at = ? WHERE id = ?",
                ("2026-01-02T00:00:00.000Z", second_diagnosis_version),
            )

        first_protocol = self.add_independent_draft(
            owner_type="intervention",
            owner_id=intervention_id,
            document_type=PROTOCOL,
            version_no=1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_independent_draft(
                owner_type="intervention",
                owner_id=intervention_id,
                document_type=PROTOCOL,
                version_no=1,
            )
        second_protocol = self.add_independent_draft(
            owner_type="intervention",
            owner_id=intervention_id,
            document_type=PROTOCOL,
            version_no=2,
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00.000Z", first_protocol),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE independent_template_versions SET status = 'active', activated_at = ? WHERE id = ?",
                ("2026-01-02T00:00:00.000Z", second_protocol),
            )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_migration_is_idempotent_and_preserves_history(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic cu istoric")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenție cu istoric")
        template_set_id, template_ids, _ = self.add_active_legacy_set(
            intervention_id,
            version_no=1,
            epicrisis=b"history-epicrisis",
            protocol=b"history-protocol",
        )
        session_id = save_generation_session(
            self.connection,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
            template_set_id=template_set_id,
            diagnosis_label_snapshot="Snapshot diagnostic",
            intervention_label_snapshot="Snapshot intervenție",
            documents={
                EPICRISIS: (template_ids[EPICRISIS], RICH_TEXT),
                PROTOCOL: (template_ids[PROTOCOL], RICH_TEXT),
            },
        )
        before_documents = self.connection.execute(
            "SELECT document_type, template_id, rich_text_json, edited_at FROM generated_documents WHERE session_id = ? ORDER BY document_type",
            (session_id,),
        ).fetchall()
        before_sets = self.connection.execute(
            "SELECT COUNT(*) FROM template_sets"
        ).fetchone()[0]

        migrate_connection(self.connection, database_path=self.database_path)
        migrated_count = self.connection.execute(
            "SELECT COUNT(*) FROM independent_template_versions"
        ).fetchone()[0]
        self.assertEqual(migrated_count, 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM generation_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0],
            "complete",
        )
        self.assertEqual(
            [tuple(row) for row in before_documents],
            [tuple(row) for row in self.connection.execute(
                "SELECT document_type, template_id, rich_text_json, edited_at FROM generated_documents WHERE session_id = ? ORDER BY document_type",
                (session_id,),
            )],
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM template_sets").fetchone()[0], before_sets)

        # Rularea repetată a scriptului v2 nu dublează versiunile copiate.
        migrations_module._apply_migration(self.connection, 2, SCHEMA_V2_SQL)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM independent_template_versions"
            ).fetchone()[0],
            migrated_count,
        )
        self.assertEqual(migrate_connection(self.connection), LATEST_SCHEMA_VERSION)

    def test_migration_error_rolls_back_schema_and_copied_rows(self) -> None:
        diagnosis_id = self.add_diagnosis("Diagnostic pentru rollback")
        intervention_id = self.add_intervention(diagnosis_id, "Intervenție pentru rollback")
        template_set_id, _, _ = self.add_active_legacy_set(
            intervention_id,
            version_no=1,
            epicrisis=b"rollback-epicrisis",
        )
        failing_sql = SCHEMA_V2_SQL + """
            INSERT INTO independent_template_versions (
                owner_type, diagnosis_id, document_type, version_no,
                source_docx, source_sha256, parsed_content_json,
                parser_report_json, parser_version
            ) VALUES ('diagnosis', 99999, 'epicrisis_recommendations', 99,
                      X'01', '0000000000000000000000000000000000000000000000000000000000000000',
                      '{"schema_version":1,"paragraphs":[]}', '{"status":"valid"}', 'test');
        """
        with patch.object(
            migrations_module,
            "MIGRATIONS",
            {1: (2, failing_sql)},
        ):
            with self.assertRaises(MigrationError):
                migrate_connection(self.connection, database_path=self.database_path)

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='independent_template_versions'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM template_sets WHERE id = ?", (template_set_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM templates").fetchone()[0],
            2,
        )


class SchemaV3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "ortodocs-v2-to-v3.db"
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)
        self.connection.executescript(SCHEMA_V1_SQL)
        self.connection.execute("PRAGMA user_version = 1")
        with (
            patch.object(migrations_module, "LATEST_SCHEMA_VERSION", 2),
            patch.object(migrations_module, "MIGRATIONS", {1: (2, SCHEMA_V2_SQL)}),
        ):
            migrate_connection(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def tearDown(self) -> None:
        pass

    def _add_diagnosis(self, name: str = "Gonartroză") -> int:
        cursor = self.connection.execute(
            "INSERT INTO diagnoses (name, name_key, is_active) VALUES (?, ?, 1)",
            (name, name.lower()),
        )
        return int(cursor.lastrowid)

    def _add_intervention(self, diagnosis_id: int, name: str = "Proteză totală") -> int:
        cursor = self.connection.execute(
            "INSERT INTO interventions (diagnosis_id, name, name_key, is_active) VALUES (?, ?, ?, 1)",
            (diagnosis_id, name, name.lower()),
        )
        return int(cursor.lastrowid)

    def test_empty_v2_database_migrates_cleanly_to_v3(self) -> None:
        migrate_connection(self.connection, database_path=self.database_path)
        self.assertEqual(
            self.connection.execute("PRAGMA user_version").fetchone()[0],
            LATEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            self.connection.execute("PRAGMA integrity_check").fetchone()[0],
            "ok",
        )
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_key_check").fetchall(),
            [],
        )
        index_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_independent_templates_id_doctype'"
        ).fetchone()
        self.assertIsNotNone(index_exists)
        columns = {
            row["name"]: row
            for row in self.connection.execute("PRAGMA table_info(generated_documents)")
        }
        self.assertIn("independent_template_version_id", columns)
        self.assertEqual(columns["template_set_id"]["notnull"], 0)
        self.assertEqual(columns["template_id"]["notnull"], 0)

    def test_populated_v2_database_preserves_legacy_history_and_independent_templates(self) -> None:
        diagnosis_id = self._add_diagnosis()
        intervention_id = self._add_intervention(diagnosis_id)
        set_id = int(
            self.connection.execute(
                """
                INSERT INTO template_sets (intervention_id, version_no, status, import_report_json)
                VALUES (?, 1, 'draft', '{}')
                """,
                (intervention_id,),
            ).lastrowid
        )
        epi_bytes = b"legacy-epi-docx"
        proto_bytes = b"legacy-proto-docx"
        epi_id = int(
            self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_report_json, parser_version, validation_status
                ) VALUES (?, ?, ?, ?, ?, '{"import_report":{"status":"valid","variables":{}}}', 'v1', 'pending')
                """,
                (set_id, EPICRISIS, epi_bytes, hashlib.sha256(epi_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        proto_id = int(
            self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_report_json, parser_version, validation_status
                ) VALUES (?, ?, ?, ?, ?, '{"import_report":{"status":"valid","variables":{}}}', 'v1', 'pending')
                """,
                (set_id, PROTOCOL, proto_bytes, hashlib.sha256(proto_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE templates SET validation_status = 'valid' WHERE id IN (?, ?)",
            (epi_id, proto_id),
        )
        self.connection.execute(
            "UPDATE template_sets SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (set_id,),
        )
        legacy_session_id = save_generation_session(
            self.connection,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
            template_set_id=set_id,
            diagnosis_label_snapshot="Gonartroză",
            intervention_label_snapshot="Proteză totală",
            documents={
                EPICRISIS: (epi_id, '{"schema_version":1,"paragraphs":[{"inlines":[{"text":"Epicriză veche"}]}]}'),
                PROTOCOL: (proto_id, '{"schema_version":1,"paragraphs":[{"inlines":[{"text":"Protocol vechi"}]}]}'),
            },
        )
        self.connection.execute(
            """
            INSERT INTO independent_template_versions (
                owner_type, diagnosis_id, document_type, version_no, status,
                source_docx, source_sha256, parsed_content_json, parser_report_json,
                parser_version, validation_status
            ) VALUES ('diagnosis', ?, ?, 1, 'draft', ?, ?, ?, '{"status":"valid","variables":{}}', 'v1', 'pending')
            """,
            (diagnosis_id, EPICRISIS, epi_bytes, hashlib.sha256(epi_bytes).hexdigest(), RICH_TEXT),
        )

        with patch.object(migrations_module, "LATEST_SCHEMA_VERSION", 3):
            migrate_connection(self.connection, database_path=self.database_path)

        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        migrated_session = self.connection.execute(
            "SELECT template_set_id, status FROM generation_sessions WHERE id = ?",
            (legacy_session_id,),
        ).fetchone()
        self.assertEqual(migrated_session["template_set_id"], set_id)
        self.assertEqual(migrated_session["status"], "complete")

        docs = self.connection.execute(
            """
            SELECT document_type, template_set_id, template_id, independent_template_version_id, rich_text_json
            FROM generated_documents WHERE session_id = ? ORDER BY document_type
            """,
            (legacy_session_id,),
        ).fetchall()
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["template_id"], epi_id)
        self.assertIsNone(docs[0]["independent_template_version_id"])
        self.assertIn("Epicriză veche", docs[0]["rich_text_json"])
        self.assertEqual(docs[1]["template_id"], proto_id)
        self.assertIsNone(docs[1]["independent_template_version_id"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM independent_template_versions").fetchone()[0],
            1,
        )

    def test_v3_constraints_enforce_exclusivity_and_matching_document_types(self) -> None:
        migrate_connection(self.connection, database_path=self.database_path)
        diagnosis_id = self._add_diagnosis()
        intervention_id = self._add_intervention(diagnosis_id)

        epi_ver = int(
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json, parser_report_json,
                    parser_version, validation_status, patient_identification_reviewed,
                    patient_identification_reviewed_at
                ) VALUES ('diagnosis', ?, ?, 1, X'01', ?, ?, '{"status":"valid","variables":{}}', 'v1', 'valid', 1, '2026-01-01T00:00:00.000Z')
                """,
                (diagnosis_id, EPICRISIS, hashlib.sha256(b"\x01").hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (epi_ver,),
        )
        proto_ver = int(
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, intervention_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json, parser_report_json,
                    parser_version, validation_status, patient_identification_reviewed,
                    patient_identification_reviewed_at
                ) VALUES ('intervention', ?, ?, 1, X'02', ?, ?, '{"status":"valid","variables":{}}', 'v1', 'valid', 1, '2026-01-01T00:00:00.000Z')
                """,
                (intervention_id, PROTOCOL, hashlib.sha256(b"\x02").hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (proto_ver,),
        )

        # Missing active protocol blocks session creation if protocol is retired.
        session_id = save_generation_session(
            self.connection,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
            template_set_id=None,
            diagnosis_label_snapshot="Gonartroză",
            intervention_label_snapshot="Proteză totală",
            documents={
                EPICRISIS: (epi_ver, RICH_TEXT),
                PROTOCOL: (proto_ver, RICH_TEXT),
            },
        )
        self.assertGreater(session_id, 0)

        # Direct insert with wrong document_type against independent_template_version_id fails.
        building_id = int(
            self.connection.execute(
                """
                INSERT INTO generation_sessions (
                    diagnosis_id, intervention_id, template_set_id,
                    diagnosis_label_snapshot, intervention_label_snapshot
                ) VALUES (?, ?, NULL, 'D', 'I')
                """,
                (diagnosis_id, intervention_id),
            ).lastrowid
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO generated_documents (
                    session_id, template_set_id, document_type,
                    template_id, independent_template_version_id, rich_text_json
                ) VALUES (?, NULL, ?, NULL, ?, ?)
                """,
                (building_id, PROTOCOL, epi_ver, RICH_TEXT),
            )
        # Mode exclusivity rejects both NULL.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO generated_documents (
                    session_id, template_set_id, document_type,
                    template_id, independent_template_version_id, rich_text_json
                ) VALUES (?, NULL, ?, NULL, NULL, ?)
                """,
                (building_id, EPICRISIS, RICH_TEXT),
            )

    def test_v2_to_v3_migration_rolls_back_cleanly_on_error(self) -> None:
        diagnosis_id = self._add_diagnosis("Diagnostic rollback V3")
        intervention_id = self._add_intervention(diagnosis_id, "Intervenție rollback V3")
        failing_v3_sql = migrations_module.SCHEMA_V3_SQL + """
            INSERT INTO independent_template_versions (
                owner_type, diagnosis_id, document_type, version_no,
                source_docx, source_sha256, parsed_content_json,
                parser_report_json, parser_version
            ) VALUES ('diagnosis', 99999, 'epicrisis_recommendations', 99,
                      X'01', '0000000000000000000000000000000000000000000000000000000000000000',
                      '{}', '{}', 'test');
        """
        with patch.object(migrations_module, "MIGRATIONS", {2: (3, failing_v3_sql)}):
            with self.assertRaises(MigrationError):
                migrate_connection(self.connection, database_path=self.database_path)

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            self.connection.execute("SELECT name FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()[0],
            "Diagnostic rollback V3",
        )
        self.assertEqual(
            self.connection.execute("SELECT name FROM interventions WHERE id = ?", (intervention_id,)).fetchone()[0],
            "Intervenție rollback V3",
        )

    def test_retain_latest_generation_sessions_affects_only_legacy_and_preserves_v3_sessions(
        self,
    ) -> None:
        migrate_connection(self.connection, database_path=self.database_path)
        diagnosis_id = self._add_diagnosis("Gonartroză retenție")
        intervention_id = self._add_intervention(diagnosis_id, "Proteză retenție")

        set_id = int(
            self.connection.execute(
                """
                INSERT INTO template_sets (intervention_id, version_no, status, import_report_json)
                VALUES (?, 1, 'draft', '{}')
                """,
                (intervention_id,),
            ).lastrowid
        )
        epi_bytes = b"legacy-epi-ret"
        proto_bytes = b"legacy-proto-ret"
        epi_id = int(
            self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_report_json, parser_version, validation_status
                ) VALUES (?, ?, ?, ?, ?, '{"import_report":{"status":"valid","variables":{}}}', 'v1', 'pending')
                """,
                (set_id, EPICRISIS, epi_bytes, hashlib.sha256(epi_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        proto_id = int(
            self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_report_json, parser_version, validation_status
                ) VALUES (?, ?, ?, ?, ?, '{"import_report":{"status":"valid","variables":{}}}', 'v1', 'pending')
                """,
                (set_id, PROTOCOL, proto_bytes, hashlib.sha256(proto_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE templates SET validation_status = 'valid' WHERE id IN (?, ?)",
            (epi_id, proto_id),
        )
        self.connection.execute(
            "UPDATE template_sets SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (set_id,),
        )

        epi_ver = int(
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json, parser_report_json,
                    parser_version, validation_status, patient_identification_reviewed,
                    patient_identification_reviewed_at
                ) VALUES ('diagnosis', ?, ?, 1, ?, ?, ?, '{"status":"valid","variables":{}}', 'v1', 'valid', 1, '2026-01-01T00:00:00.000Z')
                """,
                (diagnosis_id, EPICRISIS, epi_bytes, hashlib.sha256(epi_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (epi_ver,),
        )
        proto_ver = int(
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, intervention_id, document_type, version_no,
                    source_docx, source_sha256, parsed_content_json, parser_report_json,
                    parser_version, validation_status, patient_identification_reviewed,
                    patient_identification_reviewed_at
                ) VALUES ('intervention', ?, ?, 1, ?, ?, ?, '{"status":"valid","variables":{}}', 'v1', 'valid', 1, '2026-01-01T00:00:00.000Z')
                """,
                (intervention_id, PROTOCOL, proto_bytes, hashlib.sha256(proto_bytes).hexdigest(), RICH_TEXT),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (proto_ver,),
        )

        legacy_ids: list[int] = []
        for idx in range(1, 6):
            legacy_ids.append(
                save_generation_session(
                    self.connection,
                    diagnosis_id=diagnosis_id,
                    intervention_id=intervention_id,
                    template_set_id=set_id,
                    diagnosis_label_snapshot="Gonartroză retenție",
                    intervention_label_snapshot="Proteză retenție",
                    created_at=f"2026-01-01T10:00:{idx:02d}.000Z",
                    documents={
                        EPICRISIS: (epi_id, RICH_TEXT),
                        PROTOCOL: (proto_id, RICH_TEXT),
                    },
                )
            )

        v3_ids: list[int] = []
        for idx in range(1, 13):
            day = "01" if idx <= 6 else "02"
            v3_ids.append(
                save_generation_session(
                    self.connection,
                    diagnosis_id=diagnosis_id,
                    intervention_id=intervention_id,
                    template_set_id=None,
                    diagnosis_label_snapshot="Gonartroză retenție",
                    intervention_label_snapshot="Proteză retenție",
                    created_at=f"2026-01-{day}T09:00:{idx:02d}.000Z",
                    documents={
                        EPICRISIS: (epi_ver, RICH_TEXT),
                        PROTOCOL: (proto_ver, RICH_TEXT),
                    },
                )
            )

        removed = retain_latest_generation_sessions(self.connection, limit=3)
        self.assertEqual(removed, 2)

        remaining_legacy = [
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM generation_sessions WHERE template_set_id IS NOT NULL ORDER BY created_at ASC, id ASC"
            ).fetchall()
        ]
        self.assertEqual(remaining_legacy, legacy_ids[2:])

        remaining_v3 = [
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM generation_sessions WHERE template_set_id IS NULL ORDER BY created_at ASC, id ASC"
            ).fetchall()
        ]
        self.assertEqual(remaining_v3, v3_ids)

        v3_docs_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM generated_documents
            WHERE independent_template_version_id IS NOT NULL
            """
        ).fetchone()[0]
        self.assertEqual(v3_docs_count, 24)

        legacy_docs_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM generated_documents
            WHERE template_set_id IS NOT NULL
            """
        ).fetchone()[0]
        self.assertEqual(legacy_docs_count, 6)


class SchemaV4MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "v4_test.db"
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)

    def _init_v2_database(self) -> None:
        self.connection.executescript(SCHEMA_V1_SQL)
        self.connection.execute("PRAGMA user_version = 1")
        with (
            patch.object(migrations_module, "LATEST_SCHEMA_VERSION", 2),
            patch.object(migrations_module, "MIGRATIONS", {1: (2, SCHEMA_V2_SQL)}),
        ):
            migrate_connection(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_v2_database_migrates_through_v3_to_v4_and_preserves_data(self) -> None:
        self._init_v2_database()
        diag_id = int(
            self.connection.execute(
                "INSERT INTO diagnoses (name, name_key, code) VALUES ('Diagnostic V2', 'diagnostic v2', 'D2')"
            ).lastrowid
        )
        interv_id = int(
            self.connection.execute(
                "INSERT INTO interventions (diagnosis_id, name, name_key, code) VALUES (?, 'Intervenție V2', 'interventie v2', 'I2')",
                (diag_id,),
            ).lastrowid
        )

        final_version = migrate_connection(self.connection, database_path=self.database_path)

        self.assertEqual(final_version, 4)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "template_sync_entities",
                "template_sync_sources",
                "template_sync_runs",
                "independent_template_versions",
                "app_settings",
            }.issubset(tables)
        )
        self.assertEqual(
            self.connection.execute("SELECT name FROM diagnoses WHERE id = ?", (diag_id,)).fetchone()[0],
            "Diagnostic V2",
        )
        self.assertEqual(
            self.connection.execute("SELECT name FROM interventions WHERE id = ?", (interv_id,)).fetchone()[0],
            "Intervenție V2",
        )

    def test_v3_database_migrates_to_v4_with_sync_constraints(self) -> None:
        self._init_v2_database()
        with patch.object(migrations_module, "LATEST_SCHEMA_VERSION", 3):
            migrate_connection(self.connection, database_path=self.database_path)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 3)

        diag_id = int(
            self.connection.execute(
                "INSERT INTO diagnoses (name, name_key, code) VALUES ('Gonartroză', 'gonartroza', 'M17')"
            ).lastrowid
        )
        ver_id = int(
            self.connection.execute(
                """
                INSERT INTO independent_template_versions (
                    owner_type, diagnosis_id, document_type, version_no, status,
                    source_docx, source_sha256, parsed_content_json, parser_report_json,
                    parser_version, validation_status, patient_identification_reviewed,
                    patient_identification_reviewed_at
                ) VALUES ('diagnosis', ?, 'epicrisis_recommendations', 1, 'draft', X'01', ?, ?, '{"status":"valid","variables":{}}', 'v1', 'valid', 1, '2026-01-01T00:00:00.000Z')
                """,
                (diag_id, hashlib.sha256(b"\x01").hexdigest(), '{"schema_version":1,"paragraphs":[{"inlines":[{"text":"Ok"}]}]}'),
            ).lastrowid
        )
        self.connection.execute(
            "UPDATE independent_template_versions SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
            (ver_id,),
        )

        final_version = migrate_connection(self.connection, database_path=self.database_path)
        self.assertEqual(final_version, 4)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        self.connection.execute(
            """
            INSERT INTO template_sync_entities (entity_type, diagnosis_id, catalog_key)
            VALUES ('diagnosis', ?, 'gonartroza')
            """,
            (diag_id,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO template_sync_entities (entity_type, diagnosis_id, catalog_key)
                VALUES ('diagnosis', ?, 'alta-cheie')
                """,
                (diag_id,),
            )

        self.connection.execute(
            """
            INSERT INTO template_sync_sources (
                template_version_id, catalog_key, relative_path, synced_at
            ) VALUES (?, 'gonartroza', 'epicrize/gonartroza.docx', '2026-11-16T10:00:00.000Z')
            """,
            (ver_id,),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT relative_path FROM template_sync_sources WHERE template_version_id = ?",
                (ver_id,),
            ).fetchone()[0],
            "epicrize/gonartroza.docx",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM independent_template_versions WHERE id = ?",
                (ver_id,),
            ).fetchone()[0],
            "active",
        )


if __name__ == "__main__":
    unittest.main()
