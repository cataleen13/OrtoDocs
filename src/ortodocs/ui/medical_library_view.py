"""Interfața locală pentru administrarea șabloanelor, diagnosticelor și intervențiilor."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from tkinter import messagebox, ttk
from typing import Any

from ortodocs.models.rich_document import RichDocument
from ortodocs.services import medical_library, template_sets
from ortodocs.storage import template_sync_repositories
from ortodocs.ui.rich_text_codec import render_rich_document
from ortodocs.ui.startup_sync import has_sync_schema
from ortodocs.ui.template_sets_view import (
    _DOCUMENT_LABELS,
    _SET_STATUS_LABELS,
    _format_independent_report,
    _rich_document_has_content,
    _set_readonly_text,
)
from ortodocs.ui.theme import (
    CardFrame,
    CollapsibleSection,
    SPACE_MD,
    apply_badge_style,
    apply_soft_medical_theme,
    style_text_widget,
    style_toplevel,
)

FIELD_TYPES = ("text", "number", "date", "select")
FIELD_SCOPES = ("common", "diagnosis", "intervention")
_SCOPE_LABELS = {
    "common": "Comun",
    "diagnosis": "Diagnostic",
    "intervention": "Intervenție",
}


def _record_tree_values(record: Mapping[str, Any]) -> tuple[str, str, str]:
    """Formatează un diagnostic sau o intervenție pentru lista vizuală."""

    code = record.get("code")
    return (
        str(record["name"]),
        code if isinstance(code, str) else "",
        "Activ" if bool(record["is_active"]) else "Inactiv",
    )


def _active_marker(record: Mapping[str, Any]) -> str:
    return "" if bool(record["is_active"]) else " (inactiv)"


def _context_choices(
    scope: str,
    diagnoses: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
) -> list[tuple[int, str]]:
    if scope == "diagnosis":
        return [
            (
                int(item["id"]),
                f"{item['name']}{_active_marker(item)} · ID {item['id']}",
            )
            for item in diagnoses
        ]
    if scope == "intervention":
        diagnosis_by_id = {int(item["id"]): item for item in diagnoses}
        choices: list[tuple[int, str]] = []
        for item in interventions:
            diagnosis = diagnosis_by_id.get(int(item["diagnosis_id"]))
            diagnosis_name = (
                str(diagnosis["name"]) + _active_marker(diagnosis)
                if diagnosis is not None
                else f"Diagnostic ID {item['diagnosis_id']}"
            )
            choices.append(
                (
                    int(item["id"]),
                    f"{diagnosis_name} — {item['name']}{_active_marker(item)} · ID {item['id']}",
                )
            )
        return choices
    return []


def _field_context_text(
    field: Mapping[str, Any],
    diagnoses: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
) -> str:
    if field["scope"] == "common":
        return "Comun"
    choices = _context_choices(str(field["scope"]), diagnoses, interventions)
    target_id = field.get("diagnosis_id")
    if field["scope"] == "intervention":
        target_id = field.get("intervention_id")
    for record_id, label in choices:
        if record_id == target_id:
            return label
    return f"Context indisponibil (ID {target_id})"


class _FieldDefinitionDialog(tk.Toplevel):
    """Formular pentru o definiție nouă sau o versiune nouă a unui câmp."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        diagnoses: list[dict[str, Any]],
        interventions: list[dict[str, Any]],
        initial: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Creează versiune de câmp" if initial else "Adaugă câmp medical")
        style_toplevel(self)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.diagnoses = diagnoses
        self.interventions = interventions
        self.initial = initial
        self.result: dict[str, Any] | None = None

        content = ttk.Frame(self, style="App.TFrame", padding=18)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(content, text="Cheia variabilei").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 8)
        )
        self.key_var = tk.StringVar(
            master=self, value=str(initial["variable_key"]) if initial else ""
        )
        self.key_entry = ttk.Entry(content, textvariable=self.key_var, width=46)
        if initial:
            self.key_entry.configure(state="readonly")
        self.key_entry.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(content, text="Etichetă").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 8)
        )
        self.label_var = tk.StringVar(
            master=self, value=str(initial["label"]) if initial else ""
        )
        ttk.Entry(content, textvariable=self.label_var, width=46).grid(
            row=row, column=1, sticky="ew", pady=(0, 8)
        )
        row += 1

        ttk.Label(content, text="Tip").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 8)
        )
        self.type_var = tk.StringVar(
            master=self, value=str(initial["data_type"]) if initial else ""
        )
        type_combo = ttk.Combobox(
            content, textvariable=self.type_var, values=FIELD_TYPES, state="readonly"
        )
        type_combo.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(content, text="Scope").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 8)
        )
        self.scope_var = tk.StringVar(
            master=self, value=str(initial["scope"]) if initial else ""
        )
        self.scope_combo = ttk.Combobox(
            content,
            textvariable=self.scope_var,
            values=FIELD_SCOPES,
            state="readonly",
        )
        self.scope_combo.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(content, text="Asociere").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 8)
        )
        self.context_var = tk.StringVar(master=self, value="")
        self.context_combo = ttk.Combobox(
            content, textvariable=self.context_var, state="disabled"
        )
        self.context_combo.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        self.scope_combo.bind("<<ComboboxSelected>>", self._on_scope_selected)
        self._update_context_choices(
            preferred_id=(
                int(initial["diagnosis_id"])
                if initial and initial["scope"] == "diagnosis"
                else int(initial["intervention_id"])
                if initial and initial["scope"] == "intervention"
                else None
            )
        )
        row += 1

        buttons = ttk.Frame(content, style="App.TFrame")
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(
            buttons, text="Anulează", style="Secondary.TButton", command=self.destroy
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons, text="Salvează", style="Primary.TButton", command=self._accept
        ).pack(side="right")
        self.bind("<Return>", lambda _event: self._accept())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        if initial:
            self.label_var.set(str(initial["label"]))
        else:
            self.key_entry.focus_set()

    def _on_scope_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._update_context_choices()

    def _update_context_choices(self, *, preferred_id: int | None = None) -> None:
        scope = self.scope_var.get()
        choices = _context_choices(scope, self.diagnoses, self.interventions)
        self.context_ids = [record_id for record_id, _label in choices]
        labels = [label for _record_id, label in choices]
        if not choices:
            self.context_combo.configure(values=(), state="disabled")
            self.context_var.set("")
            return
        self.context_combo.configure(values=tuple(labels), state="readonly")
        if preferred_id in self.context_ids:
            index = self.context_ids.index(preferred_id)
            self.context_combo.current(index)
        else:
            self.context_combo.set("")

    def _accept(self) -> None:
        scope = self.scope_var.get()
        context_id: int | None = None
        index = self.context_combo.current()
        if 0 <= index < len(self.context_ids):
            context_id = self.context_ids[index]
        self.result = {
            "variable_key": self.key_var.get(),
            "label": self.label_var.get(),
            "data_type": self.type_var.get(),
            "scope": scope,
            "diagnosis_id": context_id if scope == "diagnosis" else None,
            "intervention_id": context_id if scope == "intervention" else None,
        }
        self.destroy()


def _prompt_field_definition(
    parent: tk.Misc,
    *,
    diagnoses: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
    initial: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    dialog = _FieldDefinitionDialog(
        parent,
        diagnoses=diagnoses,
        interventions=interventions,
        initial=initial,
    )
    parent.wait_window(dialog)
    return dialog.result


def _prompt_field_option(parent: tk.Misc) -> tuple[str, str] | None:
    dialog = tk.Toplevel(parent)
    dialog.title("Adaugă opțiune")
    style_toplevel(dialog)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)
    result: dict[str, tuple[str, str] | None] = {"value": None}
    content = ttk.Frame(dialog, style="App.TFrame", padding=18)
    content.grid(row=0, column=0, sticky="nsew")
    content.columnconfigure(1, weight=1)
    value_var = tk.StringVar(master=dialog, value="")
    label_var = tk.StringVar(master=dialog, value="")
    ttk.Label(content, text="Valoare").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
    value_entry = ttk.Entry(content, textvariable=value_var, width=42)
    value_entry.grid(row=0, column=1, sticky="ew", pady=6)
    ttk.Label(content, text="Etichetă afișată").grid(
        row=1, column=0, sticky="w", padx=(0, 12), pady=6
    )
    ttk.Entry(content, textvariable=label_var, width=42).grid(
        row=1, column=1, sticky="ew", pady=6
    )

    def accept() -> None:
        result["value"] = (value_var.get(), label_var.get())
        dialog.destroy()

    buttons = ttk.Frame(content, style="App.TFrame")
    buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
    ttk.Button(
        buttons, text="Anulează", style="Secondary.TButton", command=dialog.destroy
    ).pack(side="right", padx=(8, 0))
    ttk.Button(
        buttons, text="Adaugă", style="Primary.TButton", command=accept
    ).pack(side="right")
    dialog.bind("<Return>", lambda _event: accept())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.grab_set()
    value_entry.focus_set()
    parent.wait_window(dialog)
    return result["value"]


class _RecordDialog(tk.Toplevel):
    """Dialog simplu pentru denumire, cod și relația informativă a intervenției."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        name: str = "",
        code: str | None = None,
        relationship: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        style_toplevel(self)
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.result: tuple[str, str | None] | None = None

        content = ttk.Frame(self, style="App.TFrame", padding=20)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(1, weight=1)

        row = 0
        if relationship is not None:
            ttk.Label(content, text="Diagnostic asociat").grid(
                row=row, column=0, sticky="nw", padx=(0, 12), pady=(0, 10)
            )
            ttk.Label(content, text=relationship, wraplength=360).grid(
                row=row, column=1, sticky="w", pady=(0, 10)
            )
            row += 1

        ttk.Label(content, text="Denumire").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 9)
        )
        self.name_var = tk.StringVar(master=self, value=name)
        self.name_entry = ttk.Entry(content, textvariable=self.name_var, width=48)
        self.name_entry.grid(row=row, column=1, sticky="ew", pady=(0, 9))
        row += 1

        ttk.Label(content, text="Cod (opțional)").grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 14)
        )
        self.code_var = tk.StringVar(master=self, value=code or "")
        ttk.Entry(content, textvariable=self.code_var, width=48).grid(
            row=row, column=1, sticky="ew", pady=(0, 14)
        )
        row += 1

        buttons = ttk.Frame(content, style="App.TFrame")
        buttons.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(
            buttons, text="Anulează", style="Secondary.TButton", command=self.destroy
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons, text="Salvează", style="Primary.TButton", command=self._accept
        ).pack(side="right")

        self.bind("<Return>", lambda _event: self._accept())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.name_entry.focus_set()

    def _accept(self) -> None:
        name = self.name_var.get()
        code = self.code_var.get().strip() or None
        self.result = (name, code)
        self.destroy()


def _prompt_record(
    parent: tk.Misc,
    *,
    title: str,
    name: str = "",
    code: str | None = None,
    relationship: str | None = None,
) -> tuple[str, str | None] | None:
    dialog = _RecordDialog(
        parent,
        title=title,
        name=name,
        code=code,
        relationship=relationship,
    )
    parent.wait_window(dialog)
    return dialog.result


def _friendly_validation_badge(row: Mapping[str, Any] | None) -> tuple[str, str]:
    """Returnează eticheta prietenoasă și tipul semantic al validării DOCX."""

    if row is None:
        return "Fără șablon", "neutral"
    validation_status = str(row.get("validation_status") or "")
    parser_report = row.get("parser_report")
    parser_status = (
        parser_report.get("status") if isinstance(parser_report, Mapping) else None
    )
    warnings = (
        parser_report.get("warnings") if isinstance(parser_report, Mapping) else None
    )
    if validation_status == "rejected" or parser_status == "rejected":
        return "Document invalid", "error"
    if validation_status == "valid":
        if warnings:
            return "Document valid (avertismente)", "warning"
        return "Document valid", "success"
    return "În așteptare activare", "warning"


class _VersionsHistoryWindow(tk.Toplevel):
    """Fereastră secundară cu istoricul versiunilor DOCX și detaliile de validare."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        section: _IndependentDocumentSection,
    ) -> None:
        super().__init__(parent)
        self.section = section
        self.selected_version_id: int | None = section.selected_version_id
        style_toplevel(self)
        self.transient(parent.winfo_toplevel())
        self.geometry("920x600")
        self.minsize(760, 480)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        container = ttk.Frame(self, style="App.TFrame", padding=(20, 18))
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        header = ttk.Frame(container, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        self.header_title_var = tk.StringVar(master=self, value="")
        self.header_subtitle_var = tk.StringVar(master=self, value="")
        ttk.Label(
            header,
            textvariable=self.header_title_var,
            style="PageTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            textvariable=self.header_subtitle_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        table_card = CardFrame(container, padding=(16, 14))
        table_card.grid(row=1, column=0, sticky="nsew", pady=(0, SPACE_MD))
        table_card.body.columnconfigure(0, weight=1)
        table_card.body.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_card.body,
            columns=("version", "status", "source", "synced_at", "variables"),
            show="headings",
            selectmode="browse",
            height=6,
        )
        for column, heading, width in (
            ("version", "Ver.", 58),
            ("status", "Status", 86),
            ("source", "Sursă DOCX", 230),
            ("synced_at", "Sincronizat", 160),
            ("variables", "Variabile", 180),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(
                column,
                width=width,
                minwidth=48,
                anchor="w",
                stretch=column in {"source", "variables"},
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(
            table_card.body, orient="vertical", command=self.tree.yview
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_version_selected)

        details_card = CardFrame(container, padding=(16, 12))
        details_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        details_card.body.columnconfigure(0, weight=1)

        self.details_collapsible = CollapsibleSection(
            details_card.body,
            title="Detalii tehnice și raport validare (doar citire)",
            expanded=True,
            style="Card.TFrame",
        )
        self.details_collapsible.grid(row=0, column=0, sticky="ew")
        self.details_collapsible.content.columnconfigure(0, weight=1)

        self.report_text = tk.Text(
            self.details_collapsible.content,
            height=7,
            wrap="word",
            state="disabled",
        )
        style_text_widget(self.report_text, subtle=True)
        self.report_text.grid(row=0, column=0, sticky="ew")
        rep_scroll = ttk.Scrollbar(
            self.details_collapsible.content,
            orient="vertical",
            command=self.report_text.yview,
        )
        rep_scroll.grid(row=0, column=1, sticky="ns")
        self.report_text.configure(yscrollcommand=rep_scroll.set)

        actions = ttk.Frame(container, style="App.TFrame")
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        ttk.Label(
            actions,
            text="Gestionat prin Microsoft Word • actualizare prin Setări → Template-uri DOCX.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(actions, style="App.TFrame")
        buttons.grid(row=0, column=1, sticky="e")
        self.preview_button = ttk.Button(
            buttons,
            text="Previzualizează versiunea",
            style="Primary.TButton",
            command=self._preview_selected,
            state="disabled",
        )
        self.preview_button.pack(side="left", padx=(0, 8))
        ttk.Button(
            buttons,
            text="Închide",
            style="Secondary.TButton",
            command=self.destroy,
        ).pack(side="left")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.refresh()

    def refresh(self) -> None:
        doc_label = _DOCUMENT_LABELS[self.section.document_type]
        owner_suffix = f" — {self.section.owner_name}" if self.section.owner_name else ""
        self.title(f"Istoric versiuni — {doc_label}{owner_suffix}")
        self.header_title_var.set(f"Istoric versiuni: {doc_label}")
        self.header_subtitle_var.set(
            f"Asociat: {self.section.owner_name}"
            if self.section.owner_name
            else "Nicio entitate selectată"
        )

        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        rows = self.section.rows
        sync_sources = self.section.sync_sources_by_version
        for row in rows:
            version_id = int(row["id"])
            sync_source = sync_sources.get(version_id)
            source_cell = (
                str(sync_source["relative_path"])
                if sync_source is not None
                else "Istoric / import manual"
            )
            synced_at_cell = (
                str(sync_source["synced_at"])
                if sync_source is not None
                else str(row.get("created_at") or "—")
            )
            variables_cell = ", ".join(row.get("variables") or ()) or "—"
            self.tree.insert(
                "",
                "end",
                iid=str(version_id),
                values=(
                    f"v{row['version_no']}",
                    _SET_STATUS_LABELS.get(str(row["status"]), str(row["status"])),
                    source_cell,
                    synced_at_cell,
                    variables_cell,
                ),
            )

        target_id = self.selected_version_id or self.section.selected_version_id
        selected_row = next(
            (row for row in rows if target_id is not None and int(row["id"]) == target_id),
            None,
        )
        if selected_row is None and rows:
            selected_row = next(
                (row for row in rows if row.get("status") == "active"), rows[0]
            )
        if selected_row is not None:
            iid = str(selected_row["id"])
            self.selected_version_id = int(selected_row["id"])
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        else:
            self.selected_version_id = None
        self._render_selected()

    def _selected_row(self) -> dict[str, Any] | None:
        if self.selected_version_id is None:
            return None
        return next(
            (row for row in self.section.rows if int(row["id"]) == self.selected_version_id),
            None,
        )

    def _on_version_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selected_version_id = None
        else:
            try:
                self.selected_version_id = int(selection[0])
            except ValueError:
                self.selected_version_id = None
        self._render_selected()

    def _render_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            _set_readonly_text(self.report_text, _format_independent_report(None))
            self.preview_button.configure(state="disabled")
            return
        _set_readonly_text(self.report_text, self.section.format_version_details(row))
        parser_report = row.get("parser_report")
        parser_status = (
            parser_report.get("status") if isinstance(parser_report, Mapping) else None
        )
        self.preview_button.configure(
            state="normal" if parser_status != "rejected" else "disabled"
        )

    def _preview_selected(self) -> None:
        if self.selected_version_id is not None:
            self.section.preview_version(self.selected_version_id)


class _IndependentDocumentSection(CardFrame):
    """Card curat Soft Medical pentru prezentarea unui document DOCX activ."""

    _DOC_TYPES = {
        "diagnosis": template_sets.EPICRISIS_RECOMMENDATIONS,
        "intervention": template_sets.OPERATIVE_PROTOCOL,
    }
    _EMPTY_PROMPTS = {
        "diagnosis": "Selectați un diagnostic pentru a vedea șablonul Epicriză + Recomandări.",
        "intervention": "Selectați o intervenție pentru a vedea șablonul Protocol operator.",
    }

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
        owner_type: str,
        title: str,
        status_callback: Callable[[str, bool], None],
        preview_callback: Callable[[str, RichDocument], tk.Toplevel | None],
    ) -> None:
        super().__init__(parent, padding=(16, 12))
        if owner_type not in self._DOC_TYPES:
            raise ValueError(f"owner_type necunoscut: {owner_type!r}")
        self.connection = connection
        self.owner_type = owner_type
        self.document_type = self._DOC_TYPES[owner_type]
        self._title = title
        self._status_callback = status_callback
        self._preview_callback = preview_callback
        self.owner_id: int | None = None
        self.owner_name: str | None = None
        self.rows: list[dict[str, Any]] = []
        self.sync_sources_by_version: dict[int, dict[str, Any]] = {}
        self.selected_version_id: int | None = None
        self._history_window: _VersionsHistoryWindow | None = None

        self.summary_var = tk.StringVar(master=self, value=self._EMPTY_PROMPTS[owner_type])
        self.validation_var = tk.StringVar(master=self, value="Neselectat")
        self.version_var = tk.StringVar(master=self, value="—")
        self.source_var = tk.StringVar(master=self, value="—")
        self.synced_at_var = tk.StringVar(master=self, value="—")
        self.variables_var = tk.StringVar(master=self, value="—")
        self._build_widgets()

    def _build_widgets(self) -> None:
        body = self.body
        body.columnconfigure(1, weight=1)

        header = ttk.Frame(body, style="Card.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=self._title, style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.validation_badge = ttk.Label(
            header,
            textvariable=self.validation_var,
            style="NeutralBadge.TLabel",
        )
        self.validation_badge.grid(row=0, column=1, sticky="e")

        ttk.Label(
            header,
            text="Gestionat prin Microsoft Word",
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Label(
            body,
            textvariable=self.summary_var,
            style="CardMuted.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 8))

        meta_card = ttk.Frame(body, style="Subtle.TFrame", padding=(12, 8))
        meta_card.grid(row=2, column=0, columnspan=2, sticky="ew")
        meta_card.columnconfigure(1, weight=1)

        meta_rows = (
            ("Versiune activă", self.version_var),
            ("Fișier / sursă DOCX", self.source_var),
            ("Ultima sincronizare", self.synced_at_var),
            ("Câmpuri detectate", self.variables_var),
        )
        for index, (label, variable) in enumerate(meta_rows):
            ttk.Label(
                meta_card,
                text=label,
                style="SubtleFieldLabel.TLabel",
            ).grid(row=index, column=0, sticky="nw", padx=(0, 14), pady=2)
            ttk.Label(
                meta_card,
                textvariable=variable,
                style="Subtle.TLabel",
                wraplength=260,
                justify="left",
            ).grid(row=index, column=1, sticky="nw", pady=2)

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.preview_button = ttk.Button(
            actions,
            text="Previzualizează",
            style="Secondary.TButton",
            command=self.preview_version,
            state="disabled",
        )
        self.preview_button.pack(side="left", padx=(0, 8))

        self.history_button = ttk.Button(
            actions,
            text="Istoric versiuni",
            style="Subtle.TButton",
            command=self.open_versions_history,
            state="disabled",
        )
        self.history_button.pack(side="left")

    def set_owner(
        self,
        owner_id: int | None,
        *,
        owner_name: str | None = None,
        preferred_version_id: int | None = None,
    ) -> None:
        """Asociază cardul unui diagnostic/intervenție și reîncarcă starea."""

        owner_changed = owner_id != self.owner_id
        self.owner_id = owner_id
        self.owner_name = owner_name
        if owner_changed and preferred_version_id is None:
            self.selected_version_id = None
        self.refresh(preferred_version_id=preferred_version_id)

    def refresh(self, *, preferred_version_id: int | None = None) -> None:
        """Reîncarcă versiunile și actualizează sumarul curat al cardului."""

        self.rows = []
        self.sync_sources_by_version = {}
        target_id = (
            preferred_version_id
            if preferred_version_id is not None
            else self.selected_version_id
        )
        self.selected_version_id = None

        if self.owner_id is None:
            self.summary_var.set(self._EMPTY_PROMPTS[self.owner_type])
            self.validation_var.set("Neselectat")
            apply_badge_style(self.validation_badge, "neutral")
            self.version_var.set("—")
            self.source_var.set("—")
            self.synced_at_var.set("—")
            self.variables_var.set("—")
            self._update_actions()
            self._refresh_history_window()
            return

        try:
            rows = template_sets.list_independent_templates(
                self.connection,
                owner_type=self.owner_type,
                owner_id=self.owner_id,
            )
            version_ids = [int(row["id"]) for row in rows]
            sync_sources = (
                template_sync_repositories.list_sync_sources_for_versions(
                    self.connection,
                    version_ids,
                )
                if version_ids and has_sync_schema(self.connection)
                else {}
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            message = f"Versiunile nu au putut fi încărcate: {exc}"
            self.summary_var.set(message)
            self.validation_var.set("Eroare încărcare")
            apply_badge_style(self.validation_badge, "error")
            self.version_var.set("—")
            self.source_var.set("—")
            self.synced_at_var.set("—")
            self.variables_var.set("—")
            self._update_actions()
            self._status_callback(message, True)
            self._refresh_history_window()
            return

        self.rows = rows
        self.sync_sources_by_version = sync_sources
        active_row = next((row for row in rows if row.get("status") == "active"), None)
        historical_count = sum(1 for row in rows if row.get("status") != "active")
        selected_row = None
        if target_id is not None:
            selected_row = next(
                (row for row in rows if int(row["id"]) == target_id),
                None,
            )
        if selected_row is None and rows:
            selected_row = active_row if active_row is not None else rows[0]

        if selected_row is not None:
            self.selected_version_id = int(selected_row["id"])

        display_row = active_row if active_row is not None else selected_row
        owner_label = self.owner_name or f"ID {self.owner_id}"
        if not rows:
            self.summary_var.set(
                f"Nu există încă un document Word sincronizat pentru «{owner_label}»."
            )
            self.validation_var.set("Fără șablon")
            apply_badge_style(self.validation_badge, "neutral")
            self.version_var.set("Nicio versiune")
            self.source_var.set("Neasociat — configurați în Setări → Template-uri DOCX")
            self.synced_at_var.set("Niciodată")
            self.variables_var.set("—")
        else:
            badge_text, badge_kind = _friendly_validation_badge(display_row)
            self.validation_var.set(badge_text)
            apply_badge_style(self.validation_badge, badge_kind)

            if active_row is not None:
                hist_note = (
                    f" ({historical_count} în istoric)" if historical_count else ""
                )
                self.version_var.set(f"v{active_row['version_no']} — activă{hist_note}")
            else:
                self.version_var.set(
                    f"Fără versiune activă ({len(rows)} în istoric)"
                )

            assert display_row is not None
            display_id = int(display_row["id"])
            sync_source = sync_sources.get(display_id)
            if sync_source is not None:
                self.source_var.set(str(sync_source["relative_path"]))
                self.synced_at_var.set(str(sync_source["synced_at"]))
                self.summary_var.set(
                    f"{owner_label} · versiune activă v{display_row['version_no']}"
                    if active_row is not None
                    else f"{owner_label} · fără versiune activă"
                )
            else:
                self.source_var.set("Istoric / import manual anterior")
                self.synced_at_var.set(str(display_row.get("created_at") or "—"))
                self.summary_var.set(
                    f"{owner_label} · importat anterior sincronizării automate"
                )

            variables = display_row.get("variables") or ()
            self.variables_var.set(", ".join(variables) if variables else "Fără câmpuri dinamice")

        self._update_actions(display_row)
        self._refresh_history_window()

    def _selected_row(self) -> dict[str, Any] | None:
        if self.selected_version_id is None:
            return None
        return next(
            (row for row in self.rows if int(row["id"]) == self.selected_version_id),
            None,
        )

    def format_version_details(self, row: Mapping[str, Any] | None) -> str:
        base_report = _format_independent_report(row)
        if row is None:
            return base_report
        version_id = int(row["id"])
        sync_source = self.sync_sources_by_version.get(version_id)
        extra_lines = [""]
        if sync_source is not None:
            extra_lines.append(f"Sursă DOCX: {sync_source['relative_path']}")
            extra_lines.append(f"Data sincronizării: {sync_source['synced_at']}")
        else:
            extra_lines.append("Sursă DOCX: Istoric / import anterior sincronizării")
            if row.get("created_at"):
                extra_lines.append(f"Data înregistrării: {row['created_at']}")
        return base_report + "\n" + "\n".join(extra_lines)

    def _update_actions(self, selected: Mapping[str, Any] | None = None) -> None:
        row = selected if selected is not None else self._selected_row()
        self.history_button.configure(state="normal" if self.rows else "disabled")
        if row is None:
            self.preview_button.configure(state="disabled")
            return

        parser_report = row.get("parser_report")
        parser_status = (
            parser_report.get("status")
            if isinstance(parser_report, Mapping)
            else None
        )
        self.preview_button.configure(
            state="normal" if parser_status != "rejected" else "disabled"
        )

    def _refresh_history_window(self) -> None:
        window = self._history_window
        if window is None:
            return
        try:
            if not window.winfo_exists():
                self._history_window = None
                return
        except tk.TclError:
            self._history_window = None
            return
        window.refresh()

    def open_versions_history(self) -> tk.Toplevel | None:
        """Deschide fereastra secundară cu toate versiunile și raportul complet."""

        if not self.rows:
            return None
        existing = self._history_window
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.refresh()
                    existing.deiconify()
                    existing.lift()
                    existing.focus_set()
                    return existing
            except tk.TclError:
                pass
        window = _VersionsHistoryWindow(self, section=self)
        self._history_window = window
        return window

    def _resolve_target_id(self, template_version_id: int | None) -> int | None:
        return (
            template_version_id
            if template_version_id is not None
            else self.selected_version_id
        )

    def preview_version(
        self,
        template_version_id: int | None = None,
    ) -> tk.Toplevel | None:
        """Deschide previzualizarea read-only a versiunii selectate."""

        target_id = self._resolve_target_id(template_version_id)
        if target_id is None:
            return None
        try:
            content = template_sets.get_independent_template_content(
                self.connection,
                target_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._status_callback(f"Conținutul versiunii nu a putut fi încărcat: {exc}", True)
            return None
        document = content.get("document")
        if not _rich_document_has_content(document):
            self._status_callback(
                "Previzualizarea nu este disponibilă: conținutul parsat este gol.",
                True,
            )
            return None
        assert isinstance(document, RichDocument)
        doc_label = _DOCUMENT_LABELS[self.document_type]
        return self._preview_callback(f"Previzualizare — {doc_label}", document)


class MedicalLibraryView(ttk.Frame):
    """Pagina integrată «Șabloane» pentru diagnostice, intervenții și DOCX-uri V3."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
    ) -> None:
        super().__init__(parent, style="App.TFrame")
        self.connection = connection
        self.diagnoses: list[dict[str, Any]] = []
        self.interventions: list[dict[str, Any]] = []
        self._preview_window: tk.Toplevel | None = None
        self._preview_text_widget: tk.Text | None = None
        self._preview_render_state: Any = None
        style = ttk.Style(self)
        if not style.lookup("CardTitle.TLabel", "font"):
            apply_soft_medical_theme(self.winfo_toplevel())
        self._build_content()
        self.refresh()

    def _build_content(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1, minsize=250)
        self.rowconfigure(1, weight=0)

        records_frame = ttk.Frame(self, style="App.TFrame")
        records_frame.grid(row=0, column=0, sticky="nsew")
        records_frame.columnconfigure(0, weight=1, uniform="library-panels")
        records_frame.columnconfigure(1, weight=1, uniform="library-panels")
        records_frame.rowconfigure(0, weight=1)

        diagnoses_card = CardFrame(records_frame, padding=(14, 12))
        self.diagnoses_panel = diagnoses_card
        diagnoses_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        diagnoses_panel = diagnoses_card.body
        diagnoses_panel.columnconfigure(0, weight=1)
        diagnoses_panel.rowconfigure(2, weight=1)

        diag_header = ttk.Frame(diagnoses_panel, style="Card.TFrame")
        diag_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        diag_header.columnconfigure(1, weight=1)
        ttk.Label(diag_header, text="Diagnostice", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            diag_header,
            text="Epicriză + Recomandări",
            style="CardMuted.TLabel",
        ).grid(row=0, column=1, sticky="e")

        diag_search_row = ttk.Frame(diagnoses_panel, style="Card.TFrame")
        diag_search_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        diag_search_row.columnconfigure(1, weight=1)
        ttk.Label(
            diag_search_row,
            text="Caută",
            style="CardMuted.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.diagnosis_search_var = tk.StringVar(master=self, value="")
        self.diagnosis_search_entry = ttk.Entry(
            diag_search_row,
            textvariable=self.diagnosis_search_var,
        )
        self.diagnosis_search_entry.grid(row=0, column=1, sticky="ew")
        self.diagnosis_search_var.trace_add("write", self._on_diagnosis_search_changed)

        self.diagnosis_tree = self._build_tree(diagnoses_panel)
        self.diagnosis_tree.grid(row=2, column=0, sticky="nsew")
        self.diagnosis_tree.bind("<<TreeviewSelect>>", self._on_diagnosis_selected)
        diagnosis_scroll = ttk.Scrollbar(
            diagnoses_panel, orient="vertical", command=self.diagnosis_tree.yview
        )
        diagnosis_scroll.grid(row=2, column=1, sticky="ns")
        self.diagnosis_tree.configure(yscrollcommand=diagnosis_scroll.set)

        diagnosis_buttons = ttk.Frame(diagnoses_panel, style="Card.TFrame")
        diagnosis_buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.add_diagnosis_button = ttk.Button(
            diagnosis_buttons,
            text="Adaugă",
            style="Primary.TButton",
            command=self._add_diagnosis,
        )
        self.add_diagnosis_button.pack(side="left")
        self.edit_diagnosis_button = ttk.Button(
            diagnosis_buttons,
            text="Editează",
            style="Secondary.TButton",
            command=self._edit_diagnosis,
            state="disabled",
        )
        self.edit_diagnosis_button.pack(side="left", padx=(6, 0))
        self.toggle_diagnosis_button = ttk.Button(
            diagnosis_buttons,
            text="Activează / Dezactivează",
            style="Secondary.TButton",
            command=self._toggle_diagnosis,
            state="disabled",
        )
        self.toggle_diagnosis_button.pack(side="left", padx=(6, 0))

        interventions_card = CardFrame(records_frame, padding=(14, 12))
        self.interventions_panel = interventions_card
        interventions_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        interventions_panel = interventions_card.body
        interventions_panel.columnconfigure(0, weight=1)
        interventions_panel.rowconfigure(2, weight=1)

        int_header = ttk.Frame(interventions_panel, style="Card.TFrame")
        int_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        int_header.columnconfigure(1, weight=1)
        ttk.Label(int_header, text="Intervenții", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.selected_diagnosis_var = tk.StringVar(
            master=self, value="Selectați un diagnostic pentru a vedea intervențiile."
        )
        ttk.Label(
            int_header,
            textvariable=self.selected_diagnosis_var,
            style="CardMuted.TLabel",
            wraplength=250,
            justify="right",
        ).grid(row=0, column=1, sticky="e")

        int_search_row = ttk.Frame(interventions_panel, style="Card.TFrame")
        int_search_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        int_search_row.columnconfigure(1, weight=1)
        ttk.Label(
            int_search_row,
            text="Caută",
            style="CardMuted.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.intervention_search_var = tk.StringVar(master=self, value="")
        self.intervention_search_entry = ttk.Entry(
            int_search_row,
            textvariable=self.intervention_search_var,
        )
        self.intervention_search_entry.grid(row=0, column=1, sticky="ew")
        self.intervention_search_var.trace_add("write", self._on_intervention_search_changed)

        self.intervention_tree = self._build_tree(interventions_panel)
        self.intervention_tree.grid(row=2, column=0, sticky="nsew")
        self.intervention_tree.bind("<<TreeviewSelect>>", self._on_intervention_selected)
        intervention_scroll = ttk.Scrollbar(
            interventions_panel, orient="vertical", command=self.intervention_tree.yview
        )
        intervention_scroll.grid(row=2, column=1, sticky="ns")
        self.intervention_tree.configure(yscrollcommand=intervention_scroll.set)

        intervention_buttons = ttk.Frame(interventions_panel, style="Card.TFrame")
        intervention_buttons.grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        self.add_intervention_button = ttk.Button(
            intervention_buttons,
            text="Adaugă",
            style="Primary.TButton",
            command=self._add_intervention,
            state="disabled",
        )
        self.add_intervention_button.pack(side="left")
        self.edit_intervention_button = ttk.Button(
            intervention_buttons,
            text="Editează",
            style="Secondary.TButton",
            command=self._edit_intervention,
            state="disabled",
        )
        self.edit_intervention_button.pack(side="left", padx=(6, 0))
        self.toggle_intervention_button = ttk.Button(
            intervention_buttons,
            text="Activează / Dezactivează",
            style="Secondary.TButton",
            command=self._toggle_intervention,
            state="disabled",
        )
        self.toggle_intervention_button.pack(side="left", padx=(6, 0))

        self.documents_zone = ttk.Frame(self, style="App.TFrame")
        self.documents_zone.grid(row=1, column=0, sticky="ew", pady=(SPACE_MD, 0))
        self.documents_zone.columnconfigure(0, weight=1, uniform="docx-panels")
        self.documents_zone.columnconfigure(1, weight=1, uniform="docx-panels")

        self.epicrisis_panel = _IndependentDocumentSection(
            self.documents_zone,
            connection=self.connection,
            owner_type="diagnosis",
            title="Epicriză + Recomandări",
            status_callback=lambda msg, err: self._set_status(msg, error=err),
            preview_callback=lambda title, doc: self._open_preview_window(
                title=title, document=doc
            ),
        )
        self.epicrisis_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.protocol_panel = _IndependentDocumentSection(
            self.documents_zone,
            connection=self.connection,
            owner_type="intervention",
            title="Protocol operator",
            status_callback=lambda msg, err: self._set_status(msg, error=err),
            preview_callback=lambda title, doc: self._open_preview_window(
                title=title, document=doc
            ),
        )
        self.protocol_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self.epicrisis_summary_var = self.epicrisis_panel.summary_var
        self.epicrisis_preview_button = self.epicrisis_panel.preview_button
        self.epicrisis_history_button = self.epicrisis_panel.history_button

        self.protocol_summary_var = self.protocol_panel.summary_var
        self.protocol_preview_button = self.protocol_panel.preview_button
        self.protocol_history_button = self.protocol_panel.history_button

        self.status_var = tk.StringVar(master=self, value="")
        self.status_label = ttk.Label(
            self,
            textvariable=self.status_var,
            style="Muted.TLabel",
            wraplength=980,
            justify="left",
        )
        self.status_label.grid(row=2, column=0, sticky="w", pady=(8, 0))

        # Cadru nemapat păstrat exclusiv pentru compatibilitatea testelor de backend/câmpuri.
        self.fields_tab = ttk.Frame(self, padding=(12, 12))
        self._build_fields_tab()

    def _open_preview_window(
        self,
        *,
        title: str,
        document: RichDocument,
    ) -> tk.Toplevel | None:
        window = tk.Toplevel(self)
        window.title(title)
        style_toplevel(window)
        window.geometry("860x640")
        window.minsize(640, 440)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        shell = ttk.Frame(window, style="App.TFrame", padding=(18, 16))
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=title, style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Previzualizare read-only (nu modifică documentul Word)",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        card = CardFrame(shell, padding=(14, 14))
        card.grid(row=1, column=0, sticky="nsew")
        card.body.columnconfigure(0, weight=1)
        card.body.rowconfigure(0, weight=1)

        preview = tk.Text(card.body, wrap="word", undo=False, state="disabled")
        style_text_widget(preview, subtle=False)
        preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(
            card.body,
            orient="vertical",
            command=preview.yview,
        )
        preview_scroll.grid(row=0, column=1, sticky="ns")
        preview.configure(yscrollcommand=preview_scroll.set)
        try:
            self._preview_render_state = render_rich_document(preview, document)
        except (TypeError, ValueError, tk.TclError) as exc:
            window.destroy()
            self._set_status(f"Previzualizarea nu a putut fi afișată: {exc}", error=True)
            return None
        preview.configure(state="disabled")

        footer = ttk.Frame(shell, style="App.TFrame")
        footer.grid(row=2, column=0, sticky="e", pady=(10, 0))
        ttk.Button(
            footer,
            text="Închide",
            style="Secondary.TButton",
            command=window.destroy,
        ).pack(side="right")

        window.bind("<Escape>", lambda _event: window.destroy())
        self._preview_window = window
        self._preview_text_widget = preview
        self._set_status(
            "Previzualizarea este read-only; șablonul original nu a fost modificat."
        )
        return window

    def _build_fields_tab(self) -> None:
        self.fields_tab.columnconfigure(0, weight=1)
        self.fields_tab.columnconfigure(1, weight=2)
        self.fields_tab.rowconfigure(1, weight=1)
        self.fields_tab.rowconfigure(3, weight=1)

        filters = ttk.Frame(self.fields_tab)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        filters.columnconfigure(3, weight=1)
        ttk.Label(filters, text="Domeniu").grid(row=0, column=0, sticky="w")
        self.scope_filter_var = tk.StringVar(master=self, value="all")
        self.scope_filter_combo = ttk.Combobox(
            filters,
            textvariable=self.scope_filter_var,
            values=("all", *FIELD_SCOPES),
            state="readonly",
            width=18,
        )
        self.scope_filter_combo.grid(row=0, column=1, sticky="w", padx=(8, 18))
        self.scope_filter_combo.bind("<<ComboboxSelected>>", self._on_scope_filter_changed)
        ttk.Label(filters, text="Asociere").grid(row=0, column=2, sticky="w")
        self.field_context_filter_var = tk.StringVar(master=self, value="")
        self.field_context_filter_combo = ttk.Combobox(
            filters,
            textvariable=self.field_context_filter_var,
            state="disabled",
            width=48,
        )
        self.field_context_filter_combo.grid(row=0, column=3, sticky="ew", padx=(8, 0))
        self.field_context_filter_combo.bind(
            "<<ComboboxSelected>>", self._on_field_context_filter_changed
        )

        fields_panel = ttk.LabelFrame(self.fields_tab, text="Definiții curente", padding=8)
        fields_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        fields_panel.columnconfigure(0, weight=1)
        fields_panel.rowconfigure(0, weight=1)
        self.field_tree = ttk.Treeview(
            fields_panel,
            columns=("key", "label", "type", "scope", "context"),
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in (
            ("key", "Cheie", 130),
            ("label", "Etichetă", 150),
            ("type", "Tip", 80),
            ("scope", "Domeniu", 105),
            ("context", "Asociere", 190),
        ):
            self.field_tree.heading(column, text=heading)
            self.field_tree.column(column, width=width, anchor="w", stretch=column in {"label", "context"})
        self.field_tree.grid(row=0, column=0, sticky="nsew")
        field_scroll = ttk.Scrollbar(fields_panel, orient="vertical", command=self.field_tree.yview)
        field_scroll.grid(row=0, column=1, sticky="ns")
        self.field_tree.configure(yscrollcommand=field_scroll.set)
        self.field_tree.bind("<<TreeviewSelect>>", self._on_field_selected)
        field_buttons = ttk.Frame(fields_panel)
        field_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(field_buttons, text="Adaugă câmp", command=self._add_variable).pack(side="left")
        self.new_version_button = ttk.Button(
            field_buttons, text="Creează versiune", command=self._create_variable_version, state="disabled"
        )
        self.new_version_button.pack(side="left", padx=(7, 0))

        detail_panel = ttk.LabelFrame(self.fields_tab, text="Versiuni și detalii", padding=8)
        detail_panel.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        detail_panel.columnconfigure(0, weight=1)
        detail_panel.rowconfigure(1, weight=1)
        self.field_detail_var = tk.StringVar(master=self, value="Selectați un câmp pentru detalii.")
        ttk.Label(detail_panel, textvariable=self.field_detail_var, justify="left", wraplength=650).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8)
        )
        self.version_tree = ttk.Treeview(
            detail_panel,
            columns=("version", "current", "label", "type", "scope"),
            show="headings",
            selectmode="browse",
            height=6,
        )
        for column, heading, width in (
            ("version", "Versiune", 75),
            ("current", "Curentă", 75),
            ("label", "Etichetă", 170),
            ("type", "Tip", 80),
            ("scope", "Domeniu", 100),
        ):
            self.version_tree.heading(column, text=heading)
            self.version_tree.column(column, width=width, anchor="w", stretch=column == "label")
        self.version_tree.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.version_tree.bind("<<TreeviewSelect>>", self._on_version_selected)
        version_buttons = ttk.Frame(detail_panel)
        version_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.set_current_button = ttk.Button(
            version_buttons, text="Setează versiunea curentă", command=self._set_current_version, state="disabled"
        )
        self.set_current_button.pack(side="left")
        self.rollback_button = ttk.Button(
            version_buttons, text="Revino la versiune", command=self._rollback_version, state="disabled"
        )
        self.rollback_button.pack(side="left", padx=(7, 0))

        options_panel = ttk.LabelFrame(self.fields_tab, text="Opțiuni pentru versiunea selectată", padding=8)
        options_panel.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        options_panel.columnconfigure(0, weight=1)
        self.option_tree = ttk.Treeview(
            options_panel,
            columns=("value", "label", "order"),
            show="headings",
            selectmode="browse",
            height=4,
        )
        for column, heading, width in (
            ("value", "Valoare", 180),
            ("label", "Etichetă afișată", 300),
            ("order", "Ordine", 90),
        ):
            self.option_tree.heading(column, text=heading)
            self.option_tree.column(column, width=width, anchor="w", stretch=column == "label")
        self.option_tree.grid(row=0, column=0, sticky="ew")
        option_scroll = ttk.Scrollbar(options_panel, orient="vertical", command=self.option_tree.yview)
        option_scroll.grid(row=0, column=1, sticky="ns")
        self.option_tree.configure(yscrollcommand=option_scroll.set)
        self.add_option_button = ttk.Button(
            options_panel, text="Adaugă opțiune", command=self._add_field_option, state="disabled"
        )
        self.add_option_button.grid(row=1, column=0, sticky="w", pady=(7, 0))

        self.field_diagnoses: list[dict[str, Any]] = []
        self.field_interventions: list[dict[str, Any]] = []
        self.field_definitions: list[dict[str, Any]] = []
        self.selected_field_versions: list[dict[str, Any]] = []
        self.selected_field: dict[str, Any] | None = None
        self.selected_version: dict[str, Any] | None = None
        self.field_context_ids: list[int] = []

    def _on_scope_filter_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._refresh_field_context_filter()
        self._refresh_field_list()

    def _on_field_context_filter_changed(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._refresh_field_list()

    def _refresh_field_context_filter(self, preferred_id: int | None = None) -> None:
        scope = self.scope_filter_var.get()
        choices = _context_choices(scope, self.field_diagnoses, self.field_interventions)
        self.field_context_ids = [record_id for record_id, _label in choices]
        if scope not in {"diagnosis", "intervention"}:
            self.field_context_filter_combo.configure(values=(), state="disabled")
            self.field_context_filter_var.set("")
            return
        labels = ["Toate"] + [label for _record_id, label in choices]
        self.field_context_filter_combo.configure(values=tuple(labels), state="readonly")
        if preferred_id in self.field_context_ids:
            self.field_context_filter_combo.current(self.field_context_ids.index(preferred_id) + 1)
        else:
            self.field_context_filter_combo.current(0)

    def _refresh_fields(self, *, preferred_key: str | None = None, preferred_version_id: int | None = None) -> None:
        old_key = self._selected_field_key()
        if preferred_key is None:
            preferred_key = old_key
        old_version_id = self._selected_version_id()
        if preferred_version_id is None:
            preferred_version_id = old_version_id
        try:
            self.field_diagnoses = medical_library.list_diagnoses(self.connection, active_only=False)
            self.field_interventions = medical_library.list_interventions(self.connection, active_only=False)
            self.field_definitions = medical_library.list_variables(
                self.connection,
                scope=None if self.scope_filter_var.get() == "all" else self.scope_filter_var.get(),
                current_only=True,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.field_diagnoses = []
            self.field_interventions = []
            self.field_definitions = []
            self._replace_field_rows(())
            self._clear_field_details()
            self._set_status(f"Câmpurile medicale nu au putut fi încărcate: {exc}", error=True)
            return
        self._refresh_field_context_filter(
            self._selected_context_filter_id()
        )
        context_id = self._selected_context_filter_id()
        scope = self.scope_filter_var.get()
        records = [
            item
            for item in self.field_definitions
            if (scope != "diagnosis" or context_id is None or item.get("diagnosis_id") == context_id)
            and (scope != "intervention" or context_id is None or item.get("intervention_id") == context_id)
        ]
        self.field_definitions = records
        self._replace_field_rows(records)
        if preferred_key and any(item["variable_key"] == preferred_key for item in records):
            self._select_tree_item(self.field_tree, str(preferred_key))
            self._load_field_versions(str(preferred_key), preferred_version_id=preferred_version_id)
        else:
            self.field_tree.selection_remove(self.field_tree.selection())
            self._clear_field_details()

    def _refresh_field_list(self, *, preferred_key: str | None = None) -> None:
        self._refresh_fields(preferred_key=preferred_key)

    def _selected_context_filter_id(self) -> int | None:
        if self.scope_filter_var.get() not in {"diagnosis", "intervention"}:
            return None
        index = self.field_context_filter_combo.current()
        if index <= 0 or index - 1 >= len(self.field_context_ids):
            return None
        return self.field_context_ids[index - 1]

    def _replace_field_rows(self, records: Iterable[Mapping[str, Any]]) -> None:
        for item in self.field_tree.get_children():
            self.field_tree.delete(item)
        for record in records:
            key = str(record["variable_key"])
            self.field_tree.insert(
                "", "end", iid=key,
                values=(key, str(record["label"]), str(record["data_type"]),
                        _SCOPE_LABELS[str(record["scope"])],
                        _field_context_text(record, self.field_diagnoses, self.field_interventions)),
            )

    def _selected_field_key(self) -> str | None:
        selection = self.field_tree.selection()
        return str(selection[0]) if selection else None

    def _selected_version_id(self) -> int | None:
        selection = self.version_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def _on_field_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        key = self._selected_field_key()
        if key is None:
            self._clear_field_details()
            return
        self.new_version_button.configure(state="normal")
        self._load_field_versions(key)

    def _load_field_versions(self, key: str, *, preferred_version_id: int | None = None) -> None:
        try:
            versions = medical_library.list_variables(self.connection, variable_key=key)
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._clear_field_details()
            self._set_status(f"Versiunile câmpului nu au putut fi încărcate: {exc}", error=True)
            return
        self.selected_field_versions = versions
        for item in self.version_tree.get_children():
            self.version_tree.delete(item)
        for version in versions:
            definition_id = int(version["id"])
            self.version_tree.insert(
                "", "end", iid=str(definition_id),
                values=(version["version"], "Da" if version["is_current"] else "Nu",
                        version["label"], version["data_type"], _SCOPE_LABELS[version["scope"]]),
            )
        current = next((row for row in versions if row["is_current"]), None)
        target_id = preferred_version_id
        if target_id is None or not any(int(row["id"]) == target_id for row in versions):
            target_id = int(current["id"]) if current else (int(versions[-1]["id"]) if versions else None)
        if target_id is not None:
            self._select_tree_item(self.version_tree, target_id)
            self._display_selected_version()
        else:
            self._clear_field_details()

    def _on_version_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._display_selected_version()

    def _display_selected_version(self) -> None:
        version_id = self._selected_version_id()
        version = next((row for row in self.selected_field_versions if int(row["id"]) == version_id), None)
        self.selected_field = version
        self.selected_version = version
        if version is None:
            self.field_detail_var.set("Selectați o versiune pentru detalii.")
            self.set_current_button.configure(state="disabled")
            self.rollback_button.configure(state="disabled")
            self._clear_options()
            return
        context = _field_context_text(version, self.field_diagnoses, self.field_interventions)
        current = "da" if version["is_current"] else "nu"
        self.field_detail_var.set(
            f"ID definiție: {version['id']}\nCheie: {version['variable_key']} · versiunea {version['version']}\n"
            f"Etichetă: {version['label']} · tip: {version['data_type']}\n"
            f"Domeniu: {_SCOPE_LABELS[version['scope']]} · asociere: {context}\nVersiune curentă: {current}"
        )
        current_version = next((row for row in self.selected_field_versions if row["is_current"]), None)
        self.set_current_button.configure(state="normal" if not version["is_current"] else "disabled")
        self.rollback_button.configure(
            state="normal" if current_version and int(version["version"]) < int(current_version["version"]) else "disabled"
        )
        self._load_options(version)

    def _load_options(self, version: Mapping[str, Any]) -> None:
        for item in self.option_tree.get_children():
            self.option_tree.delete(item)
        is_select = version["data_type"] == "select"
        self.add_option_button.configure(state="normal" if is_select else "disabled")
        if not is_select:
            return
        try:
            options = medical_library.list_field_options(self.connection, int(version["id"]))
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_status(f"Opțiunile câmpului nu au putut fi încărcate: {exc}", error=True)
            return
        for index, option in enumerate(options):
            iid = f"option-{index}"
            self.option_tree.insert(
                "", "end", iid=iid,
                values=(option["option_value"], option["option_label"], option["sort_order"]),
            )

    def _clear_options(self) -> None:
        if hasattr(self, "option_tree"):
            for item in self.option_tree.get_children():
                self.option_tree.delete(item)
            self.add_option_button.configure(state="disabled")

    def _clear_field_details(self) -> None:
        self.selected_field_versions = []
        self.selected_field = None
        self.selected_version = None
        if hasattr(self, "version_tree"):
            for item in self.version_tree.get_children():
                self.version_tree.delete(item)
            self.field_detail_var.set("Selectați un câmp pentru detalii.")
            self.new_version_button.configure(state="disabled")
            self.set_current_button.configure(state="disabled")
            self.rollback_button.configure(state="disabled")
            self._clear_options()

    def _field_dialog_context(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self.field_diagnoses, self.field_interventions

    def _add_variable(self) -> None:
        diagnoses, interventions = self._field_dialog_context()
        result = _prompt_field_definition(self, diagnoses=diagnoses, interventions=interventions)
        if result is None:
            return
        try:
            definition_id = medical_library.create_variable(
                self.connection,
                result["variable_key"], result["label"], result["data_type"], result["scope"],
                diagnosis_id=result["diagnosis_id"], intervention_id=result["intervention_id"],
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_field_operation_error("Câmpul nu a fost creat", exc)
            return
        key = str(result["variable_key"])
        self._refresh_fields(preferred_key=key, preferred_version_id=definition_id)
        self._set_status(
            "Câmpul a fost creat. Adăugați opțiunile necesare pentru versiunea select dacă este cazul."
            if result["data_type"] == "select" else "Câmpul a fost creat."
        )

    def _create_variable_version(self) -> None:
        key = self._selected_field_key()
        version = self.selected_version
        if key is None or version is None:
            return
        diagnoses, interventions = self._field_dialog_context()
        result = _prompt_field_definition(
            self, diagnoses=diagnoses, interventions=interventions, initial=version
        )
        if result is None:
            return
        try:
            definition_id = medical_library.create_variable_version(
                self.connection,
                key, result["label"], result["data_type"], result["scope"],
                diagnosis_id=result["diagnosis_id"], intervention_id=result["intervention_id"],
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_field_operation_error("Versiunea nouă nu a fost creată", exc)
            return
        self._refresh_fields(preferred_key=key, preferred_version_id=definition_id)
        self._set_status(
            "Versiunea nouă a fost creată și nu este curentă. Adăugați opțiunile necesare dacă tipul este select."
            if result["data_type"] == "select" else "Versiunea nouă a fost creată și nu este curentă."
        )

    def _set_current_version(self) -> None:
        version = self.selected_version
        if version is None or version["is_current"]:
            return
        if not messagebox.askyesno(
            "Schimbă versiunea curentă",
            f"Setați versiunea {version['version']} a câmpului {version['variable_key']} ca versiune curentă?",
            parent=self,
        ):
            return
        key = str(version["variable_key"])
        try:
            medical_library.set_current_variable_version(self.connection, key, int(version["version"]))
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_field_operation_error("Versiunea curentă nu a fost schimbată", exc)
            return
        self._refresh_fields(preferred_key=key, preferred_version_id=int(version["id"]))
        self._set_status("Versiunea curentă a fost schimbată.")

    def _rollback_version(self) -> None:
        version = self.selected_version
        if version is None:
            return
        key = str(version["variable_key"])
        if not messagebox.askyesno(
            "Revino la o versiune anterioară",
            f"Reveniți la versiunea {version['version']} a câmpului {key}?",
            parent=self,
        ):
            return
        try:
            medical_library.rollback_variable_version(self.connection, key, int(version["version"]))
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_field_operation_error("Revenirea la versiunea selectată nu a reușit", exc)
            return
        self._refresh_fields(preferred_key=key, preferred_version_id=int(version["id"]))
        self._set_status("Versiunea selectată este acum versiunea curentă.")

    def _add_field_option(self) -> None:
        version = self.selected_version
        if version is None or version["data_type"] != "select":
            return
        result = _prompt_field_option(self)
        if result is None:
            return
        try:
            medical_library.add_field_option(
                self.connection, int(version["id"]), result[0], result[1]
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_field_operation_error("Opțiunea nu a fost adăugată", exc)
            return
        self._display_selected_version()
        self._set_status("Opțiunea a fost adăugată.")

    @staticmethod
    def _build_tree(parent: ttk.Frame) -> ttk.Treeview:
        tree = ttk.Treeview(
            parent,
            columns=("name", "code", "status"),
            show="headings",
            selectmode="browse",
            height=6,
        )
        for column, heading, width in (
            ("name", "Denumire", 220),
            ("code", "Cod", 85),
            ("status", "Stare", 70),
        ):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor="w", stretch=column == "name")
        return tree

    def on_show(self) -> None:
        """Reîncarcă listele când utilizatorul revine în secțiunea Șabloane."""

        self.refresh()

    def refresh(self) -> None:
        diagnosis_id = self._selected_id(self.diagnosis_tree)
        intervention_id = self._selected_id(self.intervention_tree)
        self._refresh(diagnosis_id=diagnosis_id, intervention_id=intervention_id)

    @staticmethod
    def _filter_records(
        records: Iterable[Mapping[str, Any]],
        query: str,
    ) -> list[Mapping[str, Any]]:
        cleaned = query.strip().casefold()
        if not cleaned:
            return list(records)
        filtered: list[Mapping[str, Any]] = []
        for record in records:
            name = str(record.get("name") or "").casefold()
            raw_code = record.get("code")
            code = raw_code.strip().casefold() if isinstance(raw_code, str) else ""
            if cleaned in name or (bool(code) and cleaned in code):
                filtered.append(record)
        return filtered

    def _update_diagnosis_action_buttons(self, selected: Mapping[str, Any] | None) -> None:
        if selected is None:
            self.edit_diagnosis_button.configure(state="disabled")
            self.toggle_diagnosis_button.configure(
                text="Activează / Dezactivează", state="disabled"
            )
            return
        self.edit_diagnosis_button.configure(state="normal")
        self.toggle_diagnosis_button.configure(
            text="Dezactivează" if bool(selected["is_active"]) else "Activează",
            state="normal",
        )

    def _update_intervention_action_buttons(
        self, selected: Mapping[str, Any] | None
    ) -> None:
        if selected is None:
            self.edit_intervention_button.configure(state="disabled")
            self.toggle_intervention_button.configure(
                text="Activează / Dezactivează", state="disabled"
            )
            return
        self.edit_intervention_button.configure(state="normal")
        self.toggle_intervention_button.configure(
            text="Dezactivează" if bool(selected["is_active"]) else "Activează",
            state="normal",
        )

    def _on_diagnosis_search_changed(self, *_args: object) -> None:
        if not hasattr(self, "diagnosis_tree"):
            return
        preferred_id = self._selected_id(self.diagnosis_tree)
        filtered = self._filter_records(
            self.diagnoses, self.diagnosis_search_var.get()
        )
        self._replace_tree(self.diagnosis_tree, filtered)
        if preferred_id is not None and any(
            int(item["id"]) == preferred_id for item in filtered
        ):
            self._select_tree_item(self.diagnosis_tree, preferred_id)
            self._update_diagnosis_action_buttons(self._selected_diagnosis())
        else:
            self._update_diagnosis_action_buttons(None)
            self._clear_interventions()

    def _on_intervention_search_changed(self, *_args: object) -> None:
        if not hasattr(self, "intervention_tree"):
            return
        preferred_id = self._selected_id(self.intervention_tree)
        filtered = self._filter_records(
            self.interventions, self.intervention_search_var.get()
        )
        self._replace_tree(self.intervention_tree, filtered)
        selected = next(
            (
                item
                for item in filtered
                if preferred_id is not None and int(item["id"]) == preferred_id
            ),
            None,
        )
        if selected is not None:
            self._select_tree_item(self.intervention_tree, int(selected["id"]))
            self._update_intervention_action_buttons(selected)
            self.protocol_panel.set_owner(
                int(selected["id"]),
                owner_name=str(selected["name"]),
            )
        else:
            self._update_intervention_action_buttons(None)
            self.protocol_panel.set_owner(None)

    def _refresh(
        self,
        *,
        diagnosis_id: int | None,
        intervention_id: int | None = None,
    ) -> bool:
        try:
            self.diagnoses = medical_library.list_diagnoses(
                self.connection, active_only=False
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.diagnoses = []
            self._replace_tree(self.diagnosis_tree, ())
            self._clear_interventions()
            self._set_status(f"Diagnosticele nu au putut fi încărcate: {exc}", error=True)
            self._update_diagnosis_action_buttons(None)
            self._refresh_fields()
            return False

        self._set_status("")
        search_query = (
            self.diagnosis_search_var.get()
            if hasattr(self, "diagnosis_search_var")
            else ""
        )
        filtered_diagnoses = self._filter_records(self.diagnoses, search_query)
        self._replace_tree(self.diagnosis_tree, filtered_diagnoses)
        selected_diagnosis = next(
            (
                item
                for item in filtered_diagnoses
                if diagnosis_id is not None and int(item["id"]) == diagnosis_id
            ),
            None,
        )
        if selected_diagnosis is not None:
            self._select_tree_item(self.diagnosis_tree, int(selected_diagnosis["id"]))
            self._update_diagnosis_action_buttons(selected_diagnosis)
            if not self._load_interventions(
                int(selected_diagnosis["id"]), intervention_id=intervention_id
            ):
                self._refresh_fields()
                return False
        else:
            self._update_diagnosis_action_buttons(None)
            self._clear_interventions()
        self._refresh_fields()
        return True

    @staticmethod
    def _replace_tree(
        tree: ttk.Treeview,
        records: Iterable[Mapping[str, Any]],
    ) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for record in records:
            tree.insert("", "end", iid=str(record["id"]), values=_record_tree_values(record))

    @staticmethod
    def _selected_id(tree: ttk.Treeview) -> int | None:
        selection = tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    @staticmethod
    def _select_tree_item(tree: ttk.Treeview, record_id: int) -> None:
        item_id = str(record_id)
        if tree.exists(item_id):
            tree.selection_set(item_id)
            tree.focus(item_id)
            tree.see(item_id)

    def _on_diagnosis_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        diagnosis_id = self._selected_id(self.diagnosis_tree)
        selected_intervention = self._selected_intervention()
        intervention_id = None
        if (
            selected_intervention is not None
            and int(selected_intervention["diagnosis_id"]) == diagnosis_id
        ):
            intervention_id = int(selected_intervention["id"])
        self._update_diagnosis_action_buttons(self._selected_diagnosis())
        self._load_interventions(diagnosis_id, intervention_id=intervention_id)

    def _load_interventions(
        self,
        diagnosis_id: int | None,
        *,
        intervention_id: int | None = None,
    ) -> bool:
        selected_diagnosis = next(
            (item for item in self.diagnoses if int(item["id"]) == diagnosis_id),
            None,
        )
        if selected_diagnosis is None:
            self._clear_interventions()
            return True
        diagnosis_name = str(selected_diagnosis["name"])
        self.selected_diagnosis_var.set(f"Diagnostic selectat: {diagnosis_name}")
        self.add_intervention_button.configure(state="normal")
        self.epicrisis_panel.set_owner(diagnosis_id, owner_name=diagnosis_name)
        try:
            self.interventions = medical_library.list_interventions(
                self.connection,
                diagnosis_id=diagnosis_id,
                active_only=False,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.interventions = []
            self._replace_tree(self.intervention_tree, ())
            self._update_intervention_action_buttons(None)
            self.protocol_panel.set_owner(None)
            self._set_status(f"Intervențiile nu au putut fi încărcate: {exc}", error=True)
            return False
        int_query = (
            self.intervention_search_var.get()
            if hasattr(self, "intervention_search_var")
            else ""
        )
        filtered_interventions = self._filter_records(self.interventions, int_query)
        self._replace_tree(self.intervention_tree, filtered_interventions)
        selected_intervention = next(
            (
                item
                for item in filtered_interventions
                if intervention_id is not None and int(item["id"]) == intervention_id
            ),
            None,
        )
        if selected_intervention is not None:
            self._select_tree_item(
                self.intervention_tree, int(selected_intervention["id"])
            )
            self._update_intervention_action_buttons(selected_intervention)
            self.protocol_panel.set_owner(
                int(selected_intervention["id"]),
                owner_name=str(selected_intervention["name"]),
            )
        else:
            self._update_intervention_action_buttons(None)
            self.protocol_panel.set_owner(None)
        return True

    def _clear_interventions(self) -> None:
        self.interventions = []
        if not hasattr(self, "intervention_tree"):
            return
        self._replace_tree(self.intervention_tree, ())
        self.selected_diagnosis_var.set(
            "Selectați un diagnostic pentru a vedea intervențiile."
        )
        self.add_intervention_button.configure(state="disabled")
        self._update_intervention_action_buttons(None)
        self.epicrisis_panel.set_owner(None)
        self.protocol_panel.set_owner(None)

    def _on_intervention_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._selected_diagnosis() is None:
            selection = self.intervention_tree.selection()
            if selection:
                self.intervention_tree.selection_remove(*selection)
            self._update_intervention_action_buttons(None)
            self.protocol_panel.set_owner(None)
            return
        selected = self._selected_intervention()
        self._update_intervention_action_buttons(selected)
        if selected is not None:
            self.protocol_panel.set_owner(
                int(selected["id"]),
                owner_name=str(selected["name"]),
            )
        else:
            self.protocol_panel.set_owner(None)

    def _selected_diagnosis(self) -> dict[str, Any] | None:
        selected_id = self._selected_id(self.diagnosis_tree)
        return next(
            (item for item in self.diagnoses if int(item["id"]) == selected_id),
            None,
        )

    def _selected_intervention(self) -> dict[str, Any] | None:
        selected_id = self._selected_id(self.intervention_tree)
        return next(
            (item for item in self.interventions if int(item["id"]) == selected_id),
            None,
        )

    def _add_diagnosis(self) -> None:
        result = _prompt_record(self, title="Adaugă diagnostic")
        if result is None:
            return
        name, code = result
        try:
            diagnosis_id = medical_library.add_diagnosis(
                self.connection, name, code=code
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Diagnosticul nu a fost adăugat", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id):
            self._set_status("Diagnosticul a fost adăugat.")

    def _edit_diagnosis(self) -> None:
        diagnosis = self._selected_diagnosis()
        if diagnosis is None:
            return
        result = _prompt_record(
            self,
            title="Editează diagnostic",
            name=str(diagnosis["name"]),
            code=diagnosis.get("code"),
        )
        if result is None:
            return
        name, code = result
        diagnosis_id = int(diagnosis["id"])
        try:
            medical_library.update_diagnosis(
                self.connection, diagnosis_id, name, code=code
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Diagnosticul nu a fost modificat", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id):
            self._set_status("Diagnosticul a fost modificat.")

    def _toggle_diagnosis(self) -> None:
        diagnosis = self._selected_diagnosis()
        if diagnosis is None:
            return
        next_active = not bool(diagnosis["is_active"])
        if not next_active and not messagebox.askyesno(
            "Dezactivează diagnosticul",
            "Generarea documentelor pentru intervențiile asociate va fi blocată "
            "cât timp diagnosticul este inactiv. Doriți să continuați?",
            parent=self,
        ):
            return
        diagnosis_id = int(diagnosis["id"])
        try:
            medical_library.set_diagnosis_active(
                self.connection, diagnosis_id, next_active
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Starea diagnosticului nu a fost modificată", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id):
            self._set_status(
                "Diagnosticul a fost activat."
                if next_active
                else "Diagnosticul a fost dezactivat."
            )

    def _add_intervention(self) -> None:
        diagnosis = self._selected_diagnosis()
        if diagnosis is None:
            return
        result = _prompt_record(self, title="Adaugă intervenție")
        if result is None:
            return
        name, code = result
        diagnosis_id = int(diagnosis["id"])
        try:
            intervention_id = medical_library.add_intervention(
                self.connection, diagnosis_id, name, code=code
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Intervenția nu a fost adăugată", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id, intervention_id=intervention_id):
            self._set_status("Intervenția a fost adăugată.")

    def _edit_intervention(self) -> None:
        diagnosis = self._selected_diagnosis()
        intervention = self._selected_intervention()
        if diagnosis is None or intervention is None:
            return
        diagnosis_name = str(diagnosis["name"])
        result = _prompt_record(
            self,
            title="Editează intervenție",
            name=str(intervention["name"]),
            code=intervention.get("code"),
            relationship=diagnosis_name,
        )
        if result is None:
            return
        name, code = result
        intervention_id = int(intervention["id"])
        diagnosis_id = int(intervention["diagnosis_id"])
        try:
            medical_library.update_intervention(
                self.connection,
                intervention_id,
                name,
                code=code,
                diagnosis_id=diagnosis_id,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Intervenția nu a fost modificată", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id, intervention_id=intervention_id):
            self._set_status("Intervenția a fost modificată.")

    def _toggle_intervention(self) -> None:
        diagnosis = self._selected_diagnosis()
        intervention = self._selected_intervention()
        if diagnosis is None or intervention is None:
            return
        next_active = not bool(intervention["is_active"])
        if not next_active and not messagebox.askyesno(
            "Dezactivează intervenția",
            "Doriți să dezactivați această intervenție? Generarea documentelor "
            "pentru ea nu va mai fi disponibilă.",
            parent=self,
        ):
            return
        diagnosis_id = int(diagnosis["id"])
        intervention_id = int(intervention["id"])
        try:
            medical_library.set_intervention_active(
                self.connection, intervention_id, next_active
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_operation_error("Starea intervenției nu a fost modificată", exc)
            return
        if self._refresh(diagnosis_id=diagnosis_id, intervention_id=intervention_id):
            self._set_status(
                "Intervenția a fost activată."
                if next_active
                else "Intervenția a fost dezactivată."
            )

    def _set_operation_error(self, operation: str, error: Exception) -> None:
        if isinstance(error, medical_library.MedicalLibraryValidationError):
            detail = f"Verificați denumirea și codul. {error}"
        elif isinstance(error, medical_library.MedicalLibraryDuplicateError):
            detail = str(error)
        elif isinstance(error, medical_library.MedicalLibraryNotFoundError):
            detail = f"Înregistrarea asociată nu mai există. {error}"
        else:
            detail = str(error)
        self._set_status(f"{operation}: {detail}", error=True)

    def _set_field_operation_error(self, operation: str, error: Exception) -> None:
        self._set_status(f"{operation}: {error}", error=True)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_var.set(message)
        self.status_label.configure(
            style="GenerationError.TLabel" if error else "Muted.TLabel"
        )


TemplatesView = MedicalLibraryView

__all__ = ["MedicalLibraryView", "TemplatesView"]
