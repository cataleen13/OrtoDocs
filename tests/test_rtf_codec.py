"""Teste independente de Tk pentru codificarea și decodificarea RTF."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.services.rtf_codec import (
    MAX_DOCUMENT_CHARACTERS,
    MAX_GROUP_DEPTH,
    MAX_RTF_INPUT_LENGTH,
    RtfCodecError,
    decode_rtf,
    encode_rtf,
)


def document(*paragraphs: Paragraph) -> RichDocument:
    return RichDocument(paragraphs=list(paragraphs))


class RtfCodecTests(unittest.TestCase):
    def assert_round_trip(self, source: RichDocument) -> str:
        encoded = encode_rtf(source)
        decoded = decode_rtf(encoded)
        self.assertEqual(decoded.to_dict(), source.to_dict())
        return encoded

    def test_round_trips_plain_text_and_produces_a_complete_rtf_group(self) -> None:
        source = document(Paragraph([TextInline("Text simplu.")]))

        encoded = self.assert_round_trip(source)

        self.assertTrue(encoded.startswith(r"{\rtf1\ansi"))
        self.assertTrue(encoded.endswith("}"))
        self.assertIn(r"\par", encoded)

    def test_round_trips_all_eight_bold_italic_underline_combinations(self) -> None:
        styles = tuple(
            (bold, italic, underline)
            for bold in (False, True)
            for italic in (False, True)
            for underline in (False, True)
        )
        source = document(
            Paragraph(
                [
                    TextInline(
                        f"stil-{index}",
                        bold=style[0],
                        italic=style[1],
                        underline=style[2],
                    )
                    for index, style in enumerate(styles)
                ]
            )
        )

        self.assert_round_trip(source)

    def test_encodes_romanian_diacritics_as_unicode_and_round_trips_them(self) -> None:
        source = document(Paragraph([TextInline("ăâîșț ĂÂÎȘȚ — „ortopedie”")]))

        encoded = self.assert_round_trip(source)

        self.assertIn(r"\u259?", encoded)
        self.assertIn(r"\u537?", encoded)
        self.assertTrue(encoded.isascii())

    def test_encodes_non_bmp_characters_as_a_surrogate_pair(self) -> None:
        source = document(Paragraph([TextInline("A😀B")]))

        encoded = self.assert_round_trip(source)

        self.assertIn(r"\u-10179?\u-8704?", encoded)

    def test_escapes_braces_and_backslashes(self) -> None:
        source = document(Paragraph([TextInline(r"acolade {text} și backslash \ final")]))

        encoded = self.assert_round_trip(source)

        self.assertIn(r"\{text\}", encoded)
        self.assertIn(r"\\ final", encoded)

    def test_round_trips_paragraphs_and_line_breaks_as_distinct_controls(self) -> None:
        source = document(
            Paragraph([TextInline("prima linie"), LineBreakInline(), TextInline("continuare")]),
            Paragraph([TextInline("paragraf nou")]),
        )

        encoded = self.assert_round_trip(source)

        self.assertIn(r"\line", encoded)
        self.assertEqual(encoded.count(r"\par"), 2)

    def test_round_trips_consecutive_empty_paragraphs(self) -> None:
        source = document(Paragraph([]), Paragraph([]), Paragraph([]))

        encoded = self.assert_round_trip(source)

        self.assertEqual(encoded.count(r"\par"), 3)

    def test_distinguishes_zero_paragraphs_from_one_empty_paragraph(self) -> None:
        zero = RichDocument()
        one_empty = document(Paragraph([]))

        self.assertEqual(decode_rtf(encode_rtf(zero)).paragraphs, [])
        self.assertEqual(decode_rtf(encode_rtf(one_empty)).to_dict(), one_empty.to_dict())

    def test_preserves_text_order_and_fragments_with_different_styles(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("înainte ", italic=True),
                    TextInline("bold", bold=True),
                    TextInline(" ambele", bold=True, italic=True, underline=True),
                    TextInline(" după", underline=True),
                ]
            )
        )

        self.assert_round_trip(source)

    def test_merges_adjacent_text_fragments_with_identical_style(self) -> None:
        source = document(
            Paragraph([TextInline("fragment 1", bold=True), TextInline(" fragment 2", bold=True)])
        )

        decoded = decode_rtf(encode_rtf(source))

        self.assertEqual(decoded.paragraphs[0].inlines, [TextInline("fragment 1 fragment 2", bold=True)])

    def test_nested_groups_restore_inherited_bold_italic_and_underline(self) -> None:
        source = r"{\rtf1\ansi\uc1 a{\b b{\i c}d{\ul e}}f\par}"

        decoded = decode_rtf(source)

        self.assertEqual(
            decoded.to_dict(),
            document(
                Paragraph(
                    [
                        TextInline("a"),
                        TextInline("b", bold=True),
                        TextInline("c", bold=True, italic=True),
                        TextInline("d", bold=True),
                        TextInline("e", bold=True, underline=True),
                        TextInline("f"),
                    ]
                )
            ).to_dict(),
        )

    def test_decodes_consecutive_unicode_escapes_and_surrogate_pairs(self) -> None:
        source = r"{\rtf1\ansi\uc1 \u259?\u537?\u-10179?\u-8704?\par}"

        decoded = decode_rtf(source)

        self.assertEqual(decoded.paragraphs[0].inlines, [TextInline("ăș😀")])

    def test_decodes_unicode_with_a_multi_character_fallback(self) -> None:
        source = r"{\rtf1\ansi\uc2 \u259abX\par}"

        decoded = decode_rtf(source)

        self.assertEqual(decoded.paragraphs[0].inlines, [TextInline("ăX")])

    def test_decodes_escaped_cp1252_characters(self) -> None:
        source = r"{\rtf1\ansi\ansicpg1252\uc1 caf\'e9\par}"

        self.assertEqual(decode_rtf(source).paragraphs[0].inlines, [TextInline("café")])

    def test_empty_nested_groups_do_not_add_text(self) -> None:
        source = r"{\rtf1\ansi\uc1 a{}b\par}"

        self.assertEqual(decode_rtf(source).paragraphs[0].inlines, [TextInline("ab")])

    def test_rejects_fields_images_objects_hyperlinks_fonts_and_other_controls(self) -> None:
        unsupported = (
            r"{\rtf1\ansi {\field text}}",
            r"{\rtf1\ansi {\pict image}}",
            r"{\rtf1\ansi {\object object}}",
            r"{\rtf1\ansi {\*\foo hidden}}",
            r"{\rtf1\ansi {\fonttbl{\f0 Arial;}}}",
            r"{\rtf1\ansi {\colortbl;}}",
            r"{\rtf1\ansi \tab}",
            r"{\rtf1\ansi \v hidden}",
            r"{\rtf1\ansi \cf1 colored}",
            r"{\rtf1\ansi \plain text}",
            r"{\rtf1\ansi \bin4 data}",
        )
        for source in unsupported:
            with self.subTest(source=source):
                with self.assertRaises(RtfCodecError):
                    decode_rtf(source)

    def test_rejects_malformed_groups_headers_controls_and_unicode(self) -> None:
        malformed = (
            "",
            "text",
            r"{xrtf1abc}",
            r"{rtf1abc}",
            r"{\tf1abc}",
            r"{\text instead}",
            r"{\ansi text}",
            r"{\rtf1\ansi text",
            r"{\rtf1\ansi text}}",
            "{\\rtf1\\ansi \\",
            r"{\rtf1\ansi \'zz}",
            r"{\rtf1\ansi \b2 text}",
            r"{\rtf1\ansi \uX?}",
            r"{\rtf1\ansi \u999999?}",
            r"{\rtf1\ansi\uc1 \u65}",
            r"{\rtf1\ansi\uc1 \u-10179?x}",
            r"{\rtf1\ansi\uc1 \u-8704?}",
            r"{\rtf1\ansi\uc1 \ansicpg1250 x}",
        )
        for source in malformed:
            with self.subTest(source=source):
                with self.assertRaises(RtfCodecError):
                    decode_rtf(source)

    def test_rejects_a_surrogate_pair_split_by_a_group_boundary(self) -> None:
        source = r"{\rtf1\ansi\uc1 \u-10179?{\b\u-8704?}\par}"

        with self.assertRaises(RtfCodecError):
            decode_rtf(source)

    def test_rejects_excessive_group_depth(self) -> None:
        nested = "{" * MAX_GROUP_DEPTH + "}" * MAX_GROUP_DEPTH
        source = r"{\rtf1\ansi\uc1 " + nested + "}"

        with self.assertRaisesRegex(RtfCodecError, "Adâncimea grupurilor"):
            decode_rtf(source)

    def test_rejects_input_above_the_size_limit(self) -> None:
        source = "x" * (MAX_RTF_INPUT_LENGTH + 1)

        with self.assertRaisesRegex(RtfCodecError, "depășește limita"):
            decode_rtf(source)

    def test_rejects_documents_above_the_character_limit_on_encode_and_decode(self) -> None:
        oversized_document = document(
            Paragraph([TextInline("x" * (MAX_DOCUMENT_CHARACTERS + 1))])
        )
        oversized_rtf = r"{\rtf1\ansi\uc1 " + ("x" * (MAX_DOCUMENT_CHARACTERS + 1)) + r"\par}"

        with self.assertRaisesRegex(RtfCodecError, "limita"):
            encode_rtf(oversized_document)
        with self.assertRaisesRegex(RtfCodecError, "limita"):
            decode_rtf(oversized_rtf)

    def test_character_limit_accepts_exact_boundary_for_explicit_and_implicit_paragraphs(self) -> None:
        text = "x" * (MAX_DOCUMENT_CHARACTERS - 1)
        source = document(Paragraph([TextInline(text)]))

        encoded = encode_rtf(source)
        decoded_explicit = decode_rtf(encoded)
        decoded_implicit = decode_rtf(r"{\rtf1\ansi\uc1 " + text + "}")

        self.assertEqual(decoded_explicit.to_dict(), source.to_dict())
        self.assertEqual(decoded_implicit.to_dict(), source.to_dict())

    def test_character_limit_rejects_one_over_for_encode_and_explicit_or_implicit_decode(self) -> None:
        text = "x" * MAX_DOCUMENT_CHARACTERS
        source = document(Paragraph([TextInline(text)]))
        explicit_rtf = r"{\rtf1\ansi\uc1 " + text + r"\par}"
        implicit_rtf = r"{\rtf1\ansi\uc1 " + text + "}"

        with self.assertRaisesRegex(RtfCodecError, "limita"):
            encode_rtf(source)
        with self.assertRaisesRegex(RtfCodecError, "limita"):
            decode_rtf(explicit_rtf)
        with self.assertRaisesRegex(RtfCodecError, "limita"):
            decode_rtf(implicit_rtf)

    def test_rejects_non_rich_document_and_non_string_rtf(self) -> None:
        with self.assertRaises(TypeError):
            encode_rtf("text")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            decode_rtf(b"{\\rtf1}")  # type: ignore[arg-type]

    def test_encode_does_not_mutate_source_and_decodes_independent_instances(self) -> None:
        source = document(
            Paragraph([TextInline("original", bold=True), LineBreakInline(), TextInline("final")]),
            Paragraph([]),
        )
        original_json = source.to_json()
        encoded = encode_rtf(source)
        first = decode_rtf(encoded)
        second = decode_rtf(encoded)

        first.paragraphs[0].inlines[0].text = "modificat"

        self.assertEqual(source.to_json(), original_json)
        self.assertEqual(second.to_dict(), source.to_dict())


if __name__ == "__main__":
    unittest.main()
