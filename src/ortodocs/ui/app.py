"""Fereastra principală, navigarea și ciclul de viață al conexiunii SQLite."""

from __future__ import annotations

import sqlite3
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ortodocs.services.template_sync import SyncOutcome
from ortodocs.ui.generation_view import GenerationView
from ortodocs.ui.history_view import HistoryView
from ortodocs.ui.medical_library_view import MedicalLibraryView
from ortodocs.ui.settings_view import SettingsView
from ortodocs.ui.theme import BG_APP, apply_soft_medical_theme, apply_window_icon


NAVIGATION_SECTIONS = (
    ("generation", "📝 Generare documente"),
    ("templates", "📚 Șabloane"),
    ("history", "🕘 Istoric"),
    ("settings", "⚙️ Setări"),
)

_SECTION_SUBTITLES = {
    "generation": "Selectează diagnosticul și intervenția, completează datele și generează documentele DOCX.",
    "templates": "Administrează diagnosticele, intervențiile și șabloanele Microsoft Word active.",
    "history": "Consultă și redeschide documentele medicale generate anterior.",
    "settings": "Configurează folderul de template-uri Microsoft Word și detaliile aplicației.",
}

class OrtoDocsApp:
    """Construiește shell-ul aplicației și deține conexiunea primită."""

    def __init__(
        self,
        root: tk.Tk,
        connection: sqlite3.Connection,
        *,
        database_path: str | Path,
        initial_sync_outcome: SyncOutcome | None = None,
    ) -> None:
        self.root = root
        self.connection = connection
        self.database_path = Path(database_path)
        self.initial_sync_outcome = initial_sync_outcome
        self._connection_closed = False
        self._active_section: str | None = None
        self.pages: dict[str, ttk.Frame] = {}
        self.navigation_buttons: dict[str, ttk.Button] = {}
        self._active_markers: dict[str, ttk.Frame] = {}

        self._configure_window()
        self._build_layout()
        self._build_pages()
        self.show_section("generation")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    @property
    def active_section(self) -> str | None:
        return self._active_section

    def _configure_window(self) -> None:
        self.root.title("OrtoDocs")
        apply_window_icon(self.root)
        apply_soft_medical_theme(self.root)
        try:
            scaling = float(self.root.tk.call("tk", "scaling"))
        except (tk.TclError, TypeError, ValueError):
            scaling = 1.3333
        dpi_factor = min(max(scaling / 1.3333, 1.0), 1.25)
        screen_w = max(800, int(self.root.winfo_screenwidth()))
        screen_h = max(600, int(self.root.winfo_screenheight()))
        target_w = min(int(round(1280 * dpi_factor)), max(1100, screen_w - 80))
        target_h = min(int(round(800 * dpi_factor)), max(700, screen_h - 80))
        min_w = min(1100, max(900, screen_w - 80))
        min_h = min(700, max(600, screen_h - 80))
        self.root.geometry(f"{target_w}x{target_h}")
        self.root.minsize(min_w, min_h)
        self.root.configure(background=BG_APP)

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(2, weight=1)
        shell.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", padding=(16, 22), width=238)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        ttk.Label(sidebar, text="OrtoDocs", style="SidebarTitle.TLabel").pack(
            anchor="w", pady=(0, 3)
        )
        ttk.Label(
            sidebar,
            text="Aplicație locală documente",
            style="SidebarMuted.TLabel",
        ).pack(anchor="w", pady=(0, 16))

        ttk.Frame(sidebar, height=1, style="Hairline.TFrame").pack(
            fill="x", pady=(0, 14)
        )

        for key, label in NAVIGATION_SECTIONS:
            row = ttk.Frame(sidebar, style="Sidebar.TFrame")
            row.pack(fill="x", pady=3)
            row.columnconfigure(1, weight=1)
            marker = ttk.Frame(row, width=4, style="ActiveMarker.TFrame")
            marker.grid(row=0, column=0, sticky="ns", padx=(0, 8), pady=3)
            marker.grid_propagate(False)
            marker.grid_remove()
            self._active_markers[key] = marker

            button = ttk.Button(
                row,
                text=label,
                style="Navigation.TButton",
                command=lambda section=key: self.show_section(section),
            )
            button.grid(row=0, column=1, sticky="ew")
            self.navigation_buttons[key] = button

        divider = ttk.Frame(shell, width=1, style="Hairline.TFrame")
        divider.grid(row=0, column=1, sticky="ns")

        content = ttk.Frame(shell, style="App.TFrame", padding=(28, 22, 28, 22))
        content.grid(row=0, column=2, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        header = ttk.Frame(content, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.columnconfigure(0, weight=1)

        self.page_title = ttk.Label(header, style="PageTitle.TLabel")
        self.page_title.grid(row=0, column=0, sticky="w")
        self.page_subtitle = ttk.Label(header, style="Muted.TLabel")
        self.page_subtitle.grid(row=1, column=0, sticky="w", pady=(3, 0))

        self.page_host = ttk.Frame(content, style="App.TFrame")
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

    def _build_pages(self) -> None:
        for key, _ in NAVIGATION_SECTIONS:
            if key == "generation":
                page = GenerationView(
                    self.page_host,
                    connection=self.connection,
                )
            elif key == "templates":
                page = MedicalLibraryView(
                    self.page_host,
                    connection=self.connection,
                )
            elif key == "history":
                generation_page = self.pages.get("generation")
                if not isinstance(generation_page, GenerationView):
                    raise RuntimeError("GenerationView trebuie creată înainte de HistoryView.")
                page = HistoryView(
                    self.page_host,
                    connection=self.connection,
                    open_session=generation_page.open_documents_session,
                )
            elif key == "settings":
                page = SettingsView(
                    self.page_host,
                    connection=self.connection,
                    database_path=self.database_path,
                    initial_sync_outcome=self.initial_sync_outcome,
                    on_sync_applied=self._handle_sync_applied,
                )
            else:
                raise ValueError(f"Nu există o pagină pentru secțiunea {key!r}.")
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page

    def _handle_sync_applied(self, outcome: SyncOutcome) -> None:
        self.initial_sync_outcome = outcome
        for key, page in self.pages.items():
            if key == "settings":
                continue
            on_show = getattr(page, "on_show", None)
            if callable(on_show):
                on_show()

    def show_section(self, section: str) -> None:
        """Afișează pagina aleasă și actualizează titlul și indicatorul activ."""

        labels = dict(NAVIGATION_SECTIONS)
        if section not in labels:
            raise ValueError(f"Secțiune OrtoDocs necunoscută: {section!r}.")

        previous_section = self._active_section
        self._active_section = section
        self.page_title.configure(text=labels[section])
        if hasattr(self, "page_subtitle"):
            self.page_subtitle.configure(text=_SECTION_SUBTITLES.get(section, ""))
        self.pages[section].tkraise()
        if previous_section is not None and previous_section != section:
            on_show = getattr(self.pages[section], "on_show", None)
            if callable(on_show):
                on_show()
        nav_buttons = getattr(self, "navigation_buttons", {})
        for key, marker in self._active_markers.items():
            button = nav_buttons.get(key)
            if key == section:
                marker.grid()
                if button is not None:
                    button.configure(style="NavigationActive.TButton")
            else:
                marker.grid_remove()
                if button is not None:
                    button.configure(style="Navigation.TButton")

    def close(self) -> None:
        """Închide o singură dată conexiunea și fereastra principală."""

        if self._connection_closed:
            return
        generation_page = self.pages.get("generation")
        resolve_close = getattr(generation_page, "resolve_application_close", None)
        if callable(resolve_close) and not resolve_close():
            return
        self._connection_closed = True
        try:
            self.connection.close()
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
