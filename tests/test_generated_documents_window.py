from __future__ import annotations

import re
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

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import GeneratedSession
from ortodocs.models.rich_document import Paragraph, RichDocument, TextInline
from ortodocs.services.document_generation import generate_documents
from ortodocs.services.medical_library import add_diagnosis, add_intervention
from ortodocs.services.template_sets import (
    activate_independent_template,
    import_independent_template,
)
from ortodocs.storage.database import connect_database
from ortodocs.storage.generation_repositories import get_generation_session
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.generated_documents_window import (
    DOCUMENT_TYPES,
    GeneratedDocumentsWindow,
)
from ortodocs.ui.windows_clipboard import WindowsClipboardError


def _docx(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _document(text: str) -> RichDocument:
    return RichDocument([Paragraph([TextInline(text)])])


def _session() -> GeneratedSession:
    return GeneratedSession(7, _document("Epicriză"), _document("Protocol"))


class _FakeEditor:
    def __init__(self, text: str, *, dirty: bool = False) -> None:
        self.document = _document(text)
        self.is_dirty = dirty
        self.mark_saved_calls = 0

    def get_document(self) -> RichDocument:
        return self.document

    def mark_saved(self) -> None:
        self.mark_saved_calls += 1
        self.is_dirty = False


class _FakeVariable:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _FakeButton:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)


class _FakeNotebook:
    def __init__(self, index: int = 0) -> None:
        self.active_index = index

    def select(self) -> str:
        return "selected-tab"

    def index(self, _tab: str) -> int:
        return self.active_index


class _FakeWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def winfo_id(self) -> int:
        return 1234

    def destroy(self) -> None:
        self.destroyed = True


def _logic_window() -> GeneratedDocumentsWindow:
    window = GeneratedDocumentsWindow.__new__(GeneratedDocumentsWindow)
    window.connection = sqlite3.connect(":memory:", autocommit=True)
    window.session_id = 7
    window._on_closed = None
    window._closed = False
    window._document_types = DOCUMENT_TYPES
    window.editors = {
        DOCUMENT_TYPES[0]: _FakeEditor("Epicriză", dirty=True),
        DOCUMENT_TYPES[1]: _FakeEditor("Protocol", dirty=True),
    }
    window.window = _FakeWindow()
    window.notebook = _FakeNotebook()
    window.dirty_var = _FakeVariable()
    window.status_var = _FakeVariable()
    window.save_button = _FakeButton()
    return window


class GeneratedDocumentsWindowLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = _logic_window()
        self.addCleanup(self.window.connection.close)

    def test_save_persists_each_dirty_document_separately_with_canonical_timestamp(self) -> None:
        with patch(
            "ortodocs.ui.generated_documents_window.update_generated_document"
        ) as update:
            self.assertTrue(self.window.save_dirty_documents())

        self.assertEqual(update.call_count, 2)
        self.assertEqual(
            [call.args[2] for call in update.call_args_list],
            list(DOCUMENT_TYPES),
        )
        for call in update.call_args_list:
            self.assertIs(call.args[0], self.window.connection)
            self.assertEqual(call.args[1], 7)
            self.assertRegex(call.args[4], re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"))
        self.assertTrue(all(not editor.is_dirty for editor in self.window.editors.values()))
        self.assertEqual([editor.mark_saved_calls for editor in self.window.editors.values()], [1, 1])

    def test_second_save_failure_keeps_its_content_and_dirty_state(self) -> None:
        first, second = (self.window.editors[key] for key in DOCUMENT_TYPES)
        original_second_document = second.document

        def save_one_then_fail(*args: object) -> None:
            if args[2] == DOCUMENT_TYPES[1]:
                raise sqlite3.OperationalError("disk busy")

        with (
            patch(
                "ortodocs.ui.generated_documents_window.update_generated_document",
                side_effect=save_one_then_fail,
            ) as update,
            patch("ortodocs.ui.generated_documents_window.messagebox.showerror") as show_error,
        ):
            self.assertFalse(self.window.save_dirty_documents())

        self.assertEqual(update.call_count, 2)
        self.assertFalse(first.is_dirty)
        self.assertTrue(second.is_dirty)
        self.assertEqual(first.mark_saved_calls, 1)
        self.assertEqual(second.mark_saved_calls, 0)
        self.assertIs(second.document, original_second_document)
        show_error.assert_called_once()

    def test_ctrl_s_uses_the_same_save_callback(self) -> None:
        with patch.object(self.window, "save_dirty_documents", return_value=True) as save:
            self.assertEqual(self.window._on_save_shortcut(), "break")
        save.assert_called_once_with()

    def test_copy_uses_active_document_and_window_handle_without_changing_dirty_state(self) -> None:
        self.window.notebook.active_index = 1
        with patch(
            "ortodocs.ui.generated_documents_window.copy_rich_document_to_clipboard"
        ) as copy_document:
            self.assertTrue(self.window.copy_active_document())

        copy_document.assert_called_once_with(
            self.window.editors[DOCUMENT_TYPES[1]].document,
            1234,
        )
        self.assertTrue(self.window.editors[DOCUMENT_TYPES[0]].is_dirty)
        self.assertTrue(self.window.editors[DOCUMENT_TYPES[1]].is_dirty)

    def test_clipboard_error_does_not_change_dirty_state(self) -> None:
        with (
            patch(
                "ortodocs.ui.generated_documents_window.copy_rich_document_to_clipboard",
                side_effect=WindowsClipboardError("clipboard unavailable"),
            ),
            patch("ortodocs.ui.generated_documents_window.messagebox.showerror") as show_error,
        ):
            self.assertFalse(self.window.copy_active_document())

        self.assertTrue(self.window.has_dirty_documents)
        show_error.assert_called_once()
        self.assertIn("clipboard unavailable", self.window.status_var.value)

    def test_close_cancel_keeps_window_and_discard_closes_without_saving(self) -> None:
        with patch.object(self.window, "_ask_close_action", return_value="cancel"):
            self.assertFalse(self.window.close())
        self.assertFalse(self.window.window.destroyed)

        with (
            patch.object(self.window, "_ask_close_action", return_value="discard"),
            patch("ortodocs.ui.generated_documents_window.update_generated_document") as update,
        ):
            self.assertTrue(self.window.close())
        update.assert_not_called()
        self.assertTrue(self.window.window.destroyed)

    def test_close_save_only_closes_after_all_documents_save(self) -> None:
        with (
            patch.object(self.window, "_ask_close_action", return_value="save"),
            patch(
                "ortodocs.ui.generated_documents_window.update_generated_document",
                side_effect=sqlite3.OperationalError("disk busy"),
            ),
            patch("ortodocs.ui.generated_documents_window.messagebox.showerror"),
        ):
            self.assertFalse(self.window.close())
        self.assertFalse(self.window.window.destroyed)
        self.assertTrue(self.window.has_dirty_documents)

    def test_close_save_closes_only_after_successful_saves(self) -> None:
        with (
            patch.object(self.window, "_ask_close_action", return_value="save"),
            patch("ortodocs.ui.generated_documents_window.update_generated_document") as update,
        ):
            self.assertTrue(self.window.close())

        self.assertEqual(update.call_count, 2)
        self.assertTrue(self.window.window.destroyed)


class GeneratedDocumentsWindowWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu are un runtime Tcl/Tk disponibil: {exc}")

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.connection = sqlite3.connect(":memory:", autocommit=True)
        self.window = GeneratedDocumentsWindow(self.root, self.connection, _session())
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        finally:
            self.connection.close()

    def test_both_editors_remain_mounted_and_dirty_independently_across_tabs(self) -> None:
        self.assertEqual(len(self.window.notebook.tabs()), 2)
        self.assertEqual(set(self.window.editors), set(DOCUMENT_TYPES))
        first = self.window.editors[DOCUMENT_TYPES[0]]
        second = self.window.editors[DOCUMENT_TYPES[1]]
        first.text_widget.insert("end-1c", " editat")
        self.assertTrue(first.is_dirty)
        self.window.notebook.select(self.window.notebook.tabs()[1])
        self.root.update()
        self.assertTrue(first.is_dirty)
        self.assertFalse(second.is_dirty)
        second.text_widget.insert("end-1c", " modificat")
        self.window.notebook.select(self.window.notebook.tabs()[0])
        self.root.update()
        self.assertTrue(first.is_dirty)
        self.assertTrue(second.is_dirty)
        self.assertEqual(len(self.window.notebook.tabs()), 2)

    def test_copy_targets_current_tab(self) -> None:
        self.window.notebook.select(self.window.notebook.tabs()[1])
        self.root.update()
        with patch(
            "ortodocs.ui.generated_documents_window.copy_rich_document_to_clipboard"
        ) as copy_document:
            self.assertTrue(self.window.copy_active_document())
        copied_document, owner = copy_document.call_args.args
        self.assertEqual(copied_document.to_json(), _document("Protocol").to_json())
        self.assertEqual(owner, int(self.window.window.winfo_id()))

    def test_initially_hidden_window_stays_withdrawn_until_shown(self) -> None:
        hidden_window = GeneratedDocumentsWindow(
            self.root,
            self.connection,
            _session(),
            initially_hidden=True,
        )
        self.addCleanup(hidden_window.close_after_replacement)
        self.root.update_idletasks()

        self.assertEqual(hidden_window.window.state(), "withdrawn")

        hidden_window.show()
        self.root.update_idletasks()
        self.assertEqual(hidden_window.window.state(), "normal")

    def test_constructor_failure_destroys_partial_hidden_toplevel(self) -> None:
        existing_children = set(self.root.winfo_children())
        with (
            patch(
                "ortodocs.ui.generated_documents_window.RichTextEditor",
                side_effect=RuntimeError("editor construction failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "editor construction failed"),
        ):
            GeneratedDocumentsWindow(
                self.root,
                self.connection,
                _session(),
                initially_hidden=True,
            )

        self.root.update_idletasks()
        self.assertEqual(set(self.root.winfo_children()), existing_children)

    def test_opening_and_editing_both_documents_without_selection_in_mainloop(self) -> None:
        recorded_error: list[BaseException] = []
        previous_reporter = self.root.report_callback_exception

        def _capture_exception(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: object,
        ) -> None:
            recorded_error.append(exc_value)
            self.root.quit()

        def exercise() -> None:
            try:
                generated_window = GeneratedDocumentsWindow(
                    self.root,
                    self.connection,
                    _session(),
                    initially_hidden=True,
                )
                self.addCleanup(generated_window.close_after_replacement)
                generated_window.show()
                self.root.update()

                first = generated_window.editors[DOCUMENT_TYPES[0]]
                second = generated_window.editors[DOCUMENT_TYPES[1]]
                self.assertEqual(first.text_widget.tag_ranges("sel"), ())
                self.assertEqual(second.text_widget.tag_ranges("sel"), ())

                first.text_widget.focus_force()
                self.root.update()
                first.bold_button.invoke()
                first.text_widget.event_generate("<KeyPress-A>", when="now")
                first.text_widget.event_generate("<<Copy>>", when="now")

                generated_window.notebook.select(generated_window.notebook.tabs()[1])
                self.root.update()
                second.text_widget.focus_force()
                self.root.update()
                second.italic_button.invoke()
                second.text_widget.event_generate("<KeyPress-B>", when="now")

                with patch("ortodocs.ui.generated_documents_window.update_generated_document") as update:
                    self.assertTrue(generated_window.save_dirty_documents())
                self.assertEqual(update.call_count, 2)
                self.assertTrue(generated_window.window.winfo_exists())
            except BaseException as exc:
                recorded_error.append(exc)
            finally:
                self.root.after_idle(self.root.quit)

        self.root.report_callback_exception = _capture_exception  # type: ignore[method-assign]
        try:
            self.root.after_idle(exercise)
            self.root.mainloop()
        finally:
            self.root.report_callback_exception = previous_reporter  # type: ignore[method-assign]
        if recorded_error:
            raise recorded_error[0]

    def test_keyboard_shortcuts_format_save_reopen_and_isolate_both_editors_in_mainloop(
        self,
    ) -> None:
        recorded_error: list[BaseException] = []
        previous_reporter = self.root.report_callback_exception

        def _capture_exception(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: object,
        ) -> None:
            recorded_error.append(exc_value)
            self.root.quit()

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "shortcuts-roundtrip.db"
            initialize_database(database_path)
            db_connection = connect_database(database_path)
            try:
                diagnosis_id = add_diagnosis(db_connection, "Fractură humerus")
                intervention_id = add_intervention(
                    db_connection,
                    diagnosis_id,
                    "Osteosinteză placă",
                )
                epi_draft = import_independent_template(
                    db_connection,
                    owner_type="diagnosis",
                    owner_id=diagnosis_id,
                    document_bytes=_docx("Epicriză pacient {{sex}}"),
                )
                activate_independent_template(
                    db_connection,
                    int(epi_draft["id"]),
                    patient_identification_review_confirmed=True,
                )
                proto_draft = import_independent_template(
                    db_connection,
                    owner_type="intervention",
                    owner_id=intervention_id,
                    document_bytes=_docx("Protocol {{lateralitate}}"),
                )
                activate_independent_template(
                    db_connection,
                    int(proto_draft["id"]),
                    patient_identification_review_confirmed=True,
                )
                session = generate_documents(
                    db_connection,
                    intervention_id,
                    {"sex": "Pacient", "lateralitate": "dreapta"},
                )

                def _trigger(widget: tk.Text, sequence: str) -> None:
                    widget.event_generate(sequence, when="now")
                    key = sequence.strip("<>").split("-")[-1]
                    widget.event_generate(f"<KeyRelease-{key}>", when="now")
                    widget.event_generate("<KeyRelease-Control_L>", when="now")

                def exercise() -> None:
                    external_entry: ttk.Entry | None = None
                    generated_window: GeneratedDocumentsWindow | None = None
                    reopened_window: GeneratedDocumentsWindow | None = None
                    try:
                        try:
                            self.root.attributes("-alpha", 0.0)
                        except tk.TclError:
                            pass
                        self.root.deiconify()
                        external_entry = ttk.Entry(self.root)
                        external_entry.insert(0, "extern")
                        external_entry.pack()

                        generated_window = GeneratedDocumentsWindow(
                            self.root,
                            db_connection,
                            session,
                            initially_hidden=True,
                        )
                        try:
                            generated_window.window.attributes("-alpha", 0.0)
                        except tk.TclError:
                            pass
                        generated_window.show()
                        self.root.update()

                        epi_editor = generated_window.editors[DOCUMENT_TYPES[0]]
                        proto_editor = generated_window.editors[DOCUMENT_TYPES[1]]

                        # Scurtăturile într-un câmp extern nu afectează editorii.
                        external_entry.focus_force()
                        self.root.update()
                        self.assertIs(self.root.focus_get(), external_entry)
                        for seq in ("<Control-b>", "<Control-i>", "<Control-u>"):
                            external_entry.event_generate(seq, when="now")
                        self.assertFalse(epi_editor.is_dirty)
                        self.assertFalse(proto_editor.is_dirty)
                        self.assertEqual(epi_editor._active_style, (False, False, False))

                        # Editor 1: formatare pe selecție (Ctrl+B, Ctrl+I) + fără selecție (Ctrl+U) + Undo/Redo.
                        epi_editor.text_widget.focus_force()
                        self.root.update()
                        epi_editor.text_widget.tag_add("sel", "1.0", "1.8")
                        epi_editor.text_widget.mark_set("insert", "1.8")
                        _trigger(epi_editor.text_widget, "<Control-b>")
                        _trigger(epi_editor.text_widget, "<Control-i>")
                        epi_editor.text_widget.tag_remove("sel", "1.0", "end")
                        epi_editor.text_widget.mark_set("insert", "end-1c")
                        epi_editor.text_widget.edit_separator()
                        _trigger(epi_editor.text_widget, "<Control-u>")
                        epi_editor.text_widget.event_generate("<KeyPress-Z>", when="now")
                        epi_editor.text_widget.event_generate("<KeyRelease-Z>", when="now")
                        epi_editor.text_widget.event_generate("<<Undo>>", when="now")
                        epi_editor.text_widget.event_generate("<<Redo>>", when="now")
                        self.assertNotIn("\t", epi_editor.text_widget.get("1.0", "end-1c"))

                        # Editor 2: formatare independentă pe celălalt tab.
                        generated_window.notebook.select(generated_window.notebook.tabs()[1])
                        self.root.update()
                        proto_editor.text_widget.focus_force()
                        self.root.update()
                        proto_editor.text_widget.tag_add("sel", "1.0", "1.8")
                        proto_editor.text_widget.mark_set("insert", "1.8")
                        _trigger(proto_editor.text_widget, "<Control-u>")
                        proto_editor.text_widget.tag_remove("sel", "1.0", "end")
                        proto_editor.text_widget.mark_set("insert", "end-1c")
                        _trigger(proto_editor.text_widget, "<Control-b>")
                        _trigger(proto_editor.text_widget, "<Control-i>")
                        proto_editor.text_widget.event_generate("<KeyPress-W>", when="now")
                        proto_editor.text_widget.event_generate("<KeyRelease-W>", when="now")
                        self.assertNotIn("\t", proto_editor.text_widget.get("1.0", "end-1c"))

                        expected_epi = epi_editor.get_document().to_dict()
                        expected_proto = proto_editor.get_document().to_dict()

                        self.assertTrue(generated_window.save_dirty_documents())
                        self.assertFalse(epi_editor.is_dirty)
                        self.assertFalse(proto_editor.is_dirty)
                        self.assertTrue(generated_window.close())

                        # Redeschidere din baza de date și verificare păstrare formatare.
                        reloaded_detail = get_generation_session(db_connection, session.session_id)
                        assert reloaded_detail is not None
                        reopened_session = GeneratedSession(
                            reloaded_detail.history_item.session_id,
                            reloaded_detail.epicrisis_recommendations,
                            reloaded_detail.operative_protocol,
                        )
                        reopened_window = GeneratedDocumentsWindow(
                            self.root,
                            db_connection,
                            reopened_session,
                            initially_hidden=True,
                        )
                        self.root.update_idletasks()

                        self.assertEqual(
                            reopened_window.editors[DOCUMENT_TYPES[0]].get_document().to_dict(),
                            expected_epi,
                        )
                        self.assertEqual(
                            reopened_window.editors[DOCUMENT_TYPES[1]].get_document().to_dict(),
                            expected_proto,
                        )
                        self.assertEqual(
                            expected_epi,
                            RichDocument(
                                [
                                    Paragraph(
                                        [
                                            TextInline("Epicriză", bold=True, italic=True),
                                            TextInline(" pacient Pacient"),
                                            TextInline("Z", underline=True),
                                        ]
                                    )
                                ]
                            ).to_dict(),
                        )
                        self.assertEqual(
                            expected_proto,
                            RichDocument(
                                [
                                    Paragraph(
                                        [
                                            TextInline("Protocol", underline=True),
                                            TextInline(" dreapta"),
                                            TextInline("W", bold=True, italic=True),
                                        ]
                                    )
                                ]
                            ).to_dict(),
                        )
                    except BaseException as exc:
                        recorded_error.append(exc)
                    finally:
                        if reopened_window is not None:
                            reopened_window.close_after_replacement()
                        if generated_window is not None:
                            generated_window.close_after_replacement()
                        if external_entry is not None:
                            try:
                                external_entry.destroy()
                            except tk.TclError:
                                pass
                        self.root.after_idle(self.root.quit)

                self.root.report_callback_exception = _capture_exception  # type: ignore[method-assign]
                try:
                    self.root.after_idle(exercise)
                    self.root.mainloop()
                finally:
                    self.root.report_callback_exception = previous_reporter  # type: ignore[method-assign]
                if recorded_error:
                    raise recorded_error[0]
            finally:
                db_connection.close()


if __name__ == "__main__":
    unittest.main()
