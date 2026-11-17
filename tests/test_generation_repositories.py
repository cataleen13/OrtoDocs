"""Teste pentru repository-ul istoricului și editarea documentelor generate."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import GenerationHistoryItem, GenerationSessionDetail
from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.storage.database import connect_database, save_generation_session
from ortodocs.storage.migrations import initialize_database
from ortodocs.storage.generation_repositories import (
    GeneratedDocumentNotFoundError,
    GenerationPersistenceError,
    GenerationSessionNotFoundError,
    GenerationSessionStateError,
    get_generation_session,
    list_generation_sessions,
    update_generated_document,
)


EPICRISIS = "epicrisis_recommendations"
PROTOCOL = "operative_protocol"
DOCUMENT_TYPES = (EPICRISIS, PROTOCOL)
EDITED_AT = "2026-11-10T08:30:00.123Z"


def make_document(text: str, *, bold: bool = False) -> RichDocument:
    return RichDocument(paragraphs=[Paragraph([TextInline(text, bold=bold)])])


class GenerationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "generation-repository-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = self._insert_diagnosis()
        self.intervention_id = self._insert_intervention(self.diagnosis_id)
        self.template_set_id, self.template_ids = self._create_active_template_set()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _insert_diagnosis(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO diagnoses (name, name_key) VALUES ('Diagnostic test', 'diagnostic_test')"
        )
        return int(cursor.lastrowid)

    def _insert_intervention(self, diagnosis_id: int) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO interventions (diagnosis_id, name, name_key)
            VALUES (?, 'Intervenție test', 'interventie_test')
            """,
            (diagnosis_id,),
        )
        return int(cursor.lastrowid)

    def _create_active_template_set(self) -> tuple[int, dict[str, int]]:
        set_cursor = self.connection.execute(
            "INSERT INTO template_sets (intervention_id, version_no) VALUES (?, 1)",
            (self.intervention_id,),
        )
        template_set_id = int(set_cursor.lastrowid)
        template_ids: dict[str, int] = {}
        for document_type in DOCUMENT_TYPES:
            source_docx = f"test-template:{document_type}".encode("utf-8")
            source_sha256 = hashlib.sha256(source_docx).hexdigest()
            cursor = self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_status, validation_report_json,
                    parser_version
                )
                VALUES (?, ?, ?, ?, ?, 'pending', '{}', 'test-parser-1')
                """,
                (
                    template_set_id,
                    document_type,
                    source_docx,
                    source_sha256,
                    RichDocument().to_json(),
                ),
            )
            template_ids[document_type] = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE templates SET validation_status = 'valid' WHERE id = ?",
                (cursor.lastrowid,),
            )
        self.connection.execute(
            """
            UPDATE template_sets
            SET status = 'active', activated_at = '2026-11-10T08:00:00.000Z'
            WHERE id = ?
            """,
            (template_set_id,),
        )
        return template_set_id, template_ids

    def create_session(
        self,
        *,
        created_at: str | None = None,
        epicrisis: RichDocument | None = None,
        protocol: RichDocument | None = None,
    ) -> int:
        documents = {
            EPICRISIS: (
                self.template_ids[EPICRISIS],
                (epicrisis or make_document("Epicriză inițială", bold=True)).to_json(),
            ),
            PROTOCOL: (
                self.template_ids[PROTOCOL],
                (protocol or make_document("Protocol inițial")).to_json(),
            ),
        }
        arguments = {
            "diagnosis_id": self.diagnosis_id,
            "intervention_id": self.intervention_id,
            "template_set_id": self.template_set_id,
            "diagnosis_label_snapshot": "Diagnostic la generare",
            "intervention_label_snapshot": "Intervenție la generare",
            "documents": documents,
        }
        if created_at is not None:
            arguments["created_at"] = created_at
        return save_generation_session(self.connection, **arguments)  # type: ignore[arg-type]

    def create_building_session(self) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO generation_sessions (
                diagnosis_id, intervention_id, template_set_id,
                diagnosis_label_snapshot, intervention_label_snapshot
            )
            VALUES (?, ?, ?, 'Diagnostic în lucru', 'Intervenție în lucru')
            """,
            (self.diagnosis_id, self.intervention_id, self.template_set_id),
        )
        return int(cursor.lastrowid)

    def document_rows(self, session_id: int) -> dict[str, tuple[int, str, str]]:
        rows = self.connection.execute(
            """
            SELECT document_type, template_id, rich_text_json, edited_at
            FROM generated_documents
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
        return {row[0]: (row[1], row[2], row[3]) for row in rows}

    def test_history_lists_complete_sessions_newest_first(self) -> None:
        older = self.create_session(created_at="2026-11-10T08:00:00.000Z")
        newest = self.create_session(created_at="2026-11-10T10:00:00.000Z")
        middle = self.create_session(created_at="2026-11-10T09:00:00.000Z")

        result = list_generation_sessions(self.connection)

        self.assertIsInstance(result, tuple)
        self.assertTrue(all(isinstance(item, GenerationHistoryItem) for item in result))
        self.assertEqual([item.session_id for item in result], [newest, middle, older])
        self.assertEqual(result[0].diagnosis_label_snapshot, "Diagnostic la generare")

    def test_history_uses_descending_session_id_for_identical_timestamps(self) -> None:
        timestamp = "2026-11-10T08:30:00.000Z"
        first = self.create_session(created_at=timestamp)
        second = self.create_session(created_at=timestamp)
        third = self.create_session(created_at=timestamp)

        self.assertEqual(
            [item.session_id for item in list_generation_sessions(self.connection)],
            [third, second, first],
        )

    def test_history_default_limit_returns_at_most_ten_recent_sessions(self) -> None:
        session_ids = [
            self.create_session(created_at=f"2026-11-10T08:{index:02d}:00.000Z")
            for index in range(12)
        ]

        result = list_generation_sessions(self.connection)

        self.assertEqual(len(result), 10)
        self.assertEqual([item.session_id for item in result], list(reversed(session_ids[-10:])))

    def test_history_rejects_invalid_limits(self) -> None:
        for limit in (0, -1, 11, True, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    list_generation_sessions(self.connection, limit=limit)  # type: ignore[arg-type]

    def test_history_excludes_building_and_deleting_sessions(self) -> None:
        building_id = self.create_building_session()
        deleting_id = self.create_session()
        self.connection.execute(
            "UPDATE generation_sessions SET status = 'deleting' WHERE id = ?",
            (deleting_id,),
        )

        self.assertEqual(list_generation_sessions(self.connection), ())
        self.assertIsNone(get_generation_session(self.connection, building_id))
        self.assertIsNone(get_generation_session(self.connection, deleting_id))

    def test_history_can_be_empty(self) -> None:
        self.assertEqual(list_generation_sessions(self.connection), ())

    def test_history_limit_can_be_lower_than_ten(self) -> None:
        session_ids = [self.create_session() for _ in range(4)]

        result = list_generation_sessions(self.connection, limit=2)

        self.assertEqual([item.session_id for item in result], list(reversed(session_ids[-2:])))

    def test_get_session_returns_both_documents_and_per_document_metadata(self) -> None:
        epicrisis = RichDocument(
            paragraphs=[
                Paragraph([TextInline("Epicriză ", bold=True), TextInline("editată", italic=True)]),
                Paragraph([TextInline("Recomandări", underline=True)]),
            ]
        )
        protocol = RichDocument(
            paragraphs=[Paragraph([TextInline("Protocol", bold=True), LineBreakInline(), TextInline("operator")])]
        )
        session_id = self.create_session(epicrisis=epicrisis, protocol=protocol)
        self.connection.execute(
            "UPDATE generated_documents SET edited_at = ? WHERE session_id = ? AND document_type = ?",
            ("2026-11-10T08:31:00.111Z", session_id, EPICRISIS),
        )
        self.connection.execute(
            "UPDATE generated_documents SET edited_at = ? WHERE session_id = ? AND document_type = ?",
            ("2026-11-10T08:32:00.222Z", session_id, PROTOCOL),
        )

        detail = get_generation_session(self.connection, session_id)

        self.assertIsInstance(detail, GenerationSessionDetail)
        assert detail is not None
        self.assertEqual(detail.history_item.session_id, session_id)
        self.assertEqual(detail.epicrisis_recommendations.to_dict(), epicrisis.to_dict())
        self.assertEqual(detail.operative_protocol.to_dict(), protocol.to_dict())
        self.assertEqual(detail.epicrisis_template_id, self.template_ids[EPICRISIS])
        self.assertEqual(detail.operative_template_id, self.template_ids[PROTOCOL])
        self.assertEqual(detail.epicrisis_edited_at, "2026-11-10T08:31:00.111Z")
        self.assertEqual(detail.operative_edited_at, "2026-11-10T08:32:00.222Z")
        self.assertIsNot(detail.epicrisis_recommendations, detail.operative_protocol)
        detail.epicrisis_recommendations.paragraphs[0].inlines[0].text = "Modificat în memorie"
        self.assertEqual(detail.operative_protocol.paragraphs[0].inlines[0].text, "Protocol")

    def test_get_session_rejects_invalid_persisted_edited_at_for_each_document(self) -> None:
        session_id = self.create_session()
        invalid_timestamps = (
            "not a date",
            "2026-02-30T08:30:00.123Z",
            "2026-11-10T08:30:00.123",
            "2026-11-10T08:30:00.123+02:00",
            "2026-11-10T08:30:00.12Z",
            "2026-11-10T08:30:00.1234Z",
        )

        for document_type in DOCUMENT_TYPES:
            for invalid_timestamp in invalid_timestamps:
                with self.subTest(
                    document_type=document_type,
                    edited_at=invalid_timestamp,
                ):
                    self.connection.execute(
                        "UPDATE generated_documents SET edited_at = ? WHERE session_id = ?",
                        (EDITED_AT, session_id),
                    )
                    self.connection.execute(
                        """
                        UPDATE generated_documents
                        SET edited_at = ?
                        WHERE session_id = ? AND document_type = ?
                        """,
                        (invalid_timestamp, session_id, document_type),
                    )
                    corrupted_rows = self.document_rows(session_id)

                    with self.assertRaisesRegex(
                        GenerationPersistenceError,
                        rf"{document_type}.*sesiunea {session_id}.*edited_at invalid",
                    ):
                        get_generation_session(self.connection, session_id)

                    self.assertEqual(self.document_rows(session_id), corrupted_rows)

    def test_mutating_retrieved_document_does_not_persist_to_database(self) -> None:
        original_text = "Saved epicrisis"
        session_id = self.create_session(epicrisis=make_document(original_text))

        first_detail = get_generation_session(self.connection, session_id)
        self.assertIsNotNone(first_detail)
        assert first_detail is not None
        first_detail.epicrisis_recommendations.paragraphs[0].inlines[0].text = (
            "Memory-only change"
        )

        second_detail = get_generation_session(self.connection, session_id)

        self.assertIsNotNone(second_detail)
        assert second_detail is not None
        self.assertEqual(
            second_detail.epicrisis_recommendations.paragraphs[0].inlines[0].text,
            original_text,
        )

    def test_get_session_returns_none_for_missing_or_incomplete_session(self) -> None:
        self.assertIsNone(get_generation_session(self.connection, 9999))
        incomplete_id = self.create_building_session()
        self.connection.execute(
            """
            INSERT INTO generated_documents (
                session_id, template_set_id, document_type, template_id, rich_text_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                incomplete_id,
                self.template_set_id,
                EPICRISIS,
                self.template_ids[EPICRISIS],
                make_document("Document parțial").to_json(),
            ),
        )
        self.assertIsNone(get_generation_session(self.connection, incomplete_id))

    def test_get_session_rejects_complete_session_missing_a_document(self) -> None:
        session_id = self.create_session()
        self.connection.execute("DROP TRIGGER generated_document_delete_guard")
        self.connection.execute(
            "DELETE FROM generated_documents WHERE session_id = ? AND document_type = ?",
            (session_id, PROTOCOL),
        )

        with self.assertRaisesRegex(GenerationPersistenceError, "exact ambele documente"):
            get_generation_session(self.connection, session_id)

    def test_get_session_reports_invalid_json_as_persistence_error(self) -> None:
        session_id = self.create_session()
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        try:
            self.connection.execute(
                "UPDATE generated_documents SET rich_text_json = '{' WHERE session_id = ? AND document_type = ?",
                (session_id, EPICRISIS),
            )
        finally:
            self.connection.execute("PRAGMA ignore_check_constraints = OFF")

        with self.assertRaisesRegex(GenerationPersistenceError, "JSON Rich Text invalid"):
            get_generation_session(self.connection, session_id)

    def test_get_session_reports_invalid_rich_text_structure_as_persistence_error(self) -> None:
        session_id = self.create_session()
        self.connection.execute(
            "UPDATE generated_documents SET rich_text_json = '{}' WHERE session_id = ? AND document_type = ?",
            (session_id, EPICRISIS),
        )

        with self.assertRaisesRegex(GenerationPersistenceError, "JSON Rich Text invalid"):
            get_generation_session(self.connection, session_id)

    def test_updates_both_document_types_independently_and_preserves_originals(self) -> None:
        original_epicrisis = make_document("Epicriză originală", bold=True)
        original_protocol = make_document("Protocol original")
        first_session = self.create_session(
            epicrisis=original_epicrisis,
            protocol=original_protocol,
        )
        other_session = self.create_session()
        first_before = self.document_rows(first_session)
        other_before = self.document_rows(other_session)
        templates_before = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT id, source_docx, source_sha256, parsed_content_json FROM templates ORDER BY id"
            )
        ]
        updated_epicrisis = RichDocument(
            paragraphs=[
                Paragraph([TextInline("Nou ", bold=True), TextInline("text", italic=True)]),
                Paragraph([TextInline("linie nouă", underline=True)]),
            ]
        )
        updated_protocol = RichDocument(
            paragraphs=[Paragraph([TextInline("Protocol nou", bold=True), LineBreakInline(), TextInline("B/I/U", underline=True)])]
        )
        epicrisis_before_call = updated_epicrisis.to_json()
        protocol_before_call = updated_protocol.to_json()

        update_generated_document(
            self.connection,
            first_session,
            EPICRISIS,
            updated_epicrisis,
            "2026-11-10T09:00:00.111Z",
        )
        after_epicrisis = self.document_rows(first_session)
        self.assertEqual(after_epicrisis[EPICRISIS][0], first_before[EPICRISIS][0])
        self.assertEqual(after_epicrisis[EPICRISIS][1], updated_epicrisis.to_json())
        self.assertEqual(after_epicrisis[EPICRISIS][2], "2026-11-10T09:00:00.111Z")
        self.assertEqual(after_epicrisis[PROTOCOL], first_before[PROTOCOL])

        update_generated_document(
            self.connection,
            first_session,
            PROTOCOL,
            updated_protocol,
            "2026-11-10T09:01:00.222Z",
        )
        after_protocol = self.document_rows(first_session)
        self.assertEqual(after_protocol[PROTOCOL][0], first_before[PROTOCOL][0])
        self.assertEqual(after_protocol[PROTOCOL][1], updated_protocol.to_json())
        self.assertEqual(after_protocol[PROTOCOL][2], "2026-11-10T09:01:00.222Z")
        self.assertEqual(after_protocol[EPICRISIS], after_epicrisis[EPICRISIS])
        self.assertEqual(self.document_rows(other_session), other_before)
        self.assertEqual(updated_epicrisis.to_json(), epicrisis_before_call)
        self.assertEqual(updated_protocol.to_json(), protocol_before_call)
        self.assertEqual(
            [
                tuple(row)
                for row in self.connection.execute(
                    "SELECT id, source_docx, source_sha256, parsed_content_json FROM templates ORDER BY id"
                )
            ],
            templates_before,
        )
        detail = get_generation_session(self.connection, first_session)
        assert detail is not None
        self.assertEqual(detail.epicrisis_recommendations.to_dict(), updated_epicrisis.to_dict())
        self.assertEqual(detail.operative_protocol.to_dict(), updated_protocol.to_dict())

    def test_update_rejects_invalid_arguments_without_changing_rows(self) -> None:
        session_id = self.create_session()
        before = self.document_rows(session_id)
        document = make_document("Nou")

        for invalid_id in (0, -1, True, "1"):
            with self.subTest(session_id=invalid_id):
                with self.assertRaises(ValueError):
                    update_generated_document(
                        self.connection,
                        invalid_id,  # type: ignore[arg-type]
                        EPICRISIS,
                        document,
                        EDITED_AT,
                    )
        with self.assertRaises(ValueError):
            update_generated_document(self.connection, session_id, "unknown", document, EDITED_AT)
        with self.assertRaises(TypeError):
            update_generated_document(self.connection, session_id, EPICRISIS, "text", EDITED_AT)  # type: ignore[arg-type]

        invalid_document = make_document("Valid înainte")
        invalid_document.paragraphs[0].inlines.append(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "RichDocument valid"):
            update_generated_document(self.connection, session_id, EPICRISIS, invalid_document, EDITED_AT)

        for invalid_timestamp in (
            None,
            "",
            "2026-11-10T08:30:00Z",
            "2026-11-10T08:30:00.123",
            "2026-13-10T08:30:00.000Z",
            "2026-11-10T08:30:00.000+00:00",
            "2026-11-10T08:30:00.123+02:00",
        ):
            with self.subTest(edited_at=invalid_timestamp):
                with self.assertRaises(ValueError):
                    update_generated_document(
                        self.connection,
                        session_id,
                        EPICRISIS,
                        document,
                        invalid_timestamp,  # type: ignore[arg-type]
                    )
        self.assertEqual(self.document_rows(session_id), before)

    def test_update_rejects_missing_and_incomplete_sessions(self) -> None:
        document = make_document("Nou")
        with self.assertRaises(GenerationSessionNotFoundError):
            update_generated_document(self.connection, 9999, EPICRISIS, document, EDITED_AT)
        building_id = self.create_building_session()
        self.connection.execute(
            """
            INSERT INTO generated_documents (
                session_id, template_set_id, document_type, template_id, rich_text_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                building_id,
                self.template_set_id,
                EPICRISIS,
                self.template_ids[EPICRISIS],
                make_document("Document parțial").to_json(),
            ),
        )
        with self.assertRaises(GenerationSessionStateError):
            update_generated_document(self.connection, building_id, EPICRISIS, document, EDITED_AT)

    def test_update_reports_a_missing_document(self) -> None:
        session_id = self.create_session()
        self.connection.execute("DROP TRIGGER generated_document_delete_guard")
        self.connection.execute(
            "DELETE FROM generated_documents WHERE session_id = ? AND document_type = ?",
            (session_id, EPICRISIS),
        )
        remaining_before = self.document_rows(session_id)

        with self.assertRaises(GeneratedDocumentNotFoundError):
            update_generated_document(
                self.connection,
                session_id,
                EPICRISIS,
                make_document("Nu"),
                EDITED_AT,
            )
        self.assertEqual(self.document_rows(session_id), remaining_before)

    def test_sql_failure_rolls_back_document_update(self) -> None:
        session_id = self.create_session()
        before = self.document_rows(session_id)
        self.connection.execute(
            """
            CREATE TRIGGER fail_generated_document_update
            BEFORE UPDATE OF rich_text_json ON generated_documents
            WHEN OLD.session_id = %d
            BEGIN
                SELECT RAISE(ABORT, 'injected repository update failure');
            END
            """ % session_id
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected repository update failure"):
            update_generated_document(
                self.connection,
                session_id,
                EPICRISIS,
                make_document("Nu"),
                EDITED_AT,
            )

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.document_rows(session_id), before)

    def test_repository_uses_caller_connection_and_nested_savepoint(self) -> None:
        session_id = self.create_session()
        original = self.document_rows(session_id)
        replacement = make_document("Editat în tranzacția apelantului")

        with patch("sqlite3.connect", side_effect=AssertionError("repository opened a connection")):
            self.assertEqual(len(list_generation_sessions(self.connection)), 1)
            self.assertIsNotNone(get_generation_session(self.connection, session_id))
            self.connection.execute("BEGIN IMMEDIATE")
            update_generated_document(self.connection, session_id, EPICRISIS, replacement, EDITED_AT)

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.document_rows(session_id)[EPICRISIS][1], replacement.to_json())
        self.connection.execute("ROLLBACK")
        self.assertEqual(self.document_rows(session_id), original)

    def test_nested_savepoint_failure_preserves_callers_transaction(self) -> None:
        session_id = self.create_session()
        before = self.document_rows(session_id)
        self.connection.execute(
            """
            CREATE TRIGGER fail_nested_generated_document_update
            BEFORE UPDATE OF rich_text_json ON generated_documents
            BEGIN
                SELECT RAISE(ABORT, 'injected nested update failure');
            END
            """
        )
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "INSERT INTO app_settings (setting_key, value) VALUES ('temporary', 'outer transaction')"
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected nested update failure"):
            update_generated_document(
                self.connection,
                session_id,
                EPICRISIS,
                make_document("Nu"),
                EDITED_AT,
            )

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.document_rows(session_id), before)
        self.assertEqual(
            self.connection.execute(
                "SELECT value FROM app_settings WHERE setting_key = 'temporary'"
            ).fetchone()[0],
            "outer transaction",
        )
        self.connection.execute("ROLLBACK")
        self.assertIsNone(
            self.connection.execute(
                "SELECT value FROM app_settings WHERE setting_key = 'temporary'"
            ).fetchone()
        )
        self.assertEqual(self.document_rows(session_id), before)


if __name__ == "__main__":
    unittest.main()
