"""Editor Tk reutilizabil pentru documente Rich Text OrtoDocs."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

from ortodocs.models.rich_document import RichDocument
from ortodocs.ui.rich_text_codec import (
    LINE_BREAK_TAG,
    PARAGRAPH_SEPARATOR_TAG,
    RichTextRenderState,
    read_rich_document,
    render_rich_document,
)


_STYLE_TAG_PREFIX = "_ortodocs_codec_style_"
_STYLE_COMBINATIONS = tuple(
    (bold, italic, underline)
    for bold in (False, True)
    for italic in (False, True)
    for underline in (False, True)
)
_STYLE_TAGS = {
    style: f"{_STYLE_TAG_PREFIX}{int(style[0])}{int(style[1])}{int(style[2])}"
    for style in _STYLE_COMBINATIONS
}
_STYLE_FOR_TAG = {tag: style for style, tag in _STYLE_TAGS.items()}
_STYLE_ATTRIBUTE_INDEX = {"bold": 0, "italic": 1, "underline": 2}
_FORMAT_SHORTCUT_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("<Control-b>", "bold"),
    ("<Control-B>", "bold"),
    ("<Control-i>", "italic"),
    ("<Control-I>", "italic"),
    ("<Control-u>", "underline"),
    ("<Control-U>", "underline"),
)
_NAVIGATION_AND_DELETE_KEYSYMS = frozenset(
    {
        "Left",
        "Right",
        "Up",
        "Down",
        "Home",
        "End",
        "Prior",
        "Next",
        "BackSpace",
        "Delete",
    }
)
_MUTATING_TEXT_COMMANDS = {"insert", "delete", "replace"}
_MUTATING_EDIT_SUBCOMMANDS = {"undo", "redo"}


@dataclass(frozen=True, slots=True)
class _EditorUndoState:
    """Snapshot al documentului, cursorului și selecției pentru Undo/Redo."""

    document: RichDocument
    insert_index: str
    selection: tuple[str, str] | None
    active_style: tuple[bool, bool, bool]


class RichTextEditorError(ValueError):
    """Widgetul nu poate fi reprezentat în modelul RichDocument existent."""


def _copy_document(document: RichDocument) -> RichDocument:
    if not isinstance(document, RichDocument):
        raise TypeError("document trebuie să fie un RichDocument.")
    try:
        return RichDocument.from_json(document.to_json())
    except (AttributeError, RecursionError, TypeError, ValueError) as exc:
        raise RichTextEditorError(f"document nu este valid: {exc}") from exc


class RichTextEditor(ttk.Frame):
    """Componentă editabilă Rich Text cu formatare B/I/U și stare dirty."""

    def __init__(
        self,
        parent: tk.Misc,
        document: RichDocument,
        *,
        on_dirty_changed: Callable[[bool], None] | None = None,
    ) -> None:
        initial_document = _copy_document(document)
        if on_dirty_changed is not None and not callable(on_dirty_changed):
            raise TypeError("on_dirty_changed trebuie să fie apelabil sau None.")

        super().__init__(parent)
        self._on_dirty_changed = on_dirty_changed
        self._saved_snapshot = initial_document
        self._render_state: RichTextRenderState | None = None
        self._dirty = False
        self._suspend_dirty = 0
        self._active_style = (False, False, False)
        self._pending_style_cursor: str | None = None
        self._undo_stack: list[_EditorUndoState] = []
        self._redo_stack: list[_EditorUndoState] = []
        self._current_undo_state: _EditorUndoState | None = None
        self._undo_separator_pending = True
        self._last_undo_command: str | None = None
        self._proxy_installed = False
        self._destroyed = False
        self._original_text_command = ""
        self._prepare_text_command_name = ""
        self._after_text_command_name = ""
        self._style_variables: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(master=self, value=False)
            for name in _STYLE_ATTRIBUTE_INDEX
        }

        self._build_widgets()
        self._original_text_command = f"{self.text_widget._w}_ortodocs_original"
        self._prepare_text_command_name = f"{self.text_widget._w}_ortodocs_prepare"
        self._after_text_command_name = f"{self.text_widget._w}_ortodocs_after"
        self._install_text_proxy()
        self.set_document(initial_document)

    @property
    def is_dirty(self) -> bool:
        """True când textul sau formatarea diferă de ultima stare salvată."""

        return self._dirty

    def get_document(self) -> RichDocument:
        """Reconstruiește și returnează un RichDocument independent."""

        return read_rich_document(self.text_widget, self._render_state)

    def mark_saved(self) -> None:
        """Marchează documentul curent drept snapshot salvat în memorie."""

        self._saved_snapshot = _copy_document(self.get_document())
        self._set_dirty(False)

    def set_document(self, document: RichDocument) -> None:
        """Înlocuiește documentul din editor și resetează cursorul și dirty."""

        snapshot = _copy_document(document)
        self._suspend_dirty += 1
        try:
            self._render_state = render_rich_document(self.text_widget, snapshot)
            self.text_widget.tag_remove("sel", "1.0", "end")
            self.text_widget.mark_set("insert", "1.0")
            self.text_widget.see("1.0")
            self.text_widget.edit_modified(False)
            self.text_widget.edit_reset()
        finally:
            self._suspend_dirty -= 1

        self._saved_snapshot = _copy_document(self.get_document())
        self._pending_style_cursor = None
        self._active_style = (False, False, False)
        self._sync_active_style_from_cursor()
        self._reset_undo_history()
        self._set_dirty(False)
        self._update_toolbar_state()

    def destroy(self) -> None:
        """Elimină proxy-ul Tcl înainte de distrugerea widgeturilor copil."""

        if not self._destroyed:
            self._destroyed = True
            self._remove_text_proxy(restore_original=True)
        super().destroy()

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        toolbar.columnconfigure(3, weight=1)

        self.bold_button = self._make_format_button(toolbar, "bold", "B")
        self.bold_button.grid(row=0, column=0, padx=(0, 2))
        self.italic_button = self._make_format_button(toolbar, "italic", "I")
        self.italic_button.grid(row=0, column=1, padx=2)
        self.underline_button = self._make_format_button(toolbar, "underline", "U")
        self.underline_button.grid(row=0, column=2, padx=2)

        self.dirty_indicator = ttk.Label(
            toolbar,
            text="Modificări nesalvate",
            style="RichTextEditor.Dirty.TLabel",
        )
        self.dirty_indicator.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self.dirty_indicator.grid_remove()

        self.text_widget = tk.Text(
            self,
            wrap="word",
            undo=True,
            exportselection=False,
            padx=8,
            pady=8,
        )
        self.text_widget.grid(row=1, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.text_widget.yview,
        )
        self.scrollbar.grid(row=1, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=self.scrollbar.set)

        for sequence, attribute in _FORMAT_SHORTCUT_SEQUENCES:
            self.text_widget.bind(
                sequence,
                lambda event, attr=attribute: self._on_format_shortcut(attr, event),
            )
        self.text_widget.bind("<Return>", self._insert_paragraph_separator, add="+")
        self.text_widget.bind("<KP_Enter>", self._insert_paragraph_separator, add="+")
        self.text_widget.bind("<Shift-Return>", self._insert_line_break, add="+")
        self.text_widget.bind("<Shift-KP_Enter>", self._insert_line_break, add="+")
        for sequence in (
            "<KeyRelease>",
            "<ButtonRelease-1>",
            "<ButtonRelease-2>",
            "<ButtonRelease-3>",
        ):
            self.text_widget.bind(sequence, self._on_cursor_or_selection_changed, add="+")
        self.text_widget.bind("<Destroy>", self._on_text_destroyed, add="+")

    def _make_format_button(
        self,
        parent: ttk.Frame,
        attribute: str,
        label: str,
    ) -> ttk.Checkbutton:
        button = ttk.Checkbutton(
            parent,
            text=label,
            variable=self._style_variables[attribute],
            style="Toolbutton",
            takefocus=False,
        )
        button.configure(command=lambda: self._toggle_style(attribute))
        return button

    def _install_text_proxy(self) -> None:
        tk_widget = self.text_widget.tk
        try:
            tk_widget.call(
                "rename",
                self.text_widget._w,
                self._original_text_command,
            )
            tk_widget.createcommand(
                self._prepare_text_command_name,
                self._prepare_text_command,
            )
            tk_widget.createcommand(
                self._after_text_command_name,
                self._after_text_command,
            )
            # Executăm comanda widgetului printr-un proc Tcl cu uplevel, astfel încât
            # blocurile native `catch` din text.tcl să gestioneze erorile Tcl
            # fără a seta flag-ul C `_tkinter` errorInCmd din callback-uri Python.
            proxy_body = (
                'if {($command eq "insert" && [llength $args] == 2) '
                '|| ($command eq "replace" && [llength $args] == 3)} {\n'
                f'    set forwarded [{self._prepare_text_command_name} $command {{*}}$args]\n'
                f'    set result [uplevel 1 [list {self._original_text_command} $command {{*}}$forwarded]]\n'
                f'    {self._after_text_command_name} $command {{*}}$args\n'
                '    return $result\n'
                '}\n'
                'if {$command eq "edit"} {\n'
                '    set subcommand [lindex $args 0]\n'
                '    if {$subcommand eq "undo" || $subcommand eq "redo"} {\n'
                f'        if {{![{self._after_text_command_name} edit $subcommand]}} {{\n'
                '            return -code error "nothing to $subcommand"\n'
                '        }\n'
                '        return ""\n'
                '    }\n'
                f'    set result [uplevel 1 [list {self._original_text_command} $command {{*}}$args]]\n'
                '    if {$subcommand eq "reset" || $subcommand eq "separator"} {\n'
                f'        {self._after_text_command_name} edit $subcommand\n'
                '    }\n'
                '    return $result\n'
                '}\n'
                f'set result [uplevel 1 [list {self._original_text_command} $command {{*}}$args]]\n'
                'if {$command eq "insert" || $command eq "replace" || $command eq "delete" '
                '|| ($command eq "mark" && [lindex $args 0] eq "set" && [lindex $args 1] eq "insert")} {\n'
                f'    {self._after_text_command_name} $command {{*}}$args\n'
                '}\n'
                'return $result'
            )
            tk_widget.call("proc", self.text_widget._w, "command args", proxy_body)
        except tk.TclError as exc:
            self._cleanup_partial_proxy_installation()
            raise RuntimeError("Nu s-a putut instala proxy-ul Tcl pentru tkinter.Text.") from exc
        self._proxy_installed = True

    def _cleanup_partial_proxy_installation(self) -> None:
        tk_widget = self.text_widget.tk
        for helper_command in (
            self._prepare_text_command_name,
            self._after_text_command_name,
        ):
            if not helper_command:
                continue
            try:
                tk_widget.deletecommand(helper_command)
            except (tk.TclError, ValueError):
                pass
        try:
            original_exists = bool(
                tk_widget.call("info", "commands", self._original_text_command)
            )
        except tk.TclError:
            original_exists = False
        if original_exists:
            try:
                if tk_widget.call("info", "commands", self.text_widget._w):
                    tk_widget.call("rename", self.text_widget._w, "")
            except tk.TclError:
                pass
            try:
                tk_widget.call("rename", self._original_text_command, self.text_widget._w)
            except tk.TclError:
                pass

    def _remove_text_proxy(self, *, restore_original: bool) -> None:
        if not self._proxy_installed:
            return
        tk_widget = self.text_widget.tk
        try:
            tk_widget.call("rename", self.text_widget._w, "")
        except tk.TclError:
            pass
        for helper_command in (
            self._prepare_text_command_name,
            self._after_text_command_name,
        ):
            if not helper_command:
                continue
            try:
                tk_widget.deletecommand(helper_command)
            except (tk.TclError, ValueError):
                pass
        try:
            if restore_original:
                tk_widget.call(
                    "rename",
                    self._original_text_command,
                    self.text_widget._w,
                )
            else:
                tk_widget.call("rename", self._original_text_command, "")
        except tk.TclError:
            pass
        self._proxy_installed = False

    def _on_text_destroyed(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.text_widget:
            self._remove_text_proxy(restore_original=False)

    def _prepare_text_command(
        self,
        command: str,
        *arguments: str,
    ) -> tuple[object, ...] | tuple[str, ...]:
        if command == "insert":
            return self._transform_plain_insertion(arguments, replace=False)
        if command == "replace":
            return self._transform_plain_insertion(arguments, replace=True)
        return arguments

    def _after_text_command(self, command: str, *arguments: str) -> int | str:
        if (
            command == "edit"
            and arguments
            and arguments[0] in _MUTATING_EDIT_SUBCOMMANDS
        ):
            if self._suspend_dirty:
                return 0
            return 1 if self._perform_undo_or_redo(arguments[0]) else 0

        if self._suspend_dirty:
            return ""

        if command == "edit" and arguments:
            subcommand = arguments[0]
            if subcommand == "reset":
                self._reset_undo_history()
            elif subcommand == "separator":
                self._mark_undo_separator()
            return ""

        if command == "mark":
            self._pending_style_cursor = None
            if self._selected_text_indices() is None:
                self._sync_active_style_from_cursor()
            self._update_toolbar_state()
            self._mark_undo_separator()
            return ""

        if command in _MUTATING_TEXT_COMMANDS:
            self._record_undo_mutation(command_name=command)
            self._refresh_dirty()
        return ""

    def _transform_plain_insertion(
        self,
        arguments: tuple[str, ...],
        *,
        replace: bool,
    ) -> tuple[object, ...] | tuple[str, ...]:
        prefix_length = 2 if replace else 1
        expected_length = prefix_length + 1
        if len(arguments) != expected_length:
            # Tagurile explicite sunt folosite de codec și de comenzile
            # structurale; ele trebuie transmise fără nicio modificare.
            return arguments

        text = arguments[-1]
        if not text:
            return arguments

        # Textul simplu lipit poate conține rânduri noi. Modelăm fiecare
        # asemenea rând drept paragraf, iar codec-ul păstrează separat
        # întreruperile LineBreakInline introduse de Shift+Enter.
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        pieces = normalized_text.split("\n")
        insertion_parts: list[object] = []
        for index, piece in enumerate(pieces):
            if piece:
                insertion_parts.extend((piece, (self._style_tag(self._active_style),)))
            if index < len(pieces) - 1:
                insertion_parts.extend(("\n", (PARAGRAPH_SEPARATOR_TAG,)))

        return (*arguments[:-1], *insertion_parts)

    def _style_tag(self, style: tuple[bool, bool, bool]) -> str:
        return _STYLE_TAGS[style]

    def _style_at(self, index: str) -> tuple[bool, bool, bool]:
        tags = set(self.text_widget.tag_names(index))
        found = [style for tag, style in _STYLE_FOR_TAG.items() if tag in tags]
        if len(found) > 1:
            raise RichTextEditorError(f"Mai multe taguri de stil sunt active la indexul {index}.")
        return found[0] if found else (False, False, False)

    def _style_at_cursor(self) -> tuple[bool, bool, bool]:
        cursor = self.text_widget.index("insert")
        if self.text_widget.compare(cursor, ">", "1.0"):
            previous = self.text_widget.index(f"{cursor}-1c")
            if self.text_widget.get(previous, cursor) != "\n":
                return self._style_at(previous)

        following = self.text_widget.index(f"{cursor}+1c")
        if self.text_widget.get(cursor, following) != "\n":
            return self._style_at(cursor)

        candidate = self.text_widget.index(f"{cursor}-1c")
        while self.text_widget.compare(candidate, ">", "1.0"):
            previous = self.text_widget.index(f"{candidate}-1c")
            if self.text_widget.get(previous, candidate) != "\n":
                return self._style_at(previous)
            candidate = previous
        return (False, False, False)

    def _sync_active_style_from_cursor(self) -> None:
        self._active_style = self._style_at_cursor()

    def _selection_bounds(self) -> tuple[str, str] | None:
        ranges = self.text_widget.tag_ranges("sel")
        if len(ranges) < 2:
            return None
        return (
            self.text_widget.index(str(ranges[0])),
            self.text_widget.index(str(ranges[1])),
        )

    def _selected_text_indices(self) -> list[str] | None:
        bounds = self._selection_bounds()
        if bounds is None:
            return None
        start, end = bounds

        content_end = self.text_widget.index("end-1c")
        if self.text_widget.compare(end, ">", content_end):
            end = content_end

        indices: list[str] = []
        current = start
        while self.text_widget.compare(current, "<", end):
            next_index = self.text_widget.index(f"{current}+1c")
            character = self.text_widget.get(current, next_index)
            if character and character != "\n":
                indices.append(current)
            current = next_index
        return indices

    def _on_format_shortcut(
        self,
        attribute: str,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> str:
        self._toggle_style(attribute)
        return "break"

    def _toggle_style(self, attribute: str) -> None:
        if attribute not in _STYLE_ATTRIBUTE_INDEX:
            raise ValueError(f"Atribut B/I/U necunoscut: {attribute!r}.")

        indices = self._selected_text_indices()
        position = _STYLE_ATTRIBUTE_INDEX[attribute]
        if indices:
            pre_state = self._capture_undo_state()
            currently_enabled = all(self._style_at(index)[position] for index in indices)
            replacement_value = not currently_enabled
            self._suspend_dirty += 1
            try:
                for index in indices:
                    style = list(self._style_at(index))
                    style[position] = replacement_value
                    self._replace_style_at(index, tuple(style))
            finally:
                self._suspend_dirty -= 1
            self._pending_style_cursor = None
            self._sync_active_style_from_cursor()
            self._record_undo_mutation(
                pre_state=pre_state,
                force_separator=True,
                command_name="format",
            )
        else:
            style = list(self._active_style)
            style[position] = not style[position]
            self._active_style = tuple(style)
            self._pending_style_cursor = self.text_widget.index("insert")
            self._mark_undo_separator()

        self._update_toolbar_state()
        self._refresh_dirty()
        self.text_widget.focus_set()

    def _replace_style_at(
        self,
        index: str,
        style: tuple[bool, bool, bool],
    ) -> None:
        end = self.text_widget.index(f"{index}+1c")
        for tag in _STYLE_TAGS.values():
            self.text_widget.tag_remove(tag, index, end)
        self.text_widget.tag_add(self._style_tag(style), index, end)

    def _update_toolbar_state(self) -> None:
        indices = self._selected_text_indices()
        if indices:
            style_values = tuple(
                all(self._style_at(index)[position] for index in indices)
                for position in range(3)
            )
        else:
            style_values = self._active_style
        for name, position in _STYLE_ATTRIBUTE_INDEX.items():
            self._style_variables[name].set(style_values[position])

    def _insert_paragraph_separator(self, _event: tk.Event[tk.Misc]) -> str:
        self._insert_structural_newline(PARAGRAPH_SEPARATOR_TAG)
        return "break"

    def _insert_line_break(self, _event: tk.Event[tk.Misc]) -> str:
        self._insert_structural_newline(LINE_BREAK_TAG)
        return "break"

    def _insert_structural_newline(self, tag: str) -> None:
        pre_state = self._capture_undo_state()
        self._suspend_dirty += 1
        try:
            bounds = self._selection_bounds()
            if bounds is not None:
                self.text_widget.delete(bounds[0], bounds[1])
            self.text_widget.insert("insert", "\n", (tag,))
        finally:
            self._suspend_dirty -= 1
        if self._pending_style_cursor is not None:
            self._pending_style_cursor = self.text_widget.index("insert")
        self._record_undo_mutation(
            pre_state=pre_state,
            force_separator=True,
            command_name="insert",
        )
        self._refresh_dirty()
        self._update_toolbar_state()

    def _on_cursor_or_selection_changed(self, _event: tk.Event[tk.Misc]) -> None:
        keysym = getattr(_event, "keysym", "")
        event_num = getattr(_event, "num", None)
        event_type = str(getattr(_event, "type", ""))
        is_button_release = (
            isinstance(event_num, int) and event_num in {1, 2, 3}
        ) or event_type in {"5", "ButtonRelease", "EventType.ButtonRelease"}
        if is_button_release or keysym in _NAVIGATION_AND_DELETE_KEYSYMS:
            self._pending_style_cursor = None
            self._mark_undo_separator()

        if self._selected_text_indices() is None:
            cursor = self.text_widget.index("insert")
            keep_pending_style = (
                self._pending_style_cursor is not None
                and self.text_widget.compare(cursor, "==", self._pending_style_cursor)
            )
            if not keep_pending_style and keysym not in {"Return", "KP_Enter"}:
                self._pending_style_cursor = None
                self._sync_active_style_from_cursor()
        else:
            self._pending_style_cursor = None
        self._update_toolbar_state()

    def _capture_undo_state(self) -> _EditorUndoState | None:
        if self._destroyed:
            return None
        try:
            return _EditorUndoState(
                document=_copy_document(self.get_document()),
                insert_index=self.text_widget.index("insert"),
                selection=self._selection_bounds(),
                active_style=self._active_style,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError, tk.TclError):
            return None

    def _reset_undo_history(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._current_undo_state = self._capture_undo_state()
        self._undo_separator_pending = True
        self._last_undo_command = None

    def _mark_undo_separator(self) -> None:
        self._undo_separator_pending = True
        if self._current_undo_state is None:
            self._current_undo_state = self._capture_undo_state()
            return
        try:
            insert_index = self.text_widget.index("insert")
            selection = self._selection_bounds()
        except tk.TclError:
            return
        self._current_undo_state = _EditorUndoState(
            document=self._current_undo_state.document,
            insert_index=insert_index,
            selection=selection,
            active_style=self._current_undo_state.active_style,
        )

    def _record_undo_mutation(
        self,
        *,
        pre_state: _EditorUndoState | None = None,
        force_separator: bool = False,
        command_name: str | None = None,
    ) -> None:
        new_state = self._capture_undo_state()
        if new_state is None:
            return
        base_state = pre_state if pre_state is not None else self._current_undo_state
        if base_state is None:
            self._current_undo_state = new_state
            return
        if new_state.document.to_json() == base_state.document.to_json():
            self._current_undo_state = new_state
            return

        command_switched = (
            command_name is not None
            and self._last_undo_command is not None
            and command_name != self._last_undo_command
        )
        should_push = (
            force_separator
            or self._undo_separator_pending
            or not self._undo_stack
            or command_switched
            or command_name == "replace"
        )
        if should_push:
            self._undo_stack.append(base_state)
        self._redo_stack.clear()
        self._current_undo_state = new_state
        self._last_undo_command = command_name
        self._undo_separator_pending = force_separator

    def _perform_undo_or_redo(self, subcommand: str) -> bool:
        if self._destroyed:
            return False
        if subcommand == "undo":
            source_stack = self._undo_stack
            target_stack = self._redo_stack
        elif subcommand == "redo":
            source_stack = self._redo_stack
            target_stack = self._undo_stack
        else:
            return False

        if not source_stack:
            return False

        current_state = self._capture_undo_state()
        if current_state is None:
            return False

        target_state = source_stack.pop()
        target_stack.append(current_state)

        self._suspend_dirty += 1
        try:
            self._render_state = render_rich_document(
                self.text_widget,
                target_state.document,
            )
            self.text_widget.tag_remove("sel", "1.0", "end")
            if target_state.selection is not None:
                self.text_widget.tag_add(
                    "sel",
                    target_state.selection[0],
                    target_state.selection[1],
                )
            self.text_widget.mark_set("insert", target_state.insert_index)
            self.text_widget.see("insert")
        finally:
            self._suspend_dirty -= 1

        self._current_undo_state = target_state
        self._undo_separator_pending = True
        self._last_undo_command = None
        self._pending_style_cursor = None
        self._sync_active_style_from_cursor()
        self._update_toolbar_state()
        self._refresh_dirty()
        return True

    def _refresh_dirty(self) -> None:
        if self._suspend_dirty or self._destroyed or self._saved_snapshot is None:
            return
        try:
            current = self.get_document()
            dirty = current.to_json() != self._saved_snapshot.to_json()
        except (AttributeError, RuntimeError, TypeError, ValueError, tk.TclError):
            # Conținutul incompatibil rămâne vizibil pentru corectare, dar nu
            # poate fi raportat drept nemodificat sau salvat accidental.
            dirty = True
        self._set_dirty(dirty)

    def _set_dirty(self, dirty: bool) -> None:
        if self._dirty == dirty:
            return
        self._dirty = dirty
        if dirty:
            self.dirty_indicator.grid()
        else:
            self.dirty_indicator.grid_remove()
        if self._on_dirty_changed is not None:
            self._on_dirty_changed(dirty)
