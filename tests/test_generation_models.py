"""Teste pentru modelele de date ale generării OrtoDocs."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
import sys
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import (
    FieldValidationIssue,
    GeneratedSession,
    GenerationField,
    GenerationFieldOption,
    GenerationForm,
    GenerationHistoryItem,
    GenerationSessionDetail,
)
from ortodocs.models.rich_document import Paragraph, RichDocument, TextInline


EPICRISIS = "epicrisis_recommendations"
PROTOCOL = "operative_protocol"


def make_document(text: str) -> RichDocument:
    return RichDocument(paragraphs=[Paragraph([TextInline(text)])])


def make_field(
    *,
    variable_key: str = "lateralitate",
    field_definition_id: int = 42,
    data_type: str = "text",
    options: tuple[GenerationFieldOption, ...] = (),
    required_in: tuple[str, ...] = (EPICRISIS,),
) -> GenerationField:
    return GenerationField(
        variable_key=variable_key,
        field_definition_id=field_definition_id,
        label="Lateralitate",
        data_type=data_type,  # type: ignore[arg-type]
        scope="intervention",
        version=3,
        options=options,
        required_in=required_in,  # type: ignore[arg-type]
    )


class GenerationModelTests(unittest.TestCase):
    def test_generation_field_and_form_preserve_exact_field_version(self) -> None:
        field = make_field(field_definition_id=927)
        form = GenerationForm(
            diagnosis_id=2,
            intervention_id=5,
            template_set_id=11,
            fields=(field,),
        )

        self.assertEqual(form.fields[0].field_definition_id, 927)
        self.assertEqual(form.fields[0].version, 3)
        self.assertEqual(form.fields[0].required_in, (EPICRISIS,))

    def test_required_field_can_apply_to_either_or_both_documents(self) -> None:
        epicrisis_only = make_field(required_in=(EPICRISIS,))
        protocol_only = make_field(required_in=(PROTOCOL,))
        both = make_field(required_in=(EPICRISIS, PROTOCOL))

        self.assertEqual(epicrisis_only.required_in, (EPICRISIS,))
        self.assertEqual(protocol_only.required_in, (PROTOCOL,))
        self.assertEqual(both.required_in, (EPICRISIS, PROTOCOL))

    def test_select_field_contains_immutable_options(self) -> None:
        field = make_field(
            data_type="select",
            options=(
                GenerationFieldOption("dreapta", "Dreapta", 1),
                GenerationFieldOption("stanga", "Stânga", 2),
            ),
        )

        self.assertEqual(
            tuple(option.option_value for option in field.options),
            ("dreapta", "stanga"),
        )
        self.assertEqual(field.options[1].option_label, "Stânga")
        with self.assertRaises(FrozenInstanceError):
            field.options[0].option_label = "altă valoare"  # type: ignore[misc]

    def test_field_validation_issue_identifies_documents_and_message(self) -> None:
        issue = FieldValidationIssue(
            variable_key="data_operatie",
            reason="required_missing",
            document_types=(EPICRISIS, PROTOCOL),
            message="Completați data operației; câmpul este necesar în ambele documente.",
        )

        self.assertEqual(issue.variable_key, "data_operatie")
        self.assertEqual(issue.document_types, (EPICRISIS, PROTOCOL))
        self.assertIn("ambele documente", issue.message)

    def test_generated_session_uses_existing_rich_document_models(self) -> None:
        epicrisis = make_document("Epicriză")
        protocol = make_document("Protocol")
        session = GeneratedSession(17, epicrisis, protocol)

        self.assertIs(session.epicrisis_recommendations, epicrisis)
        self.assertIs(session.operative_protocol, protocol)
        self.assertIsInstance(session.epicrisis_recommendations, RichDocument)

    def test_history_models_include_schema_snapshots_and_documents(self) -> None:
        history = GenerationHistoryItem(
            session_id=8,
            diagnosis_id=2,
            intervention_id=5,
            template_set_id=11,
            diagnosis_label_snapshot="Diagnostic snapshot",
            intervention_label_snapshot="Intervenție snapshot",
            created_at="2026-11-10T08:30:00Z",
        )
        detail = GenerationSessionDetail(
            history_item=history,
            epicrisis_recommendations=make_document("Epicriză editată"),
            operative_protocol=make_document("Protocol editat"),
            epicrisis_template_id=101,
            epicrisis_edited_at="2026-11-10T08:30:00.123Z",
            operative_template_id=102,
            operative_edited_at="2026-11-10T08:30:00.456Z",
        )

        self.assertEqual(detail.history_item.session_id, 8)
        self.assertEqual(detail.history_item.template_set_id, 11)
        self.assertEqual(detail.history_item.status, "complete")
        self.assertEqual(
            detail.epicrisis_recommendations.paragraphs[0].inlines[0].text,
            "Epicriză editată",
        )
        self.assertEqual(
            detail.operative_protocol.paragraphs[0].inlines[0].text,
            "Protocol editat",
        )
        self.assertEqual(detail.epicrisis_template_id, 101)
        self.assertEqual(detail.epicrisis_edited_at, "2026-11-10T08:30:00.123Z")
        self.assertEqual(detail.operative_template_id, 102)
        self.assertEqual(detail.operative_edited_at, "2026-11-10T08:30:00.456Z")

    def test_structurally_invalid_models_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_field(field_definition_id=0)
        with self.assertRaises(ValueError):
            make_field(data_type="unknown")
        with self.assertRaises(ValueError):
            make_field(data_type="text", options=(GenerationFieldOption("x", "X"),))
        with self.assertRaises(ValueError):
            make_field(data_type="select")
        with self.assertRaises(ValueError):
            make_field(
                data_type="select",
                options=(
                    GenerationFieldOption("x", "Prima"),
                    GenerationFieldOption("x", "Duplicat"),
                ),
            )
        with self.assertRaises(ValueError):
            make_field(required_in=(EPICRISIS, EPICRISIS))
        with self.assertRaises(ValueError):
            GenerationForm(1, 2, 3, fields=(make_field(), make_field()))
        with self.assertRaises(ValueError):
            GenerationHistoryItem(1, 2, 3, 4, "Diagnostic", "Intervenție", "", "complete")
        history = GenerationHistoryItem(1, 2, 3, 4, "Diagnostic", "Intervenție", "2026-11-10T08:30:00Z")
        with self.assertRaises(ValueError):
            GenerationSessionDetail(
                history,
                make_document("Epicriză"),
                make_document("Protocol"),
                0,
                "2026-11-10T08:30:00.000Z",
                2,
                "2026-11-10T08:30:00.000Z",
            )

    def test_non_select_option_input_and_unknown_document_types_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            GenerationField(
                variable_key="age",
                field_definition_id=1,
                label="Vârstă",
                data_type="text",
                scope="common",
                version=1,
                options=[GenerationFieldOption("x", "X")],  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            make_field(required_in=("unknown_document",))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            FieldValidationIssue("age", "invalid", (), "Mesaj")

    def test_documents_must_be_distinct_rich_document_instances(self) -> None:
        document = make_document("Document")
        with self.assertRaises(ValueError):
            GeneratedSession(1, document, document)
        with self.assertRaises(TypeError):
            GeneratedSession(1, document, "text")  # type: ignore[arg-type]

    def test_instances_do_not_share_mutable_field_or_document_state(self) -> None:
        first_form = GenerationForm(1, 2, 3, fields=(make_field(),))
        second_form = GenerationForm(1, 2, 3)
        self.assertEqual(second_form.fields, ())
        with self.assertRaises(FrozenInstanceError):
            first_form.fields[0].label = "Schimbat"  # type: ignore[misc]

        first_session = GeneratedSession(1, make_document("Primul"), make_document("Protocol 1"))
        second_session = GeneratedSession(2, make_document("Al doilea"), make_document("Protocol 2"))
        first_session.epicrisis_recommendations.paragraphs[0].inlines[0].text = "Editat"
        self.assertEqual(
            second_session.epicrisis_recommendations.paragraphs[0].inlines[0].text,
            "Al doilea",
        )


if __name__ == "__main__":
    unittest.main()
