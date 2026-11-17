from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.generation import (
    GeneratedSession,
    GenerationHistoryItem,
    GenerationSessionDetail,
)
from ortodocs.models.rich_document import Paragraph, RichDocument, TextInline
from ortodocs.storage.database import (
    connect_database,
    save_generation_session,
)
from ortodocs.storage.generation_repositories import (
    GenerationPersistenceError,
    get_generation_session,
    list_generation_sessions,
)
from ortodocs.storage.migrations import initialize_database
from ortodocs.ui.history_view import HistoryView, format_generation_timestamp


class HistoryViewHelperTests(unittest.TestCase):
    def test_session_detail_conversion_preserves_id_and_distinct_documents(self) -> None:
        epicrisis = RichDocument([Paragraph([TextInline("Epicriză")])])
        protocol = RichDocument([Paragraph([TextInline("Protocol")])])
        detail = GenerationSessionDetail(
            history_item=GenerationHistoryItem(
                session_id=42,
                diagnosis_id=1,
                intervention_id=2,
                template_set_id=3,
                diagnosis_label_snapshot="Diagnostic istoric",
                intervention_label_snapshot="Intervenție istorică",
                created_at="2026-11-10T08:30:00.000Z",
            ),
            epicrisis_recommendations=epicrisis,
            operative_protocol=protocol,
            epicrisis_template_id=4,
            epicrisis_edited_at="2026-11-10T08:30:00.000Z",
            operative_template_id=5,
            operative_edited_at="2026-11-10T08:30:00.000Z",
        )

        session = HistoryView._to_generated_session(detail)

        self.assertEqual(session.session_id, 42)
        self.assertIs(session.epicrisis_recommendations, epicrisis)
        self.assertIs(session.operative_protocol, protocol)
        self.assertIsNot(
            session.epicrisis_recommendations,
            session.operative_protocol,
        )

    def test_timestamp_displays_utc_date_and_time(self) -> None:
        self.assertEqual(
            format_generation_timestamp("2026-11-10T08:30:00.123Z"),
            "10.11.2026 08:30 UTC",
        )

    def test_invalid_timestamp_is_preserved_for_visibility(self) -> None:
        self.assertEqual(format_generation_timestamp("timestamp invalid"), "timestamp invalid")


class HistoryViewWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tkinter nu poate inițializa Tcl/Tk: {exc}")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "history-view.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)
        self.open_session = Mock(return_value=True)
        self.diagnosis_id, self.intervention_id, self.template_set_id, self.template_ids = (
            self._create_active_template_context()
        )
        self.root = tk.Tk()
        self.root.withdraw()
        self.view = HistoryView(
            self.root,
            connection=self.connection,
            open_session=self.open_session,
        )
        self.view.pack(fill="both", expand=True)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        finally:
            self.connection.close()
            self.temp_dir.cleanup()

    def _create_active_template_context(self) -> tuple[int, int, int, dict[str, int]]:
        diagnosis_cursor = self.connection.execute(
            "INSERT INTO diagnoses (name, name_key) VALUES ('Diagnostic curent', 'diagnostic_curent')"
        )
        diagnosis_id = int(diagnosis_cursor.lastrowid)
        intervention_cursor = self.connection.execute(
            """
            INSERT INTO interventions (diagnosis_id, name, name_key)
            VALUES (?, 'Intervenție curentă', 'interventie_curenta')
            """,
            (diagnosis_id,),
        )
        intervention_id = int(intervention_cursor.lastrowid)
        set_cursor = self.connection.execute(
            "INSERT INTO template_sets (intervention_id, version_no) VALUES (?, 1)",
            (intervention_id,),
        )
        template_set_id = int(set_cursor.lastrowid)
        template_ids: dict[str, int] = {}
        for document_type in (
            "epicrisis_recommendations",
            "operative_protocol",
        ):
            source_docx = f"history-test:{document_type}".encode("utf-8")
            cursor = self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, validation_status, validation_report_json,
                    parser_version
                )
                VALUES (?, ?, ?, ?, ?, 'pending', '{}', 'test-parser-1')
                """,
                (
                    template_set_id,
                    document_type,
                    source_docx,
                    hashlib.sha256(source_docx).hexdigest(),
                    RichDocument().to_json(),
                ),
            )
            template_ids[document_type] = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE templates SET validation_status = 'valid' WHERE id = ?",
                (cursor.lastrowid,),
            )
        self.connection.execute(
            """
            UPDATE template_sets
            SET status = 'active', activated_at = '2026-11-10T08:00:00.000Z'
            WHERE id = ?
            """,
            (template_set_id,),
        )
        return diagnosis_id, intervention_id, template_set_id, template_ids

    def _create_session(
        self,
        *,
        created_at: str,
        diagnosis_snapshot: str = "Diagnostic snapshot la generare",
        intervention_snapshot: str = "Intervenție snapshot la generare",
    ) -> int:
        document_json = RichDocument(
            paragraphs=[Paragraph([TextInline("Conținut de test")])]
        ).to_json()
        return save_generation_session(
            self.connection,
            diagnosis_id=self.diagnosis_id,
            intervention_id=self.intervention_id,
            template_set_id=self.template_set_id,
            diagnosis_label_snapshot=diagnosis_snapshot,
            intervention_label_snapshot=intervention_snapshot,
            documents={
                "epicrisis_recommendations": (
                    self.template_ids["epicrisis_recommendations"],
                    document_json,
                ),
                "operative_protocol": (
                    self.template_ids["operative_protocol"],
                    document_json,
                ),
            },
            created_at=created_at,
        )

    def _select_session(self, session_id: int) -> None:
        self.view.history_tree.selection_set(str(session_id))
        self.view.history_tree.focus(str(session_id))
        # selection_set() is programmatic and does not emit Tk's virtual event.
        # A real mouse/keyboard selection does, so dispatch it for the UI binding.
        self.view.history_tree.event_generate("<<TreeviewSelect>>", when="now")
        self.root.update_idletasks()

    @staticmethod
    def _visible_session_ids(view: HistoryView) -> list[int]:
        return [int(item) for item in view.history_tree.get_children()]

    def test_empty_history_message_and_no_automatic_selection(self) -> None:
        self.assertEqual(self.view.history_tree.get_children(), ())
        self.assertEqual(self.view.history_tree.selection(), ())
        self.assertEqual(
            self.view.status_label.cget("text"),
            "Nu există sesiuni complete în istoric.",
        )

    def test_lists_at_most_ten_sessions_in_repository_order(self) -> None:
        session_ids = [
            self._create_session(created_at=f"2026-11-10T08:{index:02d}:00.000Z")
            for index in range(12)
        ]

        self.assertTrue(self.view.refresh())

        expected = list(reversed(session_ids[-10:]))
        self.assertEqual(self._visible_session_ids(self.view), expected)
        self.assertEqual(len(self.view.history_items), 10)
        self.assertEqual(self.view.history_tree.selection(), ())

    def test_displays_historical_snapshots_not_current_library_names(self) -> None:
        session_id = self._create_session(
            created_at="2026-11-10T08:30:00.123Z",
            diagnosis_snapshot="Diagnostic istoric snapshot",
            intervention_snapshot="Intervenție istoric snapshot",
        )
        self.connection.execute(
            "UPDATE diagnoses SET name = 'Diagnostic redenumit' WHERE id = ?",
            (self.diagnosis_id,),
        )
        self.connection.execute(
            "UPDATE interventions SET name = 'Intervenție redenumită' WHERE id = ?",
            (self.intervention_id,),
        )

        self.assertTrue(self.view.refresh())

        self.assertEqual(self.view.history_tree.item(str(session_id), "values"), (
            "10.11.2026 08:30 UTC",
            "Diagnostic istoric snapshot",
            "Intervenție istoric snapshot",
            "Epicriză + Protocol",
        ))

    def test_refresh_preserves_selection_when_session_remains(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)

        self.assertTrue(self.view.refresh())

        self.assertEqual(self.view.history_tree.selection(), (str(session_id),))
        self.assertEqual(str(self.view.open_button.cget("state")), "normal")

    def test_refresh_clears_selection_when_session_is_no_longer_listed(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)
        self.connection.execute(
            "UPDATE generation_sessions SET status = 'deleting' WHERE id = ?",
            (session_id,),
        )
        self.connection.execute("DELETE FROM generation_sessions WHERE id = ?", (session_id,))

        self.assertTrue(self.view.refresh())

        self.assertEqual(self.view.history_tree.selection(), ())
        self.assertEqual(str(self.view.open_button.cget("state")), "disabled")
        self.assertEqual(self.view.history_tree.get_children(), ())
        self.assertEqual(
            self.view.status_label.cget("text"),
            "Nu există sesiuni complete în istoric.",
        )

    def test_load_error_preserves_existing_rows_and_selection(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)
        previous_values = self.view.history_tree.item(str(session_id), "values")

        with patch(
            "ortodocs.ui.history_view.list_generation_sessions",
            side_effect=sqlite3.OperationalError("temporary read failure"),
        ) as list_sessions:
            self.assertFalse(self.view.refresh())

        list_sessions.assert_called_once_with(self.connection, limit=10)
        self.assertEqual(self.view.history_tree.get_children(), (str(session_id),))
        self.assertEqual(self.view.history_tree.selection(), (str(session_id),))
        self.assertEqual(self.view.history_tree.item(str(session_id), "values"), previous_values)
        self.assertEqual(str(self.view.open_button.cget("state")), "normal")
        self.assertIn("Datele afișate au fost păstrate", self.view.status_label.cget("text"))
        self.assertIn("temporary read failure", self.view.status_label.cget("text"))

    def test_page_uses_the_connection_received_from_application(self) -> None:
        self._create_session(created_at="2026-11-10T08:30:00.000Z")

        with patch(
            "ortodocs.ui.history_view.list_generation_sessions",
            wraps=list_generation_sessions,
        ) as list_sessions:
            self.assertTrue(self.view.refresh())

        self.assertIs(self.view.connection, self.connection)
        list_sessions.assert_called_once_with(self.connection, limit=10)

    def test_on_show_reloads_history(self) -> None:
        with patch(
            "ortodocs.ui.history_view.list_generation_sessions",
            return_value=(),
        ) as list_sessions:
            self.view.on_show()

        list_sessions.assert_called_once_with(self.connection, limit=10)

    def test_open_button_tracks_selection_and_opens_selected_session(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self.assertEqual(str(self.view.open_button.cget("state")), "disabled")

        self._select_session(session_id)
        self.assertEqual(str(self.view.open_button.cget("state")), "normal")

        with patch(
            "ortodocs.ui.history_view.get_generation_session",
            wraps=get_generation_session,
        ) as get_session:
            self.view.open_button.invoke()

        get_session.assert_called_once_with(self.connection, session_id)
        self.open_session.assert_called_once()
        generated_session = self.open_session.call_args.args[0]
        self.assertIsInstance(generated_session, GeneratedSession)
        self.assertEqual(generated_session.session_id, session_id)
        self.assertEqual(
            generated_session.epicrisis_recommendations.to_json(),
            RichDocument.from_json(
                self.connection.execute(
                    """
                    SELECT rich_text_json FROM generated_documents
                    WHERE session_id = ? AND document_type = 'epicrisis_recommendations'
                    """,
                    (session_id,),
                ).fetchone()[0]
            ).to_json(),
        )
        self.assertEqual(
            generated_session.operative_protocol.to_json(),
            RichDocument.from_json(
                self.connection.execute(
                    """
                    SELECT rich_text_json FROM generated_documents
                    WHERE session_id = ? AND document_type = 'operative_protocol'
                    """,
                    (session_id,),
                ).fetchone()[0]
            ).to_json(),
        )
        self.assertIsNot(
            generated_session.epicrisis_recommendations,
            generated_session.operative_protocol,
        )

    def test_double_click_opens_the_row_under_pointer(self) -> None:
        previously_selected_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        pointer_session_id = self._create_session(created_at="2026-11-10T08:31:00.000Z")
        self.view.refresh()
        self.root.geometry("1100x700")
        self.root.deiconify()
        self.root.update()
        self._select_session(previously_selected_id)
        row = self.view.history_tree.bbox(str(pointer_session_id))
        self.assertTrue(row, "rândul trebuie să fie vizibil pentru dublu-click")
        self.assertTrue(self.view.history_tree.bind("<Double-1>"))

        event = SimpleNamespace(y=row[1] + row[3] // 2)
        self.assertEqual(self.view._on_double_click(event), "break")
        self.root.update()

        self.assertEqual(self.view.history_tree.selection(), (str(pointer_session_id),))
        self.open_session.assert_called_once()
        self.assertEqual(self.open_session.call_args.args[0].session_id, pointer_session_id)

    def test_missing_or_incomplete_session_refreshes_history_without_opening(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)
        self.connection.execute(
            "UPDATE generation_sessions SET status = 'deleting' WHERE id = ?",
            (session_id,),
        )
        self.connection.execute("DELETE FROM generation_sessions WHERE id = ?", (session_id,))

        self.assertFalse(self.view.open_selected_session())

        self.open_session.assert_not_called()
        self.assertEqual(self.view.history_tree.get_children(), ())
        self.assertEqual(self.view.history_tree.selection(), ())
        self.assertIn("nu mai este disponibilă", self.view.status_label.cget("text"))
        self.assertIn("actualizată", self.view.status_label.cget("text"))

    def test_persistence_error_is_shown_without_calling_editor_callback(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)

        with patch(
            "ortodocs.ui.history_view.get_generation_session",
            side_effect=GenerationPersistenceError("JSON Rich Text corupt"),
        ):
            self.assertFalse(self.view.open_selected_session())

        self.open_session.assert_not_called()
        self.assertEqual(self.view.history_tree.selection(), (str(session_id),))
        self.assertIn("JSON Rich Text corupt", self.view.status_label.cget("text"))
        self.assertIn("Editorul existent a fost păstrat", self.view.status_label.cget("text"))

    def test_cancelled_editor_replacement_keeps_selection_and_persisted_documents(self) -> None:
        session_id = self._create_session(created_at="2026-11-10T08:30:00.000Z")
        self.view.refresh()
        self._select_session(session_id)
        before = self.connection.execute(
            """
            SELECT document_type, rich_text_json FROM generated_documents
            WHERE session_id = ? ORDER BY document_type
            """,
            (session_id,),
        ).fetchall()
        self.open_session.return_value = False

        self.assertFalse(self.view.open_selected_session())

        self.assertEqual(self.view.history_tree.selection(), (str(session_id),))
        self.assertEqual(
            self.connection.execute(
                """
                SELECT document_type, rich_text_json FROM generated_documents
                WHERE session_id = ? ORDER BY document_type
                """,
                (session_id,),
            ).fetchall(),
            before,
        )
        self.assertEqual(self.open_session.call_args.args[0].session_id, session_id)

    def test_search_filters_history_rows_and_shows_empty_state_when_no_match(self) -> None:
        s1 = self._create_session(
            created_at="2026-11-10T08:30:00.000Z",
            diagnosis_snapshot="Fractură radius",
            intervention_snapshot="Osteosinteză placă",
        )
        s2 = self._create_session(
            created_at="2026-11-10T09:30:00.000Z",
            diagnosis_snapshot="Gonartroză",
            intervention_snapshot="Proteză totală genunchi",
        )
        self.view.refresh()
        self.assertEqual(len(self.view.history_tree.get_children()), 2)

        self.view.search_var.set("gonartroz")
        self.root.update_idletasks()
        self.assertEqual(self.view.history_tree.get_children(), (str(s2),))

        self.view.search_var.set("inexistent")
        self.root.update_idletasks()
        self.assertEqual(self.view.history_tree.get_children(), ())
        self.assertNotEqual(self.view.empty_state_label.winfo_manager(), "")

        self.view.search_var.set("")
        self.root.update_idletasks()
        self.assertEqual(set(self.view.history_tree.get_children()), {str(s1), str(s2)})
        self.assertEqual(self.view.empty_state_label.winfo_manager(), "")


if __name__ == "__main__":
    unittest.main()
