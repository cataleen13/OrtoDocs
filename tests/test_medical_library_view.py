from __future__ import annotations

import sqlite3
import sys
import tempfile
import tkinter as tk
import unittest
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.services import template_sets
from ortodocs.services.medical_library import (
    FieldOptionError,
    add_field_option,
    add_diagnosis,
    add_intervention,
    create_variable,
    create_variable_version,
    list_field_options,
    list_variables,
    set_diagnosis_active,
    set_intervention_active,
)
from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.medical_library_view import (
    MedicalLibraryView,
    _context_choices,
    _field_context_text,
    _record_tree_values,
)


def _make_docx_bytes(text: str, *, warning: bool = False) -> bytes:
    document = Document()
    paragraph = document.add_paragraph(text)
    if warning:
        marker = OxmlElement("w:proofErr")
        marker.set(qn("w:type"), "spellStart")
        paragraph._p.insert(0, marker)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class MedicalLibraryViewHelperTests(unittest.TestCase):
    def test_record_values_show_optional_code_and_active_state(self) -> None:
        self.assertEqual(
            _record_tree_values(
                {"name": "Fractură", "code": "S72", "is_active": 1}
            ),
            ("Fractură", "S72", "Activ"),
        )
        self.assertEqual(
            _record_tree_values(
                {"name": "Intervenție", "code": None, "is_active": 0}
            ),
            ("Intervenție", "", "Inactiv"),
        )


    def test_field_context_labels_mark_inactive_records(self) -> None:
        diagnoses = [{"id": 4, "name": "Diagnosis", "is_active": 0}]
        interventions = [
            {"id": 8, "diagnosis_id": 4, "name": "Intervention", "is_active": 0}
        ]
        diagnosis_choices = _context_choices("diagnosis", diagnoses, interventions)
        intervention_choices = _context_choices("intervention", diagnoses, interventions)
        self.assertIn("(inactiv)", diagnosis_choices[0][1])
        self.assertIn("Diagnosis (inactiv)", intervention_choices[0][1])
        self.assertIn("Intervention (inactiv)", intervention_choices[0][1])
        self.assertEqual(
            _field_context_text(
                {"scope": "intervention", "intervention_id": 8},
                diagnoses,
                interventions,
            ),
            intervention_choices[0][1],
        )


class MedicalLibraryViewWidgetTests(unittest.TestCase):
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
        self.database_path = Path(self.temp_dir.name) / "medical-library-view.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.diagnosis_id = add_diagnosis(
            self.connection, "Fractură de test", code="S72"
        )
        self.second_diagnosis_id = add_diagnosis(self.connection, "Alt diagnostic")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție inițială",
            code="OP-1",
        )
        self.second_intervention_id = add_intervention(
            self.connection,
            self.second_diagnosis_id,
            "Intervenție pentru alt diagnostic",
        )

        self.root = tk.Tk()
        self.root.withdraw()
        self.view = MedicalLibraryView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        finally:
            self.connection.close()
            self.temp_dir.cleanup()

    @staticmethod
    def _record_ids(tree: tk.Widget) -> list[int]:
        return [int(item) for item in tree.get_children()]

    def _select_diagnosis(self, diagnosis_id: int) -> None:
        self.view.diagnosis_tree.selection_set(str(diagnosis_id))
        self.view.diagnosis_tree.event_generate("<<TreeviewSelect>>", when="now")
        self.root.update()

    def _select_intervention(self, intervention_id: int) -> None:
        self.view.intervention_tree.selection_set(str(intervention_id))
        self.view.intervention_tree.event_generate("<<TreeviewSelect>>", when="now")
        self.root.update()

    def test_lists_active_and_inactive_diagnoses_without_auto_selection(self) -> None:
        set_diagnosis_active(self.connection, self.second_diagnosis_id, False)
        self.view.refresh()

        self.assertEqual(
            self._record_ids(self.view.diagnosis_tree),
            # The service orders ties in sort_order by normalized name, not insertion ID.
            [self.second_diagnosis_id, self.diagnosis_id],
        )
        rows = {
            int(item): self.view.diagnosis_tree.item(item, "values")
            for item in self.view.diagnosis_tree.get_children()
        }
        self.assertEqual(rows[self.diagnosis_id], ("Fractură de test", "S72", "Activ"))
        self.assertEqual(rows[self.second_diagnosis_id], ("Alt diagnostic", "", "Inactiv"))
        self.assertEqual(self.view.diagnosis_tree.selection(), ())
        self.assertEqual(self.view.intervention_tree.get_children(), ())

    def test_selecting_diagnosis_lists_its_active_and_inactive_interventions(self) -> None:
        inactive_id = add_intervention(
            self.connection, self.diagnosis_id, "Intervenție inactivă"
        )
        set_intervention_active(self.connection, inactive_id, False)

        self._select_diagnosis(self.diagnosis_id)

        self.assertEqual(
            self._record_ids(self.view.intervention_tree),
            # "Intervenție inactivă" sorts before "Intervenție inițială" by name_key.
            [inactive_id, self.intervention_id],
        )
        self.assertNotIn(self.second_intervention_id, self._record_ids(self.view.intervention_tree))
        self.assertEqual(
            self.view.selected_diagnosis_var.get(),
            "Diagnostic selectat: Fractură de test",
        )

    def test_add_and_edit_diagnosis_refreshes_list_and_keeps_selection(self) -> None:
        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("Diagnostic nou", "D-3"),
        ):
            self.view._add_diagnosis()

        new_id = max(self._record_ids(self.view.diagnosis_tree))
        self.assertIn(new_id, self._record_ids(self.view.diagnosis_tree))
        self.assertEqual(self.view.diagnosis_tree.selection(), (str(new_id),))
        self.assertEqual(
            self.view.diagnosis_tree.item(str(new_id), "values"),
            ("Diagnostic nou", "D-3", "Activ"),
        )

        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("Diagnostic redenumit", None),
        ):
            self.view._edit_diagnosis()

        self.assertEqual(self.view.diagnosis_tree.selection(), (str(new_id),))
        self.assertEqual(
            self.view.diagnosis_tree.item(str(new_id), "values"),
            ("Diagnostic redenumit", "", "Activ"),
        )

    def test_add_and_edit_intervention_keeps_original_diagnosis(self) -> None:
        self._select_diagnosis(self.diagnosis_id)
        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("Intervenție nouă", "OP-2"),
        ):
            self.view._add_intervention()

        new_id = max(self._record_ids(self.view.intervention_tree))
        self.assertEqual(self.view.intervention_tree.selection(), (str(new_id),))
        self.assertEqual(
            self.view.intervention_tree.item(str(new_id), "values"),
            ("Intervenție nouă", "OP-2", "Activ"),
        )

        self._select_intervention(new_id)
        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("Intervenție redenumită", "OP-3"),
        ) as prompt:
            self.view._edit_intervention()

        self.assertEqual(prompt.call_args.kwargs["relationship"], "Fractură de test")
        row = self.connection.execute(
            "SELECT diagnosis_id, name, code FROM interventions WHERE id = ?",
            (new_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (self.diagnosis_id, "Intervenție redenumită", "OP-3"))
        self.assertEqual(self.view.intervention_tree.selection(), (str(new_id),))

    def test_diagnosis_deactivation_requires_confirmation_and_warns_about_generation(self) -> None:
        self._select_diagnosis(self.diagnosis_id)
        with patch(
            "ortodocs.ui.medical_library_view.messagebox.askyesno", return_value=False
        ) as confirm:
            self.view._toggle_diagnosis()

        confirm.assert_called_once()
        self.assertIn("va fi blocată", confirm.call_args.args[1])
        self.assertEqual(
            self.connection.execute(
                "SELECT is_active FROM diagnoses WHERE id = ?", (self.diagnosis_id,)
            ).fetchone()[0],
            1,
        )

        with patch(
            "ortodocs.ui.medical_library_view.messagebox.askyesno", return_value=True
        ):
            self.view._toggle_diagnosis()
        self.assertEqual(
            self.view.diagnosis_tree.item(str(self.diagnosis_id), "values")[2],
            "Inactiv",
        )
        self.assertEqual(self.view.diagnosis_tree.selection(), (str(self.diagnosis_id),))

    def test_intervention_activation_and_deactivation_refreshes_state(self) -> None:
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)
        with patch(
            "ortodocs.ui.medical_library_view.messagebox.askyesno", return_value=True
        ) as confirm:
            self.view._toggle_intervention()

        confirm.assert_called_once()
        self.assertEqual(
            self.view.intervention_tree.item(str(self.intervention_id), "values")[2],
            "Inactiv",
        )
        self.assertEqual(self.view.intervention_tree.selection(), (str(self.intervention_id),))

        self.view._toggle_intervention()
        self.assertEqual(
            self.view.intervention_tree.item(str(self.intervention_id), "values")[2],
            "Activ",
        )

    def test_duplicate_and_validation_errors_are_shown_without_changing_data(self) -> None:
        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("  Fractură de test  ", "DUP"),
        ):
            self.view._add_diagnosis()
        self.assertIn("Există deja", self.view.status_var.get())
        self.assertEqual(len(self.view.diagnoses), 2)

        self._select_diagnosis(self.diagnosis_id)
        with patch(
            "ortodocs.ui.medical_library_view._prompt_record",
            return_value=("   ", None),
        ):
            self.view._add_intervention()
        self.assertIn("nu poate fi goală", self.view.status_var.get())
        self.assertEqual(len(self.view.interventions), 1)

    def test_refresh_reloads_external_changes_and_preserves_selection_if_possible(self) -> None:
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)
        external_id = add_diagnosis(self.connection, "Adăugat extern")

        self.view.on_show()

        self.assertIn(external_id, self._record_ids(self.view.diagnosis_tree))
        self.assertEqual(self.view.diagnosis_tree.selection(), (str(self.diagnosis_id),))

    @staticmethod
    def _walk_widgets(root: tk.Misc) -> list[tk.Misc]:
        items: list[tk.Misc] = []
        stack = [root]
        while stack:
            current = stack.pop(0)
            children = current.winfo_children()
            items.extend(children)
            stack.extend(children)
        return items

    def _write_docx(self, filename: str, content: bytes) -> str:
        target = Path(self.temp_dir.name) / filename
        target.write_bytes(content)
        return str(target)

    def test_template_sets_tab_shares_connection_and_refreshes_with_library(self) -> None:
        self.assertFalse(hasattr(self.view, "notebook"))
        self.assertFalse(
            any(isinstance(widget, ttk.Notebook) for widget in self._walk_widgets(self.view))
        )
        self.assertIs(self.view.epicrisis_panel.connection, self.connection)
        self.assertIs(self.view.protocol_panel.connection, self.connection)

        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)
        with (
            patch.object(self.view.epicrisis_panel, "refresh") as epi_refresh,
            patch.object(self.view.protocol_panel, "refresh") as proto_refresh,
        ):
            self.view.on_show()

        epi_refresh.assert_called_once_with(preferred_version_id=None)
        proto_refresh.assert_called_once_with(preferred_version_id=None)
        self.assertEqual(self.view.intervention_tree.selection(), (str(self.intervention_id),))

    def test_old_three_tabs_are_absent_and_three_zones_are_visible_in_single_page(self) -> None:
        widgets = self._walk_widgets(self.view)
        self.assertFalse(any(isinstance(widget, ttk.Notebook) for widget in widgets))
        self.assertEqual(self.view.fields_tab.winfo_manager(), "")

        hidden_field_descendants = set(self._walk_widgets(self.view.fields_tab))
        hidden_field_descendants.add(self.view.fields_tab)
        active_widgets = [
            w
            for w in widgets
            if w not in hidden_field_descendants and w.winfo_manager() != ""
        ]

        visible_texts: list[str] = []
        for widget in active_widgets:
            try:
                text = widget.cget("text")
            except tk.TclError:
                continue
            if isinstance(text, str) and text:
                visible_texts.append(text)
        for forbidden_label in (
            "Diagnostice și intervenții",
            "Câmpuri medicale",
            "Șabloane DOCX",
            "Bibliotecă medicală",
        ):
            self.assertNotIn(forbidden_label, visible_texts)

        self.assertNotEqual(self.view.diagnoses_panel.winfo_manager(), "")
        self.assertIn("Diagnostice", visible_texts)
        self.assertNotEqual(self.view.interventions_panel.winfo_manager(), "")
        self.assertIn("Intervenții", visible_texts)
        self.assertNotEqual(self.view.documents_zone.winfo_manager(), "")
        self.assertNotEqual(self.view.epicrisis_panel.winfo_manager(), "")
        self.assertIn("Epicriză + Recomandări", visible_texts)
        self.assertNotEqual(self.view.protocol_panel.winfo_manager(), "")
        self.assertIn("Protocol operator", visible_texts)
        self.assertIn("Gestionat prin Microsoft Word", visible_texts)
        self.assertIn("Istoric versiuni", visible_texts)
        self.assertIn("Previzualizează", visible_texts)

    def test_selecting_diagnosis_and_intervention_updates_epicrisis_and_protocol_panels(self) -> None:
        for removed_attr in (
            "import_epicrisis_button",
            "import_protocol_button",
            "epicrisis_activate_button",
            "protocol_activate_button",
            "epicrisis_validate_button",
            "protocol_validate_button",
            "epicrisis_patient_review_check",
            "protocol_patient_review_check",
            "epicrisis_confirm_warnings_button",
            "protocol_confirm_warnings_button",
            "epicrisis_reject_button",
            "protocol_reject_button",
            "epicrisis_retire_button",
            "protocol_retire_button",
        ):
            self.assertFalse(hasattr(self.view, removed_attr))

        self.assertIsNone(self.view.epicrisis_panel.owner_id)
        self.assertIsNone(self.view.protocol_panel.owner_id)
        self.assertEqual(str(self.view.epicrisis_preview_button.cget("state")), "disabled")
        self.assertEqual(str(self.view.protocol_preview_button.cget("state")), "disabled")

        # Fără diagnostic selectat nu se poate selecta nicio intervenție.
        self.view.intervention_tree.event_generate("<<TreeviewSelect>>", when="now")
        self.root.update()
        self.assertEqual(self.view.intervention_tree.selection(), ())
        self.assertIsNone(self.view.protocol_panel.owner_id)

        self._select_diagnosis(self.diagnosis_id)
        self.assertEqual(self.view.epicrisis_panel.owner_id, self.diagnosis_id)
        self.assertIsNone(self.view.protocol_panel.owner_id)

        self._select_intervention(self.intervention_id)
        self.assertEqual(self.view.epicrisis_panel.owner_id, self.diagnosis_id)
        self.assertEqual(self.view.protocol_panel.owner_id, self.intervention_id)

        # Schimbarea diagnosticului actualizează intervențiile și resetează protocolul.
        self._select_diagnosis(self.second_diagnosis_id)
        self.assertEqual(self._record_ids(self.view.intervention_tree), [self.second_intervention_id])
        self.assertEqual(self.view.epicrisis_panel.owner_id, self.second_diagnosis_id)
        self.assertIsNone(self.view.protocol_panel.owner_id)
        self.assertEqual(str(self.view.protocol_preview_button.cget("state")), "disabled")

    def test_readonly_synced_docx_versions_display_and_preview_in_templates_page(self) -> None:
        from ortodocs.services.template_sync import TemplateSyncService

        templates_dir = Path(self.temp_dir.name) / "sync_folder"
        (templates_dir / "epicrize").mkdir(parents=True)
        (templates_dir / "protocoale").mkdir(parents=True)
        (templates_dir / "epicrize" / "epi.docx").write_bytes(
            _make_docx_bytes("Epicriză {{sex}} {{varsta}}")
        )
        (templates_dir / "protocoale" / "proto.docx").write_bytes(
            _make_docx_bytes("Protocol v1 {{lateralitate}}")
        )
        (templates_dir / "catalog.json").write_text(
            '{\n'
            '  "catalog_version": 1,\n'
            '  "diagnoses": [\n'
            '    {"key": "diag-1", "name": "Fractură de test", "code": "S72", "epicrisis_path": "epicrize/epi.docx"}\n'
            '  ],\n'
            '  "interventions": [\n'
            '    {"key": "int-1", "diagnosis_key": "diag-1", "name": "Intervenție inițială", "code": "OP-1", "protocol_path": "protocoale/proto.docx"}\n'
            '  ]\n'
            '}\n',
            encoding="utf-8",
        )

        sync_service = TemplateSyncService(self.connection, database_path=self.database_path)
        sync_service.set_configured_folder(templates_dir)
        first_outcome = sync_service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(first_outcome.status, "applied")

        (templates_dir / "protocoale" / "proto.docx").write_bytes(
            _make_docx_bytes("Protocol v2 {{lateralitate}} {{observatii_test}}")
        )
        second_outcome = sync_service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(second_outcome.status, "applied")

        self.view.refresh()
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)

        self.assertEqual(self.view.epicrisis_panel.validation_var.get(), "Document valid")
        self.assertIn("v1", self.view.epicrisis_panel.version_var.get())
        self.assertEqual(self.view.epicrisis_panel.source_var.get(), "epicrize/epi.docx")
        self.assertIn("sex", self.view.epicrisis_panel.variables_var.get())

        epi_history = self.view.epicrisis_panel.open_versions_history()
        self.assertIsNotNone(epi_history)
        assert epi_history is not None
        self.assertEqual(len(epi_history.tree.get_children()), 1)
        epi_id = epi_history.tree.get_children()[0]
        epi_values = epi_history.tree.item(epi_id, "values")
        self.assertEqual(epi_values[0], "v1")
        self.assertEqual(epi_values[1], "Activ")
        self.assertEqual(epi_values[2], "epicrize/epi.docx")
        self.assertIn("sex", epi_values[4])
        epi_history.destroy()

        self.assertEqual(self.view.protocol_panel.validation_var.get(), "Document valid")
        self.assertIn("v2", self.view.protocol_panel.version_var.get())
        self.assertIn("1 în istoric", self.view.protocol_panel.version_var.get())
        self.assertEqual(self.view.protocol_panel.source_var.get(), "protocoale/proto.docx")

        proto_history = self.view.protocol_panel.open_versions_history()
        self.assertIsNotNone(proto_history)
        assert proto_history is not None
        self.assertEqual(len(proto_history.tree.get_children()), 2)
        report_content = proto_history.report_text.get("1.0", "end")
        self.assertIn("Sursă DOCX: protocoale/proto.docx", report_content)
        self.assertIn("Data sincronizării:", report_content)
        proto_history.destroy()

        self.assertEqual(str(self.view.epicrisis_preview_button.cget("state")), "normal")
        self.view.epicrisis_preview_button.invoke()
        self.assertIsNotNone(self.view._preview_window)
        if self.view._preview_window is not None and self.view._preview_window.winfo_exists():
            self.view._preview_window.destroy()

        self.assertEqual(str(self.view.protocol_preview_button.cget("state")), "normal")
        self.view.protocol_preview_button.invoke()
        self.assertIsNotNone(self.view._preview_window)
        if self.view._preview_window is not None and self.view._preview_window.winfo_exists():
            self.view._preview_window.destroy()

    def test_adopted_manual_version_shows_synced_folder_source_and_updates_on_modify(self) -> None:
        from ortodocs.services.template_sync import TemplateSyncService

        epi_bytes = _make_docx_bytes("Epicriză manuală identică {{sex}}")
        proto_bytes = _make_docx_bytes("Protocol manual identic {{lateralitate}}")

        manual_epi = template_sets.import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=epi_bytes,
        )
        template_sets.activate_independent_template(
            self.connection,
            int(manual_epi["id"]),
            patient_identification_review_confirmed=True,
        )
        manual_proto = template_sets.import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=proto_bytes,
        )
        template_sets.activate_independent_template(
            self.connection,
            int(manual_proto["id"]),
            patient_identification_review_confirmed=True,
        )

        self.view.refresh()
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)
        self.assertIn("Istoric / import manual", self.view.epicrisis_panel.source_var.get())

        templates_dir = Path(self.temp_dir.name) / "adopted_folder"
        (templates_dir / "epicrize").mkdir(parents=True)
        (templates_dir / "protocoale").mkdir(parents=True)
        (templates_dir / "epicrize" / "epi_manual.docx").write_bytes(epi_bytes)
        (templates_dir / "protocoale" / "proto_manual.docx").write_bytes(proto_bytes)
        (templates_dir / "catalog.json").write_text(
            '{\n'
            '  "catalog_version": 1,\n'
            '  "diagnoses": [\n'
            '    {"key": "diag-1", "name": "Fractură de test", "code": "S72", "epicrisis_path": "epicrize/epi_manual.docx"}\n'
            '  ],\n'
            '  "interventions": [\n'
            '    {"key": "int-1", "diagnosis_key": "diag-1", "name": "Intervenție inițială", "code": "OP-1", "protocol_path": "protocoale/proto_manual.docx"}\n'
            '  ]\n'
            '}\n',
            encoding="utf-8",
        )

        sync_service = TemplateSyncService(self.connection, database_path=self.database_path)
        sync_service.set_configured_folder(templates_dir)
        adopt_outcome = sync_service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(adopt_outcome.status, "applied")
        self.assertEqual(adopt_outcome.imported_count, 0)
        self.assertEqual(adopt_outcome.linked_count, 2)

        self.view.refresh()
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)

        self.assertEqual(self.view.epicrisis_panel.source_var.get(), "epicrize/epi_manual.docx")
        self.assertEqual(self.view.protocol_panel.source_var.get(), "protocoale/proto_manual.docx")
        epi_history = self.view.epicrisis_panel.open_versions_history()
        assert epi_history is not None
        self.assertEqual(len(epi_history.tree.get_children()), 1)
        epi_after = epi_history.tree.item(str(manual_epi["id"]), "values")
        self.assertEqual(epi_after[2], "epicrize/epi_manual.docx")
        self.assertIn("Sursă DOCX: epicrize/epi_manual.docx", epi_history.report_text.get("1.0", "end"))
        epi_history.destroy()

        # Repetarea sincronizării nu creează duplicate.
        repeat_outcome = sync_service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(repeat_outcome.status, "no_changes")
        self.assertEqual(len(self.view.protocol_panel.rows), 1)

        # Modificarea ulterioară creează v2 activă, păstrând v1 în istoric.
        (templates_dir / "protocoale" / "proto_manual.docx").write_bytes(
            _make_docx_bytes("Protocol modificat după adoptare {{lateralitate}}")
        )
        modify_outcome = sync_service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(modify_outcome.status, "applied")
        self.assertEqual(modify_outcome.activated_count, 1)

        self.view.refresh()
        self._select_diagnosis(self.diagnosis_id)
        self._select_intervention(self.intervention_id)
        self.assertEqual(len(self.view.protocol_panel.rows), 2)
        self.assertIn("v2", self.view.protocol_panel.version_var.get())
        self.assertIn("1 în istoric", self.view.protocol_panel.version_var.get())
        self.assertEqual(self.view.protocol_panel.source_var.get(), "protocoale/proto_manual.docx")

        self.view.protocol_preview_button.invoke()
        self.assertIsNotNone(self.view._preview_window)
        if self.view._preview_window is not None and self.view._preview_window.winfo_exists():
            self.view._preview_window.destroy()

    def test_no_intervention_can_be_added_without_selected_diagnosis(self) -> None:
        self.assertEqual(self.view.add_intervention_button.instate(["disabled"]), 1)
        with patch("ortodocs.ui.medical_library_view._prompt_record") as prompt:
            self.view._add_intervention()
        prompt.assert_not_called()
        self.assertEqual(self.view.intervention_tree.get_children(), ())

    def test_current_fields_filter_by_scope_and_context_in_service_order(self) -> None:
        create_variable(self.connection, "common_field", "Common", "text", "common")
        create_variable(
            self.connection, "diagnosis_field", "Diagnosis field", "date", "diagnosis",
            diagnosis_id=self.diagnosis_id,
        )
        create_variable(
            self.connection, "diagnosis_field_2", "Second diagnosis field", "text", "diagnosis",
            diagnosis_id=self.second_diagnosis_id,
        )
        create_variable(
            self.connection, "intervention_field", "Intervention field", "select", "intervention",
            intervention_id=self.intervention_id,
        )
        create_variable(
            self.connection, "intervention_field_2", "Second intervention field", "text", "intervention",
            intervention_id=self.second_intervention_id,
        )
        set_diagnosis_active(self.connection, self.diagnosis_id, False)
        self.view.refresh()

        expected = [row["variable_key"] for row in list_variables(self.connection, current_only=True)]
        self.assertEqual(list(self.view.field_tree.get_children()), expected)
        self.assertEqual(self.view.field_tree.selection(), ())
        self.assertIn("(inactiv)", self.view.field_tree.item("diagnosis_field", "values")[4])

        self.view.scope_filter_var.set("diagnosis")
        self.view._on_scope_filter_changed()
        self.assertEqual(
            list(self.view.field_tree.get_children()),
            ["diagnosis_field", "diagnosis_field_2"],
        )
        diagnosis_index = self.view.field_context_ids.index(self.diagnosis_id) + 1
        self.view.field_context_filter_combo.current(diagnosis_index)
        self.view._on_field_context_filter_changed()
        self.assertEqual(list(self.view.field_tree.get_children()), ["diagnosis_field"])
        second_diagnosis_index = self.view.field_context_ids.index(self.second_diagnosis_id) + 1
        self.view.field_context_filter_combo.current(second_diagnosis_index)
        self.view._on_field_context_filter_changed()
        self.assertEqual(list(self.view.field_tree.get_children()), ["diagnosis_field_2"])

        self.view.scope_filter_var.set("intervention")
        self.view._on_scope_filter_changed()
        self.assertEqual(
            list(self.view.field_tree.get_children()),
            ["intervention_field", "intervention_field_2"],
        )
        intervention_index = self.view.field_context_ids.index(self.intervention_id) + 1
        self.view.field_context_filter_combo.current(intervention_index)
        self.view._on_field_context_filter_changed()
        self.assertEqual(list(self.view.field_tree.get_children()), ["intervention_field"])
        second_intervention_index = self.view.field_context_ids.index(self.second_intervention_id) + 1
        self.view.field_context_filter_combo.current(second_intervention_index)
        self.view._on_field_context_filter_changed()
        self.assertEqual(list(self.view.field_tree.get_children()), ["intervention_field_2"])

    def test_create_field_and_duplicate_error(self) -> None:
        values = {
            "variable_key": "new_field",
            "label": "New field",
            "data_type": "select",
            "scope": "common",
            "diagnosis_id": None,
            "intervention_id": None,
        }
        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_definition", return_value=values
        ):
            self.view._add_variable()
        created = list_variables(self.connection, variable_key="new_field")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["is_current"], 1)
        self.assertEqual(self.view.field_tree.selection(), ("new_field",))
        self.assertIn("select", self.view.status_var.get().lower())

        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_definition", return_value=values
        ):
            self.view._add_variable()
        self.assertIn("new_field", self.view.status_var.get())
        self.assertEqual(len(list_variables(self.connection, variable_key="new_field")), 1)

        invalid_values = dict(values, variable_key="INVALID KEY")
        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_definition",
            return_value=invalid_values,
        ):
            self.view._add_variable()
        self.assertIn("Cheia variabilei", self.view.status_var.get())
        self.assertEqual(list_variables(self.connection, current_only=True), [created[0]])

    def test_versions_can_be_created_selected_set_current_and_rolled_back(self) -> None:
        first_id = create_variable(self.connection, "versioned", "Version one", "text", "common")
        second_id = create_variable_version(
            self.connection, "versioned", "Version two", "number", "common"
        )
        self.view.refresh()
        self.view.field_tree.selection_set("versioned")
        self.view._on_field_selected()
        self.assertEqual(list(self.view.version_tree.get_children()), [str(first_id), str(second_id)])
        self.assertEqual(self.view.version_tree.item(str(first_id), "values")[1], "Da")
        self.assertEqual(self.view.version_tree.item(str(second_id), "values")[1], "Nu")
        self.assertIn(f"ID definiție: {first_id}", self.view.field_detail_var.get())
        self.assertIn("versiunea 1", self.view.field_detail_var.get())
        self.assertIn("tip: text", self.view.field_detail_var.get())
        self.assertIn("Domeniu: Comun", self.view.field_detail_var.get())

        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_definition",
            return_value={
                "variable_key": "versioned", "label": "Version three", "data_type": "date",
                "scope": "common", "diagnosis_id": None, "intervention_id": None,
            },
        ):
            self.view._create_variable_version()
        versions = list_variables(self.connection, variable_key="versioned")
        third = versions[-1]
        self.assertEqual((third["version"], third["is_current"]), (3, 0))
        self.assertEqual(self.view.version_tree.selection(), (str(third["id"]),))

        self.view.version_tree.selection_set(str(second_id))
        self.view._display_selected_version()
        with patch("ortodocs.ui.medical_library_view.messagebox.askyesno", return_value=True):
            self.view._set_current_version()
        current = next(row for row in list_variables(self.connection, variable_key="versioned") if row["is_current"])
        self.assertEqual(current["id"], second_id)

        self.view.version_tree.selection_set(str(first_id))
        self.view._display_selected_version()
        self.assertEqual(self.view.rollback_button.instate(["disabled"]), 0)
        with patch("ortodocs.ui.medical_library_view.messagebox.askyesno", return_value=True):
            self.view._rollback_version()
        current = next(row for row in list_variables(self.connection, variable_key="versioned") if row["is_current"])
        self.assertEqual(current["id"], first_id)

    def test_select_options_are_loaded_and_saved_for_exact_version(self) -> None:
        first_id = create_variable(self.connection, "choices", "Choice v1", "select", "common")
        add_field_option(self.connection, first_id, "old", "Old choice", sort_order=2)
        second_id = create_variable_version(
            self.connection, "choices", "Choice v2", "select", "common"
        )
        self.assertEqual(list_field_options(self.connection, second_id), [])
        add_field_option(self.connection, second_id, "new", "New choice", sort_order=1)
        self.view.refresh()
        self.view.field_tree.selection_set("choices")
        self.view._load_field_versions("choices", preferred_version_id=first_id)
        self.assertEqual(
            self.view.option_tree.item(self.view.option_tree.get_children()[0], "values"),
            ("old", "Old choice", "2"),
        )

        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_option",
            return_value=("old-2", "Another old choice"),
        ):
            self.view._add_field_option()
        self.assertEqual(
            [row["option_value"] for row in list_field_options(self.connection, first_id)],
            ["old-2", "old"],
        )
        self.assertEqual(
            [row["option_value"] for row in list_field_options(self.connection, second_id)],
            ["new"],
        )

        with patch(
            "ortodocs.ui.medical_library_view._prompt_field_option", return_value=("blocked", "Blocked")
        ), patch(
            "ortodocs.ui.medical_library_view.medical_library.add_field_option",
            side_effect=FieldOptionError("Published options are immutable."),
        ):
            self.view._add_field_option()
        self.assertIn("immutable", self.view.status_var.get())
        self.assertEqual(
            [row["option_value"] for row in list_field_options(self.connection, first_id)],
            ["old-2", "old"],
        )

    def test_refresh_keeps_valid_field_selection_and_clears_hidden_details(self) -> None:
        create_variable(self.connection, "keep_field", "Keep", "text", "common")
        self.view.refresh()
        self.view.field_tree.selection_set("keep_field")
        self.view._on_field_selected()
        self.view.refresh()
        self.assertEqual(self.view.field_tree.selection(), ("keep_field",))
        self.assertTrue(self.view.version_tree.get_children())

        self.view.scope_filter_var.set("diagnosis")
        self.view._on_scope_filter_changed()
        self.assertEqual(self.view.field_tree.selection(), ())
        self.assertEqual(self.view.version_tree.get_children(), ())
        self.assertEqual(self.view.option_tree.get_children(), ())
        self.assertEqual(self.view.new_version_button.instate(["disabled"]), 1)

    def test_version_and_option_actions_are_disabled_when_not_applicable(self) -> None:
        create_variable(self.connection, "plain_field", "Plain field", "text", "common")
        self.assertEqual(self.view.new_version_button.instate(["disabled"]), 1)
        self.assertEqual(self.view.set_current_button.instate(["disabled"]), 1)
        self.assertEqual(self.view.rollback_button.instate(["disabled"]), 1)
        self.assertEqual(self.view.add_option_button.instate(["disabled"]), 1)

        self.view.refresh()
        self.view.field_tree.selection_set("plain_field")
        self.view._on_field_selected()
        self.assertEqual(self.view.new_version_button.instate(["!disabled"]), 1)
        self.assertEqual(self.view.set_current_button.instate(["disabled"]), 1)
        self.assertEqual(self.view.rollback_button.instate(["disabled"]), 1)
        self.assertEqual(self.view.add_option_button.instate(["disabled"]), 1)

    def test_search_filters_and_contextual_toggle_button_labels(self) -> None:
        initial_db_rows = self.connection.execute(
            "SELECT id, name, code FROM diagnoses ORDER BY id"
        ).fetchall()
        initial_children = self.view.diagnosis_tree.get_children()
        self.assertEqual(len(initial_children), len(initial_db_rows))
        self.assertEqual(
            {int(item_id) for item_id in initial_children},
            {int(row[0]) for row in initial_db_rows},
        )
        self.assertEqual(
            {int(row[0]) for row in initial_db_rows},
            {self.diagnosis_id, self.second_diagnosis_id},
        )

        new_diag_id = add_diagnosis(self.connection, "Luxație umăr", code="S43")
        self.view.refresh()

        all_db_rows = self.connection.execute(
            "SELECT id, name, code FROM diagnoses ORDER BY id"
        ).fetchall()
        refreshed_children = self.view.diagnosis_tree.get_children()
        self.assertEqual(len(refreshed_children), len(initial_children) + 1)
        self.assertEqual(len(refreshed_children), len(all_db_rows))
        self.assertEqual(
            {int(item_id) for item_id in refreshed_children},
            {self.diagnosis_id, self.second_diagnosis_id, new_diag_id},
        )

        self.view.diagnosis_search_var.set("luxaț")
        self.root.update_idletasks()
        self.assertEqual(self.view.diagnosis_tree.get_children(), (str(new_diag_id),))

        self.view.diagnosis_search_var.set("S43")
        self.root.update_idletasks()
        self.assertEqual(self.view.diagnosis_tree.get_children(), (str(new_diag_id),))

        self.view.diagnosis_search_var.set("")
        self.root.update_idletasks()
        self.assertEqual(self.view.diagnosis_tree.get_children(), refreshed_children)

        self._select_diagnosis(new_diag_id)
        self.assertEqual(self.view.toggle_diagnosis_button.cget("text"), "Dezactivează")
        set_diagnosis_active(self.connection, new_diag_id, False)
        self.view.refresh()
        self._select_diagnosis(new_diag_id)
        self.assertEqual(self.view.toggle_diagnosis_button.cget("text"), "Activează")

    def test_search_filters_by_partial_code_spaces_and_scoped_interventions(self) -> None:
        second_int_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Osteosinteză centromedulară",
            code="OP-002",
        )
        self.view.refresh()

        # Diagnostic fără cod rămâne căutabil după denumire.
        self.view.diagnosis_search_var.set("  alt diag ")
        self.root.update_idletasks()
        self.assertEqual(self.view.diagnosis_tree.get_children(), (str(self.second_diagnosis_id),))

        # Căutare diagnostic după cod parțial cu spații și majuscule/minuscule.
        self.view.diagnosis_search_var.set(" s7 ")
        self.root.update_idletasks()
        self.assertEqual(self.view.diagnosis_tree.get_children(), (str(self.diagnosis_id),))

        # Selectăm diagnosticul 1 și filtrăm doar în intervențiile lui.
        self._select_diagnosis(self.diagnosis_id)
        self.assertEqual(
            set(self.view.intervention_tree.get_children()),
            {str(self.intervention_id), str(second_int_id)},
        )

        # Căutare intervenție după denumire.
        self.view.intervention_search_var.set("osteosint")
        self.root.update_idletasks()
        self.assertEqual(self.view.intervention_tree.get_children(), (str(second_int_id),))

        # Căutare intervenție după cod complet și parțial.
        self.view.intervention_search_var.set("op-002")
        self.root.update_idletasks()
        self.assertEqual(self.view.intervention_tree.get_children(), (str(second_int_id),))

        self.view.intervention_search_var.set(" 002 ")
        self.root.update_idletasks()
        self.assertEqual(self.view.intervention_tree.get_children(), (str(second_int_id),))

        # Căutarea unei intervenții aparținând altui diagnostic nu o afișează aici.
        self.view.intervention_search_var.set("pentru alt diagnostic")
        self.root.update_idletasks()
        self.assertEqual(self.view.intervention_tree.get_children(), ())

        # Selectând al doilea diagnostic, intervenția lui fără cod rămâne căutabilă după denumire.
        self.view.diagnosis_search_var.set("")
        self.view.intervention_search_var.set("")
        self.root.update_idletasks()
        self._select_diagnosis(self.second_diagnosis_id)
        self.view.intervention_search_var.set("alt diagnostic")
        self.root.update_idletasks()
        self.assertEqual(
            self.view.intervention_tree.get_children(),
            (str(self.second_intervention_id),),
        )


if __name__ == "__main__":
    unittest.main()
