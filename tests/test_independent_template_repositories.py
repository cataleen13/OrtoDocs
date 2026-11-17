"""Teste SQL pentru repository-ul șabloanelor independente."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database
from ortodocs.storage import independent_template_repositories as repository


EPICRISIS = "epicrisis_recommendations"
PROTOCOL = "operative_protocol"
RICH_TEXT = '{"schema_version":1,"paragraphs":[]}'
TIMESTAMP = "2026-11-12T10:00:00.000Z"


class IndependentTemplateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "independent-templates.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = self._add_diagnosis("Fractură col femural")
        self.other_diagnosis_id = self._add_diagnosis("Fractură pertrohanteriană")
        self.intervention_id = self._add_intervention(
            self.diagnosis_id,
            "Proteza bipolară",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _add_diagnosis(self, name: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO diagnoses (name, name_key) VALUES (?, ?)",
            (name, name.casefold().replace(" ", "_")),
        )
        return int(cursor.lastrowid)

    def _add_intervention(self, diagnosis_id: int, name: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO interventions (diagnosis_id, name, name_key)
            VALUES (?, ?, ?)
            """,
            (diagnosis_id, name, name.casefold().replace(" ", "_")),
        )
        return int(cursor.lastrowid)

    def _insert_version(
        self,
        *,
        owner_type: str = "diagnosis",
        owner_id: int | None = None,
        document_type: str | None = None,
        version_no: int = 1,
        status: str = "draft",
        parser_status: str = "valid",
        validation_status: str = "pending",
        patient_reviewed: bool = False,
        warnings_confirmed: bool = False,
    ) -> int:
        if owner_id is None:
            owner_id = (
                self.diagnosis_id if owner_type == "diagnosis" else self.intervention_id
            )
        if document_type is None:
            document_type = EPICRISIS if owner_type == "diagnosis" else PROTOCOL
        source_docx = f"docx-{owner_type}-{owner_id}-{version_no}".encode()
        template_id = repository.insert_independent_template_version(
            self.connection,
            owner_type=owner_type,
            owner_id=owner_id,
            document_type=document_type,
            version_no=version_no,
            source_docx=source_docx,
            source_sha256=hashlib.sha256(source_docx).hexdigest(),
            parsed_content_json=RICH_TEXT,
            parser_report_json=json.dumps(
                {"status": parser_status, "variables": {"fracture_type": 1}}
            ),
            parser_version="test-parser-1",
            validation_status=validation_status,
        )
        if warnings_confirmed or patient_reviewed:
            repository.update_independent_template_confirmations(
                self.connection,
                template_id,
                warnings_confirmed=warnings_confirmed,
                warnings_confirmed_at=TIMESTAMP if warnings_confirmed else None,
                patient_identification_reviewed=patient_reviewed,
                patient_identification_reviewed_at=TIMESTAMP if patient_reviewed else None,
            )
        if status == "rejected":
            repository.reject_independent_template_version(self.connection, template_id)
        elif status == "active":
            if validation_status != "valid":
                repository.update_independent_template_validation(
                    self.connection,
                    template_id,
                    "valid",
                )
            if not patient_reviewed or (parser_status == "valid_with_warnings" and not warnings_confirmed):
                repository.update_independent_template_confirmations(
                    self.connection,
                    template_id,
                    warnings_confirmed=warnings_confirmed,
                    warnings_confirmed_at=TIMESTAMP if warnings_confirmed else None,
                    patient_identification_reviewed=True,
                    patient_identification_reviewed_at=TIMESTAMP,
                )
            repository.activate_independent_template_version(
                self.connection,
                template_id,
                TIMESTAMP,
            )
        elif status == "retired":
            if validation_status != "valid":
                repository.update_independent_template_validation(
                    self.connection,
                    template_id,
                    "valid",
                )
            repository.update_independent_template_confirmations(
                self.connection,
                template_id,
                warnings_confirmed=warnings_confirmed,
                warnings_confirmed_at=TIMESTAMP if warnings_confirmed else None,
                patient_identification_reviewed=True,
                patient_identification_reviewed_at=TIMESTAMP,
            )
            repository.activate_independent_template_version(
                self.connection,
                template_id,
                TIMESTAMP,
            )
            repository.retire_independent_template_version(
                self.connection,
                template_id,
                TIMESTAMP,
            )
        return template_id

    def _prepare_for_activation(self, template_version_id: int) -> None:
        repository.update_independent_template_validation(
            self.connection,
            template_version_id,
            "valid",
        )
        repository.update_independent_template_confirmations(
            self.connection,
            template_version_id,
            warnings_confirmed=False,
            warnings_confirmed_at=None,
            patient_identification_reviewed=True,
            patient_identification_reviewed_at=TIMESTAMP,
        )

    def test_list_filters_owner_document_type_and_status_in_version_order(self) -> None:
        old = self._insert_version(version_no=1)
        newest = self._insert_version(version_no=3)
        rejected = self._insert_version(version_no=2, status="rejected")
        retired = self._insert_version(version_no=4, status="active")
        active = self._insert_version(version_no=5)
        self._prepare_for_activation(active)
        repository.retire_independent_template_version(
            self.connection,
            retired,
            TIMESTAMP,
        )
        repository.activate_independent_template_version(
            self.connection,
            active,
            TIMESTAMP,
        )
        other_owner = self._insert_version(
            owner_type="diagnosis",
            owner_id=self.other_diagnosis_id,
            version_no=1,
        )
        protocol = self._insert_version(owner_type="intervention", version_no=1)

        rows = repository.list_independent_template_versions(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
        )
        self.assertEqual(
            [row["id"] for row in rows],
            [active, retired, newest, rejected, old],
        )
        self.assertEqual(
            {row["status"] for row in rows},
            {"draft", "active", "retired", "rejected"},
        )
        self.assertTrue(all(row["owner_name"] == "Fractură col femural" for row in rows))
        self.assertEqual(
            [row["id"] for row in repository.list_independent_template_versions(
                self.connection,
                owner_type="diagnosis",
                owner_id=self.diagnosis_id,
                document_type=EPICRISIS,
                status="rejected",
            )],
            [rejected],
        )
        self.assertEqual(
            [row["id"] for row in repository.list_independent_template_versions(
                self.connection,
                owner_type="intervention",
                owner_id=self.intervention_id,
                document_type=PROTOCOL,
            )],
            [protocol],
        )
        self.assertEqual(
            [row["id"] for row in repository.list_independent_template_versions(
                self.connection,
                owner_type="diagnosis",
                owner_id=self.other_diagnosis_id,
                document_type=EPICRISIS,
            )],
            [other_owner],
        )

    def test_list_does_not_load_docx_or_parsed_rich_text(self) -> None:
        self._insert_version()

        row = repository.list_independent_template_versions(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
        )[0]

        self.assertNotIn("source_docx", row)
        self.assertNotIn("parsed_content_json", row)
        self.assertNotIn(b"docx-diagnosis", tuple(row.values()))

    def test_details_include_parser_report_and_content_returns_original_docx(self) -> None:
        template_id = self._insert_version(parser_status="valid_with_warnings")

        details = repository.get_independent_template_details(self.connection, template_id)
        content = repository.get_independent_template_content(self.connection, template_id)

        self.assertIsNotNone(details)
        self.assertEqual(details["parser_report_json"], '{"status": "valid_with_warnings", "variables": {"fracture_type": 1}}')
        self.assertEqual(details["owner_type"], "diagnosis")
        self.assertEqual(details["document_type"], EPICRISIS)
        self.assertNotIn("source_docx", details)
        self.assertEqual(content["source_docx"], b"docx-diagnosis-1-1")
        self.assertEqual(content["parsed_content_json"], RICH_TEXT)
        self.assertEqual(content["parser_report_json"], details["parser_report_json"])

    def test_next_version_counts_all_lifecycle_states_for_each_owner(self) -> None:
        self._insert_version(version_no=1, status="rejected")
        self._insert_version(version_no=2)
        self._insert_version(owner_type="intervention", version_no=1)

        self.assertEqual(
            repository.get_next_independent_template_version(
                self.connection,
                owner_type="diagnosis",
                owner_id=self.diagnosis_id,
                document_type=EPICRISIS,
            ),
            3,
        )
        self.assertEqual(
            repository.get_next_independent_template_version(
                self.connection,
                owner_type="intervention",
                owner_id=self.intervention_id,
                document_type=PROTOCOL,
            ),
            2,
        )

    def test_insert_creates_draft_and_preserves_caller_transaction(self) -> None:
        source_docx = b"transactional-docx"
        self.connection.execute("BEGIN IMMEDIATE")
        template_id = repository.insert_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
            version_no=1,
            source_docx=source_docx,
            source_sha256=hashlib.sha256(source_docx).hexdigest(),
            parsed_content_json=RICH_TEXT,
            parser_report_json='{"status":"valid","variables":{}}',
            parser_version="test-parser-1",
        )

        self.assertTrue(self.connection.in_transaction)
        row = self.connection.execute(
            "SELECT status, validation_status FROM independent_template_versions WHERE id = ?",
            (template_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("draft", "pending"))
        self.connection.execute("ROLLBACK")
        self.assertIsNone(repository.get_independent_template_details(self.connection, template_id))

    def test_confirmation_and_validation_updates_are_limited_to_drafts(self) -> None:
        template_id = self._insert_version()

        self.assertEqual(
            repository.update_independent_template_confirmations(
                self.connection,
                template_id,
                warnings_confirmed=True,
                warnings_confirmed_at=TIMESTAMP,
                patient_identification_reviewed=True,
                patient_identification_reviewed_at=TIMESTAMP,
            ),
            1,
        )
        self.assertEqual(
            repository.update_independent_template_validation(
                self.connection,
                template_id,
                "valid",
            ),
            1,
        )
        row = self.connection.execute(
            """
            SELECT warnings_confirmed, warnings_confirmed_at,
                   patient_identification_reviewed,
                   patient_identification_reviewed_at, validation_status
            FROM independent_template_versions WHERE id = ?
            """,
            (template_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (1, TIMESTAMP, 1, TIMESTAMP, "valid"))

    def test_active_version_can_be_read_and_replaced_by_a_new_version(self) -> None:
        old_id = self._insert_version(version_no=1)
        self._prepare_for_activation(old_id)
        self.assertEqual(
            repository.activate_independent_template_version(
                self.connection,
                old_id,
                TIMESTAMP,
            ),
            1,
        )
        active = repository.get_active_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
        )
        self.assertEqual(active["id"], old_id)

        new_id = self._insert_version(version_no=2)
        self._prepare_for_activation(new_id)
        self.assertEqual(
            repository.retire_independent_template_version(
                self.connection,
                old_id,
                TIMESTAMP,
            ),
            1,
        )
        self.assertEqual(
            repository.activate_independent_template_version(
                self.connection,
                new_id,
                TIMESTAMP,
            ),
            1,
        )
        active = repository.get_active_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
        )
        self.assertEqual(active["id"], new_id)
        old_details = repository.get_independent_template_details(self.connection, old_id)
        self.assertEqual(old_details["status"], "retired")
        self.assertEqual(old_details["activated_at"], TIMESTAMP)

    def test_failed_active_version_replacement_rolls_back_completely(self) -> None:
        old_id = self._insert_version(version_no=1)
        self._prepare_for_activation(old_id)
        repository.activate_independent_template_version(
            self.connection,
            old_id,
            TIMESTAMP,
        )
        new_id = self._insert_version(version_no=2)

        self.connection.execute("BEGIN IMMEDIATE")
        repository.retire_independent_template_version(
            self.connection,
            old_id,
            TIMESTAMP,
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "independent template is not ready for activation",
        ):
            repository.activate_independent_template_version(
                self.connection,
                new_id,
                TIMESTAMP,
            )

        self.connection.execute("ROLLBACK")

        old_details = repository.get_independent_template_details(
            self.connection,
            old_id,
        )
        new_details = repository.get_independent_template_details(
            self.connection,
            new_id,
        )
        active = repository.get_active_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
        )
        active_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM independent_template_versions
            WHERE owner_type = 'diagnosis'
              AND diagnosis_id = ?
              AND document_type = ?
              AND status = 'active'
            """,
            (self.diagnosis_id, EPICRISIS),
        ).fetchone()[0]

        self.assertEqual(old_details["status"], "active")
        self.assertEqual(new_details["status"], "draft")
        self.assertEqual(active["id"], old_id)
        self.assertEqual(active_count, 1)

    def test_rejection_and_missing_records_return_expected_row_counts(self) -> None:
        template_id = self._insert_version()

        self.assertEqual(
            repository.reject_independent_template_version(self.connection, template_id),
            1,
        )
        self.assertEqual(
            repository.update_independent_template_validation(
                self.connection,
                template_id,
                "valid",
            ),
            0,
        )
        self.assertIsNone(repository.get_independent_template_details(self.connection, 9999))
        self.assertIsNone(repository.get_independent_template_content(self.connection, 9999))
        self.assertIsNone(
            repository.get_active_independent_template_version(
                self.connection,
                owner_type="diagnosis",
                owner_id=self.diagnosis_id,
                document_type=EPICRISIS,
            )
        )
        self.assertEqual(
            repository.retire_independent_template_version(self.connection, 9999, TIMESTAMP),
            0,
        )
        self.assertEqual(
            repository.reject_independent_template_version(self.connection, 9999),
            0,
        )

    def test_database_constraints_reject_invalid_owner_document_and_duplicate_versions(self) -> None:
        source_docx = b"bad-association"
        arguments = {
            "owner_type": "diagnosis",
            "owner_id": self.diagnosis_id,
            "document_type": PROTOCOL,
            "version_no": 1,
            "source_docx": source_docx,
            "source_sha256": hashlib.sha256(source_docx).hexdigest(),
            "parsed_content_json": RICH_TEXT,
            "parser_report_json": '{"status":"valid","variables":{}}',
            "parser_version": "test-parser-1",
        }
        with self.assertRaises(sqlite3.IntegrityError):
            repository.insert_independent_template_version(self.connection, **arguments)

        arguments["document_type"] = EPICRISIS
        arguments["owner_id"] = 9999
        with self.assertRaises(sqlite3.IntegrityError):
            repository.insert_independent_template_version(self.connection, **arguments)

        arguments["owner_id"] = self.diagnosis_id
        repository.insert_independent_template_version(self.connection, **arguments)
        with self.assertRaises(sqlite3.IntegrityError):
            repository.insert_independent_template_version(self.connection, **arguments)

    def test_schema_enforces_activation_requirements_and_one_active_version(self) -> None:
        first = self._insert_version(version_no=1)
        with self.assertRaises(sqlite3.IntegrityError):
            repository.activate_independent_template_version(
                self.connection,
                first,
                TIMESTAMP,
            )

        self._prepare_for_activation(first)
        repository.activate_independent_template_version(self.connection, first, TIMESTAMP)
        second = self._insert_version(version_no=2)
        self._prepare_for_activation(second)
        with self.assertRaises(sqlite3.IntegrityError):
            repository.activate_independent_template_version(
                self.connection,
                second,
                TIMESTAMP,
            )

    def test_confirmation_consistency_and_published_immutability_are_enforced(self) -> None:
        template_id = self._insert_version()
        with self.assertRaises(sqlite3.IntegrityError):
            repository.update_independent_template_confirmations(
                self.connection,
                template_id,
                warnings_confirmed=True,
                warnings_confirmed_at=None,
                patient_identification_reviewed=False,
                patient_identification_reviewed_at=None,
            )

        self._prepare_for_activation(template_id)
        repository.activate_independent_template_version(self.connection, template_id, TIMESTAMP)
        self.assertEqual(
            repository.update_independent_template_validation(
                self.connection,
                template_id,
                "invalid",
            ),
            0,
        )
        self.assertEqual(
            repository.update_independent_template_confirmations(
                self.connection,
                template_id,
                warnings_confirmed=False,
                warnings_confirmed_at=None,
                patient_identification_reviewed=False,
                patient_identification_reviewed_at=None,
            ),
            0,
        )

    def test_repository_operations_do_not_commit_or_rollback_callers_transaction(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        template_id = repository.insert_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_type=EPICRISIS,
            version_no=1,
            source_docx=b"caller-owned-transaction",
            source_sha256=hashlib.sha256(b"caller-owned-transaction").hexdigest(),
            parsed_content_json=RICH_TEXT,
            parser_report_json='{"status":"valid","variables":{}}',
            parser_version="test-parser-1",
        )
        repository.update_independent_template_validation(
            self.connection,
            template_id,
            "valid",
        )

        self.assertTrue(self.connection.in_transaction)
        self.assertIsNotNone(repository.get_independent_template_details(self.connection, template_id))
        self.connection.execute("ROLLBACK")
        self.assertIsNone(repository.get_independent_template_details(self.connection, template_id))


if __name__ == "__main__":
    unittest.main()
