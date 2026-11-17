from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import tkinter as tk
import tomllib
import unittest
from io import BytesIO
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

from docx import Document

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.main import main
from ortodocs.services.template_sync import PATIENT_CONFIRMATION_TEXT, TemplateSyncService
from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.app import NAVIGATION_SECTIONS, OrtoDocsApp
from ortodocs.ui.dpi import DpiConfigurationResult
from ortodocs.ui.generation_view import GenerationView
from ortodocs.ui.history_view import HistoryView
from ortodocs.ui.medical_library_view import MedicalLibraryView
from ortodocs.ui.settings_view import (
    SettingsView,
    get_application_version,
    read_settings_snapshot,
)
from ortodocs.ui.theme import (
    TEXT_DISCRETE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    apply_window_icon,
    resolve_app_icon_path,
)
from ortodocs.ui.startup_sync import (
    BatchSyncConfirmationDialog,
    ask_batch_sync_confirmation,
)


class SettingsSnapshotTests(unittest.TestCase):
    def test_settings_snapshot_reads_database_path_and_schema_version(self) -> None:
        connection = sqlite3.connect(":memory:", autocommit=True)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA user_version = 7")
        database_path = Path("C:/Local/OrtoDocs/ortodocs.db")

        snapshot = read_settings_snapshot(
            connection,
            database_path,
            "0.1.0",
        )

        self.assertEqual(snapshot.database_path, str(database_path))
        self.assertEqual(snapshot.application_version, "0.1.0")
        self.assertEqual(snapshot.schema_version, 7)

    def test_application_version_comes_from_project_metadata(self) -> None:
        project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project_file.open("rb") as handle:
            expected_version = tomllib.load(handle)["project"]["version"]

        self.assertEqual(expected_version, "1.0.0")
        self.assertEqual(get_application_version(), expected_version)

    def test_application_version_reads_bundled_pyproject_in_frozen_mode(self) -> None:
        import importlib.metadata

        with tempfile.TemporaryDirectory() as tmpdir:
            meipass_dir = Path(tmpdir)
            (meipass_dir / "pyproject.toml").write_text(
                '[project]\nname = "ortodocs"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            with (
                patch("ortodocs.ui.settings_view.importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError),
                patch.object(sys, "_MEIPASS", str(meipass_dir), create=True),
            ):
                self.assertEqual(get_application_version(), "1.0.0")

                root = tk.Tk()
                root.withdraw()
                connection = sqlite3.connect(":memory:", autocommit=True)
                try:
                    view = SettingsView(
                        root,
                        connection=connection,
                        database_path=meipass_dir / "ortodocs.db",
                    )
                    labels = [
                        child.cget("text")
                        for child in view.winfo_children()
                        for sub in child.winfo_children()
                        for widget in sub.winfo_children()
                        for child in ([widget] + list(widget.winfo_children()))
                        if isinstance(child, ttk.Label)
                    ]
                    self.assertIn("1.0.0", labels)
                    self.assertNotIn("Indisponibilă în această instalare", labels)
                finally:
                    connection.close()
                    root.destroy()

    def test_application_version_uses_installed_distribution_metadata_in_frozen_mode(self) -> None:
        with (
            patch("ortodocs.ui.settings_view.importlib.metadata.version", return_value="1.0.0"),
            patch.object(sys, "_MEIPASS", "C:/nonexistent/_MEIPASS", create=True),
        ):
            self.assertEqual(get_application_version(), "1.0.0")

    def test_window_icon_resolves_and_applies_without_altering_dpi(self) -> None:
        dev_icon = resolve_app_icon_path()
        self.assertIsNotNone(dev_icon)
        assert dev_icon is not None
        self.assertEqual(dev_icon.name, "ortodocs.ico")
        self.assertTrue(dev_icon.is_file())

        with tempfile.TemporaryDirectory() as tmpdir:
            meipass_dir = Path(tmpdir)
            frozen_icon = meipass_dir / "ortodocs.ico"
            frozen_icon.write_bytes(dev_icon.read_bytes())
            with patch.object(sys, "_MEIPASS", str(meipass_dir), create=True):
                self.assertEqual(resolve_app_icon_path(), frozen_icon)

        root = tk.Tk()
        root.withdraw()
        try:
            scaling_before = float(root.tk.call("tk", "scaling"))
            applied_root = apply_window_icon(root)
            top = tk.Toplevel(root)
            top.withdraw()
            applied_top = apply_window_icon(top)
            scaling_after = float(root.tk.call("tk", "scaling"))

            self.assertEqual(applied_root, sys.platform == "win32")
            self.assertEqual(applied_top, sys.platform == "win32")
            self.assertEqual(scaling_before, scaling_after)

            with patch("ortodocs.ui.theme.sys.platform", "linux"):
                self.assertFalse(apply_window_icon(root))
        finally:
            root.destroy()


class NavigationDefinitionTests(unittest.TestCase):
    def test_navigation_contains_exactly_the_four_requested_sections(self) -> None:
        self.assertEqual(
            NAVIGATION_SECTIONS,
            (
                ("generation", "📝 Generare documente"),
                ("templates", "📚 Șabloane"),
                ("history", "🕘 Istoric"),
                ("settings", "⚙️ Setări"),
            ),
        )
        keys = [key for key, _label in NAVIGATION_SECTIONS]
        labels = [label for _key, label in NAVIGATION_SECTIONS]
        self.assertNotIn("medical_library", keys)
        self.assertNotIn("Bibliotecă medicală", labels)


class ApplicationCloseProtectionTests(unittest.TestCase):
    class _Root:
        def __init__(self) -> None:
            self.destroyed = False

        def destroy(self) -> None:
            self.destroyed = True

    class _GenerationPage:
        def __init__(self, allow_close: bool) -> None:
            self.allow_close = allow_close
            self.calls = 0

        def resolve_application_close(self) -> bool:
            self.calls += 1
            return self.allow_close

    def _app(self, allow_close: bool) -> tuple[OrtoDocsApp, sqlite3.Connection, _Root, _GenerationPage]:
        app = OrtoDocsApp.__new__(OrtoDocsApp)
        connection = sqlite3.connect(":memory:", autocommit=True)
        root = self._Root()
        page = self._GenerationPage(allow_close)
        app.connection = connection
        app.root = root
        app.pages = {"generation": page}
        app._connection_closed = False
        return app, connection, root, page

    def test_cancel_or_save_failure_keeps_application_and_connection_open(self) -> None:
        app, connection, root, page = self._app(False)
        self.addCleanup(connection.close)

        app.close()

        self.assertEqual(page.calls, 1)
        self.assertFalse(app._connection_closed)
        self.assertFalse(root.destroyed)
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_successful_resolution_closes_connection_and_application(self) -> None:
        app, connection, root, page = self._app(True)

        app.close()

        self.assertEqual(page.calls, 1)
        self.assertTrue(app._connection_closed)
        self.assertTrue(root.destroyed)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_section_navigation_does_not_close_the_documents_window(self) -> None:
        class _Page:
            def __init__(self, *, refreshable: bool = False) -> None:
                self.raised = False
                self.refresh_calls = 0
                self.refreshable = refreshable

            def tkraise(self) -> None:
                self.raised = True

            def on_show(self) -> None:
                self.refresh_calls += 1

        class _Widget:
            def configure(self, **_options: object) -> None:
                pass

            def grid(self) -> None:
                pass

            def grid_remove(self) -> None:
                pass

        app = OrtoDocsApp.__new__(OrtoDocsApp)
        generation_page = _Page()
        generation_page.documents_window = object()
        history_page = _Page(refreshable=True)
        markers = {key: _Widget() for key, _label in NAVIGATION_SECTIONS}
        app.pages = {"generation": generation_page, "history": history_page}
        app.page_title = _Widget()
        app._active_markers = markers
        app._active_section = "generation"

        app.show_section("history")

        self.assertTrue(history_page.raised)
        self.assertEqual(history_page.refresh_calls, 1)
        self.assertIsNotNone(generation_page.documents_window)

    def test_returning_to_a_section_calls_its_refresh_hook(self) -> None:
        class _Page:
            def __init__(self) -> None:
                self.refresh_calls = 0

            def tkraise(self) -> None:
                pass

            def on_show(self) -> None:
                self.refresh_calls += 1

        class _Widget:
            def configure(self, **_options: object) -> None:
                pass

            def grid(self) -> None:
                pass

            def grid_remove(self) -> None:
                pass

        app = OrtoDocsApp.__new__(OrtoDocsApp)
        app.pages = {key: _Page() for key, _label in NAVIGATION_SECTIONS}
        app.page_title = _Widget()
        app._active_markers = {key: _Widget() for key, _label in NAVIGATION_SECTIONS}
        app._active_section = "generation"

        app.show_section("templates")
        app.show_section("history")
        app.show_section("templates")

        self.assertEqual(app.pages["templates"].refresh_calls, 2)
        self.assertEqual(app.pages["history"].refresh_calls, 1)


class _FakeRoot:
    def __init__(self) -> None:
        self.withdrawn = False
        self.visible = False
        self.destroyed = False
        self.mainloop_called = False

    def withdraw(self) -> None:
        self.withdrawn = True

    def deiconify(self) -> None:
        self.visible = True

    def destroy(self) -> None:
        self.destroyed = True

    def mainloop(self) -> None:
        self.mainloop_called = True


class StartupErrorTests(unittest.TestCase):
    def test_initialization_error_is_reported_without_opening_connection(self) -> None:
        root = _FakeRoot()
        with (
            patch("ortodocs.main.configure_windows_dpi_awareness") as configure_dpi,
            patch("ortodocs.main.tk.Tk", return_value=root),
            patch(
                "ortodocs.main.initialize_database",
                side_effect=RuntimeError("migration failed"),
            ),
            patch("ortodocs.main.connect_database") as connect,
            patch("ortodocs.main.messagebox.showerror") as show_error,
        ):
            main()

        configure_dpi.assert_called_once_with()
        connect.assert_not_called()
        show_error.assert_called_once()
        self.assertIn("migration failed", show_error.call_args.args[1])
        self.assertTrue(root.destroyed)

    def test_ui_construction_error_closes_the_opened_connection(self) -> None:
        root = _FakeRoot()
        connection = sqlite3.connect(":memory:", autocommit=True)
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch("ortodocs.main.configure_windows_dpi_awareness") as configure_dpi,
            patch("ortodocs.main.tk.Tk", return_value=root),
            patch(
                "ortodocs.main.initialize_database",
                return_value=Path(temporary_directory) / "ortodocs.db",
            ),
            patch("ortodocs.main.connect_database", return_value=connection),
            patch(
                "ortodocs.main.OrtoDocsApp",
                side_effect=RuntimeError("widget construction failed"),
            ),
            patch("ortodocs.main.messagebox.showerror") as show_error,
        ):
            main()

        configure_dpi.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        show_error.assert_called_once()
        self.assertIn("widget construction failed", show_error.call_args.args[1])
        self.assertTrue(root.destroyed)


class TkShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu are un display Tcl/Tk disponibil: {exc}")

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.connection = sqlite3.connect(":memory:", autocommit=True)
        self.app = _build_app_for_test(self.root, self.connection)

    def tearDown(self) -> None:
        self.app.close()

    def test_switching_sections_updates_active_page_and_title(self) -> None:
        self.assertEqual(len(self.app.pages), 4)
        button_labels = [
            button.cget("text") for button in self.app.navigation_buttons.values()
        ]
        self.assertEqual(
            button_labels,
            ["📝 Generare documente", "📚 Șabloane", "🕘 Istoric", "⚙️ Setări"],
        )
        self.assertNotIn("Bibliotecă medicală", button_labels)

        self.app.show_section("templates")
        self.assertEqual(self.app.active_section, "templates")
        self.assertEqual(self.app.page_title.cget("text"), "📚 Șabloane")

        self.app.show_section("history")
        self.assertEqual(self.app.active_section, "history")
        self.assertEqual(self.app.page_title.cget("text"), "🕘 Istoric")

    def test_settings_view_uses_collapsible_technical_details_for_sqlite_schema(self) -> None:
        self.app.show_section("settings")
        settings_page = self.app.pages["settings"]
        assert isinstance(settings_page, SettingsView)
        self.assertFalse(settings_page.technical_details.is_expanded)
        self.assertEqual(settings_page.technical_details.content.winfo_manager(), "")
        settings_page.technical_details.toggle()
        self.root.update_idletasks()
        self.assertTrue(settings_page.technical_details.is_expanded)
        self.assertEqual(settings_page.technical_details.content.winfo_manager(), "grid")

    def test_all_four_pages_share_centralized_soft_medical_typography_and_contrast(self) -> None:
        style = ttk.Style(self.root)
        self.assertEqual(TEXT_PRIMARY, "#24332F")
        self.assertEqual(TEXT_SECONDARY, "#5F706A")
        self.assertEqual(TEXT_DISCRETE, "#667770")

        self.assertEqual(str(style.lookup("TLabel", "foreground")), TEXT_PRIMARY)
        self.assertEqual(str(style.lookup("Muted.TLabel", "foreground")), TEXT_SECONDARY)
        self.assertEqual(str(style.lookup("CardMuted.TLabel", "foreground")), TEXT_SECONDARY)
        self.assertEqual(str(style.lookup("CardCode.TLabel", "foreground")), TEXT_DISCRETE)
        self.assertEqual(str(style.lookup("Treeview", "foreground")), TEXT_PRIMARY)
        self.assertEqual(str(style.lookup("Treeview.Heading", "foreground")), TEXT_PRIMARY)
        self.assertTrue(style.lookup("SubtleFieldLabel.TLabel", "font"))

        for page in self.app.pages.values():
            self.assertEqual(page.cget("style"), "App.TFrame")

        gen_page = self.app.pages["generation"]
        assert isinstance(gen_page, GenerationView)
        self.assertEqual(gen_page.diagnosis_code_label.cget("style"), "CardCode.TLabel")
        self.assertEqual(gen_page.intervention_code_label.cget("style"), "CardCode.TLabel")
        self.assertIn(
            str(gen_page.status_label.cget("style")),
            {"GenerationMuted.TLabel", "GenerationError.TLabel"},
        )

        hist_page = self.app.pages["history"]
        assert isinstance(hist_page, HistoryView)
        self.assertEqual(str(hist_page.status_label.cget("style")), "Muted.TLabel")

        tpl_page = self.app.pages["templates"]
        assert isinstance(tpl_page, MedicalLibraryView)
        self.assertIn(
            str(tpl_page.status_label.cget("style")),
            {"Muted.TLabel", "GenerationError.TLabel"},
        )

    def test_every_page_receives_the_callers_connection(self) -> None:
        self.assertTrue(all(page.connection is self.connection for page in self.app.pages.values()))
        self.assertIs(self.app.pages["settings"].connection, self.connection)
        self.assertIsInstance(self.app.pages["settings"], SettingsView)
        self.assertIsInstance(self.app.pages["templates"], MedicalLibraryView)
        self.assertIsInstance(self.app.pages["history"], HistoryView)
        history_opener = self.app.pages["history"].open_session
        self.assertIs(history_opener.__self__, self.app.pages["generation"])
        self.assertIsInstance(history_opener.__self__, GenerationView)
        self.assertIs(history_opener.__func__, GenerationView.open_documents_session)

    def test_close_closes_connection_once_and_is_repeatable(self) -> None:
        self.app.close()
        self.app.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connection.execute("SELECT 1")


def _build_app_for_test(root: tk.Tk, connection: sqlite3.Connection):
    from ortodocs.ui.app import OrtoDocsApp

    return OrtoDocsApp(
        root,
        connection,
        database_path="C:/Temp/ortodocs.db",
    )


def _make_docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _create_sync_folder(root_dir: Path, *, valid_docx: bool = True) -> Path:
    sync_dir = root_dir / "sync_templates"
    (sync_dir / "epicrize").mkdir(parents=True, exist_ok=True)
    (sync_dir / "protocoale").mkdir(parents=True, exist_ok=True)
    if valid_docx:
        (sync_dir / "epicrize" / "epi.docx").write_bytes(
            _make_docx_bytes("Epicriză {{sex}}")
        )
    else:
        (sync_dir / "epicrize" / "epi.docx").write_bytes(b"corrupt-docx")
    (sync_dir / "protocoale" / "proto.docx").write_bytes(
        _make_docx_bytes("Protocol {{lateralitate}}")
    )
    (sync_dir / "catalog.json").write_text(
        json.dumps(
            {
                "catalog_version": 1,
                "diagnoses": [
                    {
                        "key": "diag-1",
                        "name": "Coxartroză",
                        "code": "M16",
                        "epicrisis_path": "epicrize/epi.docx",
                    }
                ],
                "interventions": [
                    {
                        "key": "int-1",
                        "diagnosis_key": "diag-1",
                        "name": "Proteză șold",
                        "code": "ORT-1",
                        "protocol_path": "protocoale/proto.docx",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return sync_dir


class TemplateSyncUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu are un display Tcl/Tk disponibil: {exc}")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_tmp = Path(self.temp_dir.name)
        self.database_path = self.root_tmp / "profile" / "ortodocs.db"
        initialize_database(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 24. Modal confirmare cu textul și butoanele exacte
    def test_24_batch_confirmation_dialog_contains_exact_text_and_buttons(self) -> None:
        sync_dir = _create_sync_folder(self.root_tmp)
        conn = connect_database(self.database_path)
        root = tk.Tk()
        root.withdraw()
        try:
            service = TemplateSyncService(conn, database_path=self.database_path)
            report = service.scan_and_validate(folder_path=sync_dir)
            self.assertTrue(report.can_apply)

            dialog_cancel = BatchSyncConfirmationDialog(root, report)
            self.assertEqual(dialog_cancel.confirmation_label.cget("text"), PATIENT_CONFIRMATION_TEXT)
            self.assertEqual(dialog_cancel.confirm_button.cget("text"), "Confirm și actualizează")
            self.assertEqual(dialog_cancel.cancel_button.cget("text"), "Anulează")
            dialog_cancel.cancel_button.invoke()
            self.assertFalse(dialog_cancel.confirmed)

            def _click_confirm_later() -> None:
                for child in root.winfo_children():
                    if isinstance(child, BatchSyncConfirmationDialog):
                        child.confirm_button.invoke()

            root.after(20, _click_confirm_later)
            confirmed = ask_batch_sync_confirmation(root, report)
            self.assertTrue(confirmed)
        finally:
            if root.winfo_exists():
                root.destroy()
            conn.close()

    # 25. SettingsView salvează folderul și actualizează UI-ul „Template-uri DOCX”
    def test_25_settings_view_persists_sync_folder_and_updates_status_ui(self) -> None:
        sync_dir = _create_sync_folder(self.root_tmp)
        conn = connect_database(self.database_path)
        root = tk.Tk()
        root.withdraw()
        try:
            settings = SettingsView(
                root,
                connection=conn,
                database_path=self.database_path,
            )
            self.assertEqual(str(settings.folder_entry.cget("state")), "readonly")
            self.assertEqual(settings.choose_folder_button.cget("text"), "Alege folder")
            self.assertEqual(settings.sync_now_button.cget("text"), "Sincronizează acum")
            self.assertEqual(settings.active_templates_var.get(), "0 (Epicriză: 0, Protocol: 0)")

            with patch(
                "ortodocs.ui.settings_view.filedialog.askdirectory",
                return_value=str(sync_dir),
            ):
                settings.choose_folder_button.invoke()

            self.assertEqual(settings.folder_var.get(), str(sync_dir.resolve()))
            self.assertEqual(settings.availability_var.get(), "Disponibil")

            with (
                patch("tkinter.messagebox.showinfo") as show_info,
                patch("tkinter.messagebox.showerror") as show_error,
            ):
                outcome = settings.synchronize_now(
                    confirm_callback=lambda _report: True,
                )

            show_error.assert_not_called()
            self.assertEqual(outcome.status, "applied")
            show_info.assert_called_once()
            self.assertEqual(settings.active_templates_var.get(), "2 (Epicriză: 1, Protocol: 1)")
            self.assertNotEqual(settings.last_sync_at_var.get(), "Niciodată")
            self.assertIn("Reușită", settings.last_sync_status_var.get())

            # Redeschidem SettingsView pe o conexiune nouă pentru a verifica persistența.
            conn2 = connect_database(self.database_path)
            try:
                settings2 = SettingsView(
                    root,
                    connection=conn2,
                    database_path=self.database_path,
                )
                self.assertEqual(settings2.folder_var.get(), str(sync_dir.resolve()))
                self.assertEqual(settings2.active_templates_var.get(), "2 (Epicriză: 1, Protocol: 1)")
                self.assertIn("Reușită", settings2.last_sync_status_var.get())
            finally:
                conn2.close()
        finally:
            if root.winfo_exists():
                root.destroy()
            conn.close()

    def _run_main_with_automation(
        self,
        *,
        confirm_dialog: bool,
    ) -> tuple[dict[str, object], object, object]:
        original_tk = tk.Tk
        captured_apps: list[OrtoDocsApp] = []
        state: dict[str, object] = {
            "dialogs_seen": 0,
            "timed_out": False,
        }

        def _capture_app(*args: object, **kwargs: object) -> OrtoDocsApp:
            app = OrtoDocsApp(*args, **kwargs)
            captured_apps.append(app)
            return app

        def _make_root() -> tk.Tk:
            r = original_tk()
            after_ids: list[str] = []
            ticks = 0

            def _shutdown() -> None:
                if captured_apps:
                    captured_apps[-1].close()
                elif r.winfo_exists():
                    r.quit()
                    r.destroy()

            def _poll() -> None:
                nonlocal ticks
                ticks += 1
                if not r.winfo_exists():
                    return
                for child in list(r.winfo_children()):
                    if isinstance(child, BatchSyncConfirmationDialog):
                        state["dialogs_seen"] = int(state["dialogs_seen"]) + 1
                        if confirm_dialog:
                            child.confirm_button.invoke()
                        else:
                            child.cancel_button.invoke()
                if captured_apps and r.title() == "OrtoDocs":
                    after_ids.append(r.after(15, _shutdown))
                    return
                if ticks >= 250:
                    state["timed_out"] = True
                    _shutdown()
                    return
                after_ids.append(r.after(20, _poll))

            after_ids.append(r.after(20, _poll))
            return r

        with (
            patch(
                "ortodocs.main.configure_windows_dpi_awareness",
                return_value=DpiConfigurationResult("already_aware", None, 1, 1),
            ) as configure_dpi,
            patch("ortodocs.main.tk.Tk", side_effect=_make_root),
            patch("ortodocs.main.initialize_database", return_value=self.database_path),
            patch("ortodocs.main.OrtoDocsApp", side_effect=_capture_app),
            patch("tkinter.messagebox.showinfo") as sync_info,
            patch("tkinter.messagebox.showerror") as sync_error,
        ):
            main()

        configure_dpi.assert_called_once_with()

        state["app_started"] = bool(captured_apps)
        if captured_apps:
            state["initial_sync_outcome"] = captured_apps[-1].initial_sync_outcome
        self.assertFalse(state["timed_out"], "Sesiunea mainloop nu s-a închis în timpul alocat.")
        return state, sync_info, sync_error

    # 22. Sincronizare la pornire în mainloop()
    def test_22_startup_sync_applies_updates_inside_mainloop_session(self) -> None:
        sync_dir = _create_sync_folder(self.root_tmp)
        conn = connect_database(self.database_path)
        TemplateSyncService(conn, database_path=self.database_path).set_configured_folder(sync_dir)
        conn.close()

        state, sync_info, sync_error = self._run_main_with_automation(
            confirm_dialog=True,
        )

        self.assertTrue(state["app_started"])
        self.assertGreaterEqual(int(state["dialogs_seen"]), 1)
        sync_error.assert_not_called()
        sync_info.assert_called_once()
        check_conn = connect_database(self.database_path)
        try:
            active_count = check_conn.execute(
                "SELECT COUNT(*) FROM independent_template_versions WHERE status = 'active'"
            ).fetchone()[0]
            self.assertEqual(active_count, 2)
        finally:
            check_conn.close()

    # 23. Pornirea continuă normal dacă sincronizarea eșuează sau utilizatorul anulează
    def test_23_startup_continues_normally_when_sync_fails_or_cancels(self) -> None:
        # Cazul A: DOCX corupt la pornire -> eroare raportată non-fatal, aplicația pornește normal.
        bad_dir = _create_sync_folder(self.root_tmp / "bad", valid_docx=False)
        conn = connect_database(self.database_path)
        TemplateSyncService(conn, database_path=self.database_path).set_configured_folder(bad_dir)
        conn.close()

        state_a, sync_info_a, sync_error_a = self._run_main_with_automation(
            confirm_dialog=False,
        )
        self.assertTrue(state_a["app_started"])
        sync_info_a.assert_not_called()
        sync_error_a.assert_called_once()
        check_conn = connect_database(self.database_path)
        try:
            self.assertEqual(
                check_conn.execute("SELECT COUNT(*) FROM independent_template_versions").fetchone()[0],
                0,
            )
        finally:
            check_conn.close()

        # Cazul B: Utilizatorul apasă "Anulează" în dialog -> 0 modificări în DB, aplicația pornește normal.
        good_dir = _create_sync_folder(self.root_tmp / "good", valid_docx=True)
        conn = connect_database(self.database_path)
        TemplateSyncService(conn, database_path=self.database_path).set_configured_folder(good_dir)
        conn.close()

        state_b, sync_info_b, sync_error_b = self._run_main_with_automation(
            confirm_dialog=False,
        )
        self.assertTrue(state_b["app_started"])
        self.assertGreaterEqual(int(state_b["dialogs_seen"]), 1)
        sync_error_b.assert_not_called()
        sync_info_b.assert_not_called()

        check_conn2 = connect_database(self.database_path)
        try:
            self.assertEqual(
                check_conn2.execute("SELECT COUNT(*) FROM independent_template_versions").fetchone()[0],
                0,
            )
        finally:
            check_conn2.close()

    def test_startup_without_configured_folder_starts_cleanly(self) -> None:
        state, sync_info, sync_error = self._run_main_with_automation(
            confirm_dialog=False,
        )
        self.assertTrue(state["app_started"])
        self.assertEqual(int(state["dialogs_seen"]), 0)
        sync_info.assert_not_called()
        sync_error.assert_not_called()
        self.assertEqual(state["initial_sync_outcome"].status, "not_configured")

    def test_startup_with_unavailable_folder_reports_error_and_starts_cleanly(self) -> None:
        missing_dir = self.root_tmp / "temporarily_unavailable"
        missing_dir.mkdir(parents=True)
        conn = connect_database(self.database_path)
        TemplateSyncService(conn, database_path=self.database_path).set_configured_folder(missing_dir)
        conn.close()
        missing_dir.rmdir()

        state, sync_info, sync_error = self._run_main_with_automation(
            confirm_dialog=False,
        )
        self.assertTrue(state["app_started"])
        self.assertEqual(int(state["dialogs_seen"]), 0)
        sync_info.assert_not_called()
        sync_error.assert_called_once()
        self.assertEqual(state["initial_sync_outcome"].status, "folder_unavailable")

    def test_startup_adopts_manual_active_versions_without_duplication(self) -> None:
        from ortodocs.services import medical_library, template_sets

        sync_dir = _create_sync_folder(self.root_tmp / "adopt")
        epi_bytes = (sync_dir / "epicrize" / "epi.docx").read_bytes()
        proto_bytes = (sync_dir / "protocoale" / "proto.docx").read_bytes()

        conn = connect_database(self.database_path)
        try:
            diag_id = medical_library.add_diagnosis(conn, "Coxartroză", code="M16")
            interv_id = medical_library.add_intervention(conn, diag_id, "Proteză șold", code="ORT-1")
            epi_v1 = template_sets.import_independent_template(
                conn,
                owner_type="diagnosis",
                owner_id=diag_id,
                document_bytes=epi_bytes,
            )
            template_sets.activate_independent_template(
                conn,
                int(epi_v1["id"]),
                patient_identification_review_confirmed=True,
            )
            proto_v1 = template_sets.import_independent_template(
                conn,
                owner_type="intervention",
                owner_id=interv_id,
                document_bytes=proto_bytes,
            )
            template_sets.activate_independent_template(
                conn,
                int(proto_v1["id"]),
                patient_identification_review_confirmed=True,
            )
            TemplateSyncService(conn, database_path=self.database_path).set_configured_folder(sync_dir)
        finally:
            conn.close()

        state, sync_info, sync_error = self._run_main_with_automation(
            confirm_dialog=False,
        )
        self.assertTrue(state["app_started"])
        self.assertEqual(int(state["dialogs_seen"]), 0)
        sync_error.assert_not_called()
        sync_info.assert_called_once()
        outcome = state["initial_sync_outcome"]
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(outcome.imported_count, 0)
        self.assertEqual(outcome.linked_count, 2)
        self.assertEqual(outcome.unchanged_count, 2)

        check_conn = connect_database(self.database_path)
        try:
            self.assertEqual(
                check_conn.execute("SELECT COUNT(*) FROM independent_template_versions").fetchone()[0],
                2,
            )
            self.assertEqual(
                check_conn.execute("SELECT COUNT(*) FROM template_sync_sources").fetchone()[0],
                2,
            )
        finally:
            check_conn.close()


if __name__ == "__main__":
    unittest.main()
