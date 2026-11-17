"""Înlocuirea literală a token-urilor într-un document Rich Text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ortodocs.models.rich_document import (
    LineBreakInline,
    Paragraph,
    RichDocument,
    TextInline,
)


# Păstrată identic cu regula din docx_import._VARIABLE_KEY.
_VARIABLE_KEY = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_LINE_BREAK_SENTINEL = "\x00"
_PARAGRAPH_BREAK_SENTINEL = "\x01"


class TokenSubstitutionError(ValueError):
    """Documentul sau valorile nu pot fi substituite fără pierderi."""


@dataclass(frozen=True, slots=True)
class _TokenOccurrence:
    paragraph_index: int
    start: int
    end: int
    variable_key: str
    style: tuple[object, ...]


def _clone_document(document: RichDocument) -> RichDocument:
    if not isinstance(document, RichDocument):
        raise TokenSubstitutionError("document trebuie să fie un RichDocument valid.")
    try:
        return RichDocument.from_dict(document.to_dict())
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError) as exc:
        raise TokenSubstitutionError(
            f"Documentul Rich Text este invalid și nu poate fi substituit: {exc}"
        ) from exc


def _snapshot_values(
    values: Mapping[str, str],
    expected_keys: set[str],
) -> tuple[dict[str, str], frozenset[str]]:
    if not isinstance(values, Mapping):
        raise TokenSubstitutionError("values trebuie să fie o mapare de chei la șiruri.")
    if not isinstance(expected_keys, set):
        raise TokenSubstitutionError("expected_keys trebuie să fie un set de chei.")

    expected = frozenset(expected_keys)
    invalid_expected = sorted(
        (key for key in expected if not isinstance(key, str) or not _VARIABLE_KEY.fullmatch(key)),
        key=repr,
    )
    if invalid_expected:
        raise TokenSubstitutionError(
            f"expected_keys conține chei cu sintaxă invalidă: {invalid_expected!r}."
        )

    try:
        value_map = dict(values)
    except (TypeError, ValueError) as exc:
        raise TokenSubstitutionError("values nu poate fi copiată ca mapare validă.") from exc

    actual = set(value_map)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        problems: list[str] = []
        if missing:
            problems.append(f"lipsesc valorile pentru cheile {sorted(missing)!r}")
        if extra:
            problems.append(f"există chei suplimentare {sorted(extra, key=repr)!r}")
        raise TokenSubstitutionError("Maparea values nu corespunde cheilor așteptate: " + "; ".join(problems) + ".")

    for key, value in value_map.items():
        if not isinstance(key, str):
            raise TokenSubstitutionError(f"Cheia valorii {key!r} trebuie să fie text.")
        if not isinstance(value, str):
            raise TokenSubstitutionError(f"Valoarea pentru cheia {key!r} trebuie să fie text.")
        try:
            # TextInline aplică exact restricțiile structurale ale RichDocument.
            TextInline(value)
        except (TypeError, ValueError) as exc:
            raise TokenSubstitutionError(
                f"Valoarea pentru cheia {key!r} nu poate fi reprezentată în Rich Text: {exc}"
            ) from exc

    return value_map, expected


def _flatten_document(
    document: RichDocument,
) -> tuple[str, list[tuple[object, ...] | None], list[tuple[int, int] | None]]:
    parts: list[str] = []
    styles: list[tuple[object, ...] | None] = []
    locations: list[tuple[int, int] | None] = []

    for paragraph_index, paragraph in enumerate(document.paragraphs):
        paragraph_parts: list[str] = []
        paragraph_styles: list[tuple[object, ...] | None] = []
        paragraph_locations: list[tuple[int, int] | None] = []
        local_position = 0

        for inline in paragraph.inlines:
            if isinstance(inline, TextInline):
                style = inline.formatting_key()
                paragraph_parts.append(inline.text)
                paragraph_styles.extend([style] * len(inline.text))
                paragraph_locations.extend(
                    (paragraph_index, local_position + offset)
                    for offset in range(len(inline.text))
                )
                local_position += len(inline.text)
            elif isinstance(inline, LineBreakInline):
                paragraph_parts.append(_LINE_BREAK_SENTINEL)
                paragraph_styles.append(None)
                paragraph_locations.append((paragraph_index, local_position))
                local_position += 1
            else:  # Documentele mutate după construire sunt validate prin clonare.
                raise TokenSubstitutionError(
                    f"Fragment Rich Text nesuportat în paragraful {paragraph_index + 1}."
                )

        parts.extend(paragraph_parts)
        styles.extend(paragraph_styles)
        locations.extend(paragraph_locations)
        if paragraph_index < len(document.paragraphs) - 1:
            parts.append(_PARAGRAPH_BREAK_SENTINEL)
            styles.append(None)
            locations.append(None)

    return "".join(parts), styles, locations


def _scan_tokens(
    document: RichDocument,
    expected_keys: frozenset[str],
) -> list[_TokenOccurrence]:
    text, styles, locations = _flatten_document(document)
    occurrences: list[_TokenOccurrence] = []
    found_keys: set[str] = set()
    index = 0

    while index < len(text):
        if text.startswith("{{", index):
            close = text.find("}}", index + 2)
            nested = text.find("{{", index + 2, close if close >= 0 else len(text))
            location = locations[index]
            paragraph_number = location[0] + 1 if location is not None else 1

            if nested >= 0:
                raise TokenSubstitutionError(
                    f"Token imbricat la paragraful {paragraph_number}; marcajele {{...}} nu pot fi imbricate."
                )
            if close < 0:
                raise TokenSubstitutionError(
                    f"Token incomplet la paragraful {paragraph_number}: marcajul {{ deschis nu are închidere }}."
                )

            token_units = text[index : close + 2]
            if _PARAGRAPH_BREAK_SENTINEL in token_units:
                raise TokenSubstitutionError(
                    f"Un token de la paragraful {paragraph_number} traversează limita unui paragraf."
                )
            if _LINE_BREAK_SENTINEL in token_units:
                raise TokenSubstitutionError(
                    f"Un token de la paragraful {paragraph_number} traversează o întrerupere de rând."
                )

            key = text[index + 2 : close]
            if not _VARIABLE_KEY.fullmatch(key):
                raise TokenSubstitutionError(
                    f"Tokenul {token_units!r} de la paragraful {paragraph_number} are o cheie invalidă."
                )
            if key not in expected_keys:
                raise TokenSubstitutionError(
                    f"Token necunoscut {key!r} la paragraful {paragraph_number}; cheia nu este așteptată."
                )

            token_styles = set(styles[index : close + 2])
            if None in token_styles or len(token_styles) != 1:
                raise TokenSubstitutionError(
                    f"Tokenul {key!r} de la paragraful {paragraph_number} are formatare B/I/U mixtă."
                )

            start_location = locations[index]
            end_location = locations[close + 1]
            if start_location is None or end_location is None or start_location[0] != end_location[0]:
                raise TokenSubstitutionError(
                    f"Tokenul {key!r} nu poate traversa limitele structurale ale documentului."
                )

            style = next(iter(token_styles))
            assert style is not None
            occurrences.append(
                _TokenOccurrence(
                    paragraph_index=start_location[0],
                    start=start_location[1],
                    end=end_location[1] + 1,
                    variable_key=key,
                    style=style,
                )
            )
            found_keys.add(key)
            index = close + 2
            continue

        if text.startswith("}}", index):
            location = locations[index]
            paragraph_number = location[0] + 1 if location is not None else 1
            raise TokenSubstitutionError(
                f"Marcajul de închidere }} apare fără token de deschidere la paragraful {paragraph_number}."
            )
        index += 1

    missing_tokens = expected_keys - found_keys
    if missing_tokens:
        raise TokenSubstitutionError(
            f"Lipsesc din document token-urile pentru cheile așteptate {sorted(missing_tokens)!r}."
        )
    return occurrences


def _append_inline_slice(
    output: list[TextInline | LineBreakInline],
    inlines: list[TextInline | LineBreakInline],
    start: int,
    end: int,
) -> None:
    position = 0
    for inline in inlines:
        inline_length = len(inline.text) if isinstance(inline, TextInline) else 1
        inline_end = position + inline_length
        overlap_start = max(start, position)
        overlap_end = min(end, inline_end)

        if overlap_start < overlap_end:
            if isinstance(inline, TextInline):
                begin = overlap_start - position
                finish = overlap_end - position
                output.append(inline.clone_with_text(inline.text[begin:finish]))
            else:
                output.append(LineBreakInline())

        position = inline_end


def _substitute_paragraph(
    paragraph: Paragraph,
    occurrences: list[_TokenOccurrence],
    values: Mapping[str, str],
) -> Paragraph:
    if not occurrences:
        return paragraph

    output: list[TextInline | LineBreakInline] = []
    cursor = 0
    for occurrence in occurrences:
        _append_inline_slice(output, paragraph.inlines, cursor, occurrence.start)
        r_fonts_items = occurrence.style[3]
        output.append(
            TextInline(
                values[occurrence.variable_key],
                bold=bool(occurrence.style[0]),
                italic=bool(occurrence.style[1]),
                underline=bool(occurrence.style[2]),
                r_fonts=dict(r_fonts_items) if r_fonts_items is not None else None,  # type: ignore[arg-type]
                font_size_half_points=occurrence.style[4],  # type: ignore[arg-type]
                complex_font_size_half_points=occurrence.style[5],  # type: ignore[arg-type]
            )
        )
        cursor = occurrence.end

    total_length = sum(len(inline.text) if isinstance(inline, TextInline) else 1 for inline in paragraph.inlines)
    _append_inline_slice(output, paragraph.inlines, cursor, total_length)
    return Paragraph(output)


def substitute_variables(
    document: RichDocument,
    values: Mapping[str, str],
    expected_keys: set[str],
) -> RichDocument:
    """Înlocuiește token-urile așteptate și întoarce un RichDocument nou.

    Textul introdus este literal și este validat conform regulilor TextInline.
    Token-urile sunt recunoscute între fragmente TextInline adiacente, fără să
    traverseze întreruperi de rând sau limite de paragraf.
    """

    value_map, expected = _snapshot_values(values, expected_keys)
    cloned_document = _clone_document(document)
    occurrences = _scan_tokens(cloned_document, expected)

    occurrences_by_paragraph: dict[int, list[_TokenOccurrence]] = {}
    for occurrence in occurrences:
        occurrences_by_paragraph.setdefault(occurrence.paragraph_index, []).append(occurrence)

    paragraphs = [
        _substitute_paragraph(paragraph, occurrences_by_paragraph.get(index, []), value_map)
        for index, paragraph in enumerate(cloned_document.paragraphs)
    ]
    return RichDocument(paragraphs=paragraphs, schema_version=cloned_document.schema_version)
