"""Fereastra de editare pentru perechile de documente generate."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from ortodocs.models.generation import GeneratedSession
from ortodocs.models.rich_document import RichDocument
from ortodocs.services.template_sets import _now_utc as _utc_timestamp
from ortodocs.storage.generation_repositories import update_generated_document
from ortodocs.ui.rich_text_editor import RichTextEditor
from ortodocs.ui.theme import CardFrame, apply_soft_medical_theme, style_toplevel
from ortodocs.ui.windows_clipboard import (
    WindowsClipboardError,
    copy_rich_document_to_clipboard,
)


DOCUMENT_TYPES = (
    "epicrisis_recommendations",
    "operative_protocol",
)
DOCUMENT_LABELS = {
    "epicrisis_recommendations": "Epicriz\u0103 + Recomand\u0103ri",
    "operative_protocol": "Protocol operator",
}


class GeneratedDocumentsWindow:
    """De\u021bine dou\u0103 editoare independente pentru aceea\u0219i sesiune."""

    def __init__(
        self,
        parent: tk.Misc,
        connection: sqlite3.Connection,
        session: GeneratedSession,
        *,
        on_closed: Callable[[GeneratedDocumentsWindow], None] | None = None,
        initially_hidden: bool = False,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection trebuie s\u0103 fie o conexiune SQLite.")
        if not isinstance(session, GeneratedSession):
            raise TypeError("session trebuie s\u0103 fie GeneratedSession.")
        if on_closed is not None and not callable(on_closed):
            raise TypeError("on_closed trebuie s\u0103 fie apelabil sau None.")

        self.connection = connection
        self.session_id = session.session_id
        self._on_closed = on_closed
        self._closed = False
        self.editors: dict[str, RichTextEditor] = {}
        self._document_types = DOCUMENT_TYPES

        self.window = tk.Toplevel(parent)
        try:
            if initially_hidden:
                self.window.withdraw()
            self._build_window(session)
        except BaseException:
            self._closed = True
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            raise

    def _build_window(self, session: GeneratedSession) -> None:
        self.window.title(f"Documente generate — sesiunea {session.session_id}")
        self.window.geometry("1120x780")
        self.window.minsize(850, 560)
        style_toplevel(self.window)
        style = ttk.Style(self.window)
        if not style.lookup("CardTitle.TLabel", "font"):
            apply_soft_medical_theme(self.window)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Control-s>", self._on_save_shortcut)
        self.window.bind("<Control-S>", self._on_save_shortcut)

        content = ttk.Frame(self.window, style="App.TFrame", padding=(18, 16))
        content.pack(fill="both", expand=True)
        content.rowconfigure(1, weight=1)
        content.columnconfigure(0, weight=1)

        toolbar_card = CardFrame(content, padding=(14, 10))
        toolbar_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        toolbar = toolbar_card.body
        toolbar.columnconfigure(2, weight=1)
        self.save_button = ttk.Button(
            toolbar,
            text="Salveaz\u0103",
            style="Primary.TButton",
            command=self.save_dirty_documents,
        )
        self.save_button.grid(row=0, column=0, padx=(0, 8))
        self.copy_button = ttk.Button(
            toolbar,
            text="Copiaz\u0103",
            style="Secondary.TButton",
            command=self.copy_active_document,
        )
        self.copy_button.grid(row=0, column=1)
        self.dirty_var = tk.StringVar(master=self.window, value="")
        ttk.Label(
            toolbar,
            textvariable=self.dirty_var,
            style="CardMuted.TLabel",
        ).grid(row=0, column=3, sticky="e")

        self.notebook = ttk.Notebook(content)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        documents = {
            "epicrisis_recommendations": session.epicrisis_recommendations,
            "operative_protocol": session.operative_protocol,
        }
        for document_type in self._document_types:
            tab = ttk.Frame(self.notebook, style="Card.TFrame", padding=12)
            self.notebook.add(tab, text=DOCUMENT_LABELS[document_type])
            editor = RichTextEditor(
                tab,
                documents[document_type],
                on_dirty_changed=lambda _dirty, key=document_type: self._on_editor_dirty(key),
            )
            editor.pack(fill="both", expand=True)
            self.editors[document_type] = editor

        self.status_var = tk.StringVar(master=self.window, value="")
        ttk.Label(
            content,
            textvariable=self.status_var,
            style="Muted.TLabel",
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._refresh_dirty_status()

    def show(self) -> None:
        """Afișează și aduce în față fereastra editorului, dacă este încă deschisă."""

        if self._closed:
            return
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    @property
    def dirty_document_types(self) -> tuple[str, ...]:
        return tuple(
            document_type
            for document_type in self._document_types
            if self.editors[document_type].is_dirty
        )

    @property
    def has_dirty_documents(self) -> bool:
        return bool(self.dirty_document_types)

    @property
    def active_document_type(self) -> str:
        index = self.notebook.index(self.notebook.select())
        return self._document_types[index]

    def ask_replacement_action(self) -> str:
        """Întreabă cum se rezolvă editările înaintea unei înlocuiri."""

        if not self.has_dirty_documents:
            return "replace"
        answer = messagebox.askyesnocancel(
            "Modific\u0103ri nesalvate",
            "Da: salveaz\u0103 documentele modificate. Nu: renun\u021b\u0103 la ele numai "
            "dac\u0103 noua sesiune se salveaz\u0103. Anulare: p\u0103streaz\u0103 fereastra.",
            parent=self.window,
        )
        if answer is True:
            return "save"
        if answer is False:
            return "discard"
        return "cancel"

    def save_dirty_documents(self) -> bool:
        """Salvează independent fiecare editor dirty, marcându-l clean doar după succes."""

        saved_any = False
        for document_type in self._document_types:
            editor = self.editors[document_type]
            if not editor.is_dirty:
                continue
            try:
                document = editor.get_document()
                update_generated_document(
                    self.connection,
                    self.session_id,
                    document_type,
                    document,
                    _utc_timestamp(),
                )
                editor.mark_saved()
                saved_any = True
            except Exception as exc:
                label = DOCUMENT_LABELS[document_type]
                self.status_var.set(f"{label}: salvarea a e\u0219uat. Modific\u0103rile au fost p\u0103strate.")
                messagebox.showerror(
                    "Salvare e\u0219uat\u0103",
                    f"Documentul {label} nu a putut fi salvat:\n{exc}",
                    parent=self.window,
                )
                self._refresh_dirty_status()
                return False

        if saved_any:
            self.status_var.set("Modific\u0103rile documentelor au fost salvate.")
        else:
            self.status_var.set("Nu exist\u0103 modific\u0103ri nesalvate.")
        self._refresh_dirty_status()
        return True

    def copy_active_document(self) -> bool:
        """Copiază documentul tabului curent în clipboard ca RTF și Unicode."""

        document_type = self.active_document_type
        try:
            document = self.editors[document_type].get_document()
            copy_rich_document_to_clipboard(document, int(self.window.winfo_id()))
        except WindowsClipboardError as exc:
            self.status_var.set(f"Copierea a e\u0219uat: {exc}")
            messagebox.showerror("Copiere e\u0219uat\u0103", str(exc), parent=self.window)
            return False
        except Exception as exc:
            self.status_var.set(f"Documentul nu a putut fi copiat: {exc}")
            messagebox.showerror("Copiere e\u0219uat\u0103", str(exc), parent=self.window)
            return False

        self.status_var.set(f"{DOCUMENT_LABELS[document_type]} a fost copiat.")
        return True

    def close(self) -> bool:
        """Aplică Save/Discard/Cancel pentru închiderea normală a ferestrei."""

        if self._closed:
            return True
        if self.has_dirty_documents:
            action = self._ask_close_action()
            if action == "cancel":
                return False
            if action == "save" and not self.save_dirty_documents():
                return False
        self._destroy_window()
        return True

    def close_after_replacement(self) -> None:
        """Închide fără dialog după ce sesiunea înlocuitoare a fost salvată."""

        self._destroy_window()

    def _ask_close_action(self) -> str:
        answer = messagebox.askyesnocancel(
            "Modific\u0103ri nesalvate",
            "Da: salveaz\u0103. Nu: renun\u021b\u0103 la modific\u0103ri. Anulare: "
            "p\u0103streaz\u0103 fereastra deschis\u0103.",
            parent=self.window,
        )
        if answer is True:
            return "save"
        if answer is False:
            return "discard"
        return "cancel"

    def _on_save_shortcut(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        self.save_dirty_documents()
        return "break"

    def _on_tab_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        # Tab changes only select which document Copy targets; editors stay mounted.
        return None

    def _on_editor_dirty(self, _document_type: str) -> None:
        self._refresh_dirty_status()

    def _refresh_dirty_status(self) -> None:
        dirty = self.dirty_document_types
        if dirty:
            labels = ", ".join(DOCUMENT_LABELS[item] for item in dirty)
            self.dirty_var.set(f"Modific\u0103ri nesalvate: {labels}")
            self.save_button.configure(state="normal")
        else:
            self.dirty_var.set("Toate documentele sunt salvate.")
            self.save_button.configure(state="disabled")

    def _destroy_window(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        finally:
            if self._on_closed is not None:
                self._on_closed(self)


__all__ = ["DOCUMENT_LABELS", "DOCUMENT_TYPES", "GeneratedDocumentsWindow"]
