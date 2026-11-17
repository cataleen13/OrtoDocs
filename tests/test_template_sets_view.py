from __future__ import annotations

import sqlite3
import hashlib
import sys
import tempfile
import tkinter as tk
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.services import medical_library, template_sets
from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.rich_text_codec import read_rich_document
from ortodocs.ui.template_sets_view import (
    TemplateSetsView,
    _context_choice_label,
    _document_row_values,
    _format_import_report,
    _format_validation_issues,
    _rich_document_has_content,
    _template_set_row_values,
    _validation_summary,
)


def make_docx(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def make_warning_docx(text: str) -> bytes:
    document = Document()
    paragraph = document.add_paragraph(text)
    marker = OxmlElement("w:proofErr")
    marker.set(qn("w:type"), "spellStart")
    paragraph._p.insert(0, marker)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def make_formatted_docx() -> bytes:
    document = Document()
    paragraph = document.add_paragraph()
    bold = paragraph.add_run("Bold")
    bold.bold = True
    italic = paragraph.add_run(" italic")
    italic.italic = True
    paragraph.add_run().add_break(WD_BREAK.LINE)
    underlined = paragraph.add_run("Underlined")
    underlined.underline = True
    document.add_paragraph("Second paragraph")
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class TemplateSetsViewHelperTests(unittest.TestCase):
    def test_context_labels_mark_inactive_records(self) -> None:
        self.assertEqual(
            _context_choice_label(
                {"id": 7, "name": "Diagnostic", "is_active": 0}
            ),
            "Diagnostic (inactiv) · ID 7",
        )
        self.assertEqual(
            _context_choice_label(
                {"id": 8, "name": "Intervenție", "is_active": 1}
            ),
            "Intervenție · ID 8",
        )

    def test_validation_and_set_rows_show_persisted_summary(self) -> None:
        self.assertEqual(
            _validation_summary(
                {"status": "valid", "can_activate": True}
            ),
            "Valid · activare permisă",
        )
        self.assertEqual(
            _validation_summary(
                {"status": "blocked", "can_activate": False}
            ),
            "Blocat · activare blocată",
        )
        self.assertEqual(_validation_summary(None), "Nedisponibilă")
        self.assertEqual(
            _template_set_row_values(
                {
                    "version_no": 4,
                    "status": "draft",
                    "validation": {"status": "not_run", "can_activate": False},
                    "document_count": 1,
                    "created_at": "2026-01-01T12:00:00.000Z",
                    "activated_at": None,
                }
            ),
            (
                "4",
                "Draft",
                "Neverificat · activare blocată",
                "1",
                "2026-01-01T12:00:00.000Z",
                "",
            ),
        )

    def test_validation_issue_formatter_includes_all_context_and_problem_details(self) -> None:
        formatted = _format_validation_issues(
            [
                {
                    "code": "unmapped_variable",
                    "message": "Tokenul nu este mapat.",
                    "document_type": template_sets.EPICRISIS_RECOMMENDATIONS,
                    "variable_key": "data_internarii",
                },
                {
                    "code": "inactive_diagnosis",
                    "message": "Diagnosticul este inactiv.",
                },
                {
                    "code": "second_issue",
                    "message": "A doua problemă rămâne vizibilă.",
                    "document_type": template_sets.OPERATIVE_PROTOCOL,
                },
            ]
        )

        self.assertIn("Epicriză + Recomandări", formatted)
        self.assertIn("{{data_internarii}}", formatted)
        self.assertIn("[unmapped_variable]", formatted)
        self.assertIn("Tokenul nu este mapat.", formatted)
        self.assertIn("Pereche/context", formatted)
        self.assertIn("[inactive_diagnosis]", formatted)
        self.assertIn("Protocol operator", formatted)
        self.assertIn("[second_issue]", formatted)
        self.assertIn("A doua problemă rămâne vizibilă.", formatted)
        self.assertEqual(formatted.count("\n"), 2)

    def test_document_summary_distinguishes_missing_and_imported_documents(self) -> None:
        self.assertEqual(
            _document_row_values("operative_protocol", None),
            ("Protocol operator", "", "Lipsește", "—", "0", "—", "—"),
        )
        self.assertEqual(
            _document_row_values(
                "epicrisis_recommendations",
                {
                    "id": 12,
                    "validation_status": "pending",
                    "imported_at": "2026-01-01T12:00:00.000Z",
                    "validation_report": {
                        "import_report": {
                            "status": "valid_with_warnings",
                            "variables": {"data_internarii": 2},
                        },
                        "warning_confirmation": {"confirmed": True},
                    },
                },
            ),
            (
                "Epicriză + Recomandări",
                "12",
                "Valid cu avertismente",
                "În așteptare",
                "1",
                "Da",
                "2026-01-01T12:00:00.000Z",
            ),
        )

    def test_parser_report_preserves_issue_codes_messages_locations_and_confirmation(self) -> None:
        report = _format_import_report(
            {
                "validation_report": {
                    "import_report": {
                        "status": "valid_with_warnings",
                        "formatting_errors": [],
                        "warnings": [
                            {
                                "code": "proofing_annotation",
                                "message": "Marcaj de corectură ignorat.",
                                "part": "word/document.xml",
                                "paragraph_index": 2,
                                "element": "w:proofErr",
                            }
                        ],
                    },
                    "warning_confirmation": {"confirmed": True},
                }
            }
        )

        self.assertIn("Status parser: valid_with_warnings", report)
        self.assertIn("proofing_annotation", report)
        self.assertIn("Marcaj de corectură ignorat.", report)
        self.assertIn("part=word/document.xml", report)
        self.assertIn("paragraph_index=2", report)
        self.assertIn("element=w:proofErr", report)
        self.assertIn("Avertismente revizuite: Da", report)
        self.assertIn("nu aprobă automat setul pentru activare", report)

    def test_preview_content_check_accepts_text_and_line_break_only_documents(self) -> None:
        self.assertFalse(_rich_document_has_content(RichDocument()))
        self.assertFalse(_rich_document_has_content(RichDocument([Paragraph()])))
        self.assertTrue(
            _rich_document_has_content(
                RichDocument([Paragraph([TextInline("text")])])
            )
        )
        self.assertTrue(
            _rich_document_has_content(
                RichDocument([Paragraph([LineBreakInline()])])
            )
        )


class TemplateSetsViewWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu poate inițializa Tcl/Tk: {exc}")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "template-sets-view.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = medical_library.add_diagnosis(
            self.connection,
            "Diagnostic activ",
        )
        self.other_diagnosis_id = medical_library.add_diagnosis(
            self.connection,
            "Alt diagnostic",
        )
        self.intervention_id = medical_library.add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție activă",
        )
        self.other_intervention_id = medical_library.add_intervention(
            self.connection,
            self.other_diagnosis_id,
            "Intervenție secundară",
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.view = TemplateSetsView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)
        self.view.refresh()
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        finally:
            self.connection.close()
            self.temp_dir.cleanup()

    def _choose_diagnosis(self, diagnosis_id: int) -> None:
        self.view.diagnosis_combo.current(
            self.view._diagnosis_ids.index(diagnosis_id)
        )
        self.view._on_diagnosis_selected()

    def _choose_intervention(self, intervention_id: int) -> None:
        self.view.intervention_combo.current(
            self.view._intervention_ids.index(intervention_id)
        )
        self.view._on_intervention_selected()

    def _create_and_activate_set(self, intervention_id: int) -> int:
        diagnosis_id = (
            self.diagnosis_id
            if intervention_id == self.intervention_id
            else self.other_diagnosis_id
        )
        set_id = template_sets.create_template_set_draft(
            self.connection,
            diagnosis_id,
            intervention_id,
        )
        template_sets.import_template_pair(
            self.connection,
            set_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză"),
                template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
            },
        )
        result = template_sets.activate_template_set(
            self.connection,
            set_id,
            patient_identification_review_confirmed=True,
        )
        self.assertTrue(result.can_activate)
        return set_id

    def _select_set(self, template_set_id: int) -> None:
        self.view.template_set_tree.selection_set(str(template_set_id))
        self.view._on_template_set_selected()

    def _select_document(self, document_type: str) -> None:
        self.view.document_tree.selection_set(document_type)
        self.view._on_document_selected()

    def _create_draft_in_view(self) -> int:
        template_set_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)
        self.view._load_template_sets(preferred_template_set_id=template_set_id)
        return template_set_id

    def _create_field(
        self,
        variable_key: str,
        *,
        data_type: str = "text",
        scope: str = "common",
        diagnosis_id: int | None = None,
        intervention_id: int | None = None,
    ) -> int:
        return medical_library.create_variable(
            self.connection,
            variable_key,
            variable_key.replace("_", " ").title(),
            data_type,
            scope,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
        )

    def _create_draft_with_tokens(
        self,
        *,
        epicrisis: str,
        protocol: str = "Protocol fără tokenuri",
    ) -> int:
        template_set_id = self._create_draft_in_view()
        template_sets.import_template_pair(
            self.connection,
            template_set_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_docx(epicrisis),
                template_sets.OPERATIVE_PROTOCOL: make_docx(protocol),
            },
        )
        self.view._reload_selected_set(
            template_set_id,
            template_sets.EPICRISIS_RECOMMENDATIONS,
        )
        return template_set_id

    def _select_mapping_token(self, variable_key: str) -> None:
        self.view.mapping_tree.selection_set(variable_key)
        self.view._on_mapping_token_selected()

    def _choose_mapping_definition(self, field_definition_id: int) -> None:
        index = self.view._field_definition_ids.index(field_definition_id)
        self.view.field_definition_combo.current(index)
        self.view._on_mapping_definition_selected()

    def _save_mapping_as(self, required: bool) -> None:
        self.view.required_combo.set("Obligatoriu" if required else "Opțional")
        self.view._update_mapping_controls()
        self.view._save_selected_mapping()

    def _write_docx(self, name: str, content: bytes) -> str:
        path = Path(self.temp_dir.name) / name
        path.write_bytes(content)
        return str(path)

    def test_initialization_uses_shared_connection_and_does_not_select_context(self) -> None:
        self.assertIs(self.view.connection, self.connection)
        self.assertEqual(self.view.diagnosis_combo.current(), -1)
        self.assertEqual(self.view.diagnosis_combo.get(), "")
        self.assertEqual(self.view.intervention_combo.current(), -1)
        self.assertEqual(self.view.intervention_combo.get(), "")
        self.assertEqual(self.view.template_set_tree.get_children(), ())
        self.assertEqual(self.view.document_tree.get_children(), ())
        self.assertEqual(str(self.view.new_set_button.cget("state")), "disabled")

    def test_diagnosis_change_filters_interventions_without_auto_selecting_one(self) -> None:
        inactive_id = medical_library.add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție inactivă",
        )
        medical_library.set_intervention_active(self.connection, inactive_id, False)

        self._choose_diagnosis(self.diagnosis_id)

        self.assertEqual(
            set(self.view._intervention_ids),
            {self.intervention_id, inactive_id},
        )
        self.assertEqual(self.view.intervention_combo.current(), -1)
        self.assertEqual(self.view.intervention_combo.get(), "")
        self.assertTrue(
            any("(inactiv)" in label for label in self.view.intervention_combo.cget("values"))
        )
        self.assertEqual(self.view.template_set_tree.get_children(), ())

        self._choose_diagnosis(self.other_diagnosis_id)
        self.assertEqual(self.view._intervention_ids, [self.other_intervention_id])
        self.assertEqual(self.view.intervention_combo.current(), -1)

    def test_set_list_shows_all_statuses_and_selected_history_details(self) -> None:
        rejected_id = template_sets.create_template_set_draft(
            self.connection, self.diagnosis_id, self.intervention_id
        )
        template_sets.reject_template_set(
            self.connection,
            rejected_id,
            reason="Draft respins pentru test.",
        )
        retired_id = self._create_and_activate_set(self.intervention_id)
        active_id = self._create_and_activate_set(self.intervention_id)
        draft_id = template_sets.create_template_set_draft(
            self.connection, self.diagnosis_id, self.intervention_id
        )

        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)

        rows = [
            self.view.template_set_tree.item(item, "values")
            for item in self.view.template_set_tree.get_children()
        ]
        self.assertEqual([row[1] for row in rows], ["Draft", "Activ", "Retras", "Respins"])
        self.assertEqual([int(row[0]) for row in rows], [4, 3, 2, 1])
        self.assertEqual([row[3] for row in rows], ["0", "2", "2", "0"])

        for set_id, expected_status in (
            (draft_id, "draft"),
            (active_id, "active"),
            (retired_id, "retired"),
            (rejected_id, "rejected"),
        ):
            self._select_set(set_id)
            self.assertEqual(self.view.selected_template_set["status"], expected_status)
            self.assertEqual(len(self.view.document_tree.get_children()), 2)
            self.assertIn(f"ID set: {set_id}", self.view.detail_summary_var.get())
            self.assertIn("Diagnostic activ", self.view.detail_summary_var.get())
            self.assertIn("Intervenție activă", self.view.detail_summary_var.get())

    def test_inactive_context_history_remains_consultable(self) -> None:
        set_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            False,
        )
        medical_library.set_intervention_active(
            self.connection,
            self.intervention_id,
            False,
        )

        self.view.refresh()
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)

        self.assertEqual(self.view.template_set_tree.get_children(), (str(set_id),))
        self.assertEqual(str(self.view.new_set_button.cget("state")), "disabled")
        self._select_set(set_id)
        self.assertIn("Diagnostic activ (inactiv)", self.view.detail_summary_var.get())
        self.assertIn("Intervenție activă (inactiv)", self.view.detail_summary_var.get())

    def test_template_sets_are_filtered_by_selected_intervention(self) -> None:
        first_set = template_sets.create_template_set_draft(
            self.connection, self.diagnosis_id, self.intervention_id
        )
        second_set = template_sets.create_template_set_draft(
            self.connection, self.other_diagnosis_id, self.other_intervention_id
        )

        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)
        self.assertEqual(
            self.view.template_set_tree.get_children(),
            (str(first_set),),
        )

        self._choose_diagnosis(self.other_diagnosis_id)
        self._choose_intervention(self.other_intervention_id)
        self.assertEqual(
            self.view.template_set_tree.get_children(),
            (str(second_set),),
        )

    def test_new_draft_requires_confirmation_and_selects_created_version(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)
        self.assertEqual(str(self.view.new_set_button.cget("state")), "normal")

        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ) as confirm:
            self.view._create_new_set()

        confirm.assert_called_once()
        selected = self.view.selected_template_set
        self.assertIsNotNone(selected)
        self.assertEqual(selected["status"], "draft")
        self.assertEqual(self.view.selected_template_set_id, selected["id"])
        self.assertIn(f"ID {selected['id']}", self.view.status_var.get())
        self.assertIn(f"versiunea {selected['version_no']}", self.view.status_var.get())

    def test_inactive_diagnosis_or_intervention_disables_new_draft(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)

        medical_library.set_diagnosis_active(self.connection, self.diagnosis_id, False)
        self.view.refresh()
        self.assertEqual(str(self.view.new_set_button.cget("state")), "disabled")
        self.assertIn("(inactiv)", self.view.diagnosis_combo.get())

        medical_library.set_diagnosis_active(self.connection, self.diagnosis_id, True)
        medical_library.set_intervention_active(
            self.connection, self.intervention_id, False
        )
        self.view.refresh()
        self.assertEqual(str(self.view.new_set_button.cget("state")), "disabled")
        self.assertIn("(inactiv)", self.view.intervention_combo.get())

    def test_refresh_preserves_valid_selection_and_clears_missing_set_details(self) -> None:
        set_id = template_sets.create_template_set_draft(
            self.connection, self.diagnosis_id, self.intervention_id
        )
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)
        self._select_set(set_id)

        self.view.refresh()
        self.assertEqual(self.view.selected_diagnosis_id, self.diagnosis_id)
        self.assertEqual(self.view.selected_intervention_id, self.intervention_id)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertIsNotNone(self.view.selected_template_set)

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.list_template_sets",
            return_value=[],
        ):
            self.view.refresh()

        self.assertIsNone(self.view.selected_template_set_id)
        self.assertIsNone(self.view.selected_template_set)
        self.assertEqual(
            self.view.detail_summary_var.get(),
            "Selectați un set pentru a consulta detaliile.",
        )
        self.assertEqual(self.view.document_tree.get_children(), ())

    def test_details_error_clears_selected_details(self) -> None:
        set_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_set_details",
            side_effect=template_sets.TemplateSetNotFoundError("set șters"),
        ):
            self._select_set(set_id)

        self.assertIsNone(self.view.selected_template_set_id)
        self.assertIsNone(self.view.selected_template_set)
        self.assertEqual(self.view.document_tree.get_children(), ())
        self.assertIn("Detaliile setului nu au putut fi încărcate", self.view.status_var.get())

    def test_service_errors_are_shown_without_traceback(self) -> None:
        with patch(
            "ortodocs.ui.template_sets_view.medical_library.list_diagnoses",
            side_effect=medical_library.MedicalLibraryIntegrityError("eroare test"),
        ):
            self.assertFalse(self.view.refresh())
        self.assertIn("Diagnosticele nu au putut fi încărcate", self.view.status_var.get())
        self.assertEqual(self.view.diagnosis_combo.get(), "")

        self.view.refresh()
        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.create_template_set_draft",
            side_effect=template_sets.TemplateSetStateError("context changed"),
        ):
            self._choose_diagnosis(self.diagnosis_id)
            self._choose_intervention(self.intervention_id)
            self.view._create_new_set()
        self.assertIn("Draftul nu a putut fi creat", self.view.status_var.get())
        self.assertEqual(self.view.template_set_tree.get_children(), ())

    def test_individual_import_handles_each_document_type_and_reloads_details(self) -> None:
        template_set_id = self._create_draft_in_view()
        uploads = {
            template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză importată"),
            template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol importat"),
        }

        for document_type, document_bytes in uploads.items():
            path = self._write_docx(f"{document_type}.docx", document_bytes)
            self._select_document(document_type)
            with patch(
                "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
                return_value=path,
            ):
                self.view._import_selected_template()

            details = template_sets.get_template_set_details(
                self.connection,
                template_set_id,
            )
            imported = next(
                item
                for item in details["templates"]
                if item["document_type"] == document_type
            )
            self.assertEqual(
                imported["source_sha256"],
                hashlib.sha256(document_bytes).hexdigest(),
            )
            self.assertEqual(self.view.selected_template_set_id, template_set_id)
            self.assertEqual(self.view._selected_document_type, document_type)
            self.assertIn("Status parser: valid", self.view.parser_report_text.get("1.0", "end"))

        self.assertEqual(len(details["templates"]), 2)

    def test_pair_import_uses_exact_mapping_and_persists_both_documents(self) -> None:
        template_set_id = self._create_draft_in_view()
        documents = {
            template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză în pereche"),
            template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol în pereche"),
        }
        paths = {
            kind: self._write_docx(f"pair-{kind}.docx", value)
            for kind, value in documents.items()
        }

        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            side_effect=[paths[kind] for kind in documents],
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.import_template_pair",
            wraps=template_sets.import_template_pair,
        ) as import_pair:
            self.view._import_template_pair()

        import_pair.assert_called_once_with(
            self.connection,
            template_set_id,
            documents,
        )
        details = template_sets.get_template_set_details(
            self.connection,
            template_set_id,
        )
        self.assertEqual(
            {item["document_type"] for item in details["templates"]},
            set(documents),
        )
        self.assertEqual(len(details["templates"]), 2)
        self.assertEqual(self.view.selected_template_set_id, template_set_id)

    def test_pair_import_rolls_back_both_documents_when_second_insert_fails(self) -> None:
        template_set_id = self._create_draft_in_view()
        documents = {
            template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză"),
            template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
        }
        paths = [
            self._write_docx(f"atomic-{index}.docx", content)
            for index, content in enumerate(documents.values())
        ]
        self.connection.execute(
            """
            CREATE TEMP TRIGGER fail_protocol_insert
            BEFORE INSERT ON templates
            WHEN NEW.document_type = 'operative_protocol'
            BEGIN
                SELECT RAISE(ABORT, 'simulated second insert failure');
            END
            """
        )

        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            side_effect=paths,
        ):
            self.view._import_template_pair()

        self.assertIn("Perechea de documente nu a putut fi importată", self.view.status_var.get())
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.connection.in_transaction)

    def test_cancelled_file_dialogs_do_not_modify_the_draft(self) -> None:
        template_set_id = self._create_draft_in_view()
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value="",
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.import_template"
        ) as import_one:
            self.view._import_selected_template()
        import_one.assert_not_called()

        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            side_effect=[self._write_docx("first.docx", make_docx("first")), ""],
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.import_template_pair"
        ) as import_pair:
            self.view._import_template_pair()
        import_pair.assert_not_called()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )

    def test_replacement_requires_confirmation_and_keeps_original_on_cancel(self) -> None:
        template_set_id = self._create_draft_in_view()
        original = self._write_docx("original.docx", make_docx("Original"))
        replacement = self._write_docx("replacement.docx", make_docx("Înlocuit"))
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=original,
        ):
            self.view._import_selected_template()
        original_source = Path(original).read_bytes()
        original_template_id = int(self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )["id"])

        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=False,
        ), patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename"
        ) as choose_file:
            self.view._import_selected_template()
        choose_file.assert_not_called()
        self.assertEqual(
            int(self.view._template_for_document(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )["id"]),
            original_template_id,
        )
        self.assertEqual(
            template_sets.get_template_content(
                self.connection,
                original_template_id,
            )["source_docx"],
            original_source,
        )

        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ), patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value="",
        ):
            self.view._import_selected_template()
        self.assertEqual(
            int(self.view._template_for_document(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )["id"]),
            original_template_id,
        )
        self.assertEqual(
            template_sets.get_template_content(
                self.connection,
                original_template_id,
            )["source_docx"],
            original_source,
        )

        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ), patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=replacement,
        ):
            self.view._import_selected_template()

        replacement_id = int(self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )["id"])
        self.assertEqual(
            template_sets.get_template_content(
                self.connection,
                replacement_id,
            )["source_docx"],
            Path(replacement).read_bytes(),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            1,
        )

    def test_non_draft_sets_are_read_only_for_import_actions(self) -> None:
        active_id = self._create_draft_in_view()
        template_sets.import_template_pair(
            self.connection,
            active_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză"),
                template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
            },
        )
        template_sets.activate_template_set(
            self.connection,
            active_id,
            patient_identification_review_confirmed=True,
        )
        rejected_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        template_sets.reject_template_set(
            self.connection,
            rejected_id,
            reason="Test read-only.",
        )

        for template_set_id in (active_id, rejected_id):
            self.view._load_template_sets(
                preferred_template_set_id=template_set_id
            )
            self.assertEqual(str(self.view.import_document_button.cget("state")), "disabled")
            self.assertEqual(str(self.view.import_pair_button.cget("state")), "disabled")
            with patch(
                "ortodocs.ui.template_sets_view.template_sets.import_template"
            ) as import_one:
                self.view._import_selected_template()
            import_one.assert_not_called()

    def test_parser_reports_missing_rejected_and_warning_documents_separately(self) -> None:
        template_set_id = self._create_draft_in_view()
        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        self.assertIn("Documentul nu a fost importat", self.view.parser_report_text.get("1.0", "end"))

        warning_path = self._write_docx("warning.docx", make_warning_docx("Avertisment"))
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=warning_path,
        ):
            self.view._import_selected_template()
        self.assertIn("Status parser: valid_with_warnings", self.view.parser_report_text.get("1.0", "end"))
        self.assertIn("proofing_annotation", self.view.parser_report_text.get("1.0", "end"))
        self.assertIn("part=word/document.xml", self.view.parser_report_text.get("1.0", "end"))

        self._select_document(template_sets.OPERATIVE_PROTOCOL)
        rejected_path = self._write_docx("rejected.docx", b"not a DOCX archive")
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=rejected_path,
        ):
            self.view._import_selected_template()
        parser_text = self.view.parser_report_text.get("1.0", "end")
        self.assertIn("Status parser: rejected", parser_text)
        self.assertIn("docx_archive_error", parser_text)
        self.assertIn("Previzualizarea este indisponibilă", parser_text)
        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "draft",
        )

    def test_warning_confirmation_is_individual_and_reloads_details(self) -> None:
        template_set_id = self._create_draft_in_view()
        warning = make_warning_docx("Avertisment")
        paths = [
            self._write_docx("warning-epicrisis.docx", warning),
            self._write_docx("warning-protocol.docx", warning),
        ]
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            side_effect=paths,
        ):
            self.view._import_template_pair()

        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "normal")
        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ):
            self.view._confirm_selected_warnings()
        self.assertEqual(self.view.selected_template_set_id, template_set_id)
        epicrisis = self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )
        protocol = self.view._template_for_document(template_sets.OPERATIVE_PROTOCOL)
        self.assertTrue(epicrisis["validation_report"]["warning_confirmation"]["confirmed"])
        self.assertFalse(protocol["validation_report"]["warning_confirmation"]["confirmed"])
        self.assertEqual(self.view.document_tree.item(
            template_sets.EPICRISIS_RECOMMENDATIONS, "values"
        )[5], "Da")
        self.assertEqual(self.view.document_tree.item(
            template_sets.OPERATIVE_PROTOCOL, "values"
        )[5], "Nu")

        self._select_document(template_sets.OPERATIVE_PROTOCOL)
        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ):
            self.view._confirm_selected_warnings()
        details = template_sets.get_template_set_details(
            self.connection,
            template_set_id,
        )
        self.assertTrue(
            all(item["validation_report"]["warning_confirmation"]["confirmed"]
                for item in details["templates"])
        )

    def test_preview_is_read_only_and_preserves_rich_document_structure(self) -> None:
        self._create_draft_in_view()
        path = self._write_docx("formatted.docx", make_formatted_docx())
        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=path,
        ):
            self.view._import_selected_template()

        expected = template_sets.get_template_content(
            self.connection,
            int(self.view._template_for_document(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )["id"]),
        )["document"]
        window = self.view._preview_selected_document()

        self.assertIsNotNone(window)
        self.assertEqual(str(self.view._preview_text_widget.cget("state")), "disabled")
        self.assertEqual(
            read_rich_document(
                self.view._preview_text_widget,
                self.view._preview_render_state,
            ),
            expected,
        )
        self.assertEqual(len(expected.paragraphs), 2)
        self.assertTrue(
            any(isinstance(inline, LineBreakInline) for inline in expected.paragraphs[0].inlines)
        )
        self.assertTrue(any(
            isinstance(inline, TextInline) and inline.bold
            for inline in expected.paragraphs[0].inlines
        ))
        self.assertTrue(any(
            isinstance(inline, TextInline) and inline.italic
            for inline in expected.paragraphs[0].inlines
        ))
        self.assertTrue(any(
            isinstance(inline, TextInline) and inline.underline
            for inline in expected.paragraphs[0].inlines
        ))

    def test_rejected_or_empty_preview_is_explained_and_parser_report_remains(self) -> None:
        self._create_draft_in_view()
        rejected_path = self._write_docx("preview-rejected.docx", b"invalid zip")
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=rejected_path,
        ):
            self.view._import_selected_template()
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_content"
        ) as get_content:
            self.assertIsNone(self.view._preview_selected_document())
        get_content.assert_not_called()
        self.assertIn("Status parser: rejected", self.view.parser_report_text.get("1.0", "end"))
        self.assertIn("parserul a respins documentul", self.view.status_var.get())

        self._select_document(template_sets.OPERATIVE_PROTOCOL)
        valid_path = self._write_docx("preview-empty.docx", make_docx("Conținut"))
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=valid_path,
        ):
            self.view._import_selected_template()
        report_before = self.view.parser_report_text.get("1.0", "end")
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_content",
            return_value={"document": RichDocument()},
        ):
            self.assertIsNone(self.view._preview_selected_document())
        self.assertIn("conținutul parsat este gol", self.view.status_var.get())
        self.assertEqual(self.view.parser_report_text.get("1.0", "end"), report_before)

    def test_file_read_and_service_errors_are_shown_without_partial_import(self) -> None:
        template_set_id = self._create_draft_in_view()
        missing_path = str(Path(self.temp_dir.name) / "missing.docx")
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=missing_path,
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.import_template"
        ) as import_one:
            self.view._import_selected_template()
        import_one.assert_not_called()
        self.assertIn("Fișierul DOCX nu a putut fi citit", self.view.status_var.get())

        path = self._write_docx("service-error.docx", make_docx("Text"))
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=path,
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.import_template",
            side_effect=template_sets.TemplateSetStateError("set changed"),
        ):
            self.view._import_selected_template()
        self.assertIn("Documentul nu a putut fi importat", self.view.status_var.get())
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM templates WHERE template_set_id = ?",
                (template_set_id,),
            ).fetchone()[0],
            0,
        )

    def test_warning_confirmation_and_preview_service_errors_are_reported(self) -> None:
        self._create_draft_in_view()
        warning_path = self._write_docx("confirmation-error.docx", make_warning_docx("Avertisment"))
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=warning_path,
        ):
            self.view._import_selected_template()

        with patch(
            "ortodocs.ui.template_sets_view.messagebox.askyesno",
            return_value=True,
        ), patch(
            "ortodocs.ui.template_sets_view.template_sets.confirm_template_warnings",
            side_effect=template_sets.TemplateSetStateError("confirmation failed"),
        ):
            self.view._confirm_selected_warnings()
        self.assertIn("Avertismentele nu au putut fi confirmate", self.view.status_var.get())
        selected_template = self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )
        self.assertFalse(
            selected_template["validation_report"]["warning_confirmation"]["confirmed"]
        )

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_content",
            side_effect=template_sets.TemplateSetIntegrityError("bad content"),
        ):
            self.assertIsNone(self.view._preview_selected_document())
        self.assertIn("Conținutul documentului nu a putut fi încărcat", self.view.status_var.get())


    def test_mapping_list_shows_unique_tokens_occurrences_definition_and_requirement(self) -> None:
        template_set_id = self._create_draft_with_tokens(
            epicrisis="{{data_internarii}} și {{data_internarii}}",
            protocol="{{data_internarii}}",
        )
        field_id = self._create_field("data_internarii")
        template_id = self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )["id"]
        template_sets.add_variable_mapping(
            self.connection,
            int(template_id),
            "data_internarii",
            field_id,
            is_required=True,
        )
        self.view._reload_selected_set(
            template_set_id,
            template_sets.EPICRISIS_RECOMMENDATIONS,
        )

        self.assertEqual(self.view.mapping_tree.get_children(), ("data_internarii",))
        values = self.view.mapping_tree.item("data_internarii", "values")
        self.assertEqual(values[0], "2")
        self.assertEqual(values[1], "Mapat")
        self.assertIn(f"ID {field_id}", values[2])
        self.assertIn("v1", values[2])
        self.assertEqual(values[3], "Obligatoriu")
        self._select_mapping_token("data_internarii")
        self.assertIn(f"ID {field_id}", self.view.mapping_token_summary_var.get())
        self.assertIn(
            "Protocol operator: token prezent, nemapat",
            self.view.mapping_other_document_var.get(),
        )

    def test_mapping_add_remap_historical_version_and_remove_use_exact_definition_id(self) -> None:
        template_set_id = self._create_draft_with_tokens(
            epicrisis="{{data_internarii}}",
        )
        first_id = self._create_field("data_internarii")
        second_id = medical_library.create_variable_version(
            self.connection,
            "data_internarii",
            "Data internării versiunea 2",
            "date",
            "common",
        )
        medical_library.set_current_variable_version(
            self.connection,
            "data_internarii",
            2,
        )
        self._select_mapping_token("data_internarii")
        self.assertEqual(self.view._field_definition_ids, [first_id, second_id])
        self._choose_mapping_definition(first_id)
        self._save_mapping_as(required=True)

        template = self.view._template_for_document(
            template_sets.EPICRISIS_RECOMMENDATIONS
        )
        mapping = next(
            item for item in template["mappings"]
            if item["variable_key"] == "data_internarii"
        )
        self.assertEqual(mapping["field_definition_id"], first_id)
        self.assertEqual(mapping["version"], 1)
        self.assertEqual(mapping["is_required"], 1)
        self.assertIn("versiune istorică", self.view.mapping_field_summary_var.get())

        self._choose_mapping_definition(second_id)
        self._save_mapping_as(required=False)
        mapping = next(
            item
            for item in self.view._template_for_document(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )["mappings"]
            if item["variable_key"] == "data_internarii"
        )
        self.assertEqual(mapping["field_definition_id"], second_id)
        self.assertEqual(mapping["version"], 2)
        self.assertEqual(mapping["is_required"], 0)

        self.view._remove_selected_mapping()
        self.assertFalse(
            any(
                item["variable_key"] == "data_internarii" and item["is_mapped"]
                for item in self.view.document_variables
            )
        )
        self.assertEqual(self.view.selected_template_set_id, template_set_id)

    def test_mapping_filters_definitions_by_exact_key_and_template_context(self) -> None:
        self._create_draft_with_tokens(
            epicrisis="{{camp_comun}} {{camp_diagnostic}} "
            "{{camp_interventie}} {{camp_alt_diagnostic}}",
        )
        common_id = self._create_field("camp_comun")
        diagnosis_id = self._create_field(
            "camp_diagnostic",
            scope="diagnosis",
            diagnosis_id=self.diagnosis_id,
        )
        intervention_id = self._create_field(
            "camp_interventie",
            scope="intervention",
            intervention_id=self.intervention_id,
        )
        foreign_id = self._create_field(
            "camp_alt_diagnostic",
            scope="diagnosis",
            diagnosis_id=self.other_diagnosis_id,
        )

        expected = {
            "camp_comun": [common_id],
            "camp_diagnostic": [diagnosis_id],
            "camp_interventie": [intervention_id],
            "camp_alt_diagnostic": [],
        }
        for variable_key, expected_ids in expected.items():
            self._select_mapping_token(variable_key)
            self.assertEqual(self.view._field_definition_ids, expected_ids)
        self.assertNotIn(foreign_id, self.view._field_definition_ids)
        self.assertIn("contextul", self.view.mapping_guidance_var.get())

    def test_shared_token_shows_incompatible_mapping_warning_and_keeps_required_per_document(self) -> None:
        self._create_draft_with_tokens(
            epicrisis="{{data_internarii}}",
            protocol="{{data_internarii}}",
        )
        first_id = self._create_field("data_internarii")
        second_id = medical_library.create_variable_version(
            self.connection,
            "data_internarii",
            "Data internării versiunea 2",
            "date",
            "common",
        )
        medical_library.set_current_variable_version(
            self.connection,
            "data_internarii",
            2,
        )

        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(first_id)
        self._save_mapping_as(required=True)
        self._select_document(template_sets.OPERATIVE_PROTOCOL)
        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(second_id)
        self._save_mapping_as(required=False)

        warning = self.view.mapping_other_document_var.get()
        self.assertIn("AVERTISMENT", warning)
        self.assertIn("nu poate fi activată", warning)
        self.assertIn("obligatoriu în acel document", warning)
        rows = self.connection.execute(
            """
            SELECT t.document_type, tv.field_definition_id, tv.is_required
            FROM template_variables tv
            JOIN templates t ON t.id = tv.template_id
            WHERE tv.variable_key = ? ORDER BY t.document_type
            """,
            ("data_internarii",),
        ).fetchall()
        self.assertEqual(
            [(row["field_definition_id"], row["is_required"]) for row in rows],
            [(first_id, 1), (second_id, 0)],
        )

    def test_shared_token_allows_different_required_flags_for_same_exact_mapping(self) -> None:
        self._create_draft_with_tokens(
            epicrisis="{{data_internarii}}",
            protocol="{{data_internarii}}",
        )
        field_id = self._create_field("data_internarii")

        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(field_id)
        self._save_mapping_as(required=True)
        self._select_document(template_sets.OPERATIVE_PROTOCOL)
        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(field_id)
        self._save_mapping_as(required=False)

        self.assertNotIn("AVERTISMENT", self.view.mapping_other_document_var.get())
        self.assertIn("obligatoriu în acel document", self.view.mapping_other_document_var.get())
        rows = self.connection.execute(
            """
            SELECT t.document_type, tv.field_definition_id, tv.is_required
            FROM template_variables tv
            JOIN templates t ON t.id = tv.template_id
            WHERE tv.variable_key = ? ORDER BY t.document_type
            """,
            ("data_internarii",),
        ).fetchall()
        self.assertEqual(
            [(row["field_definition_id"], row["is_required"]) for row in rows],
            [(field_id, 1), (field_id, 0)],
        )

    def test_mapping_is_read_only_for_published_sets(self) -> None:
        field_id = self._create_field("data_internarii")
        def create_mapped_pair() -> int:
            set_id = template_sets.create_template_set_draft(
                self.connection,
                self.diagnosis_id,
                self.intervention_id,
            )
            template_ids = template_sets.import_template_pair(
                self.connection,
                set_id,
                {
                    template_sets.EPICRISIS_RECOMMENDATIONS: make_docx(
                        "{{data_internarii}}"
                    ),
                    template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
                },
            )
            template_sets.add_variable_mapping(
                self.connection,
                template_ids[template_sets.EPICRISIS_RECOMMENDATIONS],
                "data_internarii",
                field_id,
                is_required=True,
            )
            return set_id

        first_active_id = create_mapped_pair()
        self.assertTrue(
            template_sets.activate_template_set(
                self.connection,
                first_active_id,
                patient_identification_review_confirmed=True,
            ).can_activate
        )
        second_active_id = create_mapped_pair()
        self.assertTrue(
            template_sets.activate_template_set(
                self.connection,
                second_active_id,
                patient_identification_review_confirmed=True,
            ).can_activate
        )
        rejected_id = create_mapped_pair()
        template_sets.reject_template_set(
            self.connection,
            rejected_id,
            reason="Set respins pentru test.",
        )
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)
        self.view.refresh()
        for set_id in (first_active_id, second_active_id, rejected_id):
            self._select_set(set_id)
            self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
            self._select_mapping_token("data_internarii")

            self.assertEqual(
                str(self.view.field_definition_combo.cget("state")), "disabled"
            )
            self.assertEqual(
                str(self.view.required_combo.cget("state")), "disabled"
            )
            self.assertEqual(
                str(self.view.save_mapping_button.cget("state")), "disabled"
            )
            self.assertEqual(
                str(self.view.remove_mapping_button.cget("state")), "disabled"
            )

    def test_mapping_is_read_only_for_parser_rejected_document_in_draft(self) -> None:
        template_set_id = self._create_draft_in_view()
        field_id = self._create_field("camp_respins")

        document = Document()
        run = document.add_paragraph().add_run("{{camp_respins}}")
        highlight = OxmlElement("w:highlight")
        highlight.set(qn("w:val"), "yellow")
        run._r.get_or_add_rPr().append(highlight)
        stream = BytesIO()
        document.save(stream)
        rejected_path = self._write_docx("mapping-rejected.docx", stream.getvalue())

        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        with patch(
            "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
            return_value=rejected_path,
        ):
            self.view._import_selected_template()

        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (template_set_id,),
            ).fetchone()[0],
            "draft",
        )
        self.assertTrue(
            self.view.document_tree.exists(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )
        )
        document_values = self.view.document_tree.item(
            template_sets.EPICRISIS_RECOMMENDATIONS, "values"
        )
        self.assertTrue(document_values[1])
        self.assertEqual(document_values[2], "Respins")
        parser_report = self.view.parser_report_text.get("1.0", "end")
        self.assertIn("Status parser: rejected", parser_report)
        self.assertIn("highlight", parser_report)

        self._select_mapping_token("camp_respins")

        self.assertEqual(self.view.mapping_tree.get_children(), ("camp_respins",))
        self.assertEqual(self.view._field_definition_ids, [field_id])
        self.assertEqual(
            str(self.view.field_definition_combo.cget("state")), "disabled"
        )
        self.assertEqual(str(self.view.required_combo.cget("state")), "disabled")
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "disabled")
        self.assertEqual(
            self.view.parser_report_text.get("1.0", "end"), parser_report
        )

    def test_unmapped_token_explains_that_same_key_must_be_created_in_field_tab(self) -> None:
        self._create_draft_with_tokens(epicrisis="{{camp_nou}}")
        self._select_mapping_token("camp_nou")

        self.assertIn("Nu există un câmp cu această cheie", self.view.mapping_guidance_var.get())
        self.assertIn("aceeași cheie", self.view.mapping_guidance_var.get())
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "disabled")

    def test_select_mapping_shows_options_for_exact_historical_version(self) -> None:
        self._create_draft_with_tokens(
            epicrisis="{{tip_implant}} {{tip_fara_optiuni}}"
        )
        old_id = self._create_field("tip_implant", data_type="select")
        medical_library.add_field_option(
            self.connection,
            old_id,
            "old",
            "Opțiunea istorică",
            sort_order=1,
        )
        new_id = medical_library.create_variable_version(
            self.connection,
            "tip_implant",
            "Tip implant versiunea 2",
            "select",
            "common",
        )
        medical_library.add_field_option(
            self.connection,
            new_id,
            "new",
            "Opțiunea curentă",
            sort_order=1,
        )
        medical_library.set_current_variable_version(
            self.connection,
            "tip_implant",
            2,
        )
        no_options_id = self._create_field(
            "tip_fara_optiuni",
            data_type="select",
        )
        template_id = int(
            self.view._template_for_document(
                template_sets.EPICRISIS_RECOMMENDATIONS
            )["id"]
        )
        template_sets.add_variable_mapping(
            self.connection,
            template_id,
            "tip_implant",
            old_id,
            is_required=False,
        )
        self.view._reload_selected_set(
            int(self.view.selected_template_set_id),
            template_sets.EPICRISIS_RECOMMENDATIONS,
        )
        self._select_mapping_token("tip_implant")

        self.assertEqual(self.view.field_definition_combo.current(), 0)
        self.assertEqual(self.view._field_definition_ids[0], old_id)
        self.assertIn("Opțiunea istorică", self.view.mapping_options_var.get())
        self.assertNotIn("Opțiunea curentă", self.view.mapping_options_var.get())
        self._select_mapping_token("tip_fara_optiuni")
        self._choose_mapping_definition(no_options_id)
        self.assertIn(
            "niciuna configurată",
            self.view.mapping_options_var.get(),
        )

    def test_mapping_refresh_preserves_token_selection_and_reloads_new_field(self) -> None:
        self._create_draft_with_tokens(epicrisis="{{camp_creat_ulterior}}")
        self._select_mapping_token("camp_creat_ulterior")
        self.assertFalse(self.view.field_definitions)

        created_id = self._create_field("camp_creat_ulterior")
        self.assertTrue(self.view.refresh())

        self.assertEqual(self.view._selected_variable_key, "camp_creat_ulterior")
        self.assertEqual(self.view.mapping_tree.selection(), ("camp_creat_ulterior",))
        self.assertEqual(self.view._field_definition_ids, [created_id])
        self.assertIn("curentă", self.view.field_definition_combo.cget("values")[0])

    def test_mapping_service_errors_keep_selected_token_and_loaded_document_information(self) -> None:
        self._create_draft_with_tokens(epicrisis="{{data_internarii}}")
        field_id = self._create_field("data_internarii")
        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(field_id)
        self.view.required_combo.set("Obligatoriu")

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.add_variable_mapping",
            side_effect=template_sets.TemplateSetValidationError("mapare respinsă"),
        ):
            self.view._save_selected_mapping()

        self.assertEqual(self.view._selected_variable_key, "data_internarii")
        self.assertEqual(self.view.mapping_tree.selection(), ("data_internarii",))
        self.assertEqual(self.view.mapping_tree.get_children(), ("data_internarii",))
        self.assertIn("{{data_internarii}}", self.view.mapping_token_summary_var.get())
        self.assertIn("Maparea nu a putut fi salvată", self.view.status_var.get())
        self.assertFalse(
            any(
                item["variable_key"] == "data_internarii" and item["is_mapped"]
                for item in self.view.document_variables
            )
        )

    def test_validation_displays_all_document_token_and_pair_context_issues(self) -> None:
        set_id = self._create_draft_with_tokens(
            epicrisis="{{data_internarii}} {{ora_internarii}}"
        )
        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            False,
        )
        self.view.patient_review_var.set(True)

        self.view._validate_selected_set()

        report = self.view.validation_issues_text.get("1.0", "end")
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertIn("Epicriză + Recomandări", report)
        self.assertIn("{{data_internarii}}", report)
        self.assertIn("{{ora_internarii}}", report)
        self.assertIn("[unmapped_variable]", report)
        self.assertIn("Pereche/context", report)
        self.assertIn("[inactive_diagnosis]", report)
        self.assertIn("Status set: Draft", self.view.validation_set_status_var.get())
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (set_id,),
            ).fetchone()[0],
            "draft",
        )

    def test_blocked_validation_keeps_draft_and_shows_missing_documents(self) -> None:
        set_id = self._create_draft_in_view()
        self.view.patient_review_var.set(True)

        self.view._validate_selected_set()

        report = self.view.validation_issues_text.get("1.0", "end")
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertIn("[missing_document]", report)
        self.assertIn("Epicriză + Recomandări", report)
        self.assertIn("Protocol operator", report)
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

    def test_successful_validation_is_persisted_and_updates_document_statuses(self) -> None:
        set_id = self._create_draft_with_tokens(
            epicrisis="Epicriză", protocol="Protocol"
        )
        self.view.patient_review_var.set(True)

        self.view._validate_selected_set()

        details = template_sets.get_template_set_details(self.connection, set_id)
        validation = details["import_report"]["validation"]
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["can_activate"])
        self.assertTrue(
            details["import_report"]["patient_identification_review"]["confirmed"]
        )
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertEqual(
            [self.view.document_tree.item(item, "values")[3] for item in self.view.document_tree.get_children()],
            ["Valid", "Valid"],
        )
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_patient_review_starts_unchecked_and_is_passed_separately(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="Epicriză")
        self.assertFalse(self.view.patient_review_var.get())
        self.view.patient_review_var.set(True)

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.validate_template_set",
            wraps=template_sets.validate_template_set,
        ) as validate:
            self.view._validate_selected_set()

        validate.assert_called_once_with(
            self.connection,
            set_id,
            patient_identification_review_confirmed=True,
        )
        self.assertTrue(self.view.patient_review_var.get())

    def test_patient_review_does_not_confirm_parser_warnings(self) -> None:
        set_id = self._create_draft_in_view()
        template_sets.import_template_pair(
            self.connection,
            set_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_warning_docx(
                    "Epicriză cu avertisment"
                ),
                template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
            },
        )
        self.view._reload_selected_set(set_id)
        self.view.patient_review_var.set(True)

        self.view._validate_selected_set()

        details = template_sets.get_template_set_details(self.connection, set_id)
        warning_template = next(
            template
            for template in details["templates"]
            if template["document_type"] == template_sets.EPICRISIS_RECOMMENDATIONS
        )
        self.assertFalse(warning_template["validation_report"]["warning_confirmation"]["confirmed"])
        self.assertIn("[warnings_not_confirmed]", self.view.validation_issues_text.get("1.0", "end"))
        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "normal")

    def test_patient_review_resets_on_set_change_and_document_reimport(self) -> None:
        self._create_draft_with_tokens(epicrisis="Primul document")
        first_set_id = int(self.view.selected_template_set_id)
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")
        second_set_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        template_sets.import_template_pair(
            self.connection,
            second_set_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_docx("Epicriză veche"),
                template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
            },
        )
        self.view._load_template_sets(preferred_template_set_id=second_set_id)
        self.assertEqual(self.view.selected_template_set_id, second_set_id)
        self.assertFalse(self.view.patient_review_var.get())
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        self.view.patient_review_var.set(True)
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        replacement = self._write_docx("replacement.docx", make_docx("Epicriză nouă"))
        with (
            patch(
                "ortodocs.ui.template_sets_view.messagebox.askyesno",
                return_value=True,
            ),
            patch(
                "ortodocs.ui.template_sets_view.filedialog.askopenfilename",
                return_value=replacement,
            ),
        ):
            self.view._import_selected_template()

        self.assertEqual(self.view.selected_template_set_id, second_set_id)
        self.assertFalse(self.view.patient_review_var.get())
        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertNotEqual(first_set_id, second_set_id)

    def test_patient_review_survives_refresh_and_mapping_change_invalidates_activation(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="{{data_internarii}}")
        field_id = self._create_field("data_internarii")
        self._select_mapping_token("data_internarii")
        self._choose_mapping_definition(field_id)
        self._save_mapping_as(required=True)
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        self.assertTrue(self.view.refresh())
        self.assertTrue(self.view.patient_review_var.get())
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        self._select_mapping_token("data_internarii")
        self.view._remove_selected_mapping()
        self.assertTrue(self.view.patient_review_var.get())
        self.assertIn("Neverificat", self.view.validation_summary_var.get())
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        self.assertEqual(self.view.selected_template_set_id, set_id)

    def test_changing_patient_review_after_validation_disables_activation(self) -> None:
        self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        self.view.patient_review_var.set(False)

        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

    def test_returning_patient_review_to_validated_value_requires_revalidation(self) -> None:
        self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        self.view.patient_review_var.set(False)
        self.view.patient_review_var.set(True)

        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        self.view._validate_selected_set()

        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_activation_retires_previous_set_and_selects_new_active_set(self) -> None:
        previous_id = self._create_and_activate_set(self.intervention_id)
        new_id = self._create_draft_with_tokens(
            epicrisis="Epicriză", protocol="Protocol"
        )
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()

        self.view._activate_selected_set()

        rows = template_sets.list_template_sets(
            self.connection,
            intervention_id=self.intervention_id,
        )
        statuses = {int(row["id"]): row["status"] for row in rows}
        self.assertEqual(statuses[new_id], "active")
        self.assertEqual(statuses[previous_id], "retired")
        self.assertEqual(self.view.selected_template_set_id, new_id)
        self.assertEqual(self.view.selected_template_set["status"], "active")
        self.assertEqual(self.view.template_set_tree.selection(), (str(new_id),))
        self.assertIn("Setul ID", self.view.status_var.get())
        self.assertIn("Status set: Activ", self.view.validation_set_status_var.get())

    def test_activation_revalidation_block_keeps_draft_selected_and_shows_issues(self) -> None:
        set_id = self._create_draft_with_tokens(
            epicrisis="Epicriză", protocol="Protocol"
        )
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")
        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            False,
        )

        self.view._activate_selected_set()

        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertEqual(self.view.selected_template_set["status"], "draft")
        self.assertIn("[inactive_diagnosis]", self.view.validation_issues_text.get("1.0", "end"))
        self.assertTrue(self.view.patient_review_var.get())
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

    def test_deactivating_diagnosis_after_validation_disables_activation_until_refreshed(self) -> None:
        set_id = self._create_draft_with_tokens(
            epicrisis="Epicriză", protocol="Protocol"
        )
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            False,
        )

        self.assertTrue(self.view.refresh())

        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertFalse(self.view.selected_template_set["diagnosis_is_active"])
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            True,
        )
        self.assertTrue(self.view.refresh())

        self.assertTrue(self.view.selected_template_set["diagnosis_is_active"])
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_deactivating_intervention_after_validation_disables_activation_until_refreshed(self) -> None:
        self._create_draft_with_tokens(epicrisis="Epicriză", protocol="Protocol")
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        medical_library.set_intervention_active(
            self.connection,
            self.intervention_id,
            False,
        )

        self.assertTrue(self.view.refresh())

        self.assertFalse(self.view.selected_template_set["intervention_is_active"])
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        medical_library.set_intervention_active(
            self.connection,
            self.intervention_id,
            True,
        )
        self.assertTrue(self.view.refresh())

        self.assertTrue(self.view.selected_template_set["intervention_is_active"])
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_reactivated_context_does_not_enable_activation_from_invalid_validation(self) -> None:
        self._create_draft_with_tokens(epicrisis="Epicriză", protocol="Protocol")
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            False,
        )
        self.assertTrue(self.view.refresh())
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        self.assertIn(
            "inactive_diagnosis",
            self.view.validation_issues_text.get("1.0", "end"),
        )

        medical_library.set_diagnosis_active(
            self.connection,
            self.diagnosis_id,
            True,
        )
        self.assertTrue(self.view.refresh())

        self.assertTrue(self.view.selected_template_set["diagnosis_is_active"])
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        self.view._validate_selected_set()

        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_validation_and_activation_service_errors_preserve_loaded_details(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        details_before = self.view.selected_template_set
        summary_before = self.view.detail_summary_var.get()
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.validate_template_set",
            side_effect=template_sets.TemplateSetValidationError("validare eșuată"),
        ):
            self.view._validate_selected_set()
        self.assertIs(self.view.selected_template_set, details_before)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertEqual(self.view.detail_summary_var.get(), summary_before)
        self.assertTrue(self.view.patient_review_var.get())
        self.assertIn("Setul nu a putut fi validat", self.view.status_var.get())
        self.assertNotIn("Traceback", self.view.status_var.get())

        self.view._validate_selected_set()
        details_before = self.view.selected_template_set
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.activate_template_set",
            side_effect=template_sets.TemplateSetValidationError("activare eșuată"),
        ):
            self.view._activate_selected_set()
        self.assertIs(self.view.selected_template_set, details_before)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertTrue(self.view.patient_review_var.get())
        self.assertIn("Perechea nu a putut fi activată", self.view.status_var.get())
        self.assertNotIn("Traceback", self.view.status_var.get())

    def test_validation_list_reload_failure_preserves_result_until_refresh_succeeds(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        details_before = self.view.selected_template_set

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.list_template_sets",
            side_effect=template_sets.TemplateSetValidationError("lista indisponibilă"),
        ):
            self.view._validate_selected_set()

        report_after_validation = self.view.validation_issues_text.get("1.0", "end")
        self.assertIs(self.view.selected_template_set, details_before)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertIn("Nu au fost găsite probleme", report_after_validation)
        self.assertIn("Validarea setului s-a încheiat", self.view.status_var.get())
        self.assertIn("Reîncărcarea seturilor de șabloane a eșuat", self.view.status_var.get())
        self.assertIn("pot fi învechite", self.view.status_var.get())
        self.assertTrue(self.view._template_data_out_of_sync)
        self.assertEqual(str(self.view.validate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.validate_template_set"
        ) as validate:
            self.view._validate_selected_set()
            validate.assert_not_called()

        self.assertTrue(self.view.refresh())

        self.assertFalse(self.view._template_data_out_of_sync)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_validation_details_reload_failure_preserves_result_until_refresh_succeeds(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        details_before = self.view.selected_template_set

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_set_details",
            side_effect=template_sets.TemplateSetValidationError("detalii indisponibile"),
        ):
            self.view._validate_selected_set()

        self.assertIs(self.view.selected_template_set, details_before)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertIn("Reîncărcarea detaliilor setului a eșuat", self.view.status_var.get())
        self.assertEqual(str(self.view.validate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

        self.assertTrue(self.view.refresh())

        self.assertFalse(self.view._template_data_out_of_sync)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")

    def test_activation_reload_failure_preserves_result_until_refresh_succeeds(self) -> None:
        set_id = self._create_draft_with_tokens(epicrisis="Epicriză")
        self.view.patient_review_var.set(True)
        self.view._validate_selected_set()
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "normal")
        details_before = self.view.selected_template_set

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_set_details",
            side_effect=template_sets.TemplateSetValidationError("detalii indisponibile"),
        ):
            self.view._activate_selected_set()

        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM template_sets WHERE id = ?",
                (set_id,),
            ).fetchone()[0],
            "active",
        )
        self.assertIs(self.view.selected_template_set, details_before)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertEqual(self.view.selected_template_set["status"], "active")
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertIn("a fost activat", self.view.status_var.get())
        self.assertIn("Reîncărcarea detaliilor setului a eșuat", self.view.status_var.get())
        self.assertTrue(self.view._template_data_out_of_sync)
        self.assertEqual(str(self.view.validate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.import_document_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.import_pair_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "disabled")
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.activate_template_set"
        ) as activate:
            self.view._activate_selected_set()
            activate.assert_not_called()

        self.assertTrue(self.view.refresh())

        self.assertFalse(self.view._template_data_out_of_sync)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.selected_template_set["status"], "active")
        self.assertIn("Nu au fost găsite probleme", self.view.validation_issues_text.get("1.0", "end"))
        self.assertEqual(str(self.view.validate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.import_document_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.import_pair_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "disabled")

    def test_sync_failure_disables_all_mutations_and_guards_callbacks_until_refresh(self) -> None:
        set_id = self._create_draft_in_view()
        mapped_field_id = self._create_field("camp_mapat")
        unmapped_field_id = self._create_field("camp_nou")
        template_ids = template_sets.import_template_pair(
            self.connection,
            set_id,
            {
                template_sets.EPICRISIS_RECOMMENDATIONS: make_warning_docx(
                    "{{camp_mapat}} {{camp_nou}}"
                ),
                template_sets.OPERATIVE_PROTOCOL: make_docx("Protocol"),
            },
        )
        template_sets.add_variable_mapping(
            self.connection,
            template_ids[template_sets.EPICRISIS_RECOMMENDATIONS],
            "camp_mapat",
            mapped_field_id,
            is_required=True,
        )
        self.view._reload_selected_set(
            set_id,
            template_sets.EPICRISIS_RECOMMENDATIONS,
        )

        self._select_document(template_sets.EPICRISIS_RECOMMENDATIONS)
        self._select_mapping_token("camp_mapat")
        self._choose_mapping_definition(mapped_field_id)
        self.view.required_combo.set("Obligatoriu")
        self.view._update_mapping_controls()
        self.assertEqual(str(self.view.import_document_button.cget("state")), "normal")
        self.assertEqual(str(self.view.import_pair_button.cget("state")), "normal")
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "normal")
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "normal")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "normal")
        self.assertEqual(str(self.view.new_set_button.cget("state")), "normal")

        self._select_mapping_token("camp_nou")
        self._choose_mapping_definition(unmapped_field_id)
        self.view.required_combo.set("Obligatoriu")
        self.view._update_mapping_controls()
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "normal")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "disabled")
        self.view.patient_review_var.set(True)

        with patch(
            "ortodocs.ui.template_sets_view.template_sets.list_template_sets",
            side_effect=template_sets.TemplateSetValidationError("lista indisponibilă"),
        ):
            self.view._validate_selected_set()

        self.assertTrue(self.view._template_data_out_of_sync)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertIn("Reîncărcarea seturilor de șabloane a eșuat", self.view.status_var.get())
        self.assertIn("pot fi învechite", self.view.status_var.get())
        self.assertIn("[warnings_not_confirmed]", self.view.validation_issues_text.get("1.0", "end"))
        self.assertEqual(str(self.view.diagnosis_combo.cget("state")), "disabled")
        self.assertEqual(str(self.view.intervention_combo.cget("state")), "disabled")
        self.assertIn("disabled", self.view.template_set_tree.state())

        for widget in (
            self.view.new_set_button,
            self.view.import_document_button,
            self.view.import_pair_button,
            self.view.confirm_warnings_button,
            self.view.field_definition_combo,
            self.view.required_combo,
            self.view.save_mapping_button,
            self.view.remove_mapping_button,
            self.view.patient_review_check,
            self.view.validate_set_button,
            self.view.activate_set_button,
            self.view.preview_button,
        ):
            self.assertEqual(str(widget.cget("state")), "disabled")
        with patch(
            "ortodocs.ui.template_sets_view.template_sets.get_template_content"
        ) as get_content:
            self.assertIsNone(self.view._preview_selected_document())
            get_content.assert_not_called()

        self._select_mapping_token("camp_mapat")
        self._choose_mapping_definition(mapped_field_id)
        self.view.required_combo.set("Obligatoriu")
        self._select_mapping_token("camp_nou")
        self._choose_mapping_definition(unmapped_field_id)
        self.view.required_combo.set("Obligatoriu")
        status_before_callbacks = self.view.status_var.get()
        validation_report_before_callbacks = self.view.validation_issues_text.get(
            "1.0", "end"
        )
        parser_report_before_callbacks = self.view.parser_report_text.get("1.0", "end")
        selected_set_before_callbacks = self.view.template_set_tree.selection()
        persisted_before_callbacks = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM template_sets),
                (SELECT COUNT(*) FROM templates),
                (SELECT COUNT(*) FROM template_variables)
            """
        ).fetchone()

        with (
            patch("ortodocs.ui.template_sets_view.template_sets.create_template_set_draft") as create_draft,
            patch("ortodocs.ui.template_sets_view.template_sets.import_template") as import_one,
            patch("ortodocs.ui.template_sets_view.template_sets.import_template_pair") as import_pair,
            patch("ortodocs.ui.template_sets_view.template_sets.add_variable_mapping") as add_mapping,
            patch("ortodocs.ui.template_sets_view.template_sets.update_variable_mapping") as update_mapping,
            patch("ortodocs.ui.template_sets_view.template_sets.remove_variable_mapping") as remove_mapping,
            patch("ortodocs.ui.template_sets_view.template_sets.confirm_template_warnings") as confirm_warnings,
            patch("ortodocs.ui.template_sets_view.template_sets.validate_template_set") as validate_set,
            patch("ortodocs.ui.template_sets_view.template_sets.activate_template_set") as activate_set,
            patch("ortodocs.ui.template_sets_view.filedialog.askopenfilename") as choose_file,
            patch("ortodocs.ui.template_sets_view.messagebox.askyesno") as confirm_dialog,
        ):
            self.view._create_new_set()
            self.view._import_selected_template()
            self.view._import_template_pair()
            self.view._confirm_selected_warnings()
            self._select_mapping_token("camp_mapat")
            self._choose_mapping_definition(mapped_field_id)
            self.view.required_combo.set("Obligatoriu")
            self.view._save_selected_mapping()
            self.view._remove_selected_mapping()
            self._select_mapping_token("camp_nou")
            self._choose_mapping_definition(unmapped_field_id)
            self.view.required_combo.set("Obligatoriu")
            self.view._save_selected_mapping()
            self.view._validate_selected_set()
            self.view._activate_selected_set()

        for service_mock in (
            create_draft,
            import_one,
            import_pair,
            add_mapping,
            update_mapping,
            remove_mapping,
            confirm_warnings,
            validate_set,
            activate_set,
            choose_file,
            confirm_dialog,
        ):
            service_mock.assert_not_called()
        self.assertEqual(self.view.status_var.get(), status_before_callbacks)
        self.assertEqual(
            self.view.validation_issues_text.get("1.0", "end"),
            validation_report_before_callbacks,
        )
        self.assertEqual(
            self.view.parser_report_text.get("1.0", "end"),
            parser_report_before_callbacks,
        )
        self.assertEqual(self.view.template_set_tree.selection(), selected_set_before_callbacks)
        self.assertEqual(
            tuple(persisted_before_callbacks),
            tuple(
                self.connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM template_sets),
                        (SELECT COUNT(*) FROM templates),
                        (SELECT COUNT(*) FROM template_variables)
                    """
                ).fetchone()
            ),
        )

        self.assertTrue(self.view.refresh())

        self.assertFalse(self.view._template_data_out_of_sync)
        self.assertEqual(self.view.selected_template_set_id, set_id)
        self.assertEqual(self.view.template_set_tree.selection(), (str(set_id),))
        self.assertEqual(str(self.view.new_set_button.cget("state")), "normal")
        self.assertEqual(str(self.view.import_document_button.cget("state")), "normal")
        self.assertEqual(str(self.view.import_pair_button.cget("state")), "normal")
        self.assertEqual(str(self.view.confirm_warnings_button.cget("state")), "normal")
        self.assertEqual(str(self.view.field_definition_combo.cget("state")), "readonly")
        self.assertEqual(str(self.view.required_combo.cget("state")), "readonly")
        self.assertEqual(self.view.field_definition_combo.current(), -1)
        self._choose_mapping_definition(unmapped_field_id)
        self.view.required_combo.set("Obligatoriu")
        self.view._update_mapping_controls()
        self.assertEqual(str(self.view.save_mapping_button.cget("state")), "normal")
        self.assertEqual(str(self.view.remove_mapping_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.validate_set_button.cget("state")), "normal")
        self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")

    def test_non_draft_sets_disable_validation_activation_and_patient_review(self) -> None:
        retired_id = self._create_and_activate_set(self.intervention_id)
        active_id = self._create_and_activate_set(self.intervention_id)
        rejected_id = template_sets.create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        template_sets.reject_template_set(
            self.connection,
            rejected_id,
            reason="Respins pentru test.",
        )
        self._choose_diagnosis(self.diagnosis_id)
        self._choose_intervention(self.intervention_id)

        for set_id in (active_id, retired_id, rejected_id):
            self._select_set(set_id)
            self.assertEqual(str(self.view.validate_set_button.cget("state")), "disabled")
            self.assertEqual(str(self.view.activate_set_button.cget("state")), "disabled")
            self.assertEqual(str(self.view.patient_review_check.cget("state")), "disabled")


class TemplateSetsViewV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu are un runtime Tcl/Tk disponibil: {exc}")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "template-sets-view-v3.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)

        self.diagnosis_id = medical_library.add_diagnosis(self.connection, "Hernie de disc")
        self.intervention_id = medical_library.add_intervention(
            self.connection,
            self.diagnosis_id,
            "Discectomie lombară",
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.view = TemplateSetsView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)
        self.view.refresh()
        self.root.update_idletasks()

    def _write_docx(self, filename: str, content: bytes) -> str:
        target = Path(self.temp_dir.name) / filename
        target.write_bytes(content)
        return str(target)

    def test_independent_templates_tab_lifecycle(self) -> None:
        self.assertEqual(self.view.details_notebook.index("end"), 4)

        # Selectăm diagnosticul -> butonul Importă Epicriză devine activ.
        self.view.diagnosis_combo.current(0)
        self.view.diagnosis_combo.event_generate("<<ComboboxSelected>>")
        self.assertEqual(str(self.view.import_independent_epicrisis_button.cget("state")), "normal")
        self.assertEqual(str(self.view.import_independent_protocol_button.cget("state")), "disabled")

        epi_path = self._write_docx(
            "epi_warn.docx",
            make_warning_docx("Epicriză {{sex}} {{varsta}}"),
        )
        created_epi = self.view._import_independent_docx("diagnosis", path=epi_path)
        assert created_epi is not None
        self.assertEqual(len(self.view.independent_tree.get_children()), 1)
        self.assertEqual(str(self.view.confirm_independent_warnings_button.cget("state")), "normal")
        self.assertEqual(str(self.view.activate_independent_button.cget("state")), "disabled")

        self.view.confirm_independent_warnings_button.invoke()
        self.view.independent_patient_review_check.invoke()
        self.view.validate_independent_button.invoke()
        self.assertEqual(str(self.view.activate_independent_button.cget("state")), "normal")

        self.view.activate_independent_button.invoke()
        epi_row = template_sets.get_independent_template_details(
            self.connection,
            int(created_epi["id"]),
        )
        self.assertEqual(epi_row["status"], "active")

        # Selectăm intervenția -> gestionăm protocolul operator v1 și v2.
        self.view.intervention_combo.current(0)
        self.view.intervention_combo.event_generate("<<ComboboxSelected>>")
        self.assertEqual(str(self.view.import_independent_protocol_button.cget("state")), "normal")

        proto_v1_path = self._write_docx(
            "proto_v1.docx",
            make_docx("Protocol v1 {{lateralitate}}"),
        )
        proto_v1 = self.view._import_independent_docx("intervention", path=proto_v1_path)
        assert proto_v1 is not None
        self.view.preview_independent_button.invoke()
        self.assertIsNotNone(self.view._preview_window)
        if self.view._preview_window is not None and self.view._preview_window.winfo_exists():
            self.view._preview_window.destroy()

        self.view.independent_patient_review_check.invoke()
        self.view.validate_independent_button.invoke()
        self.view.activate_independent_button.invoke()
        self.assertEqual(
            template_sets.get_independent_template_details(
                self.connection,
                int(proto_v1["id"]),
            )["status"],
            "active",
        )

        # Importăm și activăm Protocol v2 -> v1 devine retras.
        proto_v2_path = self._write_docx(
            "proto_v2.docx",
            make_docx("Protocol v2 {{lateralitate}}"),
        )
        proto_v2 = self.view._import_independent_docx("intervention", path=proto_v2_path)
        assert proto_v2 is not None
        self.view.independent_patient_review_check.invoke()
        self.view.validate_independent_button.invoke()
        self.view.activate_independent_button.invoke()

        self.assertEqual(
            template_sets.get_independent_template_details(
                self.connection,
                int(proto_v1["id"]),
            )["status"],
            "retired",
        )
        self.assertEqual(
            template_sets.get_independent_template_details(
                self.connection,
                int(proto_v2["id"]),
            )["status"],
            "active",
        )

        # Retragem activul v2 și respingem un nou draft.
        self.view.retire_independent_button.invoke()
        self.assertEqual(
            template_sets.get_independent_template_details(
                self.connection,
                int(proto_v2["id"]),
            )["status"],
            "retired",
        )
        proto_v3_path = self._write_docx(
            "proto_v3.docx",
            make_docx("Protocol draft respins"),
        )
        proto_v3 = self.view._import_independent_docx("intervention", path=proto_v3_path)
        assert proto_v3 is not None
        self.view.reject_independent_button.invoke()
        self.assertEqual(
            template_sets.get_independent_template_details(
                self.connection,
                int(proto_v3["id"]),
            )["status"],
            "rejected",
        )


if __name__ == "__main__":
    unittest.main()
