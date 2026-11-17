"""Conversie între modelul RichDocument și widgetul tkinter.Text."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import font as tkfont

from ortodocs.models.rich_document import (
    LineBreakInline,
    Paragraph,
    RichDocument,
    TextInline,
)


PARAGRAPH_SEPARATOR_TAG = "_ortodocs_codec_paragraph_separator"
LINE_BREAK_TAG = "_ortodocs_codec_line_break"

_STYLE_TAGS: dict[tuple[bool, bool, bool], str] = {
    (bold, italic, underline): (
        f"_ortodocs_codec_style_{int(bold)}{int(italic)}{int(underline)}"
    )
    for bold in (False, True)
    for italic in (False, True)
    for underline in (False, True)
}
_STYLE_BY_TAG = {tag: style for style, tag in _STYLE_TAGS.items()}


class RichTextCodecError(ValueError):
    """Widgetul conține structură pe care RichDocument nu o poate reprezenta."""


@dataclass(slots=True)
class RichTextRenderState:
    """Stare asociată randării, inclusiv distincția dintre două documente goale."""

    _widget: tk.Text = field(repr=False)
    _was_zero_paragraph_document: bool = False


def _validate_widget(widget: tk.Text) -> None:
    if not isinstance(widget, tk.Text):
        raise TypeError("widget trebuie să fie un tkinter.Text.")


def _clone_and_validate_document(document: RichDocument) -> RichDocument:
    if not isinstance(document, RichDocument):
        raise TypeError("document trebuie să fie un RichDocument.")
    try:
        # Dataclasses sunt mutabile; serializarea verifică și starea curentă a
        # listelor interne fără să schimbe obiectul primit.
        return RichDocument.from_json(document.to_json())
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise RichTextCodecError(f"document nu este un RichDocument valid: {exc}") from exc


def _configure_style_tags(widget: tk.Text) -> tuple[tkfont.Font, ...]:
    base_font = tkfont.Font(root=widget, font=widget.cget("font"))
    actual = base_font.actual()
    fonts: list[tkfont.Font] = []

    for style, tag_name in _STYLE_TAGS.items():
        bold, italic, underline = style
        style_font = tkfont.Font(
            root=widget,
            family=actual["family"],
            size=actual["size"],
            weight="bold" if bold else "normal",
            slant="italic" if italic else "roman",
            underline=int(underline),
            overstrike=0,
        )
        widget.tag_configure(tag_name, font=style_font)
        widget.tag_raise(tag_name)
        fonts.append(style_font)

    widget.tag_configure(PARAGRAPH_SEPARATOR_TAG)
    widget.tag_configure(LINE_BREAK_TAG)
    return tuple(fonts)


def render_rich_document(
    widget: tk.Text,
    document: RichDocument,
) -> RichTextRenderState:
    """Înlocuiește conținutul widgetului cu documentul și aplică tagurile codec-ului.

    Newline-urile explicite sunt inserate numai între paragrafe sau pentru
    LineBreakInline. Newline-ul terminal propriu widgetului nu este document.
    Tagurile altor componente nu sunt șterse sau reconfigurate.
    """

    _validate_widget(widget)
    snapshot = _clone_and_validate_document(document)
    font_refs = _configure_style_tags(widget)

    original_state = str(widget.cget("state"))
    if original_state == "disabled":
        widget.configure(state="normal")
    try:
        widget.delete("1.0", "end")

        for paragraph_index, paragraph in enumerate(snapshot.paragraphs):
            for inline in paragraph.inlines:
                insertion_index = "end-1c"
                if isinstance(inline, TextInline):
                    if inline.text:
                        style = (inline.bold, inline.italic, inline.underline)
                        widget.insert(insertion_index, inline.text, (_STYLE_TAGS[style],))
                elif isinstance(inline, LineBreakInline):
                    widget.insert(insertion_index, "\n", (LINE_BREAK_TAG,))
                else:  # Protecție pentru model mutat după construirea sa.
                    raise RichTextCodecError(
                        f"Fragment Rich Text nesuportat în paragraful {paragraph_index + 1}."
                    )

            if paragraph_index < len(snapshot.paragraphs) - 1:
                widget.insert("end-1c", "\n", (PARAGRAPH_SEPARATOR_TAG,))

        # Referințele Python țin în viață fonturile numite folosite de taguri.
        setattr(widget, "_ortodocs_codec_font_refs", font_refs)
        widget.edit_modified(False)
        widget.edit_reset()
    finally:
        if original_state == "disabled":
            widget.configure(state="disabled")

    return RichTextRenderState(
        _widget=widget,
        _was_zero_paragraph_document=not snapshot.paragraphs,
    )


def _append_text(
    paragraph: Paragraph,
    text: str,
    style: tuple[bool, bool, bool],
    index: str,
) -> None:
    if not text:
        return
    try:
        inline = TextInline(
            text=text,
            bold=style[0],
            italic=style[1],
            underline=style[2],
        )
    except (TypeError, ValueError) as exc:
        raise RichTextCodecError(
            f"Textul din widget la indexul {index} nu poate fi reprezentat: {exc}"
        ) from exc

    if paragraph.inlines and isinstance(paragraph.inlines[-1], TextInline):
        previous = paragraph.inlines[-1]
        if (previous.bold, previous.italic, previous.underline) == style:
            previous.text += text
            return
    paragraph.inlines.append(inline)


def read_rich_document(
    widget: tk.Text,
    state: RichTextRenderState | None = None,
) -> RichDocument:
    """Reconstruiește RichDocument din textul și tagurile efective ale widgetului.

    Un newline fără unul dintre tagurile structurale ale codec-ului este
    respins explicit. Fără state, un widget vizual gol reprezintă un paragraf
    gol. Dacă starea arată că a fost randat un document cu zero paragrafe și
    widgetul nu a fost modificat, se păstrează zero paragrafe.
    """

    _validate_widget(widget)
    if state is not None and not isinstance(state, RichTextRenderState):
        raise TypeError("state trebuie să fie RichTextRenderState sau None.")
    if state is not None and state._widget is not widget:
        raise ValueError("state aparține unui alt widget tkinter.Text.")

    logical_text = widget.get("1.0", "end-1c")
    if (
        state is not None
        and state._was_zero_paragraph_document
        and not widget.edit_modified()
        and logical_text == ""
    ):
        return RichDocument()

    paragraphs: list[Paragraph] = []
    current_paragraph = Paragraph()
    active_tags: set[str] = set()

    for kind, value, index in widget.dump("1.0", "end-1c", text=True, tag=True):
        if kind == "tagon":
            active_tags.add(value)
            continue
        if kind == "tagoff":
            active_tags.discard(value)
            continue
        if kind != "text":
            continue

        style_tags = active_tags.intersection(_STYLE_BY_TAG)
        if len(style_tags) > 1:
            raise RichTextCodecError(
                f"S-au găsit mai multe taguri de stil la indexul {index}."
            )
        style = _STYLE_BY_TAG[next(iter(style_tags))] if style_tags else (False, False, False)

        offset = 0
        pieces = value.split("\n")
        for piece_index, piece in enumerate(pieces):
            piece_index_in_widget = (
                index if offset == 0 else widget.index(f"{index}+{offset}c")
            )
            if piece:
                if PARAGRAPH_SEPARATOR_TAG in active_tags or LINE_BREAK_TAG in active_tags:
                    raise RichTextCodecError(
                        "Un tag structural OrtoDocs este aplicat textului "
                        f"de la indexul {piece_index_in_widget}."
                    )
                _append_text(current_paragraph, piece, style, piece_index_in_widget)
                offset += len(piece)

            if piece_index < len(pieces) - 1:
                paragraph_tagged = PARAGRAPH_SEPARATOR_TAG in active_tags
                line_break_tagged = LINE_BREAK_TAG in active_tags
                if paragraph_tagged == line_break_tagged:
                    newline_index = widget.index(f"{index}+{offset}c")
                    raise RichTextCodecError(
                        f"Newline-ul de la indexul {newline_index} nu are un singur "
                        "tag structural OrtoDocs."
                    )
                if paragraph_tagged:
                    paragraphs.append(current_paragraph)
                    current_paragraph = Paragraph()
                else:
                    current_paragraph.inlines.append(LineBreakInline())
                offset += 1

    paragraphs.append(current_paragraph)
    return RichDocument(paragraphs=paragraphs)
