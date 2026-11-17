"""Teste cu widgeturi Tk reale pentru componenta RichTextEditor."""

from __future__ import annotations

import sys
import tkinter as tk
import unittest
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.ui.rich_text_editor import RichTextEditor


def document(*inlines: TextInline | LineBreakInline) -> RichDocument:
    return RichDocument(paragraphs=[Paragraph(list(inlines))])


class RichTextEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
            try:
                # Tk aplică transparența ferestrei de nivel superior pe Windows.
                # Unele servere Tk/platforme nu o acceptă; în acest caz lăsăm
                # fereastra vizibilă pentru a păstra widgeturile mapate.
                cls.root.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            cls.root.deiconify()
            cls.root.update()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk nu poate inițializa root-ul în acest mediu: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def setUp(self) -> None:
        self.editor = RichTextEditor(self.root, document(TextInline("text inițial")))
        self.editor.pack(fill="both", expand=True)
        self.root.deiconify()
        self.root.update()

    def tearDown(self) -> None:
        try:
            self.editor.destroy()
        except tk.TclError:
            pass

    def _select(self, start: str, end: str) -> None:
        text = self.editor.text_widget
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", start, end)
        text.mark_set("insert", end)

    def _prepare_keyboard_target(self) -> None:
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.root.winfo_ismapped(), "Fereastra root trebuie să fie mapată")
        self.assertTrue(
            self.editor.text_widget.winfo_ismapped(),
            "Widgetul Text trebuie să fie mapat înaintea evenimentului de tastatură",
        )
        self.editor.text_widget.focus_force()
        self.root.update()

    def _set_text(self, value: str, *, style: tuple[bool, bool, bool] = (False, False, False)) -> None:
        self.editor.set_document(
            document(
                TextInline(
                    value,
                    bold=style[0],
                    italic=style[1],
                    underline=style[2],
                )
            )
        )

    def _press_format_shortcut(self, sequence: str) -> None:
        self.editor.text_widget.event_generate(sequence, when="now")
        key = sequence.strip("<>").split("-")[-1]
        self.editor.text_widget.event_generate(f"<KeyRelease-{key}>", when="now")
        self.editor.text_widget.event_generate("<KeyRelease-Control_L>", when="now")

    def _assert_single_shortcut_with_and_without_selection(
        self,
        *,
        attribute: str,
        sequences: tuple[str, str],
        style_index: int,
    ) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            for sequence in sequences:
                with self.subTest(mode="selection", sequence=sequence):
                    self._set_text("abc def")
                    self.editor.text_widget.focus_force()
                    self.root.update()
                    self._select("1.1", "1.4")
                    self.editor.text_widget.mark_set("insert", "1.4")

                    self._press_format_shortcut(sequence)

                    self.assertEqual(
                        tuple(str(index) for index in self.editor.text_widget.tag_ranges("sel")),
                        ("1.1", "1.4"),
                    )
                    self.assertEqual(self.editor.text_widget.index("insert"), "1.4")
                    self.assertTrue(self.editor._style_variables[attribute].get())
                    self.assertTrue(self.editor.is_dirty)
                    self.assertNotIn("\t", self.editor.text_widget.get("1.0", "end-1c"))

                    expected_style = [False, False, False]
                    expected_style[style_index] = True
                    self.assertEqual(
                        self.editor.get_document().to_dict(),
                        document(
                            TextInline("a"),
                            TextInline(
                                "bc ",
                                bold=expected_style[0],
                                italic=expected_style[1],
                                underline=expected_style[2],
                            ),
                            TextInline("def"),
                        ).to_dict(),
                    )

            with self.subTest(mode="no_selection", sequence=sequences[0]):
                self._set_text("bază ")
                self.editor.text_widget.focus_force()
                self.root.update()
                self.editor.text_widget.mark_set("insert", "end-1c")

                self._press_format_shortcut(sequences[0])

                expected_style = tuple(index == style_index for index in range(3))
                self.assertEqual(self.editor._active_style, expected_style)
                self.assertTrue(self.editor._style_variables[attribute].get())
                self.assertFalse(self.editor.is_dirty)

                self.editor.text_widget.event_generate("<KeyPress-X>", when="now")
                self.editor.text_widget.event_generate("<KeyRelease-X>", when="now")
                self.assertEqual(self.editor._active_style, expected_style)
                self.assertTrue(self.editor._style_variables[attribute].get())
                self.assertNotIn("\t", self.editor.text_widget.get("1.0", "end-1c"))
                self.assertEqual(
                    self.editor.get_document().to_dict(),
                    document(
                        TextInline("bază "),
                        TextInline(
                            "X",
                            bold=expected_style[0],
                            italic=expected_style[1],
                            underline=expected_style[2],
                        ),
                    ).to_dict(),
                )

        self._run_in_mainloop(exercise)

    def test_loads_document_and_get_document_returns_rich_document(self) -> None:
        source = RichDocument(
            paragraphs=[
                Paragraph([TextInline("Epicriză", bold=True), LineBreakInline(), TextInline("continuare")]),
                Paragraph([]),
            ]
        )
        self.editor.set_document(source)

        result = self.editor.get_document()

        self.assertIsInstance(result, RichDocument)
        self.assertEqual(result.to_dict(), source.to_dict())
        self.assertIsNot(result, source)

    def test_editor_keeps_independent_copies_of_input_and_returned_documents(self) -> None:
        source = document(TextInline("original"))
        self.editor.set_document(source)
        source.paragraphs[0].inlines[0].text = "schimbat în exterior"  # type: ignore[union-attr]

        first_result = self.editor.get_document()
        first_result.paragraphs[0].inlines[0].text = "schimbat în rezultat"  # type: ignore[union-attr]

        self.assertEqual(
            self.editor.get_document().to_dict(),
            document(TextInline("original")).to_dict(),
        )

    def test_set_document_resets_cursor_selection_snapshot_and_dirty_state(self) -> None:
        self.editor.text_widget.insert("end-1c", " plus")
        self.assertTrue(self.editor.is_dirty)
        self._select("1.0", "1.2")

        replacement = document(TextInline("nou"))
        self.editor.set_document(replacement)

        self.assertEqual(self.editor.get_document().to_dict(), replacement.to_dict())
        self.assertFalse(self.editor.is_dirty)
        self.assertEqual(self.editor.text_widget.index("insert"), "1.0")
        self.assertEqual(self.editor.text_widget.tag_ranges("sel"), ())
        self.assertEqual(self.editor.dirty_indicator.winfo_manager(), "")

    def test_round_trips_all_eight_bold_italic_underline_combinations(self) -> None:
        styles = tuple(
            (bold, italic, underline)
            for bold in (False, True)
            for italic in (False, True)
            for underline in (False, True)
        )
        source = document(
            *(
                TextInline(
                    f"stil-{index}",
                    bold=style[0],
                    italic=style[1],
                    underline=style[2],
                )
                for index, style in enumerate(styles)
            )
        )
        self.editor.set_document(source)

        self.assertEqual(self.editor.get_document().to_dict(), source.to_dict())

    def test_each_format_button_applies_its_attribute_to_a_selection(self) -> None:
        buttons = (
            ("bold", self.editor.bold_button),
            ("italic", self.editor.italic_button),
            ("underline", self.editor.underline_button),
        )
        for attribute, button in buttons:
            with self.subTest(attribute=attribute):
                self._set_text("abc")
                self._select("1.1", "1.2")
                button.invoke()

                result = self.editor.get_document()
                formatted = result.paragraphs[0].inlines[1]
                self.assertIsInstance(formatted, TextInline)
                self.assertTrue(getattr(formatted, attribute))
                self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "abc")

    def test_removing_format_keeps_the_other_two_attributes(self) -> None:
        self._set_text("abc", style=(True, True, True))
        self._select("1.0", "1.3")

        self.editor.bold_button.invoke()

        result = self.editor.get_document()
        self.assertEqual(
            result.to_dict(),
            document(TextInline("abc", bold=False, italic=True, underline=True)).to_dict(),
        )

    def test_selection_formatting_changes_only_selected_text_and_preserves_neighbors(self) -> None:
        self._set_text("înainte după")
        self._select("1.8", "1.11")

        self.editor.underline_button.invoke()

        self.assertEqual(
            self.editor.get_document().to_dict(),
            document(
                TextInline("înainte "),
                TextInline("dup", underline=True),
                TextInline("ă"),
            ).to_dict(),
        )

    def test_selection_toggle_preserves_mixed_other_attributes_per_character(self) -> None:
        source = document(
            TextInline("a", italic=True),
            TextInline("b", italic=True, underline=True),
        )
        self.editor.set_document(source)
        self._select("1.0", "1.2")

        self.editor.bold_button.invoke()

        self.assertEqual(
            self.editor.get_document().to_dict(),
            document(
                TextInline("a", bold=True, italic=True),
                TextInline("b", bold=True, italic=True, underline=True),
            ).to_dict(),
        )

    def test_cursor_formatting_applies_all_style_combinations_to_unicode_input(self) -> None:
        styles = tuple(
            (bold, italic, underline)
            for bold in (False, True)
            for italic in (False, True)
            for underline in (False, True)
        )
        buttons = {
            "bold": self.editor.bold_button,
            "italic": self.editor.italic_button,
            "underline": self.editor.underline_button,
        }
        for style in styles:
            with self.subTest(style=style):
                self.editor.set_document(RichDocument(paragraphs=[Paragraph([])]))
                for enabled, attribute in zip(style, buttons):
                    if enabled:
                        buttons[attribute].invoke()
                self.editor.text_widget.insert("insert", "ăȘț")

                self.assertEqual(
                    self.editor.get_document().to_dict(),
                    document(
                        TextInline(
                            "ăȘț",
                            bold=style[0],
                            italic=style[1],
                            underline=style[2],
                        )
                    ).to_dict(),
                )

    def test_keyboard_text_input_uses_active_style(self) -> None:
        self.editor.set_document(RichDocument(paragraphs=[Paragraph([])]))
        self.editor.bold_button.invoke()
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<KeyPress-a>", when="now")

        self.assertEqual(
            self.editor.get_document().to_dict(),
            document(TextInline("a", bold=True)).to_dict(),
        )

    def test_enter_creates_a_paragraph_separator(self) -> None:
        self._set_text("ab")
        self.editor.text_widget.mark_set("insert", "1.1")
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<KeyPress-Return>", when="now")

        self.assertEqual(
            self.editor.get_document().to_dict(),
            RichDocument(paragraphs=[Paragraph([TextInline("a")]), Paragraph([TextInline("b")])]).to_dict(),
        )

    def test_shift_enter_creates_a_line_break_inline(self) -> None:
        self._set_text("ab")
        self.editor.text_widget.mark_set("insert", "1.1")
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<Shift-KeyPress-Return>", when="now")

        self.assertEqual(
            self.editor.get_document().to_dict(),
            document(TextInline("a"), LineBreakInline(), TextInline("b")).to_dict(),
        )

    def test_backspace_joins_paragraphs_without_an_unclassified_newline(self) -> None:
        self.editor.set_document(
            RichDocument(paragraphs=[Paragraph([TextInline("ab")]), Paragraph([TextInline("cd")])])
        )
        self.editor.text_widget.mark_set("insert", "2.0")
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<KeyPress-BackSpace>", when="now")

        self.assertEqual(self.editor.get_document().to_dict(), document(TextInline("abcd")).to_dict())

    def test_delete_joins_paragraphs_without_an_unclassified_newline(self) -> None:
        self.editor.set_document(
            RichDocument(paragraphs=[Paragraph([TextInline("ab")]), Paragraph([TextInline("cd")])])
        )
        self.editor.text_widget.mark_set("insert", "1.2")
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<KeyPress-Delete>", when="now")

        self.assertEqual(self.editor.get_document().to_dict(), document(TextInline("abcd")).to_dict())

    def test_replacing_selection_across_a_paragraph_boundary_stays_serializable(self) -> None:
        self.editor.set_document(
            RichDocument(paragraphs=[Paragraph([TextInline("ab")]), Paragraph([TextInline("cd")])])
        )
        self._select("1.1", "2.1")
        self._prepare_keyboard_target()
        self.editor.text_widget.event_generate("<KeyPress-x>", when="now")

        self.assertEqual(self.editor.get_document().to_dict(), document(TextInline("axd")).to_dict())

    def test_plain_inserted_newlines_become_paragraph_separators(self) -> None:
        self.editor.set_document(RichDocument(paragraphs=[Paragraph([])]))
        self.editor.text_widget.insert("insert", "unu\ndoi\r\ntrei")

        self.assertEqual(
            self.editor.get_document().to_dict(),
            RichDocument(
                paragraphs=[
                    Paragraph([TextInline("unu")]),
                    Paragraph([TextInline("doi")]),
                    Paragraph([TextInline("trei")]),
                ]
            ).to_dict(),
        )

    def test_text_modification_sets_dirty_indicator_and_callback(self) -> None:
        changes: list[bool] = []
        editor = RichTextEditor(
            self.root,
            document(TextInline("x")),
            on_dirty_changed=changes.append,
        )
        try:
            editor.text_widget.insert("end-1c", "y")

            self.assertTrue(editor.is_dirty)
            self.assertEqual(editor.dirty_indicator.winfo_manager(), "grid")
            self.assertEqual(changes, [True])
        finally:
            editor.destroy()

    def test_formatting_modification_sets_dirty(self) -> None:
        self._set_text("abc")
        self._select("1.0", "1.3")

        self.editor.italic_button.invoke()

        self.assertTrue(self.editor.is_dirty)
        self.assertEqual(self.editor.dirty_indicator.winfo_manager(), "grid")

    def test_returning_to_saved_text_and_formatting_restores_clean_state(self) -> None:
        self._set_text("abc")
        self.editor.text_widget.insert("end-1c", "x")
        self.assertTrue(self.editor.is_dirty)
        self.editor.text_widget.delete("1.3", "1.4")
        self.assertFalse(self.editor.is_dirty)

        self._select("1.0", "1.3")
        self.editor.bold_button.invoke()
        self.assertTrue(self.editor.is_dirty)

        self.editor.bold_button.invoke()

        self.assertFalse(self.editor.is_dirty)
        self.assertEqual(self.editor.dirty_indicator.winfo_manager(), "")

    def test_mark_saved_resets_dirty_and_notifies_callback(self) -> None:
        changes: list[bool] = []
        editor = RichTextEditor(
            self.root,
            document(TextInline("x")),
            on_dirty_changed=changes.append,
        )
        try:
            editor.text_widget.insert("end-1c", "y")
            editor.mark_saved()

            self.assertFalse(editor.is_dirty)
            self.assertEqual(editor.dirty_indicator.winfo_manager(), "")
            self.assertEqual(changes, [True, False])
        finally:
            editor.destroy()

    def test_set_document_clears_dirty_and_notifies_callback(self) -> None:
        changes: list[bool] = []
        editor = RichTextEditor(
            self.root,
            document(TextInline("x")),
            on_dirty_changed=changes.append,
        )
        try:
            editor.text_widget.insert("end-1c", "y")
            replacement = document(TextInline("înlocuit"))
            editor.set_document(replacement)

            self.assertFalse(editor.is_dirty)
            self.assertEqual(editor.get_document().to_dict(), replacement.to_dict())
            self.assertEqual(changes, [True, False])
        finally:
            editor.destroy()

    def _run_in_mainloop(self, callback: Callable[[], None]) -> None:
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
                callback()
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

    def test_commands_without_selection_work_in_mainloop(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            created = RichTextEditor(self.root, document(TextInline("Conținut inițial")))
            try:
                created.pack(fill="both", expand=True)
                self.root.update()
                created.text_widget.focus_force()
                self.root.update()
                created.set_document(document(TextInline("ab")))
                self.assertEqual(created.text_widget.tag_ranges("sel"), ())
                created.text_widget.event_generate("<Button-1>", x=4, y=4, when="now")
                created.text_widget.event_generate("<ButtonRelease-1>", x=4, y=4, when="now")
                self.assertEqual(created.text_widget.tag_ranges("sel"), ())

                created.text_widget.mark_set("insert", "1.1")
                created.text_widget.event_generate("<KeyPress-Right>", when="now")
                created.text_widget.event_generate("<KeyRelease-Right>", when="now")
                created.text_widget.mark_set("insert", "1.1")
                created.text_widget.event_generate("<KeyPress-Return>", when="now")
                created.text_widget.event_generate("<Shift-KeyPress-Return>", when="now")
                created.text_widget.event_generate("<KeyPress-BackSpace>", when="now")
                created.text_widget.event_generate("<KeyPress-Delete>", when="now")
                self.assertEqual(created.text_widget.tag_ranges("sel"), ())
                self.assertEqual(created.text_widget.get("1.0", "end-1c"), "a\n")
                self.assertTrue(created.is_dirty)

                # Dublu și triplu clic declanșate prin evenimente <Button-1> succesive
                # execută legăturile native text.tcl în mainloop fără erori Tcl.
                for _ in range(3):
                    created.text_widget.event_generate("<Button-1>", x=4, y=4, when="now")
                    created.text_widget.event_generate("<ButtonRelease-1>", x=4, y=4, when="now")
                self.assertEqual(
                    tuple(str(index) for index in created.text_widget.tag_ranges("sel")),
                    ("1.0", "2.0"),
                )
                created.set_document(RichDocument(paragraphs=[Paragraph([])]))
                self.assertEqual(created.text_widget.tag_ranges("sel"), ())
                created.bold_button.invoke()
                self.assertFalse(created.is_dirty)
            finally:
                created.destroy()

        self._run_in_mainloop(exercise)

    def test_commands_with_selection_work_in_mainloop(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("abcdef")
            self._select("1.1", "1.3")
            self.editor.text_widget.event_generate("<KeyPress-X>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "aXdef")

            self._select("1.2", "1.4")
            self.editor.text_widget.event_generate("<KeyPress-Return>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                RichDocument(
                    paragraphs=[
                        Paragraph([TextInline("aX")]),
                        Paragraph([TextInline("f")]),
                    ]
                ).to_dict(),
            )

            self._select("1.1", "2.0")
            self.editor.text_widget.event_generate("<KeyPress-Delete>", when="now")
            self.assertEqual(self.editor.get_document().to_dict(), document(TextInline("af")).to_dict())

        self._run_in_mainloop(exercise)

    def test_formatting_without_and_with_selection_in_mainloop(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("abc")
            self.assertEqual(self.editor.text_widget.tag_ranges("sel"), ())
            self.editor.text_widget.mark_set("insert", "end-1c")
            self.editor.bold_button.invoke()
            self.editor.italic_button.invoke()
            self.editor.text_widget.event_generate("<KeyPress-z>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("abc"),
                    TextInline("z", bold=True, italic=True),
                ).to_dict(),
            )

            self._select("1.0", "1.2")
            self.editor.underline_button.invoke()
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("ab", underline=True),
                    TextInline("c"),
                    TextInline("z", bold=True, italic=True),
                ).to_dict(),
            )
            self.assertTrue(self.editor.is_dirty)

        self._run_in_mainloop(exercise)

    def test_copy_cut_paste_without_and_with_selection_in_mainloop(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("text inițial")
            self.root.clipboard_clear()
            self.root.clipboard_append("păstrat")
            self.root.update()

            self.editor.text_widget.event_generate("<<Copy>>", when="now")
            self.editor.text_widget.event_generate("<<Cut>>", when="now")
            self.editor.text_widget.event_generate("<<Clear>>", when="now")
            self.assertEqual(self.root.clipboard_get(), "păstrat")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "text inițial")
            self.assertFalse(self.editor.is_dirty)

            self._select("1.5", "1.12")
            self.editor.text_widget.event_generate("<<Copy>>", when="now")
            self.assertEqual(self.root.clipboard_get(), "inițial")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "text inițial")

            self._select("1.0", "1.5")
            self.editor.text_widget.event_generate("<<Cut>>", when="now")
            self.assertEqual(self.root.clipboard_get(), "text ")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "inițial")
            self.assertTrue(self.editor.is_dirty)

            self.root.clipboard_clear()
            self.root.clipboard_append("adăugat ")
            self.root.update()
            self.editor.text_widget.mark_set("insert", "1.0")
            self.editor.text_widget.event_generate("<<Paste>>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "adăugat inițial")

            self._select("1.0", "1.7")
            self.root.clipboard_clear()
            self.root.clipboard_append("nou")
            self.root.update()
            self.editor.text_widget.event_generate("<<Paste>>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "nou inițial")

        self._run_in_mainloop(exercise)

    def test_undo_redo_commands_and_dirty_state_in_mainloop(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("bază")

            self.editor.text_widget.event_generate("<<Undo>>", when="now")
            self.editor.text_widget.event_generate("<<Redo>>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază")
            self.assertFalse(self.editor.is_dirty)

            with self.assertRaises(tk.TclError):
                self.editor.text_widget.edit_undo()

            self.editor.text_widget.edit_separator()
            self.editor.text_widget.insert("end-1c", " modificată")
            self.editor.text_widget.edit_separator()
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază modificată")
            self.assertTrue(self.editor.is_dirty)

            self.editor.text_widget.event_generate("<<Undo>>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază")
            self.assertFalse(self.editor.is_dirty)

            self.editor.text_widget.event_generate("<<Redo>>", when="now")
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază modificată")
            self.assertTrue(self.editor.is_dirty)

            self.editor.text_widget.edit_undo()
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază")
            self.assertFalse(self.editor.is_dirty)
            self.editor.text_widget.edit_redo()
            self.assertEqual(self.editor.text_widget.get("1.0", "end-1c"), "bază modificată")
            self.assertTrue(self.editor.is_dirty)

        self._run_in_mainloop(exercise)

    def test_proxy_preserves_real_tcl_errors_and_cleans_commands_on_destroy(self) -> None:
        with self.assertRaises(tk.TclError):
            self.editor.text_widget.index("sel.first")
        with self.assertRaises(tk.TclError):
            self.editor.text_widget.index("invalid.index")

        toplevel = tk.Toplevel(self.root)
        toplevel.withdraw()
        scoped_editor = RichTextEditor(toplevel, document(TextInline("temporar")))
        text_path = scoped_editor.text_widget._w
        helper_names = (
            scoped_editor._original_text_command,
            scoped_editor._prepare_text_command_name,
            scoped_editor._after_text_command_name,
        )
        for name in helper_names:
            self.assertTrue(bool(self.root.tk.call("info", "commands", name)))

        toplevel.destroy()
        self.root.update_idletasks()
        self.assertEqual(self.root.tk.call("info", "commands", text_path), "")
        for name in helper_names:
            self.assertEqual(self.root.tk.call("info", "commands", name), "")

    def test_ctrl_b_shortcut_with_and_without_selection(self) -> None:
        self._assert_single_shortcut_with_and_without_selection(
            attribute="bold",
            sequences=("<Control-b>", "<Control-B>"),
            style_index=0,
        )

    def test_ctrl_i_shortcut_with_and_without_selection(self) -> None:
        self._assert_single_shortcut_with_and_without_selection(
            attribute="italic",
            sequences=("<Control-i>", "<Control-I>"),
            style_index=1,
        )

    def test_ctrl_u_shortcut_with_and_without_selection(self) -> None:
        self._assert_single_shortcut_with_and_without_selection(
            attribute="underline",
            sequences=("<Control-u>", "<Control-U>"),
            style_index=2,
        )

    def test_repeating_shortcut_disables_formatting_with_and_without_selection(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            for attribute, sequence, style_index in (
                ("bold", "<Control-b>", 0),
                ("italic", "<Control-i>", 1),
                ("underline", "<Control-u>", 2),
            ):
                with self.subTest(attribute=attribute):
                    self._set_text("text")
                    self.editor.text_widget.focus_force()
                    self.root.update()
                    self._select("1.0", "1.4")

                    self._press_format_shortcut(sequence)
                    self.assertTrue(self.editor._style_variables[attribute].get())
                    self.assertTrue(self.editor.is_dirty)

                    self._press_format_shortcut(sequence)
                    self.assertEqual(
                        tuple(str(index) for index in self.editor.text_widget.tag_ranges("sel")),
                        ("1.0", "1.4"),
                    )
                    self.assertFalse(self.editor._style_variables[attribute].get())
                    self.assertFalse(self.editor.is_dirty)
                    self.assertEqual(
                        self.editor.get_document().to_dict(),
                        document(TextInline("text")).to_dict(),
                    )

                    self.editor.text_widget.tag_remove("sel", "1.0", "end")
                    self.editor.text_widget.mark_set("insert", "1.4")
                    self._press_format_shortcut(sequence)
                    self.assertTrue(self.editor._active_style[style_index])
                    self.assertTrue(self.editor._style_variables[attribute].get())

                    self._press_format_shortcut(sequence)
                    self.assertFalse(self.editor._active_style[style_index])
                    self.assertFalse(self.editor._style_variables[attribute].get())

                    self.editor.text_widget.event_generate("<KeyPress-q>", when="now")
                    self.editor.text_widget.event_generate("<KeyRelease-q>", when="now")
                    self.assertEqual(
                        self.editor.get_document().to_dict(),
                        document(TextInline("textq")).to_dict(),
                    )

        self._run_in_mainloop(exercise)

    def test_shortcuts_apply_uniform_formatting_to_mixed_selection(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            source = document(
                TextInline("a", bold=True),
                TextInline("b", italic=True),
                TextInline("c", underline=True),
            )
            self.editor.set_document(source)
            self.editor.text_widget.focus_force()
            self.root.update()
            self._select("1.0", "1.3")

            self._press_format_shortcut("<Control-b>")
            self.assertTrue(self.editor._style_variables["bold"].get())
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("a", bold=True),
                    TextInline("b", bold=True, italic=True),
                    TextInline("c", bold=True, underline=True),
                ).to_dict(),
            )

            self._press_format_shortcut("<Control-i>")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("ab", bold=True, italic=True),
                    TextInline("c", bold=True, italic=True, underline=True),
                ).to_dict(),
            )

            self._press_format_shortcut("<Control-u>")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", bold=True, italic=True, underline=True)).to_dict(),
            )

            self._press_format_shortcut("<Control-b>")
            self.assertFalse(self.editor._style_variables["bold"].get())
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", italic=True, underline=True)).to_dict(),
            )

        self._run_in_mainloop(exercise)

    def test_simultaneous_multiple_shortcuts_with_and_without_selection(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("bază ")
            self.editor.text_widget.focus_force()
            self.root.update()
            self.editor.text_widget.mark_set("insert", "end-1c")

            self._press_format_shortcut("<Control-b>")
            self._press_format_shortcut("<Control-i>")
            self.assertEqual(self.editor._active_style, (True, True, False))
            self.assertTrue(self.editor._style_variables["bold"].get())
            self.assertTrue(self.editor._style_variables["italic"].get())
            self.assertFalse(self.editor._style_variables["underline"].get())

            self.editor.text_widget.event_generate("<KeyPress-A>", when="now")
            self.editor.text_widget.event_generate("<KeyRelease-A>", when="now")
            self._press_format_shortcut("<Control-i>")
            self.assertEqual(self.editor._active_style, (True, False, False))
            self.editor.text_widget.event_generate("<KeyPress-B>", when="now")
            self.editor.text_widget.event_generate("<KeyRelease-B>", when="now")

            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("bază "),
                    TextInline("A", bold=True, italic=True),
                    TextInline("B", bold=True),
                ).to_dict(),
            )

            self._select("1.0", "1.4")
            self._press_format_shortcut("<Control-b>")
            self._press_format_shortcut("<Control-u>")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("bază", bold=True, underline=True),
                    TextInline(" "),
                    TextInline("A", bold=True, italic=True),
                    TextInline("B", bold=True),
                ).to_dict(),
            )

        self._run_in_mainloop(exercise)

    def test_undo_redo_after_shortcut_formatting(self) -> None:
        self._prepare_keyboard_target()

        def exercise() -> None:
            self._set_text("abc")
            self.editor.text_widget.focus_force()
            self.root.update()
            self._select("1.0", "1.3")

            self._press_format_shortcut("<Control-b>")
            self._press_format_shortcut("<Control-i>")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", bold=True, italic=True)).to_dict(),
            )

            self.editor.text_widget.tag_remove("sel", "1.0", "end")
            self.editor.text_widget.mark_set("insert", "1.3")
            self.editor.text_widget.edit_separator()
            self._press_format_shortcut("<Control-u>")
            self.assertEqual(self.editor._active_style, (True, True, True))
            self.editor.text_widget.event_generate("<KeyPress-Z>", when="now")
            self.editor.text_widget.event_generate("<KeyRelease-Z>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("abc", bold=True, italic=True),
                    TextInline("Z", bold=True, italic=True, underline=True),
                ).to_dict(),
            )

            self.editor.text_widget.event_generate("<<Undo>>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", bold=True, italic=True)).to_dict(),
            )
            self.assertTrue(self.editor.is_dirty)

            self.editor.text_widget.event_generate("<<Undo>>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", bold=True)).to_dict(),
            )

            self.editor.text_widget.event_generate("<<Undo>>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc")).to_dict(),
            )
            self.assertFalse(self.editor.is_dirty)

            self.editor.text_widget.event_generate("<<Redo>>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(TextInline("abc", bold=True)).to_dict(),
            )
            self.editor.text_widget.event_generate("<<Redo>>", when="now")
            self.editor.text_widget.event_generate("<<Redo>>", when="now")
            self.assertEqual(
                self.editor.get_document().to_dict(),
                document(
                    TextInline("abc", bold=True, italic=True),
                    TextInline("Z", bold=True, italic=True, underline=True),
                ).to_dict(),
            )

        self._run_in_mainloop(exercise)

    def test_shortcuts_do_not_affect_fields_outside_editor(self) -> None:
        external_entry = ttk.Entry(self.root)
        external_entry.insert(0, "câmp extern")
        external_entry.pack()
        self.addCleanup(external_entry.destroy)
        self._prepare_keyboard_target()

        def exercise() -> None:
            initial = document(TextInline("neafectat"))
            self.editor.set_document(initial)
            external_entry.focus_force()
            self.root.update()
            self.assertIs(self.root.focus_get(), external_entry)

            for sequence in (
                "<Control-b>",
                "<Control-B>",
                "<Control-i>",
                "<Control-I>",
                "<Control-u>",
                "<Control-U>",
            ):
                external_entry.event_generate(sequence, when="now")

            self.assertEqual(self.editor.get_document().to_dict(), initial.to_dict())
            self.assertEqual(self.editor._active_style, (False, False, False))
            self.assertFalse(self.editor._style_variables["bold"].get())
            self.assertFalse(self.editor._style_variables["italic"].get())
            self.assertFalse(self.editor._style_variables["underline"].get())
            self.assertFalse(self.editor.is_dirty)
            self.assertEqual(external_entry.get(), "câmp extern")

        self._run_in_mainloop(exercise)


if __name__ == "__main__":
    unittest.main()
