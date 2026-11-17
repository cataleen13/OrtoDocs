"""Teste izolate pentru importul, maparea și activarea pachetelor DOCX."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.import_report import ImportIssue, ImportReport
from ortodocs.models.rich_document import Paragraph, RichDocument, TextInline
from ortodocs.services.medical_library import (
    add_diagnosis,
    add_field_option,
    add_intervention,
    create_variable,
    create_variable_version,
    set_diagnosis_active,
    set_intervention_active,
    set_current_variable_version,
)
from ortodocs.services.docx_import import MAX_DOCX_COMPRESSED_BYTES
from ortodocs.services.template_sets import (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
    TemplateSetDuplicateMappingError,
    TemplateSetIntegrityError,
    TemplateSetNotFoundError,
    TemplateSetStateError,
    TemplateSetValidationError,
    activate_independent_template,
    activate_template_set,
    add_variable_mapping,
    confirm_independent_template_patient_review,
    confirm_independent_template_warnings,
    confirm_template_warnings,
    create_template_set_draft,
    get_independent_template_content,
    get_independent_template_details,
    get_template_content,
    get_template_set_details,
    import_independent_template,
    import_template,
    import_template_pair,
    list_document_variables,
    list_independent_templates,
    list_template_sets,
    reject_independent_template,
    reject_template_set,
    retire_independent_template,
    update_variable_mapping,
    validate_independent_template,
    validate_template_set,
)
from ortodocs.storage.database import connect_database, save_generation_session
from ortodocs.storage.migrations import initialize_database
from ortodocs.storage.template_repositories import (
    list_template_sets as repository_list_template_sets,
)


def make_docx(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class TemplateSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "template-sets-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = add_diagnosis(self.connection, "Fractură col femural")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Proteza bipolară",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def create_draft(self) -> int:
        return create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )

    def import_pair(
        self,
        template_set_id: int,
        *,
        epicrisis: str = "Epicriză fără variabile",
        protocol: str = "Protocol fără variabile",
    ) -> dict[str, int]:
        return import_template_pair(
            self.connection,
            template_set_id,
            {
                EPICRISIS_RECOMMENDATIONS: make_docx(epicrisis),
                OPERATIVE_PROTOCOL: make_docx(protocol),
            },
        )

    def template_id(self, template_set_id: int, document_type: str) -> int:
        details = get_template_set_details(self.connection, template_set_id)
        return next(
            item["id"]
            for item in details["templates"]
            if item["document_type"] == document_type
        )

    def activate(self, template_set_id: int):
        return activate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=True,
        )

    def create_common_field(self, key: str = "data_internarii", data_type: str = "text") -> int:
        return create_variable(
            self.connection,
            key,
            key.replace("_", " ").title(),
            data_type,
            "common",
        )

    def test_create_draft_checks_active_records_and_assigns_consecutive_versions(self) -> None:
        first = self.create_draft()
        reject_template_set(self.connection, first, reason="Draft de test abandonat")
        second = self.create_draft()
        self.assertEqual(
            [row["version_no"] for row in self.connection.execute(
                "SELECT version_no FROM template_sets WHERE intervention_id = ? ORDER BY version_no",
                (self.intervention_id,),
            )],
            [1, 2],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (second,)
            ).fetchone()[0],
            "draft",
        )

        other_diagnosis = add_diagnosis(self.connection, "Fractură pertrohanteriană")
        with self.assertRaises(TemplateSetValidationError):
            create_template_set_draft(
                self.connection,
                other_diagnosis,
                self.intervention_id,
            )
        self.connection.execute(
            "UPDATE diagnoses SET is_active = 0 WHERE id = ?",
            (self.diagnosis_id,),
        )
        with self.assertRaises(TemplateSetStateError):
            create_template_set_draft(
                self.connection,
                self.diagnosis_id,
                self.intervention_id,
            )

    def test_repository_lists_empty_filters_and_returns_lightweight_metadata(self) -> None:
        self.assertEqual(repository_list_template_sets(self.connection), [])

        first_set = self.create_draft()
        self.import_pair(first_set)
        self.assertTrue(self.activate(first_set).can_activate)

        second_intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Fixare alternativă",
        )
        second_set = create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            second_intervention_id,
        )

        changes_before = self.connection.total_changes
        all_sets = repository_list_template_sets(self.connection)
        filtered_sets = repository_list_template_sets(
            self.connection,
            intervention_id=self.intervention_id,
        )
        service_sets = list_template_sets(self.connection)

        self.assertEqual(self.connection.total_changes, changes_before)
        self.assertEqual(
            {item["id"] for item in all_sets},
            {first_set, second_set},
        )
        self.assertEqual([item["id"] for item in filtered_sets], [first_set])

        first = next(item for item in all_sets if item["id"] == first_set)
        self.assertEqual(first["diagnosis_id"], self.diagnosis_id)
        self.assertEqual(first["diagnosis_name"], "Fractură col femural")
        self.assertEqual(first["intervention_id"], self.intervention_id)
        self.assertEqual(first["intervention_name"], "Proteza bipolară")
        self.assertEqual(first["diagnosis_is_active"], 1)
        self.assertEqual(first["intervention_is_active"], 1)
        self.assertEqual(first["document_count"], 2)
        self.assertEqual(first["status"], "active")
        self.assertIsNotNone(first["created_at"])
        self.assertIsNotNone(first["activated_at"])
        self.assertIsNone(first["retired_at"])
        self.assertNotIn("source_docx", first)
        self.assertNotIn("parsed_content_json", first)
        self.assertNotIn("validation_report_json", first)

        service_first = next(item for item in service_sets if item["id"] == first_set)
        self.assertEqual(service_first["validation"]["status"], "valid")
        self.assertNotIn("import_report_json", service_first)
        self.assertNotIn("source_docx", service_first)
        self.assertNotIn("parsed_content_json", service_first)

    def test_service_lists_every_status_in_newest_version_order_and_reads_saved_summary(self) -> None:
        rejected_set = self.create_draft()
        reject_template_set(
            self.connection,
            rejected_set,
            reason="Pachet respins pentru test.",
        )

        retired_set = self.create_draft()
        self.import_pair(retired_set)
        self.assertTrue(self.activate(retired_set).can_activate)

        active_set = self.create_draft()
        self.import_pair(active_set)
        self.assertTrue(self.activate(active_set).can_activate)

        draft_set = self.create_draft()
        other_intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție secundară",
        )
        other_set = create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            other_intervention_id,
        )

        set_diagnosis_active(self.connection, self.diagnosis_id, False)
        set_intervention_active(self.connection, self.intervention_id, False)

        # Un rezumat stocat fără anumite chei trebuie returnat fără completări inventate.
        stored_report = {
            "status": "draft",
            "validation": {
                "status": "not_run",
                "issues": [],
            },
        }
        self.connection.execute(
            "UPDATE template_sets SET import_report_json = ? WHERE id = ?",
            (json.dumps(stored_report), draft_set),
        )

        changes_before = self.connection.total_changes
        listed = list_template_sets(
            self.connection,
            intervention_id=self.intervention_id,
        )
        all_listed = list_template_sets(self.connection)
        self.assertEqual(self.connection.total_changes, changes_before)

        self.assertEqual(
            [item["id"] for item in listed],
            [draft_set, active_set, retired_set, rejected_set],
        )
        self.assertEqual(
            {item["status"] for item in listed},
            {"draft", "active", "retired", "rejected"},
        )
        self.assertEqual(
            [item["document_count"] for item in listed],
            [0, 2, 2, 0],
        )
        draft = listed[0]
        self.assertEqual(draft["validation"], stored_report["validation"])
        self.assertNotIn("can_activate", draft["validation"])
        self.assertEqual(draft["diagnosis_is_active"], 0)
        self.assertEqual(draft["intervention_is_active"], 0)
        self.assertEqual(
            {item["id"] for item in all_listed},
            {draft_set, active_set, retired_set, rejected_set, other_set},
        )

    def test_service_rejects_invalid_intervention_id_for_listing(self) -> None:
        for invalid_id in (0, -1, True, "1"):
            with self.subTest(intervention_id=invalid_id):
                with self.assertRaises(TemplateSetValidationError):
                    list_template_sets(
                        self.connection,
                        intervention_id=invalid_id,
                    )

    def test_incremental_import_keeps_package_in_draft_until_second_document(self) -> None:
        template_set_id = self.create_draft()
        self.assertFalse(self.connection.in_transaction)
        template_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            make_docx("Epicriză"),
        )
        self.assertFalse(self.connection.in_transaction)
        details = get_template_set_details(self.connection, template_set_id)
        self.assertEqual(details["status"], "draft")
        self.assertEqual([item["id"] for item in details["templates"]], [template_id])
        result = self.activate(template_set_id)
        self.assertFalse(result.can_activate)
        self.assertIn("missing_document", {issue["code"] for issue in result.issues})
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (template_set_id,)
            ).fetchone()[0],
            "draft",
        )

    def test_pair_import_is_atomic_if_second_template_insert_fails(self) -> None:
        template_set_id = self.create_draft()
        self.connection.execute(
            """
            CREATE TRIGGER fail_protocol_import
            BEFORE INSERT ON templates
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'injected second import failure');
            END
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_pair(template_set_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_atomic_pair_reimport_restores_both_previous_documents_on_failure(self) -> None:
        template_set_id = self.create_draft()
        old_ids = self.import_pair(
            template_set_id,
            epicrisis="Versiunea veche Epicriză",
            protocol="Versiunea veche Protocol",
        )
        old_sources = {
            kind: self.connection.execute(
                "SELECT source_docx FROM templates WHERE id = ?", (template_id,)
            ).fetchone()[0]
            for kind, template_id in old_ids.items()
        }
        self.connection.execute(
            """
            CREATE TRIGGER fail_protocol_reimport
            BEFORE INSERT ON templates
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'injected protocol replacement failure');
            END
            """
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_pair(
                template_set_id,
                epicrisis="Versiunea nouă Epicriză",
                protocol="Versiunea nouă Protocol",
            )
        details = get_template_set_details(self.connection, template_set_id)
        self.assertEqual(
            {item["document_type"]: item["id"] for item in details["templates"]},
            old_ids,
        )
        self.assertEqual(
            {
                item["document_type"]: get_template_content(self.connection, item["id"])["source_docx"]
                for item in details["templates"]
            },
            old_sources,
        )

    def test_inactive_record_after_draft_creation_blocks_validation(self) -> None:
        template_set_id = self.create_draft()
        self.import_pair(template_set_id)
        self.connection.execute(
            "UPDATE interventions SET is_active = 0 WHERE id = ?",
            (self.intervention_id,),
        )
        result = self.activate(template_set_id)
        self.assertFalse(result.can_activate)
        self.assertIn("inactive_intervention", {issue["code"] for issue in result.issues})

    def test_rejected_document_original_is_available_through_service(self) -> None:
        template_set_id = self.create_draft()
        source = b"invalid DOCX bytes"
        template_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            source,
        )
        content = get_template_content(self.connection, template_id)
        self.assertEqual(content["source_docx"], source)
        self.assertEqual(content["document"], RichDocument())

    def test_empty_upload_is_refused_but_oversized_docx_is_retained_as_rejected(self) -> None:
        template_set_id = self.create_draft()
        with self.assertRaises(TemplateSetValidationError):
            import_template(
                self.connection,
                template_set_id,
                EPICRISIS_RECOMMENDATIONS,
                b"",
            )

        oversized = b"x" * (MAX_DOCX_COMPRESSED_BYTES + 1)
        template_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            oversized,
        )
        row = self.connection.execute(
            """
            SELECT source_docx, parsed_content_json, validation_status,
                   validation_report_json
            FROM templates WHERE id = ?
            """,
            (template_id,),
        ).fetchone()
        self.assertEqual(row[0], oversized)
        self.assertEqual(RichDocument.from_json(row[1]), RichDocument())
        self.assertEqual(row[2], "invalid")
        self.assertEqual(
            json.loads(row[3])["import_report"]["status"],
            "rejected",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            1,
        )

    def test_pair_import_parses_both_documents_before_opening_write_transaction(self) -> None:
        template_set_id = self.create_draft()
        original_parser = __import__(
            "ortodocs.services.template_sets", fromlist=["parse_docx_bytes"]
        ).parse_docx_bytes
        transaction_states: list[bool] = []

        def parser(data: bytes):
            transaction_states.append(self.connection.in_transaction)
            return original_parser(data)

        with patch("ortodocs.services.template_sets.parse_docx_bytes", side_effect=parser):
            self.import_pair(template_set_id)
        self.assertEqual(transaction_states, [False, False])

    def test_pair_import_refuses_existing_transaction_before_parser_or_writes(self) -> None:
        template_set_id = self.create_draft()
        package_report_before = self.connection.execute(
            "SELECT import_report_json FROM template_sets WHERE id = ?",
            (template_set_id,),
        ).fetchone()[0]

        self.connection.execute("BEGIN IMMEDIATE")
        with patch("ortodocs.services.template_sets.parse_docx_bytes") as parser:
            with self.assertRaises(TemplateSetStateError):
                self.import_pair(template_set_id)
        parser.assert_not_called()

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT import_report_json FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            package_report_before,
        )
        self.connection.execute("ROLLBACK")

    def test_individual_import_refuses_existing_transaction_before_parser_or_writes(self) -> None:
        template_set_id = self.create_draft()
        package_report_before = self.connection.execute(
            "SELECT import_report_json FROM template_sets WHERE id = ?",
            (template_set_id,),
        ).fetchone()[0]

        self.connection.execute("BEGIN IMMEDIATE")
        with patch("ortodocs.services.template_sets.parse_docx_bytes") as parser:
            with self.assertRaises(TemplateSetStateError):
                import_template(
                    self.connection,
                    template_set_id,
                    EPICRISIS_RECOMMENDATIONS,
                    make_docx("Nu se parsează"),
                )
        parser.assert_not_called()

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT import_report_json FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            package_report_before,
        )
        self.connection.execute("ROLLBACK")

    def test_document_reimport_refuses_existing_transaction_without_changing_rows(self) -> None:
        template_set_id = self.create_draft()
        original_template_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            make_docx("Conținut original"),
        )
        field_id = self.create_common_field()
        self.connection.execute(
            """
            INSERT INTO template_variables (
                template_id, variable_key, field_definition_id, is_required
            ) VALUES (?, ?, ?, 1)
            """,
            (original_template_id, "data_internarii", field_id),
        )
        template_rows_before = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT id, source_docx, source_sha256, parsed_content_json,
                       validation_status, validation_report_json
                FROM templates WHERE template_set_id = ? ORDER BY id
                """,
                (template_set_id,),
            ).fetchall()
        ]
        mapping_rows_before = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT template_id, variable_key, field_definition_id, is_required
                FROM template_variables WHERE template_id = ? ORDER BY variable_key
                """,
                (original_template_id,),
            ).fetchall()
        ]
        package_report_before = self.connection.execute(
            "SELECT import_report_json FROM template_sets WHERE id = ?",
            (template_set_id,),
        ).fetchone()[0]

        self.connection.execute("BEGIN IMMEDIATE")
        with patch("ortodocs.services.template_sets.parse_docx_bytes") as parser:
            with self.assertRaises(TemplateSetStateError):
                import_template(
                    self.connection,
                    template_set_id,
                    EPICRISIS_RECOMMENDATIONS,
                    make_docx("Conținut nou"),
                )
        parser.assert_not_called()

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            [
                tuple(row)
                for row in self.connection.execute(
                    """
                    SELECT id, source_docx, source_sha256, parsed_content_json,
                           validation_status, validation_report_json
                    FROM templates WHERE template_set_id = ? ORDER BY id
                    """,
                    (template_set_id,),
                ).fetchall()
            ],
            template_rows_before,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.connection.execute(
                    """
                    SELECT template_id, variable_key, field_definition_id, is_required
                    FROM template_variables WHERE template_id = ? ORDER BY variable_key
                    """,
                    (original_template_id,),
                ).fetchall()
            ],
            mapping_rows_before,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT import_report_json FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            package_report_before,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT id FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            original_template_id,
        )
        self.connection.execute("ROLLBACK")

    def test_rejected_docx_original_and_report_are_retained_with_empty_rich_text(self) -> None:
        template_set_id = self.create_draft()
        source = b"not a DOCX archive"
        template_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            source,
        )
        template = self.connection.execute(
            """
            SELECT source_docx, source_sha256, parsed_content_json,
                   validation_status, validation_report_json
            FROM templates WHERE id = ?
            """,
            (template_id,),
        ).fetchone()
        self.assertEqual(template["source_docx"], source)
        self.assertEqual(template["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(template["parsed_content_json"], RichDocument().to_json())
        self.assertEqual(template["validation_status"], "invalid")
        report = json.loads(template["validation_report_json"])
        self.assertEqual(report["import_report"]["status"], "rejected")
        self.assertTrue(report["import_report"]["archive_errors"])
        result = validate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=True,
        )
        self.assertFalse(result.can_activate)
        self.assertIn("docx_rejected", {issue["code"] for issue in result.issues})

    def test_repeated_variable_is_mapped_once_and_requirement_is_per_document(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(
            template_set_id,
            epicrisis="{{data_internarii}} / {{data_internarii}}",
            protocol="{{data_internarii}}",
        )
        field_id = self.create_common_field()
        epicrisis_id = template_ids[EPICRISIS_RECOMMENDATIONS]
        protocol_id = template_ids[OPERATIVE_PROTOCOL]
        self.assertEqual(
            list_document_variables(self.connection, epicrisis_id),
            [
                {
                    "variable_key": "data_internarii",
                    "occurrence_count": 2,
                    "is_mapped": False,
                    "field_definition_id": None,
                    "field_version": None,
                    "field_label": None,
                    "is_required": None,
                }
            ],
        )
        add_variable_mapping(
            self.connection,
            epicrisis_id,
            "data_internarii",
            field_id,
            is_required=True,
        )
        with self.assertRaises(TemplateSetDuplicateMappingError):
            add_variable_mapping(
                self.connection,
                epicrisis_id,
                "data_internarii",
                field_id,
                is_required=False,
            )
        add_variable_mapping(
            self.connection,
            protocol_id,
            "data_internarii",
            field_id,
            is_required=False,
        )
        mappings = self.connection.execute(
            "SELECT template_id, field_definition_id, is_required "
            "FROM template_variables WHERE variable_key = ? ORDER BY template_id",
            ("data_internarii",),
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["template_id"],
                    row["field_definition_id"],
                    row["is_required"],
                )
                for row in mappings
            ],
            [(epicrisis_id, field_id, 1), (protocol_id, field_id, 0)],
        )
        result = self.activate(template_set_id)
        self.assertTrue(result.can_activate)

    def test_shared_variable_mapped_to_different_field_versions_blocks_activation(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(
            template_set_id,
            epicrisis="{{data_internarii}}",
            protocol="{{data_internarii}}",
        )
        first_field_id = self.create_common_field()
        second_field_id = create_variable_version(
            self.connection,
            "data_internarii",
            "Data internării versiunea 2",
            "date",
            "common",
        )
        set_current_variable_version(self.connection, "data_internarii", 2)
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            first_field_id,
            is_required=True,
        )
        add_variable_mapping(
            self.connection,
            template_ids[OPERATIVE_PROTOCOL],
            "data_internarii",
            second_field_id,
            is_required=False,
        )

        result = self.activate(template_set_id)

        self.assertFalse(result.can_activate)
        mismatch = next(
            issue
            for issue in result.issues
            if issue["code"] == "shared_variable_mapping_mismatch"
        )
        self.assertEqual(mismatch["variable_key"], "data_internarii")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()["status"],
            "draft",
        )

    def test_missing_and_extra_mappings_are_reported_and_block_activation(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(
            template_set_id,
            epicrisis="{{data_internarii}}",
        )
        extra_field = self.create_common_field("extra_camp")
        epicrisis_id = template_ids[EPICRISIS_RECOMMENDATIONS]
        # Injectăm o mapare SQL validă structural, dar care nu are token în DOCX,
        # pentru a verifica detectarea mapărilor suplimentare la nivelul serviciului.
        self.connection.execute(
            """
            INSERT INTO template_variables (
                template_id, variable_key, field_definition_id, is_required
            ) VALUES (?, 'extra_camp', ?, 0)
            """,
            (epicrisis_id, extra_field),
        )
        result = self.activate(template_set_id)
        codes = {issue["code"] for issue in result.issues}
        self.assertIn("unmapped_variable", codes)
        self.assertIn("extra_variable_mapping", codes)
        self.assertEqual(result.report["mappings_missing"][EPICRISIS_RECOMMENDATIONS], ["data_internarii"])
        self.assertEqual(result.report["mappings_extra"][EPICRISIS_RECOMMENDATIONS], ["extra_camp"])
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (template_set_id,)
            ).fetchone()[0],
            "draft",
        )

    def test_token_without_library_definition_remains_unmapped(self) -> None:
        template_set_id = self.create_draft()
        self.import_pair(template_set_id, epicrisis="{{camp_nedefinit}}")
        result = self.activate(template_set_id)
        self.assertIn("unmapped_variable", {issue["code"] for issue in result.issues})

    def test_mapping_rejects_wrong_key_and_wrong_scope(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{camp_specific}}")
        common_field = self.create_common_field("data_internarii")
        with self.assertRaises(TemplateSetValidationError):
            add_variable_mapping(
                self.connection,
                template_ids[EPICRISIS_RECOMMENDATIONS],
                "camp_specific",
                common_field,
                is_required=True,
            )

        other_diagnosis = add_diagnosis(self.connection, "Fractură pertrohanteriană")
        other_field = create_variable(
            self.connection,
            "camp_specific",
            "Câmp specific",
            "text",
            "diagnosis",
            diagnosis_id=other_diagnosis,
        )
        with self.assertRaises(TemplateSetValidationError):
            add_variable_mapping(
                self.connection,
                template_ids[EPICRISIS_RECOMMENDATIONS],
                "camp_specific",
                other_field,
                is_required=True,
            )

    def test_outdated_field_version_blocks_activation_until_remapped(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        first_id = self.create_common_field("data_internarii")
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            first_id,
            is_required=True,
        )
        second_id = create_variable_version(
            self.connection,
            "data_internarii",
            "Data nouă",
            "date",
            "common",
        )
        set_current_variable_version(self.connection, "data_internarii", 2)

        result = self.activate(template_set_id)
        self.assertIn("outdated_field_version", {issue["code"] for issue in result.issues})
        self.assertEqual(
            result.report["outdated_field_versions"][0]["field_definition_id"],
            first_id,
        )
        update_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            second_id,
            is_required=True,
        )
        self.assertTrue(self.activate(template_set_id).can_activate)

    def test_select_field_requires_options_for_exact_mapped_version(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{tip_interventie}}")
        field_id = self.create_common_field("tip_interventie", "select")
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "tip_interventie",
            field_id,
            is_required=True,
        )
        result = self.activate(template_set_id)
        self.assertIn("select_options_missing", {issue["code"] for issue in result.issues})
        add_field_option(self.connection, field_id, "standard", "Standard")
        self.assertTrue(self.activate(template_set_id).can_activate)

    def test_warning_confirmation_is_required_and_persisted_per_document(self) -> None:
        template_set_id = self.create_draft()
        source = make_docx("Document cu avertizare")
        warning_report = ImportReport(
            status="valid_with_warnings",
            paragraph_count=1,
            fragment_count=1,
            warnings=[
                ImportIssue(
                    code="proofing_annotation",
                    message="Marcaj de corectură ignorat.",
                    part="word/document.xml",
                )
            ],
        )
        warning_document = RichDocument([Paragraph([TextInline("Document cu avertizare")])])
        with patch(
            "ortodocs.services.template_sets.parse_docx_bytes",
            return_value=(warning_document, warning_report, hashlib.sha256(source).hexdigest()),
        ):
            warning_template_id = import_template(
                self.connection,
                template_set_id,
                EPICRISIS_RECOMMENDATIONS,
                source,
            )
        import_template(
            self.connection,
            template_set_id,
            OPERATIVE_PROTOCOL,
            make_docx("Protocol fără avertizări"),
        )
        result = self.activate(template_set_id)
        self.assertIn("warnings_not_confirmed", {issue["code"] for issue in result.issues})
        report = json.loads(
            self.connection.execute(
                "SELECT validation_report_json FROM templates WHERE id = ?",
                (warning_template_id,),
            ).fetchone()[0]
        )
        self.assertEqual(report["import_report"]["status"], "valid_with_warnings")

        confirm_template_warnings(
            self.connection,
            warning_template_id,
            confirmed=True,
        )
        result = self.activate(template_set_id)
        self.assertTrue(result.can_activate)
        saved_report = json.loads(
            self.connection.execute(
                "SELECT validation_report_json FROM templates WHERE id = ?",
                (warning_template_id,),
            ).fetchone()[0]
        )
        self.assertTrue(saved_report["warning_confirmation"]["confirmed"])
        self.assertTrue(saved_report["warning_confirmation"]["confirmed_at"])

    def test_warning_confirmation_cannot_be_applied_to_rejected_or_clean_document(self) -> None:
        template_set_id = self.create_draft()
        clean_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            make_docx("Curat"),
        )
        with self.assertRaises(TemplateSetStateError):
            confirm_template_warnings(self.connection, clean_id, confirmed=True)
        rejected_id = import_template(
            self.connection,
            template_set_id,
            OPERATIVE_PROTOCOL,
            b"invalid DOCX",
        )
        with self.assertRaises(TemplateSetStateError):
            confirm_template_warnings(self.connection, rejected_id, confirmed=True)

    def test_activation_requires_identification_review_confirmation(self) -> None:
        template_set_id = self.create_draft()
        self.import_pair(template_set_id)
        result = validate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=False,
        )
        self.assertFalse(result.can_activate)
        self.assertIn(
            "patient_identification_review_not_confirmed",
            {issue["code"] for issue in result.issues},
        )
        details = get_template_set_details(self.connection, template_set_id)
        self.assertFalse(details["import_report"]["patient_identification_review"]["confirmed"])
        self.assertEqual(details["status"], "draft")

    def test_valid_pair_activates_both_templates_together(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id)
        result = self.activate(template_set_id)
        self.assertTrue(result.can_activate)
        self.assertEqual(result.status, "active")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (template_set_id,)
            ).fetchone()[0],
            "active",
        )
        statuses = self.connection.execute(
            "SELECT document_type, validation_status FROM templates WHERE template_set_id = ?",
            (template_set_id,),
        ).fetchall()
        self.assertEqual(
            {row["document_type"]: row["validation_status"] for row in statuses},
            {EPICRISIS_RECOMMENDATIONS: "valid", OPERATIVE_PROTOCOL: "valid"},
        )
        self.assertEqual(
            set(template_ids),
            {EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL},
        )

    def test_changing_a_mapping_resets_template_validation_status(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        field_id = self.create_common_field()
        template_id = template_ids[EPICRISIS_RECOMMENDATIONS]
        add_variable_mapping(
            self.connection,
            template_id,
            "data_internarii",
            field_id,
            is_required=True,
        )
        self.assertTrue(
            validate_template_set(
                self.connection,
                template_set_id,
                patient_identification_review_confirmed=True,
            ).can_activate
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT validation_status FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()[0],
            "valid",
        )

        update_variable_mapping(
            self.connection,
            template_id,
            "data_internarii",
            field_id,
            is_required=False,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT validation_status FROM templates WHERE id = ?",
                (template_id,),
            ).fetchone()[0],
            "pending",
        )

    def test_new_activation_retires_previous_package_and_preserves_history(self) -> None:
        first_set = self.create_draft()
        first_ids = self.import_pair(first_set)
        self.assertTrue(self.activate(first_set).can_activate)
        session_id = save_generation_session(
            self.connection,
            diagnosis_id=self.diagnosis_id,
            intervention_id=self.intervention_id,
            template_set_id=first_set,
            diagnosis_label_snapshot="Fractură col femural",
            intervention_label_snapshot="Proteza bipolară",
            documents={
                EPICRISIS_RECOMMENDATIONS: (
                    first_ids[EPICRISIS_RECOMMENDATIONS],
                    RichDocument().to_json(),
                ),
                OPERATIVE_PROTOCOL: (
                    first_ids[OPERATIVE_PROTOCOL],
                    RichDocument().to_json(),
                ),
            },
        )
        second_set = self.create_draft()
        second_ids = self.import_pair(second_set, epicrisis="Versiunea nouă", protocol="Protocol nou")
        self.assertTrue(self.activate(second_set).can_activate)

        set_statuses = {
            row["id"]: row["status"]
            for row in self.connection.execute(
                "SELECT id, status FROM template_sets WHERE intervention_id = ?",
                (self.intervention_id,),
            )
        }
        self.assertEqual(set_statuses, {first_set: "retired", second_set: "active"})
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id IN (?, ?)",
                (first_set, second_set),
            ).fetchone()[0],
            4,
        )
        self.assertEqual(set(second_ids), {EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL})

    def test_failed_new_activation_rolls_back_retirement_of_previous_package(self) -> None:
        first_set = self.create_draft()
        self.import_pair(first_set)
        self.assertTrue(self.activate(first_set).can_activate)
        second_set = self.create_draft()
        self.import_pair(second_set)
        self.connection.execute(
            """
            CREATE TRIGGER fail_template_set_activation
            BEFORE UPDATE OF status ON template_sets
            WHEN OLD.id = NEW.id AND NEW.status = 'active'
            BEGIN
                SELECT RAISE(ABORT, 'injected activation failure');
            END
            """
        )
        with self.assertRaises(TemplateSetIntegrityError):
            self.activate(second_set)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (first_set,)
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?", (second_set,)
            ).fetchone()[0],
            "draft",
        )
        self.assertFalse(self.connection.in_transaction)

    def test_published_package_cannot_be_reimported_remapped_or_rejected(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        field_id = self.create_common_field()
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            field_id,
            is_required=True,
        )
        self.assertTrue(self.activate(template_set_id).can_activate)
        with self.assertRaises(TemplateSetStateError):
            import_template(
                self.connection,
                template_set_id,
                EPICRISIS_RECOMMENDATIONS,
                make_docx("Înlocuire interzisă"),
            )
        with self.assertRaises(TemplateSetStateError):
            add_variable_mapping(
                self.connection,
                template_ids[EPICRISIS_RECOMMENDATIONS],
                "data_internarii",
                field_id,
                is_required=False,
            )
        with self.assertRaises(TemplateSetStateError):
            reject_template_set(self.connection, template_set_id, reason="Nu se poate")

    def test_reimport_in_draft_replaces_document_and_clears_old_mappings(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        field_id = self.create_common_field()
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            field_id,
            is_required=True,
        )
        new_source = make_docx("Document înlocuit")
        new_id = import_template(
            self.connection,
            template_set_id,
            EPICRISIS_RECOMMENDATIONS,
            new_source,
        )
        self.assertNotEqual(new_id, template_ids[EPICRISIS_RECOMMENDATIONS])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM template_variables WHERE template_id = ?",
                (new_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT source_docx FROM templates WHERE id = ?", (new_id,)
            ).fetchone()[0],
            new_source,
        )
        report = json.loads(
            self.connection.execute(
                "SELECT import_report_json FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0]
        )
        self.assertFalse(report["patient_identification_review"]["confirmed"])

    def test_validation_report_lists_stale_versions_and_confirmation_state(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        first_id = self.create_common_field()
        add_variable_mapping(
            self.connection,
            template_ids[EPICRISIS_RECOMMENDATIONS],
            "data_internarii",
            first_id,
            is_required=True,
        )
        create_variable_version(
            self.connection,
            "data_internarii",
            "Versiunea doi",
            "date",
            "common",
        )
        set_current_variable_version(self.connection, "data_internarii", 2)
        result = validate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=True,
        )
        saved = get_template_set_details(self.connection, template_set_id)
        self.assertIn("outdated_field_version", {issue["code"] for issue in result.issues})
        self.assertEqual(
            saved["import_report"]["outdated_field_versions"][0]["version"],
            1,
        )
        self.assertTrue(saved["import_report"]["patient_identification_review"]["confirmed"])

    def test_mapping_field_definition_must_exist(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="{{data_internarii}}")
        with self.assertRaises(TemplateSetNotFoundError):
            add_variable_mapping(
                self.connection,
                template_ids[EPICRISIS_RECOMMENDATIONS],
                "data_internarii",
                9999,
                is_required=True,
            )

    def test_direct_extra_mapping_is_reported_even_when_sql_relations_are_valid(self) -> None:
        template_set_id = self.create_draft()
        template_ids = self.import_pair(template_set_id, epicrisis="Text fără variabile")
        extra_field = self.create_common_field("camp_extra")
        self.connection.execute(
            """
            INSERT INTO template_variables (
                template_id, variable_key, field_definition_id, is_required
            ) VALUES (?, 'camp_extra', ?, 1)
            """,
            (template_ids[EPICRISIS_RECOMMENDATIONS], extra_field),
        )
        result = self.activate(template_set_id)
        self.assertIn("extra_variable_mapping", {issue["code"] for issue in result.issues})

    def test_schema_templates_are_retained_after_package_replacement(self) -> None:
        template_set_id = self.create_draft()
        self.import_pair(template_set_id)
        self.assertTrue(self.activate(template_set_id).can_activate)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            )


def make_warning_docx(text: str) -> bytes:
    document = Document()
    paragraph = document.add_paragraph(text)
    marker = OxmlElement("w:proofErr")
    marker.set(qn("w:type"), "spellStart")
    paragraph._p.insert(0, marker)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class IndependentTemplateServiceV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "independent-templates.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)
        self.diagnosis_id = add_diagnosis(self.connection, "Coxartroză bilaterală")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Artroplastie totală șold",
        )

    def test_import_list_and_content_for_diagnosis_and_intervention(self) -> None:
        epi_bytes_1 = make_docx("Epicriză {{sex}} {{varsta}} {{observatii}}")
        epi_bytes_2 = make_docx("Epicriză v2 {{sex}}")
        proto_bytes = make_docx("Protocol {{lateralitate}} {{implant}}")

        with self.assertRaises(TemplateSetValidationError):
            import_independent_template(
                self.connection,
                owner_type="diagnosis",
                owner_id=self.diagnosis_id,
                document_bytes=epi_bytes_1,
                document_type=OPERATIVE_PROTOCOL,
            )

        d1 = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=epi_bytes_1,
        )
        d2 = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=epi_bytes_2,
        )
        p1 = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=proto_bytes,
        )

        self.assertEqual(d1["version_no"], 1)
        self.assertEqual(d2["version_no"], 2)
        self.assertEqual(d1["status"], "draft")
        self.assertEqual(d1["variables"], ["observatii", "sex", "varsta"])
        self.assertEqual(p1["document_type"], OPERATIVE_PROTOCOL)
        self.assertEqual(p1["variables"], ["implant", "lateralitate"])

        diag_versions = list_independent_templates(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
        )
        self.assertEqual([row["version_no"] for row in diag_versions], [2, 1])

        content = get_independent_template_content(self.connection, int(d1["id"]))
        self.assertEqual(content["source_docx"], epi_bytes_1)
        self.assertEqual(content["source_sha256"], hashlib.sha256(epi_bytes_1).hexdigest())
        self.assertIsInstance(content["document"], RichDocument)

    def test_warning_confirmation_patient_review_validation_and_activation_lifecycle(self) -> None:
        proto = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=make_docx("Protocol activ {{lateralitate}}"),
        )
        activate_independent_template(
            self.connection,
            int(proto["id"]),
            patient_identification_review_confirmed=True,
        )

        epi_warn = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=make_warning_docx("Epicriză v1 {{varsta}}"),
        )
        epi_v1_id = int(epi_warn["id"])
        self.assertEqual(epi_warn["parser_report"]["status"], "valid_with_warnings")

        blocked = validate_independent_template(self.connection, epi_v1_id)
        self.assertFalse(blocked["can_activate"])
        issue_codes = {item["code"] for item in blocked["validation_issues"]}
        self.assertEqual(
            issue_codes,
            {"warnings_not_confirmed", "patient_identification_review_not_confirmed"},
        )
        with self.assertRaises(TemplateSetStateError):
            activate_independent_template(self.connection, epi_v1_id)

        confirm_independent_template_warnings(self.connection, epi_v1_id, confirmed=True)
        confirm_independent_template_patient_review(self.connection, epi_v1_id, confirmed=True)
        validated = validate_independent_template(self.connection, epi_v1_id)
        self.assertTrue(validated["can_activate"])
        self.assertEqual(validated["validation_status"], "valid")

        activated_v1 = activate_independent_template(self.connection, epi_v1_id)
        self.assertEqual(activated_v1["status"], "active")

        epi_v2 = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=make_docx("Epicriză v2 {{sex}}"),
        )
        activated_v2 = activate_independent_template(
            self.connection,
            int(epi_v2["id"]),
            patient_identification_review_confirmed=True,
        )
        self.assertEqual(activated_v2["status"], "active")
        self.assertEqual(
            get_independent_template_details(self.connection, epi_v1_id)["status"],
            "retired",
        )
        self.assertEqual(
            get_independent_template_details(self.connection, int(proto["id"]))["status"],
            "active",
        )

    def test_reject_draft_retire_active_and_block_reserved_or_rejected_docx(self) -> None:
        draft_to_reject = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=make_docx("Draft abandonat"),
        )
        rejected = reject_independent_template(self.connection, int(draft_to_reject["id"]))
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(TemplateSetStateError):
            validate_independent_template(self.connection, int(draft_to_reject["id"]))

        reserved_docx = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=make_docx("Nume {{nume_pacient}}"),
        )
        self.assertEqual(reserved_docx["validation_status"], "invalid")
        reserved_validation = validate_independent_template(
            self.connection,
            int(reserved_docx["id"]),
            patient_identification_review_confirmed=True,
        )
        self.assertFalse(reserved_validation["can_activate"])
        self.assertEqual(
            {i["code"] for i in reserved_validation["validation_issues"]},
            {"reserved_patient_name_variable"},
        )

        corrupt = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=b"not-a-valid-zip",
        )
        self.assertEqual(corrupt["validation_status"], "invalid")
        with self.assertRaises(TemplateSetStateError):
            activate_independent_template(
                self.connection,
                int(corrupt["id"]),
                patient_identification_review_confirmed=True,
            )

        active_proto = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=make_docx("Protocol retragere"),
        )
        activate_independent_template(
            self.connection,
            int(active_proto["id"]),
            patient_identification_review_confirmed=True,
        )
        retired = retire_independent_template(self.connection, int(active_proto["id"]))
        self.assertEqual(retired["status"], "retired")
        self.assertTrue(retired["retired_at"])


if __name__ == "__main__":
    unittest.main()
