"""Teste pentru înlocuirea token-urilor în documente Rich Text."""

from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path

from docx import Document

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.services.token_substitution import (
    TokenSubstitutionError,
    substitute_variables,
)
from ortodocs.services.docx_import import parse_docx_bytes


def document(*paragraphs: Paragraph) -> RichDocument:
    return RichDocument(paragraphs=list(paragraphs))


def docx_with_token(key: str) -> bytes:
    word_document = Document()
    word_document.add_paragraph(f"{{{{{key}}}}}")
    stream = BytesIO()
    word_document.save(stream)
    return stream.getvalue()


def plain_text(value: RichDocument) -> list[str]:
    return [
        "".join(
            "\n" if isinstance(inline, LineBreakInline) else inline.text
            for inline in paragraph.inlines
        )
        for paragraph in value.paragraphs
    ]


class TokenSubstitutionTests(unittest.TestCase):
    def test_replaces_single_token_in_one_text_inline(self) -> None:
        source = document(Paragraph([TextInline("Salut, {{sex}}.")]))

        result = substitute_variables(source, {"sex": "Pacientă"}, {"sex"})

        self.assertEqual(plain_text(result), ["Salut, Pacientă."])

    def test_replaces_multiple_and_repeated_tokens_in_one_paragraph(self) -> None:
        source = document(Paragraph([TextInline("{{sex}} / {{data}} / {{sex}}")]))

        result = substitute_variables(
            source,
            {"sex": "Pacient", "data": "2026-11-10"},
            {"sex", "data"},
        )

        self.assertEqual(plain_text(result), ["Pacient / 2026-11-10 / Pacient"])

    def test_replaces_tokens_in_different_paragraphs(self) -> None:
        source = document(
            Paragraph([TextInline("{{varsta}}")]),
            Paragraph([TextInline("{{sex}}")]),
        )

        result = substitute_variables(source, {"varsta": "70", "sex": "Pacient"}, {"varsta", "sex"})

        self.assertEqual(plain_text(result), ["70", "Pacient"])

    def test_recognizes_token_split_between_two_adjacent_text_inlines(self) -> None:
        source = document(
            Paragraph([TextInline("{{data_", bold=True), TextInline("operatie}}", bold=True)])
        )

        result = substitute_variables(source, {"data_operatie": "10.11.2026"}, {"data_operatie"})

        self.assertEqual(result.paragraphs[0].inlines, [TextInline("10.11.2026", bold=True)])

    def test_recognizes_token_split_across_three_or_more_inlines(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("{{", italic=True),
                    TextInline("later", italic=True),
                    TextInline("alitate", italic=True),
                    TextInline("}}", italic=True),
                ]
            )
        )

        result = substitute_variables(source, {"lateralitate": "stânga"}, {"lateralitate"})

        self.assertEqual(result.paragraphs[0].inlines, [TextInline("stânga", italic=True)])

    def test_recognizes_character_split_token_around_empty_inlines_and_preserves_order(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("", italic=True),
                    TextInline("înainte ", italic=True),
                    TextInline("", bold=True),
                    TextInline("{", bold=True),
                    TextInline("{", bold=True),
                    TextInline("n", bold=True),
                    TextInline("", italic=True),
                    TextInline("u", bold=True),
                    TextInline("m", bold=True),
                    TextInline("e", bold=True),
                    TextInline("}", bold=True),
                    TextInline("}", bold=True),
                    TextInline("", underline=True),
                    TextInline(" după", underline=True),
                    TextInline("", italic=True),
                ]
            )
        )

        result = substitute_variables(source, {"nume": "valoare"}, {"nume"})

        self.assertEqual(plain_text(result), ["înainte valoare după"])
        self.assertEqual(
            result.paragraphs[0].inlines,
            [
                TextInline("înainte ", italic=True),
                TextInline("valoare", bold=True),
                TextInline(" după", underline=True),
            ],
        )

    def test_rejects_token_split_across_mixed_formatting(self) -> None:
        source = document(Paragraph([TextInline("{{sex", bold=True), TextInline("}}")]))

        with self.assertRaisesRegex(TokenSubstitutionError, "formatare B/I/U mixtă"):
            substitute_variables(source, {"sex": "Pacient"}, {"sex"})

    def test_preserves_static_fragments_and_their_distinct_formatting(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("Înainte ", italic=True),
                    TextInline("{{sex}}", bold=True, underline=True),
                    TextInline(" după", italic=True, underline=True),
                ]
            )
        )

        result = substitute_variables(source, {"sex": "Pacientă"}, {"sex"})

        self.assertEqual(
            result.paragraphs[0].inlines,
            [
                TextInline("Înainte ", italic=True),
                TextInline("Pacientă", bold=True, underline=True),
                TextInline(" după", italic=True, underline=True),
            ],
        )

    def test_replaces_tokens_at_paragraph_start_and_end(self) -> None:
        source = document(Paragraph([TextInline("{{prima}} mijloc {{ultima}}")]))

        result = substitute_variables(source, {"prima": "A", "ultima": "Z"}, {"prima", "ultima"})

        self.assertEqual(plain_text(result), ["A mijloc Z"])

    def test_preserves_bold_italic_underline_and_combined_styles(self) -> None:
        source = document(
            Paragraph(
                [
                    TextInline("{{bold}}", bold=True),
                    TextInline("{{italic}}", italic=True),
                    TextInline("{{under}}", underline=True),
                    TextInline("{{all}}", bold=True, italic=True, underline=True),
                ]
            )
        )

        result = substitute_variables(
            source,
            {"bold": "B", "italic": "I", "under": "U", "all": "BIU"},
            {"bold", "italic", "under", "all"},
        )

        self.assertEqual(
            result.paragraphs[0].inlines,
            [
                TextInline("B", bold=True),
                TextInline("I", italic=True),
                TextInline("U", underline=True),
                TextInline("BIU", bold=True, italic=True, underline=True),
            ],
        )

    def test_preserves_empty_paragraphs_and_line_breaks(self) -> None:
        source = document(
            Paragraph([]),
            Paragraph([TextInline("{{first}}"), LineBreakInline(), TextInline("{{second}}")]),
            Paragraph([]),
        )

        result = substitute_variables(
            source,
            {"first": "unu", "second": "doi"},
            {"first", "second"},
        )

        self.assertEqual(len(result.paragraphs), 3)
        self.assertEqual(result.paragraphs[0].inlines, [])
        self.assertEqual(
            result.paragraphs[1].inlines,
            [TextInline("unu"), LineBreakInline(), TextInline("doi")],
        )
        self.assertEqual(result.paragraphs[2].inlines, [])

    def test_rejects_token_crossing_line_break_or_paragraph(self) -> None:
        line_crossing = document(
            Paragraph([TextInline("{{da"), LineBreakInline(), TextInline("ta}}")])
        )
        paragraph_crossing = document(
            Paragraph([TextInline("{{da")]),
            Paragraph([TextInline("ta}}")]),
        )

        with self.assertRaisesRegex(TokenSubstitutionError, "întrerupere de rând"):
            substitute_variables(line_crossing, {"data": "x"}, {"data"})
        with self.assertRaisesRegex(TokenSubstitutionError, "limita unui paragraf"):
            substitute_variables(paragraph_crossing, {"data": "x"}, {"data"})

    def test_values_are_inserted_literally_without_recursive_substitution(self) -> None:
        source = document(Paragraph([TextInline("{{text}}")]))
        literal = "Pacientă — fractură {{alta_variabila}} (dreapta) {x}!"

        result = substitute_variables(source, {"text": literal}, {"text"})

        self.assertEqual(plain_text(result), [literal])

    def test_preserves_romanian_unicode_punctuation_braces_and_empty_values(self) -> None:
        source = document(Paragraph([TextInline("{{romanian}}|{{empty}}")]))
        literal = "Șold, stângă: (fractură) — ăâîșț {}"

        result = substitute_variables(
            source,
            {"romanian": literal, "empty": ""},
            {"romanian", "empty"},
        )

        self.assertEqual(plain_text(result), [literal + "|"])

    def test_rejects_newline_tab_control_and_surrogate_values_with_key(self) -> None:
        source = document(Paragraph([TextInline("{{value}}")]))
        for invalid in ("a\nb", "a\rb", "a\tb", "a\x01b", "a\ud800b"):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaisesRegex(TokenSubstitutionError, "value"):
                    substitute_variables(source, {"value": invalid}, {"value"})

    def test_rejects_non_string_values(self) -> None:
        source = document(Paragraph([TextInline("{{value}}")]))

        with self.assertRaisesRegex(TokenSubstitutionError, "trebuie să fie text"):
            substitute_variables(source, {"value": 12}, {"value"})  # type: ignore[dict-item]

    def test_rejects_non_string_and_empty_values_keys(self) -> None:
        source = document(Paragraph([TextInline("{{known}}")]))
        invalid_maps = ({1: "x"}, {"": "x"})

        for values in invalid_maps:
            with self.subTest(values=values):
                with self.assertRaisesRegex(TokenSubstitutionError, "chei suplimentare"):
                    substitute_variables(source, values, {"known"})  # type: ignore[arg-type]

    def test_expected_keys_must_be_a_set(self) -> None:
        source = document(Paragraph([]))
        invalid_expected_keys = ([], (), None, "known", {"known": True})

        for expected_keys in invalid_expected_keys:
            with self.subTest(expected_keys=expected_keys):
                with self.assertRaisesRegex(TokenSubstitutionError, "expected_keys trebuie să fie un set"):
                    substitute_variables(source, {}, expected_keys)  # type: ignore[arg-type]

    def test_expected_keys_reject_invalid_syntax_and_non_string_elements(self) -> None:
        source = document(Paragraph([]))
        invalid_keys: tuple[object, ...] = ("", "Upper", "with space", "1starts_with_digit", 7)

        for invalid_key in invalid_keys:
            with self.subTest(invalid_key=invalid_key):
                with self.assertRaisesRegex(TokenSubstitutionError, "sintaxă invalidă"):
                    substitute_variables(source, {}, {invalid_key})  # type: ignore[arg-type]

    def test_requires_exact_values_keys(self) -> None:
        source = document(Paragraph([TextInline("{{known}}")]))

        with self.assertRaisesRegex(TokenSubstitutionError, "lipsesc valorile"):
            substitute_variables(source, {}, {"known"})
        with self.assertRaisesRegex(TokenSubstitutionError, "chei suplimentare"):
            substitute_variables(source, {"known": "x", "extra": "y"}, {"known"})

    def test_rejects_unknown_missing_malformed_nested_and_unmatched_tokens(self) -> None:
        cases = (
            ("{{unknown}}", {"known"}, {"known": "x"}, "Token necunoscut"),
            (
                "Text fără token",
                {"known"},
                {"known": "x"},
                "Lipsesc din document token-urile",
            ),
            (
                "{{invalid-key}}",
                {"invalid_key"},
                {"invalid_key": "x"},
                "cheie invalidă",
            ),
            (
                "{{outer{{inner}}}}",
                {"inner"},
                {"inner": "x"},
                "Token imbricat",
            ),
            (
                "{{incomplete",
                {"incomplete"},
                {"incomplete": "x"},
                "Token incomplet",
            ),
            ("închidere }}", set(), {}, "fără token de deschidere"),
        )
        for text, expected, values, message in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(TokenSubstitutionError, message):
                    substitute_variables(document(Paragraph([TextInline(text)])), values, expected)

    def test_rejects_document_invalidated_after_construction(self) -> None:
        source = document(Paragraph([TextInline("{{key}}")]))
        source.paragraphs[0].inlines.append(object())  # type: ignore[arg-type]

        with self.assertRaisesRegex(TokenSubstitutionError, "Documentul Rich Text este invalid"):
            substitute_variables(source, {"key": "value"}, {"key"})

    def test_original_is_unchanged_and_result_is_independent(self) -> None:
        source = document(Paragraph([TextInline("{{key}}", bold=True)]))

        result = substitute_variables(source, {"key": "nou"}, {"key"})

        self.assertIsNot(result, source)
        self.assertEqual(source.paragraphs[0].inlines, [TextInline("{{key}}", bold=True)])
        result.paragraphs[0].inlines[0].text = "editat"
        self.assertEqual(source.paragraphs[0].inlines[0].text, "{{key}}")

    def test_consecutive_calls_do_not_change_previous_results(self) -> None:
        source = document(Paragraph([TextInline("{{key}}")]))

        first = substitute_variables(source, {"key": "primul"}, {"key"})
        second = substitute_variables(source, {"key": "al doilea"}, {"key"})

        self.assertEqual(plain_text(first), ["primul"])
        self.assertEqual(plain_text(second), ["al doilea"])
        self.assertIsNot(first, second)

    def test_failure_does_not_return_or_mutate_a_partially_substituted_document(self) -> None:
        source = document(Paragraph([TextInline("{{known}} {{unknown}}")]))
        before = source.to_json()

        with self.assertRaises(TokenSubstitutionError):
            substitute_variables(source, {"known": "deja procesat"}, {"known"})

        self.assertEqual(source.to_json(), before)

    def test_empty_expected_keys_allows_literal_single_braces(self) -> None:
        source = document(Paragraph([TextInline("{literal} și text simplu")]))

        result = substitute_variables(source, {}, set())

        self.assertEqual(plain_text(result), ["{literal} și text simplu"])

    def test_rejects_non_rich_document_and_non_mapping_values(self) -> None:
        with self.assertRaisesRegex(TokenSubstitutionError, "RichDocument valid"):
            substitute_variables("text", {}, set())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TokenSubstitutionError, "mapare"):
            substitute_variables(document(Paragraph([])), [], set())  # type: ignore[arg-type]

    def test_token_key_acceptance_matches_docx_importer(self) -> None:
        valid_keys = ("x", "field2", "field_name")
        invalid_keys = ("", "Upper", "with space", "1starts_with_digit", "diacritică", "with-hyphen")

        for key in valid_keys:
            with self.subTest(key=key, accepted=True):
                _, report, _ = parse_docx_bytes(docx_with_token(key))
                self.assertTrue(report.is_accepted)
                self.assertEqual(report.variables, {key: 1})
                source = document(Paragraph([TextInline(f"{{{{{key}}}}}")]))
                result = substitute_variables(source, {key: "înlocuit"}, {key})
                self.assertEqual(plain_text(result), ["înlocuit"])

        for key in invalid_keys:
            with self.subTest(key=key, accepted=False):
                _, report, _ = parse_docx_bytes(docx_with_token(key))
                self.assertEqual(report.status, "rejected")
                self.assertIn("invalid_variable_key", {issue.code for issue in report.variable_errors})
                source = document(Paragraph([TextInline(f"{{{{{key}}}}}")]))
                with self.assertRaisesRegex(TokenSubstitutionError, "cheie invalidă"):
                    substitute_variables(source, {"placeholder": "înlocuit"}, {"placeholder"})


if __name__ == "__main__":
    unittest.main()
