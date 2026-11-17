"""Consultarea seturilor de șabloane și a versiunilor lor în Biblioteca medicală."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections.abc import Mapping
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ortodocs.models.rich_document import LineBreakInline, RichDocument, TextInline
from ortodocs.services import medical_library, template_sets
from ortodocs.ui.rich_text_codec import render_rich_document
from ortodocs.ui.theme import apply_window_icon


_SET_STATUS_LABELS = {
    "draft": "Draft",
    "active": "Activ",
    "retired": "Retras",
    "rejected": "Respins",
}
_VALIDATION_STATUS_LABELS = {
    "not_run": "Neverificat",
    "blocked": "Blocat",
    "valid": "Valid",
}
_IMPORT_STATUS_LABELS = {
    "valid": "Valid",
    "valid_with_warnings": "Valid cu avertismente",
    "rejected": "Respins",
}
_TEMPLATE_VALIDATION_LABELS = {
    "pending": "În așteptare",
    "valid": "Valid",
    "invalid": "Invalid",
}
_DOCUMENT_LABELS = {
    template_sets.EPICRISIS_RECOMMENDATIONS: "Epicriză + Recomandări",
    template_sets.OPERATIVE_PROTOCOL: "Protocol operator",
}
_IMPORT_ERROR_CATEGORIES = (
    ("formatting_errors", "Erori de formatare"),
    ("unsupported_elements", "Elemente nesuportate"),
    ("variable_errors", "Erori de tokenuri"),
    ("content_errors", "Erori de conținut"),
    ("archive_errors", "Erori de arhivă"),
    ("xml_errors", "Erori XML"),
    ("structure_errors", "Erori de structură"),
    ("resource_errors", "Erori de resurse"),
    ("parsing_errors", "Erori de parsare"),
)


def _format_import_issue_lines(import_report: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    issue_groups = [
        (label, import_report.get(key))
        for key, label in _IMPORT_ERROR_CATEGORIES
    ]
    issue_groups.append(("Avertismente", import_report.get("warnings")))
    found_issues = False
    for label, issues in issue_groups:
        if not isinstance(issues, list) or not issues:
            continue
        found_issues = True
        lines.append("")
        lines.append(f"{label}:")
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            code = str(issue.get("code", "cod indisponibil"))
            message = str(issue.get("message", "Mesaj indisponibil."))
            lines.append(f"• {code}: {message}")
            location = [
                f"{field}={issue[field]}"
                for field in ("part", "paragraph_index", "run_index", "element")
                if issue.get(field) is not None
            ]
            if location:
                lines.append("  Locație: " + ", ".join(location))
    if not found_issues:
        lines.append("")
        lines.append("Raportul nu conține erori sau avertismente.")
    return lines


def _format_import_report(template: Mapping[str, Any] | None) -> str:
    if template is None:
        return "Documentul nu a fost importat."

    validation_report = template.get("validation_report")
    validation_report = (
        validation_report if isinstance(validation_report, Mapping) else {}
    )
    import_report = validation_report.get("import_report")
    if not isinstance(import_report, Mapping):
        return "Raportul parserului nu este disponibil pentru acest document."

    status = str(import_report.get("status", "Nedisponibil"))
    lines = [f"Status parser: {status}"]
    confirmation = validation_report.get("warning_confirmation")
    if status == "valid_with_warnings":
        confirmed = (
            isinstance(confirmation, Mapping)
            and confirmation.get("confirmed") is True
        )
        lines.append(
            "Avertismente revizuite: " + ("Da" if confirmed else "Nu")
        )
        lines.append(
            "Confirmarea indică revizuirea avertismentelor parserului; "
            "nu aprobă automat setul pentru activare."
        )
    else:
        lines.append("Confirmarea avertismentelor: Nu se aplică.")

    lines.extend(_format_import_issue_lines(import_report))
    return "\n".join(lines)


def _format_independent_report(details: Mapping[str, Any] | None) -> str:
    if details is None:
        return "Selectați o versiune independentă pentru a vedea raportul parserului."
    parser_report = details.get("parser_report")
    if not isinstance(parser_report, Mapping):
        return "Raportul parserului nu este disponibil pentru această versiune."

    parser_status = str(parser_report.get("status", "Nedisponibil"))
    doc_type = str(details.get("document_type", ""))
    lines = [
        f"Document: {_DOCUMENT_LABELS.get(doc_type, doc_type)} · Versiunea {details.get('version_no', '')}",
        f"Proprietar: {details.get('owner_name') or '—'} · Status: {_SET_STATUS_LABELS.get(str(details.get('status')), str(details.get('status')))}",
        f"Status parser: {_IMPORT_STATUS_LABELS.get(parser_status, parser_status)} · Validare: {_TEMPLATE_VALIDATION_LABELS.get(str(details.get('validation_status')), str(details.get('validation_status')))}",
    ]
    variables = details.get("variables")
    if isinstance(variables, (list, tuple)) and variables:
        lines.append("Variabile detectate: " + ", ".join(f"{{{{{key}}}}}" for key in variables))
    else:
        lines.append("Variabile detectate: Nicio variabilă dinamică.")

    if parser_status == "valid_with_warnings":
        confirmed = bool(details.get("warnings_confirmed"))
        lines.append("Avertismente confirmate: " + ("Da" if confirmed else "Nu"))
    else:
        lines.append("Avertismente confirmate: Nu se aplică.")
    lines.append(
        "Verificare identificatori pacient: "
        + ("Confirmată" if details.get("patient_identification_reviewed") else "Neconfirmată")
    )
    lines.extend(_format_import_issue_lines(parser_report))

    validation_issues = details.get("validation_issues")
    if isinstance(validation_issues, (list, tuple)) and validation_issues:
        lines.append("")
        lines.append("Probleme de validare:")
        for issue in validation_issues:
            if isinstance(issue, Mapping):
                lines.append(f"• [{issue.get('code', '')}] {issue.get('message', '')}")
    return "\n".join(lines)


def _rich_document_has_content(document: Any) -> bool:
    if not isinstance(document, RichDocument):
        return False
    return any(
        isinstance(inline, LineBreakInline)
        or isinstance(inline, TextInline) and bool(inline.text)
        for paragraph in document.paragraphs
        for inline in paragraph.inlines
    )


def _set_readonly_text(widget: tk.Text, value: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value)
    widget.configure(state="disabled")


def _context_choice_label(record: Mapping[str, Any]) -> str:
    inactive = " (inactiv)" if not bool(record["is_active"]) else ""
    return f"{record['name']}{inactive} · ID {record['id']}"


def _validation_summary(validation: Any) -> str:
    if not isinstance(validation, Mapping):
        return "Nedisponibilă"
    status = _VALIDATION_STATUS_LABELS.get(
        str(validation.get("status")), "Necunoscută"
    )
    can_activate = validation.get("can_activate")
    if isinstance(can_activate, bool):
        activation = "activare permisă" if can_activate else "activare blocată"
        return f"{status} · {activation}"
    return status


def _format_validation_issues(issues: Any) -> str:
    """Prezintă integral problemele deja calculate de serviciul backend."""

    if not isinstance(issues, (list, tuple)) or not issues:
        return "Nu există probleme de validare raportate."

    lines: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            lines.append("Pereche/context · problemă de validare fără detalii structurate.")
            continue
        document_type = issue.get("document_type")
        category = (
            _DOCUMENT_LABELS.get(str(document_type), str(document_type))
            if document_type
            else "Pereche/context"
        )
        variable_key = issue.get("variable_key")
        token = f" · {{{{{variable_key}}}}}" if variable_key else ""
        code = str(issue.get("code", "cod indisponibil"))
        message = str(issue.get("message", "Mesaj indisponibil."))
        lines.append(f"{category}{token} · [{code}] {message}")
    return "\n".join(lines)


def _format_validation_report(validation: Any) -> str:
    if not isinstance(validation, Mapping):
        return "Setul nu a fost validat încă."
    issues = validation.get("issues")
    if isinstance(issues, (list, tuple)) and issues:
        return _format_validation_issues(issues)
    if validation.get("status") == "valid" and validation.get("can_activate") is True:
        return "Nu au fost găsite probleme. Activarea este permisă."
    if validation.get("status") == "blocked":
        return "Validarea a blocat activarea, dar raportul nu conține detalii."
    return "Setul nu a fost validat încă."


def _template_set_row_values(record: Mapping[str, Any]) -> tuple[str, ...]:
    validation = _validation_summary(record.get("validation"))
    return (
        str(record["version_no"]),
        _SET_STATUS_LABELS.get(str(record["status"]), str(record["status"])),
        validation,
        str(record["document_count"]),
        str(record.get("created_at") or ""),
        str(record.get("activated_at") or ""),
    )


def _document_row_values(
    document_type: str,
    template: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    label = _DOCUMENT_LABELS.get(document_type, document_type)
    if template is None:
        return (label, "", "Lipsește", "—", "0", "—", "—")

    report = template.get("validation_report")
    report = report if isinstance(report, Mapping) else {}
    import_report = report.get("import_report")
    import_report = import_report if isinstance(import_report, Mapping) else {}
    variables = import_report.get("variables")
    token_count = len(variables) if isinstance(variables, Mapping) else 0
    warning_confirmation = report.get("warning_confirmation")
    warning_confirmed = (
        "Da"
        if isinstance(warning_confirmation, Mapping)
        and warning_confirmation.get("confirmed") is True
        else "Nu"
        if import_report.get("status") == "valid_with_warnings"
        else "—"
    )
    return (
        label,
        str(template.get("id", "")),
        _IMPORT_STATUS_LABELS.get(
            str(import_report.get("status")), "Nedisponibil"
        ),
        _TEMPLATE_VALIDATION_LABELS.get(
            str(template.get("validation_status")), "Nedisponibil"
        ),
        str(token_count),
        warning_confirmed,
        str(template.get("imported_at") or ""),
    )


class TemplateSetsView(ttk.Frame):
    """Selectoare de context, istoric de seturi și detalii în mod consultare."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
    ) -> None:
        super().__init__(parent, padding=(12, 12))
        self.connection = connection
        self.diagnoses: list[dict[str, Any]] = []
        self.interventions: list[dict[str, Any]] = []
        self.template_set_rows: list[dict[str, Any]] = []
        self.selected_diagnosis_id: int | None = None
        self.selected_intervention_id: int | None = None
        self.selected_template_set_id: int | None = None
        self.selected_template_set: dict[str, Any] | None = None
        self._selected_document_type: str | None = None
        self.document_variables: list[dict[str, Any]] = []
        self._selected_variable_key: str | None = None
        self._mapping_template_id: int | None = None
        self.field_definitions: list[dict[str, Any]] = []
        self._field_definition_ids: list[int] = []
        self._preview_window: tk.Toplevel | None = None
        self._preview_text_widget: tk.Text | None = None
        self._preview_render_state: Any = None
        self._patient_review_set_id: int | None = None
        self._last_validation_input: tuple[int, bool] | None = None
        self._programmatic_patient_review_update = False
        self._patient_review_changed_since_validation = False
        self._template_data_out_of_sync = False
        self._sync_operation_message: str | None = None
        self._sync_target_template_set_id: int | None = None
        self._sync_preferred_document_type: str | None = None
        self._build_content()

    def _build_content(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        context = ttk.LabelFrame(self, text="Context", padding=(10, 8))
        context.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        context.columnconfigure(1, weight=1)
        context.columnconfigure(3, weight=1)

        ttk.Label(context, text="Diagnostic").grid(row=0, column=0, sticky="w")
        self.diagnosis_combo = ttk.Combobox(context, state="readonly", width=32)
        self.diagnosis_combo.grid(row=0, column=1, sticky="ew", padx=(8, 18))
        self.diagnosis_combo.bind(
            "<<ComboboxSelected>>", self._on_diagnosis_selected
        )

        ttk.Label(context, text="Intervenție").grid(row=0, column=2, sticky="w")
        self.intervention_combo = ttk.Combobox(
            context,
            state="disabled",
            width=36,
        )
        self.intervention_combo.grid(row=0, column=3, sticky="ew", padx=(8, 12))
        self.intervention_combo.bind(
            "<<ComboboxSelected>>", self._on_intervention_selected
        )
        self.new_set_button = ttk.Button(
            context,
            text="Set nou",
            command=self._create_new_set,
            state="disabled",
        )
        self.new_set_button.grid(row=0, column=4, sticky="e")

        content = ttk.Frame(self)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=2, uniform="template-set-panels")
        content.columnconfigure(1, weight=3, uniform="template-set-panels")
        content.rowconfigure(0, weight=1)

        list_panel = ttk.LabelFrame(
            content,
            text="Versiuni și statusuri",
            padding=(8, 8),
        )
        list_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        list_panel.columnconfigure(0, weight=1)
        list_panel.rowconfigure(0, weight=1)
        self.template_set_tree = ttk.Treeview(
            list_panel,
            columns=("version", "status", "validation", "documents", "created", "activated"),
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in (
            ("version", "Versiune", 65),
            ("status", "Status", 78),
            ("validation", "Validare", 150),
            ("documents", "Doc.", 48),
            ("created", "Creat", 155),
            ("activated", "Activat", 155),
        ):
            self.template_set_tree.heading(column, text=heading)
            self.template_set_tree.column(
                column,
                width=width,
                anchor="w",
                stretch=column == "validation",
            )
        self.template_set_tree.grid(row=0, column=0, sticky="nsew")
        set_scroll = ttk.Scrollbar(
            list_panel,
            orient="vertical",
            command=self.template_set_tree.yview,
        )
        set_scroll.grid(row=0, column=1, sticky="ns")
        self.template_set_tree.configure(yscrollcommand=set_scroll.set)
        self.template_set_tree.bind(
            "<<TreeviewSelect>>", self._on_template_set_selected
        )

        details_panel = ttk.LabelFrame(
            content,
            text="Detalii set selectat",
            padding=(10, 8),
        )
        details_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        details_panel.columnconfigure(0, weight=1)
        details_panel.rowconfigure(2, weight=1)
        self.detail_summary_var = tk.StringVar(
            master=self,
            value="Selectați un set pentru a consulta detaliile.",
        )
        ttk.Label(
            details_panel,
            textvariable=self.detail_summary_var,
            justify="left",
            wraplength=520,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.validation_summary_var = tk.StringVar(master=self, value="Validare: —")
        ttk.Label(
            details_panel,
            textvariable=self.validation_summary_var,
            justify="left",
            wraplength=520,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 8))

        self.details_notebook = ttk.Notebook(details_panel)
        self.details_notebook.grid(row=2, column=0, sticky="nsew")
        documents_tab = ttk.Frame(self.details_notebook, padding=(6, 6))
        documents_tab.columnconfigure(0, weight=1)
        documents_tab.rowconfigure(0, weight=1)
        documents_tab.rowconfigure(1, weight=1)
        mapping_tab = ttk.Frame(self.details_notebook, padding=(6, 6))
        mapping_tab.columnconfigure(0, weight=1)
        mapping_tab.rowconfigure(1, weight=1)
        validation_tab = ttk.Frame(self.details_notebook, padding=(10, 8))
        validation_tab.columnconfigure(0, weight=1)
        validation_tab.rowconfigure(2, weight=1)
        independent_tab = ttk.Frame(self.details_notebook, padding=(8, 8))
        independent_tab.columnconfigure(0, weight=1)
        independent_tab.rowconfigure(2, weight=1)
        self.details_notebook.add(documents_tab, text="Documente și parser")
        self.details_notebook.add(mapping_tab, text="Mapare tokenuri")
        self.details_notebook.add(validation_tab, text="Validare și activare")
        self.details_notebook.add(independent_tab, text="Șabloane independente (V3)")
        self._independent_tab = independent_tab

        self.validation_set_status_var = tk.StringVar(
            master=self,
            value="Status set: —",
        )
        ttk.Label(
            validation_tab,
            textvariable=self.validation_set_status_var,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(
            validation_tab,
            textvariable=self.validation_summary_var,
            justify="left",
            wraplength=720,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 6))

        validation_report_panel = ttk.LabelFrame(
            validation_tab,
            text="Probleme raportate de validare",
            padding=(6, 6),
        )
        validation_report_panel.grid(row=2, column=0, sticky="nsew")
        validation_report_panel.columnconfigure(0, weight=1)
        validation_report_panel.rowconfigure(0, weight=1)
        self.validation_issues_text = tk.Text(
            validation_report_panel,
            height=8,
            wrap="word",
            state="disabled",
        )
        self.validation_issues_text.grid(row=0, column=0, sticky="nsew")
        validation_scroll = ttk.Scrollbar(
            validation_report_panel,
            orient="vertical",
            command=self.validation_issues_text.yview,
        )
        validation_scroll.grid(row=0, column=1, sticky="ns")
        self.validation_issues_text.configure(yscrollcommand=validation_scroll.set)

        self.patient_review_var = tk.BooleanVar(master=self, value=False)
        self.patient_review_check = ttk.Checkbutton(
            validation_tab,
            text="Am verificat ambele șabloane pentru nume și identificatori de pacienți.",
            variable=self.patient_review_var,
            command=self._update_validation_actions,
            state="disabled",
        )
        self.patient_review_check.grid(
            row=3,
            column=0,
            sticky="w",
            pady=(8, 2),
        )
        ttk.Label(
            validation_tab,
            text=(
                "Aceasta este o atestare manuală. Aplicația nu detectează automat "
                "nume sau identificatori în fișierele DOCX. Confirmarea este separată "
                "de revizuirea avertismentelor parserului pentru fiecare document."
            ),
            justify="left",
            wraplength=720,
        ).grid(row=4, column=0, sticky="ew", pady=(0, 8))
        validation_actions = ttk.Frame(validation_tab)
        validation_actions.grid(row=5, column=0, sticky="ew")
        self.validate_set_button = ttk.Button(
            validation_actions,
            text="Validează setul",
            command=self._validate_selected_set,
            state="disabled",
        )
        self.validate_set_button.pack(side="left", padx=(0, 6))
        self.activate_set_button = ttk.Button(
            validation_actions,
            text="Activează perechea",
            command=self._activate_selected_set,
            state="disabled",
        )
        self.activate_set_button.pack(side="left")
        self.patient_review_var.trace_add(
            "write",
            self._on_patient_review_changed,
        )

        document_panel = ttk.LabelFrame(
            documents_tab,
            text="Documente importate",
            padding=(6, 6),
        )
        document_panel.grid(row=0, column=0, sticky="nsew")
        document_panel.columnconfigure(0, weight=1)
        document_panel.rowconfigure(0, weight=1)
        self.document_tree = ttk.Treeview(
            document_panel,
            columns=("document", "template_id", "import", "validation", "tokens", "warnings", "imported"),
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in (
            ("document", "Document", 155),
            ("template_id", "ID", 45),
            ("import", "Import", 105),
            ("validation", "Validare", 85),
            ("tokens", "Variabile", 62),
            ("warnings", "Avertismente", 100),
            ("imported", "Importat", 155),
        ):
            self.document_tree.heading(column, text=heading)
            self.document_tree.column(
                column,
                width=width,
                anchor="w",
                stretch=column == "document",
            )
        self.document_tree.grid(row=0, column=0, sticky="nsew")
        self.document_tree.bind(
            "<<TreeviewSelect>>", self._on_document_selected
        )
        document_scroll = ttk.Scrollbar(
            document_panel,
            orient="vertical",
            command=self.document_tree.yview,
        )
        document_scroll.grid(row=0, column=1, sticky="ns")
        self.document_tree.configure(yscrollcommand=document_scroll.set)

        document_actions = ttk.Frame(document_panel)
        document_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.import_document_button = ttk.Button(
            document_actions,
            text="Importă",
            command=self._import_selected_template,
            state="disabled",
        )
        self.import_document_button.pack(side="left", padx=(0, 6))
        self.import_pair_button = ttk.Button(
            document_actions,
            text="Importă perechea",
            command=self._import_template_pair,
            state="disabled",
        )
        self.import_pair_button.pack(side="left", padx=(0, 6))
        self.confirm_warnings_button = ttk.Button(
            document_actions,
            text="Confirmă avertismentele",
            command=self._confirm_selected_warnings,
            state="disabled",
        )
        self.confirm_warnings_button.pack(side="left", padx=(0, 6))
        self.preview_button = ttk.Button(
            document_actions,
            text="Previzualizare",
            command=self._preview_selected_document,
            state="disabled",
        )
        self.preview_button.pack(side="left")

        report_panel = ttk.LabelFrame(
            documents_tab,
            text="Raportul parserului pentru documentul selectat",
            padding=(6, 6),
        )
        report_panel.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        report_panel.columnconfigure(0, weight=1)
        report_panel.rowconfigure(0, weight=1)
        self.parser_report_text = tk.Text(
            report_panel,
            height=8,
            wrap="word",
            state="disabled",
        )
        self.parser_report_text.grid(row=0, column=0, sticky="nsew")
        report_scroll = ttk.Scrollbar(
            report_panel,
            orient="vertical",
            command=self.parser_report_text.yview,
        )
        report_scroll.grid(row=0, column=1, sticky="ns")
        self.parser_report_text.configure(yscrollcommand=report_scroll.set)

        mapping_summary = ttk.Label(
            mapping_tab,
            text=(
                "Tokenurile sunt listate o singură dată pentru documentul selectat. "
                "Maparea și obligativitatea se configurează separat pentru fiecare document."
            ),
            justify="left",
            wraplength=760,
        )
        mapping_summary.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        mapping_content = ttk.Frame(mapping_tab)
        mapping_content.grid(row=1, column=0, sticky="nsew")
        mapping_content.columnconfigure(0, weight=2, uniform="mapping-panels")
        mapping_content.columnconfigure(1, weight=3, uniform="mapping-panels")
        mapping_content.rowconfigure(0, weight=1)

        token_panel = ttk.LabelFrame(
            mapping_content,
            text="Tokenuri detectate",
            padding=(6, 6),
        )
        token_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        token_panel.columnconfigure(0, weight=1)
        token_panel.rowconfigure(0, weight=1)
        self.mapping_tree = ttk.Treeview(
            token_panel,
            columns=("occurrences", "state", "field", "required"),
            show="tree headings",
            selectmode="browse",
        )
        self.mapping_tree.heading("#0", text="Cheie token")
        self.mapping_tree.column("#0", width=150, anchor="w", stretch=True)
        for column, heading, width in (
            ("occurrences", "Apariții", 65),
            ("state", "Stare", 75),
            ("field", "Definiție mapată", 170),
            ("required", "Cerință", 105),
        ):
            self.mapping_tree.heading(column, text=heading)
            self.mapping_tree.column(column, width=width, anchor="w", stretch=False)
        self.mapping_tree.grid(row=0, column=0, sticky="nsew")
        mapping_tree_scroll = ttk.Scrollbar(
            token_panel,
            orient="vertical",
            command=self.mapping_tree.yview,
        )
        mapping_tree_scroll.grid(row=0, column=1, sticky="ns")
        self.mapping_tree.configure(yscrollcommand=mapping_tree_scroll.set)
        self.mapping_tree.bind(
            "<<TreeviewSelect>>", self._on_mapping_token_selected
        )

        mapping_detail = ttk.LabelFrame(
            mapping_content,
            text="Definiția câmpului pentru tokenul selectat",
            padding=(8, 8),
        )
        mapping_detail.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        mapping_detail.columnconfigure(1, weight=1)
        mapping_detail.rowconfigure(4, weight=1)
        self.mapping_token_summary_var = tk.StringVar(
            master=self,
            value="Selectați un token pentru a consulta maparea.",
        )
        ttk.Label(
            mapping_detail,
            textvariable=self.mapping_token_summary_var,
            justify="left",
            wraplength=430,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.mapping_other_document_var = tk.StringVar(master=self, value="")
        ttk.Label(
            mapping_detail,
            textvariable=self.mapping_other_document_var,
            justify="left",
            wraplength=430,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(mapping_detail, text="Definiție exactă").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
        )
        self.field_definition_combo = ttk.Combobox(
            mapping_detail,
            state="disabled",
        )
        self.field_definition_combo.grid(
            row=2, column=1, sticky="ew", pady=(0, 6)
        )
        self.field_definition_combo.bind(
            "<<ComboboxSelected>>", self._on_mapping_definition_selected
        )

        ttk.Label(mapping_detail, text="Obligativitate în acest document").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 6)
        )
        self.required_combo = ttk.Combobox(
            mapping_detail,
            state="disabled",
            values=("Obligatoriu", "Opțional"),
        )
        self.required_combo.grid(row=3, column=1, sticky="ew", pady=(0, 6))
        self.required_combo.bind(
            "<<ComboboxSelected>>", self._on_mapping_requirement_changed
        )

        self.mapping_guidance_var = tk.StringVar(master=self, value="")
        ttk.Label(
            mapping_detail,
            textvariable=self.mapping_guidance_var,
            justify="left",
            wraplength=430,
        ).grid(row=4, column=0, columnspan=2, sticky="new", pady=(0, 6))
        self.mapping_field_summary_var = tk.StringVar(master=self, value="")
        ttk.Label(
            mapping_detail,
            textvariable=self.mapping_field_summary_var,
            justify="left",
            wraplength=430,
        ).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.mapping_options_var = tk.StringVar(master=self, value="")
        ttk.Label(
            mapping_detail,
            textvariable=self.mapping_options_var,
            justify="left",
            wraplength=430,
        ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        mapping_actions = ttk.Frame(mapping_detail)
        mapping_actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.save_mapping_button = ttk.Button(
            mapping_actions,
            text="Salvează maparea",
            command=self._save_selected_mapping,
            state="disabled",
        )
        self.save_mapping_button.pack(side="left", padx=(0, 6))
        self.remove_mapping_button = ttk.Button(
            mapping_actions,
            text="Elimină maparea",
            command=self._remove_selected_mapping,
            state="disabled",
        )
        self.remove_mapping_button.pack(side="left")

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(
            self,
            textvariable=self.status_var,
            style="Muted.TLabel",
            wraplength=1050,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        self._diagnosis_ids: list[int] = []
        self._intervention_ids: list[int] = []
        self._build_independent_tab(self._independent_tab)
        self._update_validation_actions()

    def refresh(self) -> bool:
        """Reîncarcă listele și păstrează selecțiile încă valide."""

        if self._template_data_out_of_sync:
            return self._refresh_after_sync_failure()

        preferred_diagnosis_id = self.selected_diagnosis_id
        preferred_intervention_id = self.selected_intervention_id
        preferred_template_set_id = self.selected_template_set_id
        preferred_document_type = self._selected_document_type
        self.status_var.set("")
        try:
            diagnoses = medical_library.list_diagnoses(
                self.connection,
                active_only=False,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.diagnoses = []
            self._diagnosis_ids = []
            self.diagnosis_combo.configure(values=())
            self.diagnosis_combo.set("")
            self.selected_diagnosis_id = None
            self._clear_interventions()
            self._set_status(f"Diagnosticele nu au putut fi încărcate: {exc}")
            return False

        self.diagnoses = diagnoses
        self._diagnosis_ids = [int(item["id"]) for item in diagnoses]
        self.diagnosis_combo.configure(
            values=tuple(_context_choice_label(item) for item in diagnoses)
        )
        diagnosis_index = (
            self._diagnosis_ids.index(preferred_diagnosis_id)
            if preferred_diagnosis_id in self._diagnosis_ids
            else None
        )
        if diagnosis_index is None:
            self.diagnosis_combo.set("")
            self.selected_diagnosis_id = None
            self._clear_interventions()
            return True

        self.diagnosis_combo.current(diagnosis_index)
        self.selected_diagnosis_id = preferred_diagnosis_id
        return self._load_interventions(
            preferred_intervention_id=preferred_intervention_id,
            preferred_template_set_id=preferred_template_set_id,
            preferred_document_type=preferred_document_type,
        )

    def _load_interventions(
        self,
        *,
        preferred_intervention_id: int | None = None,
        preferred_template_set_id: int | None = None,
        preferred_document_type: str | None = None,
        preloaded_interventions: list[dict[str, Any]] | None = None,
        preloaded_template_sets: list[dict[str, Any]] | None = None,
        preloaded_details: dict[str, Any] | None = None,
    ) -> bool:
        diagnosis_id = self.selected_diagnosis_id
        if diagnosis_id is None:
            self._clear_interventions()
            return True
        if preloaded_interventions is None:
            try:
                interventions = medical_library.list_interventions(
                    self.connection,
                    diagnosis_id=diagnosis_id,
                    active_only=False,
                )
            except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
                self._clear_interventions()
                self._set_status(f"Intervențiile nu au putut fi încărcate: {exc}")
                return False
        else:
            interventions = preloaded_interventions

        self.interventions = interventions
        self._intervention_ids = [int(item["id"]) for item in interventions]
        self.intervention_combo.configure(
            values=tuple(_context_choice_label(item) for item in interventions),
            state="readonly" if interventions else "disabled",
        )
        if preferred_intervention_id in self._intervention_ids:
            intervention_index = self._intervention_ids.index(
                preferred_intervention_id
            )
            self.intervention_combo.current(intervention_index)
            self.selected_intervention_id = preferred_intervention_id
            self._update_new_set_state()
            loaded = self._load_template_sets(
                preferred_template_set_id=preferred_template_set_id,
                preferred_document_type=preferred_document_type,
                preloaded_rows=preloaded_template_sets,
                preloaded_details=preloaded_details,
            )
            self._load_independent_templates()
            return loaded

        self.intervention_combo.set("")
        self.selected_intervention_id = None
        self._clear_template_sets()
        self._update_new_set_state()
        self._load_independent_templates()
        return True

    def _clear_interventions(self) -> None:
        self.interventions = []
        self._intervention_ids = []
        self.intervention_combo.configure(values=(), state="disabled")
        self.intervention_combo.set("")
        self.selected_intervention_id = None
        self._clear_template_sets()
        self._update_new_set_state()
        self._load_independent_templates()

    def _on_diagnosis_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        index = self.diagnosis_combo.current()
        if index < 0 or index >= len(self._diagnosis_ids):
            self.selected_diagnosis_id = None
            self._clear_interventions()
            return
        self.selected_diagnosis_id = self._diagnosis_ids[index]
        self._clear_interventions()
        self._load_interventions()

    def _on_intervention_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        index = self.intervention_combo.current()
        if index < 0 or index >= len(self._intervention_ids):
            self.selected_intervention_id = None
            self._clear_template_sets()
            self._update_new_set_state()
            self._load_independent_templates()
            return
        self.selected_intervention_id = self._intervention_ids[index]
        self._clear_template_sets()
        self._load_template_sets()
        self._update_new_set_state()
        self._load_independent_templates()

    def _load_template_sets(
        self,
        *,
        preferred_template_set_id: int | None = None,
        preferred_document_type: str | None = None,
        preloaded_rows: list[dict[str, Any]] | None = None,
        preloaded_details: dict[str, Any] | None = None,
    ) -> bool:
        intervention_id = self.selected_intervention_id
        if intervention_id is None:
            self._clear_template_sets()
            return True
        if preloaded_rows is None:
            try:
                rows = template_sets.list_template_sets(
                    self.connection,
                    intervention_id=intervention_id,
                )
            except (template_sets.TemplateSetError, sqlite3.Error) as exc:
                self._clear_template_sets()
                self._set_status(f"Seturile de șabloane nu au putut fi încărcate: {exc}")
                return False
        else:
            rows = preloaded_rows

        self.template_set_rows = rows
        for item in self.template_set_tree.get_children():
            self.template_set_tree.delete(item)
        available_ids: list[int] = []
        for record in rows:
            set_id = int(record["id"])
            available_ids.append(set_id)
            self.template_set_tree.insert(
                "",
                "end",
                iid=str(set_id),
                values=_template_set_row_values(record),
            )

        if preferred_template_set_id in available_ids:
            self.template_set_tree.selection_set(str(preferred_template_set_id))
            self.template_set_tree.focus(str(preferred_template_set_id))
            self.template_set_tree.see(str(preferred_template_set_id))
            self.selected_template_set_id = preferred_template_set_id
            return self._load_details(
                preferred_template_set_id,
                preferred_document_type=preferred_document_type,
                preloaded_details=preloaded_details,
            )

        self._clear_details()
        return True

    def _clear_template_sets(self) -> None:
        self.template_set_rows = []
        for item in self.template_set_tree.get_children():
            self.template_set_tree.delete(item)
        self._clear_details()

    def _on_template_set_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        selection = self.template_set_tree.selection()
        if not selection:
            self._clear_details()
            return
        try:
            template_set_id = int(selection[0])
        except ValueError:
            self._clear_details()
            return
        self._load_details(template_set_id)

    def _load_details(
        self,
        template_set_id: int,
        *,
        preferred_document_type: str | None = None,
        preloaded_details: dict[str, Any] | None = None,
    ) -> bool:
        if preloaded_details is None:
            try:
                details = template_sets.get_template_set_details(
                    self.connection,
                    template_set_id,
                )
            except (template_sets.TemplateSetError, sqlite3.Error) as exc:
                self._clear_details()
                self.template_set_tree.selection_remove(
                    *self.template_set_tree.selection()
                )
                self._set_status(f"Detaliile setului nu au putut fi încărcate: {exc}")
                return False
        else:
            details = preloaded_details

        self.selected_template_set_id = template_set_id
        if self._patient_review_set_id != template_set_id:
            self._set_patient_review_programmatically(False)
            self._patient_review_set_id = template_set_id
            self._last_validation_input = None
            self._patient_review_changed_since_validation = False
        self.selected_template_set = details
        summary = next(
            (
                item
                for item in self.template_set_rows
                if int(item["id"]) == template_set_id
            ),
            {},
        )
        diagnosis_state = "activ" if details.get("diagnosis_is_active") else "inactiv"
        intervention_state = (
            "activ" if details.get("intervention_is_active") else "inactiv"
        )
        self.detail_summary_var.set(
            f"ID set: {details['id']} · versiunea {details['version_no']} · "
            f"status: {_SET_STATUS_LABELS.get(str(details['status']), details['status'])}\n"
            f"Diagnostic: {summary.get('diagnosis_name', '—')} ({diagnosis_state})\n"
            f"Intervenție: {summary.get('intervention_name', '—')} ({intervention_state})"
        )

        package_report = details.get("import_report")
        package_report = package_report if isinstance(package_report, Mapping) else {}
        validation = package_report.get("validation")
        self.validation_summary_var.set(
            f"Ultima validare: {_validation_summary(validation)}"
        )
        self.validation_set_status_var.set(
            f"Status set: {_SET_STATUS_LABELS.get(str(details.get('status')), str(details.get('status', 'Necunoscut')))}"
        )
        _set_readonly_text(
            self.validation_issues_text,
            _format_validation_report(validation),
        )

        templates = details.get("templates")
        templates_by_type = {
            str(item["document_type"]): item
            for item in templates
            if isinstance(item, Mapping) and "document_type" in item
        } if isinstance(templates, list) else {}
        for item in self.document_tree.get_children():
            self.document_tree.delete(item)
        for document_type in (
            template_sets.EPICRISIS_RECOMMENDATIONS,
            template_sets.OPERATIVE_PROTOCOL,
        ):
            template = templates_by_type.get(document_type)
            self.document_tree.insert(
                "",
                "end",
                iid=document_type,
                values=_document_row_values(document_type, template),
            )
        document_types = self.document_tree.get_children()
        target_document_type = (
            preferred_document_type
            if preferred_document_type in document_types
            else self._selected_document_type
            if self._selected_document_type in document_types
            else document_types[0]
            if document_types
            else None
        )
        if target_document_type is not None:
            self.document_tree.selection_set(target_document_type)
            self._show_document_report(target_document_type)
        else:
            self._selected_document_type = None
            _set_readonly_text(self.parser_report_text, "Nu există documente în set.")
        self._update_document_actions()
        return True

    def _clear_details(self) -> None:
        self.selected_template_set_id = None
        self.selected_template_set = None
        self._selected_document_type = None
        self._patient_review_set_id = None
        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        self._set_patient_review_programmatically(False)
        self.detail_summary_var.set("Selectați un set pentru a consulta detaliile.")
        self.validation_summary_var.set("Validare: —")
        self.validation_set_status_var.set("Status set: —")
        _set_readonly_text(
            self.validation_issues_text,
            "Selectați un set pentru a consulta raportul de validare.",
        )
        for item in self.document_tree.get_children():
            self.document_tree.delete(item)
        self.document_tree.selection_remove(*self.document_tree.selection())
        _set_readonly_text(
            self.parser_report_text,
            "Selectați un set pentru a consulta rapoartele parserului.",
        )
        self._clear_mapping_details()
        self._update_document_actions()

    def _on_patient_review_changed(self, *_args: Any) -> None:
        if (
            not self._programmatic_patient_review_update
            and self._last_validation_input is not None
        ):
            self._patient_review_changed_since_validation = True
        self._update_validation_actions()

    def _set_patient_review_programmatically(self, value: bool) -> None:
        self._programmatic_patient_review_update = True
        try:
            self.patient_review_var.set(value)
        finally:
            self._programmatic_patient_review_update = False

    def _set_template_data_out_of_sync(self, out_of_sync: bool) -> None:
        self._template_data_out_of_sync = out_of_sync
        if out_of_sync:
            self.diagnosis_combo.configure(state="disabled")
            self.intervention_combo.configure(state="disabled")
            self.template_set_tree.state(["disabled"])
        else:
            self.diagnosis_combo.configure(state="readonly")
            self.intervention_combo.configure(
                state="readonly" if self.interventions else "disabled"
            )
            self.template_set_tree.state(["!disabled"])
        self._update_new_set_state()
        self._update_document_actions()
        self._update_mapping_controls()
        self._update_validation_actions()

    def _mutation_blocked_while_out_of_sync(self) -> bool:
        """Block writes until a successful refresh confirms the displayed context."""

        return self._template_data_out_of_sync

    def _update_validation_actions(self) -> None:
        draft = bool(
            self.selected_template_set is not None
            and self.selected_template_set.get("status") == "draft"
        )
        self.patient_review_check.configure(
            state=(
                "normal"
                if draft and not self._template_data_out_of_sync
                else "disabled"
            )
        )
        self.validate_set_button.configure(
            state=(
                "normal"
                if draft and not self._template_data_out_of_sync
                else "disabled"
            )
        )

        package_report = (
            self.selected_template_set.get("import_report")
            if self.selected_template_set is not None
            else None
        )
        package_report = package_report if isinstance(package_report, Mapping) else {}
        validation = package_report.get("validation")
        review = package_report.get("patient_identification_review")
        selected_id = (
            int(self.selected_template_set["id"])
            if self.selected_template_set is not None
            else None
        )
        current_review_input = (selected_id, bool(self.patient_review_var.get()))
        context_is_active = bool(
            self.selected_template_set is not None
            and self.selected_template_set.get("diagnosis_is_active")
            and self.selected_template_set.get("intervention_is_active")
        )
        can_activate = bool(
            draft
            and context_is_active
            and not self._template_data_out_of_sync
            and not self._patient_review_changed_since_validation
            and self._last_validation_input == current_review_input
            and isinstance(validation, Mapping)
            and validation.get("status") == "valid"
            and validation.get("can_activate") is True
            and bool(self.patient_review_var.get())
            and isinstance(review, Mapping)
            and review.get("confirmed") is True
        )
        self.activate_set_button.configure(
            state="normal" if can_activate else "disabled"
        )

    def _show_validation_result(self, result: Any) -> None:
        report = getattr(result, "report", None)
        validation = report.get("validation") if isinstance(report, Mapping) else None
        if self.selected_template_set is not None and isinstance(report, Mapping):
            self.selected_template_set["import_report"] = dict(report)
            self.validation_set_status_var.set(
                "Status set: "
                + _SET_STATUS_LABELS.get(
                    str(self.selected_template_set.get("status")),
                    str(self.selected_template_set.get("status", "Necunoscut")),
                )
            )
        if not isinstance(validation, Mapping):
            validation = {
                "status": getattr(result, "status", "blocked"),
                "can_activate": getattr(result, "can_activate", False),
                "issues": getattr(result, "issues", ()),
            }
        self.validation_summary_var.set(
            f"Ultima validare: {_validation_summary(validation)}"
        )
        result_status = getattr(result, "status", None)
        if result_status == "active":
            self.validation_set_status_var.set("Status set: Activ")
            if self.selected_template_set is not None:
                self.selected_template_set["status"] = "active"
        elif self.selected_template_set is not None:
            current_status = str(self.selected_template_set.get("status", "draft"))
            self.validation_set_status_var.set(
                "Status set: "
                + _SET_STATUS_LABELS.get(current_status, current_status)
            )
        _set_readonly_text(
            self.validation_issues_text,
            _format_validation_report(validation),
        )
        self._update_validation_actions()

    def _validate_selected_set(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        template_set_id = self._selected_draft_id()
        if template_set_id is None:
            return
        confirmed = bool(self.patient_review_var.get())
        try:
            result = template_sets.validate_template_set(
                self.connection,
                template_set_id,
                patient_identification_review_confirmed=confirmed,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Setul nu a putut fi validat: {exc}")
            return

        self._last_validation_input = (template_set_id, confirmed)
        self._patient_review_changed_since_validation = False
        self._show_validation_result(result)
        success_message = (
            "Validarea setului s-a încheiat. "
            + (
                "Activarea este permisă."
                if result.can_activate
                else "Activarea este blocată; consultați toate problemele raportate."
            )
        )
        self._reload_after_successful_operation(
            template_set_id,
            self._selected_document_type,
            success_message=success_message,
        )

    def _activate_selected_set(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        template_set_id = self._selected_draft_id()
        if template_set_id is None:
            return
        if str(self.activate_set_button.cget("state")) == "disabled":
            self._set_status(
                "Activați numai după o validare reușită cu atestarea identificatorilor confirmată."
            )
            return
        confirmed = bool(self.patient_review_var.get())
        try:
            result = template_sets.activate_template_set(
                self.connection,
                template_set_id,
                patient_identification_review_confirmed=confirmed,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Perechea nu a putut fi activată: {exc}")
            return

        self._last_validation_input = (template_set_id, confirmed)
        self._patient_review_changed_since_validation = False
        self._show_validation_result(result)
        if not result.can_activate:
            self._reload_after_successful_operation(
                template_set_id,
                self._selected_document_type,
                success_message=(
                    "Verificarea activării s-a încheiat, dar activarea a fost blocată. "
                    "Consultați toate problemele raportate."
                ),
            )
            return

        self._reload_after_successful_operation(
            template_set_id,
            self._selected_document_type,
            success_message=(
                f"Setul ID {template_set_id} a fost activat. "
                "Versiunea activă anterioară a fost retrasă automat."
            ),
        )

    def _update_new_set_state(self) -> None:
        diagnosis = next(
            (
                item
                for item in self.diagnoses
                if int(item["id"]) == self.selected_diagnosis_id
            ),
            None,
        )
        intervention = next(
            (
                item
                for item in self.interventions
                if int(item["id"]) == self.selected_intervention_id
            ),
            None,
        )
        allowed = bool(
            not self._template_data_out_of_sync
            and diagnosis is not None
            and intervention is not None
            and bool(diagnosis["is_active"])
            and bool(intervention["is_active"])
            and int(intervention["diagnosis_id"]) == int(diagnosis["id"])
        )
        self.new_set_button.configure(state="normal" if allowed else "disabled")

    def _create_new_set(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        self._update_new_set_state()
        if str(self.new_set_button.cget("state")) == "disabled":
            self._set_status(
                "Un set nou poate fi creat numai pentru un diagnostic și o intervenție active asociate corect."
            )
            return
        diagnosis = next(
            item
            for item in self.diagnoses
            if int(item["id"]) == self.selected_diagnosis_id
        )
        intervention = next(
            item
            for item in self.interventions
            if int(item["id"]) == self.selected_intervention_id
        )
        if not messagebox.askyesno(
            "Set nou de șabloane",
            "Creați un draft nou pentru:\n"
            f"{diagnosis['name']} — {intervention['name']}?",
            parent=self,
        ):
            return
        try:
            template_set_id = template_sets.create_template_set_draft(
                self.connection,
                int(diagnosis["id"]),
                int(intervention["id"]),
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Draftul nu a putut fi creat: {exc}")
            return

        self._load_template_sets(preferred_template_set_id=template_set_id)
        record = next(
            (
                item
                for item in self.template_set_rows
                if int(item["id"]) == template_set_id
            ),
            None,
        )
        if record is None:
            self._set_status(f"Draft creat cu ID {template_set_id}.")
            return
        self.status_var.set(
            f"Draft creat: ID {template_set_id}, versiunea {record['version_no']}."
        )

    def _template_for_document(self, document_type: str | None) -> Mapping[str, Any] | None:
        if document_type is None or self.selected_template_set is None:
            return None
        templates = self.selected_template_set.get("templates")
        if not isinstance(templates, list):
            return None
        return next(
            (
                item
                for item in templates
                if isinstance(item, Mapping)
                and item.get("document_type") == document_type
            ),
            None,
        )

    def _show_document_report(self, document_type: str) -> None:
        self._selected_document_type = document_type
        template = self._template_for_document(document_type)
        text = _format_import_report(template)
        report = template.get("validation_report") if template is not None else None
        report = report if isinstance(report, Mapping) else {}
        import_report = report.get("import_report")
        import_report = import_report if isinstance(import_report, Mapping) else {}
        if import_report.get("status") == "rejected":
            text += (
                "\n\nPrevizualizarea este indisponibilă deoarece parserul a respins "
                "documentul. Raportul de mai sus rămâne disponibil."
            )
        _set_readonly_text(self.parser_report_text, text)
        self._load_document_variables(
            preferred_variable_key=self._selected_variable_key
        )

    def _on_document_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        selection = self.document_tree.selection()
        if not selection:
            self._selected_document_type = None
            _set_readonly_text(
                self.parser_report_text,
                "Selectați un document pentru a consulta raportul parserului.",
            )
            self._clear_mapping_details()
            self._update_document_actions()
            return
        document_type = str(selection[0])
        self._show_document_report(document_type)
        self._update_document_actions()

    def _clear_mapping_details(self) -> None:
        self.document_variables = []
        self._selected_variable_key = None
        self._mapping_template_id = None
        self.field_definitions = []
        self._field_definition_ids = []
        for item in self.mapping_tree.get_children():
            self.mapping_tree.delete(item)
        self.mapping_tree.selection_remove(*self.mapping_tree.selection())
        self.mapping_token_summary_var.set(
            "Selectați un token pentru a consulta maparea."
        )
        self.mapping_other_document_var.set("")
        self.mapping_guidance_var.set("")
        self.mapping_field_summary_var.set("")
        self.mapping_options_var.set("")
        self.field_definition_combo.configure(values=(), state="disabled")
        self.field_definition_combo.set("")
        self.required_combo.set("")
        self.required_combo.configure(state="disabled")
        self._update_mapping_controls()

    def _load_document_variables(
        self,
        *,
        preferred_variable_key: str | None = None,
    ) -> bool:
        template = self._template_for_document(self._selected_document_type)
        if template is None:
            self._clear_mapping_details()
            self.mapping_guidance_var.set(
                "Importați documentul pentru a consulta și mapa tokenurile sale."
            )
            return True

        try:
            variables = template_sets.list_document_variables(
                self.connection,
                int(template["id"]),
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Tokenurile documentului nu au putut fi încărcate: {exc}")
            self._mapping_template_id = None
            self.field_definition_combo.configure(state="disabled")
            self.required_combo.configure(state="disabled")
            self._update_mapping_controls()
            return False

        self._mapping_template_id = int(template["id"])
        self.document_variables = variables
        for item in self.mapping_tree.get_children():
            self.mapping_tree.delete(item)
        variable_by_key = {
            str(item["variable_key"]): item for item in variables
        }
        for key, item in variable_by_key.items():
            mapped = bool(item.get("is_mapped"))
            mapped_label = "Mapat" if mapped else "Nemapat"
            field_label = (
                f"{item.get('field_label') or 'Câmp'} · ID "
                f"{item['field_definition_id']} · v{item['field_version']}"
                if mapped
                else "—"
            )
            required_label = (
                "Obligatoriu"
                if mapped and bool(item.get("is_required"))
                else "Opțional"
                if mapped
                else "—"
            )
            self.mapping_tree.insert(
                "",
                "end",
                iid=key,
                text=key,
                values=(
                    str(item["occurrence_count"]),
                    mapped_label,
                    field_label,
                    required_label,
                ),
            )

        target_key = (
            preferred_variable_key
            if preferred_variable_key in variable_by_key
            else None
        )
        if target_key is None:
            self._selected_variable_key = None
            self.mapping_tree.selection_remove(*self.mapping_tree.selection())
            self._clear_selected_mapping_editor()
            if not variables:
                self.mapping_guidance_var.set(
                    "Documentul nu conține tokenuri detectate de importator."
                )
            return True

        self.mapping_tree.selection_set(target_key)
        self.mapping_tree.focus(target_key)
        self.mapping_tree.see(target_key)
        self._selected_variable_key = target_key
        return self._show_selected_mapping(variable_by_key[target_key])

    def _clear_selected_mapping_editor(self) -> None:
        self._selected_variable_key = None
        self.field_definitions = []
        self._field_definition_ids = []
        self.mapping_token_summary_var.set("Selectați un token pentru a consulta maparea.")
        self.mapping_other_document_var.set("")
        self.mapping_field_summary_var.set("")
        self.mapping_options_var.set("")
        self.mapping_guidance_var.set("")
        self.field_definition_combo.configure(values=(), state="disabled")
        self.field_definition_combo.set("")
        self.required_combo.set("")
        self.required_combo.configure(state="disabled")
        self._update_mapping_controls()

    def _on_mapping_token_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        selection = self.mapping_tree.selection()
        if not selection:
            self._clear_selected_mapping_editor()
            return
        variable_key = str(selection[0])
        row = next(
            (
                item
                for item in self.document_variables
                if item.get("variable_key") == variable_key
            ),
            None,
        )
        if row is None:
            self._clear_selected_mapping_editor()
            return
        self._selected_variable_key = variable_key
        self._show_selected_mapping(row)

    def _mapping_is_editable(self) -> bool:
        template = self._template_for_document(self._selected_document_type)
        if (
            self.selected_template_set is None
            or self.selected_template_set.get("status") != "draft"
            or template is None
            or int(template["id"]) != self._mapping_template_id
        ):
            return False
        report = template.get("validation_report")
        report = report if isinstance(report, Mapping) else {}
        import_report = report.get("import_report")
        import_report = import_report if isinstance(import_report, Mapping) else {}
        return import_report.get("status") != "rejected"

    def _field_context_label(self, definition: Mapping[str, Any]) -> str:
        scope = str(definition.get("scope", ""))
        if scope == "common":
            return "Comun"
        row = next(
            (
                item
                for item in self.template_set_rows
                if self.selected_template_set_id is not None
                and int(item["id"]) == self.selected_template_set_id
            ),
            {},
        )
        if scope == "diagnosis":
            return (
                f"{row.get('diagnosis_name', 'Diagnostic')} "
                f"(ID {definition.get('diagnosis_id')})"
            )
        if scope == "intervention":
            return (
                f"{row.get('intervention_name', 'Intervenție')} "
                f"(ID {definition.get('intervention_id')})"
            )
        return "Context necunoscut"

    def _definition_matches_template_context(
        self,
        definition: Mapping[str, Any],
    ) -> bool:
        if self.selected_template_set is None:
            return False
        scope = definition.get("scope")
        if scope == "common":
            return True
        if scope == "diagnosis":
            return definition.get("diagnosis_id") == self.selected_template_set.get(
                "diagnosis_id"
            )
        if scope == "intervention":
            return definition.get("intervention_id") == self.selected_template_set.get(
                "intervention_id"
            )
        return False

    def _definition_choice_label(self, definition: Mapping[str, Any]) -> str:
        version_state = "curentă" if bool(definition.get("is_current")) else "istorică"
        return (
            f"{definition['label']} · ID {definition['id']} · v{definition['version']} · "
            f"{definition['data_type']} · {definition['scope']} · {version_state} · "
            f"{self._field_context_label(definition)}"
        )

    def _show_selected_mapping(self, variable: Mapping[str, Any]) -> bool:
        variable_key = str(variable["variable_key"])
        self._selected_variable_key = variable_key
        mapped = bool(variable.get("is_mapped"))
        self.mapping_token_summary_var.set(
            f"{{{{{variable_key}}}}} · {variable['occurrence_count']} apariție/apariții · "
            + (
                f"mapată la ID {variable['field_definition_id']}, "
                f"versiunea {variable['field_version']} ({variable.get('field_label')})."
                if mapped
                else "fără mapare."
            )
        )
        self.field_definition_combo.configure(values=(), state="disabled")
        self.field_definition_combo.set("")
        self.required_combo.set(
            "Obligatoriu"
            if mapped and bool(variable.get("is_required"))
            else "Opțional"
            if mapped
            else ""
        )
        self.required_combo.configure(state="disabled")
        self._show_mapping_in_other_document(variable_key)

        try:
            all_definitions = medical_library.list_variables(
                self.connection,
                variable_key=variable_key,
                current_only=False,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.mapping_guidance_var.set(
                f"Definițiile câmpului nu au putut fi încărcate: {exc}"
            )
            self.field_definitions = []
            self._field_definition_ids = []
            self.field_definition_combo.configure(values=(), state="disabled")
            self.field_definition_combo.set("")
            self.mapping_field_summary_var.set("")
            self.mapping_options_var.set("")
            self._update_mapping_controls()
            return False

        compatible = [
            item
            for item in all_definitions
            if self._definition_matches_template_context(item)
        ]
        self.field_definitions = compatible
        self._field_definition_ids = [int(item["id"]) for item in compatible]
        editable = self._mapping_is_editable()
        self.field_definition_combo.configure(
            values=tuple(self._definition_choice_label(item) for item in compatible),
            state="readonly" if editable and compatible else "disabled",
        )
        mapped_id = variable.get("field_definition_id") if mapped else None
        if mapped_id in self._field_definition_ids:
            selected_index = self._field_definition_ids.index(int(mapped_id))
            self.field_definition_combo.current(selected_index)
            self._show_definition_metadata(compatible[selected_index])
        else:
            self.field_definition_combo.set("")
            if mapped:
                self.mapping_field_summary_var.set(
                    "Maparea existentă indică o definiție care nu este disponibilă "
                    "în contextul acestui set."
                )
            else:
                self.mapping_field_summary_var.set("")
            self.mapping_options_var.set("")

        if not all_definitions:
            self.mapping_guidance_var.set(
                "Nu există un câmp cu această cheie. Creați un câmp cu aceeași cheie "
                "în fila «Câmpuri medicale», apoi reveniți aici."
            )
        elif not compatible:
            self.mapping_guidance_var.set(
                "Există câmpuri cu această cheie, dar niciunul nu aparține contextului "
                "diagnosticului/intervenției setului."
            )
        else:
            self.mapping_guidance_var.set(
                "Alegeți definiția după ID și versiune. Versiunile istorice rămân "
                "selectabile pentru mapările păstrate în istoric."
            )

        self.required_combo.configure(state="readonly" if editable else "disabled")
        self.required_combo.set(
            "Obligatoriu"
            if mapped and bool(variable.get("is_required"))
            else "Opțional"
            if mapped
            else ""
        )
        self._update_mapping_controls()
        return True

    def _show_definition_metadata(self, definition: Mapping[str, Any]) -> None:
        state = "versiune curentă" if bool(definition.get("is_current")) else "versiune istorică"
        self.mapping_field_summary_var.set(
            f"{definition['label']} · ID {definition['id']} · versiunea "
            f"{definition['version']} · tip {definition['data_type']} · "
            f"scope {definition['scope']} · {self._field_context_label(definition)} · {state}"
        )
        if definition.get("data_type") != "select":
            self.mapping_options_var.set("")
            return
        try:
            options = medical_library.list_field_options(
                self.connection,
                int(definition["id"]),
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self.mapping_options_var.set(f"Opțiunile nu au putut fi încărcate: {exc}")
            return
        if not options:
            self.mapping_options_var.set(
                "Opțiuni pentru această versiune: niciuna configurată."
            )
            return
        option_lines = [
            f"{item['option_label']} (valoare: {item['option_value']})"
            for item in options
        ]
        self.mapping_options_var.set(
            "Opțiuni pentru această versiune:\n" + "\n".join(option_lines)
        )

    def _show_mapping_in_other_document(self, variable_key: str) -> None:
        other_type = (
            template_sets.OPERATIVE_PROTOCOL
            if self._selected_document_type == template_sets.EPICRISIS_RECOMMENDATIONS
            else template_sets.EPICRISIS_RECOMMENDATIONS
        )
        other_template = self._template_for_document(other_type)
        if other_template is None:
            self.mapping_other_document_var.set(
                f"{_DOCUMENT_LABELS[other_type]}: documentul nu este importat."
            )
            return
        try:
            other_variables = template_sets.list_document_variables(
                self.connection,
                int(other_template["id"]),
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self.mapping_other_document_var.set(
                f"Maparea din {_DOCUMENT_LABELS[other_type]} nu a putut fi consultată: {exc}"
            )
            return
        other = next(
            (item for item in other_variables if item["variable_key"] == variable_key),
            None,
        )
        if other is None:
            self.mapping_other_document_var.set(
                f"{_DOCUMENT_LABELS[other_type]}: tokenul nu este prezent."
            )
            return
        if not other.get("is_mapped"):
            self.mapping_other_document_var.set(
                f"{_DOCUMENT_LABELS[other_type]}: token prezent, nemapat."
            )
            return
        current = next(
            (
                item
                for item in self.document_variables
                if item.get("variable_key") == variable_key
            ),
            None,
        )
        incompatible = bool(
            current is not None
            and current.get("is_mapped")
            and (
                current.get("field_definition_id") != other.get("field_definition_id")
                or current.get("field_version") != other.get("field_version")
            )
        )
        prefix = "AVERTISMENT: mapări incompatibile. " if incompatible else ""
        message = (
            f"{prefix}{_DOCUMENT_LABELS[other_type]}: mapată la ID "
            f"{other['field_definition_id']}, versiunea {other['field_version']} "
            f"({other.get('field_label')}); "
            f"{'obligatoriu' if other.get('is_required') else 'opțional'} în acel document."
        )
        if incompatible:
            message += " Perechea nu poate fi activată până când ID-ul și versiunea coincid."
        self.mapping_other_document_var.set(message)

    def _on_mapping_definition_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        index = self.field_definition_combo.current()
        if 0 <= index < len(self.field_definitions):
            self._show_definition_metadata(self.field_definitions[index])
        self._update_mapping_controls()

    def _on_mapping_requirement_changed(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        self._update_mapping_controls()

    def _update_mapping_controls(self) -> None:
        selected = bool(self._selected_variable_key)
        row = next(
            (
                item
                for item in self.document_variables
                if item.get("variable_key") == self._selected_variable_key
            ),
            None,
        )
        editable = (
            not self._template_data_out_of_sync
            and self._mapping_is_editable()
            and row is not None
        )
        self.field_definition_combo.configure(
            state="readonly" if editable and self.field_definitions else "disabled"
        )
        self.required_combo.configure(
            state="readonly" if editable else "disabled"
        )
        has_definition = 0 <= self.field_definition_combo.current() < len(
            self._field_definition_ids
        )
        required_selected = self.required_combo.get() in {
            "Obligatoriu",
            "Opțional",
        }
        self.save_mapping_button.configure(
            state=(
                "normal"
                if editable and selected and has_definition and required_selected
                else "disabled"
            ),
            text="Modifică maparea" if row and row.get("is_mapped") else "Adaugă maparea",
        )
        self.remove_mapping_button.configure(
            state=(
                "normal"
                if editable and row is not None and row.get("is_mapped")
                else "disabled"
            )
        )

    def _save_selected_mapping(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        variable_key = self._selected_variable_key
        template = self._template_for_document(self._selected_document_type)
        row = next(
            (
                item
                for item in self.document_variables
                if item.get("variable_key") == variable_key
            ),
            None,
        )
        index = self.field_definition_combo.current()
        required_choice = self.required_combo.get()
        if (
            not self._mapping_is_editable()
            or template is None
            or row is None
            or index < 0
            or index >= len(self._field_definition_ids)
            or required_choice not in {"Obligatoriu", "Opțional"}
        ):
            self._set_status("Selectați tokenul, definiția exactă și obligativitatea.")
            return
        template_id = int(template["id"])
        field_definition_id = self._field_definition_ids[index]
        is_required = required_choice == "Obligatoriu"
        try:
            if row.get("is_mapped"):
                template_sets.update_variable_mapping(
                    self.connection,
                    template_id,
                    str(variable_key),
                    field_definition_id,
                    is_required=is_required,
                )
            else:
                template_sets.add_variable_mapping(
                    self.connection,
                    template_id,
                    str(variable_key),
                    field_definition_id,
                    is_required=is_required,
                )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Maparea nu a putut fi salvată: {exc}")
            return
        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        if self._reload_selected_set(
            int(self.selected_template_set["id"]),
            self._selected_document_type,
        ):
            self._set_status(
                f"Maparea tokenului {{{{{variable_key}}}}} a fost salvată."
            )

    def _remove_selected_mapping(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        variable_key = self._selected_variable_key
        template = self._template_for_document(self._selected_document_type)
        if (
            not self._mapping_is_editable()
            or template is None
            or variable_key is None
        ):
            self._set_status("Eliminarea mapării este disponibilă numai pentru un document dintr-un draft.")
            return
        template_set_id = int(self.selected_template_set["id"])
        document_type = self._selected_document_type
        try:
            template_sets.remove_variable_mapping(
                self.connection,
                int(template["id"]),
                variable_key,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Maparea nu a putut fi eliminată: {exc}")
            return
        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        if self._reload_selected_set(template_set_id, document_type):
            self._set_status(f"Maparea tokenului {{{{{variable_key}}}}} a fost eliminată.")

    def _update_document_actions(self) -> None:
        template = self._template_for_document(self._selected_document_type)
        draft = bool(
            self.selected_template_set is not None
            and self.selected_template_set.get("status") == "draft"
        )
        editable = draft and not self._template_data_out_of_sync
        self.import_document_button.configure(
            text="Înlocuiește" if template is not None else "Importă",
            state="normal" if editable and self._selected_document_type else "disabled",
        )
        self.import_pair_button.configure(state="normal" if editable else "disabled")

        report = template.get("validation_report") if template is not None else None
        report = report if isinstance(report, Mapping) else {}
        import_report = report.get("import_report")
        import_report = import_report if isinstance(import_report, Mapping) else {}
        confirmation = report.get("warning_confirmation")
        warnings_need_confirmation = (
            import_report.get("status") == "valid_with_warnings"
            and not (
                isinstance(confirmation, Mapping)
                and confirmation.get("confirmed") is True
            )
        )
        self.confirm_warnings_button.configure(
            state=(
                "normal"
                if editable and template is not None and warnings_need_confirmation
                else "disabled"
            )
        )
        self.preview_button.configure(
            state=(
                "normal"
                if not self._template_data_out_of_sync
                and template is not None
                and import_report.get("status") in {"valid", "valid_with_warnings"}
                else "disabled"
            )
        )
        self._update_validation_actions()

    def _selected_draft_id(self) -> int | None:
        if (
            self.selected_template_set is None
            or self.selected_template_set.get("status") != "draft"
        ):
            self._set_status("Operația este disponibilă numai pentru un set draft.")
            return None
        return int(self.selected_template_set["id"])

    def _select_docx_path(self, title: str) -> str:
        return filedialog.askopenfilename(
            parent=self,
            title=title,
            filetypes=(("Documente Word (*.docx)", "*.docx"),),
        )

    def _read_docx_file(self, path: str) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            self._set_status(f"Fișierul DOCX nu a putut fi citit: {exc}")
            return None

    def _reload_selected_set(
        self,
        template_set_id: int,
        document_type: str | None = None,
    ) -> bool:
        reloaded = self._load_template_sets(
            preferred_template_set_id=template_set_id,
            preferred_document_type=document_type,
        )
        if not reloaded:
            return False
        if self.selected_template_set_id != template_set_id:
            self._set_status(
                "Operația s-a încheiat, dar setul nu mai este disponibil în contextul selectat."
            )
            return False
        return True

    def _reload_after_successful_operation(
        self,
        template_set_id: int,
        document_type: str | None,
        *,
        success_message: str,
    ) -> bool:
        self._sync_operation_message = success_message
        self._sync_target_template_set_id = template_set_id
        self._sync_preferred_document_type = document_type
        self._set_template_data_out_of_sync(True)
        refreshed = self._refresh_after_sync_failure()
        if not refreshed:
            return False
        if self.selected_template_set_id != template_set_id:
            self._set_status(
                f"{success_message} Reîncărcarea a reușit, dar setul nu mai este "
                "disponibil în contextul selectat."
            )
            return False
        self._set_status(success_message)
        return True

    def _refresh_after_sync_failure(self) -> bool:
        """Încarcă mai întâi datele; aplică snapshot-ul doar dacă toate citirile reușesc."""

        preferred_diagnosis_id = self.selected_diagnosis_id
        preferred_intervention_id = self.selected_intervention_id
        preferred_template_set_id = (
            self._sync_target_template_set_id
            if self._sync_target_template_set_id is not None
            else self.selected_template_set_id
        )
        preferred_document_type = (
            self._sync_preferred_document_type
            if self._sync_operation_message is not None
            else self._selected_document_type
        )
        failed_stage = "diagnosticelor"

        try:
            diagnoses = medical_library.list_diagnoses(
                self.connection,
                active_only=False,
            )
            diagnosis_ids = [int(item["id"]) for item in diagnoses]
            selected_diagnosis_id = (
                preferred_diagnosis_id
                if preferred_diagnosis_id in diagnosis_ids
                else None
            )

            interventions: list[dict[str, Any]] = []
            selected_intervention_id: int | None = None
            template_set_rows: list[dict[str, Any]] = []
            selected_template_set_id: int | None = None
            selected_details: dict[str, Any] | None = None

            if selected_diagnosis_id is not None:
                failed_stage = "intervențiilor"
                interventions = medical_library.list_interventions(
                    self.connection,
                    diagnosis_id=selected_diagnosis_id,
                    active_only=False,
                )
                intervention_ids = [int(item["id"]) for item in interventions]
                if preferred_intervention_id in intervention_ids:
                    selected_intervention_id = preferred_intervention_id
                    failed_stage = "seturilor de șabloane"
                    template_set_rows = template_sets.list_template_sets(
                        self.connection,
                        intervention_id=selected_intervention_id,
                    )
                    template_set_ids = [
                        int(item["id"]) for item in template_set_rows
                    ]
                    if preferred_template_set_id in template_set_ids:
                        selected_template_set_id = preferred_template_set_id
                        failed_stage = "detaliilor setului"
                        selected_details = template_sets.get_template_set_details(
                            self.connection,
                            selected_template_set_id,
                        )
        except (
            medical_library.MedicalLibraryError,
            template_sets.TemplateSetError,
            sqlite3.Error,
        ) as exc:
            self._set_template_data_out_of_sync(True)
            if self._sync_operation_message is not None:
                message = (
                    f"{self._sync_operation_message} Reîncărcarea {failed_stage} "
                    f"a eșuat: {exc}. Datele afișate pot fi învechite; "
                    "validarea și activarea sunt dezactivate până la o reîmprospătare reușită."
                )
            else:
                message = (
                    f"Reîmprospătarea {failed_stage} a eșuat: {exc}. "
                    "Datele afișate pot fi învechite; validarea și activarea "
                    "rămân dezactivate până la o reîmprospătare reușită."
                )
            self._set_status(message)
            return False

        self.status_var.set("")
        self.diagnoses = diagnoses
        self._diagnosis_ids = [int(item["id"]) for item in diagnoses]
        self.diagnosis_combo.configure(
            values=tuple(_context_choice_label(item) for item in diagnoses)
        )
        if selected_diagnosis_id is None:
            self.diagnosis_combo.set("")
            self.selected_diagnosis_id = None
            self._clear_interventions()
        else:
            self.diagnosis_combo.current(
                self._diagnosis_ids.index(selected_diagnosis_id)
            )
            self.selected_diagnosis_id = selected_diagnosis_id
            self._load_interventions(
                preferred_intervention_id=selected_intervention_id,
                preferred_template_set_id=selected_template_set_id,
                preferred_document_type=preferred_document_type,
                preloaded_interventions=interventions,
                preloaded_template_sets=(
                    template_set_rows
                    if selected_intervention_id is not None
                    else None
                ),
                preloaded_details=selected_details,
            )

        completed_message = self._sync_operation_message
        target_template_set_id = self._sync_target_template_set_id
        operation_remains_selected = (
            target_template_set_id is None
            or self.selected_template_set_id == target_template_set_id
        )
        self._set_template_data_out_of_sync(False)
        self._load_independent_templates()
        self._sync_operation_message = None
        self._sync_target_template_set_id = None
        self._sync_preferred_document_type = None
        if completed_message is not None and not operation_remains_selected:
            self._set_status(
                f"{completed_message} Reîncărcarea a reușit, dar setul nu mai este "
                "disponibil în contextul selectat."
            )
        elif completed_message is not None:
            self._set_status(completed_message)
        else:
            self._set_status("Datele au fost reîmprospătate.")
        return True

    def _import_selected_template(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        template_set_id = self._selected_draft_id()
        document_type = self._selected_document_type
        if template_set_id is None or document_type not in _DOCUMENT_LABELS:
            if template_set_id is not None:
                self._set_status("Selectați unul dintre documentele setului.")
            return

        existing = self._template_for_document(document_type)
        if existing is not None and not messagebox.askyesno(
            "Înlocuiește documentul",
            f"Documentul «{_DOCUMENT_LABELS[document_type]}» există deja. Îl înlocuiți?",
            parent=self,
        ):
            return

        path = self._select_docx_path(
            f"Selectați {_DOCUMENT_LABELS[document_type]} (.docx)"
        )
        if not path:
            return
        document_bytes = self._read_docx_file(path)
        if document_bytes is None:
            return
        try:
            template_sets.import_template(
                self.connection,
                template_set_id,
                document_type,
                document_bytes,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Documentul nu a putut fi importat: {exc}")
            return

        self._set_patient_review_programmatically(False)
        self._patient_review_set_id = template_set_id
        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        if self._reload_selected_set(template_set_id, document_type):
            template = self._template_for_document(document_type)
            report = template.get("validation_report") if template else None
            import_report = (
                report.get("import_report") if isinstance(report, Mapping) else None
            )
            status = (
                import_report.get("status")
                if isinstance(import_report, Mapping)
                else "Nedisponibil"
            )
            self._set_status(
                f"Importul documentului s-a încheiat. Status parser: {status}."
            )

    def _import_template_pair(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        template_set_id = self._selected_draft_id()
        if template_set_id is None:
            return
        existing_templates = self.selected_template_set.get("templates", [])
        if isinstance(existing_templates, list) and existing_templates:
            if not messagebox.askyesno(
                "Înlocuiește perechea de documente",
                "Setul conține deja cel puțin un document. Importați perechea nouă "
                "și înlocuiți documentele existente?",
                parent=self,
            ):
                return

        paths: dict[str, str] = {}
        for document_type in (
            template_sets.EPICRISIS_RECOMMENDATIONS,
            template_sets.OPERATIVE_PROTOCOL,
        ):
            path = self._select_docx_path(
                f"Selectați {_DOCUMENT_LABELS[document_type]} (.docx)"
            )
            if not path:
                return
            paths[document_type] = path

        documents: dict[str, bytes] = {}
        for document_type, path in paths.items():
            document_bytes = self._read_docx_file(path)
            if document_bytes is None:
                return
            documents[document_type] = document_bytes

        try:
            template_sets.import_template_pair(
                self.connection,
                template_set_id,
                documents,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Perechea de documente nu a putut fi importată: {exc}")
            return

        self._set_patient_review_programmatically(False)
        self._patient_review_set_id = template_set_id
        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        if self._reload_selected_set(template_set_id, self._selected_document_type):
            self._set_status("Importul atomic al perechii de documente s-a încheiat.")

    def _confirm_selected_warnings(self) -> None:
        if self._mutation_blocked_while_out_of_sync():
            return
        template_set_id = self._selected_draft_id()
        template = self._template_for_document(self._selected_document_type)
        if template_set_id is None or template is None:
            return
        template_id = int(template["id"])
        document_type = str(template["document_type"])
        if not messagebox.askyesno(
            "Confirmarea avertismentelor",
            "Confirmați că ați revizuit avertismentele parserului pentru "
            f"«{_DOCUMENT_LABELS.get(document_type, document_type)}»? Aceasta "
            "confirmă revizuirea avertismentelor, nu aprobarea setului pentru activare.",
            parent=self,
        ):
            return
        try:
            template_sets.confirm_template_warnings(
                self.connection,
                template_id,
                confirmed=True,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Avertismentele nu au putut fi confirmate: {exc}")
            return

        self._last_validation_input = None
        self._patient_review_changed_since_validation = False
        if self._reload_selected_set(template_set_id, document_type):
            self._set_status(
                "Avertismentele au fost confirmate pentru documentul selectat; "
                "setul nu este prin aceasta aprobat pentru activare."
            )

    def _preview_selected_document(self) -> tk.Toplevel | None:
        if self._template_data_out_of_sync:
            return None
        template = self._template_for_document(self._selected_document_type)
        if template is None:
            self._set_status("Selectați un document importat pentru previzualizare.")
            return None
        report = template.get("validation_report")
        report = report if isinstance(report, Mapping) else {}
        import_report = report.get("import_report")
        import_report = import_report if isinstance(import_report, Mapping) else {}
        if import_report.get("status") == "rejected":
            self._set_status(
                "Previzualizarea nu este disponibilă: parserul a respins documentul. "
                "Raportul parserului rămâne afișat."
            )
            return None

        template_id = int(template["id"])
        try:
            content = template_sets.get_template_content(
                self.connection,
                template_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Conținutul documentului nu a putut fi încărcat: {exc}")
            return None
        document = content.get("document")
        if not _rich_document_has_content(document):
            self._set_status(
                "Previzualizarea nu este disponibilă: conținutul parsat este gol. "
                "Consultați în continuare raportul parserului."
            )
            return None

        document_type = str(template.get("document_type", ""))
        return self._open_preview_window(
            title=f"Previzualizare — {_DOCUMENT_LABELS.get(document_type, document_type)}",
            document=document,
        )

    def _open_preview_window(
        self,
        *,
        title: str,
        document: RichDocument,
    ) -> tk.Toplevel | None:
        window = tk.Toplevel(self)
        apply_window_icon(window)
        window.title(title)
        window.geometry("820x620")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        preview = tk.Text(window, wrap="word", undo=False, state="disabled")
        preview.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=10)
        preview_scroll = ttk.Scrollbar(
            window,
            orient="vertical",
            command=preview.yview,
        )
        preview_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=10)
        preview.configure(yscrollcommand=preview_scroll.set)
        try:
            self._preview_render_state = render_rich_document(preview, document)
        except (TypeError, ValueError, tk.TclError) as exc:
            window.destroy()
            self._set_status(f"Previzualizarea nu a putut fi afișată: {exc}")
            return None
        preview.configure(state="disabled")
        self._preview_window = window
        self._preview_text_widget = preview
        self._set_status("Previzualizarea este read-only; șablonul original nu a fost modificat.")
        return window

    def _build_independent_tab(self, parent: ttk.Frame) -> None:
        self.independent_owner_type_var = tk.StringVar(master=self, value="diagnosis")
        self.independent_rows: list[dict[str, Any]] = []
        self.selected_independent_version_id: int | None = None
        self.independent_patient_review_var = tk.BooleanVar(master=self, value=False)
        self._independent_patient_review_programmatic = False
        self.independent_summary_var = tk.StringVar(
            master=self,
            value="Selectați un diagnostic sau o intervenție.",
        )

        controls = ttk.Frame(parent)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.independent_diagnosis_radio = ttk.Radiobutton(
            controls,
            text="Epicriză + Recomandări (Diagnostic)",
            value="diagnosis",
            variable=self.independent_owner_type_var,
            command=self._on_independent_owner_changed,
        )
        self.independent_diagnosis_radio.pack(side="left", padx=(0, 10))
        self.independent_intervention_radio = ttk.Radiobutton(
            controls,
            text="Protocol operator (Intervenție)",
            value="intervention",
            variable=self.independent_owner_type_var,
            command=self._on_independent_owner_changed,
        )
        self.independent_intervention_radio.pack(side="left", padx=(0, 12))
        self.import_independent_epicrisis_button = ttk.Button(
            controls,
            text="Importă Epicriză (.docx)",
            command=lambda: self._import_independent_docx("diagnosis"),
            state="disabled",
        )
        self.import_independent_epicrisis_button.pack(side="left", padx=(0, 6))
        self.import_independent_protocol_button = ttk.Button(
            controls,
            text="Importă Protocol (.docx)",
            command=lambda: self._import_independent_docx("intervention"),
            state="disabled",
        )
        self.import_independent_protocol_button.pack(side="left")

        ttk.Label(
            parent,
            textvariable=self.independent_summary_var,
            justify="left",
            wraplength=700,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 6))

        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.independent_tree = ttk.Treeview(
            tree_frame,
            columns=("version", "status", "validation", "warnings", "patient_review", "variables"),
            show="headings",
            selectmode="browse",
            height=5,
        )
        for column, heading, width in (
            ("version", "Ver.", 50),
            ("status", "Status", 75),
            ("validation", "Validare", 95),
            ("warnings", "Avertismente", 95),
            ("patient_review", "Review pacient", 105),
            ("variables", "Variabile", 180),
        ):
            self.independent_tree.heading(column, text=heading)
            self.independent_tree.column(
                column,
                width=width,
                anchor="w",
                stretch=column == "variables",
            )
        self.independent_tree.grid(row=0, column=0, sticky="nsew")
        ind_scroll = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self.independent_tree.yview,
        )
        ind_scroll.grid(row=0, column=1, sticky="ns")
        self.independent_tree.configure(yscrollcommand=ind_scroll.set)
        self.independent_tree.bind(
            "<<TreeviewSelect>>",
            self._on_independent_version_selected,
        )

        report_frame = ttk.LabelFrame(
            parent,
            text="Raport parser și validare versiune independentă",
            padding=(6, 6),
        )
        report_frame.grid(row=3, column=0, sticky="ew", pady=(6, 6))
        report_frame.columnconfigure(0, weight=1)
        self.independent_report_text = tk.Text(
            report_frame,
            height=7,
            wrap="word",
            state="disabled",
        )
        self.independent_report_text.grid(row=0, column=0, sticky="ew")
        rep_scroll = ttk.Scrollbar(
            report_frame,
            orient="vertical",
            command=self.independent_report_text.yview,
        )
        rep_scroll.grid(row=0, column=1, sticky="ns")
        self.independent_report_text.configure(yscrollcommand=rep_scroll.set)

        self.independent_patient_review_check = ttk.Checkbutton(
            parent,
            text="Confirm că șablonul selectat nu conține nume sau identificatori de pacienți.",
            variable=self.independent_patient_review_var,
            command=self._on_independent_patient_review_toggled,
            state="disabled",
        )
        self.independent_patient_review_check.grid(
            row=4,
            column=0,
            sticky="w",
            pady=(0, 6),
        )

        actions = ttk.Frame(parent)
        actions.grid(row=5, column=0, sticky="ew")
        self.confirm_independent_warnings_button = ttk.Button(
            actions,
            text="Confirmă avertismente",
            command=self._confirm_independent_warnings,
            state="disabled",
        )
        self.confirm_independent_warnings_button.pack(side="left", padx=(0, 6))
        self.validate_independent_button = ttk.Button(
            actions,
            text="Validează",
            command=self._validate_independent_version,
            state="disabled",
        )
        self.validate_independent_button.pack(side="left", padx=(0, 6))
        self.activate_independent_button = ttk.Button(
            actions,
            text="Activează",
            command=self._activate_independent_version,
            state="disabled",
        )
        self.activate_independent_button.pack(side="left", padx=(0, 6))
        self.reject_independent_button = ttk.Button(
            actions,
            text="Respinge draft",
            command=self._reject_independent_version,
            state="disabled",
        )
        self.reject_independent_button.pack(side="left", padx=(0, 6))
        self.retire_independent_button = ttk.Button(
            actions,
            text="Retrage activ",
            command=self._retire_independent_version,
            state="disabled",
        )
        self.retire_independent_button.pack(side="left", padx=(0, 6))
        self.preview_independent_button = ttk.Button(
            actions,
            text="Previzualizare",
            command=self._preview_independent_version,
            state="disabled",
        )
        self.preview_independent_button.pack(side="left")

    def _current_independent_owner(self) -> tuple[str, int | None]:
        owner_type = self.independent_owner_type_var.get()
        if owner_type == "diagnosis":
            return "diagnosis", self.selected_diagnosis_id
        return "intervention", self.selected_intervention_id

    def _selected_independent_row(self) -> dict[str, Any] | None:
        version_id = self.selected_independent_version_id
        if version_id is None:
            return None
        return next(
            (row for row in self.independent_rows if int(row["id"]) == version_id),
            None,
        )

    def _set_independent_patient_review_programmatically(self, value: bool) -> None:
        self._independent_patient_review_programmatic = True
        try:
            self.independent_patient_review_var.set(value)
        finally:
            self._independent_patient_review_programmatic = False

    def _on_independent_owner_changed(self) -> None:
        self._load_independent_templates()

    def _load_independent_templates(
        self,
        *,
        preferred_version_id: int | None = None,
    ) -> None:
        self.import_independent_epicrisis_button.configure(
            state="normal" if self.selected_diagnosis_id is not None else "disabled"
        )
        self.import_independent_protocol_button.configure(
            state="normal" if self.selected_intervention_id is not None else "disabled"
        )
        owner_type, owner_id = self._current_independent_owner()
        for item_id in self.independent_tree.get_children():
            self.independent_tree.delete(item_id)
        self.independent_rows = []
        target_id = (
            preferred_version_id
            if preferred_version_id is not None
            else self.selected_independent_version_id
        )
        self.selected_independent_version_id = None

        if owner_id is None:
            owner_label = "un diagnostic" if owner_type == "diagnosis" else "o intervenție"
            self.independent_summary_var.set(
                f"Selectați {owner_label} pentru a gestiona versiunile independente."
            )
            self._set_independent_patient_review_programmatically(False)
            _set_readonly_text(
                self.independent_report_text,
                _format_independent_report(None),
            )
            self._update_independent_actions()
            return

        try:
            rows = template_sets.list_independent_templates(
                self.connection,
                owner_type=owner_type,
                owner_id=owner_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self.independent_summary_var.set(
                f"Versiunile independente nu au putut fi încărcate: {exc}"
            )
            _set_readonly_text(self.independent_report_text, str(exc))
            self._update_independent_actions()
            return

        self.independent_rows = rows
        active_row = next((row for row in rows if row.get("status") == "active"), None)
        doc_label = (
            "Epicriză + Recomandări"
            if owner_type == "diagnosis"
            else "Protocol operator"
        )
        active_text = (
            f"activă: v{active_row['version_no']} (ID {active_row['id']})"
            if active_row is not None
            else "fără versiune activă"
        )
        self.independent_summary_var.set(
            f"{doc_label} · {len(rows)} versiuni · {active_text}"
        )

        for row in rows:
            parser_status = row["parser_report"].get("status")
            warnings_cell = (
                "Confirmate"
                if row["warnings_confirmed"]
                else ("Necesară" if parser_status == "valid_with_warnings" else "—")
            )
            review_cell = "Da" if row["patient_identification_reviewed"] else "Nu"
            variables_cell = ", ".join(row.get("variables") or ()) or "—"
            self.independent_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    f"v{row['version_no']}",
                    _SET_STATUS_LABELS.get(str(row["status"]), str(row["status"])),
                    _TEMPLATE_VALIDATION_LABELS.get(
                        str(row["validation_status"]),
                        str(row["validation_status"]),
                    ),
                    warnings_cell,
                    review_cell,
                    variables_cell,
                ),
            )

        selected_row = None
        if target_id is not None:
            selected_row = next(
                (row for row in rows if int(row["id"]) == target_id),
                None,
            )
        if selected_row is None and rows:
            selected_row = active_row if active_row is not None else rows[0]

        if selected_row is not None:
            iid = str(selected_row["id"])
            self.selected_independent_version_id = int(selected_row["id"])
            self.independent_tree.selection_set(iid)
            self.independent_tree.focus(iid)
        self._render_selected_independent_version()

    def _on_independent_version_selected(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        selection = self.independent_tree.selection()
        if not selection:
            self.selected_independent_version_id = None
        else:
            try:
                self.selected_independent_version_id = int(selection[0])
            except ValueError:
                self.selected_independent_version_id = None
        self._render_selected_independent_version()

    def _render_selected_independent_version(
        self,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        row = dict(details) if details is not None else self._selected_independent_row()
        if row is None:
            self._set_independent_patient_review_programmatically(False)
            _set_readonly_text(
                self.independent_report_text,
                _format_independent_report(None),
            )
            self._update_independent_actions()
            return

        self._set_independent_patient_review_programmatically(
            bool(row.get("patient_identification_reviewed"))
        )
        _set_readonly_text(
            self.independent_report_text,
            _format_independent_report(row),
        )
        self._update_independent_actions(row)

    def _update_independent_actions(
        self,
        selected: Mapping[str, Any] | None = None,
    ) -> None:
        row = selected if selected is not None else self._selected_independent_row()
        if row is None:
            self.independent_patient_review_check.configure(state="disabled")
            self.confirm_independent_warnings_button.configure(state="disabled")
            self.validate_independent_button.configure(state="disabled")
            self.activate_independent_button.configure(state="disabled")
            self.reject_independent_button.configure(state="disabled")
            self.retire_independent_button.configure(state="disabled")
            self.preview_independent_button.configure(state="disabled")
            return

        status = str(row.get("status", ""))
        is_draft = status == "draft"
        is_active = status == "active"
        parser_report = row.get("parser_report")
        parser_status = (
            parser_report.get("status")
            if isinstance(parser_report, Mapping)
            else None
        )
        can_confirm_warnings = (
            is_draft
            and parser_status == "valid_with_warnings"
            and not bool(row.get("warnings_confirmed"))
        )
        can_activate = (
            is_draft
            and str(row.get("validation_status")) == "valid"
            and bool(row.get("patient_identification_reviewed"))
            and (
                parser_status == "valid"
                or (
                    parser_status == "valid_with_warnings"
                    and bool(row.get("warnings_confirmed"))
                )
            )
        )
        self.independent_patient_review_check.configure(
            state="normal" if is_draft else "disabled"
        )
        self.confirm_independent_warnings_button.configure(
            state="normal" if can_confirm_warnings else "disabled"
        )
        self.validate_independent_button.configure(
            state="normal" if is_draft else "disabled"
        )
        self.activate_independent_button.configure(
            state="normal" if can_activate else "disabled"
        )
        self.reject_independent_button.configure(
            state="normal" if is_draft else "disabled"
        )
        self.retire_independent_button.configure(
            state="normal" if is_active else "disabled"
        )
        self.preview_independent_button.configure(
            state="normal" if parser_status != "rejected" else "disabled"
        )

    def _import_independent_docx(
        self,
        owner_type: str,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        owner_id = (
            self.selected_diagnosis_id
            if owner_type == "diagnosis"
            else self.selected_intervention_id
        )
        if owner_id is None:
            self._set_status(
                "Selectați proprietarul (diagnostic sau intervenție) înainte de import."
            )
            return None

        title = (
            "Selectați Epicriză + Recomandări (.docx)"
            if owner_type == "diagnosis"
            else "Selectați Protocol operator (.docx)"
        )
        selected_path = path if path is not None else self._select_docx_path(title)
        if not selected_path:
            return None
        document_bytes = self._read_docx_file(selected_path)
        if document_bytes is None:
            return None

        try:
            created = template_sets.import_independent_template(
                self.connection,
                owner_type=owner_type,
                owner_id=owner_id,
                document_bytes=document_bytes,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Șablonul independent nu a putut fi importat: {exc}")
            return None

        self.independent_owner_type_var.set(owner_type)
        self._load_independent_templates(preferred_version_id=int(created["id"]))
        self._set_status(
            f"Versiunea v{created['version_no']} a fost importată ca draft (ID {created['id']})."
        )
        return created

    def _on_independent_patient_review_toggled(self) -> None:
        if self._independent_patient_review_programmatic:
            return
        self._confirm_independent_patient_review(
            self.selected_independent_version_id,
            bool(self.independent_patient_review_var.get()),
        )

    def _confirm_independent_patient_review(
        self,
        template_version_id: int | None = None,
        confirmed: bool = True,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            updated = template_sets.confirm_independent_template_patient_review(
                self.connection,
                target_id,
                confirmed=confirmed,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Confirmarea identificatorilor a eșuat: {exc}")
            self._load_independent_templates(preferred_version_id=target_id)
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._set_status(
            "Confirmarea absenței datelor de pacient a fost actualizată."
        )
        return updated

    def _confirm_independent_warnings(
        self,
        template_version_id: int | None = None,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            updated = template_sets.confirm_independent_template_warnings(
                self.connection,
                target_id,
                confirmed=True,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Confirmarea avertismentelor a eșuat: {exc}")
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._set_status("Avertismentele versiunii independente au fost confirmate.")
        return updated

    def _validate_independent_version(
        self,
        template_version_id: int | None = None,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            result = template_sets.validate_independent_template(
                self.connection,
                target_id,
                patient_identification_review_confirmed=bool(
                    self.independent_patient_review_var.get()
                ),
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Validarea versiunii independente a eșuat: {exc}")
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._render_selected_independent_version(result)
        if result.get("can_activate"):
            self._set_status(f"Versiunea v{result['version_no']} este validă și pregătită pentru activare.")
        else:
            issues = "; ".join(
                str(issue.get("message", ""))
                for issue in result.get("validation_issues", ())
            )
            self._set_status(f"Validare blocată: {issues}")
        return result

    def _activate_independent_version(
        self,
        template_version_id: int | None = None,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            activated = template_sets.activate_independent_template(
                self.connection,
                target_id,
                patient_identification_review_confirmed=bool(
                    self.independent_patient_review_var.get()
                ),
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Activarea versiunii independente a eșuat: {exc}")
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._set_status(
            f"Versiunea v{activated['version_no']} a fost activată (versiunea anterioară a fost retrasă)."
        )
        return activated

    def _reject_independent_version(
        self,
        template_version_id: int | None = None,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            rejected = template_sets.reject_independent_template(
                self.connection,
                target_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Respingerea draftului a eșuat: {exc}")
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._set_status(f"Versiunea v{rejected['version_no']} a fost respinsă.")
        return rejected

    def _retire_independent_version(
        self,
        template_version_id: int | None = None,
    ) -> dict[str, Any] | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            retired = template_sets.retire_independent_template(
                self.connection,
                target_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Retragerea versiunii a eșuat: {exc}")
            return None

        self._load_independent_templates(preferred_version_id=target_id)
        self._set_status(f"Versiunea activă v{retired['version_no']} a fost retrasă.")
        return retired

    def _preview_independent_version(
        self,
        template_version_id: int | None = None,
    ) -> tk.Toplevel | None:
        target_id = (
            template_version_id
            if template_version_id is not None
            else self.selected_independent_version_id
        )
        if target_id is None:
            return None
        try:
            content = template_sets.get_independent_template_content(
                self.connection,
                target_id,
            )
        except (template_sets.TemplateSetError, sqlite3.Error) as exc:
            self._set_status(f"Conținutul versiunii nu a putut fi încărcat: {exc}")
            return None
        document = content.get("document")
        if not _rich_document_has_content(document):
            self._set_status(
                "Previzualizarea nu este disponibilă: conținutul parsat este gol."
            )
            return None
        assert isinstance(document, RichDocument)
        row = self._selected_independent_row() or {}
        document_type = str(row.get("document_type", ""))
        return self._open_preview_window(
            title=f"Previzualizare V3 — {_DOCUMENT_LABELS.get(document_type, document_type)}",
            document=document,
        )

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)


__all__ = ["TemplateSetsView"]
