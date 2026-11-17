"""Teste cu widgeturi Tk reale pentru conversia RichDocument."""

from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.ui.rich_text_codec import (
    LINE_BREAK_TAG,
    PARAGRAPH_SEPARATOR_TAG,
    RichTextCodecError,
    read_rich_document,
    render_rich_document,
)


def document(*paragraphs: Paragraph) -> RichDocument:
    return RichDocument(paragraphs=list(paragraphs))


class RichTextCodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk nu poate inițializa root-ul în acest mediu: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except tk.TclError:
            pass

    def setUp(self) -> None:
        self.widget = tk.Text(self.root)
        self.widget.pack()
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.widget.destroy()
        except tk.TclError:
            pass

    def assertRoundTrip(self, source: RichDocument, expected: RichDocument | None = None) -> None:
        state = render_rich_document(self.widget, source)
        result = read_rich_document(self.widget, state)
        self.assertEqual(result.to_dict(), (expected or source).to_dict())

    def test_round_trips_one_simple_paragraph(self) -> None:
        self.assertRoundTrip(document(Paragraph([TextInline("Text simplu.")])))

    def test_round_trips_two_paragraphs_without_an_extra_terminal_paragraph(self) -> None:
        source = document(Paragraph([TextInline("Primul")]), Paragraph([TextInline("Al doilea")]))

        state = render_rich_document(self.widget, source)

        self.assertEqual(self.widget.get("1.0", "end-1c"), "Primul\nAl doilea")
        self.assertEqual(self.widget.get("end-1c", "end"), "\n")
        result = read_rich_document(self.widget, state)
        self.assertEqual(result.to_dict(), source.to_dict())
        self.assertEqual(len(result.paragraphs), 2)

    def test_round_trips_empty_paragraph_positions_and_consecutive_empty_paragraphs(self) -> None:
        cases = (
            document(Paragraph([]), Paragraph([TextInline("final")])),
            document(Paragraph([TextInline("inițial")]), Paragraph([])),
            document(Paragraph([]), Paragraph([]), Paragraph([])),
            document(Paragraph([]), Paragraph([TextInline("mijloc")]), Paragraph([])),
        )
        for source in cases:
            with self.subTest(document=source.to_dict()):
                self.assertRoundTrip(source)

    def test_preserves_zero_paragraphs_separately_from_one_empty_paragraph(self) -> None:
        zero_paragraphs = RichDocument()
        zero_state = render_rich_document(self.widget, zero_paragraphs)
        self.assertEqual(read_rich_document(self.widget, zero_state).paragraphs, [])

        one_empty_paragraph = document(Paragraph([]))
        empty_state = render_rich_document(self.widget, one_empty_paragraph)
        self.assertEqual(
            read_rich_document(self.widget, empty_state).to_dict(),
            one_empty_paragraph.to_dict(),
        )
        self.assertEqual(read_rich_document(self.widget).to_dict(), one_empty_paragraph.to_dict())

    def test_modified_empty_document_normalizes_to_one_empty_paragraph(self) -> None:
        state = render_rich_document(self.widget, RichDocument())
        self.widget.insert("1.0", "x")
        self.widget.delete("1.0", "end-1c")

        self.assertTrue(self.widget.edit_modified())
        self.assertEqual(
            read_rich_document(self.widget, state).to_dict(),
            document(Paragraph([])).to_dict(),
        )

    def test_round_trips_line_breaks_and_paragraph_boundaries_independently(self) -> None:
        source = document(
            Paragraph([TextInline("început"), LineBreakInline(), TextInline("continuare")]),
            Paragraph([]),
            Paragraph([LineBreakInline()]),
        )

        state = render_rich_document(self.widget, source)
        result = read_rich_document(self.widget, state)

        self.assertEqual(result.to_dict(), source.to_dict())
        paragraph_ranges = self.widget.tag_ranges(PARAGRAPH_SEPARATOR_TAG)
        paragraph_separator_characters = sum(
            self.widget.get(start, end).count("\n")
            for start, end in zip(paragraph_ranges[::2], paragraph_ranges[1::2])
        )
        line_break_ranges = self.widget.tag_ranges(LINE_BREAK_TAG)
        line_break_characters = sum(
            self.widget.get(start, end).count("\n")
            for start, end in zip(line_break_ranges[::2], line_break_ranges[1::2])
        )

        self.assertEqual(paragraph_separator_characters, 2)
        self.assertEqual(line_break_characters, 2)

    def test_rejects_an_unclassified_newline_instead_of_guessing_its_meaning(self) -> None:
        self.widget.insert("1.0", "primul\nal doilea")

        with self.assertRaisesRegex(RichTextCodecError, "Newline-ul.*tag structural"):
            read_rich_document(self.widget)

    def test_round_trips_each_bold_italic_underline_combination(self) -> None:
        styles = (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (False, False, True),
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        )
        source = document(
            Paragraph(
                [
                    TextInline(
                        f"stil-{index}",
                        bold=bold,
                        italic=italic,
                        underline=underline,
                    )
                    for index, (bold, italic, underline) in enumerate(styles)
                ]
            )
        )
        expected = document(
            Paragraph(
                [
                    TextInline(
                        "".join(
                            f"stil-{index}"
                            for index, candidate in enumerate(styles)
                            if candidate == style
                        ),
                        bold=style[0],
                        italic=style[1],
                        underline=style[2],
                    )
                    for style in dict.fromkeys(styles)
                ]
            )
        )

        self.assertRoundTrip(source, expected)

    def test_preserves_static_fragments_with_distinct_styles_in_one_paragraph(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("Înainte ", italic=True),
                    TextInline("bold", bold=True),
                    TextInline(" ambele", bold=True, italic=True, underline=True),
                    TextInline(" după", underline=True),
                ]
            )
        )

        self.assertRoundTrip(source)

    def test_coalesces_consecutive_text_fragments_with_the_same_style(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("fragment 1", bold=True),
                    TextInline(" fragment 2", bold=True),
                    TextInline(" simplu"),
                ]
            )
        )
        expected = document(
            Paragraph([TextInline("fragment 1 fragment 2", bold=True), TextInline(" simplu")])
        )

        self.assertRoundTrip(source, expected)

    def test_preserves_unicode_diacritics_punctuation_braces_and_long_text(self) -> None:
        value = "Șold, stângă — fractură: {αβγ} [șțîăâ] „text” !? " * 1000
        self.assertRoundTrip(document(Paragraph([TextInline(value)])))

    def test_render_does_not_modify_the_original_document(self) -> None:
        source = document(
            Paragraph([TextInline("Nu modifica", bold=True), LineBreakInline(), TextInline("sursa")]),
            Paragraph([]),
        )
        before = source.to_json()

        render_rich_document(self.widget, source)

        self.assertEqual(source.to_json(), before)

    def test_successive_documents_do_not_retain_old_style_ranges(self) -> None:
        render_rich_document(self.widget, document(Paragraph([TextInline("bold", bold=True)])))
        second = document(Paragraph([TextInline("simplu")]))
        state = render_rich_document(self.widget, second)

        self.assertEqual(read_rich_document(self.widget, state).to_dict(), second.to_dict())
        self.assertNotIn("_ortodocs_codec_style_100", self.widget.tag_names("1.0"))
        self.assertIn("_ortodocs_codec_style_000", self.widget.tag_names("1.0"))

    def test_render_preserves_foreign_tag_configuration(self) -> None:
        self.widget.tag_configure("application_custom", foreground="#123456")
        source = document(Paragraph([TextInline("new content")]))

        render_rich_document(self.widget, source)

        self.assertIn("application_custom", self.widget.tag_names())
        self.assertEqual(self.widget.tag_cget("application_custom", "foreground"), "#123456")

    def test_render_restores_disabled_widget_state(self) -> None:
        self.widget.configure(state="disabled")

        render_rich_document(self.widget, document(Paragraph([TextInline("text")])))

        self.assertEqual(self.widget.cget("state"), "disabled")
        self.assertEqual(self.widget.get("1.0", "end-1c"), "text")

    def test_refuses_non_rich_document_and_invalid_mutated_document(self) -> None:
        with self.assertRaises(TypeError):
            render_rich_document(self.widget, "text")  # type: ignore[arg-type]

        invalid = document(Paragraph([TextInline("valid")]))
        invalid.paragraphs[0].inlines.append(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(RichTextCodecError, "RichDocument valid"):
            render_rich_document(self.widget, invalid)

    def test_read_rejects_state_for_a_different_widget(self) -> None:
        state = render_rich_document(self.widget, RichDocument())
        other_widget = tk.Text(self.root)
        try:
            with self.assertRaisesRegex(ValueError, "alt widget"):
                read_rich_document(other_widget, state)
        finally:
            other_widget.destroy()


if __name__ == "__main__":
    unittest.main()
