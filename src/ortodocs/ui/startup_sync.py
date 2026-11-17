"""Flux interactiv pentru sincronizarea automată a template-urilor DOCX la pornire și din Setări."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from ortodocs.services.template_sync import (
    PATIENT_CONFIRMATION_TEXT,
    SyncOutcome,
    SyncScanReport,
    TemplateSyncService,
)
from ortodocs.ui.theme import CardFrame, style_text_widget, style_toplevel


_SYNC_SCHEMA_TABLES = (
    "app_settings",
    "template_sync_entities",
    "template_sync_sources",
    "template_sync_runs",
)


def has_sync_schema(connection: sqlite3.Connection) -> bool:
    """Verifică dacă schema V4 de sincronizare există în conexiunea SQLite curentă."""

    placeholders = ", ".join("?" for _ in _SYNC_SCHEMA_TABLES)
    rows = connection.execute(
        f"""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name IN ({placeholders})
        """,
        _SYNC_SCHEMA_TABLES,
    ).fetchall()
    existing = {str(row[0]) for row in rows}
    return existing == set(_SYNC_SCHEMA_TABLES)


def _is_viewable(widget: object) -> bool:
    if not isinstance(widget, tk.Misc):
        return False
    try:
        return bool(widget.winfo_exists() and widget.winfo_viewable())
    except tk.TclError:
        return False


def _effective_messagebox_parent(parent: object) -> tk.Misc | None:
    if _is_viewable(parent):
        assert isinstance(parent, tk.Misc)
        return parent
    return None


def _center_window(window: tk.Toplevel) -> None:
    try:
        window.update_idletasks()
        width = max(window.winfo_reqwidth(), window.winfo_width(), 420)
        height = max(window.winfo_reqheight(), window.winfo_height(), 160)
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        pos_x = max(0, (screen_width - width) // 2)
        pos_y = max(0, (screen_height - height) // 2)
        window.geometry(f"+{pos_x}+{pos_y}")
    except tk.TclError:
        pass


def _prepare_toplevel(window: tk.Toplevel, parent: object) -> None:
    if _is_viewable(parent):
        assert isinstance(parent, tk.Misc)
        window.transient(parent)
    _center_window(window)
    window.deiconify()
    window.lift()


class _SyncProgressWindow(tk.Toplevel):
    """Fereastră simplă de stare afișată pe durata scanării/aplicării sincronizării."""

    def __init__(self, parent: tk.Misc, message: str) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Sincronizare template-uri DOCX")
        style_toplevel(self)
        self.resizable(False, False)

        container = ttk.Frame(self, style="App.TFrame", padding=(22, 18))
        container.pack(fill="both", expand=True)

        self.message_var = tk.StringVar(value=message)
        ttk.Label(
            container,
            textvariable=self.message_var,
            style="TLabel",
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.progressbar = ttk.Progressbar(container, mode="indeterminate", length=360)
        self.progressbar.pack(fill="x")
        self.progressbar.start(12)

        self.protocol("WM_DELETE_WINDOW", lambda: None)
        _prepare_toplevel(self, parent)
        self.update()

    def set_message(self, message: str) -> None:
        try:
            self.message_var.set(message)
            self.update()
        except tk.TclError:
            pass

    def close(self) -> None:
        try:
            self.progressbar.stop()
        except tk.TclError:
            pass
        try:
            if self.winfo_exists():
                self.destroy()
        except tk.TclError:
            pass


class BatchSyncConfirmationDialog(tk.Toplevel):
    """Modal unic de confirmare pentru lotul de documente DOCX noi sau modificate."""

    def __init__(
        self,
        parent: tk.Misc,
        scan_report: SyncScanReport,
    ) -> None:
        super().__init__(parent)
        self.withdraw()
        self.title("Confirmare actualizare template-uri DOCX")
        style_toplevel(self)
        self.minsize(640, 480)
        self.confirmed = False

        container = ttk.Frame(self, style="App.TFrame", padding=(22, 18))
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        ttk.Label(
            container,
            text="Au fost detectate șabloane DOCX noi sau modificate",
            style="CardTitle.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        summary_text = (
            f"Folder: {scan_report.folder_path}\n"
            f"Scanate: {scan_report.scanned_count}   |   "
            f"Noi: {scan_report.new_count}   |   "
            f"Modificate: {scan_report.modified_count}   |   "
            f"Neschimbate: {scan_report.unchanged_count}"
        )
        ttk.Label(
            container,
            text=summary_text,
            style="Muted.TLabel",
            justify="left",
            wraplength=700,
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        list_card = CardFrame(container, padding=(14, 12))
        list_card.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        list_frame = list_card.body
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(1, weight=1)
        ttk.Label(
            list_frame,
            text="Documente DOCX care vor fi importate și activate",
            style="CardFieldLabel.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.items_text = tk.Text(
            list_frame,
            height=10,
            wrap="word",
            relief="flat",
            borderwidth=0,
        )
        style_text_widget(self.items_text, subtle=True)
        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.items_text.yview,
        )
        self.items_text.configure(yscrollcommand=scrollbar.set)
        self.items_text.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        item_lines: list[str] = []
        for item in scan_report.pending_items():
            kind_label = "Nou" if item.change_kind == "new" else "Modificat"
            doc_label = (
                "Epicriză + Recomandări"
                if item.document_type == "epicrisis_recommendations"
                else "Protocol operator"
            )
            item_lines.append(
                f"• [{kind_label}] {item.relative_path} — {item.owner_name} ({doc_label})"
            )
        self.items_text.insert("1.0", "\n".join(item_lines))
        self.items_text.configure(state="disabled")

        declaration_card = CardFrame(container, padding=(14, 12))
        declaration_card.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        declaration_frame = declaration_card.body
        declaration_frame.columnconfigure(0, weight=1)
        ttk.Label(
            declaration_frame,
            text="Confirmare protecția datelor",
            style="CardFieldLabel.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.confirmation_label = ttk.Label(
            declaration_frame,
            text=PATIENT_CONFIRMATION_TEXT,
            style="Card.TLabel",
            wraplength=700,
            justify="left",
        )
        self.confirmation_label.grid(row=1, column=0, sticky="w")

        buttons = ttk.Frame(container, style="App.TFrame")
        buttons.grid(row=4, column=0, sticky="e")

        self.cancel_button = ttk.Button(
            buttons,
            text="Anulează",
            style="Secondary.TButton",
            command=self._on_cancel,
        )
        self.cancel_button.pack(side="left", padx=(0, 10))

        self.confirm_button = ttk.Button(
            buttons,
            text="Confirm și actualizează",
            style="Primary.TButton",
            command=self._on_confirm,
        )
        self.confirm_button.pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _event: self._on_cancel())

        _prepare_toplevel(self, parent)
        self.grab_set()
        self.focus_force()

    def _on_confirm(self) -> None:
        self.confirmed = True
        self._close()

    def _on_cancel(self) -> None:
        self.confirmed = False
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if self.winfo_exists():
            self.destroy()


def ask_batch_sync_confirmation(
    parent: tk.Misc,
    scan_report: SyncScanReport,
) -> bool:
    """Deschide modalul de confirmare în lot și așteaptă răspunsul utilizatorului."""

    dialog = BatchSyncConfirmationDialog(parent, scan_report)
    parent.wait_window(dialog)
    return bool(dialog.confirmed)


def _messagebox_show(
    kind: str,
    title: str,
    message: str,
    *,
    parent: object,
) -> None:
    effective_parent = _effective_messagebox_parent(parent)
    fn = messagebox.showerror if kind == "error" else messagebox.showinfo
    if effective_parent is not None:
        fn(title, message, parent=effective_parent)
    else:
        fn(title, message)


def execute_interactive_sync(
    parent: object,
    connection: sqlite3.Connection,
    *,
    database_path: str | Path,
    folder_path: str | Path | None = None,
    show_progress: bool = True,
    report_no_changes: bool = False,
    report_not_configured: bool = False,
    report_cancelled: bool = False,
    confirm_callback: Callable[[SyncScanReport], bool] | None = None,
    show_dialogs: bool = True,
) -> SyncOutcome:
    """Execută fluxul complet de sincronizare cu feedback vizual și confirmare unică."""

    if not has_sync_schema(connection):
        return SyncOutcome(
            status="not_configured",
            folder_path=None,
            summary_message="Schema de sincronizare nu este inițializată.",
        )

    progress_window: _SyncProgressWindow | None = None
    try:
        service = TemplateSyncService(connection, database_path=database_path)
        configured_folder = (
            Path(folder_path) if folder_path is not None else service.get_configured_folder()
        )
        if configured_folder is None:
            outcome = SyncOutcome(
                status="not_configured",
                folder_path=None,
                summary_message=(
                    "Nu este configurat niciun folder pentru template-urile DOCX.\n"
                    "Configurați un folder în secțiunea Setări -> Template-uri DOCX."
                ),
            )
            if report_not_configured and show_dialogs:
                _messagebox_show(
                    "info",
                    "Sincronizare template-uri DOCX",
                    outcome.summary_message,
                    parent=parent,
                )
            return outcome

        if show_progress and isinstance(parent, tk.Misc):
            progress_window = _SyncProgressWindow(
                parent,
                "Se scanează și se validează template-urile DOCX...",
            )

        try:
            scan_report = service.scan_and_validate(folder_path=configured_folder)
        finally:
            if progress_window is not None:
                progress_window.close()
                progress_window = None

        if scan_report.has_blocking_issues:
            outcome = service.apply_sync(
                scan_report,
                patient_identification_confirmed=False,
            )
            if show_dialogs:
                _messagebox_show(
                    "error",
                    "Sincronizare template-uri DOCX",
                    outcome.summary_message,
                    parent=parent,
                )
            return outcome

        if not scan_report.has_changes and not scan_report.has_pending_links:
            outcome = service.apply_sync(
                scan_report,
                patient_identification_confirmed=False,
            )
            if report_no_changes and show_dialogs:
                _messagebox_show(
                    "info",
                    "Sincronizare template-uri DOCX",
                    outcome.summary_message,
                    parent=parent,
                )
            return outcome

        if not scan_report.has_changes and scan_report.has_pending_links:
            if show_progress and isinstance(parent, tk.Misc):
                progress_window = _SyncProgressWindow(
                    parent,
                    "Se creează backup-ul și se asociază sursele DOCX cu versiunile existente...",
                )
            try:
                outcome = service.apply_sync(
                    scan_report,
                    patient_identification_confirmed=False,
                )
            finally:
                if progress_window is not None:
                    progress_window.close()
                    progress_window = None
            if show_dialogs:
                kind = "info" if outcome.status == "applied" else "error"
                _messagebox_show(
                    kind,
                    "Sincronizare template-uri DOCX",
                    outcome.summary_message,
                    parent=parent,
                )
            return outcome

        if confirm_callback is not None:
            confirmed = bool(confirm_callback(scan_report))
        elif isinstance(parent, tk.Misc):
            confirmed = ask_batch_sync_confirmation(parent, scan_report)
        else:
            confirmed = False

        if not confirmed:
            outcome = service.apply_sync(
                scan_report,
                patient_identification_confirmed=False,
            )
            if report_cancelled and show_dialogs:
                _messagebox_show(
                    "info",
                    "Sincronizare template-uri DOCX",
                    outcome.summary_message,
                    parent=parent,
                )
            return outcome

        if show_progress and isinstance(parent, tk.Misc):
            progress_window = _SyncProgressWindow(
                parent,
                "Se creează backup-ul bazei de date și se aplică șabloanele DOCX...",
            )

        try:
            outcome = service.apply_sync(
                scan_report,
                patient_identification_confirmed=True,
            )
        finally:
            if progress_window is not None:
                progress_window.close()
                progress_window = None

        if show_dialogs:
            kind = "info" if outcome.status == "applied" else "error"
            _messagebox_show(
                kind,
                "Sincronizare template-uri DOCX",
                outcome.summary_message,
                parent=parent,
            )
        return outcome
    except Exception as exc:
        if progress_window is not None:
            progress_window.close()
        outcome = SyncOutcome(
            status="failed",
            folder_path=str(folder_path) if folder_path is not None else None,
            summary_message=(
                "Sincronizarea template-urilor DOCX a eșuat fără a modifica baza de date.\n\n"
                f"Detaliu: {exc}"
            ),
        )
        if show_dialogs:
            _messagebox_show(
                "error",
                "Sincronizare template-uri DOCX",
                outcome.summary_message,
                parent=parent,
            )
        return outcome


def run_startup_template_sync(
    root: object,
    connection: sqlite3.Connection,
    *,
    database_path: str | Path,
    confirm_callback: Callable[[SyncScanReport], bool] | None = None,
    show_dialogs: bool = True,
) -> SyncOutcome:
    """Rulează sincronizarea neblocantă de la pornirea aplicației."""

    return execute_interactive_sync(
        root,
        connection,
        database_path=database_path,
        show_progress=True,
        report_no_changes=False,
        report_not_configured=False,
        report_cancelled=False,
        confirm_callback=confirm_callback,
        show_dialogs=show_dialogs,
    )
