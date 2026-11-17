"""Ecran Setări: informații despre stocare și configurarea sincronizării DOCX."""

from __future__ import annotations

import importlib.metadata
import sqlite3
import sys
import tkinter as tk
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ortodocs.services.template_sync import (
    SyncOutcome,
    SyncScanReport,
    TemplateSyncError,
    TemplateSyncService,
)
from ortodocs.ui.startup_sync import execute_interactive_sync, has_sync_schema
from ortodocs.ui.theme import (
    CardFrame,
    CollapsibleSection,
    SPACE_MD,
    apply_badge_style,
)


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    database_path: str
    application_version: str | None
    schema_version: int


def get_application_version() -> str | None:
    """Citește versiunea instalată sau metadata canonică din pyproject.toml."""

    try:
        version = importlib.metadata.version("ortodocs")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except importlib.metadata.PackageNotFoundError:
        pass

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        candidates.append(Path(meipass) / "pyproject.toml")
    candidates.append(Path(__file__).resolve().parents[3] / "pyproject.toml")

    for project_file in candidates:
        try:
            with project_file.open("rb") as handle:
                project_metadata = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue

        project = project_metadata.get("project")
        if not isinstance(project, dict):
            continue
        version = project.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()

    return None


def read_settings_snapshot(
    connection: sqlite3.Connection,
    database_path: str | Path,
    application_version: str | None,
) -> SettingsSnapshot:
    """Citește calea bazei, versiunea proiectului și user_version SQLite."""

    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("SQLite nu a returnat PRAGMA user_version.")
    return SettingsSnapshot(
        database_path=str(database_path),
        application_version=application_version,
        schema_version=int(row[0]),
    )


class SettingsView(ttk.Frame):
    """Prezintă informațiile aplicației și configurarea sincronizării DOCX."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
        database_path: str | Path,
        initial_sync_outcome: SyncOutcome | None = None,
        on_sync_applied: Callable[[SyncOutcome], None] | None = None,
    ) -> None:
        super().__init__(parent, padding=(0, 0), style="App.TFrame")
        self.connection = connection
        self.database_path = Path(database_path)
        self.sync_service = TemplateSyncService(connection, database_path=self.database_path)
        self.last_outcome = initial_sync_outcome
        self._on_sync_applied = on_sync_applied
        self.snapshot = read_settings_snapshot(
            connection,
            database_path,
            get_application_version(),
        )

        self.folder_var = tk.StringVar(value="")
        self.availability_var = tk.StringVar(value="Neconfigurat")
        self.last_sync_at_var = tk.StringVar(value="Niciodată")
        self.active_templates_var = tk.StringVar(value="0")
        self.last_sync_status_var = tk.StringVar(value="Niciodată")

        self._build_content()
        self.refresh_sync_summary()

    def _build_content(self) -> None:
        self.columnconfigure(0, weight=1)

        sync_card = CardFrame(self, padding=(18, 14))
        sync_card.pack(anchor="nw", fill="x", pady=(0, SPACE_MD))
        sync_frame = sync_card.body
        sync_frame.columnconfigure(1, weight=1)

        ttk.Label(sync_frame, text="Template-uri DOCX", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            sync_frame,
            text="Alege folderul în care sunt păstrate și editate în Microsoft Word documentele DOCX.",
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        ttk.Label(
            sync_frame,
            text="Folder template-uri",
            style="CardFieldLabel.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=(0, 20), pady=4)
        folder_controls = ttk.Frame(sync_frame, style="Card.TFrame")
        folder_controls.grid(row=2, column=1, sticky="ew", pady=4)
        folder_controls.columnconfigure(0, weight=1)

        self.folder_entry = ttk.Entry(
            folder_controls,
            textvariable=self.folder_var,
            state="readonly",
        )
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.choose_folder_button = ttk.Button(
            folder_controls,
            text="Alege folder",
            style="Secondary.TButton",
            command=self.choose_folder,
        )
        self.choose_folder_button.grid(row=0, column=1, sticky="e")

        ttk.Label(
            sync_frame,
            text="Disponibilitate folder",
            style="CardFieldLabel.TLabel",
        ).grid(row=3, column=0, sticky="w", padx=(0, 20), pady=4)
        self.availability_badge = ttk.Label(
            sync_frame,
            textvariable=self.availability_var,
            style="NeutralBadge.TLabel",
        )
        self.availability_badge.grid(row=3, column=1, sticky="w", pady=4)

        status_rows = (
            ("Data ultimei sincronizări", self.last_sync_at_var),
            ("Template-uri active", self.active_templates_var),
            ("Status ultima sincronizare", self.last_sync_status_var),
        )
        for offset, (label, variable) in enumerate(status_rows, start=4):
            ttk.Label(
                sync_frame,
                text=label,
                style="CardFieldLabel.TLabel",
            ).grid(row=offset, column=0, sticky="nw", padx=(0, 20), pady=4)
            ttk.Label(
                sync_frame,
                textvariable=variable,
                style="Card.TLabel",
                wraplength=740,
                justify="left",
            ).grid(row=offset, column=1, sticky="nw", pady=4)

        actions_row = ttk.Frame(sync_frame, style="Card.TFrame")
        actions_row.grid(
            row=len(status_rows) + 4,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(10, 0),
        )

        self.sync_now_button = ttk.Button(
            actions_row,
            text="Sincronizează acum",
            style="Primary.TButton",
            command=self.synchronize_now,
        )
        self.sync_now_button.pack(side="left")

        info_card = CardFrame(self, padding=(18, 14))
        info_card.pack(anchor="nw", fill="x")
        information = info_card.body
        information.columnconfigure(1, weight=1)

        ttk.Label(
            information,
            text="Informații despre aplicație",
            style="CardTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            information,
            text="Stocare locală pe acest calculator și identificarea versiunii curente.",
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        rows = (
            (
                "Versiunea aplicației",
                self.snapshot.application_version or "Indisponibilă în această instalare",
            ),
            ("Locația bazei de date", self.snapshot.database_path),
        )
        for offset, (label, value) in enumerate(rows, start=2):
            ttk.Label(
                information,
                text=label,
                style="CardFieldLabel.TLabel",
            ).grid(row=offset, column=0, sticky="nw", padx=(0, 20), pady=4)
            ttk.Label(
                information,
                text=value,
                style="Card.TLabel",
                wraplength=740,
                justify="left",
            ).grid(row=offset, column=1, sticky="nw", pady=4)

        self.technical_details = CollapsibleSection(
            information,
            title="Detalii tehnice",
            expanded=False,
            style="Card.TFrame",
        )
        self.technical_details.grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0)
        )
        self.technical_details.content.columnconfigure(1, weight=1)
        ttk.Label(
            self.technical_details.content,
            text="Versiunea schemei SQLite",
            style="Subtle.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 20), pady=4)
        ttk.Label(
            self.technical_details.content,
            text=str(self.snapshot.schema_version),
            style="Mono.TLabel",
        ).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(
            self.technical_details.content,
            text="Datele aplicației și versiunile de șabloane sunt păstrate exclusiv local pe acest calculator.",
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def on_show(self) -> None:
        self.refresh_sync_summary()

    def _update_availability_badge(self, text: str) -> None:
        normalized = text.strip().lower()
        if "neconfigurat" in normalized:
            apply_badge_style(self.availability_badge, "neutral")
        elif "inaccesibil" in normalized or "eroare" in normalized or "lip" in normalized:
            apply_badge_style(self.availability_badge, "warning")
        else:
            apply_badge_style(self.availability_badge, "success")

    def refresh_sync_summary(self) -> None:
        if not has_sync_schema(self.connection):
            self.folder_var.set("")
            self.availability_var.set("Neconfigurat")
            self._update_availability_badge("Neconfigurat")
            self.last_sync_at_var.set("Niciodată")
            self.active_templates_var.set("0")
            self.last_sync_status_var.set("Niciodată")
            return

        summary = self.sync_service.get_settings_summary(last_outcome=self.last_outcome)
        self.folder_var.set(str(summary["folder_path"]))
        availability_text = str(summary["folder_availability_text"])
        self.availability_var.set(availability_text)
        self._update_availability_badge(availability_text)
        self.last_sync_at_var.set(summary["last_sync_at"] or "Niciodată")
        total = int(summary["active_templates_total"])
        epi_count = int(summary["active_epicrisis_count"])
        proto_count = int(summary["active_protocol_count"])
        self.active_templates_var.set(
            f"{total} (Epicriză: {epi_count}, Protocol: {proto_count})"
        )
        self.last_sync_status_var.set(str(summary["last_sync_status"]))

    def choose_folder(self) -> Path | None:
        initial_dir = self.folder_var.get().strip() or None
        selected = filedialog.askdirectory(
            parent=self,
            title="Alege folderul cu template-uri DOCX",
            initialdir=initial_dir,
        )
        if not selected:
            return None
        try:
            saved_path = self.sync_service.set_configured_folder(selected)
        except (TemplateSyncError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror("Setări — Template-uri DOCX", str(exc), parent=self)
            return None

        self.refresh_sync_summary()
        return saved_path

    def synchronize_now(
        self,
        *,
        confirm_callback: Callable[[SyncScanReport], bool] | None = None,
        show_dialogs: bool = True,
    ) -> SyncOutcome:
        outcome = execute_interactive_sync(
            self,
            self.connection,
            database_path=self.database_path,
            show_progress=True,
            report_no_changes=True,
            report_not_configured=True,
            report_cancelled=True,
            confirm_callback=confirm_callback,
            show_dialogs=show_dialogs,
        )
        self.last_outcome = outcome
        self.refresh_sync_summary()
        if outcome.status == "applied" and self._on_sync_applied is not None:
            self._on_sync_applied(outcome)
        return outcome
