"""Export DOCX fidel pornind de la modelul RichDocument OrtoDocs."""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from ortodocs.models.rich_document import (
    LineBreakInline,
    RichDocument,
    TextInline,
)


def _apply_text_inline_formatting(run: object, inline: TextInline) -> None:
    """Scrie proprietățile OOXML acceptate în ordine canonică CT_RPr."""

    has_rfonts = inline.r_fonts is not None
    has_size = inline.font_size_half_points is not None
    has_size_cs = inline.complex_font_size_half_points is not None
    if not (inline.bold or inline.italic or inline.underline or has_rfonts or has_size or has_size_cs):
        return

    rpr = run._r.get_or_add_rPr()  # type: ignore[attr-defined]
    if has_rfonts and inline.r_fonts is not None:
        rfonts = OxmlElement("w:rFonts")
        for attr_key, attr_value in inline.r_fonts.items():
            rfonts.set(qn(f"w:{attr_key}"), attr_value)
        rpr.append(rfonts)

    if inline.bold:
        rpr.append(OxmlElement("w:b"))
    if inline.italic:
        rpr.append(OxmlElement("w:i"))
    if inline.underline:
        underline_el = OxmlElement("w:u")
        underline_el.set(qn("w:val"), "single")
        rpr.append(underline_el)

    if has_size:
        sz_el = OxmlElement("w:sz")
        sz_el.set(qn("w:val"), str(inline.font_size_half_points))
        rpr.append(sz_el)
    if has_size_cs:
        sz_cs_el = OxmlElement("w:szCs")
        sz_cs_el.set(qn("w:val"), str(inline.complex_font_size_half_points))
        rpr.append(sz_cs_el)


def export_rich_document_to_docx_bytes(document: RichDocument) -> bytes:
    """Generează un pachet DOCX OOXML valid păstrând formatarea RichDocument."""

    if not isinstance(document, RichDocument):
        raise TypeError("document trebuie să fie o instanță RichDocument.")
    snapshot = RichDocument.from_dict(document.to_dict())

    word_doc = Document()
    for paragraph in snapshot.paragraphs:
        word_paragraph = word_doc.add_paragraph()
        for inline in paragraph.inlines:
            if isinstance(inline, TextInline):
                run = word_paragraph.add_run(inline.text)
                _apply_text_inline_formatting(run, inline)
            elif isinstance(inline, LineBreakInline):
                word_paragraph.add_run().add_break(WD_BREAK.LINE)

    stream = BytesIO()
    word_doc.save(stream)
    return stream.getvalue()


__all__ = ["export_rich_document_to_docx_bytes"]
