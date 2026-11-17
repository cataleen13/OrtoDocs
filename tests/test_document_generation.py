"""Teste de integrare pentru serviciul principal de generare."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docx import Document

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import PreparedGeneration
from ortodocs.models.rich_document import LineBreakInline, RichDocument, TextInline
from ortodocs.services import document_generation as generation_service
from ortodocs.services.document_generation import (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
    GenerationConfigurationError,
    GenerationConflictError,
    GenerationContextError,
    GenerationPersistenceError,
    GenerationTemplateError,
    GenerationValidationError,
    generate_documents,
    get_generation_form,
    persist_prepared_documents,
    prepare_documents,
)
from ortodocs.services.medical_library import (
    add_diagnosis,
    add_field_options,
    add_intervention,
    create_variable,
    create_variable_version,
    set_current_variable_version,
    set_diagnosis_active,
    set_intervention_active,
)
from ortodocs.services.template_sets import (
    activate_independent_template,
    activate_template_set,
    add_variable_mapping,
    create_template_set_draft,
    import_independent_template,
    import_template_pair,
    retire_independent_template,
)
from ortodocs.storage.database import connect_database, save_generation_session
from ortodocs.storage.generation_repositories import (
    get_generation_session,
    list_generation_sessions,
    update_generated_document,
)
from ortodocs.storage.migrations import initialize_database
from ortodocs.storage.template_repositories import get_active_template_set


def make_docx_text(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def make_docx_runs(
    paragraphs: tuple[tuple[tuple[str, bool, bool, bool] | None, ...], ...],
) -> bytes:
    document = Document()
    for paragraph_spec in paragraphs:
        paragraph = document.add_paragraph()
        for item in paragraph_spec:
            if item is None:
                paragraph.add_run().add_break()
                continue
            text, bold, italic, underline = item
            run = paragraph.add_run(text)
            run.bold = bold
            run.italic = italic
            run.underline = underline
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def rich_text(document: RichDocument) -> str:
    paragraphs: list[str] = []
    for paragraph in document.paragraphs:
        parts: list[str] = []
        for inline in paragraph.inlines:
            if isinstance(inline, TextInline):
                parts.append(inline.text)
            elif isinstance(inline, LineBreakInline):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


class DocumentGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "document-generation-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = add_diagnosis(self.connection, "Diagnostic de test")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție de test",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _create_field(
        self,
        key: str,
        data_type: str = "text",
        *,
        scope: str = "common",
        options: tuple[tuple[str, str, int], ...] = (),
    ) -> int:
        scope_ids: dict[str, int] = {}
        if scope == "diagnosis":
            scope_ids["diagnosis_id"] = self.diagnosis_id
        elif scope == "intervention":
            scope_ids["intervention_id"] = self.intervention_id
        field_id = create_variable(
            self.connection,
            key,
            f"Etichetă {key}",
            data_type,
            scope,
            **scope_ids,
        )
        if options:
            add_field_options(self.connection, field_id, options)
        return field_id

    def _create_pair_draft(
        self,
        mappings: dict[str, list[tuple[str, int, bool]]],
        *,
        documents: dict[str, bytes] | None = None,
        intervention_id: int | None = None,
        diagnosis_id: int | None = None,
    ) -> tuple[int, dict[str, int]]:
        selected_intervention = intervention_id or self.intervention_id
        selected_diagnosis = diagnosis_id or self.diagnosis_id
        template_set_id = create_template_set_draft(
            self.connection,
            selected_diagnosis,
            selected_intervention,
        )
        if documents is None:
            documents = {}
            for document_type in (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL):
                keys = [key for key, _, _ in mappings.get(document_type, [])]
                content = "Șablon static " + " / ".join(f"{{{{{key}}}}}" for key in keys)
                documents[document_type] = make_docx_text(content)
        template_ids = import_template_pair(
            self.connection,
            template_set_id,
            documents,
        )
        for document_type in (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL):
            for key, field_id, required in mappings.get(document_type, []):
                add_variable_mapping(
                    self.connection,
                    template_ids[document_type],
                    key,
                    field_id,
                    is_required=required,
                )
        return template_set_id, template_ids

    def _activate_pair(
        self,
        mappings: dict[str, list[tuple[str, int, bool]]],
        *,
        documents: dict[str, bytes] | None = None,
        intervention_id: int | None = None,
        diagnosis_id: int | None = None,
    ) -> tuple[int, dict[str, int]]:
        template_set_id, template_ids = self._create_pair_draft(
            mappings,
            documents=documents,
            intervention_id=intervention_id,
            diagnosis_id=diagnosis_id,
        )
        result = activate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=True,
        )
        self.assertTrue(result.can_activate, result.issues)
        return template_set_id, template_ids

    def _prepare_standard_pair(
        self,
        *,
        shared_required: tuple[bool, bool] = (True, True),
        documents: dict[str, bytes] | None = None,
    ) -> tuple[int, dict[str, int], dict[str, int]]:
        fields = {
            "shared": self._create_field("shared"),
            "epi_required": self._create_field("epi_required", scope="diagnosis"),
            "protocol_required": self._create_field(
                "protocol_required",
                scope="intervention",
            ),
            "optional_text": self._create_field("optional_text"),
            "optional_number": self._create_field("optional_number", "number"),
            "optional_date": self._create_field("optional_date", "date"),
            "optional_select": self._create_field(
                "optional_select",
                "select",
                options=(("x", "Opțiunea X", 0), ("y", "Opțiunea Y", 1)),
            ),
        }
        mappings = {
            EPICRISIS_RECOMMENDATIONS: [
                ("shared", fields["shared"], shared_required[0]),
                ("epi_required", fields["epi_required"], True),
                ("optional_text", fields["optional_text"], False),
                ("optional_number", fields["optional_number"], False),
                ("optional_date", fields["optional_date"], False),
                ("optional_select", fields["optional_select"], False),
            ],
            OPERATIVE_PROTOCOL: [
                ("shared", fields["shared"], shared_required[1]),
                ("protocol_required", fields["protocol_required"], True),
            ],
        }
        if documents is None:
            documents = {
                EPICRISIS_RECOMMENDATIONS: make_docx_text(
                    "Epicriză statică {{shared}} / {{epi_required}} / "
                    "{{optional_text}} / {{optional_number}} / "
                    "{{optional_date}} / {{optional_select}}"
                ),
                OPERATIVE_PROTOCOL: make_docx_text(
                    "Protocol static {{shared}} / {{protocol_required}}"
                ),
            }
        template_set_id, template_ids = self._activate_pair(
            mappings,
            documents=documents,
        )
        return template_set_id, template_ids, fields

    @staticmethod
    def _required_values() -> dict[str, str]:
        return {
            "shared": "Șold – 東京",
            "epi_required": "Epicriză completată",
            "protocol_required": "Protocol completat",
        }

    def _session_ids(self) -> list[int]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM generation_sessions ORDER BY id"
            ).fetchall()
        ]

    def _document_rows(self, session_id: int) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT document_type, template_set_id, template_id, rich_text_json
                FROM generated_documents
                WHERE session_id = ?
                ORDER BY document_type
                """,
                (session_id,),
            ).fetchall()
        ]

    def test_form_uses_valid_active_pair_and_preserves_document_usage(self) -> None:
        template_set_id, _, fields = self._prepare_standard_pair(
            shared_required=(False, True),
        )

        form = get_generation_form(self.connection, self.intervention_id)
        form_fields = {field.variable_key: field for field in form.fields}

        self.assertEqual(form.diagnosis_id, self.diagnosis_id)
        self.assertEqual(form.intervention_id, self.intervention_id)
        self.assertEqual(form.template_set_id, template_set_id)
        self.assertEqual(set(form_fields), set(fields))
        self.assertEqual(form_fields["shared"].field_definition_id, fields["shared"])
        self.assertEqual(form_fields["shared"].used_in, (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL))
        self.assertEqual(form_fields["shared"].required_in, (OPERATIVE_PROTOCOL,))
        self.assertEqual(
            tuple(option.option_value for option in form_fields["optional_select"].options),
            ("x", "y"),
        )

    def test_form_refuses_missing_or_inactive_context(self) -> None:
        with self.assertRaises(GenerationTemplateError):
            get_generation_form(self.connection, self.intervention_id)
        with self.assertRaises(ValueError):
            get_generation_form(self.connection, 0)
        with self.assertRaises(GenerationContextError):
            get_generation_form(self.connection, 9999)

        self._prepare_standard_pair()
        other_intervention = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție fără set",
        )
        with self.assertRaises(GenerationTemplateError):
            get_generation_form(self.connection, other_intervention)

        set_intervention_active(self.connection, self.intervention_id, False)
        with self.assertRaisesRegex(GenerationContextError, "inactivă"):
            get_generation_form(self.connection, self.intervention_id)

    def test_form_refuses_inactive_diagnosis(self) -> None:
        self._prepare_standard_pair()
        set_diagnosis_active(self.connection, self.diagnosis_id, False)

        with self.assertRaisesRegex(GenerationContextError, "Diagnosticul"):
            get_generation_form(self.connection, self.intervention_id)

    def test_form_refuses_template_set_for_another_intervention(self) -> None:
        self._prepare_standard_pair()
        other_intervention = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Altă intervenție activă",
        )
        self._activate_pair({}, intervention_id=other_intervention)
        foreign_set = get_active_template_set(self.connection, other_intervention)
        assert foreign_set is not None

        with patch(
            "ortodocs.services.document_generation.template_repositories.get_active_template_set",
            return_value=foreign_set,
        ):
            with self.assertRaisesRegex(GenerationTemplateError, "nu aparține"):
                get_generation_form(self.connection, self.intervention_id)

    def test_form_refuses_a_missing_document_row(self) -> None:
        _, template_ids, _ = self._prepare_standard_pair()
        missing_template_id = template_ids[OPERATIVE_PROTOCOL]
        self.connection.execute("DROP TRIGGER template_variable_no_delete_after_publication")
        self.connection.execute("DROP TRIGGER template_no_delete_after_publication")
        self.connection.execute(
            "DELETE FROM template_variables WHERE template_id = ?",
            (missing_template_id,),
        )
        self.connection.execute("DELETE FROM templates WHERE id = ?", (missing_template_id,))
        with self.assertRaisesRegex(GenerationTemplateError, "exact două șabloane"):
            get_generation_form(self.connection, self.intervention_id)

    def test_form_detects_duplicate_document_metadata_defensively(self) -> None:
        self._prepare_standard_pair()
        from ortodocs.services.template_sets import get_template_set_details

        details = get_template_set_details(
            self.connection,
            get_active_template_set(self.connection, self.intervention_id)["id"],
        )
        details["templates"].append(dict(details["templates"][0]))
        with patch(
            "ortodocs.services.document_generation.template_sets.get_template_set_details",
            return_value=details,
        ):
            with self.assertRaisesRegex(GenerationTemplateError, "exact două șabloane"):
                get_generation_form(self.connection, self.intervention_id)

    def test_form_rejects_invalid_template_status(self) -> None:
        _, template_ids, _ = self._prepare_standard_pair()
        self.connection.execute(
            "DROP TRIGGER template_validation_no_update_after_publication"
        )
        self.connection.execute(
            "UPDATE templates SET validation_status = 'invalid' WHERE id = ?",
            (template_ids[EPICRISIS_RECOMMENDATIONS],),
        )

        with self.assertRaisesRegex(GenerationTemplateError, "nu este validat"):
            get_generation_form(self.connection, self.intervention_id)

    def _assert_form_rejects_mapping_mismatch(self, *, extra: bool) -> None:
        extra_field = self._create_field("not_in_template") if extra else None
        known_field = self._create_field("mapped")
        _, template_ids = self._activate_pair(
            {EPICRISIS_RECOMMENDATIONS: [("mapped", known_field, True)]},
        )
        template_id = template_ids[EPICRISIS_RECOMMENDATIONS]
        if extra:
            self.connection.execute("DROP TRIGGER template_variable_scope_guard_insert")
            self.connection.execute(
                """
                INSERT INTO template_variables (
                    template_id, variable_key, field_definition_id, is_required
                ) VALUES (?, 'not_in_template', ?, 0)
                """,
                (template_id, extra_field),
            )
        else:
            self.connection.execute(
                "DROP TRIGGER template_variable_no_delete_after_publication"
            )
            self.connection.execute(
                "DELETE FROM template_variables WHERE template_id = ?",
                (template_id,),
            )
        with self.assertRaisesRegex(GenerationTemplateError, "nu corespund exact"):
            get_generation_form(self.connection, self.intervention_id)

    def test_form_rejects_missing_mapping_rows(self) -> None:
        self._assert_form_rejects_mapping_mismatch(extra=False)

    def test_form_rejects_extra_mapping_rows(self) -> None:
        self._assert_form_rejects_mapping_mismatch(extra=True)

    def test_form_rejects_common_variable_mapped_to_different_field_ids_and_versions(self) -> None:
        field_v1 = self._create_field("shared")
        field_v2 = create_variable_version(
            self.connection,
            "shared",
            "Etichetă shared v2",
            "text",
            "common",
        )
        set_current_variable_version(self.connection, "shared", 2)
        template_set_id, template_ids = self._activate_pair(
            {
                EPICRISIS_RECOMMENDATIONS: [("shared", field_v2, True)],
                OPERATIVE_PROTOCOL: [("shared", field_v2, True)],
            },
        )
        self.assertGreater(field_v2, field_v1)
        self.connection.execute("DROP TRIGGER template_variable_scope_guard_update")
        self.connection.execute("DROP TRIGGER template_variable_no_update_after_publication")
        self.connection.execute(
            """
            UPDATE template_variables SET field_definition_id = ?
            WHERE template_id = ? AND variable_key = 'shared'
            """,
            (field_v1, template_ids[OPERATIVE_PROTOCOL]),
        )

        with self.assertRaisesRegex(GenerationTemplateError, "definiții sau versiuni diferite"):
            get_generation_form(self.connection, self.intervention_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "active",
        )

    def test_historical_select_version_is_used_for_form_and_generation(self) -> None:
        select_v1 = self._create_field(
            "choice",
            "select",
            options=(("old_a", "Veche A", 0), ("old_b", "Veche B", 1)),
        )
        template_set_id, template_ids = self._activate_pair(
            {
                EPICRISIS_RECOMMENDATIONS: [("choice", select_v1, True)],
            },
            documents={
                EPICRISIS_RECOMMENDATIONS: make_docx_text("Alegere: {{choice}}"),
                OPERATIVE_PROTOCOL: make_docx_text("Protocol static"),
            },
        )
        select_v2 = create_variable_version(
            self.connection,
            "choice",
            "Etichetă choice v2",
            "select",
            "common",
        )
        add_field_options(self.connection, select_v2, [("new", "Nou", 0)])
        set_current_variable_version(self.connection, "choice", 2)

        form = get_generation_form(self.connection, self.intervention_id)
        choice = form.fields[0]
        self.assertEqual(choice.field_definition_id, select_v1)
        self.assertEqual(choice.version, 1)
        self.assertEqual(
            tuple(option.option_value for option in choice.options),
            ("old_a", "old_b"),
        )

        templates_before = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT id, document_type, source_docx, source_sha256, parsed_content_json
                FROM templates WHERE template_set_id = ? ORDER BY document_type
                """,
                (template_set_id,),
            ).fetchall()
        ]
        mappings_before = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT template_id, variable_key, field_definition_id, is_required
                FROM template_variables WHERE template_id IN (?, ?) ORDER BY template_id
                """,
                tuple(template_ids.values()),
            ).fetchall()
        ]

        generated = generate_documents(
            self.connection,
            self.intervention_id,
            {"choice": "old_a"},
        )
        expected_text = {
            EPICRISIS_RECOMMENDATIONS: "Alegere: old_a",
            OPERATIVE_PROTOCOL: "Protocol static",
        }
        returned_documents = {
            EPICRISIS_RECOMMENDATIONS: generated.epicrisis_recommendations,
            OPERATIVE_PROTOCOL: generated.operative_protocol,
        }
        for document_type, returned_document in returned_documents.items():
            self.assertEqual(rich_text(returned_document), expected_text[document_type])
        stored_rows = self._document_rows(generated.session_id)
        self.assertEqual(len(stored_rows), 2)
        stored_by_type = {row[0]: row[3] for row in stored_rows}
        self.assertEqual(set(stored_by_type), {EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL})
        for document_type, returned_document in returned_documents.items():
            stored_document = RichDocument.from_json(stored_by_type[document_type])
            self.assertEqual(stored_document, returned_document)
            self.assertEqual(rich_text(stored_document), expected_text[document_type])

        sessions_before_rejection = self._session_ids()
        with self.assertRaises(GenerationValidationError) as raised:
            generate_documents(
                self.connection,
                self.intervention_id,
                {"choice": "new"},
            )
        issue = next(item for item in raised.exception.issues if item.variable_key == "choice")
        self.assertEqual(issue.reason, "invalid_value")
        self.assertEqual(self._session_ids(), sessions_before_rejection)

        templates_after = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT id, document_type, source_docx, source_sha256, parsed_content_json
                FROM templates WHERE template_set_id = ? ORDER BY document_type
                """,
                (template_set_id,),
            ).fetchall()
        ]
        mappings_after = [
            tuple(row)
            for row in self.connection.execute(
                """
                SELECT template_id, variable_key, field_definition_id, is_required
                FROM template_variables WHERE template_id IN (?, ?) ORDER BY template_id
                """,
                tuple(template_ids.values()),
            ).fetchall()
        ]
        self.assertEqual(templates_after, templates_before)
        self.assertEqual(mappings_after, mappings_before)

    def test_optional_usage_in_both_documents_is_not_marked_required(self) -> None:
        field_id = self._create_field("optional_shared")
        self._activate_pair(
            {
                EPICRISIS_RECOMMENDATIONS: [("optional_shared", field_id, False)],
                OPERATIVE_PROTOCOL: [("optional_shared", field_id, False)],
            },
        )
        field = get_generation_form(self.connection, self.intervention_id).fields[0]
        self.assertEqual(field.used_in, (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL))
        self.assertEqual(field.required_in, ())

    def test_validation_reports_required_fields_per_document_and_together(self) -> None:
        self._prepare_standard_pair()
        with self.assertRaises(GenerationValidationError) as raised:
            generate_documents(self.connection, self.intervention_id, {})

        issues = {issue.variable_key: issue for issue in raised.exception.issues}
        self.assertEqual(set(issues), {"shared", "epi_required", "protocol_required"})
        self.assertEqual(
            issues["shared"].document_types,
            (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
        )
        self.assertEqual(issues["epi_required"].document_types, (EPICRISIS_RECOMMENDATIONS,))
        self.assertEqual(issues["protocol_required"].document_types, (OPERATIVE_PROTOCOL,))
        self.assertEqual(self._session_ids(), [])

    def test_required_whitespace_and_unknown_or_non_string_inputs_are_reported(self) -> None:
        self._prepare_standard_pair()
        values: dict[Any, Any] = self._required_values()
        values["shared"] = "   "
        values["unknown"] = "value"
        values[42] = "value"
        values["epi_required"] = 7

        with self.assertRaises(GenerationValidationError) as raised:
            generate_documents(self.connection, self.intervention_id, values)  # type: ignore[arg-type]

        reasons = {issue.reason for issue in raised.exception.issues}
        self.assertTrue({"required_empty", "unknown_key", "invalid_key_type", "invalid_value_type"}.issubset(reasons))
        self.assertEqual(self._session_ids(), [])

    def test_values_must_be_a_mapping(self) -> None:
        self._prepare_standard_pair()
        for values in (None, [], "not a mapping"):
            with self.subTest(values_type=type(values).__name__):
                with self.assertRaises(GenerationValidationError) as raised:
                    generate_documents(
                        self.connection,
                        self.intervention_id,
                        values,  # type: ignore[arg-type]
                    )
                self.assertEqual(raised.exception.issues[0].reason, "invalid_mapping")
                self.assertEqual(self._session_ids(), [])

    def test_optional_empty_number_date_and_select_values_are_substituted_and_saved(self) -> None:
        self._prepare_standard_pair()
        required_values = self._required_values()
        expected_text = {
            EPICRISIS_RECOMMENDATIONS: (
                f"Epicriză statică {required_values['shared']} / "
                f"{required_values['epi_required']} /  /  /  / "
            ),
            OPERATIVE_PROTOCOL: (
                f"Protocol static {required_values['shared']} / "
                f"{required_values['protocol_required']}"
            ),
        }
        cases = (
            ("omitted", dict(required_values)),
            (
                "explicit_empty",
                {
                    **required_values,
                    "optional_number": "",
                    "optional_date": "",
                    "optional_select": "",
                },
            ),
        )
        for case_name, values in cases:
            with self.subTest(case=case_name):
                generated = generate_documents(self.connection, self.intervention_id, values)
                returned_documents = {
                    EPICRISIS_RECOMMENDATIONS: generated.epicrisis_recommendations,
                    OPERATIVE_PROTOCOL: generated.operative_protocol,
                }
                stored_rows = self._document_rows(generated.session_id)
                self.assertEqual(len(stored_rows), 2)
                stored_by_type = {row[0]: row[3] for row in stored_rows}
                self.assertEqual(
                    set(stored_by_type),
                    {EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL},
                )
                for document_type, returned_document in returned_documents.items():
                    with self.subTest(case=case_name, document=document_type):
                        self.assertEqual(rich_text(returned_document), expected_text[document_type])
                        stored_document = RichDocument.from_json(stored_by_type[document_type])
                        self.assertEqual(stored_document, returned_document)
                        self.assertEqual(rich_text(stored_document), expected_text[document_type])
                        self.assertNotIn("{{optional_number}}", rich_text(stored_document))
                        self.assertNotIn("{{optional_date}}", rich_text(stored_document))
                        self.assertNotIn("{{optional_select}}", rich_text(stored_document))

    def test_optional_nonempty_values_are_validated_and_preserved_literally(self) -> None:
        self._prepare_standard_pair()
        values = self._required_values()
        values.update(
            {
                "optional_text": "  Diacritice Ș și text {{shared}}  ",
                "optional_number": "-12.50",
                "optional_date": "29.02.2024",
                "optional_select": "x",
            }
        )
        generated = generate_documents(self.connection, self.intervention_id, values)

        epi_text = rich_text(generated.epicrisis_recommendations)
        self.assertIn("  Diacritice Ș și text {{shared}}  ", epi_text)
        self.assertIn("-12.50", epi_text)
        self.assertIn("29.02.2024", epi_text)
        self.assertIn("x", epi_text)

    def test_text_values_reject_controls_and_preserve_unicode(self) -> None:
        self._prepare_standard_pair()
        values = self._required_values()
        values["shared"] = "fractură – Șold 東京"
        generated = generate_documents(self.connection, self.intervention_id, values)
        self.assertIn("fractură – Șold 東京", rich_text(generated.epicrisis_recommendations))

        for invalid_value in ("a\nb", "a\rb", "a\tb", "a\x01b", "a\ud800b"):
            with self.subTest(value=repr(invalid_value)):
                invalid_values = self._required_values()
                invalid_values["shared"] = invalid_value
                with self.assertRaises(GenerationValidationError) as raised:
                    generate_documents(self.connection, self.intervention_id, invalid_values)
                self.assertIn("invalid_value", {issue.reason for issue in raised.exception.issues})

    def test_number_values_accept_integer_decimal_and_minus_and_reject_invalid_values(self) -> None:
        self._prepare_standard_pair()
        for valid_value in ("12", "12.75", "-0.25", "+7"):
            with self.subTest(value=valid_value):
                values = self._required_values()
                values["optional_number"] = valid_value
                generated = generate_documents(self.connection, self.intervention_id, values)
                self.assertIn(valid_value, rich_text(generated.epicrisis_recommendations))

        for invalid_value in ("not-number", "1,25", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=invalid_value):
                values = self._required_values()
                values["optional_number"] = invalid_value
                with self.assertRaises(GenerationValidationError) as raised:
                    generate_documents(self.connection, self.intervention_id, values)
                issue = next(item for item in raised.exception.issues if item.variable_key == "optional_number")
                self.assertEqual(issue.reason, "invalid_value")

    def test_date_values_require_exact_calendar_valid_dd_mm_yyyy(self) -> None:
        self._prepare_standard_pair()
        values = self._required_values()
        values["optional_date"] = "10.11.2026"
        generated = generate_documents(self.connection, self.intervention_id, values)
        self.assertIn("10.11.2026", rich_text(generated.epicrisis_recommendations))

        for invalid_value in ("31.02.2026", "2026-11-10", "1.2.2026"):
            with self.subTest(value=invalid_value):
                invalid_values = self._required_values()
                invalid_values["optional_date"] = invalid_value
                with self.assertRaises(GenerationValidationError) as raised:
                    generate_documents(self.connection, self.intervention_id, invalid_values)
                issue = next(item for item in raised.exception.issues if item.variable_key == "optional_date")
                self.assertEqual(issue.reason, "invalid_value")

    def test_select_values_accept_only_option_values_from_mapped_version(self) -> None:
        self._prepare_standard_pair()
        values = self._required_values()
        values["optional_select"] = "x"
        generated = generate_documents(self.connection, self.intervention_id, values)
        self.assertIn("x", rich_text(generated.epicrisis_recommendations))

        values["optional_select"] = "Opțiunea X"
        with self.assertRaises(GenerationValidationError) as raised:
            generate_documents(self.connection, self.intervention_id, values)
        issue = next(item for item in raised.exception.issues if item.variable_key == "optional_select")
        self.assertEqual(issue.reason, "invalid_value")

    def test_prepare_valid_generation_is_read_only_and_leaves_no_transaction(self) -> None:
        self._prepare_standard_pair()
        before_changes = self.connection.total_changes

        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )

        self.assertIsInstance(prepared, PreparedGeneration)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.total_changes, before_changes)
        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )

    def test_invalid_preparation_preserves_validation_issues_and_writes_nothing(self) -> None:
        self._prepare_standard_pair()
        before_changes = self.connection.total_changes

        with self.assertRaises(GenerationValidationError) as wrapper_error:
            generate_documents(self.connection, self.intervention_id, {})
        with self.assertRaises(GenerationValidationError) as raised:
            prepare_documents(self.connection, self.intervention_id, {})

        self.assertEqual(raised.exception.issues, wrapper_error.exception.issues)
        self.assertEqual({issue.reason for issue in raised.exception.issues}, {"required_missing"})
        self.assertEqual(self.connection.total_changes, before_changes)
        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_persist_prepared_saves_exact_documents_and_template_ids(self) -> None:
        template_set_id, template_ids, _ = self._prepare_standard_pair()
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        expected_json = {
            EPICRISIS_RECOMMENDATIONS: prepared.epicrisis_recommendations_json,
            OPERATIVE_PROTOCOL: prepared.operative_protocol_json,
        }
        self.assertFalse(self.connection.in_transaction)

        generated = persist_prepared_documents(self.connection, prepared)

        self.assertFalse(self.connection.in_transaction)
        session = self.connection.execute(
            "SELECT diagnosis_id, intervention_id, template_set_id, status "
            "FROM generation_sessions WHERE id = ?",
            (generated.session_id,),
        ).fetchone()
        self.assertEqual(
            tuple(session),
            (self.diagnosis_id, self.intervention_id, template_set_id, "complete"),
        )
        rows = self._document_rows(generated.session_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row[0]: (row[1], row[2], row[3]) for row in rows},
            {
                EPICRISIS_RECOMMENDATIONS: (
                    template_set_id,
                    template_ids[EPICRISIS_RECOMMENDATIONS],
                    expected_json[EPICRISIS_RECOMMENDATIONS],
                ),
                OPERATIVE_PROTOCOL: (
                    template_set_id,
                    template_ids[OPERATIVE_PROTOCOL],
                    expected_json[OPERATIVE_PROTOCOL],
                ),
            },
        )
        self.assertEqual(
            generated.epicrisis_recommendations.to_json(),
            expected_json[EPICRISIS_RECOMMENDATIONS],
        )
        self.assertEqual(
            generated.operative_protocol.to_json(),
            expected_json[OPERATIVE_PROTOCOL],
        )
        self.assertIsNot(generated.epicrisis_recommendations, generated.operative_protocol)

    def test_prepared_json_is_immutable_and_detached_from_values_and_source_documents(self) -> None:
        self._prepare_standard_pair()
        values = self._required_values()
        loaded_templates: dict[str, RichDocument] = {}
        load_context = generation_service._load_generation_context

        def capture_source_documents(*args: Any) -> Any:
            result = load_context(*args)
            loaded_templates.update(result[1])
            return result

        with patch.object(
            generation_service,
            "_load_generation_context",
            side_effect=capture_source_documents,
        ):
            prepared = prepare_documents(self.connection, self.intervention_id, values)

        expected_epi_json = prepared.epicrisis_recommendations_json
        expected_protocol_json = prepared.operative_protocol_json
        values["shared"] = "changed after preparation"
        for source in loaded_templates.values():
            for paragraph in source.paragraphs:
                for inline in paragraph.inlines:
                    if isinstance(inline, TextInline):
                        inline.text = "changed source template"
                        break
                else:
                    continue
                break

        self.assertEqual(prepared.epicrisis_recommendations_json, expected_epi_json)
        self.assertEqual(prepared.operative_protocol_json, expected_protocol_json)
        with self.assertRaises(FrozenInstanceError):
            prepared.epicrisis_recommendations_json = "{}"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            prepared.template_set_id = 999  # type: ignore[misc]

    def test_context_deactivated_between_preparation_and_persistence_is_rejected(self) -> None:
        self._prepare_standard_pair()
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        set_diagnosis_active(self.connection, self.diagnosis_id, False)

        with self.assertRaises(GenerationConflictError):
            persist_prepared_documents(self.connection, prepared)

        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

        set_diagnosis_active(self.connection, self.diagnosis_id, True)
        set_intervention_active(self.connection, self.intervention_id, False)
        with self.assertRaises(GenerationConflictError):
            persist_prepared_documents(self.connection, prepared)
        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_active_template_set_change_between_phases_is_rejected(self) -> None:
        _, _, fields = self._prepare_standard_pair()
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        second_set, _ = self._create_pair_draft(
            {
                EPICRISIS_RECOMMENDATIONS: [
                    ("shared", fields["shared"], True),
                    ("epi_required", fields["epi_required"], True),
                    ("optional_text", fields["optional_text"], False),
                    ("optional_number", fields["optional_number"], False),
                    ("optional_date", fields["optional_date"], False),
                    ("optional_select", fields["optional_select"], False),
                ],
                OPERATIVE_PROTOCOL: [
                    ("shared", fields["shared"], True),
                    ("protocol_required", fields["protocol_required"], True),
                ],
            },
        )
        activation = activate_template_set(
            self.connection,
            second_set,
            patient_identification_review_confirmed=True,
        )
        self.assertTrue(activation.can_activate, activation.issues)

        with self.assertRaises(GenerationConflictError):
            persist_prepared_documents(self.connection, prepared)

        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )

    def test_prepared_second_document_failure_rolls_back_entire_session(self) -> None:
        self._prepare_standard_pair()
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        self.connection.execute(
            """
            CREATE TRIGGER fail_prepared_second_document
            BEFORE INSERT ON generated_documents
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'injected prepared document failure');
            END
            """
        )

        with self.assertRaisesRegex(GenerationPersistenceError, "prepared document failure"):
            persist_prepared_documents(self.connection, prepared)

        self.assertEqual(self._session_ids(), [])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_prepared_retention_failure_rolls_back_new_session_and_old_history(self) -> None:
        self._prepare_standard_pair()
        for _ in range(10):
            generate_documents(self.connection, self.intervention_id, self._required_values())
        before_sessions = self._session_ids()
        before_document_count = self.connection.execute(
            "SELECT COUNT(*) FROM generated_documents"
        ).fetchone()[0]
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        self.connection.execute(
            """
            CREATE TRIGGER fail_prepared_retention
            BEFORE UPDATE OF status ON generation_sessions
            WHEN OLD.status = 'complete' AND NEW.status = 'deleting'
            BEGIN
                SELECT RAISE(ABORT, 'injected prepared retention failure');
            END
            """
        )

        with self.assertRaisesRegex(GenerationPersistenceError, "prepared retention failure"):
            persist_prepared_documents(self.connection, prepared)

        self.assertEqual(self._session_ids(), before_sessions)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            before_document_count,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_generate_documents_remains_a_prepare_then_persist_compatible_wrapper(self) -> None:
        self._prepare_standard_pair()
        prepared = prepare_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        generated = generate_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )

        self.assertEqual(
            generated.epicrisis_recommendations.to_json(),
            prepared.epicrisis_recommendations_json,
        )
        self.assertEqual(
            generated.operative_protocol.to_json(),
            prepared.operative_protocol_json,
        )
        self.assertEqual(len(self._document_rows(generated.session_id)), 2)
        self.assertFalse(self.connection.in_transaction)

    def test_generated_pair_is_saved_complete_with_correct_template_ids_and_snapshots(self) -> None:
        template_set_id, template_ids, _ = self._prepare_standard_pair()
        generated = generate_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )

        session = self.connection.execute(
            "SELECT diagnosis_id, intervention_id, template_set_id, diagnosis_label_snapshot, "
            "intervention_label_snapshot, status FROM generation_sessions WHERE id = ?",
            (generated.session_id,),
        ).fetchone()
        self.assertEqual(tuple(session[:3]), (self.diagnosis_id, self.intervention_id, template_set_id))
        self.assertEqual(session[3], "Diagnostic de test")
        self.assertEqual(session[4], "Intervenție de test")
        self.assertEqual(session[5], "complete")
        rows = self._document_rows(generated.session_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row[0]: (row[1], row[2]) for row in rows},
            {
                EPICRISIS_RECOMMENDATIONS: (template_set_id, template_ids[EPICRISIS_RECOMMENDATIONS]),
                OPERATIVE_PROTOCOL: (template_set_id, template_ids[OPERATIVE_PROTOCOL]),
            },
        )
        self.assertIsNot(generated.epicrisis_recommendations, generated.operative_protocol)

    def test_validation_finishes_before_substitution_or_saving(self) -> None:
        self._prepare_standard_pair()
        with (
            patch.object(generation_service, "substitute_variables") as substitute,
            patch.object(generation_service, "save_generation_session") as save,
        ):
            with self.assertRaises(GenerationValidationError):
                generate_documents(self.connection, self.intervention_id, {})
        substitute.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self._session_ids(), [])

    def test_second_document_failure_rolls_back_and_preserves_old_history(self) -> None:
        self._prepare_standard_pair()
        for _ in range(10):
            generate_documents(self.connection, self.intervention_id, self._required_values())
        before_sessions = self._session_ids()
        before_documents = sum(
            self.connection.execute(
                "SELECT COUNT(*) FROM generated_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            for session_id in before_sessions
        )
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

        with self.assertRaisesRegex(GenerationPersistenceError, "second document failure"):
            generate_documents(self.connection, self.intervention_id, self._required_values())

        self.assertEqual(self._session_ids(), before_sessions)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            before_documents,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_active_template_set_change_before_save_is_a_conflict(self) -> None:
        _, _, fields = self._prepare_standard_pair()
        second_set, _ = self._create_pair_draft(
            {
                EPICRISIS_RECOMMENDATIONS: [
                    ("shared", fields["shared"], True),
                    ("epi_required", fields["epi_required"], True),
                    ("optional_text", fields["optional_text"], False),
                    ("optional_number", fields["optional_number"], False),
                    ("optional_date", fields["optional_date"], False),
                    ("optional_select", fields["optional_select"], False),
                ],
                OPERATIVE_PROTOCOL: [
                    ("shared", fields["shared"], True),
                    ("protocol_required", fields["protocol_required"], True),
                ],
            },
        )
        real_save = save_generation_session

        def activate_new_pair_then_save(*args: Any, **kwargs: Any) -> int:
            result = activate_template_set(
                self.connection,
                second_set,
                patient_identification_review_confirmed=True,
            )
            self.assertTrue(result.can_activate, result.issues)
            active = get_active_template_set(self.connection, self.intervention_id)
            self.assertIsNotNone(active)
            self.assertEqual(active["id"], second_set)
            return real_save(*args, **kwargs)

        # Switch the active pair only after generation has loaded and substituted
        # the first pair, immediately before the atomic repository save.
        with patch.object(
            generation_service,
            "save_generation_session",
            side_effect=activate_new_pair_then_save,
        ):
            with self.assertRaises(GenerationConflictError):
                generate_documents(self.connection, self.intervention_id, self._required_values())
        self.assertEqual(self._session_ids(), [])

    def _assert_conflict_after_deactivation(self, *, target: str) -> None:
        self._prepare_standard_pair()
        real_save = save_generation_session

        def deactivate_then_save(*args: Any, **kwargs: Any) -> int:
            if target == "diagnosis":
                self.connection.execute(
                    "UPDATE diagnoses SET is_active = 0 WHERE id = ?",
                    (self.diagnosis_id,),
                )
            else:
                self.connection.execute(
                    "UPDATE interventions SET is_active = 0 WHERE id = ?",
                    (self.intervention_id,),
                )
            return real_save(*args, **kwargs)

        with patch.object(
            generation_service,
            "save_generation_session",
            side_effect=deactivate_then_save,
        ):
            with self.assertRaises(GenerationConflictError):
                generate_documents(
                    self.connection,
                    self.intervention_id,
                    self._required_values(),
                )
        self.assertEqual(self._session_ids(), [])

    def test_inactive_diagnosis_before_save_is_a_conflict(self) -> None:
        self._assert_conflict_after_deactivation(target="diagnosis")

    def test_inactive_intervention_before_save_is_a_conflict(self) -> None:
        self._assert_conflict_after_deactivation(target="intervention")

    def test_active_transaction_is_refused_without_closing_callers_transaction(self) -> None:
        self._prepare_standard_pair()
        self.connection.execute("BEGIN IMMEDIATE")

        with self.assertRaises(GenerationConflictError):
            generate_documents(self.connection, self.intervention_id, self._required_values())

        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self._session_ids(), [])
        self.connection.execute("ROLLBACK")

    def test_disabled_foreign_keys_raise_configuration_error_not_conflict(self) -> None:
        self._prepare_standard_pair()
        self.connection.execute("PRAGMA foreign_keys = OFF")

        with self.assertRaisesRegex(GenerationConfigurationError, "PRAGMA foreign_keys = ON"):
            generate_documents(self.connection, self.intervention_id, self._required_values())

        self.assertEqual(self._session_ids(), [])

    def test_generation_preserves_formatting_paragraphs_and_line_breaks(self) -> None:
        shared = self._create_field("shared")
        epi_required = self._create_field("epi_required", scope="diagnosis")
        protocol_required = self._create_field("protocol_required", scope="intervention")
        mappings = {
            EPICRISIS_RECOMMENDATIONS: [("shared", shared, True), ("epi_required", epi_required, True)],
            OPERATIVE_PROTOCOL: [("shared", shared, True), ("protocol_required", protocol_required, True)],
        }
        documents = {
            EPICRISIS_RECOMMENDATIONS: make_docx_runs(
                (
                    (
                        ("Static bold ", True, False, False),
                        ("{{shared}}", False, True, False),
                        (" / ", False, False, False),
                        ("{{epi_required}}", False, False, True),
                        None,
                        ("linie nouă", False, False, False),
                    ),
                    (("al doilea paragraf", False, False, False),),
                ),
            ),
            OPERATIVE_PROTOCOL: make_docx_runs(
                ((("Protocol ", False, False, False), ("{{shared}}", True, False, True), (" {{protocol_required}}", False, False, False)),),
            ),
        }
        self._activate_pair(mappings, documents=documents)
        generated = generate_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )

        epicrisis = generated.epicrisis_recommendations
        text_fragments = [
            inline
            for paragraph in epicrisis.paragraphs
            for inline in paragraph.inlines
            if isinstance(inline, TextInline)
        ]
        self.assertTrue(any(inline.text == "Static bold " and inline.bold for inline in text_fragments))
        self.assertTrue(any(inline.text == "Șold – 東京" and inline.italic for inline in text_fragments))
        self.assertTrue(any(inline.text == "Epicriză completată" and inline.underline for inline in text_fragments))
        self.assertTrue(any(isinstance(inline, LineBreakInline) for inline in epicrisis.paragraphs[0].inlines))
        self.assertEqual(len(epicrisis.paragraphs), 2)
        self.assertIn("al doilea paragraf", rich_text(epicrisis))
        protocol_shared = next(
            inline
            for paragraph in generated.operative_protocol.paragraphs
            for inline in paragraph.inlines
            if isinstance(inline, TextInline) and inline.text == "Șold – 東京"
        )
        self.assertTrue(protocol_shared.bold)
        self.assertTrue(protocol_shared.underline)

    def test_repeated_tokens_and_token_like_values_are_literal_and_non_recursive(self) -> None:
        shared = self._create_field("shared")
        optional = self._create_field("optional_text")
        self._activate_pair(
            {
                EPICRISIS_RECOMMENDATIONS: [("shared", shared, True), ("optional_text", optional, False)],
                OPERATIVE_PROTOCOL: [("shared", shared, True)],
            },
            documents={
                EPICRISIS_RECOMMENDATIONS: make_docx_text("{{shared}} {{shared}} {{optional_text}}"),
                OPERATIVE_PROTOCOL: make_docx_text("{{shared}}"),
            },
        )
        values = {"shared": "literal {{other}} {brace}", "optional_text": "{{shared}}"}
        generated = generate_documents(self.connection, self.intervention_id, values)
        self.assertEqual(
            rich_text(generated.epicrisis_recommendations),
            "literal {{other}} {brace} literal {{other}} {brace} {{shared}}",
        )
        self.assertEqual(rich_text(generated.operative_protocol), "literal {{other}} {brace}")

    def test_templates_and_sessions_remain_independent_across_repeated_generation(self) -> None:
        _, template_ids, _ = self._prepare_standard_pair()
        template_before = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT id, source_sha256, parsed_content_json FROM templates ORDER BY id"
            ).fetchall()
        ]
        first = generate_documents(
            self.connection,
            self.intervention_id,
            self._required_values(),
        )
        first.epicrisis_recommendations.paragraphs[0].inlines[0].text = "editare locală"
        self.assertNotEqual(
            first.epicrisis_recommendations.paragraphs[0].inlines[0].text,
            first.operative_protocol.paragraphs[0].inlines[0].text,
        )
        second_values = self._required_values()
        second_values["shared"] = "A doua sesiune"
        second = generate_documents(self.connection, self.intervention_id, second_values)

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotIn("Șold – 東京", rich_text(second.epicrisis_recommendations))
        self.assertIn("A doua sesiune", rich_text(second.epicrisis_recommendations))
        template_after = [
            tuple(row)
            for row in self.connection.execute(
                "SELECT id, source_sha256, parsed_content_json FROM templates ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(template_after, template_before)
        self.assertEqual(set(template_ids.values()), {row[0] for row in template_before})
        stored_first = self.connection.execute(
            "SELECT rich_text_json FROM generated_documents WHERE session_id = ? AND document_type = ?",
            (first.session_id, EPICRISIS_RECOMMENDATIONS),
        ).fetchone()[0]
        self.assertNotIn("editare locală", stored_first)

    def test_token_document_mismatch_blocks_generation_without_saving(self) -> None:
        _, template_ids, fields = self._prepare_standard_pair()
        self.connection.execute("DROP TRIGGER template_content_no_update")
        corrupted = RichDocument.from_json(
            RichDocument().to_json()
        )
        from ortodocs.models.rich_document import Paragraph

        corrupted.paragraphs.append(Paragraph([TextInline("{{unknown_token}}")]))
        self.connection.execute(
            "UPDATE templates SET parsed_content_json = ? WHERE id = ?",
            (corrupted.to_json(), template_ids[EPICRISIS_RECOMMENDATIONS]),
        )
        values = self._required_values()

        with self.assertRaises(GenerationTemplateError):
            generate_documents(self.connection, self.intervention_id, values)
        self.assertEqual(self._session_ids(), [])
        self.assertEqual(fields["shared"] > 0, True)


class IndependentDocumentGenerationV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "v3-generation-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)
        self.diagnosis_id = add_diagnosis(self.connection, "Fractură pertrohanteriană")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Osteosinteză cu tijă Gamma",
        )

    def _activate_epicrisis(self, text: str, *, diagnosis_id: int | None = None) -> int:
        owner_id = self.diagnosis_id if diagnosis_id is None else diagnosis_id
        draft = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=owner_id,
            document_bytes=make_docx_text(text),
        )
        activated = activate_independent_template(
            self.connection,
            int(draft["id"]),
            patient_identification_review_confirmed=True,
        )
        return int(activated["id"])

    def _activate_protocol(self, text: str, *, intervention_id: int | None = None) -> int:
        owner_id = self.intervention_id if intervention_id is None else intervention_id
        draft = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=owner_id,
            document_bytes=make_docx_text(text),
        )
        activated = activate_independent_template(
            self.connection,
            int(draft["id"]),
            patient_identification_review_confirmed=True,
        )
        return int(activated["id"])

    def test_selects_active_independent_templates_without_reparsing_and_never_falls_back_to_legacy(
        self,
    ) -> None:
        # Chiar dacă există un set legacy activ, generarea V3 nu face fallback la legacy.
        legacy_set_id = create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        import_template_pair(
            self.connection,
            legacy_set_id,
            {
                EPICRISIS_RECOMMENDATIONS: make_docx_text("Legacy epicriză"),
                OPERATIVE_PROTOCOL: make_docx_text("Legacy protocol"),
            },
        )
        self.assertTrue(
            activate_template_set(
                self.connection,
                legacy_set_id,
                patient_identification_review_confirmed=True,
            ).can_activate
        )

        # Un draft neactivat sau lipsa protocolului blochează explicit fără fallback legacy.
        epi_id = self._activate_epicrisis("Epicriză independentă {{sex}}")
        import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=make_docx_text("Protocol doar în draft"),
        )
        with self.assertRaisesRegex(GenerationTemplateError, "Protocol operator"):
            get_generation_form(self.connection, self.intervention_id)
        with self.assertRaisesRegex(GenerationTemplateError, "Protocol operator"):
            generate_documents(self.connection, self.intervention_id, {})

        # Invers: fără epicriză activă blochează explicit menționând Epicriză + Recomandări.
        proto_id = self._activate_protocol("Protocol activ {{lateralitate}}")
        retire_independent_template(self.connection, epi_id)
        with self.assertRaisesRegex(GenerationTemplateError, "Epicriză \\+ Recomandări"):
            get_generation_form(self.connection, self.intervention_id)

        epi_id_v2 = self._activate_epicrisis("Epicriză activă v2 {{sex}}")
        with patch(
            "ortodocs.services.docx_import.parse_docx_bytes",
            side_effect=AssertionError("DOCX nu trebuie reparsat la generare"),
        ):
            form = get_generation_form(self.connection, self.intervention_id)
            session = generate_documents(
                self.connection,
                self.intervention_id,
                {"sex": "Pacientă", "lateralitate": "dreapta"},
            )

        self.assertIsNone(form.template_set_id)
        self.assertEqual(form.epicrisis_template_version_id, epi_id_v2)
        self.assertEqual(form.operative_template_version_id, proto_id)
        self.assertIn("Epicriză activă v2 Pacientă", rich_text(session.epicrisis_recommendations))
        self.assertIn("Protocol activ dreapta", rich_text(session.operative_protocol))

    def test_builtin_and_unified_dynamic_variables_and_empty_substitution(self) -> None:
        epi_id = self._activate_epicrisis(
            "{{sex}} în vârstă de {{varsta}} ani, regim {{regim_prezentare}}, "
            "operat la {{data_operatiei}}, ATI: {{medic_anestezist}}. "
            "Indicație: {{indicatie_comuna}}. Comorbidități: {{comorbiditati}}."
        )
        proto_id = self._activate_protocol(
            "{{sex}} lateralitate {{lateralitate}}. "
            "Indicație: {{indicatie_comuna}}. Implant: {{implant_utilizat}}."
        )

        form = get_generation_form(self.connection, self.intervention_id)
        keys = [field.variable_key for field in form.fields]
        self.assertEqual(len(keys), len(set(keys)))
        for required_builtin in (
            "varsta",
            "sex",
            "lateralitate",
            "regim_prezentare",
            "data_operatiei",
            "medic_anestezist",
        ):
            self.assertIn(required_builtin, keys)
        for dynamic_key in ("indicatie_comuna", "comorbiditati", "implant_utilizat"):
            self.assertIn(dynamic_key, keys)

        fields_by_key = {field.variable_key: field for field in form.fields}
        self.assertEqual(
            [opt.option_value for opt in fields_by_key["sex"].options],
            ["Pacient", "Pacientă"],
        )
        self.assertEqual(
            [opt.option_value for opt in fields_by_key["regim_prezentare"].options],
            ["programat", "urgență"],
        )
        self.assertEqual(fields_by_key["data_operatiei"].data_type, "date")
        self.assertEqual(
            fields_by_key["indicatie_comuna"].used_in,
            (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
        )
        self.assertEqual(fields_by_key["varsta"].required_in, ())

        # Câmpurile omise sau goale se înlocuiesc cu "" fără valori fictive.
        session = generate_documents(
            self.connection,
            self.intervention_id,
            {
                "sex": "Pacient",
                "regim_prezentare": "programat",
                "indicatie_comuna": "Fixare internă",
                "medic_anestezist": "",
            },
        )
        epi_out = rich_text(session.epicrisis_recommendations)
        proto_out = rich_text(session.operative_protocol)
        self.assertIn("Pacient în vârstă de  ani, regim programat", epi_out)
        self.assertIn("operat la , ATI: .", epi_out)
        self.assertIn("Indicație: Fixare internă. Comorbidități: .", epi_out)
        self.assertIn("Pacient lateralitate .", proto_out)
        self.assertIn("Indicație: Fixare internă. Implant: .", proto_out)

        detail = get_generation_session(self.connection, session.session_id)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(detail.history_item.template_set_id)
        self.assertEqual(detail.epicrisis_template_version_id, epi_id)
        self.assertEqual(detail.operative_template_version_id, proto_id)

    def test_atomic_generation_rolls_back_completely_on_second_document_failure(self) -> None:
        self._activate_epicrisis("Epicriză {{sex}}")
        self._activate_protocol("Protocol {{lateralitate}}")
        self.connection.execute(
            """
            CREATE TRIGGER fail_second_v3_doc
            BEFORE INSERT ON generated_documents
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'simulated failure on second independent document');
            END
            """
        )

        with self.assertRaises(GenerationPersistenceError):
            generate_documents(
                self.connection,
                self.intervention_id,
                {"sex": "Pacient", "lateralitate": "stânga"},
            )

        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            0,
        )

    def test_more_than_ten_sessions_preserved_latest_ten_returned_and_edits_isolated_across_upgrades(
        self,
    ) -> None:
        epi_v1_id = self._activate_epicrisis("Epicriză v1 {{observatie}}")
        proto_v1_id = self._activate_protocol("Protocol v1 {{observatie}}")

        created_ids: list[int] = []
        for idx in range(1, 13):
            created = generate_documents(
                self.connection,
                self.intervention_id,
                {"observatie": f"caz-{idx:02d}"},
            )
            created_ids.append(created.session_id)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            12,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
            24,
        )
        history = list_generation_sessions(self.connection)
        self.assertEqual(len(history), 10)
        self.assertEqual(
            [item.session_id for item in history],
            list(reversed(created_ids[2:])),
        )
        self.assertTrue(all(item.template_set_id is None for item in history))

        # Editarea independentă a epicrizei din prima sesiune nu afectează protocolul sau șabloanele.
        target_session_id = created_ids[0]
        detail_before = get_generation_session(self.connection, target_session_id)
        assert detail_before is not None
        from ortodocs.models.rich_document import Paragraph

        edited_epicrisis = RichDocument([Paragraph([TextInline("Epicriză modificată independent")])])
        update_generated_document(
            self.connection,
            target_session_id,
            EPICRISIS_RECOMMENDATIONS,
            edited_epicrisis,
            "2026-11-15T18:00:00.000Z",
        )

        # Activăm o versiune v2 pentru ambele șabloane; documentele generate anterior rămân neschimbate.
        self._activate_epicrisis("Epicriză v2 {{observatie}}")
        self._activate_protocol("Protocol v2 {{observatie}}")

        reopened = get_generation_session(self.connection, target_session_id)
        assert reopened is not None
        self.assertIn("Epicriză modificată independent", rich_text(reopened.epicrisis_recommendations))
        self.assertIn("Protocol v1 caz-01", rich_text(reopened.operative_protocol))
        self.assertEqual(reopened.epicrisis_template_version_id, epi_v1_id)
        self.assertEqual(reopened.operative_template_version_id, proto_v1_id)
        self.assertEqual(
            reopened.operative_protocol.to_json(),
            detail_before.operative_protocol.to_json(),
        )

    def test_builtin_v3_fields_ignore_incompatible_db_definitions_and_preserve_canonical_specs(
        self,
    ) -> None:
        self._activate_epicrisis(
            "{{varsta}} | {{sex}} | {{regim_prezentare}} | {{data_operatiei}} | {{medic_anestezist}}"
        )
        self._activate_protocol("{{sex}} | {{lateralitate}}")

        create_variable(self.connection, "varsta", "Vârstă DB", "number", "common")
        bad_sex_id = create_variable(self.connection, "sex", "Sex DB", "select", "common")
        add_field_options(self.connection, bad_sex_id, [("M", "Masculin", 1), ("F", "Feminin", 2)])
        create_variable(
            self.connection,
            "lateralitate",
            "Lateralitate DB",
            "text",
            "intervention",
            intervention_id=self.intervention_id,
        )
        bad_regim_id = create_variable(
            self.connection,
            "regim_prezentare",
            "Regim DB",
            "select",
            "common",
        )
        add_field_options(self.connection, bad_regim_id, [("ambulator", "ambulator", 1)])
        create_variable(
            self.connection,
            "data_operatiei",
            "Data operației DB",
            "text",
            "diagnosis",
            diagnosis_id=self.diagnosis_id,
        )
        bad_med_id = create_variable(
            self.connection,
            "medic_anestezist",
            "Anestezist DB",
            "select",
            "common",
        )
        add_field_options(self.connection, bad_med_id, [("dr_x", "Dr. X", 1)])

        form = get_generation_form(self.connection, self.intervention_id)
        by_key = {field.variable_key: field for field in form.fields}

        self.assertEqual(by_key["varsta"].data_type, "text")
        self.assertEqual(by_key["varsta"].options, ())
        self.assertEqual(by_key["sex"].data_type, "select")
        self.assertEqual(
            [opt.option_value for opt in by_key["sex"].options],
            ["Pacient", "Pacientă"],
        )
        self.assertEqual(by_key["lateralitate"].data_type, "select")
        self.assertEqual(
            [opt.option_value for opt in by_key["lateralitate"].options],
            ["dreapta", "stânga", "bilateral"],
        )
        self.assertEqual(by_key["regim_prezentare"].data_type, "select")
        self.assertEqual(
            [opt.option_value for opt in by_key["regim_prezentare"].options],
            ["programat", "urgență"],
        )
        self.assertEqual(by_key["data_operatiei"].data_type, "date")
        self.assertEqual(by_key["data_operatiei"].options, ())
        self.assertEqual(by_key["medic_anestezist"].data_type, "text")
        self.assertEqual(by_key["medic_anestezist"].options, ())

        for key in (
            "varsta",
            "sex",
            "lateralitate",
            "regim_prezentare",
            "data_operatiei",
            "medic_anestezist",
        ):
            self.assertIsNone(by_key[key].field_definition_id)
            self.assertEqual(by_key[key].required_in, ())

        # Opțiunile și tipurile incompatibile din DB sunt respinse de validarea canonică V3.
        with self.assertRaises(GenerationValidationError):
            generate_documents(
                self.connection,
                self.intervention_id,
                {"sex": "M", "regim_prezentare": "ambulator", "data_operatiei": "text liber"},
            )

        session = generate_documents(
            self.connection,
            self.intervention_id,
            {
                "varsta": "65 ani",
                "sex": "Pacientă",
                "lateralitate": "bilateral",
                "regim_prezentare": "urgență",
                "data_operatiei": "15.11.2026",
                "medic_anestezist": "Dr. Popescu",
            },
        )
        epi_out = rich_text(session.epicrisis_recommendations)
        proto_out = rich_text(session.operative_protocol)
        self.assertIn("65 ani | Pacientă | urgență | 15.11.2026 | Dr. Popescu", epi_out)
        self.assertIn("Pacientă | bilateral", proto_out)

    def test_dynamic_select_and_scope_validation_in_v3(self) -> None:
        # 1. Select fără nicio opțiune activă -> GenerationTemplateError și blocare generare.
        self._activate_epicrisis("Epicriză {{tip_anestezie}}")
        self._activate_protocol("Protocol {{sex}}")
        create_variable(self.connection, "tip_anestezie", "Tip anestezie", "select", "common")

        with self.assertRaisesRegex(GenerationTemplateError, "tip_anestezie"):
            get_generation_form(self.connection, self.intervention_id)
        with self.assertRaisesRegex(GenerationTemplateError, "tip_anestezie"):
            generate_documents(self.connection, self.intervention_id, {})
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            0,
        )

        # 2a. Select cu opțiuni doar pe versiunea anterioară (is_current=0), dar fără opțiuni
        # pe versiunea curentă -> GenerationTemplateError și blocare generare.
        v1_id = create_variable(self.connection, "tip_tija", "Tip tijă", "select", "common")
        add_field_options(self.connection, v1_id, [("scurta", "Scurtă", 1)])
        create_variable_version(self.connection, "tip_tija", "Tip tijă v2", "select", "common")
        set_current_variable_version(self.connection, "tip_tija", 2)

        self._activate_epicrisis("Epicriză {{tip_tija}}")
        with self.assertRaisesRegex(GenerationTemplateError, "tip_tija"):
            get_generation_form(self.connection, self.intervention_id)
        with self.assertRaisesRegex(GenerationTemplateError, "tip_tija"):
            generate_documents(self.connection, self.intervention_id, {})

        # 2b. Select ale cărui opțiuni returnate sunt toate inactive (is_active=0) -> eroare.
        v3_id = create_variable_version(self.connection, "tip_tija", "Tip tijă v3", "select", "common")
        set_current_variable_version(self.connection, "tip_tija", 3)
        with patch(
            "ortodocs.services.document_generation.repositories.list_field_options",
            return_value=[
                {
                    "field_definition_id": v3_id,
                    "option_value": "inactiva",
                    "option_label": "Inactivă",
                    "sort_order": 1,
                    "is_active": 0,
                }
            ],
        ):
            with self.assertRaisesRegex(GenerationTemplateError, "tip_tija"):
                get_generation_form(self.connection, self.intervention_id)
            with self.assertRaisesRegex(GenerationTemplateError, "tip_tija"):
                generate_documents(self.connection, self.intervention_id, {})

        # 3. Definiție cu scope incompatibil (altă intervenție) -> GenerationTemplateError.
        other_diagnosis = add_diagnosis(self.connection, "Alt diagnostic")
        other_intervention = add_intervention(self.connection, other_diagnosis, "Altă intervenție")
        create_variable(
            self.connection,
            "marime_surub",
            "Mărime șurub",
            "text",
            "intervention",
            intervention_id=other_intervention,
        )
        self._activate_epicrisis("Epicriză {{marime_surub}}")
        with self.assertRaisesRegex(GenerationTemplateError, "marime_surub"):
            get_generation_form(self.connection, self.intervention_id)
        with self.assertRaisesRegex(GenerationTemplateError, "marime_surub"):
            generate_documents(self.connection, self.intervention_id, {})
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM generation_sessions").fetchone()[0],
            0,
        )

        # 4 & 5. Definiție common select validă + variabilă fără definiție -> funcționează.
        valid_select_id = create_variable(
            self.connection,
            "profilaxie_tvp",
            "Profilaxie TVP",
            "select",
            "common",
        )
        add_field_options(
            self.connection,
            valid_select_id,
            [("enoxaparina", "Enoxaparină", 1), ("fara", "Fără", 2)],
        )
        self._activate_epicrisis("Epicriză {{profilaxie_tvp}} | {{fara_definitie}}")
        form = get_generation_form(self.connection, self.intervention_id)
        fields_by_key = {field.variable_key: field for field in form.fields}

        self.assertEqual(fields_by_key["profilaxie_tvp"].data_type, "select")
        self.assertEqual(fields_by_key["profilaxie_tvp"].scope, "common")
        self.assertEqual(
            [opt.option_value for opt in fields_by_key["profilaxie_tvp"].options],
            ["enoxaparina", "fara"],
        )
        self.assertEqual(fields_by_key["fara_definitie"].data_type, "text")
        self.assertIsNone(fields_by_key["fara_definitie"].field_definition_id)
        self.assertEqual(fields_by_key["fara_definitie"].required_in, ())

        session = generate_documents(
            self.connection,
            self.intervention_id,
            {"profilaxie_tvp": "enoxaparina", "fara_definitie": "Observație liberă"},
        )
        self.assertIn(
            "Epicriză enoxaparina | Observație liberă",
            rich_text(session.epicrisis_recommendations),
        )

    def test_builtin_v3_fields_used_in_reflects_only_actual_template_presence(
        self,
    ) -> None:
        # varsta doar în epicriză, lateralitate doar în protocol, sex în ambele;
        # regim_prezentare, data_operatiei și medic_anestezist absente din ambele.
        self._activate_epicrisis("Epicriză {{sex}} vârsta {{varsta}}")
        self._activate_protocol("Protocol {{sex}} lat {{lateralitate}}")

        form = get_generation_form(self.connection, self.intervention_id)
        by_key = {field.variable_key: field for field in form.fields}

        self.assertEqual(by_key["varsta"].used_in, (EPICRISIS_RECOMMENDATIONS,))
        self.assertEqual(by_key["lateralitate"].used_in, (OPERATIVE_PROTOCOL,))
        self.assertEqual(
            by_key["sex"].used_in,
            (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
        )
        self.assertEqual(by_key["regim_prezentare"].used_in, ())
        self.assertEqual(by_key["data_operatiei"].used_in, ())
        self.assertEqual(by_key["medic_anestezist"].used_in, ())


if __name__ == "__main__":
    unittest.main()
