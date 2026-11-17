from __future__ import annotations

import json
import locale
import os
import sqlite3
import sys
import tempfile
import tkinter as tk
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docx import Document
from tk_date_entry import DateEntry

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import (
    FieldValidationIssue,
    GeneratedSession,
    GenerationField,
    GenerationFieldOption,
    GenerationForm,
)
from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.services import document_generation
from ortodocs.services.document_generation import (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
    GenerationValidationError,
)
from ortodocs.services.medical_library import (
    add_diagnosis,
    add_field_options,
    add_intervention,
    create_variable,
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
)
from ortodocs.storage.database import connect_database
from ortodocs.storage.generation_repositories import (
    GenerationPersistenceError,
    get_generation_session,
)
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.generation_view import (
    SCROLL_DIAG_ENV_VAR,
    GenerationView,
    collect_generation_values,
    field_usage_text,
    select_display_values,
    select_value_for_index,
    unpack_touchpad_scroll_deltas,
)
from ortodocs.ui.generated_documents_window import GeneratedDocumentsWindow


def _docx(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class GenerationViewHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.select_field = GenerationField(
            variable_key="choice",
            field_definition_id=19,
            label="Alegere",
            data_type="select",
            scope="common",
            version=2,
            options=(
                GenerationFieldOption("first", "Aceeași etichetă", 0),
                GenerationFieldOption("second", "Aceeași etichetă", 1),
            ),
            required_in=(EPICRISIS_RECOMMENDATIONS,),
            used_in=(EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
        )
        self.optional_field = GenerationField(
            variable_key="optional_text",
            field_definition_id=20,
            label="Câmp opțional",
            data_type="text",
            scope="common",
            version=1,
            used_in=(OPERATIVE_PROTOCOL,),
        )
        self.form = GenerationForm(
            diagnosis_id=1,
            intervention_id=2,
            template_set_id=3,
            fields=(self.select_field, self.optional_field),
        )

    def test_select_labels_map_by_index_and_include_empty_choice(self) -> None:
        self.assertEqual(
            select_display_values(self.select_field),
            ("", "Aceeași etichetă", "Aceeași etichetă"),
        )
        self.assertEqual(select_value_for_index(self.select_field, -1), "")
        self.assertEqual(select_value_for_index(self.select_field, 0), "")
        self.assertEqual(select_value_for_index(self.select_field, 1), "first")
        self.assertEqual(select_value_for_index(self.select_field, 2), "second")

    def test_optional_fields_are_included_as_empty_values(self) -> None:
        self.assertEqual(
            collect_generation_values(self.form, {"choice": "first"}),
            {"choice": "first", "optional_text": ""},
        )
        self.assertEqual(
            collect_generation_values(
                self.form,
                {"choice": "", "optional_text": ""},
            ),
            {"choice": "", "optional_text": ""},
        )

    def test_common_field_has_one_form_entry_and_requirement_text_is_specific(self) -> None:
        self.assertEqual(sum(field.variable_key == "choice" for field in self.form.fields), 1)
        self.assertIn("Obligatoriu în: Epicriză + Recomandări", field_usage_text(self.select_field))
        self.assertIn("Protocol operator", field_usage_text(self.select_field))
        self.assertIn("Opțional", field_usage_text(self.optional_field))

    def test_helper_rejects_invalid_select_indexes_and_non_text_values(self) -> None:
        with self.assertRaises(ValueError):
            select_value_for_index(self.select_field, 3)
        with self.assertRaises(TypeError):
            select_value_for_index(self.select_field, True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            collect_generation_values(self.form, {"choice": 1})  # type: ignore[dict-item]


class GenerationViewFlowLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", autocommit=True)
        self.addCleanup(self.connection.close)
        self.statuses: list[tuple[str, dict[str, bool]]] = []
        self.events: list[str] = []
        self.view = SimpleNamespace(
            _generating=False,
            generation_form=GenerationForm(1, 2, 3, ()),
            selected_intervention_id=2,
            field_controls={},
            field_values={},
            connection=self.connection,
            generate_button=Mock(),
            documents_window=None,
            generated_session=None,
            result_label=Mock(),
            result_var=Mock(),
            update_idletasks=Mock(),
            _clear_validation_issues=Mock(),
            _update_generate_state=Mock(),
            _display_validation_issues=Mock(),
            _set_status=lambda message, **options: self.statuses.append((message, options)),
            winfo_toplevel=lambda: "main-window",
            _on_documents_window_closed=Mock(),
        )
        self.view._request_window_replacement = lambda **kwargs: (
            GenerationView._request_window_replacement(self.view, **kwargs)
        )
        self.view.open_documents_session = lambda session, **kwargs: (
            GenerationView.open_documents_session(self.view, session, **kwargs)
        )
        self.view._open_documents_session_after_approval = lambda session, approval: (
            GenerationView._open_documents_session_after_approval(self.view, session, approval)
        )
        self.session = GeneratedSession(
            77,
            RichDocument([Paragraph([TextInline("Epicriză")])]),
            RichDocument([Paragraph([TextInline("Protocol")])]),
        )

    def _run_generate(self) -> None:
        GenerationView._on_generate(self.view)

    def test_validation_failure_precedes_prompt_and_preserves_existing_window(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        self.view.documents_window = old_window
        issue = FieldValidationIssue("field", "required", (EPICRISIS_RECOMMENDATIONS,), "Lipsește.")
        with patch.object(
            document_generation,
            "prepare_documents",
            side_effect=GenerationValidationError((issue,)),
        ), patch.object(document_generation, "persist_prepared_documents") as persist:
            self._run_generate()

        old_window.ask_replacement_action.assert_not_called()
        persist.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)
        self.view._display_validation_issues.assert_called_once_with((issue,))

    def test_cancel_does_not_persist_or_close_existing_window(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True

        def choose_cancel_without_transaction() -> str:
            self.assertFalse(self.connection.in_transaction)
            return "cancel"

        old_window.ask_replacement_action.side_effect = choose_cancel_without_transaction
        self.view.documents_window = old_window
        with patch.object(document_generation, "prepare_documents", return_value=object()), patch.object(
            document_generation,
            "persist_prepared_documents",
        ) as persist:
            self._run_generate()

        persist.assert_not_called()
        old_window.close_after_replacement.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)

    def test_discard_then_persistence_failure_preserves_old_window(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "discard"
        self.view.documents_window = old_window
        with (
            patch.object(document_generation, "prepare_documents", return_value=object()),
            patch.object(
                document_generation,
                "persist_prepared_documents",
                side_effect=GenerationPersistenceError("disk error"),
            ),
        ):
            self._run_generate()

        old_window.close_after_replacement.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)
        self.assertIn("disk error", self.statuses[-1][0])

    def test_save_failure_aborts_new_persistence(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "save"
        old_window.save_dirty_documents.return_value = False
        self.view.documents_window = old_window
        with patch.object(document_generation, "prepare_documents", return_value=object()), patch.object(
            document_generation,
            "persist_prepared_documents",
        ) as persist:
            self._run_generate()

        old_window.save_dirty_documents.assert_called_once_with()
        persist.assert_not_called()
        old_window.close_after_replacement.assert_not_called()

    def test_success_persists_then_opens_new_window_then_closes_old(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "discard"
        self.view.documents_window = old_window
        new_window = Mock()

        def persist(*_args: object) -> GeneratedSession:
            self.events.append("persist")
            return self.session

        def create_window(*_args: object, **_kwargs: object) -> Mock:
            self.events.append("open")
            return new_window

        old_window.close_after_replacement.side_effect = lambda: self.events.append("close-old")
        new_window.show.side_effect = lambda: self.events.append("show-new")
        with patch.object(document_generation, "prepare_documents", return_value=object()), patch.object(
            document_generation,
            "persist_prepared_documents",
            side_effect=persist,
        ), patch("ortodocs.ui.generation_view.GeneratedDocumentsWindow", side_effect=create_window):
            self._run_generate()

        self.assertEqual(self.events, ["persist", "open", "close-old", "show-new"])
        self.assertIs(self.view.documents_window, new_window)
        self.assertIs(self.view.generated_session, self.session)

    def test_open_documents_session_uses_the_shared_editor_window(self) -> None:
        new_window = Mock()
        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            return_value=new_window,
        ) as create_window:
            self.assertTrue(GenerationView.open_documents_session(self.view, self.session))

        create_window.assert_called_once_with(
            "main-window",
            self.connection,
            self.session,
            on_closed=self.view._on_documents_window_closed,
            initially_hidden=True,
        )
        new_window.show.assert_called_once_with()
        self.assertIs(self.view.documents_window, new_window)

    def test_opening_the_same_session_raises_existing_window_without_duplication(self) -> None:
        existing_window = Mock()
        existing_window.session_id = self.session.session_id
        existing_window._closed = False
        self.view.documents_window = existing_window

        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow"
        ) as create_window:
            self.assertTrue(GenerationView.open_documents_session(self.view, self.session))

        create_window.assert_not_called()
        existing_window.show.assert_called_once_with()
        self.assertIs(self.view.documents_window, existing_window)

    def test_switching_sessions_saves_old_edits_before_open_and_close(self) -> None:
        old_window = Mock()
        old_window.session_id = 12
        old_window._closed = False
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "save"
        new_window = Mock()
        events: list[str] = []
        old_window.save_dirty_documents.side_effect = lambda: events.append("save") or True
        old_window.close_after_replacement.side_effect = lambda: events.append("close-old")
        new_window.show.side_effect = lambda: events.append("show-new")

        def create(*_args: object, **_kwargs: object) -> Mock:
            events.append("open-new")
            return new_window

        self.view.documents_window = old_window
        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            side_effect=create,
        ):
            self.assertTrue(GenerationView.open_documents_session(self.view, self.session))

        old_window.ask_replacement_action.assert_called_once_with()
        self.assertEqual(events, ["save", "open-new", "close-old", "show-new"])
        self.assertIs(self.view.documents_window, new_window)

    def test_switching_sessions_with_discard_keeps_old_until_new_window_opens(self) -> None:
        old_window = Mock()
        old_window.session_id = 12
        old_window._closed = False
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "discard"
        new_window = Mock()
        events: list[str] = []
        old_window.close_after_replacement.side_effect = lambda: events.append("close-old")
        new_window.show.side_effect = lambda: events.append("show-new")

        def create(*_args: object, **_kwargs: object) -> Mock:
            events.append("open-new")
            return new_window

        self.view.documents_window = old_window
        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            side_effect=create,
        ):
            self.assertTrue(GenerationView.open_documents_session(self.view, self.session))

        old_window.save_dirty_documents.assert_not_called()
        self.assertEqual(events, ["open-new", "close-old", "show-new"])
        self.assertIs(self.view.documents_window, new_window)

    def test_replacement_keeps_new_window_hidden_until_old_window_is_closed(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = False
        new_window = Mock()
        visible_windows = {"old"}
        old_window.close_after_replacement.side_effect = lambda: visible_windows.remove("old")

        def show_new_window() -> None:
            self.assertEqual(visible_windows, set())
            visible_windows.add("new")

        new_window.show.side_effect = show_new_window

        def create_hidden_window(*_args: object, **kwargs: object) -> Mock:
            self.assertIs(kwargs.get("initially_hidden"), True)
            self.assertEqual(visible_windows, {"old"})
            return new_window

        self.view.documents_window = old_window
        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            side_effect=create_hidden_window,
        ):
            self.assertTrue(GenerationView.open_documents_session(self.view, self.session))

        self.assertEqual(visible_windows, {"new"})
        self.assertIs(self.view.documents_window, new_window)

    def test_public_open_cannot_bypass_dirty_prompt_and_cancel_preserves_old_content(self) -> None:
        old_window = Mock()
        old_window.session_id = 12
        old_window._closed = False
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "cancel"
        old_content = object()
        old_window.editors = {"epicrisis_recommendations": old_content}
        self.view.documents_window = old_window

        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow"
        ) as create_window:
            self.assertFalse(GenerationView.open_documents_session(self.view, self.session))

        old_window.ask_replacement_action.assert_called_once_with()
        create_window.assert_not_called()
        old_window.close_after_replacement.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)
        self.assertIs(old_window.editors["epicrisis_recommendations"], old_content)

    def test_open_failure_does_not_close_or_replace_existing_window(self) -> None:
        old_window = Mock()
        old_window.session_id = 12
        old_window._closed = False
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "discard"
        self.view.documents_window = old_window

        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            side_effect=RuntimeError("window construction failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "window construction failed"):
                GenerationView.open_documents_session(self.view, self.session)

        old_window.ask_replacement_action.assert_called_once_with()
        old_window.close_after_replacement.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)

    def test_open_documents_session_has_no_replacement_bypass_argument(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "cancel"
        self.view.documents_window = old_window

        with self.assertRaises(TypeError):
            GenerationView.open_documents_session(
                self.view,
                self.session,
                replacement_resolved=True,  # type: ignore[call-arg]
            )

    def test_window_close_callback_clears_only_the_matching_reference(self) -> None:
        active_window = Mock()
        self.view.documents_window = active_window

        GenerationView._on_documents_window_closed(self.view, active_window)

        self.assertIsNone(self.view.documents_window)

        replacement_window = Mock()
        stale_window = Mock()
        self.view.documents_window = replacement_window
        GenerationView._on_documents_window_closed(self.view, stale_window)
        self.assertIs(self.view.documents_window, replacement_window)

    def test_application_close_still_delegates_to_active_editor(self) -> None:
        active_window = Mock()
        active_window.close.return_value = False
        self.view.documents_window = active_window

        self.assertFalse(GenerationView.resolve_application_close(self.view))

        active_window.close.assert_called_once_with()

    def test_save_then_successful_generation_closes_old_window_after_new_persistence(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "save"
        old_window.save_dirty_documents.return_value = True
        self.view.documents_window = old_window
        new_window = Mock()
        events: list[str] = []

        def persist(*_args: object) -> GeneratedSession:
            events.append("persist")
            return self.session

        def create_window(*_args: object, **_kwargs: object) -> Mock:
            events.append("open")
            return new_window

        old_window.close_after_replacement.side_effect = lambda: events.append("close-old")
        new_window.show.side_effect = lambda: events.append("show-new")
        with patch.object(document_generation, "prepare_documents", return_value=object()), patch.object(
            document_generation,
            "persist_prepared_documents",
            side_effect=persist,
        ), patch("ortodocs.ui.generation_view.GeneratedDocumentsWindow", side_effect=create_window):
            self._run_generate()

        old_window.save_dirty_documents.assert_called_once_with()
        self.assertEqual(events, ["persist", "open", "close-old", "show-new"])
        self.assertIs(self.view.documents_window, new_window)

    def test_save_decision_persistence_failure_keeps_old_window_open(self) -> None:
        old_window = Mock()
        old_window.has_dirty_documents = True
        old_window.ask_replacement_action.return_value = "save"
        old_window.save_dirty_documents.return_value = True
        self.view.documents_window = old_window
        with (
            patch.object(document_generation, "prepare_documents", return_value=object()),
            patch.object(
                document_generation,
                "persist_prepared_documents",
                side_effect=GenerationPersistenceError("disk error"),
            ),
        ):
            self._run_generate()

        old_window.save_dirty_documents.assert_called_once_with()
        old_window.close_after_replacement.assert_not_called()
        self.assertIs(self.view.documents_window, old_window)


class GenerationViewWidgetTests(unittest.TestCase):
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
        self.database_path = Path(self.temp_dir.name) / "generation-view.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)

        self.diagnosis_id = add_diagnosis(self.connection, "Diagnostic de interfață")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție disponibilă",
        )
        self.second_intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție fără șablon",
        )
        self.second_diagnosis_id = add_diagnosis(self.connection, "Alt diagnostic")
        self.other_diagnosis_intervention_id = add_intervention(
            self.connection,
            self.second_diagnosis_id,
            "Intervenție pentru alt diagnostic",
        )
        inactive_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Intervenție inactivă",
        )
        set_intervention_active(self.connection, inactive_id, False)
        self._activate_pair()

        self.root = tk.Tk()
        self.root.withdraw()
        self.view = GenerationView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        finally:
            self.connection.close()
            self.temp_dir.cleanup()

    def _activate_pair(self) -> None:
        shared = create_variable(
            self.connection,
            "shared_value",
            "Câmp comun obligatoriu",
            "text",
            "common",
        )
        number = create_variable(
            self.connection,
            "optional_number",
            "Număr opțional",
            "number",
            "common",
        )
        date = create_variable(
            self.connection,
            "optional_date",
            "Dată opțională",
            "date",
            "common",
        )
        select = create_variable(
            self.connection,
            "optional_select",
            "Listă opțională",
            "select",
            "common",
        )
        add_field_options(
            self.connection,
            select,
            (("a", "Opțiunea A", 0), ("b", "Opțiunea B", 1)),
        )
        template_set_id = create_template_set_draft(
            self.connection,
            self.diagnosis_id,
            self.intervention_id,
        )
        template_ids = import_template_pair(
            self.connection,
            template_set_id,
            {
                EPICRISIS_RECOMMENDATIONS: _docx(
                    "E {{shared_value}} |{{optional_number}}|{{optional_date}}|{{optional_select}}"
                ),
                OPERATIVE_PROTOCOL: _docx("P {{shared_value}}"),
            },
        )
        for key, field_id, required in (
            ("shared_value", shared, True),
            ("optional_number", number, False),
            ("optional_date", date, False),
            ("optional_select", select, False),
        ):
            add_variable_mapping(
                self.connection,
                template_ids[EPICRISIS_RECOMMENDATIONS],
                key,
                field_id,
                is_required=required,
            )
        add_variable_mapping(
            self.connection,
            template_ids[OPERATIVE_PROTOCOL],
            "shared_value",
            shared,
            is_required=True,
        )
        activation = activate_template_set(
            self.connection,
            template_set_id,
            patient_identification_review_confirmed=True,
        )
        if not activation.can_activate:
            raise AssertionError(activation.issues)

    def _choose(self, combo: ttk.Combobox, index: int) -> None:
        combo.current(index)
        combo.event_generate("<<ComboboxSelected>>")
        self.root.update_idletasks()

    def _choose_diagnosis(self, diagnosis_id: int) -> None:
        index = next(
            index
            for index, diagnosis in enumerate(self.view.diagnoses)
            if int(diagnosis["id"]) == diagnosis_id
        )
        self._choose(self.view.diagnosis_combo, index)

    def test_loads_only_active_diagnoses_and_interventions_without_auto_selection(self) -> None:
        self.assertEqual(self.view.diagnosis_combo.current(), -1)
        self.assertEqual(self.view.intervention_combo.current(), -1)
        self.assertEqual(len(self.view.diagnoses), 2)
        self._choose_diagnosis(self.diagnosis_id)
        intervention_ids = [int(item["id"]) for item in self.view.interventions]
        self.assertEqual(
            intervention_ids,
            [self.intervention_id, self.second_intervention_id],
        )
        self.assertNotIn("Intervenție inactivă", self.view.intervention_combo.cget("values"))

    def test_replacing_editor_never_shows_two_toplevels_at_once(self) -> None:
        self.root.deiconify()
        self.root.update()
        first_session = GeneratedSession(
            901,
            RichDocument([Paragraph([TextInline("Prima epicriză")])]),
            RichDocument([Paragraph([TextInline("Primul protocol")])]),
        )
        second_session = GeneratedSession(
            902,
            RichDocument([Paragraph([TextInline("A doua epicriză")])]),
            RichDocument([Paragraph([TextInline("Al doilea protocol")])]),
        )

        self.assertTrue(self.view.open_documents_session(first_session))
        old_window = self.view.documents_window
        assert old_window is not None
        self.root.update()
        self.assertTrue(old_window.window.winfo_viewable())

        actual_constructor = GeneratedDocumentsWindow
        created_windows: list[GeneratedDocumentsWindow] = []

        def create_hidden_window(*args: object, **kwargs: object) -> GeneratedDocumentsWindow:
            self.assertIs(kwargs.get("initially_hidden"), True)
            new_window = actual_constructor(*args, **kwargs)  # type: ignore[arg-type]
            created_windows.append(new_window)
            self.root.update_idletasks()
            self.assertFalse(new_window.window.winfo_viewable())
            original_show = new_window.show

            def show_after_old_is_closed() -> None:
                self.assertFalse(old_window.window.winfo_exists())
                original_show()

            new_window.show = show_after_old_is_closed  # type: ignore[method-assign]
            return new_window

        with patch(
            "ortodocs.ui.generation_view.GeneratedDocumentsWindow",
            side_effect=create_hidden_window,
        ):
            self.assertTrue(self.view.open_documents_session(second_session))

        self.root.update()
        self.assertFalse(old_window.window.winfo_exists())
        self.assertEqual(len(created_windows), 1)
        self.assertIs(self.view.documents_window, created_windows[0])
        self.assertTrue(created_windows[0].window.winfo_viewable())

    def test_empty_active_diagnosis_list_stays_blank_without_selection(self) -> None:
        set_diagnosis_active(self.connection, self.diagnosis_id, False)
        set_diagnosis_active(self.connection, self.second_diagnosis_id, False)
        empty_view = GenerationView(self.root, connection=self.connection)
        try:
            self.assertFalse(empty_view.diagnosis_combo.cget("values"))
            self.assertEqual(empty_view.diagnosis_combo.get(), "")
            self.assertEqual(empty_view.diagnosis_combo.current(), -1)
            self.assertIsNone(empty_view.selected_diagnosis_id)
            self.assertIsNone(empty_view.selected_intervention_id)
            self.assertIsNone(empty_view.generation_form)
        finally:
            empty_view.destroy()

    def test_diagnosis_without_active_interventions_stays_unselected(self) -> None:
        set_intervention_active(self.connection, self.intervention_id, False)
        set_intervention_active(self.connection, self.second_intervention_id, False)
        empty_view = GenerationView(self.root, connection=self.connection)
        try:
            diagnosis_index = next(
                index
                for index, diagnosis in enumerate(empty_view.diagnoses)
                if int(diagnosis["id"]) == self.diagnosis_id
            )
            empty_view.diagnosis_combo.current(diagnosis_index)
            empty_view.diagnosis_combo.event_generate("<<ComboboxSelected>>")
            self.root.update_idletasks()

            self.assertFalse(empty_view.intervention_combo.cget("values"))
            self.assertEqual(empty_view.intervention_combo.get(), "")
            self.assertEqual(empty_view.intervention_combo.current(), -1)
            self.assertEqual(empty_view.selected_diagnosis_id, self.diagnosis_id)
            self.assertIsNone(empty_view.selected_intervention_id)
            self.assertIsNone(empty_view.generation_form)
            self.assertIsNone(empty_view.form_canvas)
            self.assertIsNone(empty_view.fields_container)
            self.assertIn("Nu există intervenții active", empty_view.prompt_label.cget("text"))
        finally:
            empty_view.destroy()

    def test_dynamic_form_uses_distinct_fields_and_no_select_default(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)

        self.assertEqual(set(self.view.field_controls), {
            "shared_value",
            "optional_number",
            "optional_date",
            "optional_select",
        })
        self.assertEqual(len(self.view.field_controls), len(self.view.generation_form.fields))
        select_control = self.view.field_controls["optional_select"]["widget"]
        self.assertIsInstance(select_control, ttk.Combobox)
        self.assertEqual(select_control.get(), "")
        self.assertEqual(
            select_value_for_index(
                self.view.field_controls["optional_select"]["field"],
                select_control.current(),
            ),
            "",
        )
        select_control.current(2)
        self.root.update_idletasks()
        self.assertEqual(self.view.field_values["optional_select"], "b")
        select_control.current(0)
        self.root.update_idletasks()
        self.assertEqual(select_control.get(), "")
        self.assertEqual(self.view.field_values["optional_select"], "")
        self.assertEqual(self.view.field_controls["shared_value"]["variable"].get(), "")

    def test_generation_calls_service_once_and_displays_saved_session(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)
        self.view.field_controls["shared_value"]["variable"].set("Valoare completată")

        real_prepare = document_generation.prepare_documents
        real_persist = document_generation.persist_prepared_documents

        def prepare_once(*args, **kwargs):
            self.view._on_generate()
            return real_prepare(*args, **kwargs)

        with patch.object(
            document_generation,
            "prepare_documents",
            side_effect=prepare_once,
        ) as prepare, patch.object(
            document_generation,
            "persist_prepared_documents",
            wraps=real_persist,
        ) as persist:
            self.view._on_generate()

        prepare.assert_called_once()
        persist.assert_called_once()
        sent_values = prepare.call_args.args[2]
        self.assertEqual(sent_values["shared_value"], "Valoare completată")
        self.assertEqual(sent_values["optional_number"], "")
        self.assertEqual(sent_values["optional_date"], "")
        self.assertEqual(sent_values["optional_select"], "")
        self.assertIsNotNone(self.view.generated_session)
        self.assertIsNotNone(self.view.documents_window)
        self.assertIn(str(self.view.generated_session.session_id), self.view.status_var.get())
        self.assertIn("salvate", self.view.status_var.get())
        rows = self.connection.execute(
            "SELECT document_type, rich_text_json FROM generated_documents WHERE session_id = ?",
            (self.view.generated_session.session_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        saved_documents = {row["document_type"]: RichDocument.from_json(row["rich_text_json"]) for row in rows}

        def text_of(document: RichDocument) -> str:
            parts: list[str] = []
            for paragraph in document.paragraphs:
                if parts:
                    parts.append("\n")
                for inline in paragraph.inlines:
                    if isinstance(inline, TextInline):
                        parts.append(inline.text)
                    elif isinstance(inline, LineBreakInline):
                        parts.append("\n")
            return "".join(parts)

        self.assertIn(
            "Valoare completată |||",
            text_of(saved_documents[EPICRISIS_RECOMMENDATIONS]),
        )
        self.assertIn("Valoare completată", text_of(saved_documents[OPERATIVE_PROTOCOL]))

    def test_validation_issues_are_shown_and_cleared_after_field_edit(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)

        self.view._on_generate()

        self.assertIsInstance(self.view.validation_issues["shared_value"][0], FieldValidationIssue)
        error = self.view.field_controls["shared_value"]["error_variable"].get()
        self.assertIn("Epicriză + Recomandări", error)
        self.assertIn("Protocol operator", error)
        self.assertIn("Corectați câmpurile", self.view.status_var.get())
        self.view.field_controls["shared_value"]["variable"].set("completat")
        self.assertNotIn("shared_value", self.view.validation_issues)
        self.assertEqual(self.view.field_controls["shared_value"]["error_variable"].get(), "")

    def test_context_changes_clear_form_values_and_old_result(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)
        self.view.field_controls["shared_value"]["variable"].set("valoare veche")
        self.view._on_generate()
        self.assertIsNotNone(self.view.generated_session)

        self._choose(self.view.intervention_combo, 1)

        self.assertIsNone(self.view.generation_form)
        self.assertIsNone(self.view.generated_session)
        self.assertEqual(self.view.field_controls, {})
        self.assertFalse(self.view.generate_button.instate(["!disabled"]))

        self._choose_diagnosis(self.second_diagnosis_id)
        self.assertEqual(
            [int(item["id"]) for item in self.view.interventions],
            [self.other_diagnosis_intervention_id],
        )
        self.assertIsNone(self.view.selected_intervention_id)

    def test_changing_diagnosis_clears_loaded_form_and_values(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)
        self.view.field_controls["shared_value"]["variable"].set("valoare veche")
        self.assertIsNotNone(self.view.generation_form)

        self._choose_diagnosis(self.second_diagnosis_id)

        self.assertIsNone(self.view.generation_form)
        self.assertIsNone(self.view.selected_intervention_id)
        self.assertEqual(self.view.field_controls, {})
        self.assertEqual(self.view.intervention_combo.get(), "")
        self.assertEqual(self.view.intervention_combo.current(), -1)
        self.assertEqual(
            [int(item["id"]) for item in self.view.interventions],
            [self.other_diagnosis_intervention_id],
        )

    def test_refresh_clears_stale_generation_context_and_keeps_documents_window(self) -> None:
        self._choose_diagnosis(self.diagnosis_id)
        self._choose(self.view.intervention_combo, 0)
        self.view.field_controls["shared_value"]["variable"].set("valoare veche")
        documents_window = object()
        self.view.documents_window = documents_window

        set_diagnosis_active(self.connection, self.diagnosis_id, False)
        self.view.refresh()

        self.assertNotIn(
            self.diagnosis_id,
            [int(item["id"]) for item in self.view.diagnoses],
        )
        self.assertEqual(self.view.diagnosis_combo.get(), "")
        self.assertEqual(self.view.diagnosis_combo.current(), -1)
        self.assertEqual(self.view.intervention_combo.get(), "")
        self.assertEqual(self.view.intervention_combo.current(), -1)
        self.assertIsNone(self.view.selected_diagnosis_id)
        self.assertIsNone(self.view.selected_intervention_id)
        self.assertIsNone(self.view.generation_form)
        self.assertEqual(self.view.field_controls, {})
        self.assertIs(self.view.documents_window, documents_window)


class GenerationViewV3Tests(unittest.TestCase):
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
        self.database_path = Path(self.temp_dir.name) / "generation-view-v3.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)

        self.diagnosis_id = add_diagnosis(self.connection, "Artroză genunchi")
        self.intervention_id = add_intervention(
            self.connection,
            self.diagnosis_id,
            "Artroplastie totală genunchi",
        )
        epi_draft = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=_docx(
                "Epicriză V3: {{sex}}, {{varsta}}, regim {{regim_prezentare}}, "
                "data {{data_operatiei}}, ATI {{medic_anestezist}}, obs {{observatii}}."
            ),
        )
        self.epicrisis_version_id = int(
            activate_independent_template(
                self.connection,
                int(epi_draft["id"]),
                patient_identification_review_confirmed=True,
            )["id"]
        )
        proto_draft = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=self.intervention_id,
            document_bytes=_docx(
                "Protocol V3: {{sex}}, lateralitate {{lateralitate}}, obs {{observatii}}."
            ),
        )
        self.protocol_version_id = int(
            activate_independent_template(
                self.connection,
                int(proto_draft["id"]),
                patient_identification_review_confirmed=True,
            )["id"]
        )

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.view = GenerationView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)
        self.root.update_idletasks()

    @staticmethod
    def _choose(combobox: ttk.Combobox, index: int) -> None:
        combobox.current(index)
        combobox.event_generate("<<ComboboxSelected>>")

    @classmethod
    def _find_buttons(cls, widget: tk.Misc) -> list[ttk.Button]:
        found: list[ttk.Button] = []
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                found.append(child)
            found.extend(cls._find_buttons(child))
        return found

    def test_v3_independent_form_and_calendar_picker(self) -> None:
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)

        form = self.view.generation_form
        assert form is not None
        self.assertIsNone(form.template_set_id)
        self.assertEqual(form.epicrisis_template_version_id, self.epicrisis_version_id)
        self.assertEqual(form.operative_template_version_id, self.protocol_version_id)

        for key in (
            "varsta",
            "sex",
            "lateralitate",
            "regim_prezentare",
            "data_operatiei",
            "medic_anestezist",
            "observatii",
        ):
            self.assertIn(key, self.view.field_controls)

        sex_widget = self.view.field_controls["sex"]["widget"]
        assert isinstance(sex_widget, ttk.Combobox)
        self.assertEqual(tuple(sex_widget.cget("values")), ("", "Pacient", "Pacientă"))

        lat_widget = self.view.field_controls["lateralitate"]["widget"]
        assert isinstance(lat_widget, ttk.Combobox)
        self.assertEqual(
            tuple(lat_widget.cget("values")),
            ("", "dreapta", "stânga", "bilateral"),
        )

        regim_widget = self.view.field_controls["regim_prezentare"]["widget"]
        assert isinstance(regim_widget, ttk.Combobox)
        self.assertEqual(tuple(regim_widget.cget("values")), ("", "programat", "urgență"))

        date_entry = self.view.field_controls["data_operatiei"]["widget"]
        assert isinstance(date_entry, DateEntry)
        self.assertEqual(date_entry.date_pattern, "%d.%m.%Y")
        self.assertEqual(date_entry.first_weekday, 0)
        self.assertFalse(date_entry.show_week_numbers)
        self.assertEqual(date_entry.get(), "")
        self.assertIsNone(date_entry.get_date())

        # Denumirile lunilor și zilelor săptămânii în limba română, fără modificarea locale-ului global.
        self.assertEqual(date_entry._month_names[0], "Ianuarie")
        self.assertEqual(date_entry._month_names[10], "Noiembrie")
        self.assertEqual(date_entry._weekday_names[0], "lun.")
        self.assertEqual(date_entry._weekday_names[6], "dum.")
        self.assertEqual(locale.setlocale(locale.LC_TIME), "C")

        calendar_button = self.view.field_controls["data_operatiei"]["calendar_button"]
        assert isinstance(calendar_button, ttk.Button)
        self.assertIs(calendar_button, date_entry.button)

        # Deschidere popup, navigare lună/an și selectare zi -> format DD.MM.YYYY.
        date_entry.set_date(date(2026, 10, 5))
        self.assertEqual(date_entry.get(), "05.10.2026")
        self.assertEqual(date_entry.get_date(), date(2026, 10, 5))

        calendar_button.invoke()
        self.root.update_idletasks()
        popup = date_entry._calendar_window
        assert popup is not None and popup.winfo_exists()

        # Navigare cu săgeata la luna următoare (Noiembrie 2026), apoi prin combobox la an 2027.
        next_btn = next(b for b in self._find_buttons(popup) if b.cget("text") == "\u203a")
        next_btn.invoke()
        self.root.update_idletasks()
        self.assertEqual(date_entry._display_month, date(2026, 11, 1))
        self.assertEqual(date_entry._month_var.get(), "Noiembrie")

        date_entry._year_var.set("2027")
        date_entry._on_year_combo_selected()
        self.root.update_idletasks()
        self.assertEqual(date_entry._display_month, date(2027, 11, 1))

        # Selectarea zilei 15 din calendar închide popup-ul și scrie DD.MM.YYYY.
        day_15_btn = next(b for b in self._find_buttons(popup) if b.cget("text") == "15")
        day_15_btn.invoke()
        self.root.update_idletasks()
        self.assertIsNone(date_entry._calendar_window)
        self.assertEqual(date_entry.get(), "15.11.2027")
        self.assertEqual(date_entry.get_date(), date(2027, 11, 15))
        self.assertEqual(
            self.view.field_controls["data_operatiei"]["variable"].get(),
            "15.11.2027",
        )

        # Ștergere prin butonul Clear din popup -> dată goală.
        calendar_button.invoke()
        self.root.update_idletasks()
        popup2 = date_entry._calendar_window
        assert popup2 is not None
        clear_btn = next(b for b in self._find_buttons(popup2) if b.cget("text") == "Clear")
        clear_btn.invoke()
        self.root.update_idletasks()
        self.assertIsNone(date_entry._calendar_window)
        self.assertEqual(date_entry.get(), "")
        self.assertIsNone(date_entry.get_date())

        # Selectare prin butonul Today + API set/clear.
        calendar_button.invoke()
        self.root.update_idletasks()
        popup3 = date_entry._calendar_window
        assert popup3 is not None
        today_btn = next(b for b in self._find_buttons(popup3) if b.cget("text") == "Today")
        today_btn.invoke()
        self.root.update_idletasks()
        self.assertEqual(date_entry.get(), date.today().strftime("%d.%m.%Y"))

        date_entry.set("24.12.2026")
        self.assertEqual(date_entry.get_date(), date(2026, 12, 24))
        date_entry.clear()
        self.assertEqual(date_entry.get(), "")
        self.assertIsNone(date_entry.get_date())

    def test_v3_generation_with_optional_empty_fields_and_independent_editing(self) -> None:
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)

        sex_widget = self.view.field_controls["sex"]["widget"]
        assert isinstance(sex_widget, ttk.Combobox)
        self._choose(sex_widget, 2)  # "Pacientă"
        lat_widget = self.view.field_controls["lateralitate"]["widget"]
        assert isinstance(lat_widget, ttk.Combobox)
        self._choose(lat_widget, 2)  # "stânga"
        # Restul câmpurilor rămân goale (""), fără erori de validare.

        self.view.generate_button.invoke()
        session = self.view.generated_session
        assert session is not None
        window = self.view.documents_window
        assert window is not None

        epi_editor = window.editors[EPICRISIS_RECOMMENDATIONS]
        epi_editor.text_widget.insert("1.0", "Editat local · ")
        self.assertTrue(epi_editor.is_dirty)
        self.root.update_idletasks()
        self.assertTrue(window.save_dirty_documents())
        window.close()

        reloaded = get_generation_session(self.connection, session.session_id)
        assert reloaded is not None
        self.assertIsNone(reloaded.history_item.template_set_id)
        self.assertEqual(reloaded.epicrisis_template_version_id, self.epicrisis_version_id)
        self.assertEqual(reloaded.operative_template_version_id, self.protocol_version_id)
        epi_text = "".join(
            inline.text
            for p in reloaded.epicrisis_recommendations.paragraphs
            for inline in p.inlines
            if isinstance(inline, TextInline)
        )
        proto_text = "".join(
            inline.text
            for p in reloaded.operative_protocol.paragraphs
            for inline in p.inlines
            if isinstance(inline, TextInline)
        )
        self.assertTrue(epi_text.startswith("Editat local"))
        self.assertIn("Pacientă", proto_text)
        self.assertIn("lateralitate stânga", proto_text)
        self.assertNotIn("Editat local", proto_text)

    def test_v3_ui_controls_preserve_canonical_specs_despite_db_definitions(self) -> None:
        create_variable(self.connection, "data_operatiei", "Data DB", "text", "common")
        create_variable(self.connection, "sex", "Sex DB", "text", "common")
        bad_regim_id = create_variable(self.connection, "regim_prezentare", "Regim DB", "select", "common")
        add_field_options(self.connection, bad_regim_id, [("alt_regim", "Alt regim", 1)])
        create_variable(self.connection, "lateralitate", "Lateralitate DB", "text", "common")
        create_variable(self.connection, "varsta", "Vârstă DB", "number", "common")
        bad_med_id = create_variable(self.connection, "medic_anestezist", "ATI DB", "select", "common")
        add_field_options(self.connection, bad_med_id, [("dr_a", "Dr. A", 1)])

        self.view.refresh()
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)

        self.assertIsInstance(self.view.field_controls["sex"]["widget"], ttk.Combobox)
        self.assertEqual(
            tuple(self.view.field_controls["sex"]["widget"].cget("values")),
            ("", "Pacient", "Pacientă"),
        )
        self.assertIsInstance(self.view.field_controls["lateralitate"]["widget"], ttk.Combobox)
        self.assertEqual(
            tuple(self.view.field_controls["lateralitate"]["widget"].cget("values")),
            ("", "dreapta", "stânga", "bilateral"),
        )
        self.assertIsInstance(self.view.field_controls["regim_prezentare"]["widget"], ttk.Combobox)
        self.assertEqual(
            tuple(self.view.field_controls["regim_prezentare"]["widget"].cget("values")),
            ("", "programat", "urgență"),
        )
        self.assertIsInstance(self.view.field_controls["data_operatiei"]["widget"], DateEntry)
        self.assertIn("calendar_button", self.view.field_controls["data_operatiei"])
        self.assertIsInstance(self.view.field_controls["varsta"]["widget"], ttk.Entry)
        self.assertIsInstance(self.view.field_controls["medic_anestezist"]["widget"], ttk.Entry)

    def test_v3_field_usage_labels_for_epicrisis_protocol_both_and_unused(self) -> None:
        # În setUp:
        # - varsta apare doar în Epicriză;
        # - lateralitate apare doar în Protocol;
        # - sex apare în ambele;
        # Testăm și o combinație în care medic_anestezist, regim_prezentare și data_operatiei
        # lipsesc din ambele șabloane active.
        epi_minimal = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=self.diagnosis_id,
            document_bytes=_docx("Epicriză doar cu {{sex}} și {{varsta}}."),
        )
        activate_independent_template(
            self.connection,
            int(epi_minimal["id"]),
            patient_identification_review_confirmed=True,
        )
        self.view.refresh()
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)

        varsta_label = self.view.field_controls["varsta"]["usage_label"]
        lat_label = self.view.field_controls["lateralitate"]["usage_label"]
        sex_label = self.view.field_controls["sex"]["usage_label"]
        ati_label = self.view.field_controls["medic_anestezist"]["usage_label"]
        regim_label = self.view.field_controls["regim_prezentare"]["usage_label"]
        data_label = self.view.field_controls["data_operatiei"]["usage_label"]

        assert isinstance(varsta_label, ttk.Label)
        assert isinstance(lat_label, ttk.Label)
        assert isinstance(sex_label, ttk.Label)
        assert isinstance(ati_label, ttk.Label)
        assert isinstance(regim_label, ttk.Label)
        assert isinstance(data_label, ttk.Label)

        self.assertEqual(
            str(varsta_label.cget("text")),
            "Utilizat în: Epicriză + Recomandări. Opțional.",
        )
        self.assertEqual(
            str(lat_label.cget("text")),
            "Utilizat în: Protocol operator. Opțional.",
        )
        self.assertEqual(
            str(sex_label.cget("text")),
            "Utilizat în: Epicriză + Recomandări, Protocol operator. Opțional.",
        )
        for unused_label in (ati_label, regim_label, data_label):
            text = str(unused_label.cget("text"))
            self.assertEqual(text, "Câmp standard V3 (opțional).")
            self.assertNotIn("Utilizat în:", text)

    def _run_in_mainloop(self, callback: object) -> None:
        recorded_error: list[BaseException] = []
        previous_reporter = self.root.report_callback_exception

        def _capture_exception(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: object,
        ) -> None:
            recorded_error.append(exc_value)
            self.root.quit()

        def _execute() -> None:
            try:
                callback()  # type: ignore[operator]
            except BaseException as exc:
                recorded_error.append(exc)
            finally:
                self.root.after_idle(self.root.quit)

        self.root.report_callback_exception = _capture_exception  # type: ignore[method-assign]
        try:
            self.root.after_idle(_execute)
            self.root.mainloop()
        finally:
            self.root.report_callback_exception = previous_reporter  # type: ignore[method-assign]
        if recorded_error:
            raise recorded_error[0]

    def test_generate_button_in_mainloop_without_selection_keeps_window_and_app_open(self) -> None:
        self.root.deiconify()
        self.root.update()
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)

        def exercise() -> None:
            self.view.generate_button.invoke()
            self.root.update()

            window = self.view.documents_window
            assert window is not None
            self.assertTrue(self.view.winfo_exists())
            self.assertTrue(window.window.winfo_exists())
            self.assertEqual(
                window.editors[EPICRISIS_RECOMMENDATIONS].text_widget.tag_ranges("sel"),
                (),
            )
            self.assertEqual(
                window.editors[OPERATIVE_PROTOCOL].text_widget.tag_ranges("sel"),
                (),
            )

            window.close()
            self.assertTrue(self.view.winfo_exists())

        self._run_in_mainloop(exercise)

    def test_generate_edit_and_save_both_documents_in_single_mainloop_session(self) -> None:
        self.root.deiconify()
        self.root.update()
        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)
        sex_widget = self.view.field_controls["sex"]["widget"]
        assert isinstance(sex_widget, ttk.Combobox)
        self._choose(sex_widget, 1)  # "Pacient"
        lat_widget = self.view.field_controls["lateralitate"]["widget"]
        assert isinstance(lat_widget, ttk.Combobox)
        self._choose(lat_widget, 1)  # "dreapta"

        def exercise() -> None:
            self.view.generate_button.invoke()
            self.root.update()

            session = self.view.generated_session
            window = self.view.documents_window
            assert session is not None
            assert window is not None

            epi_editor = window.editors[EPICRISIS_RECOMMENDATIONS]
            proto_editor = window.editors[OPERATIVE_PROTOCOL]

            initial_epi_text = epi_editor.text_widget.get("1.0", "end-1c")
            self.assertFalse(epi_editor.is_dirty)
            self.assertFalse(proto_editor.is_dirty)

            window.window.focus_force()
            epi_editor.text_widget.focus_force()
            self.root.update()
            epi_editor.bold_button.invoke()
            epi_editor.text_widget.edit_separator()
            epi_editor.text_widget.insert("1.0", "Notă epicriză · ")
            epi_editor.text_widget.edit_separator()
            self.assertEqual(
                epi_editor.text_widget.get("1.0", "end-1c"),
                f"Notă epicriză · {initial_epi_text}",
            )
            self.assertTrue(epi_editor.is_dirty)

            epi_editor.text_widget.event_generate("<<Undo>>", when="now")
            self.assertEqual(epi_editor.text_widget.get("1.0", "end-1c"), initial_epi_text)
            self.assertFalse(epi_editor.is_dirty)

            epi_editor.text_widget.event_generate("<<Redo>>", when="now")
            self.assertEqual(
                epi_editor.text_widget.get("1.0", "end-1c"),
                f"Notă epicriză · {initial_epi_text}",
            )
            self.assertTrue(epi_editor.is_dirty)

            window.notebook.select(window.notebook.tabs()[1])
            self.root.update()
            proto_editor.text_widget.focus_force()
            self.root.update()
            proto_editor.italic_button.invoke()
            proto_editor.text_widget.insert("1.0", "Notă protocol · ")
            self.assertTrue(proto_editor.is_dirty)

            self.assertTrue(window.save_dirty_documents())
            self.assertFalse(epi_editor.is_dirty)
            self.assertFalse(proto_editor.is_dirty)
            self.assertTrue(window.window.winfo_exists())
            self.assertTrue(self.view.winfo_exists())

            reloaded = get_generation_session(self.connection, session.session_id)
            assert reloaded is not None
            epi_text = "".join(
                inline.text
                for paragraph in reloaded.epicrisis_recommendations.paragraphs
                for inline in paragraph.inlines
                if isinstance(inline, TextInline)
            )
            proto_text = "".join(
                inline.text
                for paragraph in reloaded.operative_protocol.paragraphs
                for inline in paragraph.inlines
                if isinstance(inline, TextInline)
            )
            self.assertTrue(epi_text.startswith("Notă epicriză"))
            self.assertTrue(proto_text.startswith("Notă protocol"))
            window.close()

        self._run_in_mainloop(exercise)

    def test_v3_date_entry_popup_escape_outside_click_and_generation_in_mainloop(
        self,
    ) -> None:
        initial_locale = locale.setlocale(locale.LC_TIME)
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.root.geometry("1100x760+60+60")
        self.root.deiconify()
        self.root.update()

        self._choose(self.view.diagnosis_combo, 0)
        self._choose(self.view.intervention_combo, 0)
        sex_widget = self.view.field_controls["sex"]["widget"]
        assert isinstance(sex_widget, ttk.Combobox)
        self._choose(sex_widget, 1)  # "Pacient"
        lat_widget = self.view.field_controls["lateralitate"]["widget"]
        assert isinstance(lat_widget, ttk.Combobox)
        self._choose(lat_widget, 1)  # "dreapta"

        def exercise() -> None:
            date_entry = self.view.field_controls["data_operatiei"]["widget"]
            assert isinstance(date_entry, DateEntry)

            # 1. Poziționarea popup-ului și închiderea prin Escape.
            date_entry.button.invoke()
            self.root.update()
            popup = date_entry._calendar_window
            assert popup is not None and popup.winfo_exists()
            self.assertEqual(popup.winfo_rootx(), date_entry.winfo_rootx())
            self.assertGreaterEqual(
                popup.winfo_rooty(),
                0,
            )

            popup.event_generate("<Escape>", when="now")
            self.root.update()
            self.assertIsNone(date_entry._calendar_window)

            # 2. Închiderea prin click în afara popup-ului (pe câmpul de diagnostic).
            date_entry.button.invoke()
            self.root.update()
            popup2 = date_entry._calendar_window
            assert popup2 is not None and popup2.winfo_exists()
            outside = self.view.diagnosis_combo
            ox = outside.winfo_rootx() + 10
            oy = outside.winfo_rooty() + 10
            outside.event_generate("<ButtonPress-1>", rootx=ox, rooty=oy, x=10, y=10, when="now")
            self.root.update()
            self.assertIsNone(date_entry._calendar_window)

            # 3. Navigare lună/an și alegerea unei zile din popup -> generare CU dată.
            date_entry.set_date(date(2026, 11, 1))
            date_entry.button.invoke()
            self.root.update()
            popup3 = date_entry._calendar_window
            assert popup3 is not None
            next_btn = next(b for b in self._find_buttons(popup3) if b.cget("text") == "\u203a")
            next_btn.invoke()
            self.root.update()
            date_entry._year_var.set("2028")
            date_entry._on_year_combo_selected()
            self.root.update()
            day_20_btn = next(b for b in self._find_buttons(popup3) if b.cget("text") == "20")
            day_20_btn.invoke()
            self.root.update()
            self.assertEqual(date_entry.get(), "20.12.2028")
            self.assertEqual(self.view.field_values["data_operatiei"], "20.12.2028")

            self.view.generate_button.invoke()
            self.root.update()
            session_with_date = self.view.generated_session
            assert session_with_date is not None
            window1 = self.view.documents_window
            assert window1 is not None
            epi_with_date = "".join(
                inline.text
                for p in session_with_date.epicrisis_recommendations.paragraphs
                for inline in p.inlines
                if isinstance(inline, TextInline)
            )
            self.assertIn("data 20.12.2028", epi_with_date)
            window1.close()

            # 4. Validare dată invalidă + ștergere cu clear() + generare FĂRĂ dată.
            date_entry.set("31.02.2026")
            self.view.generate_button.invoke()
            self.root.update()
            self.assertIn("data_operatiei", self.view.validation_issues)
            self.assertEqual(str(date_entry.entry.cget("style")), "Invalid.TEntry")

            date_entry.clear()
            self.root.update()
            self.assertNotIn("data_operatiei", self.view.validation_issues)
            self.assertEqual(str(date_entry.entry.cget("style")), "TEntry")
            self.assertEqual(self.view.field_values["data_operatiei"], "")

            self.view.generate_button.invoke()
            self.root.update()
            session_without_date = self.view.generated_session
            assert session_without_date is not None
            self.assertNotEqual(session_without_date.session_id, session_with_date.session_id)
            window2 = self.view.documents_window
            assert window2 is not None
            epi_without_date = "".join(
                inline.text
                for p in session_without_date.epicrisis_recommendations.paragraphs
                for inline in p.inlines
                if isinstance(inline, TextInline)
            )
            self.assertIn("data ,", epi_without_date)
            window2.close()
            self.assertEqual(locale.setlocale(locale.LC_TIME), initial_locale)

        self._run_in_mainloop(exercise)

    def test_v3_search_filters_and_groups_standard_vs_dynamic_fields(self) -> None:
        add_diagnosis(self.connection, "Fractură de test")
        self.view.refresh()
        self.assertEqual(len(self.view.diagnosis_combo.cget("values")), 2)

        self.view.diagnosis_search_var.set("inexistent")
        self.root.update_idletasks()
        self.assertEqual(tuple(self.view.diagnosis_combo.cget("values")), ())

        self.view.diagnosis_search_var.set("artroz")
        self.root.update_idletasks()
        self.assertEqual(tuple(self.view.diagnosis_combo.cget("values")), ("Artroză genunchi",))
        self._choose(self.view.diagnosis_combo, 0)

        self.view.intervention_search_var.set("total")
        self.root.update_idletasks()
        self.assertEqual(
            tuple(self.view.intervention_combo.cget("values")),
            ("Artroplastie totală genunchi",),
        )
        self._choose(self.view.intervention_combo, 0)

        section_titles = [
            child.cget("text")
            for child in self.view.fields_container.winfo_children()
            if isinstance(child, ttk.Label)
        ]
        self.assertIn("Date generale", section_titles)
        self.assertIn("Câmpuri suplimentare din șabloane", section_titles)

    def test_v3_full_width_layout_and_step3_prompt_lifecycle(self) -> None:
        add_diagnosis(self.connection, "Fractură de test")
        self.view.refresh()

        # 1. Fără coloană laterală; layout pe o singură coloană, Pas 1 și Pas 2 alăturate.
        self.assertFalse(hasattr(self.view, "result_label"))
        self.assertFalse(hasattr(self.view, "docs_status_badge"))
        self.assertEqual(self.view.grid_size()[0], 1)
        self.assertEqual(int(self.view.diagnosis_combo.grid_info()["column"]), 0)
        self.assertEqual(int(self.view.intervention_combo.grid_info()["column"]), 1)
        self.assertEqual(int(self.view.context_card.grid_info()["row"]), 0)
        self.assertEqual(int(self.view.form_card.grid_info()["row"]), 1)

        # 2. Înainte de selecție: nu există zonă goală scrollabilă; apare mesajul simplu centrat.
        self.assertIsNone(self.view.form_canvas)
        self.assertIsNone(self.view.form_scrollbar)
        self.assertIsNone(self.view.fields_container)
        self.assertEqual(self.view.prompt_label.cget("text"), "Selectați un diagnostic și o intervenție.")
        self.assertEqual(self.view.prompt_frame.winfo_manager(), "grid")
        self.assertEqual(str(self.view.generate_button.cget("state")), "disabled")
        self.assertEqual(int(self.view.rowconfigure(1)["weight"]), 0)

        # Doar diagnosticul selectat -> Pasul 3 rămâne compact fără zonă scrollabilă.
        self._choose(self.view.diagnosis_combo, 0)
        self.assertIsNone(self.view.form_canvas)
        self.assertEqual(self.view.prompt_label.cget("text"), "Selectați un diagnostic și o intervenție.")
        self.assertEqual(self.view.prompt_frame.winfo_manager(), "grid")
        self.assertEqual(str(self.view.generate_button.cget("state")), "disabled")

        # 3. După selectarea diagnosticului și a intervenției: mesajul dispare, formularul dinamic apare.
        self._choose(self.view.intervention_combo, 0)
        self.assertEqual(self.view.prompt_frame.winfo_manager(), "")
        self.assertIsNotNone(self.view.form_canvas)
        self.assertIsNotNone(self.view.form_scrollbar)
        self.assertIsNotNone(self.view.fields_container)
        self.assertTrue(self.view.field_controls)
        self.assertEqual(int(self.view.rowconfigure(1)["weight"]), 1)
        self.assertEqual(str(self.view.generate_button.cget("state")), "normal")

        # 4. Schimbarea către un diagnostic fără intervenție selectată readuce mesajul și elimină zona de câmpuri.
        self._choose(self.view.diagnosis_combo, 1)
        self.assertIsNone(self.view.form_canvas)
        self.assertIsNone(self.view.fields_container)
        self.assertEqual(self.view.prompt_frame.winfo_manager(), "grid")
        self.assertEqual(str(self.view.generate_button.cget("state")), "disabled")
        self.assertEqual(int(self.view.rowconfigure(1)["weight"]), 0)

        # Deselectarea diagnosticului păstrează starea compactă cu mesajul de selecție.
        self.view.diagnosis_combo.set("")
        self.view.diagnosis_combo.event_generate("<<ComboboxSelected>>")
        self.root.update_idletasks()
        self.assertIsNone(self.view.form_canvas)
        self.assertEqual(self.view.prompt_label.cget("text"), "Selectați un diagnostic și o intervenție.")
        self.assertEqual(str(self.view.generate_button.cget("state")), "disabled")

    def test_v3_search_by_name_and_code_displays_codes_and_preserves_canonical_generation(self) -> None:
        coded_diag_id = add_diagnosis(
            self.connection,
            "Fractură diafizară femur",
            code="S72.3",
        )
        uncoded_diag_id = add_diagnosis(self.connection, "Luxație simplă")
        coded_int_id = add_intervention(
            self.connection,
            coded_diag_id,
            "Artroplastie totală șold",
            code="OP-001",
        )
        add_intervention(
            self.connection,
            coded_diag_id,
            "Revizie simplă",
        )
        epi = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=coded_diag_id,
            document_bytes=_docx("Epicriză {{sex}}."),
        )
        activate_independent_template(
            self.connection,
            int(epi["id"]),
            patient_identification_review_confirmed=True,
        )
        proto = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=coded_int_id,
            document_bytes=_docx("Protocol {{lateralitate}}."),
        )
        activate_independent_template(
            self.connection,
            int(proto["id"]),
            patient_identification_review_confirmed=True,
        )

        self.view.refresh()
        values = tuple(self.view.diagnosis_combo.cget("values"))
        self.assertIn("Fractură diafizară femur · S72.3", values)
        self.assertIn("Luxație simplă", values)

        # Căutare diagnostic fără cod după denumire -> fără etichetă de cod dedesubt.
        self.view.diagnosis_search_var.set("  luxaț ")
        self.root.update_idletasks()
        self.assertEqual(tuple(self.view.diagnosis_combo.cget("values")), ("Luxație simplă",))
        self._choose(self.view.diagnosis_combo, 0)
        self.assertEqual(self.view.selected_diagnosis_id, uncoded_diag_id)
        self.assertEqual(self.view.diagnosis_code_var.get(), "")
        self.assertEqual(self.view.diagnosis_code_label.winfo_manager(), "")

        # Căutare diagnostic după cod parțial și complet -> afișare cod discret dedesubt.
        self.view.diagnosis_search_var.set(" 72. ")
        self.root.update_idletasks()
        self.assertEqual(
            tuple(self.view.diagnosis_combo.cget("values")),
            ("Fractură diafizară femur · S72.3",),
        )
        self.view.diagnosis_search_var.set(" s72.3 ")
        self.root.update_idletasks()
        self.assertEqual(
            tuple(self.view.diagnosis_combo.cget("values")),
            ("Fractură diafizară femur · S72.3",),
        )
        self._choose(self.view.diagnosis_combo, 0)
        self.assertEqual(self.view.selected_diagnosis_id, coded_diag_id)
        self.assertEqual(self.view.diagnosis_code_var.get(), "Cod: S72.3")
        self.assertEqual(self.view.diagnosis_code_label.winfo_manager(), "grid")

        # Intervenții filtrate doar pentru diagnosticul selectat (cod complet, parțial, fără cod).
        int_values = tuple(self.view.intervention_combo.cget("values"))
        self.assertEqual(set(int_values), {"Artroplastie totală șold · OP-001", "Revizie simplă"})

        self.view.intervention_search_var.set("reviz")
        self.root.update_idletasks()
        self.assertEqual(tuple(self.view.intervention_combo.cget("values")), ("Revizie simplă",))
        self._choose(self.view.intervention_combo, 0)
        self.assertEqual(self.view.intervention_code_var.get(), "")
        self.assertEqual(self.view.intervention_code_label.winfo_manager(), "")

        self.view.intervention_search_var.set(" 001 ")
        self.root.update_idletasks()
        self.assertEqual(
            tuple(self.view.intervention_combo.cget("values")),
            ("Artroplastie totală șold · OP-001",),
        )
        self.view.intervention_search_var.set("op-001")
        self.root.update_idletasks()
        self._choose(self.view.intervention_combo, 0)
        self.assertEqual(self.view.selected_intervention_id, coded_int_id)
        self.assertEqual(self.view.intervention_code_var.get(), "Cod: OP-001")
        self.assertEqual(self.view.intervention_code_label.winfo_manager(), "grid")

        # Generarea folosește ID-urile și denumirile canonice fără cod în document/snapshot.
        self.view.generate_button.invoke()
        self.root.update_idletasks()
        session = self.view.generated_session
        assert session is not None
        row = self.connection.execute(
            """
            SELECT diagnosis_id, intervention_id, diagnosis_label_snapshot, intervention_label_snapshot
            FROM generation_sessions
            WHERE id = ?
            """,
            (session.session_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["diagnosis_id"]), coded_diag_id)
        self.assertEqual(int(row["intervention_id"]), coded_int_id)
        self.assertEqual(row["diagnosis_label_snapshot"], "Fractură diafizară femur")
        self.assertEqual(row["intervention_label_snapshot"], "Artroplastie totală șold")
        if self.view.documents_window is not None:
            self.view.documents_window.close()

    def test_v3_real_entry_typing_and_enter_key_selects_and_posts_dropdown(self) -> None:
        diag_target_id = add_diagnosis(
            self.connection,
            "DIAGNOSTIC TEST V3",
            code="E69.0",
        )
        diag_second_id = add_diagnosis(
            self.connection,
            "Alt diagnostic E69",
            code="E69.1",
        )
        int_target_id = add_intervention(
            self.connection,
            diag_target_id,
            "INTERVENȚIE TEST V3",
            code="INT-901",
        )
        int_second_id = add_intervention(
            self.connection,
            diag_target_id,
            "Altă intervenție 90",
            code="INT-902",
        )
        epi = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=diag_target_id,
            document_bytes=_docx("Epicriză {{sex}}."),
        )
        activate_independent_template(
            self.connection,
            int(epi["id"]),
            patient_identification_review_confirmed=True,
        )
        proto = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=int_target_id,
            document_bytes=_docx("Protocol {{lateralitate}}."),
        )
        activate_independent_template(
            self.connection,
            int(proto["id"]),
            patient_identification_review_confirmed=True,
        )

        # Simulăm revenirea din altă secțiune după modificarea bazei de date.
        self.view.on_show()

        def exercise() -> None:
            self.root.deiconify()
            self.root.update()
            # 1. Introducere reală în Entry cu mai multe rezultate ("E69") + Enter -> dropdown deschis cu filtru păstrat.
            self.view.diagnosis_search_entry.focus_set()
            self.view.diagnosis_search_entry.delete(0, "end")
            self.view.diagnosis_search_entry.insert(0, "E69")
            self.view.diagnosis_search_entry.event_generate("<KeyRelease>", when="now")
            self.root.update()
            self.assertEqual(
                set(self.view.diagnosis_combo.cget("values")),
                {"DIAGNOSTIC TEST V3 · E69.0", "Alt diagnostic E69 · E69.1"},
            )
            self.view.diagnosis_search_entry.event_generate("<Return>", when="now")
            self.root.update()
            self.assertEqual(self.view.diagnosis_search_var.get(), "E69")
            self.assertEqual(
                len(self.view.diagnosis_combo.cget("values")),
                2,
            )
            popdown = self.root.tk.call("ttk::combobox::PopdownWindow", self.view.diagnosis_combo)
            lb_items = self.root.tk.call(f"{popdown}.f.l", "get", 0, "end")
            self.assertIn("DIAGNOSTIC TEST V3 · E69.0", lb_items)
            self.assertIn("Alt diagnostic E69 · E69.1", lb_items)

            # Închidem popdown-ul și rafinăm la "E69.0" -> 1 singur rezultat -> Enter selectează automat.
            try:
                self.root.tk.call("ttk::combobox::Unpost", self.view.diagnosis_combo)
            except tk.TclError:
                pass
            self.view.diagnosis_search_entry.focus_set()
            self.view.diagnosis_search_entry.insert("end", ".0")
            self.view.diagnosis_search_entry.event_generate("<KeyRelease>", when="now")
            self.root.update()
            self.assertEqual(
                tuple(self.view.diagnosis_combo.cget("values")),
                ("DIAGNOSTIC TEST V3 · E69.0",),
            )
            self.view.diagnosis_search_entry.event_generate("<Return>", when="now")
            self.root.update()
            self.assertEqual(self.view.selected_diagnosis_id, diag_target_id)
            self.assertNotEqual(self.view.selected_diagnosis_id, diag_second_id)
            self.assertEqual(self.view.diagnosis_combo.get(), "DIAGNOSTIC TEST V3 · E69.0")
            self.assertEqual(self.view.diagnosis_code_var.get(), "Cod: E69.0")
            self.assertEqual(self.view.diagnosis_code_label.winfo_manager(), "grid")

            # 2. Aceeași interacțiune reală pentru intervenții ("INT-90" -> >1, apoi "1" -> 1 + Enter).
            self.view.intervention_search_entry.focus_set()
            self.view.intervention_search_entry.delete(0, "end")
            self.view.intervention_search_entry.insert(0, "INT-90")
            self.view.intervention_search_entry.event_generate("<KeyRelease>", when="now")
            self.root.update()
            self.assertEqual(
                set(self.view.intervention_combo.cget("values")),
                {"INTERVENȚIE TEST V3 · INT-901", "Altă intervenție 90 · INT-902"},
            )
            self.view.intervention_search_entry.event_generate("<Return>", when="now")
            self.root.update()
            self.assertEqual(self.view.intervention_search_var.get(), "INT-90")
            int_popdown = self.root.tk.call("ttk::combobox::PopdownWindow", self.view.intervention_combo)
            int_items = self.root.tk.call(f"{int_popdown}.f.l", "get", 0, "end")
            self.assertIn("INTERVENȚIE TEST V3 · INT-901", int_items)
            self.assertIn("Altă intervenție 90 · INT-902", int_items)

            try:
                self.root.tk.call("ttk::combobox::Unpost", self.view.intervention_combo)
            except tk.TclError:
                pass
            self.view.intervention_search_entry.focus_set()
            self.view.intervention_search_entry.insert("end", "1")
            self.view.intervention_search_entry.event_generate("<KeyRelease>", when="now")
            self.root.update()
            self.assertEqual(
                tuple(self.view.intervention_combo.cget("values")),
                ("INTERVENȚIE TEST V3 · INT-901",),
            )
            self.view.intervention_search_entry.event_generate("<Return>", when="now")
            self.root.update()
            self.assertEqual(self.view.selected_intervention_id, int_target_id)
            self.assertNotEqual(self.view.selected_intervention_id, int_second_id)
            self.assertEqual(self.view.intervention_combo.get(), "INTERVENȚIE TEST V3 · INT-901")
            self.assertEqual(self.view.intervention_code_var.get(), "Cod: INT-901")
            self.assertEqual(self.view.intervention_code_label.winfo_manager(), "grid")
            self.assertIsNotNone(self.view.generation_form)
            self.assertEqual(str(self.view.generate_button.cget("state")), "normal")

        self._run_in_mainloop(exercise)


class GenerationViewStep3ScrollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "step3-scroll.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.addCleanup(self.connection.close)

        self.root = tk.Tk()
        self.root.geometry("1120x1040+40+20")
        self.root.update_idletasks()
        self.view = GenerationView(self.root, connection=self.connection)
        self.view.pack(fill="both", expand=True)

    def tearDown(self) -> None:
        if self.root.winfo_exists():
            self.root.destroy()
        self.connection.close()

    def _run_in_mainloop(self, callback: object) -> None:
        errors: list[BaseException] = []

        def _execute() -> None:
            try:
                callback()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if self.root.winfo_exists():
                    self.root.quit()

        self.root.deiconify()
        self.root.update()
        self.root.after(20, _execute)
        self.root.mainloop()
        if errors:
            raise errors[0]

    def _create_context(
        self,
        *,
        diag_name: str,
        int_name: str,
        field_specs: list[tuple[str, str, str, list[str] | None]],
    ) -> tuple[int, int]:
        diag_id = add_diagnosis(self.connection, diag_name)
        int_id = add_intervention(self.connection, diag_id, int_name)
        tokens: list[str] = []
        for key, label, data_type, options in field_specs:
            existing = self.connection.execute(
                "SELECT 1 FROM field_definitions WHERE variable_key = ?",
                (key,),
            ).fetchone()
            if existing is None:
                field_id = create_variable(
                    self.connection,
                    key,
                    label,
                    data_type,
                    "common",
                )
                if data_type == "select" and options:
                    add_field_options(
                        self.connection,
                        field_id,
                        [(opt, opt, idx) for idx, opt in enumerate(options, start=1)],
                    )
            tokens.append(f"{label}: {{{{{key}}}}}")
        epi = import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=diag_id,
            document_bytes=_docx("\n".join(tokens)),
        )
        activate_independent_template(
            self.connection,
            int(epi["id"]),
            patient_identification_review_confirmed=True,
        )
        proto = import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=int_id,
            document_bytes=_docx("Protocol standard."),
        )
        activate_independent_template(
            self.connection,
            int(proto["id"]),
            patient_identification_review_confirmed=True,
        )
        return diag_id, int_id

    def _select_pair(self, diag_index: int, int_index: int = 0) -> None:
        self.view.diagnosis_combo.current(diag_index)
        self.view.diagnosis_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.view.intervention_combo.current(int_index)
        self.view.intervention_combo.event_generate("<<ComboboxSelected>>")
        self.root.update()

    def test_step3_scroll_behaves_correctly_for_short_and_long_forms(self) -> None:
        self._create_context(
            diag_name="Diagnostic Scurt",
            int_name="Intervenție Scurtă",
            field_specs=[
                ("camp_scurt_unu", "Câmp scurt 1", "text", None),
            ],
        )
        many_fields: list[tuple[str, str, str, list[str] | None]] = [
            ("observatii_pacient", "Observații pacient", "text", None),
            ("data_operatiei", "Data operației", "date", None),
            ("lateralitate_op", "Lateralitate", "select", ["Stânga", "Dreapta"]),
        ]
        for idx in range(1, 14):
            many_fields.append((f"camp_lung_{idx}", f"Câmp suplimentar {idx}", "text", None))
        self._create_context(
            diag_name="Diagnostic Lung",
            int_name="Intervenție Lungă",
            field_specs=many_fields,
        )
        self.view.refresh()

        def exercise() -> None:
            diag_values = tuple(self.view.diagnosis_combo.cget("values"))
            short_idx = diag_values.index("Diagnostic Scurt")
            long_idx = diag_values.index("Diagnostic Lung")

            # 1. Formular cu puține câmpuri: scroll inutil absent, conținutul rămâne sus.
            self._select_pair(short_idx)
            assert self.view.form_canvas is not None
            assert self.view.form_scrollbar is not None
            assert self.view.fields_container is not None
            self.assertEqual(self.view.form_scrollbar.winfo_manager(), "")
            self.assertEqual(self.view.form_canvas.yview(), (0.0, 1.0))
            short_entry = self.view.field_controls["camp_scurt_unu"]["widget"]
            assert isinstance(short_entry, ttk.Entry)
            short_entry.event_generate("<MouseWheel>", delta=-120, when="now")
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview(), (0.0, 1.0))
            self.assertEqual(self.view.generate_button.winfo_manager(), "grid")

            # 2. Formular cu multe câmpuri: bara de scroll pe dreapta este activă și butonul Generează rămâne vizibil.
            self._select_pair(long_idx)
            self.assertEqual(self.view.form_scrollbar.winfo_manager(), "grid")
            self.assertEqual(int(self.view.form_scrollbar.grid_info()["column"]), 1)
            first, last = self.view.form_canvas.yview()
            self.assertEqual(first, 0.0)
            self.assertLess(last, 1.0)
            self.assertEqual(self.view.generate_button.winfo_manager(), "grid")
            self.assertGreater(self.view.generate_button.winfo_height(), 0)

            # Completăm valori înainte de scroll pentru a verifica păstrarea lor după scroll.
            obs_entry = self.view.field_controls["observatii_pacient"]["widget"]
            assert isinstance(obs_entry, ttk.Entry)
            obs_entry.insert(0, "Pacient Scroll Test")
            select_combo = self.view.field_controls["lateralitate_op"]["widget"]
            assert isinstance(select_combo, ttk.Combobox)
            select_combo.current(2)  # index 0 = "", index 1 = "Stânga", index 2 = "Dreapta"
            select_combo.event_generate("<<ComboboxSelected>>")
            self.root.update()

            # 3. Rotița mouse-ului și delta mic (touchpad) peste Entry, Label, Combobox și DateEntry deplasează formularul.
            obs_entry.event_generate("<MouseWheel>", delta=-120, when="now")
            self.root.update()
            after_entry_first = self.view.form_canvas.yview()[0]
            self.assertGreater(after_entry_first, 0.0)

            # Touchpad delta mic (-30) peste un Label din formular deplasează suplimentar formularul.
            first_label = next(
                w for w in self.view.fields_container.winfo_children() if isinstance(w, ttk.Label)
            )
            first_label.event_generate("<MouseWheel>", delta=-30, when="now")
            self.root.update()
            after_touchpad_first = self.view.form_canvas.yview()[0]
            self.assertGreater(after_touchpad_first, after_entry_first)

            # Scroll peste Combobox închis deplasează formularul și NU modifică selecția Combobox-ului.
            select_combo.event_generate("<MouseWheel>", delta=-120, when="now")
            self.root.update()
            after_combo_first = self.view.form_canvas.yview()[0]
            self.assertGreater(after_combo_first, after_touchpad_first)
            self.assertEqual(select_combo.get(), "Dreapta")

            # Scroll peste DateEntry (câmp) deplasează formularul în sus fără a afecta widgetul.
            date_widget = self.view.field_controls["data_operatiei"]["widget"]
            date_widget.entry.event_generate("<MouseWheel>", delta=120, when="now")
            self.root.update()
            after_date_first = self.view.form_canvas.yview()[0]
            self.assertLess(after_date_first, after_combo_first)

            # 4. Dropdown-ul Combobox deschis NU este interceptat de scroll-ul formularului.
            before_popdown_first = self.view.form_canvas.yview()[0]
            self.root.tk.call("ttk::combobox::Post", select_combo)
            self.root.update()
            select_combo.event_generate("<MouseWheel>", delta=-120, when="now")
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], before_popdown_first)
            try:
                self.root.tk.call("ttk::combobox::Unpost", select_combo)
            except tk.TclError:
                pass
            self.root.update()

            # 5. Calendarul tk-date-entry funcționează normal și selectează data.
            date_widget.drop_down()
            self.root.update()
            self.assertIsNotNone(date_widget._calendar_window)
            date_widget._select_day(date(2026, 11, 17))
            self.root.update()
            self.assertEqual(date_widget.get(), "17.11.2026")

            # Valorile completate nu s-au pierdut după scroll.
            self.assertEqual(obs_entry.get(), "Pacient Scroll Test")
            self.assertEqual(self.view.field_values["observatii_pacient"], "Pacient Scroll Test")
            self.assertEqual(self.view.field_values["data_operatiei"], "17.11.2026")
            self.assertEqual(self.view.field_values["lateralitate_op"], "Dreapta")

            # 6. Scroll-ul pe controale din afara Pasului 3 (Pas 1 / Pas 2) NU deplasează formularul Pasului 3.
            before_outside_first = self.view.form_canvas.yview()[0]
            self.view.diagnosis_search_entry.event_generate("<MouseWheel>", delta=-120, when="now")
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], before_outside_first)
            self.assertNotIn(self.view._scroll_bindtag, self.view.diagnosis_search_entry.bindtags())

            # 7. Schimbarea diagnosticului reconstruiește formularul fără a acumula binding-uri și resetează scroll-ul.
            self._select_pair(short_idx)
            self.assertEqual(self.view.form_scrollbar.winfo_manager(), "")
            self.assertEqual(self.view.form_canvas.yview(), (0.0, 1.0))

            self._select_pair(long_idx)
            self.assertEqual(self.view.form_scrollbar.winfo_manager(), "grid")
            self.assertEqual(self.view.form_canvas.yview()[0], 0.0)
            rebuilt_entry = self.view.field_controls["observatii_pacient"]["widget"]
            assert isinstance(rebuilt_entry, ttk.Entry)
            tag_count = rebuilt_entry.bindtags().count(self.view._scroll_bindtag)
            self.assertEqual(tag_count, 1)

            # Focus, selecție / Ctrl+A comportament rămân funcționale normal.
            rebuilt_entry.focus_set()
            rebuilt_entry.insert(0, "TestFocus")
            rebuilt_entry.selection_range(0, "end")
            self.assertEqual(rebuilt_entry.selection_get(), "TestFocus")

        self._run_in_mainloop(exercise)

    @staticmethod
    def _pack_touchpad_delta(delta_x: int, delta_y: int) -> int:
        return ((delta_x & 0xFFFF) << 16) | (delta_y & 0xFFFF)

    def _create_long_form_context(self) -> int:
        many_fields: list[tuple[str, str, str, list[str] | None]] = [
            ("observatii_pacient", "Observații pacient", "text", None),
            ("data_operatiei", "Data operației", "date", None),
            ("lateralitate_op", "Lateralitate", "select", ["Stânga", "Dreapta"]),
        ]
        for idx in range(1, 15):
            many_fields.append((f"camp_lung_{idx}", f"Câmp suplimentar {idx}", "text", None))
        self._create_context(
            diag_name="Diagnostic Touchpad Lung",
            int_name="Intervenție Touchpad Lungă",
            field_specs=many_fields,
        )
        self.view.refresh()
        return tuple(self.view.diagnosis_combo.cget("values")).index("Diagnostic Touchpad Lung")

    def test_touchpad_delta_unpacking_matches_signed_16bit_layout(self) -> None:
        self.assertEqual(unpack_touchpad_scroll_deltas(0), (0, 0))
        self.assertEqual(unpack_touchpad_scroll_deltas(65506), (0, -30))
        self.assertEqual(unpack_touchpad_scroll_deltas(65535), (0, -1))
        self.assertEqual(unpack_touchpad_scroll_deltas(30), (0, 30))
        self.assertEqual(unpack_touchpad_scroll_deltas(self._pack_touchpad_delta(-15, -2)), (-15, -2))
        self.assertEqual(unpack_touchpad_scroll_deltas(self._pack_touchpad_delta(12, 15)), (12, 15))

    def test_step3_touchpad_scroll_small_deltas_accumulation_and_isolation(self) -> None:
        long_idx = self._create_long_form_context()

        def exercise() -> None:
            self._select_pair(long_idx)
            assert self.view.form_canvas is not None
            assert self.view.fields_container is not None
            obs_entry = self.view.field_controls["observatii_pacient"]["widget"]
            select_combo = self.view.field_controls["lateralitate_op"]["widget"]
            date_widget = self.view.field_controls["data_operatiei"]["widget"]
            assert isinstance(obs_entry, ttk.Entry)
            assert isinstance(select_combo, ttk.Combobox)
            assert isinstance(date_widget, DateEntry)

            # 1. Acumulare pentru delta foarte mici negative: -1 (<TouchpadScroll>) produce mișcare după 3 evenimente (3 * 0.4 = 1.2 px).
            self.assertEqual(self.view.form_canvas.yview()[0], 0.0)
            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -1),
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], 0.0)
            self.assertAlmostEqual(self.view._wheel_delta_remainder, 0.4, places=6)

            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -1),
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], 0.0)
            self.assertAlmostEqual(self.view._wheel_delta_remainder, 0.8, places=6)

            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -1),
                when="now",
            )
            self.root.update()
            after_three_minus_1 = self.view.form_canvas.yview()[0]
            self.assertGreater(after_three_minus_1, 0.0)
            self.assertAlmostEqual(self.view._wheel_delta_remainder, 0.2, places=6)

            # 2. Schimbarea direcției (+1) resetează restul pozitiv rămas și acumulează în sens invers.
            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, 1),
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], after_three_minus_1)
            self.assertAlmostEqual(self.view._wheel_delta_remainder, -0.4, places=6)

            # 3. Delta mici ±2, ±15 și ±30 funcționează atât pe <TouchpadScroll>, cât și pe <MouseWheel>.
            for _ in range(2):
                obs_entry.event_generate(
                    "<TouchpadScroll>",
                    delta=self._pack_touchpad_delta(0, -2),
                    when="now",
                )
            self.root.update()
            after_minus_2 = self.view.form_canvas.yview()[0]
            self.assertGreater(after_minus_2, after_three_minus_1)

            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -15),
                when="now",
            )
            self.root.update()
            after_minus_15 = self.view.form_canvas.yview()[0]
            self.assertGreater(after_minus_15, after_minus_2)

            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(-4, -30),
                when="now",
            )
            self.root.update()
            after_minus_30 = self.view.form_canvas.yview()[0]
            self.assertGreater(after_minus_30, after_minus_15)

            # Delta pozitive (+30, +15, +2 x2, +1 x3) deplasează înapoi în sus.
            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, 30),
                when="now",
            )
            self.root.update()
            after_plus_30 = self.view.form_canvas.yview()[0]
            self.assertLess(after_plus_30, after_minus_30)

            obs_entry.event_generate("<MouseWheel>", delta=15, when="now")
            self.root.update()
            after_plus_15 = self.view.form_canvas.yview()[0]
            self.assertLess(after_plus_15, after_plus_30)

            obs_entry.event_generate("<MouseWheel>", delta=-30, when="now")
            self.root.update()
            after_mw_minus_30 = self.view.form_canvas.yview()[0]
            self.assertGreater(after_mw_minus_30, after_plus_15)

            # 4. Gest strict orizontal (delta_x != 0, delta_y == 0) și delta zero nu mișcă scroll-ul vertical.
            before_horiz = self.view.form_canvas.yview()[0]
            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(-25, 0),
                when="now",
            )
            obs_entry.event_generate("<TouchpadScroll>", delta=0, when="now")
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], before_horiz)

            # 5. Combobox închis peste care se face <TouchpadScroll> derulează Pasul 3 fără să schimbe selecția.
            select_combo.current(1)
            select_combo.event_generate("<<ComboboxSelected>>")
            self.root.update()
            select_combo.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -30),
                when="now",
            )
            self.root.update()
            self.assertGreater(self.view.form_canvas.yview()[0], before_horiz)
            self.assertEqual(select_combo.get(), "Stânga")

            # 6. Dropdown Combobox deschis și calendar DateEntry deschis cu cursorul deasupra nu deplasează Pasul 3.
            frozen_first = self.view.form_canvas.yview()[0]
            self.root.tk.call("ttk::combobox::Post", select_combo)
            self.root.update()
            select_combo.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -30),
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], frozen_first)
            self.root.tk.call("ttk::combobox::Unpost", select_combo)
            self.root.update()

            date_widget.drop_down()
            self.root.update()
            cal = date_widget._calendar_window
            assert cal is not None
            date_widget.entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -30),
                x=0,
                y=0,
                rootx=cal.winfo_rootx() + 10,
                rooty=cal.winfo_rooty() + 10,
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], frozen_first)
            date_widget._select_day(date(2026, 11, 17))
            self.root.update()

            # 7. Evenimentele din afara Pasului 3 și reconstrucția formularului izolează restul delta.
            self.view.diagnosis_search_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -30),
                when="now",
            )
            self.root.update()
            self.assertEqual(self.view.form_canvas.yview()[0], frozen_first)

            obs_entry.event_generate(
                "<TouchpadScroll>",
                delta=self._pack_touchpad_delta(0, -1),
                when="now",
            )
            self.root.update()
            self.assertNotEqual(self.view._wheel_delta_remainder, 0.0)
            self._select_pair(long_idx)
            self.assertEqual(self.view._wheel_delta_remainder, 0.0)

        self._run_in_mainloop(exercise)

    def test_step3_scroll_diagnostic_disabled_by_default_and_logs_without_form_data(self) -> None:
        self.assertIsNone(self.view._scroll_diag)

        diag_file = Path(self.temp_dir.name) / "scroll-diag.jsonl"
        with patch.dict(os.environ, {SCROLL_DIAG_ENV_VAR: str(diag_file)}, clear=False):
            diag_view = GenerationView(self.root, connection=self.connection)
            diag_view.pack(fill="both", expand=True)
            try:
                many_fields: list[tuple[str, str, str, list[str] | None]] = [
                    ("observatii_pacient", "Observații pacient", "text", None),
                ]
                for idx in range(1, 15):
                    many_fields.append((f"camp_diag_{idx}", f"Câmp diag {idx}", "text", None))
                self._create_context(
                    diag_name="Diag Secret Pacient",
                    int_name="Interventie Diag",
                    field_specs=many_fields,
                )
                diag_view.refresh()

                def exercise_diag() -> None:
                    idx = tuple(diag_view.diagnosis_combo.cget("values")).index("Diag Secret Pacient")
                    diag_view.diagnosis_combo.current(idx)
                    diag_view.diagnosis_combo.event_generate("<<ComboboxSelected>>")
                    self.root.update()
                    diag_view.intervention_combo.current(0)
                    diag_view.intervention_combo.event_generate("<<ComboboxSelected>>")
                    self.root.update()

                    secret_entry = diag_view.field_controls["observatii_pacient"]["widget"]
                    assert isinstance(secret_entry, ttk.Entry)
                    secret_entry.insert(0, "CNP_SENSIBIL_NU_LOGA")
                    secret_entry.event_generate(
                        "<TouchpadScroll>",
                        delta=self._pack_touchpad_delta(0, -30),
                        when="now",
                    )
                    secret_entry.event_generate("<TouchpadScroll>", delta=0, when="now")
                    self.root.update()

                self._run_in_mainloop(exercise_diag)
            finally:
                diag_view.destroy()

        raw_log = diag_file.read_text(encoding="utf-8")
        self.assertNotIn("CNP_SENSIBIL_NU_LOGA", raw_log)
        self.assertNotIn("Diag Secret Pacient", raw_log)
        entries = [json.loads(line) for line in raw_log.splitlines() if line.strip()]
        step3_entries = [item for item in entries if item.get("stage") == "step3_handler"]
        self.assertGreaterEqual(len(step3_entries), 2)
        handled = step3_entries[0]
        self.assertEqual(handled["event_name"], "<TouchpadScroll>")
        self.assertEqual(handled["delta_y"], -30)
        self.assertTrue(handled["handler_called"])
        self.assertIsNone(handled["ignored_reason"])
        self.assertIsNotNone(handled["yview_before"])
        self.assertGreater(handled["yview_after"][0], handled["yview_before"][0])
        ignored = step3_entries[1]
        self.assertEqual(ignored["ignored_reason"], "zero_delta")


if __name__ == "__main__":
    unittest.main()
