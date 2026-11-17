"""Teste izolate, cu documente DOCX create numai în memorie."""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
import warnings
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_BREAK
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.import_report import ImportReport
from ortodocs.models.rich_document import (
    LineBreakInline,
    Paragraph,
    RichDocument,
    TextInline,
)
from ortodocs.services.docx_import import (
    DOCX_PARSER_VERSION,
    MAX_DOCX_COMPRESSED_BYTES,
    MAX_DOCX_UNCOMPRESSED_BYTES,
    MAX_ZIP_COMPONENTS,
    MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES,
    parse_docx_bytes,
)


def make_docx(builder=None) -> bytes:
    document = Document()
    if builder is not None:
        builder(document)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def rewrite_package(source: bytes, replacements: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(source), "r") as original, ZipFile(
        output, "w", ZIP_DEFLATED
    ) as rewritten:
        for item in original.infolist():
            if item.filename in replacements:
                rewritten.writestr(item.filename, replacements.pop(item.filename))
            else:
                rewritten.writestr(item, original.read(item.filename))
        for name, data in replacements.items():
            rewritten.writestr(name, data)
    return output.getvalue()


def replace_document_xml(source: bytes, mutate) -> bytes:
    return replace_package_xml(source, "word/document.xml", mutate)


def replace_package_xml(source: bytes, part_name: str, mutate) -> bytes:
    with ZipFile(BytesIO(source), "r") as archive:
        xml_data = archive.read(part_name)
    from lxml import etree

    root = etree.fromstring(xml_data)
    mutate(root)
    changed = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return rewrite_package(source, {part_name: changed})


def make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def alter_zip_flags(source: bytes, filename: str, flag: int) -> bytes:
    """Setează un flag în antetele local și central ale componentei ZIP."""
    data = bytearray(source)
    local_signature = b"PK\x03\x04"
    central_signature = b"PK\x01\x02"
    offset = 0
    while True:
        offset = data.find(local_signature, offset)
        if offset < 0:
            break
        name_length = int.from_bytes(data[offset + 26 : offset + 28], "little")
        extra_length = int.from_bytes(data[offset + 28 : offset + 30], "little")
        name = bytes(data[offset + 30 : offset + 30 + name_length]).decode("utf-8")
        if name == filename:
            flags = int.from_bytes(data[offset + 6 : offset + 8], "little") | flag
            data[offset + 6 : offset + 8] = flags.to_bytes(2, "little")
        offset += 4
    offset = 0
    while True:
        offset = data.find(central_signature, offset)
        if offset < 0:
            break
        name_length = int.from_bytes(data[offset + 28 : offset + 30], "little")
        extra_length = int.from_bytes(data[offset + 30 : offset + 32], "little")
        comment_length = int.from_bytes(data[offset + 32 : offset + 34], "little")
        name = bytes(data[offset + 46 : offset + 46 + name_length]).decode("utf-8")
        if name == filename:
            flags = int.from_bytes(data[offset + 8 : offset + 10], "little") | flag
            data[offset + 8 : offset + 10] = flags.to_bytes(2, "little")
        offset += 46 + name_length + extra_length + comment_length
    return bytes(data)


def corrupt_central_crc(source: bytes, filename: str) -> bytes:
    data = bytearray(source)
    offset = data.find(b"PK\x01\x02")
    while offset >= 0:
        name_length = int.from_bytes(data[offset + 28 : offset + 30], "little")
        extra_length = int.from_bytes(data[offset + 30 : offset + 32], "little")
        comment_length = int.from_bytes(data[offset + 32 : offset + 34], "little")
        name = bytes(data[offset + 46 : offset + 46 + name_length]).decode("utf-8")
        if name == filename:
            data[offset + 16] ^= 0x01
            return bytes(data)
        offset = data.find(b"PK\x01\x02", offset + 46 + name_length + extra_length + comment_length)
    raise AssertionError(f"Componenta {filename!r} nu există în arhivă")


class RichDocumentModelTests(unittest.TestCase):
    def test_json_round_trip_preserves_paragraphs_runs_and_line_breaks(self) -> None:
        original = RichDocument(
            paragraphs=[
                Paragraph(
                    [
                        TextInline("A", bold=True),
                        LineBreakInline(),
                        TextInline("B", italic=True, underline=True),
                    ]
                ),
                Paragraph(),
            ]
        )

        decoded = RichDocument.from_json(original.to_json())

        self.assertEqual(decoded, original)
        self.assertEqual(decoded.to_dict()["paragraphs"][1]["inlines"], [])
        self.assertEqual(decoded.paragraphs[0].inlines[1], LineBreakInline())

    def test_json_rejects_unknown_schema_and_invalid_inline(self) -> None:
        with self.assertRaises(ValueError):
            RichDocument.from_dict({"schema_version": 2, "paragraphs": []})
        with self.assertRaises(ValueError):
            RichDocument.from_dict(
                {
                    "schema_version": 1,
                    "paragraphs": [{"inlines": [{"type": "image"}]}],
                }
            )

    def test_text_inline_rejects_structural_controls_and_line_separators(self) -> None:
        self.assertEqual(TextInline("Text normal").text, "Text normal")
        for value in (
            "a\nb",
            "a\rb",
            "a\tb",
            "a\x00b",
            "a\x85b",
            "a\u2028b",
            "a\u2029b",
            "a\ud800b",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    TextInline(value)

    def test_json_rejects_invalid_text_and_formatting_types(self) -> None:
        base = {
            "schema_version": 1,
            "paragraphs": [
                {
                    "inlines": [
                        {"type": "text", "text": "text", "bold": False, "italic": False, "underline": False}
                    ]
                }
            ],
        }
        with self.assertRaises(ValueError):
            RichDocument.from_dict(
                {
                    **base,
                    "paragraphs": [
                        {
                            "inlines": [
                                {"type": "text", "text": "a\n", "bold": False, "italic": False, "underline": False}
                            ]
                        }
                    ],
                }
            )
        with self.assertRaises(TypeError):
            RichDocument.from_dict(
                {
                    **base,
                    "paragraphs": [
                        {
                            "inlines": [
                                {"type": "text", "text": "text", "bold": "false", "italic": False, "underline": False}
                            ]
                        }
                    ],
                }
            )


class DocxImportTests(unittest.TestCase):
    def assert_rejected_with(self, data: bytes, code: str) -> tuple[RichDocument, ImportReport, str]:
        document, report, digest = parse_docx_bytes(data)
        self.assertEqual(report.status, "rejected")
        all_codes = {
            issue.code
            for group in (
                report.formatting_errors,
                report.unsupported_elements,
                report.variable_errors,
                report.content_errors,
                report.archive_errors,
                report.xml_errors,
                report.structure_errors,
                report.resource_errors,
                report.parsing_errors,
            )
            for issue in group
        }
        self.assertIn(code, all_codes)
        return document, report, digest

    def test_valid_document_returns_rich_text_report_and_sha256(self) -> None:
        data = make_docx(lambda document: document.add_paragraph("Text simplu"))

        rich_document, report, digest = parse_docx_bytes(data)

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.paragraph_count, 1)
        self.assertEqual(report.fragment_count, 1)
        self.assertEqual(rich_document.paragraphs[0].inlines, [TextInline("Text simplu")])
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(DOCX_PARSER_VERSION, "1.0.0")

    def test_multiple_paragraphs_and_empty_paragraph_are_preserved(self) -> None:
        def build(document):
            document.add_paragraph("Primul")
            document.add_paragraph("")
            document.add_paragraph("Al treilea")

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.paragraph_count, 3)
        self.assertEqual(rich_document.paragraphs[1], Paragraph())
        self.assertEqual(rich_document.paragraphs[2].inlines, [TextInline("Al treilea")])

    def test_empty_or_whitespace_only_documents_are_rejected(self) -> None:
        for data in (
            make_docx(),
            make_docx(lambda document: document.add_paragraph("")),
            make_docx(lambda document: document.add_paragraph(" \t  \n")),
        ):
            with self.subTest(size=len(data)):
                _, report, _ = self.assert_rejected_with(data, "empty_document")
                self.assertEqual(report.content_errors[0].part, "word/document.xml")

    def test_document_containing_only_a_valid_variable_is_not_empty(self) -> None:
        document, report, _ = parse_docx_bytes(make_docx(lambda d: d.add_paragraph("{{data_internarii}}")))

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.variables, {"data_internarii": 1})
        self.assertEqual(document.paragraphs[0].inlines, [TextInline("{{data_internarii}}")])

    def test_bold_italic_and_simple_underline_are_preserved(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("bold").bold = True
            paragraph.add_run("italic").italic = True
            paragraph.add_run("underline").underline = True

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(
            rich_document.paragraphs[0].inlines,
            [
                TextInline("bold", bold=True),
                TextInline("italic", italic=True),
                TextInline("underline", underline=True),
            ],
        )
        self.assertEqual(report.bold_fragment_count, 1)
        self.assertEqual(report.italic_fragment_count, 1)
        self.assertEqual(report.underline_fragment_count, 1)

    def test_formatting_inherited_from_paragraph_style_is_applied(self) -> None:
        def build(document):
            style = document.styles.add_style("OrtoDocs Bold Paragraph", WD_STYLE_TYPE.PARAGRAPH)
            style.base_style = document.styles["Normal"]
            style.font.bold = True
            paragraph = document.add_paragraph("Text moștenit")
            paragraph.style = style

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(rich_document.paragraphs[0].inlines, [TextInline("Text moștenit", bold=True)])

    def test_character_style_and_based_on_chain_formatting_is_applied(self) -> None:
        def build(document):
            base = document.styles.add_style("Orto Character Base", WD_STYLE_TYPE.CHARACTER)
            base.font.bold = True
            derived = document.styles.add_style("Orto Character Derived", WD_STYLE_TYPE.CHARACTER)
            derived.base_style = base
            derived.font.italic = True
            run = document.add_paragraph().add_run("formatat")
            run.style = derived
            overridden = document.add_paragraph().add_run("suprascris")
            overridden.style = derived
            overridden.bold = False

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(
            rich_document.paragraphs[0].inlines,
            [TextInline("formatat", bold=True, italic=True)],
        )
        self.assertEqual(
            rich_document.paragraphs[1].inlines,
            [TextInline("suprascris", italic=True)],
        )

    def test_paragraph_style_chain_and_direct_false_override_are_applied(self) -> None:
        def build(document):
            base = document.styles.add_style("Orto Paragraph Base", WD_STYLE_TYPE.PARAGRAPH)
            base.font.bold = True
            derived = document.styles.add_style("Orto Paragraph Derived", WD_STYLE_TYPE.PARAGRAPH)
            derived.base_style = base
            derived.font.italic = True
            underline = OxmlElement("w:u")
            underline.set(qn("w:val"), "single")
            derived._element.get_or_add_rPr().append(underline)
            paragraph = document.add_paragraph()
            paragraph.style = derived
            paragraph.add_run("suprascris").bold = False

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(
            rich_document.paragraphs[0].inlines,
            [TextInline("suprascris", italic=True, underline=True)],
        )

    def test_ambiguous_default_and_circular_styles_are_rejected(self) -> None:
        def ambiguous(document):
            extra = document.styles.add_style("Orto Second Default", WD_STYLE_TYPE.PARAGRAPH)
            extra.element.set(qn("w:default"), "1")
            document.add_paragraph("text")

        self.assert_rejected_with(make_docx(ambiguous), "ambiguous_default_paragraph_style")

        def create_circular_styles(document):
            first = document.styles.add_style("Orto Cycle One", WD_STYLE_TYPE.PARAGRAPH)
            document.styles.add_style("Orto Cycle Two", WD_STYLE_TYPE.PARAGRAPH)
            paragraph = document.add_paragraph("text")
            paragraph.style = first

        def make_cycle(root):
            styles = {node.get(qn("w:styleId")): node for node in root.findall(qn("w:style"))}
            first = styles["OrtoCycleOne"]
            second = styles["OrtoCycleTwo"]
            first_base = first.find(qn("w:basedOn"))
            if first_base is None:
                first_base = OxmlElement("w:basedOn")
                first.append(first_base)
            first_base.set(qn("w:val"), "OrtoCycleTwo")
            second_base = second.find(qn("w:basedOn"))
            if second_base is None:
                second_base = OxmlElement("w:basedOn")
                second.append(second_base)
            second_base.set(qn("w:val"), "OrtoCycleOne")

        circular = replace_package_xml(make_docx(create_circular_styles), "word/styles.xml", make_cycle)
        self.assert_rejected_with(circular, "cyclic_style_inheritance")

    def test_internal_line_break_is_distinct_from_paragraph_end(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("Linia unu")
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("Linia doi")
            document.add_paragraph("Paragraf nou")

        rich_document, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.paragraph_count, 2)
        self.assertEqual(
            rich_document.paragraphs[0].inlines,
            [TextInline("Linia unu"), LineBreakInline(), TextInline("Linia doi")],
        )
        self.assertEqual(rich_document.paragraphs[1].inlines, [TextInline("Paragraf nou")])

    def test_non_content_proofing_markup_is_reported_as_warning(self) -> None:
        def add_proofing_marker(root):
            paragraph = root.find(".//" + qn("w:p"))
            marker = OxmlElement("w:proofErr")
            marker.set(qn("w:type"), "spellStart")
            paragraph.insert(0, marker)

        data = replace_document_xml(
            make_docx(lambda document: document.add_paragraph("text păstrat")),
            add_proofing_marker,
        )
        rich_document, report, _ = parse_docx_bytes(data)

        self.assertEqual(report.status, "valid_with_warnings")
        self.assertEqual(rich_document.paragraphs[0].inlines, [TextInline("text păstrat")])
        self.assertEqual(report.warnings[0].code, "proofing_annotation")

    def test_romanian_language_metadata_is_warned_but_visible_text_is_preserved(self) -> None:
        from lxml import etree

        source = make_docx(lambda document: document.add_paragraph("text românesc"))
        with ZipFile(BytesIO(source), "r") as archive:
            styles_xml = archive.read("word/styles.xml")
        styles_root = etree.fromstring(styles_xml)
        default_rpr = styles_root.find(".//" + qn("w:rPrDefault") + "/" + qn("w:rPr"))
        self.assertIsNotNone(default_rpr)
        language = default_rpr.find(qn("w:lang"))
        if language is None:
            language = OxmlElement("w:lang")
            default_rpr.append(language)
        language.set(qn("w:val"), "ro-RO")
        localized = rewrite_package(
            source,
            {
                "word/styles.xml": etree.tostring(
                    styles_root,
                    xml_declaration=True,
                    encoding="UTF-8",
                    standalone=True,
                )
            },
        )

        rich_document, report, _ = parse_docx_bytes(localized)

        self.assertEqual(report.status, "valid_with_warnings")
        self.assertEqual(rich_document.paragraphs[0].inlines, [TextInline("text românesc")])
        self.assertIn("language_metadata_ignored", {issue.code for issue in report.warnings})

    def test_variable_is_found_and_repeated_occurrences_are_counted(self) -> None:
        rich_document, report, _ = parse_docx_bytes(
            make_docx(lambda document: document.add_paragraph("{{data_internarii}} / {{data_internarii}}"))
        )

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.variables, {"data_internarii": 2})
        self.assertEqual(rich_document.paragraphs[0].inlines[0].text, "{{data_internarii}} / {{data_internarii}}")

    def test_variable_split_across_runs_with_same_style_is_accepted(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("{{data_")
            paragraph.add_run("internarii}}")

        _, report, _ = parse_docx_bytes(make_docx(build))

        self.assertEqual(report.status, "valid")
        self.assertEqual(report.variables, {"data_internarii": 1})

    def test_variable_split_across_runs_with_mixed_styles_is_rejected(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("{{data_").bold = True
            paragraph.add_run("internarii}}")

        self.assert_rejected_with(make_docx(build), "variable_mixed_formatting")

    def test_incomplete_and_invalid_variable_tokens_are_rejected(self) -> None:
        for text, code in (
            ("Valoare {{data", "incomplete_variable_token"),
            ("{{data-invalida}}", "invalid_variable_key"),
            ("{{Data}}", "invalid_variable_key"),
            ("închidere }}", "unmatched_variable_close"),
        ):
            with self.subTest(text=text):
                self.assert_rejected_with(
                    make_docx(lambda document: document.add_paragraph(text)),
                    code,
                )

    def test_single_braces_are_literal_and_incomplete_double_delimiters_fail(self) -> None:
        document, report, _ = parse_docx_bytes(
            make_docx(lambda d: d.add_paragraph("{literal} { and } text"))
        )
        self.assertEqual(report.status, "valid")
        self.assertEqual(report.variables, {})
        self.assertEqual(document.paragraphs[0].inlines, [TextInline("{literal} { and } text")])
        self.assert_rejected_with(make_docx(lambda d: d.add_paragraph("început {{variabila")), "incomplete_variable_token")
        self.assert_rejected_with(make_docx(lambda d: d.add_paragraph("variabila}} final")), "unmatched_variable_close")

    def test_variable_crossing_paragraphs_is_rejected(self) -> None:
        def build(document):
            document.add_paragraph("{{data_")
            document.add_paragraph("internarii}}")

        _, report, _ = self.assert_rejected_with(make_docx(build), "variable_crosses_paragraphs")
        issue = next(issue for issue in report.variable_errors if issue.code == "variable_crosses_paragraphs")
        self.assertEqual(issue.paragraph_index, 1)

    def test_variable_crossing_line_break_is_rejected(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("{{data_")
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("internarii}}")

        self.assert_rejected_with(make_docx(build), "variable_crosses_line_break")

    def test_table_is_rejected(self) -> None:
        def build(document):
            document.add_paragraph("Înainte")
            document.add_table(rows=1, cols=1).cell(0, 0).text = "Tabel"

        self.assert_rejected_with(make_docx(build), "unsupported_tbl")

    def test_image_is_rejected(self) -> None:
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/S5sAAAAASUVORK5CYII="
        )

        def build(document):
            document.add_paragraph().add_run().add_picture(BytesIO(image))

        _, report, _ = self.assert_rejected_with(make_docx(build), "unsupported_drawing")
        self.assertTrue(any("Imaginile" in issue.message for issue in report.unsupported_elements))

    def test_hyperlink_is_rejected(self) -> None:
        def build(document):
            paragraph = document.add_paragraph("Înainte")
            hyperlink = OxmlElement("w:hyperlink")
            run = OxmlElement("w:r")
            text = OxmlElement("w:t")
            text.text = "link"
            run.append(text)
            hyperlink.append(run)
            paragraph._p.append(hyperlink)

        self.assert_rejected_with(make_docx(build), "unsupported_hyperlink")

    def test_tab_is_rejected(self) -> None:
        def build(document):
            document.add_paragraph().add_run().add_tab()

        self.assert_rejected_with(make_docx(build), "tab_character")

    def test_hidden_text_is_rejected(self) -> None:
        def hidden_with(property_name: str) -> bytes:
            def mutate(root):
                run = root.find(".//" + qn("w:r"))
                rpr = run.find(qn("w:rPr"))
                if rpr is None:
                    rpr = OxmlElement("w:rPr")
                    run.insert(0, rpr)
                hidden = OxmlElement(f"w:{property_name}")
                rpr.append(hidden)

            return replace_document_xml(make_docx(lambda d: d.add_paragraph("ascuns")), mutate)

        self.assert_rejected_with(hidden_with("vanish"), "hidden_text")
        self.assert_rejected_with(hidden_with("webHidden"), "hidden_text")
        self.assert_rejected_with(hidden_with("specVanish"), "hidden_text")

    def test_superscript_and_subscript_are_rejected(self) -> None:
        for attribute in ("superscript", "subscript"):
            def build(document, current=attribute):
                setattr(document.add_paragraph().add_run("text").font, current, True)

            with self.subTest(attribute=attribute):
                self.assert_rejected_with(
                    make_docx(build),
                    "superscript_or_subscript",
                )

    def test_strikethrough_is_rejected(self) -> None:
        def build(document):
            document.add_paragraph().add_run("barat").font.strike = True

        self.assert_rejected_with(make_docx(build), "strikethrough")

    def test_color_and_highlight_are_rejected(self) -> None:
        def colored(document):
            document.add_paragraph().add_run("colorat").font.color.rgb = RGBColor(255, 0, 0)

        def highlighted(document):
            document.add_paragraph().add_run("marcat").font.highlight_color = WD_COLOR_INDEX.YELLOW

        self.assert_rejected_with(make_docx(colored), "font_color")
        self.assert_rejected_with(make_docx(highlighted), "highlight")

    def test_colored_and_theme_underlines_are_rejected(self) -> None:
        for attribute, value in (
            ("color", "FF0000"),
            ("themeColor", "accent1"),
            ("themeTint", "99"),
            ("themeShade", "80"),
        ):
            def mutate(root, attr=attribute, attr_value=value):
                run = root.find(".//" + qn("w:r"))
                rpr = run.find(qn("w:rPr"))
                if rpr is None:
                    rpr = OxmlElement("w:rPr")
                    run.insert(0, rpr)
                underline = OxmlElement("w:u")
                underline.set(qn("w:val"), "single")
                underline.set(qn(f"w:{attr}"), attr_value)
                rpr.append(underline)

            with self.subTest(attribute=attribute):
                self.assert_rejected_with(
                    replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), mutate),
                    "unsupported_underline_properties",
                )

    def test_theme_font_colors_are_rejected_directly_and_through_styles(self) -> None:
        for attribute, value in (("themeColor", "accent1"), ("themeTint", "99"), ("themeShade", "80")):
            def direct(root, attr=attribute, attr_value=value):
                run = root.find(".//" + qn("w:r"))
                rpr = run.find(qn("w:rPr"))
                if rpr is None:
                    rpr = OxmlElement("w:rPr")
                    run.insert(0, rpr)
                color = OxmlElement("w:color")
                color.set(qn("w:val"), "auto")
                color.set(qn(f"w:{attr}"), attr_value)
                rpr.append(color)

            with self.subTest(layer="direct", attribute=attribute):
                data = replace_document_xml(make_docx(lambda d: d.add_paragraph("direct")), direct)
                self.assert_rejected_with(data, "font_color")

        def inherited(document):
            style = document.styles.add_style("Orto Theme Color", WD_STYLE_TYPE.CHARACTER)
            color = OxmlElement("w:color")
            color.set(qn("w:val"), "auto")
            color.set(qn("w:themeColor"), "accent2")
            style._element.get_or_add_rPr().append(color)
            run = document.add_paragraph().add_run("moștenit")
            run.style = style

        self.assert_rejected_with(make_docx(inherited), "font_color")

        def inherited_underline(document):
            style = document.styles.add_style("Orto Theme Underline", WD_STYLE_TYPE.CHARACTER)
            underline = OxmlElement("w:u")
            underline.set(qn("w:val"), "single")
            underline.set(qn("w:themeColor"), "accent2")
            style._element.get_or_add_rPr().append(underline)
            run = document.add_paragraph().add_run("moștenit")
            run.style = style

        self.assert_rejected_with(make_docx(inherited_underline), "unsupported_underline_properties")

    def test_other_font_properties_are_rejected(self) -> None:
        for prop_name in ("spacing", "kern", "smallCaps"):
            def mutate(root, element_name=prop_name):
                run = root.find(".//" + qn("w:r"))
                rpr = run.find(qn("w:rPr"))
                if rpr is None:
                    rpr = OxmlElement("w:rPr")
                    run.insert(0, rpr)
                el = OxmlElement(f"w:{element_name}")
                if element_name in {"spacing", "kern"}:
                    el.set(qn("w:val"), "20")
                rpr.append(el)

            with self.subTest(property=prop_name):
                data = replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), mutate)
                self.assert_rejected_with(data, "unsupported_font_property")

        def invalid_rfonts_attr(root):
            run = root.find(".//" + qn("w:r"))
            rpr = OxmlElement("w:rPr")
            rfonts = OxmlElement("w:rFonts")
            rfonts.set(qn("w:unknownFontSlot"), "Arial")
            rpr.append(rfonts)
            run.insert(0, rpr)

        self.assert_rejected_with(
            replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), invalid_rfonts_attr),
            "unsupported_font_property",
        )

        def invalid_sz_value(root):
            run = root.find(".//" + qn("w:r"))
            rpr = OxmlElement("w:rPr")
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), "0")
            rpr.append(sz)
            run.insert(0, rpr)

        self.assert_rejected_with(
            replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), invalid_sz_value),
            "unsupported_font_property",
        )

    def test_non_simple_underline_is_rejected(self) -> None:
        from docx.enum.text import WD_UNDERLINE

        def build(document):
            document.add_paragraph().add_run("dublu").underline = WD_UNDERLINE.DOUBLE

        self.assert_rejected_with(make_docx(build), "unsupported_underline_style")

    def test_header_or_footer_text_is_rejected(self) -> None:
        def build(document):
            document.sections[0].header.paragraphs[0].text = "Antet"

        self.assert_rejected_with(make_docx(build), "header_footer_content")

    def test_comment_and_tracked_changes_are_rejected(self) -> None:
        def add_comment_marker(root):
            paragraph = root.find(".//" + qn("w:p"))
            start = OxmlElement("w:commentRangeStart")
            start.set(qn("w:id"), "0")
            paragraph.insert(0, start)

        commented = replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), add_comment_marker)
        self.assert_rejected_with(commented, "unsupported_commentRangeStart")

        comment_part = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:comment w:id="0"><w:p><w:r><w:t>comentariu</w:t></w:r></w:p></w:comment>'
            b'</w:comments>'
        )
        comments_document = rewrite_package(
            make_docx(lambda d: d.add_paragraph("text")),
            {"word/comments.xml": comment_part},
        )
        self.assert_rejected_with(comments_document, "comments_part")

        def wrap_run_in_insertion(root):
            paragraph = root.find(".//" + qn("w:p"))
            run = paragraph.find(qn("w:r"))
            insertion = OxmlElement("w:ins")
            insertion.set(qn("w:id"), "1")
            insertion.set(qn("w:author"), "test")
            paragraph.replace(run, insertion)
            insertion.append(run)

        inserted = replace_document_xml(
            make_docx(lambda d: d.add_paragraph("text")),
            wrap_run_in_insertion,
        )
        self.assert_rejected_with(inserted, "unsupported_ins")

        base = make_docx(lambda d: d.add_paragraph("text"))
        with ZipFile(BytesIO(base), "r") as archive:
            settings_xml = archive.read("word/settings.xml")
        from lxml import etree

        settings_root = etree.fromstring(settings_xml)
        settings_root.append(OxmlElement("w:trackRevisions"))
        tracked = rewrite_package(
            base,
            {
                "word/settings.xml": etree.tostring(
                    settings_root,
                    xml_declaration=True,
                    encoding="UTF-8",
                    standalone=True,
                )
            },
        )
        self.assert_rejected_with(tracked, "tracked_changes_enabled")

    def test_page_break_numbered_list_and_multiple_columns_are_rejected(self) -> None:
        def page_break(document):
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

        self.assert_rejected_with(make_docx(page_break), "page_or_column_break")

        def numbered(document):
            paragraph = document.add_paragraph("element")
            paragraph.style = "List Number"

        self.assert_rejected_with(make_docx(numbered), "list_paragraph_style")

        def columns(root):
            cols = root.find(".//" + qn("w:sectPr") + "/" + qn("w:cols"))
            cols.set(qn("w:num"), "2")

        multi_column = replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), columns)
        self.assert_rejected_with(multi_column, "multiple_columns")

    def test_final_single_section_is_accepted_but_section_break_is_rejected(self) -> None:
        ordinary = make_docx(lambda document: document.add_paragraph("text"))
        _, ordinary_report, _ = parse_docx_bytes(ordinary)
        self.assertEqual(ordinary_report.status, "valid")

        def add_section_break(root):
            paragraph = root.find(".//" + qn("w:p"))
            ppr = paragraph.find(qn("w:pPr"))
            if ppr is None:
                ppr = OxmlElement("w:pPr")
                paragraph.insert(0, ppr)
            section = OxmlElement("w:sectPr")
            section_type = OxmlElement("w:type")
            section_type.set(qn("w:val"), "nextPage")
            section.append(section_type)
            ppr.append(section)

        self.assert_rejected_with(
            replace_document_xml(ordinary, add_section_break),
            "section_break",
        )

    def test_unsupported_paragraph_defaults_and_section_properties_are_rejected(self) -> None:
        def add_default_keep_next(root):
            defaults = root.find(".//" + qn("w:pPrDefault") + "/" + qn("w:pPr"))
            defaults.append(OxmlElement("w:keepNext"))

        paragraph_defaults = replace_package_xml(
            make_docx(lambda d: d.add_paragraph("text")),
            "word/styles.xml",
            add_default_keep_next,
        )
        self.assert_rejected_with(paragraph_defaults, "unsupported_paragraph_formatting")

        def add_page_borders(root):
            section = root.find(".//" + qn("w:sectPr"))
            section.append(OxmlElement("w:pgBorders"))

        custom_section = replace_document_xml(
            make_docx(lambda d: d.add_paragraph("text")),
            add_page_borders,
        )
        self.assert_rejected_with(custom_section, "unsupported_section_property")

    def test_embedded_object_and_text_box_are_rejected(self) -> None:
        def add_shapes(root):
            paragraph = root.find(".//" + qn("w:p"))
            run = OxmlElement("w:r")
            drawing = OxmlElement("w:drawing")
            shape = OxmlElement("w:txbxContent")
            drawing.append(shape)
            run.append(drawing)
            paragraph.append(run)
            object_run = OxmlElement("w:r")
            object_run.append(OxmlElement("w:object"))
            paragraph.append(object_run)

        data = replace_document_xml(make_docx(lambda d: d.add_paragraph("text")), add_shapes)
        _, report, _ = self.assert_rejected_with(data, "unsupported_txbxContent")
        codes = {issue.code for issue in report.unsupported_elements}
        self.assertIn("unsupported_object", codes)

    def test_multiple_incompatible_elements_are_all_reported(self) -> None:
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("tab").add_tab()
            paragraph.add_run("color").font.color.rgb = RGBColor(0, 0, 255)
            document.add_table(rows=1, cols=1)

        _, report, _ = self.assert_rejected_with(make_docx(build), "unsupported_tbl")
        self.assertIn("tab_character", {issue.code for issue in report.unsupported_elements})
        self.assertIn("font_color", {issue.code for issue in report.formatting_errors})

    def test_invalid_zip_and_corrupt_xml_have_separate_report_categories(self) -> None:
        _, invalid_archive_report, _ = self.assert_rejected_with(b"nu este DOCX", "docx_archive_error")
        self.assertEqual(len(invalid_archive_report.archive_errors), 1)
        broken = rewrite_package(
            make_docx(lambda document: document.add_paragraph("text")),
            {"word/document.xml": b"<w:document"},
        )
        _, xml_report, _ = self.assert_rejected_with(broken, "docx_xml_error")
        self.assertEqual(len(xml_report.xml_errors), 1)

        def change_document_root(root):
            root.tag = "{urn:invalid}document"

        malformed_ooxml = replace_document_xml(
            make_docx(lambda document: document.add_paragraph("text")),
            change_document_root,
        )
        self.assert_rejected_with(malformed_ooxml, "docx_structure_error")

    def test_archive_limits_duplicate_names_encryption_and_crc_are_rejected(self) -> None:
        too_large_input = b"x" * (MAX_DOCX_COMPRESSED_BYTES + 1)
        self.assert_rejected_with(too_large_input, "resource_limit_exceeded")

        many_entries = make_zip([(f"part-{index}.bin", b"") for index in range(MAX_ZIP_COMPONENTS + 1)])
        self.assert_rejected_with(many_entries, "resource_limit_exceeded")

        oversized_component = make_zip(
            [
                ("[Content_Types].xml", b""),
                ("word/document.xml", b""),
                ("large.bin", b"0" * (MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES + 1)),
            ]
        )
        self.assert_rejected_with(oversized_component, "resource_limit_exceeded")

        aggregate_parts = [
            ("[Content_Types].xml", b""),
            ("word/document.xml", b""),
            *[
            (f"part-{index}.bin", b"0" * (9 * 1024 * 1024))
            for index in range(6)
            ],
        ]
        self.assertGreater(sum(len(value) for _, value in aggregate_parts), MAX_DOCX_UNCOMPRESSED_BYTES)
        self.assert_rejected_with(make_zip(aggregate_parts), "resource_limit_exceeded")

        source = make_docx(lambda document: document.add_paragraph("text"))
        with ZipFile(BytesIO(source), "r") as original:
            entries = [(item.filename, original.read(item)) for item in original.infolist()]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = make_zip(entries + [("word/document.xml", dict(entries)["word/document.xml"])])
        self.assert_rejected_with(duplicate, "docx_archive_error")

        encrypted = alter_zip_flags(source, "word/document.xml", 0x1)
        _, encrypted_report, _ = self.assert_rejected_with(encrypted, "docx_archive_error")
        self.assertIn("criptată", encrypted_report.archive_errors[0].message)

        damaged_crc = corrupt_central_crc(source, "word/document.xml")
        _, crc_report, _ = self.assert_rejected_with(damaged_crc, "docx_archive_error")
        self.assertIn("CRC", crc_report.archive_errors[0].message)

    def test_unexpected_parser_errors_propagate_as_programming_errors(self) -> None:
        with patch("ortodocs.services.docx_import._parse_paragraph", side_effect=RuntimeError("bug intern")):
            with self.assertRaisesRegex(RuntimeError, "bug intern"):
                parse_docx_bytes(make_docx(lambda document: document.add_paragraph("text")))

    def test_external_xml_entities_are_rejected_without_resolution(self) -> None:
        malicious_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<!DOCTYPE w:document [<!ENTITY ext SYSTEM "https://example.invalid/data">]>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body><w:p><w:r><w:t>&ext;</w:t></w:r></w:p></w:body></w:document>'
        )
        data = rewrite_package(
            make_docx(lambda document: document.add_paragraph("text")),
            {"word/document.xml": malicious_xml},
        )

        _, report, _ = self.assert_rejected_with(data, "docx_xml_error")
        self.assertIn("DOCTYPE nu este permis", report.xml_errors[0].message)

    def test_control_characters_in_word_text_are_reported_as_content_errors(self) -> None:
        for character in ("\t", "\n", "\u2028"):
            def add_control(root, value=character):
                text = root.find(".//" + qn("w:t"))
                text.text = f"before{value}after"

            data = replace_document_xml(
                make_docx(lambda document: document.add_paragraph("text")),
                add_control,
            )
            with self.subTest(character=repr(character)):
                _, report, _ = self.assert_rejected_with(data, "invalid_text_control")
                self.assertTrue(report.content_errors)

    def test_non_bytes_input_is_rejected_as_programming_error(self) -> None:
        with self.assertRaises(TypeError):
            parse_docx_bytes("not bytes")  # type: ignore[arg-type]


class ChatGptClipboardAndFontSupportTests(unittest.TestCase):
    def _build_chatgpt_pasted_docx(self) -> bytes:
        """Reproduce exact OOXML-ul observat în Word după copy-paste din ChatGPT."""

        def build(document):
            document.add_paragraph("Text scris manual {{varsta}}")
            html_code = document.styles.add_style("HTMLCode", WD_STYLE_TYPE.CHARACTER)
            html_code.base_style = document.styles["Default Paragraph Font"]
            html_rpr = html_code._element.get_or_add_rPr()
            style_rfonts = OxmlElement("w:rFonts")
            style_rfonts.set(qn("w:ascii"), "Courier New")
            style_rfonts.set(qn("w:eastAsia"), "Times New Roman")
            style_rfonts.set(qn("w:hAnsi"), "Courier New")
            style_rfonts.set(qn("w:cs"), "Courier New")
            html_rpr.append(style_rfonts)
            style_sz = OxmlElement("w:sz")
            style_sz.set(qn("w:val"), "20")
            html_rpr.append(style_sz)
            style_sz_cs = OxmlElement("w:szCs")
            style_sz_cs.set(qn("w:val"), "20")
            html_rpr.append(style_sz_cs)

            p2 = document.add_paragraph()
            run_pasted = p2.add_run("Text lipit din ChatGPT ")
            run_pasted.style = html_code
            direct_rpr = run_pasted._r.get_or_add_rPr()
            direct_rfonts = OxmlElement("w:rFonts")
            direct_rfonts.set(qn("w:eastAsiaTheme"), "minorEastAsia")
            direct_rpr.append(direct_rfonts)

            run_plain_paste = p2.add_run("Keep Text Only")
            self.assertIsNotNone(run_plain_paste)

        return make_docx(build)

    def test_three_word_situations_manual_chatgpt_paste_and_keep_text_only(self) -> None:
        # 1. Text introdus manual în Word
        manual_doc, manual_report, _ = parse_docx_bytes(
            make_docx(lambda d: d.add_paragraph("Text manual {{sex}}"))
        )
        self.assertEqual(manual_report.status, "valid")
        self.assertEqual(manual_doc.paragraphs[0].inlines, [TextInline("Text manual {{sex}}")])
        self.assertEqual(
            manual_doc.paragraphs[0].inlines[0].to_dict(),
            {
                "type": "text",
                "text": "Text manual {{sex}}",
                "bold": False,
                "italic": False,
                "underline": False,
            },
        )

        # 2 & 3. Text lipit normal din ChatGPT (HTMLCode + eastAsiaTheme) + Paste Keep Text Only
        pasted_docx = self._build_chatgpt_pasted_docx()
        pasted_doc, pasted_report, _ = parse_docx_bytes(pasted_docx)
        self.assertEqual(pasted_report.status, "valid")
        self.assertEqual(pasted_report.warnings, [])
        self.assertEqual(
            pasted_doc.paragraphs[1].inlines[0],
            TextInline(
                "Text lipit din ChatGPT ",
                r_fonts={
                    "ascii": "Courier New",
                    "hAnsi": "Courier New",
                    "cs": "Courier New",
                    "eastAsiaTheme": "minorEastAsia",
                },
                font_size_half_points=20,
                complex_font_size_half_points=20,
            ),
        )
        self.assertEqual(
            pasted_doc.paragraphs[1].inlines[1],
            TextInline("Keep Text Only"),
        )

    def test_rfonts_all_attributes_sz_szcs_and_bcs_ics_supported(self) -> None:
        from docx.shared import Pt

        def build(document):
            p = document.add_paragraph()
            r1 = p.add_run("Font complet")
            r1.font.size = Pt(14)
            r1.bold = True
            r1.italic = True
            r1.underline = True
            rpr = r1._r.get_or_add_rPr()
            rfonts = OxmlElement("w:rFonts")
            rfonts.set(qn("w:ascii"), "Arial")
            rfonts.set(qn("w:hAnsi"), "Arial")
            rfonts.set(qn("w:eastAsia"), "SimSun")
            rfonts.set(qn("w:cs"), "Times New Roman")
            rfonts.set(qn("w:hint"), "default")
            rpr.append(rfonts)
            rpr.append(OxmlElement("w:bCs"))
            rpr.append(OxmlElement("w:iCs"))
            sz_cs = OxmlElement("w:szCs")
            sz_cs.set(qn("w:val"), "30")
            rpr.append(sz_cs)

            r2 = p.add_run(" | Font tematic")
            rpr2 = r2._r.get_or_add_rPr()
            rfonts2 = OxmlElement("w:rFonts")
            rfonts2.set(qn("w:asciiTheme"), "majorHAnsi")
            rfonts2.set(qn("w:cstheme"), "majorBidi")
            rpr2.append(rfonts2)

        parsed, report, _ = parse_docx_bytes(make_docx(build))
        self.assertEqual(report.status, "valid")
        self.assertEqual(len(parsed.paragraphs[0].inlines), 2)
        first, second = parsed.paragraphs[0].inlines
        self.assertEqual(
            first,
            TextInline(
                "Font complet",
                bold=True,
                italic=True,
                underline=True,
                r_fonts={
                    "ascii": "Arial",
                    "hAnsi": "Arial",
                    "eastAsia": "SimSun",
                    "cs": "Times New Roman",
                    "hint": "default",
                },
                font_size_half_points=28,
                complex_font_size_half_points=30,
            ),
        )
        self.assertEqual(
            second,
            TextInline(
                " | Font tematic",
                r_fonts={"asciiTheme": "majorHAnsi", "cstheme": "majorBidi"},
            ),
        )

    def test_mixed_font_or_size_across_variable_is_rejected(self) -> None:
        from docx.shared import Pt

        def build(document):
            p = document.add_paragraph()
            p.add_run("{{data_").font.size = Pt(12)
            p.add_run("operatiei}}").font.size = Pt(14)

        _, report, _ = parse_docx_bytes(make_docx(build))
        self.assertEqual(report.status, "rejected")
        self.assertIn("variable_mixed_formatting", [i.code for i in report.formatting_errors])

    def test_inherited_font_properties_parsed_json_and_docx_regeneration(self) -> None:
        from lxml import etree
        from ortodocs.services.docx_export import export_rich_document_to_docx_bytes

        def build(document):
            base = document.styles.add_style("Orto Font Base", WD_STYLE_TYPE.PARAGRAPH)
            base_rpr = base._element.get_or_add_rPr()
            rf = OxmlElement("w:rFonts")
            rf.set(qn("w:ascii"), "Calibri")
            rf.set(qn("w:eastAsia"), "Times New Roman")
            base_rpr.append(rf)
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), "24")
            base_rpr.append(sz)

            derived = document.styles.add_style("Orto Font Derived", WD_STYLE_TYPE.PARAGRAPH)
            derived.base_style = base
            derived.font.bold = True

            p = document.add_paragraph()
            p.style = derived
            r_inherited = p.add_run("Moștenit ")
            self.assertIsNotNone(r_inherited)
            r_override = p.add_run("Suprascris")
            r_override.italic = True
            r_override.underline = True
            r_override_rpr = r_override._r.get_or_add_rPr()
            rf_ov = OxmlElement("w:rFonts")
            rf_ov.set(qn("w:ascii"), "Courier New")
            r_override_rpr.append(rf_ov)
            sz_cs = OxmlElement("w:szCs")
            sz_cs.set(qn("w:val"), "26")
            r_override_rpr.append(sz_cs)

        original_doc, report, _ = parse_docx_bytes(make_docx(build))
        self.assertEqual(report.status, "valid")

        # Serializare și deserializare prin parsed_content_json
        raw_json = original_doc.to_json()
        decoded_doc = RichDocument.from_json(raw_json)
        self.assertEqual(decoded_doc, original_doc)

        # Regenerare DOCX și comparare OOXML + re-parsare
        regenerated_bytes = export_rich_document_to_docx_bytes(decoded_doc)
        with ZipFile(BytesIO(regenerated_bytes), "r") as archive:
            doc_xml = etree.fromstring(archive.read("word/document.xml"))
        runs = doc_xml.findall(".//" + qn("w:r"))
        self.assertEqual(len(runs), 2)
        first_rpr = runs[0].find(qn("w:rPr"))
        second_rpr = runs[1].find(qn("w:rPr"))
        self.assertEqual(first_rpr.find(qn("w:rFonts")).get(qn("w:ascii")), "Calibri")
        self.assertEqual(first_rpr.find(qn("w:rFonts")).get(qn("w:eastAsia")), "Times New Roman")
        self.assertEqual(first_rpr.find(qn("w:sz")).get(qn("w:val")), "24")
        self.assertIsNotNone(first_rpr.find(qn("w:b")))

        self.assertEqual(second_rpr.find(qn("w:rFonts")).get(qn("w:ascii")), "Courier New")
        self.assertEqual(second_rpr.find(qn("w:sz")).get(qn("w:val")), "24")
        self.assertEqual(second_rpr.find(qn("w:szCs")).get(qn("w:val")), "26")
        self.assertIsNotNone(second_rpr.find(qn("w:i")))
        self.assertIsNotNone(second_rpr.find(qn("w:u")))

        reparsed_doc, reparsed_report, _ = parse_docx_bytes(regenerated_bytes)
        self.assertEqual(reparsed_report.status, "valid")
        self.assertEqual(reparsed_doc, original_doc)


if __name__ == "__main__":
    unittest.main()
