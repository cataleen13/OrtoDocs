"""Listă locală a sesiunilor complete din istoricul generărilor."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections.abc import Callable
from datetime import datetime, timezone
from tkinter import ttk

from ortodocs.models.generation import (
    GeneratedSession,
    GenerationHistoryItem,
    GenerationSessionDetail,
)
from ortodocs.storage.generation_repositories import (
    GenerationRepositoryError,
    get_generation_session,
    list_generation_sessions,
)
from ortodocs.ui.theme import CardFrame, SPACE_MD


def format_generation_timestamp(value: str) -> str:
    """Formatează timestamp-ul istoric UTC, păstrând o rezervă pentru date invalide."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return str(value)

    formatted = parsed.strftime("%d.%m.%Y %H:%M")
    if parsed.tzinfo == timezone.utc:
        return f"{formatted} UTC"
    if parsed.tzinfo is None:
        return formatted
    return f"{formatted} {parsed.strftime('%z')}"


class HistoryView(ttk.Frame):
    """Afișează maximum zece sesiuni fără să încarce documentele lor."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
        open_session: Callable[[GeneratedSession], bool] | None = None,
    ) -> None:
        super().__init__(parent, style="App.TFrame")
        self.connection = connection
        if open_session is not None and not callable(open_session):
            raise TypeError("open_session trebuie s\u0103 fie apelabil sau None.")
        self.open_session = open_session
        self.history_items: tuple[GenerationHistoryItem, ...] = ()
        self.search_var = tk.StringVar(master=self, value="")

        self._build_content()
        self.refresh()

    def _build_content(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar_card = CardFrame(self, padding=(18, 14))
        toolbar_card.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_MD))
        toolbar = toolbar_card.body
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(
            toolbar,
            text="Caută",
            style="CardFieldLabel.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        self.search_var.trace_add("write", self._on_search_changed)

        actions = ttk.Frame(toolbar, style="Card.TFrame")
        actions.grid(row=0, column=2, sticky="e")

        self.open_button = ttk.Button(
            actions,
            text="Deschide documentele",
            style="Primary.TButton",
            command=self.open_selected_session,
            state="disabled",
        )
        self.open_button.pack(side="left", padx=(0, 10))

        self.refresh_button = ttk.Button(
            actions,
            text="Reîmprospătează",
            style="Secondary.TButton",
            command=self.refresh,
        )
        self.refresh_button.pack(side="left")

        list_card = CardFrame(self, padding=(18, 16))
        list_card.grid(row=1, column=0, sticky="nsew")
        list_card.body.rowconfigure(1, weight=1)
        list_card.body.columnconfigure(0, weight=1)

        header_row = ttk.Frame(list_card.body, style="Card.TFrame")
        header_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(
            header_row,
            text="Sesiuni de generare",
            style="CardTitle.TLabel",
        ).pack(side="left")
        ttk.Label(
            header_row,
            text="Ultimele 10 sesiuni complete (dublu-clic sau «Deschide documentele»)",
            style="CardMuted.TLabel",
        ).pack(side="right")

        self.history_tree = ttk.Treeview(
            list_card.body,
            columns=("created_at", "diagnosis", "intervention", "documents"),
            show="headings",
            selectmode="browse",
        )
        self.history_tree.heading("created_at", text="Data și ora")
        self.history_tree.heading("diagnosis", text="Diagnostic")
        self.history_tree.heading("intervention", text="Intervenție")
        self.history_tree.heading("documents", text="Documente disponibile")
        self.history_tree.column("created_at", width=225, minwidth=205, anchor="w", stretch=False)
        self.history_tree.column("diagnosis", width=230, minwidth=160, anchor="w")
        self.history_tree.column("intervention", width=240, minwidth=170, anchor="w")
        self.history_tree.column("documents", width=245, minwidth=225, anchor="w", stretch=False)

        scrollbar = ttk.Scrollbar(
            list_card.body,
            orient="vertical",
            command=self.history_tree.yview,
        )
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.bind("<<TreeviewSelect>>", self._on_selection_changed)
        self.history_tree.bind("<Double-1>", self._on_double_click)
        self.history_tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        self.empty_state_label = ttk.Label(
            list_card.body,
            text="Nu există documente generate încă. După generare, sesiunile vor apărea aici.",
            style="CardMuted.TLabel",
            justify="center",
        )
        self.empty_state_label.grid(row=1, column=0, sticky="")
        self.empty_state_label.grid_remove()

        self.status_label = ttk.Label(
            self,
            text="",
            style="Muted.TLabel",
            wraplength=900,
            justify="left",
        )
        self.status_label.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def on_show(self) -> None:
        """Reîncarcă metadatele când utilizatorul revine în secțiunea Istoric."""

        self.refresh()

    def _on_search_changed(self, *_args: object) -> None:
        self._render_history_rows(preserve_selection=True)

    def _render_history_rows(self, *, preserve_selection: bool) -> None:
        selection = self.history_tree.selection()
        preferred_session_id = selection[0] if (preserve_selection and selection) else None
        query = self.search_var.get().strip().casefold()

        for item_id in self.history_tree.get_children():
            self.history_tree.delete(item_id)

        visible_items: list[GenerationHistoryItem] = []
        for item in self.history_items:
            formatted_ts = format_generation_timestamp(item.created_at)
            searchable = (
                f"{formatted_ts} {item.diagnosis_label_snapshot} {item.intervention_label_snapshot}"
            ).casefold()
            if query and query not in searchable:
                continue
            visible_items.append(item)
            session_id = str(item.session_id)
            self.history_tree.insert(
                "",
                "end",
                iid=session_id,
                values=(
                    formatted_ts,
                    item.diagnosis_label_snapshot,
                    item.intervention_label_snapshot,
                    "Epicriză + Protocol",
                ),
            )

        if preferred_session_id is not None and self.history_tree.exists(preferred_session_id):
            self.history_tree.selection_set(preferred_session_id)
            self.history_tree.focus(preferred_session_id)

        if not self.history_items:
            self.empty_state_label.configure(
                text="Nu există documente generate încă. După generare, sesiunile vor apărea aici."
            )
            self.empty_state_label.grid()
            self.status_label.configure(text="Nu există sesiuni complete în istoric.")
        elif not visible_items:
            self.empty_state_label.configure(
                text="Nicio sesiune nu corespunde filtrului introdus."
            )
            self.empty_state_label.grid()
            self.status_label.configure(
                text=f"Sunt afișate 0 din {len(self.history_items)} sesiuni complete."
            )
        else:
            self.empty_state_label.grid_remove()
            if query:
                self.status_label.configure(
                    text=f"Sunt afișate {len(visible_items)} din {len(self.history_items)} sesiuni complete."
                )
            else:
                self.status_label.configure(
                    text=f"Sunt afișate {len(self.history_items)} sesiuni complete."
                )
        self._update_open_button_state()

    def refresh(self) -> bool:
        """Reîncarcă lista; la o eroare păstrează rândurile deja afișate."""

        selection = self.history_tree.selection()
        preferred_session_id = selection[0] if selection else None
        try:
            items = list_generation_sessions(self.connection, limit=10)
        except (GenerationRepositoryError, sqlite3.Error) as exc:
            self.status_label.configure(
                text=f"Istoricul nu a putut fi încărcat. Datele afișate au fost păstrate. Detalii: {exc}"
            )
            return False

        self.history_items = tuple(items)
        self._render_history_rows(preserve_selection=True)
        if preferred_session_id is not None and self.history_tree.exists(preferred_session_id):
            self.history_tree.selection_set(preferred_session_id)
            self.history_tree.focus(preferred_session_id)
            self._update_open_button_state()
        return True

    def _on_selection_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._update_open_button_state()

    def _update_open_button_state(self) -> None:
        state = "normal" if self.history_tree.selection() else "disabled"
        self.open_button.configure(state=state)

    def _on_double_click(self, event: tk.Event[tk.Misc]) -> str:
        item_id = self.history_tree.identify_row(event.y)
        if item_id:
            if self.history_tree.selection() != (item_id,):
                self.history_tree.selection_set(item_id)
                self.history_tree.focus(item_id)
            self.open_selected_session()
        return "break"

    def open_selected_session(self) -> bool:
        """Deschide sesiunea selectată în editorul comun."""

        selection = self.history_tree.selection()
        if not selection:
            self._update_open_button_state()
            return False

        try:
            session_id = int(selection[0])
            detail = get_generation_session(self.connection, session_id)
        except (GenerationRepositoryError, sqlite3.Error, TypeError, ValueError) as exc:
            self.status_label.configure(
                text=(
                    f"Sesiunea istorică nu a putut fi încărcată. "
                    f"Editorul existent a fost păstrat. Detalii: {exc}"
                )
            )
            return False

        if detail is None:
            refreshed = self.refresh()
            message = f"Sesiunea {selection[0]} nu mai este disponibilă sau nu este completă."
            if refreshed:
                self.status_label.configure(
                    text=f"{message} Lista istoricului a fost actualizată."
                )
            else:
                self.status_label.configure(
                    text=(
                        f"{message} Reîncărcarea listei a eșuat; "
                        "rândurile existente au fost păstrate."
                    )
                )
            return False

        try:
            session = self._to_generated_session(detail)
        except (TypeError, ValueError) as exc:
            self.status_label.configure(
                text=(
                    f"Sesiunea {selection[0]} conține date invalide și nu poate fi "
                    f"deschisă. Detalii: {exc}"
                )
            )
            return False

        if self.open_session is None:
            self.status_label.configure(
                text="Editorul de documente nu este disponibil în această pagină."
            )
            return False

        try:
            opened = self.open_session(session)
        except Exception as exc:
            self.status_label.configure(
                text=(
                    f"Sesiunea {session.session_id} a fost încărcată, dar editorul "
                    f"nu a putut fi deschis. Editorul existent a fost păstrat. Detalii: {exc}"
                )
            )
            return False

        if opened:
            self.status_label.configure(
                text=f"Sesiunea {session.session_id} a fost deschisă în editor."
            )
        return opened

    @staticmethod
    def _to_generated_session(detail: GenerationSessionDetail) -> GeneratedSession:
        if not isinstance(detail, GenerationSessionDetail):
            raise TypeError("Detaliile sesiunii au un tip neașteptat.")
        return GeneratedSession(
            session_id=detail.history_item.session_id,
            epicrisis_recommendations=detail.epicrisis_recommendations,
            operative_protocol=detail.operative_protocol,
        )
