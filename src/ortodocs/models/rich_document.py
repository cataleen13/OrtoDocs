"""Modelul Rich Text simplu folosit pentru șabloane și documente generate."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, TypeAlias


SCHEMA_VERSION = 1
MAX_FONT_SIZE_HALF_POINTS = 3276
VALID_RFONTS_KEYS: tuple[str, ...] = (
    "ascii",
    "hAnsi",
    "eastAsia",
    "cs",
    "asciiTheme",
    "hAnsiTheme",
    "eastAsiaTheme",
    "cstheme",
    "hint",
)
_VALID_RFONTS_KEY_SET = frozenset(VALID_RFONTS_KEYS)
_VALID_RFONTS_HINTS = frozenset({"default", "eastAsia", "cs"})
_REQUIRED_TEXT_INLINE_DICT_KEYS = frozenset({"type", "text", "bold", "italic", "underline"})
_ALLOWED_TEXT_INLINE_DICT_KEYS = _REQUIRED_TEXT_INLINE_DICT_KEYS | {"r_fonts", "sz", "sz_cs"}


def _has_disallowed_control_chars(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"} for character in value)


def _normalize_r_fonts(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("TextInline.r_fonts trebuie să fie un dicționar sau None.")
    if not value:
        raise ValueError("TextInline.r_fonts nu poate fi un dicționar gol.")
    unknown_keys = set(value) - _VALID_RFONTS_KEY_SET
    if unknown_keys:
        raise ValueError(
            f"TextInline.r_fonts conține atribute nesuportate: {sorted(unknown_keys)!r}."
        )
    normalized: dict[str, str] = {}
    for key in VALID_RFONTS_KEYS:
        if key not in value:
            continue
        raw_item = value[key]
        if not isinstance(raw_item, str) or not raw_item.strip():
            raise ValueError(f"Valoarea TextInline.r_fonts[{key!r}] trebuie să fie text nevid.")
        if _has_disallowed_control_chars(raw_item):
            raise ValueError(
                f"Valoarea TextInline.r_fonts[{key!r}] conține caractere de control invalide."
            )
        if key == "hint" and raw_item not in _VALID_RFONTS_HINTS:
            raise ValueError(
                f"Valoarea TextInline.r_fonts['hint'] trebuie să fie una dintre {sorted(_VALID_RFONTS_HINTS)!r}."
            )
        normalized[key] = raw_item
    return normalized


def _normalize_half_points(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"TextInline.{field_name} trebuie să fie int sau None.")
    if value < 1 or value > MAX_FONT_SIZE_HALF_POINTS:
        raise ValueError(
            f"TextInline.{field_name} trebuie să fie între 1 și {MAX_FONT_SIZE_HALF_POINTS}."
        )
    return value


@dataclass(slots=True)
class TextInline:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    r_fonts: dict[str, str] | None = None
    font_size_half_points: int | None = None
    complex_font_size_half_points: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("TextInline.text trebuie să fie un șir de caractere.")
        if _has_disallowed_control_chars(self.text):
            raise ValueError(
                "TextInline.text nu poate conține caractere de control sau separatori de rând; "
                "folosiți LineBreakInline sau paragrafe separate."
            )
        for name in ("bold", "italic", "underline"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"TextInline.{name} trebuie să fie bool.")
        self.r_fonts = _normalize_r_fonts(self.r_fonts)
        self.font_size_half_points = _normalize_half_points(
            self.font_size_half_points,
            field_name="font_size_half_points",
        )
        self.complex_font_size_half_points = _normalize_half_points(
            self.complex_font_size_half_points,
            field_name="complex_font_size_half_points",
        )

    def formatting_key(self) -> tuple[Any, ...]:
        """Cheie canonică pentru compararea formatării între fragmente."""

        return (
            self.bold,
            self.italic,
            self.underline,
            tuple(self.r_fonts.items()) if self.r_fonts is not None else None,
            self.font_size_half_points,
            self.complex_font_size_half_points,
        )

    def clone_with_text(self, text: str) -> TextInline:
        """Creează un fragment nou păstrând integral formatarea acestuia."""

        return TextInline(
            text=text,
            bold=self.bold,
            italic=self.italic,
            underline=self.underline,
            r_fonts=dict(self.r_fonts) if self.r_fonts is not None else None,
            font_size_half_points=self.font_size_half_points,
            complex_font_size_half_points=self.complex_font_size_half_points,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "text",
            "text": self.text,
            "bold": self.bold,
            "italic": self.italic,
            "underline": self.underline,
        }
        if self.r_fonts is not None:
            payload["r_fonts"] = dict(self.r_fonts)
        if self.font_size_half_points is not None:
            payload["sz"] = self.font_size_half_points
        if self.complex_font_size_half_points is not None:
            payload["sz_cs"] = self.complex_font_size_half_points
        return payload


@dataclass(slots=True)
class LineBreakInline:
    """Întrerupere de rând în interiorul aceluiași paragraf Word."""

    def to_dict(self) -> dict[str, str]:
        return {"type": "line_break"}


RichInline: TypeAlias = TextInline | LineBreakInline


@dataclass(slots=True)
class Paragraph:
    inlines: list[RichInline] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.inlines, list) or any(
            not isinstance(inline, (TextInline, LineBreakInline)) for inline in self.inlines
        ):
            raise TypeError("Paragraph.inlines trebuie să conțină doar fragmente Rich Text.")

    def to_dict(self) -> dict[str, Any]:
        return {"inlines": [inline.to_dict() for inline in self.inlines]}


@dataclass(slots=True)
class RichDocument:
    paragraphs: list[Paragraph] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Versiune JSON Rich Text nesuportată: {self.schema_version}.")
        if not isinstance(self.paragraphs, list) or any(
            not isinstance(paragraph, Paragraph) for paragraph in self.paragraphs
        ):
            raise TypeError("RichDocument.paragraphs trebuie să fie o listă de Paragraph.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "paragraphs": [paragraph.to_dict() for paragraph in self.paragraphs],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Any) -> RichDocument:
        if not isinstance(value, dict) or set(value) != {"schema_version", "paragraphs"}:
            raise ValueError("Documentul Rich Text trebuie să conțină schema_version și paragraphs.")
        version = value["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"Versiune JSON Rich Text nesuportată: {version!r}.")
        raw_paragraphs = value["paragraphs"]
        if not isinstance(raw_paragraphs, list):
            raise ValueError("RichDocument.paragraphs trebuie să fie o listă.")

        paragraphs: list[Paragraph] = []
        for paragraph_index, raw_paragraph in enumerate(raw_paragraphs):
            if not isinstance(raw_paragraph, dict) or set(raw_paragraph) != {"inlines"}:
                raise ValueError(f"Paragraful {paragraph_index + 1} nu are structura acceptată.")
            raw_inlines = raw_paragraph["inlines"]
            if not isinstance(raw_inlines, list):
                raise ValueError(f"inlines din paragraful {paragraph_index + 1} trebuie să fie listă.")
            inlines: list[RichInline] = []
            for inline_index, raw_inline in enumerate(raw_inlines):
                location = f"paragraful {paragraph_index + 1}, fragmentul {inline_index + 1}"
                if not isinstance(raw_inline, dict):
                    raise ValueError(f"Fragmentul din {location} trebuie să fie obiect JSON.")
                inline_type = raw_inline.get("type")
                if inline_type == "text":
                    inline_keys = set(raw_inline)
                    if not (
                        _REQUIRED_TEXT_INLINE_DICT_KEYS
                        <= inline_keys
                        <= _ALLOWED_TEXT_INLINE_DICT_KEYS
                    ):
                        raise ValueError(f"Fragmentul text din {location} are câmpuri incorecte.")
                    inlines.append(
                        TextInline(
                            text=raw_inline["text"],
                            bold=raw_inline["bold"],
                            italic=raw_inline["italic"],
                            underline=raw_inline["underline"],
                            r_fonts=raw_inline.get("r_fonts"),
                            font_size_half_points=raw_inline.get("sz"),
                            complex_font_size_half_points=raw_inline.get("sz_cs"),
                        )
                    )
                elif inline_type == "line_break":
                    if set(raw_inline) != {"type"}:
                        raise ValueError(f"Întreruperea de rând din {location} are câmpuri incorecte.")
                    inlines.append(LineBreakInline())
                else:
                    raise ValueError(f"Tip de fragment nesuportat în {location}: {inline_type!r}.")
            paragraphs.append(Paragraph(inlines))
        return cls(paragraphs=paragraphs, schema_version=version)

    @classmethod
    def from_json(cls, value: str) -> RichDocument:
        if not isinstance(value, str):
            raise TypeError("JSON Rich Text trebuie furnizat ca șir.")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON Rich Text nu poate fi decodat.") from exc
        return cls.from_dict(parsed)
