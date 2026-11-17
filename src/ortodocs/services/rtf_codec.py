"""Codec strict, independent de Tk și Windows, pentru RichDocument și RTF."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from ortodocs.models.rich_document import (
    LineBreakInline,
    Paragraph,
    RichDocument,
    TextInline,
)


MAX_RTF_INPUT_LENGTH = 8_000_000
MAX_GROUP_DEPTH = 64
MAX_DOCUMENT_CHARACTERS = 200_000


class RtfCodecError(ValueError):
    """RTF invalid, prea mare sau cu structură pe care modelul nu o suportă."""


def _copy_and_validate_document(document: RichDocument) -> RichDocument:
    if not isinstance(document, RichDocument):
        raise TypeError("document trebuie să fie un RichDocument.")
    try:
        return RichDocument.from_json(document.to_json())
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise RtfCodecError(f"document nu este un RichDocument valid: {exc}") from exc


def _count_document_characters(document: RichDocument) -> int:
    count = 0
    for paragraph in document.paragraphs:
        count += 1  # delimitatorul RTF de paragraf
        if count > MAX_DOCUMENT_CHARACTERS:
            raise RtfCodecError(
                f"Documentul depășește limita de {MAX_DOCUMENT_CHARACTERS} caractere."
            )
        for inline in paragraph.inlines:
            count += len(inline.text) if isinstance(inline, TextInline) else 1
            if count > MAX_DOCUMENT_CHARACTERS:
                raise RtfCodecError(
                    f"Documentul depășește limita de {MAX_DOCUMENT_CHARACTERS} caractere."
                )
    if count > MAX_DOCUMENT_CHARACTERS:
        raise RtfCodecError(
            f"Documentul depășește limita de {MAX_DOCUMENT_CHARACTERS} caractere."
        )
    return count


def _signed_utf16_unit(unit: int) -> int:
    return unit if unit < 0x8000 else unit - 0x10000


def _encode_text(value: str) -> str:
    encoded: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character in "{}\\":
            encoded.append("\\" + character)
        elif 0x20 <= codepoint <= 0x7E:
            encoded.append(character)
        elif codepoint <= 0xFFFF:
            encoded.append(f"\\u{_signed_utf16_unit(codepoint)}?")
        else:
            scalar = codepoint - 0x10000
            high = 0xD800 + (scalar >> 10)
            low = 0xDC00 + (scalar & 0x3FF)
            encoded.append(f"\\u{_signed_utf16_unit(high)}?\\u{_signed_utf16_unit(low)}?")
    return "".join(encoded)


def _style_controls(
    previous: tuple[bool, bool, bool],
    current: tuple[bool, bool, bool],
) -> str:
    controls: list[str] = []
    if previous[0] != current[0]:
        controls.append("\\b " if current[0] else "\\b0 ")
    if previous[1] != current[1]:
        controls.append("\\i " if current[1] else "\\i0 ")
    if previous[2] != current[2]:
        controls.append("\\ul " if current[2] else "\\ulnone ")
    return "".join(controls)


def encode_rtf(document: RichDocument) -> str:
    """Codifică un RichDocument în subset RTF strict folosit de OrtoDocs.

    Fiecare paragraf este terminat cu ``\\par``; ``LineBreakInline`` devine
    ``\\line``. Textul non-ASCII este scris în unități UTF-16 RTF ``\\uN?``.
    Fragmentele text consecutive cu același stil pot fi reunite la decodare.
    """

    snapshot = _copy_and_validate_document(document)
    _count_document_characters(snapshot)

    pieces: list[str] = []
    output_length = 0

    def append_piece(piece: str) -> None:
        nonlocal output_length
        output_length += len(piece)
        if output_length > MAX_RTF_INPUT_LENGTH:
            raise RtfCodecError(
                f"RTF generat depășește limita de {MAX_RTF_INPUT_LENGTH} caractere."
            )
        pieces.append(piece)

    append_piece(r"{\rtf1\ansi\ansicpg1252\uc1 ")
    active_style = (False, False, False)
    for paragraph in snapshot.paragraphs:
        for inline in paragraph.inlines:
            if isinstance(inline, TextInline):
                style = (inline.bold, inline.italic, inline.underline)
                append_piece(_style_controls(active_style, style))
                active_style = style
                append_piece(_encode_text(inline.text))
            else:
                append_piece(r"\line ")
        append_piece(r"\par ")
    append_piece("}")
    return "".join(pieces)


@dataclass(slots=True)
class _State:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    uc_skip: int = 1

    def copy(self) -> _State:
        return _State(self.bold, self.italic, self.underline, self.uc_skip)

    @property
    def style(self) -> tuple[bool, bool, bool]:
        return (self.bold, self.italic, self.underline)


class _Parser:
    def __init__(self, source: str) -> None:
        if not isinstance(source, str):
            raise TypeError("RTF trebuie furnizat ca șir de caractere.")
        if len(source) > MAX_RTF_INPUT_LENGTH:
            raise RtfCodecError(
                f"RTF depășește limita de {MAX_RTF_INPUT_LENGTH} caractere."
            )
        self.source = source
        self.position = 0
        self.stack: list[_State] = []
        self.paragraphs: list[Paragraph] = []
        self.current_paragraph = Paragraph()
        self.current_has_content = False
        self.character_count = 0
        self.text_buffer: list[str] = []
        self.text_buffer_style: tuple[bool, bool, bool] | None = None
        self.pending_high_surrogate: tuple[int, tuple[bool, bool, bool]] | None = None

    @property
    def state(self) -> _State:
        return self.stack[-1]

    def parse(self) -> RichDocument:
        self._parse_header()
        root_closed = False

        while self.position < len(self.source):
            character = self.source[self.position]
            if character == "{":
                self._require_no_pending_surrogate()
                if len(self.stack) >= MAX_GROUP_DEPTH:
                    self._error(f"Adâncimea grupurilor RTF depășește {MAX_GROUP_DEPTH}.")
                self.stack.append(self.state.copy())
                self.position += 1
            elif character == "}":
                self._require_no_pending_surrogate()
                if len(self.stack) == 1:
                    self._flush_text()
                    self.position += 1
                    self.stack.pop()
                    root_closed = True
                    break
                if self.text_buffer_style != self.stack[-2].style:
                    self._flush_text()
                self.position += 1
                self.stack.pop()
            elif character == "\\":
                self._parse_escape()
            elif character in "\r\n":
                self._require_no_pending_surrogate()
                self.position += 1  # whitespace de formatare din sursa RTF
            else:
                self._require_no_pending_surrogate()
                self._append_character(character)
                self.position += 1

        if not root_closed:
            self._error("Grupul RTF principal nu este închis.")
        if self.stack:
            self._error("Grupurile RTF nu sunt echilibrate.")
        if self.position != len(self.source):
            trailing = self.source[self.position :]
            if any(character not in "\r\n" for character in trailing):
                self._error("Există conținut după închiderea grupului RTF principal.")
        self._require_no_pending_surrogate()
        self._flush_text()
        if self.current_has_content:
            self._increment_character_count(1)
            self.paragraphs.append(self.current_paragraph)
        return RichDocument(paragraphs=self.paragraphs)

    def _parse_header(self) -> None:
        if not self.source.startswith("{"):
            self._error("RTF trebuie să înceapă cu acolada grupului principal.")
        self.position = 1
        self.stack = [_State()]
        while self.position < len(self.source) and self.source[self.position] in "\r\n":
            self.position += 1
        if self.position >= len(self.source) or self.source[self.position] != "\\":
            self._error("Antet RTF invalid: era așteptat controlul \\rtf1.")
        word, parameter = self._read_control_word()
        if word != "rtf" or parameter != 1:
            self._error("Antet RTF invalid: este necesar controlul \\rtf1.")

    def _read_control_word(self) -> tuple[str, int | None]:
        if self.position >= len(self.source) or self.source[self.position] != "\\":
            self._error("Poziție invalidă la citirea unui cuvânt de control RTF.")
        self.position += 1
        start = self.position
        while self.position < len(self.source) and (
            "A" <= self.source[self.position] <= "Z"
            or "a" <= self.source[self.position] <= "z"
        ):
            self.position += 1
        if start == self.position:
            self._error("Era așteptat un cuvânt de control RTF.")
        word = self.source[start : self.position]

        parameter: int | None = None
        parameter_start = self.position
        if self.position < len(self.source) and self.source[self.position] == "-":
            self.position += 1
        digits_start = self.position
        while self.position < len(self.source) and "0" <= self.source[self.position] <= "9":
            self.position += 1
        if digits_start < self.position:
            raw_number = self.source[parameter_start : self.position]
            try:
                parameter = int(raw_number)
            except ValueError as exc:  # pragma: no cover - protecție pentru parser
                raise RtfCodecError("Parametru numeric RTF invalid.") from exc
        elif self.position != parameter_start:
            self._error(f"Parametru numeric RTF invalid pentru \\{word}.")

        if self.position < len(self.source) and self.source[self.position] == " ":
            self.position += 1  # delimitatorul unic al cuvântului de control
        return word, parameter

    def _parse_escape(self) -> None:
        if self.position + 1 >= len(self.source):
            self._error("Backslash final fără secvență de control.")
        next_character = self.source[self.position + 1]

        if next_character in "\\{}":
            self._require_no_pending_surrogate()
            self._append_character(next_character)
            self.position += 2
            return
        if next_character == "'":
            self._require_no_pending_surrogate()
            self._parse_hex_escape()
            return
        if next_character in "~_-":
            self._require_no_pending_surrogate()
            character = {"~": "\u00a0", "_": "\u2011", "-": "\u00ad"}[next_character]
            self._append_character(character)
            self.position += 2
            return
        if not (
            "A" <= next_character <= "Z"
            or "a" <= next_character <= "z"
        ):
            self._error(f"Secvență de control RTF nesuportată: \\{next_character}.")

        word, parameter = self._read_control_word()
        if word == "u":
            self._parse_unicode_escape(parameter)
            return
        self._require_no_pending_surrogate()

        if word in {"b", "i", "ul"}:
            if parameter not in (None, 0, 1):
                self._error(f"Valoare invalidă pentru controlul RTF \\{word}.")
            enabled = parameter != 0
            if word == "b":
                if self.state.bold != enabled:
                    self._flush_text()
                    self.state.bold = enabled
            elif word == "i":
                if self.state.italic != enabled:
                    self._flush_text()
                    self.state.italic = enabled
            else:
                if self.state.underline != enabled:
                    self._flush_text()
                    self.state.underline = enabled
        elif word == "ulnone":
            if parameter is not None:
                self._error("Controlul \\ulnone nu acceptă parametru.")
            if self.state.underline:
                self._flush_text()
                self.state.underline = False
        elif word == "uc":
            if parameter is None or not 0 <= parameter <= 16:
                self._error("\\uc trebuie să aibă un parametru între 0 și 16.")
            self.state.uc_skip = parameter
        elif word in {"ansi", "ansicpg"}:
            if word == "ansi" and parameter is not None:
                self._error("Controlul \\ansi nu acceptă parametru.")
            if word == "ansicpg" and parameter != 1252:
                self._error("Sunt acceptate numai documente RTF cu pagina ANSI 1252.")
        elif word == "par":
            if parameter is not None:
                self._error("Controlul \\par nu acceptă parametru.")
            self._append_paragraph()
        elif word == "line":
            if parameter is not None:
                self._error("Controlul \\line nu acceptă parametru.")
            self._append_line_break()
        else:
            self._error(f"Control RTF nesuportat: \\{word}.")

    def _parse_hex_escape(self) -> None:
        if self.position + 3 >= len(self.source):
            self._error("Secvența RTF \\' trebuie urmată de două cifre hexazecimale.")
        hexadecimal = self.source[self.position + 2 : self.position + 4]
        try:
            byte_value = int(hexadecimal, 16)
            character = bytes((byte_value,)).decode("cp1252")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RtfCodecError(f"Secvență hexazecimală RTF invalidă: {hexadecimal!r}.") from exc
        self._append_character(character)
        self.position += 4

    def _parse_unicode_escape(self, parameter: int | None) -> None:
        if parameter is None or not -32768 <= parameter <= 32767:
            self._error("Controlul \\u necesită un număr semnat de 16 biți.")
        unit = parameter if parameter >= 0 else parameter + 0x10000
        if 0xD800 <= unit <= 0xDBFF:
            if self.pending_high_surrogate is not None:
                self._error("Secvență RTF cu pereche surrogate incompletă.")
            self.pending_high_surrogate = (unit, self.state.style)
        elif 0xDC00 <= unit <= 0xDFFF:
            if self.pending_high_surrogate is None:
                self._error("Surrogate low RTF fără surrogate high anterior.")
            high, style = self.pending_high_surrogate
            if style != self.state.style:
                self._error("Perechea surrogate RTF are formatare mixtă.")
            codepoint = 0x10000 + ((high - 0xD800) << 10) + (unit - 0xDC00)
            self.pending_high_surrogate = None
            self._append_character(chr(codepoint), style=style)
        else:
            if self.pending_high_surrogate is not None:
                self._error("Surrogate high RTF nu este urmat de un surrogate low.")
            self._append_character(chr(unit))
        self._skip_unicode_fallback(self.state.uc_skip)

    def _skip_unicode_fallback(self, count: int) -> None:
        for _ in range(count):
            if self.position >= len(self.source):
                self._error("Lipsește textul fallback al unei secvențe \\u.")
            character = self.source[self.position]
            if character in "{}\r\n":
                self._error("Text fallback RTF invalid sau incomplet după \\u.")
            if character != "\\":
                self.position += 1
                continue
            if self.position + 1 >= len(self.source):
                self._error("Text fallback RTF incomplet după \\u.")
            escaped = self.source[self.position + 1]
            if escaped in "\\{}~_-":
                self.position += 2
            elif escaped == "'" and self.position + 3 < len(self.source):
                hexadecimal = self.source[self.position + 2 : self.position + 4]
                try:
                    byte_value = int(hexadecimal, 16)
                    bytes((byte_value,)).decode("cp1252")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RtfCodecError("Fallback hexazecimal RTF invalid după \\u.") from exc
                self.position += 4
            else:
                self._error("Tip de caracter fallback nesuportat după \\u.")

    def _append_character(
        self,
        character: str,
        *,
        style: tuple[bool, bool, bool] | None = None,
    ) -> None:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs", "Zl", "Zp"}:
            self._error("RTF conține caractere de control sau separatori nesuportați.")
        self._increment_character_count(1)
        effective_style = self.state.style if style is None else style
        if self.text_buffer_style != effective_style:
            self._flush_text()
            self.text_buffer_style = effective_style
        self.text_buffer.append(character)
        self.current_has_content = True

    def _flush_text(self) -> None:
        if not self.text_buffer:
            self.text_buffer_style = None
            return
        text = "".join(self.text_buffer)
        style = self.text_buffer_style
        assert style is not None
        self.current_paragraph.inlines.append(TextInline(text, *style))
        self.text_buffer.clear()
        self.text_buffer_style = None

    def _append_line_break(self) -> None:
        self._flush_text()
        self._increment_character_count(1)
        self.current_paragraph.inlines.append(LineBreakInline())
        self.current_has_content = True

    def _append_paragraph(self) -> None:
        self._flush_text()
        self._increment_character_count(1)
        self.paragraphs.append(self.current_paragraph)
        self.current_paragraph = Paragraph()
        self.current_has_content = False

    def _increment_character_count(self, amount: int) -> None:
        self.character_count += amount
        if self.character_count > MAX_DOCUMENT_CHARACTERS:
            self._error(
                f"Documentul RTF depășește limita de {MAX_DOCUMENT_CHARACTERS} caractere."
            )

    def _require_no_pending_surrogate(self) -> None:
        if self.pending_high_surrogate is not None:
            self._error("Secvență RTF cu surrogate high fără pereche low.")

    def _error(self, message: str) -> None:
        raise RtfCodecError(f"{message} (poziția {self.position}).")


def decode_rtf(source: str) -> RichDocument:
    """Decodifică subsetul RTF sigur acceptat de OrtoDocs.

    Sunt acceptate numai text simplu, grupuri de formatare B/I/U, ``\\par``,
    ``\\line``, secvențe Unicode și escapările de caractere RTF cunoscute.
    Câmpurile, obiectele, imaginile și orice control necunoscut sunt refuzate.
    """

    return _Parser(source).parse()


__all__ = [
    "MAX_DOCUMENT_CHARACTERS",
    "MAX_GROUP_DEPTH",
    "MAX_RTF_INPUT_LENGTH",
    "RtfCodecError",
    "decode_rtf",
    "encode_rtf",
]
