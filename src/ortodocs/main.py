"""Punctul de intrare pentru aplicația desktop OrtoDocs."""

from __future__ import annotations

import sqlite3
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.app import OrtoDocsApp
from ortodocs.ui.dpi import (
    collect_tk_dpi_diagnostics,
    configure_windows_dpi_awareness,
    format_dpi_diagnostics,
)
from ortodocs.ui.startup_sync import run_startup_template_sync
from ortodocs.ui.theme import apply_soft_medical_theme


def _show_startup_error(root: tk.Tk | None, error: BaseException) -> None:
    message = f"OrtoDocs nu a putut porni.\n\nDetalii: {error}"
    if root is None:
        # Tk nu poate afișa un dialog dacă nu a reușit nici măcar inițializarea Tcl.
        print(message, file=sys.stderr)
        return

    try:
        root.deiconify()
        messagebox.showerror("Eroare la pornirea OrtoDocs", message, parent=root)
    except tk.TclError:
        print(message, file=sys.stderr)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    """Inițializează baza, deschide conexiunea aplicației și pornește Tk."""

    dpi_result = configure_windows_dpi_awareness()
    root: tk.Tk | None = None
    connection: sqlite3.Connection | None = None
    try:
        root = tk.Tk()
        root.withdraw()
        if getattr(root, "tk", None) is not None:
            apply_soft_medical_theme(root)
            if not getattr(sys, "frozen", False):
                print(
                    format_dpi_diagnostics(
                        collect_tk_dpi_diagnostics(root),
                        config_result=dpi_result,
                    )
                )
    except Exception as exc:
        _show_startup_error(None, exc)
        return

    try:
        database_path: Path = initialize_database()
        connection = connect_database(database_path)
        initial_sync_outcome = run_startup_template_sync(
            root,
            connection,
            database_path=database_path,
        )
        application = OrtoDocsApp(
            root,
            connection,
            database_path=database_path,
            initial_sync_outcome=initial_sync_outcome,
        )
        root.deiconify()
    except Exception as exc:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        _show_startup_error(root, exc)
        return

    try:
        root.mainloop()
    except Exception:
        application.close()
        raise


if __name__ == "__main__":
    main()
