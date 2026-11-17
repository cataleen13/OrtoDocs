"""Import DOCX offline în modelul Rich Text restrâns al aplicației."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import zlib
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile, LargeZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from lxml import etree

from ortodocs.models.import_report import ImportIssue, ImportReport
from ortodocs.models.rich_document import (
    MAX_FONT_SIZE_HALF_POINTS,
    VALID_RFONTS_KEYS,
    LineBreakInline,
    Paragraph,
    RichDocument,
    RichInline,
    TextInline,
)


_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_W = f"{{{_WORD_NS}}}"
_VARIABLE_KEY = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_TRUE_VALUES = {"1", "true", "on"}
_FALSE_VALUES = {"0", "false", "off"}

DOCX_PARSER_VERSION = "1.0.0"
MAX_DOCX_COMPRESSED_BYTES = 10 * 1024 * 1024
MAX_ZIP_COMPONENTS = 500
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES = 10 * 1024 * 1024

_UNSUPPORTED_TAG_MESSAGES = {
    _W + "tbl": "Tabelele nu sunt acceptate.",
    _W + "drawing": "Imaginile și obiectele grafice nu sunt acceptate.",
    _W + "pict": "Imaginile și obiectele VML nu sunt acceptate.",
    _W + "object": "Obiectele încorporate nu sunt acceptate.",
    _W + "control": "Controalele încorporate nu sunt acceptate.",
    _W + "altChunk": "Conținutul Word importat extern nu este acceptat.",
    _W + "hyperlink": "Hyperlinkurile nu sunt acceptate.",
    _W + "txbxContent": "Casetele de text nu sunt acceptate.",
    _W + "sdt": "Controalele de conținut Word nu sunt acceptate.",
    _W + "fldSimple": "Câmpurile Word nu sunt acceptate.",
    _W + "fldChar": "Câmpurile Word nu sunt acceptate.",
    _W + "instrText": "Câmpurile Word nu sunt acceptate.",
    _W + "ins": "Modificările urmărite nu sunt acceptate.",
    _W + "del": "Modificările urmărite nu sunt acceptate.",
    _W + "moveFrom": "Modificările urmărite nu sunt acceptate.",
    _W + "moveTo": "Modificările urmărite nu sunt acceptate.",
    _W + "moveFromRangeStart": "Modificările urmărite nu sunt acceptate.",
    _W + "moveFromRangeEnd": "Modificările urmărite nu sunt acceptate.",
    _W + "moveToRangeStart": "Modificările urmărite nu sunt acceptate.",
    _W + "moveToRangeEnd": "Modificările urmărite nu sunt acceptate.",
    _W + "commentRangeStart": "Comentariile Word nu sunt acceptate.",
    _W + "commentRangeEnd": "Comentariile Word nu sunt acceptate.",
    _W + "commentReference": "Comentariile Word nu sunt acceptate.",
    _W + "footnoteReference": "Notele de subsol nu sunt acceptate.",
    _W + "endnoteReference": "Notele de final nu sunt acceptate.",
    _W + "subDoc": "Documentele încorporate nu sunt acceptate.",
    _W + "customXml": "Conținutul XML personalizat nu este acceptat.",
    _W + "smartTag": "Marcajele semantice Word nu sunt acceptate.",
    _W + "bookmarkStart": "Marcajele Word nu pot fi păstrate în Rich Text.",
    _W + "bookmarkEnd": "Marcajele Word nu pot fi păstrate în Rich Text.",
    _W + "permStart": "Marcajele de permisiuni Word nu sunt acceptate.",
    _W + "permEnd": "Marcajele de permisiuni Word nu sunt acceptate.",
    _W + "lastRenderedPageBreak": "Întreruperile de pagină Word nu sunt acceptate.",
}

_STANDARD_DEFAULT_RPR = {
    "rFonts": {
        "asciiTheme": "minorHAnsi",
        "eastAsiaTheme": "minorEastAsia",
        "hAnsiTheme": "minorHAnsi",
        "cstheme": "minorBidi",
    },
    "sz": {"val": "22"},
    "szCs": {"val": "22"},
    "lang": {"val": "en-US", "eastAsia": "en-US", "bidi": "ar-SA"},
}

_STANDARD_DEFAULT_PPR_SPACING = {
    "after": "200",
    "line": "276",
    "lineRule": "auto",
}
_VALID_RFONTS_ATTRS = frozenset(VALID_RFONTS_KEYS)
_VALID_RFONTS_HINTS = frozenset({"default", "eastAsia", "cs"})
_RFONTS_SLOT_PAIRS = (
    ("ascii", "asciiTheme"),
    ("hAnsi", "hAnsiTheme"),
    ("eastAsia", "eastAsiaTheme"),
    ("cs", "cstheme"),
)


class _ExpectedDocxError(Exception):
    """Eroare a fișierului de intrare, nu defect al parserului."""

    issue_code = "docx_error"


class _DocxArchiveError(_ExpectedDocxError):
    issue_code = "docx_archive_error"


class _DocxXmlError(_ExpectedDocxError):
    issue_code = "docx_xml_error"


class _DocxStructureError(_ExpectedDocxError):
    issue_code = "docx_structure_error"


class _DocxResourceLimitError(_ExpectedDocxError):
    issue_code = "resource_limit_exceeded"


@dataclass(slots=True)
class _Style:
    style_id: str
    style_type: str
    based_on: str | None
    name: str
    rpr: etree._Element | None
    ppr: etree._Element | None


@dataclass(slots=True)
class _TokenUnit:
    char: str
    kind: str
    style: tuple[object, ...] | None
    paragraph_index: int
    run_index: int | None = None


def _issue_once(
    target: list[ImportIssue],
    *,
    code: str,
    message: str,
    part: str | None = None,
    paragraph_index: int | None = None,
    run_index: int | None = None,
    element: str | None = None,
) -> None:
    candidate = ImportIssue(
        code=code,
        message=message,
        part=part,
        paragraph_index=paragraph_index,
        run_index=run_index,
        element=element,
    )
    if candidate not in target:
        target.append(candidate)


def _parse_xml(data: bytes, part_name: str) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_comments=True,
        remove_pis=True,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _DocxXmlError(f"XML invalid în componenta {part_name}: {exc}") from exc
    tree = root.getroottree()
    if tree.docinfo.doctype:
        raise _DocxXmlError(f"DOCTYPE nu este permis în partea {part_name}.")
    if any(isinstance(node, etree._Entity) for node in root.iter()):
        raise _DocxXmlError(f"Entitățile XML nu sunt permise în partea {part_name}.")
    return root


def _contains_rich_text_control(text: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"} for character in text)


def _load_package_parts(docx_bytes: bytes) -> dict[str, etree._Element]:
    if len(docx_bytes) > MAX_DOCX_COMPRESSED_BYTES:
        raise _DocxResourceLimitError(
            f"DOCX-ul comprimat depășește limita de {MAX_DOCX_COMPRESSED_BYTES} bytes."
        )

    parts: dict[str, etree._Element] = {}
    try:
        with ZipFile(BytesIO(docx_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_COMPONENTS:
                raise _DocxResourceLimitError(
                    f"Arhiva DOCX are {len(entries)} componente; limita este {MAX_ZIP_COMPONENTS}."
                )

            names = [item.filename for item in entries]
            if len(names) != len(set(names)):
                raise _DocxArchiveError("Arhiva DOCX conține intrări cu nume duplicate.")
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise _DocxStructureError(
                    "Arhiva nu conține componentele obligatorii pentru DOCX."
                )

            uncompressed_total = 0
            for item in entries:
                if item.flag_bits & 0x1:
                    raise _DocxArchiveError(
                        f"Componenta criptată {item.filename} nu este acceptată."
                    )
                if item.file_size > MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES:
                    raise _DocxResourceLimitError(
                        f"Componenta {item.filename} depășește limita decomprimată de "
                        f"{MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES} bytes."
                    )
                uncompressed_total += item.file_size
            if uncompressed_total > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise _DocxResourceLimitError(
                    "Dimensiunea totală decomprimată a arhivei DOCX depășește "
                    f"limita de {MAX_DOCX_UNCOMPRESSED_BYTES} bytes."
                )

            # Verifică CRC-ul fiecărei componente după validarea limitelor,
            # înainte să parseze oricare dintre ele.
            damaged_member = archive.testzip()
            if damaged_member is not None:
                raise _DocxArchiveError(
                    f"Verificarea CRC a eșuat pentru componenta {damaged_member}."
                )

            for item in entries:
                if not (item.filename.endswith(".xml") or item.filename.endswith(".rels")):
                    continue
                with archive.open(item, "r") as component:
                    data = component.read(MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES + 1)
                    if len(data) > MAX_ZIP_COMPONENT_UNCOMPRESSED_BYTES:
                        raise _DocxResourceLimitError(
                            f"Componenta {item.filename} a depășit limita în timpul citirii."
                        )
                    if len(data) != item.file_size:
                        raise _DocxArchiveError(
                            f"Dimensiunea citită pentru componenta {item.filename} nu corespunde arhivei."
                        )
                parts[item.filename] = _parse_xml(data, item.filename)
    except _ExpectedDocxError:
        raise
    except (
        BadZipFile,
        LargeZipFile,
        EOFError,
        RuntimeError,
        OSError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise _DocxArchiveError(f"Arhiva ZIP este deteriorată sau nu poate fi citită: {exc}") from exc
    return parts


def _styles_from_xml(
    styles_root: etree._Element | None,
    report: ImportReport,
) -> tuple[
    dict[str, _Style],
    etree._Element | None,
    etree._Element | None,
    str | None,
    str | None,
]:
    if styles_root is None:
        return {}, None, None, None, None
    styles: dict[str, _Style] = {}
    default_paragraph: str | None = None
    default_character: str | None = None
    for node in styles_root.findall(_W + "style"):
        style_id = node.get(_W + "styleId")
        style_type = node.get(_W + "type", "")
        if not style_id:
            continue
        base = node.find(_W + "basedOn")
        name = node.find(_W + "name")
        if style_id in styles:
            _issue_once(
                report.formatting_errors,
                code="duplicate_style_id",
                message=f"ID-ul de stil Word {style_id!r} apare de mai multe ori.",
                part="word/styles.xml",
                element="w:style",
            )
            continue
        styles[style_id] = _Style(
            style_id=style_id,
            style_type=style_type,
            based_on=base.get(_W + "val") if base is not None else None,
            name=name.get(_W + "val", style_id) if name is not None else style_id,
            rpr=node.find(_W + "rPr"),
            ppr=node.find(_W + "pPr"),
        )
        if node.get(_W + "default") in _TRUE_VALUES:
            if style_type == "paragraph":
                if default_paragraph is not None:
                    _issue_once(
                        report.formatting_errors,
                        code="ambiguous_default_paragraph_style",
                        message="Mai multe stiluri Word sunt marcate ca stil implicit de paragraf.",
                        part="word/styles.xml",
                        element="w:style[@w:default]",
                    )
                else:
                    default_paragraph = style_id
            elif style_type == "character":
                if default_character is not None:
                    _issue_once(
                        report.formatting_errors,
                        code="ambiguous_default_character_style",
                        message="Mai multe stiluri Word sunt marcate ca stil implicit de caractere.",
                        part="word/styles.xml",
                        element="w:style[@w:default]",
                    )
                else:
                    default_character = style_id

    doc_defaults_nodes = styles_root.findall(_W + "docDefaults")
    if len(doc_defaults_nodes) > 1:
        _issue_once(
            report.formatting_errors,
            code="ambiguous_doc_defaults",
            message="word/styles.xml conține mai multe elemente w:docDefaults.",
            part="word/styles.xml",
            element="w:docDefaults",
        )
    doc_defaults = doc_defaults_nodes[0] if doc_defaults_nodes else None
    rpr_defaults = (
        doc_defaults.findall(f"{_W}rPrDefault/{_W}rPr")
        if doc_defaults is not None
        else []
    )
    ppr_defaults = (
        doc_defaults.findall(f"{_W}pPrDefault/{_W}pPr")
        if doc_defaults is not None
        else []
    )
    if len(rpr_defaults) > 1 or len(ppr_defaults) > 1:
        _issue_once(
            report.formatting_errors,
            code="ambiguous_doc_default_properties",
            message="word/styles.xml conține mai multe seturi de proprietăți implicite.",
            part="word/styles.xml",
            element="w:rPrDefault/w:pPrDefault",
        )
    return (
        styles,
        rpr_defaults[0] if rpr_defaults else None,
        ppr_defaults[0] if ppr_defaults else None,
        default_paragraph,
        default_character,
    )


def _style_chain(
    style_id: str | None,
    styles: dict[str, _Style],
    *,
    expected_type: str,
    default_id: str | None,
    report: ImportReport,
    part: str,
    paragraph_index: int,
    run_index: int | None = None,
) -> list[_Style]:
    current = style_id or default_id
    if current is None:
        return []
    chain: list[_Style] = []
    visited: set[str] = set()
    while current:
        if current in visited:
            _issue_once(
                report.formatting_errors,
                code="cyclic_style_inheritance",
                message=f"Lanțul stilului {current!r} conține o referință circulară.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:basedOn",
            )
            return []
        visited.add(current)
        style = styles.get(current)
        if style is None:
            _issue_once(
                report.formatting_errors,
                code="missing_style",
                message=f"Stilul Word {current!r} nu poate fi rezolvat.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:style",
            )
            return []
        if style.style_type != expected_type:
            _issue_once(
                report.formatting_errors,
                code="incorrect_style_type",
                message=f"Stilul {current!r} nu are tipul Word {expected_type!r}.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:style",
            )
            return []
        chain.append(style)
        current = style.based_on
    chain.reverse()
    return chain


def _read_on_off(
    element: etree._Element,
    report: ImportReport,
    *,
    part: str,
    paragraph_index: int,
    run_index: int,
    property_name: str,
) -> bool | None:
    value = element.get(_W + "val")
    if value is None:
        return True
    normalized = value.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    _issue_once(
        report.formatting_errors,
        code="ambiguous_on_off_property",
        message=f"Valoarea proprietății {property_name} nu poate fi interpretată: {value!r}.",
        part=part,
        paragraph_index=paragraph_index,
        run_index=run_index,
        element=property_name,
    )
    return None


def _default_property_is_standard(prop: etree._Element) -> bool:
    local = etree.QName(prop).localname
    standard = _STANDARD_DEFAULT_RPR.get(local)
    if standard is None:
        return False
    attributes = {
        etree.QName(name).localname: value
        for name, value in prop.attrib.items()
        if name.startswith(_W)
    }
    return len(attributes) == len(prop.attrib) and attributes == standard


def _node_position(
    root: etree._Element,
    node: etree._Element,
) -> tuple[int | None, int | None]:
    paragraph = node
    run: etree._Element | None = None
    while paragraph is not None and paragraph.tag != _W + "p":
        if paragraph.tag == _W + "r" and run is None:
            run = paragraph
        paragraph = paragraph.getparent()
    if paragraph is None:
        return None, None
    paragraph_number = list(root.iter(_W + "p")).index(paragraph) + 1
    if run is None:
        return paragraph_number, None
    return paragraph_number, list(paragraph.iter(_W + "r")).index(run) + 1


def _read_r_fonts(
    element: etree._Element,
    report: ImportReport,
    *,
    part: str,
    paragraph_index: int,
    run_index: int,
) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    for attr_name, attr_value in element.attrib.items():
        if not attr_name.startswith(_W):
            local_attr = etree.QName(attr_name).localname
            _issue_once(
                report.formatting_errors,
                code="unsupported_font_property",
                message=f"Atributul {local_attr!r} din w:rFonts nu aparține spațiului Word.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:rFonts",
            )
            return None
        local_attr = etree.QName(attr_name).localname
        if local_attr not in _VALID_RFONTS_ATTRS:
            _issue_once(
                report.formatting_errors,
                code="unsupported_font_property",
                message=f"Atributul de font {local_attr!r} din w:rFonts nu este acceptat.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:rFonts",
            )
            return None
        if not attr_value or not attr_value.strip() or _contains_rich_text_control(attr_value):
            _issue_once(
                report.formatting_errors,
                code="unsupported_font_property",
                message=f"Valoarea atributului {local_attr!r} din w:rFonts este invalidă.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:rFonts",
            )
            return None
        if local_attr == "hint" and attr_value not in _VALID_RFONTS_HINTS:
            _issue_once(
                report.formatting_errors,
                code="unsupported_font_property",
                message=f"Valoarea 'hint' din w:rFonts este invalidă: {attr_value!r}.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element="w:rFonts",
            )
            return None
        parsed[local_attr] = attr_value
    return parsed


def _merge_r_fonts(
    current: dict[str, str] | None,
    incoming: dict[str, str],
) -> dict[str, str] | None:
    if not incoming:
        return current
    merged = dict(current) if current is not None else {}
    for explicit_key, theme_key in _RFONTS_SLOT_PAIRS:
        if explicit_key in incoming:
            merged.pop(theme_key, None)
            merged[explicit_key] = incoming[explicit_key]
        if theme_key in incoming:
            merged.pop(explicit_key, None)
            merged[theme_key] = incoming[theme_key]
    if "hint" in incoming:
        merged["hint"] = incoming["hint"]
    ordered = {key: merged[key] for key in VALID_RFONTS_KEYS if key in merged}
    return ordered or None


def _read_font_size_half_points(
    element: etree._Element,
    report: ImportReport,
    *,
    property_name: str,
    part: str,
    paragraph_index: int,
    run_index: int,
) -> int | None:
    unsupported_attributes = [name for name in element.attrib if name != _W + "val"]
    raw_val = element.get(_W + "val")
    if unsupported_attributes or raw_val is None:
        _issue_once(
            report.formatting_errors,
            code="unsupported_font_property",
            message=f"Atributele proprietății {property_name} nu sunt acceptate.",
            part=part,
            paragraph_index=paragraph_index,
            run_index=run_index,
            element=property_name,
        )
        return None
    try:
        parsed = int(raw_val)
    except ValueError:
        parsed = -1
    if parsed < 1 or parsed > MAX_FONT_SIZE_HALF_POINTS:
        _issue_once(
            report.formatting_errors,
            code="unsupported_font_property",
            message=f"Dimensiunea fontului din {property_name} este invalidă: {raw_val!r}.",
            part=part,
            paragraph_index=paragraph_index,
            run_index=run_index,
            element=property_name,
        )
        return None
    return parsed


def _check_run_properties(
    property_layers: list[tuple[etree._Element | None, str]],
    report: ImportReport,
    *,
    part: str,
    paragraph_index: int,
    run_index: int,
) -> tuple[bool, bool, bool, dict[str, str] | None, int | None, int | None]:
    bold = italic = underline = False
    r_fonts: dict[str, str] | None = None
    font_size_half_points: int | None = None
    complex_font_size_half_points: int | None = None
    for rpr, layer_kind in property_layers:
        if rpr is None:
            continue
        for child in rpr:
            local = etree.QName(child).localname
            tag = child.tag
            if not tag.startswith(_W):
                _issue_once(
                    report.formatting_errors,
                    code="unsupported_font_property",
                    message=f"Proprietatea {local!r} din alt spațiu de nume nu poate fi păstrată.",
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element=local,
                )
                continue
            if local in {"b", "i"}:
                value = _read_on_off(
                    child,
                    report,
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    property_name=f"w:{local}",
                )
                if value is not None:
                    if layer_kind == "style":
                        # Bold and italic are toggle properties in style definitions.
                        if value:
                            if local == "b":
                                bold = not bold
                            else:
                                italic = not italic
                    elif local == "b":
                        bold = value
                    else:
                        italic = value
                continue
            if local in {"bCs", "iCs"}:
                _read_on_off(
                    child,
                    report,
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    property_name=f"w:{local}",
                )
                continue
            if local == "rFonts":
                parsed_rfonts = _read_r_fonts(
                    child,
                    report,
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                )
                if parsed_rfonts is None:
                    continue
                if layer_kind == "docDefaults" and _default_property_is_standard(child):
                    continue
                r_fonts = _merge_r_fonts(r_fonts, parsed_rfonts)
                continue
            if local in {"sz", "szCs"}:
                parsed_size = _read_font_size_half_points(
                    child,
                    report,
                    property_name=f"w:{local}",
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                )
                if parsed_size is None:
                    continue
                if layer_kind == "docDefaults" and _default_property_is_standard(child):
                    continue
                if local == "sz":
                    font_size_half_points = parsed_size
                else:
                    complex_font_size_half_points = parsed_size
                continue
            if local == "u":
                value = (child.get(_W + "val") or "single").lower()
                unsupported_attributes = [name for name in child.attrib if name != _W + "val"]
                if unsupported_attributes:
                    _issue_once(
                        report.formatting_errors,
                        code="unsupported_underline_properties",
                        message=(
                            "Sublinierea colorată sau cu proprietăți tematice nu este acceptată."
                        ),
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element="w:u",
                    )
                    continue
                if value in {"single", "1", "true", "on"}:
                    underline = True
                elif value in {"none", "0", "false", "off"}:
                    underline = False
                else:
                    _issue_once(
                        report.formatting_errors,
                        code="unsupported_underline_style",
                        message=f"Este acceptată doar sublinierea simplă, nu {value!r}.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element="w:u",
                    )
                continue
            if local == "rStyle":
                continue
            if local in {"vanish", "webHidden", "specVanish"}:
                hidden = _read_on_off(
                    child,
                    report,
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    property_name=f"w:{local}",
                )
                if hidden:
                    _issue_once(
                        report.unsupported_elements,
                        code="hidden_text",
                        message="Textul ascuns nu este acceptat.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element=f"w:{local}",
                    )
                continue
            if local in {"strike", "dstrike"}:
                crossed_out = _read_on_off(
                    child,
                    report,
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    property_name=f"w:{local}",
                )
                if crossed_out:
                    _issue_once(
                        report.formatting_errors,
                        code="strikethrough",
                        message="Textul barat nu este acceptat.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element=f"w:{local}",
                    )
                continue
            if local == "vertAlign":
                alignment = (child.get(_W + "val") or "baseline").lower()
                if alignment != "baseline":
                    _issue_once(
                        report.formatting_errors,
                        code="superscript_or_subscript",
                        message="Superscriptul și subscriptul nu sunt acceptate.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element="w:vertAlign",
                    )
                continue
            if local == "color":
                color = (child.get(_W + "val") or "auto").lower()
                unsupported_attributes = [name for name in child.attrib if name != _W + "val"]
                if color != "auto" or unsupported_attributes:
                    _issue_once(
                        report.formatting_errors,
                        code="font_color",
                        message=(
                            "Culoarea textului sau proprietățile sale tematice nu sunt acceptate."
                        ),
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element="w:color",
                    )
                continue
            if local == "highlight":
                highlight = (child.get(_W + "val") or "none").lower()
                if highlight != "none":
                    _issue_once(
                        report.formatting_errors,
                        code="highlight",
                        message="Evidențierea textului nu este acceptată.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element="w:highlight",
                    )
                continue
            if local == "noProof":
                continue
            if local == "lang":
                if layer_kind == "docDefaults" and _default_property_is_standard(child):
                    continue
                _issue_once(
                    report.warnings,
                    code="language_metadata_ignored",
                    message="Metadatele de limbă Word nu sunt păstrate; textul și formatarea vizibilă rămân neschimbate.",
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element="w:lang",
                )
                continue
            if layer_kind == "docDefaults" and _default_property_is_standard(child):
                continue
            _issue_once(
                report.formatting_errors,
                code="unsupported_font_property",
                message=f"Proprietatea de formatare {local!r} nu poate fi păstrată.",
                part=part,
                paragraph_index=paragraph_index,
                run_index=run_index,
                element=f"w:{local}",
            )
    return (
        bold,
        italic,
        underline,
        r_fonts,
        font_size_half_points,
        complex_font_size_half_points,
    )


def _paragraph_properties_issue(
    ppr: etree._Element | None,
    report: ImportReport,
    *,
    part: str,
    paragraph_index: int,
    layer_name: str,
    allow_standard_default_spacing: bool = False,
) -> None:
    if ppr is None:
        return
    children = list(ppr)
    if allow_standard_default_spacing:
        if not children:
            return
        if len(children) == 1 and children[0].tag == _W + "spacing":
            attributes = {
                etree.QName(name).localname: value
                for name, value in children[0].attrib.items()
                if name.startswith(_W)
            }
            if len(attributes) == len(children[0].attrib) and attributes == _STANDARD_DEFAULT_PPR_SPACING:
                return
    for child in children:
        local = etree.QName(child).localname
        if not child.tag.startswith(_W):
            _issue_once(
                report.formatting_errors,
                code="unsupported_paragraph_formatting",
                message=f"Elementul de formatare {local!r} din alt spațiu de nume nu poate fi păstrat.",
                part=part,
                paragraph_index=paragraph_index,
                element=local,
            )
            continue
        if local in {"pStyle", "rPr"}:
            continue
        if local == "numPr":
            _issue_once(
                report.unsupported_elements,
                code="numbered_or_bulleted_list",
                message="Listele numerotate sau cu bullet-uri nu sunt acceptate.",
                part=part,
                paragraph_index=paragraph_index,
                element="w:numPr",
            )
        else:
            _issue_once(
                report.formatting_errors,
                code="unsupported_paragraph_formatting",
                message=f"Formatarea de paragraf {local!r} din {layer_name} nu poate fi păstrată.",
                part=part,
                paragraph_index=paragraph_index,
                element=f"w:{local}",
            )


def _check_sections(
    document_root: etree._Element,
    report: ImportReport,
    *,
    part: str,
) -> None:
    body = document_root.find(f"{_W}body")
    if body is None:
        _issue_once(
            report.structure_errors,
            code="missing_document_body",
            message="word/document.xml nu conține w:body.",
            part=part,
            element="w:body",
        )
        return

    body_sections = [child for child in body if child.tag == _W + "sectPr"]
    paragraph_sections = [
        section
        for section in document_root.iter(_W + "sectPr")
        if section.getparent() is not None and section.getparent().tag == _W + "pPr"
    ]
    if paragraph_sections:
        _issue_once(
            report.unsupported_elements,
            code="section_break",
            message="Întreruperile de secțiune Word nu sunt acceptate.",
            part=part,
            element="w:pPr/w:sectPr",
        )
    all_sections = list(document_root.iter(_W + "sectPr"))
    if (
        len(body_sections) != 1
        or (body_sections and body_sections[0] is not body[-1])
        or len(all_sections) != len(body_sections) + len(paragraph_sections)
    ):
        _issue_once(
            report.structure_errors,
            code="invalid_section_structure",
            message="Documentul trebuie să aibă o singură secțiune finală, fără secțiuni intermediare.",
            part=part,
            element="w:sectPr",
        )

    for section in body_sections:
        allowed = {"pgSz", "pgMar", "cols", "docGrid", "type", "headerReference", "footerReference"}
        allowed_attributes = {
            "pgSz": {"w", "h", "orient", "code"},
            "pgMar": {"top", "right", "bottom", "left", "header", "footer", "gutter"},
            "cols": {"num", "space", "sep", "equalWidth"},
            "type": {"val"},
            "headerReference": {"type", "id"},
            "footerReference": {"type", "id"},
            "docGrid": {"type", "linePitch", "charSpace"},
        }
        seen: set[str] = set()
        for child in section:
            local = etree.QName(child).localname
            if not child.tag.startswith(_W):
                _issue_once(
                    report.formatting_errors,
                    code="unsupported_section_property",
                    message=f"Elementul de secțiune {local!r} din alt spațiu de nume nu poate fi păstrat.",
                    part=part,
                    element=local,
                )
                continue
            if local in seen:
                _issue_once(
                    report.structure_errors,
                    code="duplicate_section_property",
                    message=f"Proprietatea w:{local} apare de mai multe ori în secțiune.",
                    part=part,
                    element=f"w:sectPr/w:{local}",
                )
            seen.add(local)
            if local not in allowed:
                _issue_once(
                    report.formatting_errors,
                    code="unsupported_section_property",
                    message=f"Proprietatea de secțiune w:{local} nu poate fi păstrată.",
                    part=part,
                    element=f"w:sectPr/w:{local}",
                )
                continue
            unexpected_attributes = {
                etree.QName(name).localname
                for name in child.attrib
                if not name.startswith(_W) or etree.QName(name).localname not in allowed_attributes[local]
            }
            if unexpected_attributes:
                _issue_once(
                    report.formatting_errors,
                    code="unsupported_section_attributes",
                    message=(
                        f"Atributele {sorted(unexpected_attributes)!r} din w:{local} "
                        "nu pot fi păstrate."
                    ),
                    part=part,
                    element=f"w:sectPr/w:{local}",
                )
            if local == "docGrid":
                attributes = {etree.QName(name).localname: value for name, value in child.attrib.items()}
                if attributes not in ({"linePitch": "360"}, {"type": "default", "linePitch": "360"}):
                    _issue_once(
                        report.formatting_errors,
                        code="unsupported_document_grid",
                        message="Grila de pagină Word nu poate fi păstrată.",
                        part=part,
                        element="w:docGrid",
                    )
            elif local == "type":
                section_type = child.get(_W + "val", "nextPage")
                if section_type not in {"nextPage", "continuous", "evenPage", "oddPage", "nextColumn"}:
                    _issue_once(
                        report.structure_errors,
                        code="invalid_section_type",
                        message=f"Tipul secțiunii {section_type!r} nu este recunoscut.",
                        part=part,
                        element="w:sectPr/w:type",
                    )
            elif local == "cols":
                try:
                    count = int(child.get(_W + "num", "1"))
                except ValueError:
                    count = 0
                explicit_columns = child.findall(_W + "col")
                if count != 1 or len(explicit_columns) > 1:
                    _issue_once(
                        report.unsupported_elements,
                        code="multiple_columns",
                        message="Machetele cu mai multe coloane nu sunt acceptate.",
                        part=part,
                        element="w:cols",
                    )
                if any(etree.QName(item).localname != "col" for item in child):
                    _issue_once(
                        report.structure_errors,
                        code="ambiguous_columns",
                        message="Configurația coloanelor Word nu poate fi interpretată complet.",
                        part=part,
                        element="w:cols",
                    )
                for column in explicit_columns:
                    unexpected_column_attributes = {
                        etree.QName(name).localname
                        for name in column.attrib
                        if not name.startswith(_W)
                        or etree.QName(name).localname not in {"w", "space"}
                    }
                    if unexpected_column_attributes:
                        _issue_once(
                            report.formatting_errors,
                            code="unsupported_section_attributes",
                            message="Atributele coloanei Word nu pot fi păstrate.",
                            part=part,
                            element="w:sectPr/w:cols/w:col",
                        )
def _detect_package_unsupported(
    parts: dict[str, etree._Element],
    report: ImportReport,
) -> None:
    for part_name, root in parts.items():
        if part_name.endswith(".rels"):
            for relationship in root:
                if relationship.get("TargetMode", "").lower() == "external":
                    relation_type = relationship.get("Type", "").rsplit("/", 1)[-1]
                    message = (
                        "Hyperlinkurile externe nu sunt acceptate."
                        if relation_type == "hyperlink"
                        else "Resursele externe nu sunt acceptate."
                    )
                    _issue_once(
                        report.unsupported_elements,
                        code="external_relationship",
                        message=message,
                        part=part_name,
                        element="Relationship",
                    )
            continue

        if part_name.startswith("word/header") or part_name.startswith("word/footer"):
            has_text = any(
                (node.text or "").strip()
                for node in root.iter(_W + "t")
            )
            has_nonempty_content = any(
                node.tag in {
                    _W + "tbl",
                    _W + "drawing",
                    _W + "pict",
                    _W + "object",
                    _W + "fldSimple",
                    _W + "hyperlink",
                }
                for node in root.iter()
            )
            if has_text or has_nonempty_content:
                _issue_once(
                    report.unsupported_elements,
                    code="header_footer_content",
                    message="Anteturile și subsolurile cu conținut nu sunt acceptate.",
                    part=part_name,
                    element="header/footer",
                )

        if part_name == "word/settings.xml" and root.find(_W + "trackRevisions") is not None:
            _issue_once(
                report.unsupported_elements,
                code="tracked_changes_enabled",
                message="Documentul are activată urmărirea modificărilor.",
                part=part_name,
                element="w:trackRevisions",
            )

        if part_name == "word/comments.xml" and next(root.iter(_W + "comment"), None) is not None:
            _issue_once(
                report.unsupported_elements,
                code="comments_part",
                message="Comentariile Word nu sunt acceptate.",
                part=part_name,
                element="w:comment",
            )

        if part_name in {"word/footnotes.xml", "word/endnotes.xml"} and any(
            (node.text or "").strip() for node in root.iter(_W + "t")
        ):
            _issue_once(
                report.unsupported_elements,
                code="notes_content",
                message="Notele de subsol și notele de final nu sunt acceptate.",
                part=part_name,
            )

        if part_name != "word/document.xml" and not (
            part_name.startswith("word/header")
            or part_name.startswith("word/footer")
            or part_name in {"word/comments.xml", "word/footnotes.xml", "word/endnotes.xml"}
        ):
            continue

        for node in root.iter():
            message = _UNSUPPORTED_TAG_MESSAGES.get(node.tag)
            if message:
                code = etree.QName(node).localname
                paragraph_index, run_index = _node_position(root, node)
                _issue_once(
                    report.unsupported_elements,
                    code=f"unsupported_{code}",
                    message=message,
                    part=part_name,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element=f"w:{code}",
                )
            elif node.tag == _W + "tab":
                paragraph_index, run_index = _node_position(root, node)
                _issue_once(
                    report.unsupported_elements,
                    code="tab_character",
                    message="Taburile Word nu sunt acceptate.",
                    part=part_name,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element="w:tab",
                )
            elif node.tag == _W + "br":
                break_type = (node.get(_W + "type") or "textWrapping").lower()
                if break_type != "textwrapping":
                    paragraph_index, run_index = _node_position(root, node)
                    _issue_once(
                        report.unsupported_elements,
                        code="page_or_column_break",
                        message="Întreruperile de pagină și coloană nu sunt acceptate.",
                        part=part_name,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element=f"w:br[{break_type}]",
                    )
            elif node.tag == _W + "cr":
                continue
            elif node.tag == _W + "proofErr":
                paragraph_index, run_index = _node_position(root, node)
                _issue_once(
                    report.warnings,
                    code="proofing_annotation",
                    message="Marcajele de verificare ortografică au fost ignorate; textul a fost păstrat.",
                    part=part_name,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element="w:proofErr",
                )
            elif node.tag == _W + "t" and node.text and ("\t" in node.text or "\n" in node.text):
                paragraph_index, run_index = _node_position(root, node)
                _issue_once(
                    report.unsupported_elements,
                    code="embedded_tab_or_line_break",
                    message="Taburile și întreruperile încorporate în text nu sunt acceptate.",
                    part=part_name,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element="w:t",
                )


def _paragraph_style_id(paragraph: etree._Element) -> str | None:
    ppr = paragraph.find(_W + "pPr")
    style = ppr.find(_W + "pStyle") if ppr is not None else None
    return style.get(_W + "val") if style is not None else None


def _run_style_id(run: etree._Element) -> str | None:
    rpr = run.find(_W + "rPr")
    style = rpr.find(_W + "rStyle") if rpr is not None else None
    return style.get(_W + "val") if style is not None else None


def _parse_paragraph(
    element: etree._Element,
    *,
    paragraph_index: int,
    part: str,
    styles: dict[str, _Style],
    default_rpr: etree._Element | None,
    default_paragraph_style: str | None,
    default_character_style: str | None,
    report: ImportReport,
) -> tuple[Paragraph, list[_TokenUnit]]:
    ppr = element.find(_W + "pPr")
    paragraph_styles = _style_chain(
        _paragraph_style_id(element),
        styles,
        expected_type="paragraph",
        default_id=default_paragraph_style,
        report=report,
        part=part,
        paragraph_index=paragraph_index,
    )
    _paragraph_properties_issue(
        ppr,
        report,
        part=part,
        paragraph_index=paragraph_index,
        layer_name="paragrafului",
    )
    for style in paragraph_styles:
        _paragraph_properties_issue(
            style.ppr,
            report,
            part=part,
            paragraph_index=paragraph_index,
            layer_name=f"stilului {style.name!r}",
        )
        if style.name.casefold().startswith("list"):
            _issue_once(
                report.unsupported_elements,
                code="list_paragraph_style",
                message="Stilurile de listă nu sunt acceptate.",
                part=part,
                paragraph_index=paragraph_index,
                element="w:pStyle",
            )

    inlines: list[RichInline] = []
    token_units: list[_TokenUnit] = []
    run_index = 0
    for child in element:
        if child.tag == _W + "pPr":
            continue
        if child.tag != _W + "r":
            # Hyperlink-uri și alte containere sunt raportate separat. Nu
            # încercăm să le aplatizăm într-un rezultat aparent fidel.
            if child.tag != _W + "proofErr" and child.tag not in _UNSUPPORTED_TAG_MESSAGES:
                local = etree.QName(child).localname
                _issue_once(
                    report.unsupported_elements,
                    code="unsupported_paragraph_content",
                    message=f"Elementul Word {local!r} din paragraf nu poate fi reprezentat.",
                    part=part,
                    paragraph_index=paragraph_index,
                    element=f"w:{local}",
                )
            continue
        run_index += 1
        rpr = child.find(_W + "rPr")
        character_styles = _style_chain(
            _run_style_id(child),
            styles,
            expected_type="character",
            default_id=default_character_style,
            report=report,
            part=part,
            paragraph_index=paragraph_index,
            run_index=run_index,
        )
        layers: list[tuple[etree._Element | None, str]] = [(default_rpr, "docDefaults")]
        layers.extend((style.rpr, "style") for style in paragraph_styles)
        layers.extend((style.rpr, "style") for style in character_styles)
        layers.append((rpr, "direct"))
        style = _check_run_properties(
            layers,
            report,
            part=part,
            paragraph_index=paragraph_index,
            run_index=run_index,
        )
        token_style_key = (
            style[0],
            style[1],
            style[2],
            tuple(style[3].items()) if style[3] is not None else None,
            style[4],
            style[5],
        )

        current_text = ""

        def flush_text() -> None:
            nonlocal current_text
            if not current_text:
                return
            if _contains_rich_text_control(current_text):
                _issue_once(
                    report.content_errors,
                    code="invalid_text_control",
                    message=(
                        "Textul Word conține un caracter de control sau separator de rând "
                        "care nu poate fi reprezentat în Rich Text."
                    ),
                    part=part,
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    element="w:t",
                )
                current_text = ""
                return
            inlines.append(
                TextInline(
                    current_text,
                    bold=style[0],
                    italic=style[1],
                    underline=style[2],
                    r_fonts=dict(style[3]) if style[3] is not None else None,
                    font_size_half_points=style[4],
                    complex_font_size_half_points=style[5],
                )
            )
            token_units.extend(
                _TokenUnit(character, "text", token_style_key, paragraph_index, run_index)
                for character in current_text
            )
            current_text = ""

        for run_child in child:
            if run_child.tag == _W + "rPr":
                continue
            if run_child.tag == _W + "t":
                current_text += run_child.text or ""
            elif run_child.tag == _W + "br":
                flush_text()
                if (run_child.get(_W + "type") or "textWrapping").lower() == "textwrapping":
                    inlines.append(LineBreakInline())
                    token_units.append(_TokenUnit("\n", "line_break", token_style_key, paragraph_index, run_index))
            elif run_child.tag == _W + "cr":
                flush_text()
                inlines.append(LineBreakInline())
                token_units.append(_TokenUnit("\n", "line_break", token_style_key, paragraph_index, run_index))
            elif run_child.tag == _W + "tab":
                flush_text()
            else:
                flush_text()
                if run_child.tag not in _UNSUPPORTED_TAG_MESSAGES and run_child.tag != _W + "proofErr":
                    local = etree.QName(run_child).localname
                    _issue_once(
                        report.unsupported_elements,
                        code="unsupported_run_content",
                        message=f"Elementul Word {local!r} din run nu poate fi reprezentat.",
                        part=part,
                        paragraph_index=paragraph_index,
                        run_index=run_index,
                        element=f"w:{local}",
                    )
        flush_text()

    return Paragraph(inlines), token_units


def _validate_variables(
    paragraph_units: list[list[_TokenUnit]],
    report: ImportReport,
) -> None:
    units: list[_TokenUnit] = []
    for index, paragraph in enumerate(paragraph_units):
        units.extend(paragraph)
        if index != len(paragraph_units) - 1:
            units.append(_TokenUnit("\u0000", "paragraph_break", None, index + 1))
    text = "".join(unit.char for unit in units)
    index = 0
    occurrences: Counter[str] = Counter()
    while index < len(text):
        if text.startswith("{{", index):
            close = text.find("}}", index + 2)
            nested = text.find("{{", index + 2, close if close >= 0 else len(text))
            start_unit = units[index]
            if nested >= 0:
                _issue_once(
                    report.variable_errors,
                    code="nested_variable_token",
                    message="Marcajele variabilelor nu pot fi imbricate.",
                    part="word/document.xml",
                    paragraph_index=start_unit.paragraph_index,
                    run_index=start_unit.run_index,
                    element="{{",
                )
                index = nested
                continue
            if close < 0:
                _issue_once(
                    report.variable_errors,
                    code="incomplete_variable_token",
                    message="Marcajul de deschidere {{ nu are un marcaj de închidere }}.",
                    part="word/document.xml",
                    paragraph_index=start_unit.paragraph_index,
                    run_index=start_unit.run_index,
                    element="{{",
                )
                index += 2
                continue

            token_units = units[index : close + 2]
            paragraph_break = next((unit for unit in token_units if unit.kind == "paragraph_break"), None)
            if paragraph_break is not None:
                _issue_once(
                    report.variable_errors,
                    code="variable_crosses_paragraphs",
                    message="Un token de variabilă nu poate traversa două paragrafe Word.",
                    part="word/document.xml",
                    paragraph_index=start_unit.paragraph_index,
                    run_index=start_unit.run_index,
                    element="{{...}}",
                )
                index = close + 2
                continue
            if any(unit.kind == "line_break" for unit in token_units):
                _issue_once(
                    report.variable_errors,
                    code="variable_crosses_line_break",
                    message="Un token de variabilă nu poate traversa o întrerupere de rând.",
                    part="word/document.xml",
                    paragraph_index=start_unit.paragraph_index,
                    run_index=start_unit.run_index,
                    element="{{...}}",
                )
                index = close + 2
                continue

            key = text[index + 2 : close]
            if not _VARIABLE_KEY.fullmatch(key):
                _issue_once(
                    report.variable_errors,
                    code="invalid_variable_key",
                    message=f"Cheia variabilei {key!r} nu respectă sintaxa ASCII acceptată.",
                    part="word/document.xml",
                    paragraph_index=start_unit.paragraph_index,
                    run_index=start_unit.run_index,
                    element="{{...}}",
                )
            else:
                occurrences[key] += 1
                styles = {unit.style for unit in token_units if unit.kind == "text"}
                if len(styles) > 1:
                    _issue_once(
                        report.formatting_errors,
                        code="variable_mixed_formatting",
                        message=f"Formatarea tokenului pentru cheia {key!r} este mixtă.",
                        part="word/document.xml",
                        paragraph_index=start_unit.paragraph_index,
                        run_index=start_unit.run_index,
                        element="{{...}}",
                    )
            index = close + 2
            continue
        if text.startswith("}}", index):
            unit = units[index]
            _issue_once(
                report.variable_errors,
                code="unmatched_variable_close",
                message="Marcajul de închidere }} nu are un token de deschidere.",
                part="word/document.xml",
                paragraph_index=unit.paragraph_index,
                run_index=unit.run_index,
                element="}}",
            )
            index += 2
            continue
        index += 1
    report.variables = dict(sorted(occurrences.items()))


def _count_fragments(document: RichDocument, report: ImportReport) -> None:
    for paragraph in document.paragraphs:
        for inline in paragraph.inlines:
            report.fragment_count += 1
            if isinstance(inline, TextInline):
                report.bold_fragment_count += int(inline.bold)
                report.italic_fragment_count += int(inline.italic)
                report.underline_fragment_count += int(inline.underline)


def parse_docx_bytes(docx_bytes: bytes) -> tuple[RichDocument, ImportReport, str]:
    """Parsează un DOCX în memorie și returnează modelul, raportul și SHA-256.

    Fișierul original nu este modificat și arhiva nu este extrasă pe disc. XML-ul
    este parsată cu entități externe și accesul la rețea dezactivate. Un rezultat
    `rejected` conține un model parțial numai pentru diagnostic; acesta nu trebuie
    activat sau folosit pentru generare.
    """

    if not isinstance(docx_bytes, bytes):
        raise TypeError("docx_bytes trebuie să fie bytes.")
    digest = hashlib.sha256(docx_bytes).hexdigest()
    report = ImportReport()
    rich_document = RichDocument()

    try:
        parts = _load_package_parts(docx_bytes)
        content_types = parts["[Content_Types].xml"]
        if content_types.tag != f"{{{_CONTENT_TYPES_NS}}}Types":
            raise _DocxStructureError("[Content_Types].xml nu are rădăcina OOXML Types.")
        document_root = parts["word/document.xml"]
        if document_root.tag != _W + "document":
            raise _DocxStructureError("word/document.xml nu are rădăcina OOXML w:document.")
        styles_root = parts.get("word/styles.xml")
        if styles_root is not None and styles_root.tag != _W + "styles":
            raise _DocxStructureError("word/styles.xml nu are rădăcina OOXML w:styles.")
        for part_name, root in parts.items():
            if part_name.endswith(".rels") and root.tag != f"{{{_RELATIONSHIPS_NS}}}Relationships":
                raise _DocxStructureError(f"Componenta {part_name} nu are rădăcina OOXML Relationships.")
        main_type = next(
            (
                node.get("ContentType", "")
                for node in content_types
                if node.get("PartName") == "/word/document.xml"
            ),
            "",
        )
        if "macroEnabled" in main_type:
            _issue_once(
                report.unsupported_elements,
                code="macro_enabled_document",
                message="Documentele Word cu macrocomenzi nu sunt acceptate; importați un DOCX.",
                part="[Content_Types].xml",
                element="ContentType",
            )
        elif main_type != "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml":
            raise _DocxStructureError(
                "Tipul componentei principale nu este un document DOCX obișnuit."
            )

        # Python-docx oferă o a doua verificare structurală a părților folosite.
        try:
            word_document = Document(BytesIO(docx_bytes))
        except (
            PackageNotFoundError,
            KeyError,
            ValueError,
            BadZipFile,
            LargeZipFile,
            EOFError,
            RuntimeError,
            OSError,
            NotImplementedError,
            zlib.error,
            etree.XMLSyntaxError,
        ) as exc:
            raise _DocxStructureError(f"Structura pachetului DOCX nu este validă: {exc}") from exc
        _detect_package_unsupported(parts, report)

        body = document_root.find(f"{_W}body")
        body_elements = document_root.findall(_W + "body")
        if body is None or len(body_elements) != 1:
            raise _DocxStructureError("word/document.xml trebuie să conțină exact un w:body.")
        for root_child in document_root:
            if root_child is not body:
                _issue_once(
                    report.unsupported_elements,
                    code="unsupported_document_root_element",
                    message=(
                        f"Elementul {etree.QName(root_child).localname!r} din w:document "
                        "nu poate fi păstrat."
                    ),
                    part="word/document.xml",
                    element=etree.QName(root_child).localname,
                )
        _check_sections(document_root, report, part="word/document.xml")
        (
            styles,
            default_rpr,
            default_ppr,
            default_paragraph,
            default_character,
        ) = _styles_from_xml(styles_root, report)
        _paragraph_properties_issue(
            default_ppr,
            report,
            part="word/styles.xml",
            paragraph_index=0,
            layer_name="docDefaults/pPrDefault",
            allow_standard_default_spacing=True,
        )

        paragraphs: list[Paragraph] = []
        paragraph_units: list[list[_TokenUnit]] = []
        raw_paragraphs = [child for child in body if child.tag == _W + "p"]
        if len(raw_paragraphs) != len(word_document.paragraphs):
            _issue_once(
                report.structure_errors,
                code="paragraph_structure_mismatch",
                message="Numărul paragrafelor OOXML nu corespunde structurii python-docx.",
                part="word/document.xml",
            )

        for body_child in body:
            if body_child.tag == _W + "p":
                paragraph_index = len(paragraphs) + 1
                paragraph, units = _parse_paragraph(
                    body_child,
                    paragraph_index=paragraph_index,
                    part="word/document.xml",
                    styles=styles,
                    default_rpr=default_rpr,
                    default_paragraph_style=default_paragraph,
                    default_character_style=default_character,
                    report=report,
                )
                paragraphs.append(paragraph)
                paragraph_units.append(units)
            elif body_child.tag == _W + "sectPr":
                continue
            elif body_child.tag == _W + "tbl":
                # Raportat și în analiza generală OOXML; evităm dublurile.
                _issue_once(
                    report.unsupported_elements,
                    code="unsupported_tbl",
                    message="Tabelele nu sunt acceptate.",
                    part="word/document.xml",
                    element="w:tbl",
                )
            else:
                local = etree.QName(body_child).localname
                _issue_once(
                    report.unsupported_elements,
                    code="unsupported_body_element",
                    message=f"Elementul Word {local!r} din corp nu poate fi reprezentat.",
                    part="word/document.xml",
                    element=f"w:{local}",
                )

        if word_document.tables:
            _issue_once(
                report.unsupported_elements,
                code="python_docx_table",
                message="python-docx a identificat tabele în document.",
                part="word/document.xml",
                element="table",
            )
        if word_document.inline_shapes:
            _issue_once(
                report.unsupported_elements,
                code="python_docx_inline_shape",
                message="python-docx a identificat imagini sau forme în document.",
                part="word/document.xml",
                element="inline_shape",
            )

        rich_document = RichDocument(paragraphs=paragraphs)
        report.paragraph_count = len(paragraphs)
        _count_fragments(rich_document, report)
        _validate_variables(paragraph_units, report)
        visible_text = "".join(
            unit.char
            for paragraph in paragraph_units
            for unit in paragraph
            if unit.kind == "text"
        )
        if not visible_text.strip():
            _issue_once(
                report.content_errors,
                code="empty_document",
                message="Documentul trebuie să conțină text sau cel puțin un marcaj de variabilă valid.",
                part="word/document.xml",
            )
    except _ExpectedDocxError as exc:
        if isinstance(exc, _DocxArchiveError):
            target = report.archive_errors
        elif isinstance(exc, _DocxXmlError):
            target = report.xml_errors
        elif isinstance(exc, _DocxStructureError):
            target = report.structure_errors
        elif isinstance(exc, _DocxResourceLimitError):
            target = report.resource_errors
        else:  # pragma: no cover - protecție pentru o clasă nouă de eroare așteptată
            target = report.parsing_errors
        _issue_once(
            target,
            code=exc.issue_code,
            message=str(exc),
            part=None,
        )
        rich_document = RichDocument()
        report.paragraph_count = report.fragment_count = 0
        report.bold_fragment_count = report.italic_fragment_count = report.underline_fragment_count = 0
        report.variables = {}

    report.finalize()
    return rich_document, report, digest
