"""Teste complete pentru serviciul intern TemplateSyncService și siguranța tranzacțională."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.services import document_generation, medical_library, template_sets
from ortodocs.services.template_sync import (
    PATIENT_CONFIRMATION_TEXT,
    TemplateSyncError,
    TemplateSyncService,
)
from ortodocs.storage import independent_template_repositories, repositories
from ortodocs.storage.database import connect_database
from ortodocs.storage.generation_repositories import get_generation_session
from ortodocs.storage.migrations import migrate_connection


def _make_docx_bytes(
    text: str,
    *,
    proof_warning: bool = False,
    lang_warning: bool = False,
) -> bytes:
    document = Document()
    paragraph = document.add_paragraph(text)
    if proof_warning:
        marker = OxmlElement("w:proofErr")
        marker.set(qn("w:type"), "spellStart")
        paragraph._p.insert(0, marker)
    if lang_warning:
        run = document.add_paragraph("Text en").runs[0]
        rpr = run._r.get_or_add_rPr()
        lang = OxmlElement("w:lang")
        lang.set(qn("w:val"), "en-US")
        rpr.append(lang)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _write_catalog(folder: Path, payload: object) -> Path:
    catalog_path = folder / "catalog.json"
    catalog_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return catalog_path


class TemplateSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_tmp = Path(self.temp_dir.name)
        self.db_dir = self.root_tmp / "profile"
        self.db_dir.mkdir(parents=True)
        self.database_path = self.db_dir / "ortodocs.db"
        self.connection = connect_database(self.database_path)
        migrate_connection(self.connection, database_path=self.database_path)

        self.sync_dir = self.root_tmp / "word_templates"
        (self.sync_dir / "epicrize").mkdir(parents=True)
        (self.sync_dir / "protocoale").mkdir(parents=True)

        self.service = TemplateSyncService(
            self.connection,
            database_path=self.database_path,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _create_standard_catalog(
        self,
        *,
        epi_text: str = "Epicriză {{sex}} {{varsta}}",
        proto_text: str = "Protocol {{lateralitate}}",
    ) -> dict[str, object]:
        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(
            _make_docx_bytes(epi_text)
        )
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(
            _make_docx_bytes(proto_text)
        )
        payload: dict[str, object] = {
            "catalog_version": 1,
            "diagnoses": [
                {
                    "key": "coxartroza",
                    "name": "Coxartroză bilaterală",
                    "code": "M16.0",
                    "epicrisis_path": "epicrize/coxartroza.docx",
                }
            ],
            "interventions": [
                {
                    "key": "proteza-sold",
                    "diagnosis_key": "coxartroza",
                    "name": "Artroplastie totală șold",
                    "code": "ORT-01",
                    "protocol_path": "protocoale/proteza_sold.docx",
                }
            ],
        }
        _write_catalog(self.sync_dir, payload)
        return payload

    def _snapshot_db_counts(self) -> dict[str, int]:
        tables = (
            "diagnoses",
            "interventions",
            "independent_template_versions",
            "template_sync_entities",
            "template_sync_sources",
            "template_sync_runs",
        )
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    # 1. Configurare și persistență folder sincronizare
    def test_01_configure_and_persist_sync_folder(self) -> None:
        saved = self.service.set_configured_folder(self.sync_dir)
        self.assertEqual(saved, self.sync_dir.resolve())

        reopened = connect_database(self.database_path)
        try:
            second_service = TemplateSyncService(reopened, database_path=self.database_path)
            self.assertEqual(second_service.get_configured_folder(), self.sync_dir.resolve())
        finally:
            reopened.close()

    # 2. Folder configurat lipsă / inaccesibil tratat grațios
    def test_02_configured_missing_folder_handled_gracefully(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        missing_folder = self.root_tmp / "missing_dir"
        with self.assertRaises(TemplateSyncError):
            self.service.set_configured_folder(missing_folder)

        # Ștergem folderul după ce fusese configurat valid.
        for path in sorted(self.sync_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        self.sync_dir.rmdir()

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "folder_unavailable")
        self.assertIn("nu există", outcome.summary_message)
        self.assertEqual(self._snapshot_db_counts()["template_sync_runs"], 0)

    # 3. catalog.json lipsă
    def test_03_catalog_missing_is_reported(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertTrue(any("Lipsește fișierul catalog.json" in issue.message for issue in outcome.issues))
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)

    # 4. catalog.json invalid JSON
    def test_04_catalog_invalid_json_is_reported(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        (self.sync_dir / "catalog.json").write_text("{ invalid json", encoding="utf-8")
        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertIn("JSON UTF-8 valid", outcome.summary_message)
        self.assertEqual(self._snapshot_db_counts()["template_sync_runs"], 0)

    # 5. Cale DOCX invalidă / path traversal / symlink blocat
    def test_05_catalog_invalid_path_and_traversal_blocked(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        for bad_path in ("../outside.docx", "C:/Windows/test.docx", r"\\server\share\a.docx"):
            _write_catalog(
                self.sync_dir,
                {
                    "catalog_version": 1,
                    "diagnoses": [
                        {
                            "key": "d1",
                            "name": "Diag 1",
                            "epicrisis_path": bad_path,
                        }
                    ],
                    "interventions": [],
                },
            )
            report = self.service.scan_and_validate()
            self.assertTrue(report.has_blocking_issues, f"Trebuia blocată calea {bad_path!r}")

        # Test symlink dacă OS/privilegiile permit crearea lui.
        outside_docx = self.root_tmp / "outside.docx"
        outside_docx.write_bytes(_make_docx_bytes("Outside"))
        symlink_path = self.sync_dir / "epicrize" / "link.docx"
        try:
            os.symlink(outside_docx, symlink_path)
        except OSError:
            return
        _write_catalog(
            self.sync_dir,
            {
                "catalog_version": 1,
                "diagnoses": [
                    {
                        "key": "d1",
                        "name": "Diag 1",
                        "epicrisis_path": "epicrize/link.docx",
                    }
                ],
                "interventions": [],
            },
        )
        report = self.service.scan_and_validate()
        self.assertTrue(report.has_blocking_issues)
        self.assertTrue(any("symlink" in issue.message.lower() for issue in report.issues))

    # 6. Duplicate chei/nume/coduri/căi și diagnosis_key invalid blocate
    def test_06_catalog_duplicate_keys_names_paths_and_broken_refs_blocked(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        (self.sync_dir / "epicrize" / "a.docx").write_bytes(_make_docx_bytes("A"))
        (self.sync_dir / "epicrize" / "b.docx").write_bytes(_make_docx_bytes("B"))
        _write_catalog(
            self.sync_dir,
            {
                "catalog_version": 1,
                "diagnoses": [
                    {"key": "dup", "name": "Diag A", "code": "C1", "epicrisis_path": "epicrize/a.docx"},
                    {"key": "dup", "name": "diag   a", "code": "C1", "epicrisis_path": "epicrize/a.docx"},
                ],
                "interventions": [
                    {
                        "key": "i1",
                        "diagnosis_key": "inexistent",
                        "name": "Int 1",
                        "protocol_path": "epicrize/b.docx",
                    }
                ],
            },
        )
        report = self.service.scan_and_validate()
        self.assertTrue(report.has_blocking_issues)
        messages = "\n".join(issue.message for issue in report.issues)
        self.assertIn("duplicată", messages)
        self.assertIn("inexistent", messages)

    # 7. Fișier DOCX lipsă pe disc raportat explicit ca eroare
    def test_07_catalog_missing_docx_file_treated_as_error(self) -> None:
        self.service.set_configured_folder(self.sync_dir)
        _write_catalog(
            self.sync_dir,
            {
                "catalog_version": 1,
                "diagnoses": [
                    {
                        "key": "coxartroza",
                        "name": "Coxartroză",
                        "epicrisis_path": "epicrize/lipsa.docx",
                    }
                ],
                "interventions": [],
            },
        )
        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertTrue(any("lipsește de pe disc" in issue.message for issue in outcome.issues))
        self.assertEqual(self._snapshot_db_counts()["diagnoses"], 0)

    # 8. Template-uri noi valide importate și activate la confirmare
    def test_08_valid_new_templates_imported_and_activated_on_confirm(self) -> None:
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)

        confirm_prompts: list[str] = []

        def confirm(report) -> bool:
            confirm_prompts.append(report.format_confirmation_summary())
            return True

        outcome = self.service.synchronize(confirm_batch=confirm)
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(outcome.scanned_count, 2)
        self.assertEqual(outcome.new_count, 2)
        self.assertEqual(outcome.modified_count, 0)
        self.assertEqual(outcome.unchanged_count, 0)
        self.assertEqual(outcome.imported_count, 2)
        self.assertEqual(outcome.activated_count, 2)
        self.assertTrue(confirm_prompts)
        self.assertIn(PATIENT_CONFIRMATION_TEXT, confirm_prompts[0])

        counts = self._snapshot_db_counts()
        self.assertEqual(counts["diagnoses"], 1)
        self.assertEqual(counts["interventions"], 1)
        self.assertEqual(counts["independent_template_versions"], 2)
        self.assertEqual(counts["template_sync_entities"], 2)
        self.assertEqual(counts["template_sync_sources"], 2)
        self.assertEqual(counts["template_sync_runs"], 1)

        summary = self.service.get_settings_summary(last_outcome=outcome)
        self.assertEqual(summary["active_templates_total"], 2)
        self.assertEqual(summary["active_epicrisis_count"], 1)
        self.assertEqual(summary["active_protocol_count"], 1)

    # 9. Template-uri neschimbate nu creează versiuni noi
    def test_09_unchanged_templates_do_not_create_duplicate_versions(self) -> None:
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)
        first = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(first.status, "applied")

        confirm_called = False

        def unexpected_confirm(_report) -> bool:
            nonlocal confirm_called
            confirm_called = True
            return True

        second = self.service.synchronize(confirm_batch=unexpected_confirm)
        self.assertEqual(second.status, "no_changes")
        self.assertFalse(confirm_called)
        self.assertEqual(second.scanned_count, 2)
        self.assertEqual(second.unchanged_count, 2)
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 2)
        self.assertEqual(self._snapshot_db_counts()["template_sync_runs"], 1)

    # 10. Template modificat creează versiune nouă activă și retrage versiunea veche
    def test_10_modified_template_creates_new_version_and_retires_previous(self) -> None:
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)
        self.service.synchronize(confirm_batch=lambda _r: True)

        # Modificăm doar protocolul operator.
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(
            _make_docx_bytes("Protocol actualizat v2 {{lateralitate}}")
        )
        second = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(second.status, "applied")
        self.assertEqual(second.modified_count, 1)
        self.assertEqual(second.unchanged_count, 1)
        self.assertEqual(second.imported_count, 1)
        self.assertEqual(second.activated_count, 1)

        interventions = medical_library.list_interventions(self.connection, active_only=False)
        proto_versions = template_sets.list_independent_templates(
            self.connection,
            owner_type="intervention",
            owner_id=int(interventions[0]["id"]),
        )
        self.assertEqual([v["version_no"] for v in proto_versions], [2, 1])
        self.assertEqual(proto_versions[0]["status"], "active")
        self.assertEqual(proto_versions[1]["status"], "retired")

    # 11. Înregistrări DB existente absente din catalog rămân intacte
    def test_11_disappeared_or_unlisted_db_records_are_not_deleted(self) -> None:
        legacy_diag_id = medical_library.add_diagnosis(
            self.connection,
            "Diagnostic existent neatins",
            code="EX01",
        )
        legacy_int_id = medical_library.add_intervention(
            self.connection,
            legacy_diag_id,
            "Intervenție existentă neatinsă",
            code="IX01",
        )
        ver = template_sets.import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=legacy_diag_id,
            document_bytes=_make_docx_bytes("Epicriză preexistentă"),
        )
        template_sets.activate_independent_template(
            self.connection,
            int(ver["id"]),
            patient_identification_review_confirmed=True,
        )

        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)
        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "applied")

        self.assertIsNotNone(repositories.get_diagnosis(self.connection, legacy_diag_id))
        self.assertIsNotNone(repositories.get_intervention(self.connection, legacy_int_id))
        self.assertEqual(
            template_sets.get_independent_template_details(self.connection, int(ver["id"]))["status"],
            "active",
        )

    # 12. Diagnostic și intervenție existente refolosite fără duplicare
    def test_12_existing_diagnosis_and_intervention_reused_without_duplication(self) -> None:
        existing_diag_id = medical_library.add_diagnosis(
            self.connection,
            "Coxartroză bilaterală",
            code="M16.0",
        )
        existing_int_id = medical_library.add_intervention(
            self.connection,
            existing_diag_id,
            "Artroplastie totală șold",
            code="ORT-01",
        )

        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)
        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "applied")

        all_diags = medical_library.list_diagnoses(self.connection, active_only=False)
        all_ints = medical_library.list_interventions(self.connection, active_only=False)
        self.assertEqual(len(all_diags), 1)
        self.assertEqual(len(all_ints), 1)
        self.assertEqual(int(all_diags[0]["id"]), existing_diag_id)
        self.assertEqual(int(all_ints[0]["id"]), existing_int_id)

        # Redenumim în DB și re-sincronizăm modificând DOCX -> cheia stabilă mapată îl regăsește tot pe ID-ul existent.
        medical_library.update_diagnosis(
            self.connection,
            existing_diag_id,
            "Coxartroză bilaterală redenumită",
            code="M16.0",
        )
        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(
            _make_docx_bytes("Epicriză nouă mapată prin cheie")
        )
        second = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(second.status, "applied")
        self.assertEqual(len(medical_library.list_diagnoses(self.connection, active_only=False)), 1)

    # 13. Un DOCX invalid blochează întreg lotul
    def test_13_one_invalid_docx_blocks_entire_batch(self) -> None:
        self._create_standard_catalog()
        # Corupem protocolul.
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(b"nu-este-un-docx-valid")
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)
        self.assertEqual(self._snapshot_db_counts()["diagnoses"], 0)

    # 14. Un DOCX cu avertisment (ex: proofing annotation / lang ignored) blochează întreg lotul
    def test_14_one_warning_docx_blocks_entire_batch(self) -> None:
        self._create_standard_catalog()
        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(
            _make_docx_bytes("Epicriză cu proofErr", proof_warning=True)
        )
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertIn("Avertisment", outcome.summary_message)
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)

        # Verificăm și avertisment de tip language_metadata_ignored.
        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(
            _make_docx_bytes("Epicriză cu lang", lang_warning=True)
        )
        outcome_lang = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome_lang.status, "validation_failed")
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)

    # 15. Variabilă rezervată de pacient blochează sincronizarea
    def test_15_patient_variable_in_docx_blocks_sync(self) -> None:
        self._create_standard_catalog(epi_text="Pacient {{nume_pacient}} diagnosticat")
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertTrue(any("nume_pacient" in issue.message for issue in outcome.issues))
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)

    # 16. Anularea confirmării nu modifică nimic în baza de date
    def test_16_batch_cancel_keeps_database_completely_untouched(self) -> None:
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: False)
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(
            self._snapshot_db_counts(),
            {
                "diagnoses": 0,
                "interventions": 0,
                "independent_template_versions": 0,
                "template_sync_entities": 0,
                "template_sync_sources": 0,
                "template_sync_runs": 0,
            },
        )
        backups_dir = self.db_dir / "backups"
        self.assertFalse(backups_dir.exists() and any(backups_dir.iterdir()))

    # 17. Backup SQLite valid creat înainte de scriere și în subdirectorul backups
    def test_17_backup_created_before_db_write(self) -> None:
        # Punem un diagnostic preexistent înainte de sync.
        medical_library.add_diagnosis(self.connection, "Preexistent", code="P00")
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "applied")
        self.assertIsNotNone(outcome.backup_path)
        assert outcome.backup_path is not None
        backup_file = Path(outcome.backup_path)
        self.assertTrue(backup_file.is_file())
        self.assertEqual(backup_file.parent, self.db_dir / "backups")

        backup_conn = sqlite3.connect(backup_file)
        try:
            self.assertEqual(backup_conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            # Backup-ul trebuie să reflecte starea DINAINTE de aplicarea modificărilor.
            self.assertEqual(
                backup_conn.execute("SELECT COUNT(*) FROM independent_template_versions").fetchone()[0],
                0,
            )
            self.assertEqual(
                backup_conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0],
                1,
            )
        finally:
            backup_conn.close()

    # 18. Eroare în mijlocul tranzacției execută ROLLBACK complet
    def test_18_transaction_failure_rolls_back_completely(self) -> None:
        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)

        scan_report = self.service.scan_and_validate()
        self.assertTrue(scan_report.can_apply)

        with patch(
            "ortodocs.storage.template_sync_repositories.insert_sync_run",
            side_effect=RuntimeError("Simulare eroare scriere DB"),
        ):
            outcome = self.service.apply_sync(
                scan_report,
                patient_identification_confirmed=True,
            )

        self.assertEqual(outcome.status, "failed")
        self.assertIn("rollback", outcome.summary_message.lower())
        self.assertEqual(
            self._snapshot_db_counts(),
            {
                "diagnoses": 0,
                "interventions": 0,
                "independent_template_versions": 0,
                "template_sync_entities": 0,
                "template_sync_sources": 0,
                "template_sync_runs": 0,
            },
        )

    # 19. Generarea documentelor folosește cele mai noi versiuni activate după sincronizare
    def test_19_generation_uses_latest_activated_versions_after_sync(self) -> None:
        self._create_standard_catalog(
            epi_text="Epicriză v1",
            proto_text="Protocol v1",
        )
        self.service.set_configured_folder(self.sync_dir)
        self.service.synchronize(confirm_batch=lambda _r: True)

        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(
            _make_docx_bytes("Epicriză v2 actualizată")
        )
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(
            _make_docx_bytes("Protocol v2 actualizat")
        )
        self.service.synchronize(confirm_batch=lambda _r: True)

        diag_id = int(medical_library.list_diagnoses(self.connection)[0]["id"])
        int_id = int(medical_library.list_interventions(self.connection)[0]["id"])
        epi_active = independent_template_repositories.get_active_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=diag_id,
            document_type=template_sets.EPICRISIS_RECOMMENDATIONS,
        )
        proto_active = independent_template_repositories.get_active_independent_template_version(
            self.connection,
            owner_type="intervention",
            owner_id=int_id,
            document_type=template_sets.OPERATIVE_PROTOCOL,
        )
        assert epi_active is not None and proto_active is not None
        epi_content = template_sets.get_independent_template_content(
            self.connection,
            int(epi_active["id"]),
        )
        proto_content = template_sets.get_independent_template_content(
            self.connection,
            int(proto_active["id"]),
        )
        self.assertEqual(epi_active["version_no"], 2)
        self.assertEqual(proto_active["version_no"], 2)
        self.assertIn("Epicriză v2", epi_content["document"].paragraphs[0].inlines[0].text)
        self.assertIn("Protocol v2", proto_content["document"].paragraphs[0].inlines[0].text)

        generated = document_generation.generate_documents(self.connection, int_id, {})
        detail = get_generation_session(self.connection, generated.session_id)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.epicrisis_template_version_id, int(epi_active["id"]))
        self.assertEqual(detail.operative_template_version_id, int(proto_active["id"]))
        self.assertIn("Epicriză v2", generated.epicrisis_recommendations.paragraphs[0].inlines[0].text)
        self.assertIn("Protocol v2", generated.operative_protocol.paragraphs[0].inlines[0].text)

    # 20. Istoricul păstrează versiunea veche pentru documentele deja generate
    def test_20_history_preserves_old_version_snapshot_after_new_sync(self) -> None:
        self._create_standard_catalog(
            epi_text="Epicriză v1 istorică",
            proto_text="Protocol v1 istoric",
        )
        self.service.set_configured_folder(self.sync_dir)
        self.service.synchronize(confirm_batch=lambda _r: True)

        diag = medical_library.list_diagnoses(self.connection)[0]
        interv = medical_library.list_interventions(self.connection)[0]
        epi_v1 = independent_template_repositories.get_active_independent_template_version(
            self.connection,
            owner_type="diagnosis",
            owner_id=int(diag["id"]),
            document_type=template_sets.EPICRISIS_RECOMMENDATIONS,
        )
        proto_v1 = independent_template_repositories.get_active_independent_template_version(
            self.connection,
            owner_type="intervention",
            owner_id=int(interv["id"]),
            document_type=template_sets.OPERATIVE_PROTOCOL,
        )
        assert epi_v1 is not None and proto_v1 is not None
        epi_v1_id = int(epi_v1["id"])
        proto_v1_id = int(proto_v1["id"])

        generated_v1 = document_generation.generate_documents(
            self.connection,
            int(interv["id"]),
            {},
        )

        # Actualizăm și sincronizăm v2.
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(
            _make_docx_bytes("Protocol v2 nou")
        )
        self.service.synchronize(confirm_batch=lambda _r: True)

        detail = get_generation_session(self.connection, generated_v1.session_id)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.epicrisis_template_version_id, epi_v1_id)
        self.assertEqual(detail.operative_template_version_id, proto_v1_id)
        self.assertIn(
            "Protocol v1 istoric",
            detail.operative_protocol.paragraphs[0].inlines[0].text,
        )

    # 21. Proprietar inactiv în DB blochează activarea/sincronizarea modificărilor pentru el
    def test_21_inactive_owner_in_db_blocks_template_sync(self) -> None:
        diag_id = medical_library.add_diagnosis(
            self.connection,
            "Coxartroză bilaterală",
            code="M16.0",
        )
        medical_library.set_diagnosis_active(self.connection, diag_id, False)

        self._create_standard_catalog()
        self.service.set_configured_folder(self.sync_dir)

        outcome = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(outcome.status, "validation_failed")
        self.assertTrue(any("inactiv" in issue.message.lower() for issue in outcome.issues))
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 0)

    # 26. Adoptarea versiunilor identice importate manual, fără duplicare, apoi repetare și modificare ulterioară
    def test_26_adopt_manual_versions_without_duplication_then_repeat_and_modify(self) -> None:
        diag_id = medical_library.add_diagnosis(
            self.connection,
            "Coxartroză bilaterală",
            code="M16.0",
        )
        interv_id = medical_library.add_intervention(
            self.connection,
            diag_id,
            "Artroplastie totală șold",
            code="ORT-01",
        )
        epi_bytes = _make_docx_bytes("Epicriză manuală identică {{sex}}")
        proto_bytes = _make_docx_bytes("Protocol manual identic {{lateralitate}}")

        manual_epi = template_sets.import_independent_template(
            self.connection,
            owner_type="diagnosis",
            owner_id=diag_id,
            document_bytes=epi_bytes,
        )
        template_sets.activate_independent_template(
            self.connection,
            int(manual_epi["id"]),
            patient_identification_review_confirmed=True,
        )
        manual_proto = template_sets.import_independent_template(
            self.connection,
            owner_type="intervention",
            owner_id=interv_id,
            document_bytes=proto_bytes,
        )
        template_sets.activate_independent_template(
            self.connection,
            int(manual_proto["id"]),
            patient_identification_review_confirmed=True,
        )

        # Punem exact aceleași fișiere DOCX în folderul de sincronizare.
        (self.sync_dir / "epicrize" / "coxartroza.docx").write_bytes(epi_bytes)
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(proto_bytes)
        _write_catalog(
            self.sync_dir,
            {
                "catalog_version": 1,
                "diagnoses": [
                    {
                        "key": "coxartroza",
                        "name": "Coxartroză bilaterală",
                        "code": "M16.0",
                        "epicrisis_path": "epicrize/coxartroza.docx",
                    }
                ],
                "interventions": [
                    {
                        "key": "proteza-sold",
                        "diagnosis_key": "coxartroza",
                        "name": "Artroplastie totală șold",
                        "code": "ORT-01",
                        "protocol_path": "protocoale/proteza_sold.docx",
                    }
                ],
            },
        )
        self.service.set_configured_folder(self.sync_dir)

        scan = self.service.scan_and_validate()
        self.assertEqual(scan.scanned_count, 2)
        self.assertEqual(scan.new_count, 0)
        self.assertEqual(scan.modified_count, 0)
        self.assertEqual(scan.unchanged_count, 2)
        self.assertEqual(scan.linked_count, 2)
        self.assertTrue(scan.can_apply)

        confirm_called = False

        def unexpected_confirm(_report) -> bool:
            nonlocal confirm_called
            confirm_called = True
            return True

        outcome = self.service.synchronize(confirm_batch=unexpected_confirm)
        self.assertFalse(confirm_called)
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(outcome.imported_count, 0)
        self.assertEqual(outcome.activated_count, 0)
        self.assertEqual(outcome.linked_count, 2)
        self.assertEqual(outcome.unchanged_count, 2)

        # Nu s-au creat versiuni artificiale și sursa DOCX istorică a rămas intactă.
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 2)
        self.assertEqual(self._snapshot_db_counts()["template_sync_entities"], 2)
        self.assertEqual(self._snapshot_db_counts()["template_sync_sources"], 2)

        epi_row = self.connection.execute(
            """
            SELECT itv.id, itv.version_no, itv.status, itv.source_sha256, itv.source_docx,
                   tss.catalog_key, tss.relative_path
            FROM independent_template_versions AS itv
            JOIN template_sync_sources AS tss ON tss.template_version_id = itv.id
            WHERE itv.id = ?
            """,
            (int(manual_epi["id"]),),
        ).fetchone()
        self.assertIsNotNone(epi_row)
        assert epi_row is not None
        self.assertEqual(epi_row["version_no"], 1)
        self.assertEqual(epi_row["status"], "active")
        self.assertEqual(epi_row["catalog_key"], "coxartroza")
        self.assertEqual(epi_row["relative_path"], "epicrize/coxartroza.docx")
        self.assertEqual(epi_row["source_sha256"], manual_epi["source_sha256"])
        self.assertEqual(bytes(epi_row["source_docx"]), epi_bytes)

        # Repetarea sincronizării recunoaște documentele ca neschimbate fără duplicate.
        second = self.service.synchronize(confirm_batch=unexpected_confirm)
        self.assertEqual(second.status, "no_changes")
        self.assertEqual(second.linked_count, 0)
        self.assertEqual(second.unchanged_count, 2)
        self.assertEqual(self._snapshot_db_counts()["independent_template_versions"], 2)
        self.assertEqual(self._snapshot_db_counts()["template_sync_runs"], 1)

        # Modificarea ulterioară a fișierului Word creează și activează v2, retrăgând v1.
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(
            _make_docx_bytes("Protocol modificat ulterior v2 {{lateralitate}}")
        )
        third = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(third.status, "applied")
        self.assertEqual(third.modified_count, 1)
        self.assertEqual(third.imported_count, 1)
        self.assertEqual(third.activated_count, 1)
        self.assertEqual(third.unchanged_count, 1)

        proto_versions = template_sets.list_independent_templates(
            self.connection,
            owner_type="intervention",
            owner_id=interv_id,
        )
        self.assertEqual([v["version_no"] for v in proto_versions], [2, 1])
        self.assertEqual(proto_versions[0]["status"], "active")
        self.assertEqual(proto_versions[1]["status"], "retired")
        self.assertEqual(int(proto_versions[1]["id"]), int(manual_proto["id"]))

    # 27. Sincronizare template modificat cu text lipit din ChatGPT (HTMLCode + rFonts + sz + szCs), fără duplicate și cu generare fidelă
    def test_27_sync_chatgpt_pasted_modified_template_no_duplicates_and_generates(self) -> None:
        from docx.enum.style import WD_STYLE_TYPE

        self._create_standard_catalog(
            epi_text="Epicriză simplă",
            proto_text="Protocol inițial",
        )
        self.service.set_configured_folder(self.sync_dir)
        first = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(first.status, "applied")

        # Modificăm protocolul cu un paragraf ce reproduce exact lipirea din ChatGPT (HTMLCode + eastAsiaTheme).
        doc = Document()
        doc.add_paragraph("Protocol inițial")
        html_code = doc.styles.add_style("HTMLCode", WD_STYLE_TYPE.CHARACTER)
        html_code.base_style = doc.styles["Default Paragraph Font"]
        rpr = html_code._element.get_or_add_rPr()
        rf = OxmlElement("w:rFonts")
        rf.set(qn("w:ascii"), "Courier New")
        rf.set(qn("w:eastAsia"), "Times New Roman")
        rf.set(qn("w:hAnsi"), "Courier New")
        rf.set(qn("w:cs"), "Courier New")
        rpr.append(rf)
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), "20")
        rpr.append(sz)
        sz_cs = OxmlElement("w:szCs")
        sz_cs.set(qn("w:val"), "20")
        rpr.append(sz_cs)

        p = doc.add_paragraph()
        run = p.add_run("TEST CHATGPT ")
        run.style = html_code
        direct_rf = OxmlElement("w:rFonts")
        direct_rf.set(qn("w:eastAsiaTheme"), "minorEastAsia")
        run._r.get_or_add_rPr().append(direct_rf)

        stream = BytesIO()
        doc.save(stream)
        (self.sync_dir / "protocoale" / "proteza_sold.docx").write_bytes(stream.getvalue())

        second = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(second.status, "applied")
        self.assertEqual(second.modified_count, 1)
        self.assertEqual(second.activated_count, 1)

        # Repetarea sincronizării nu creează versiuni duplicate.
        repeat = self.service.synchronize(confirm_batch=lambda _r: True)
        self.assertEqual(repeat.status, "no_changes")
        int_id = int(medical_library.list_interventions(self.connection)[0]["id"])
        proto_versions = template_sets.list_independent_templates(
            self.connection,
            owner_type="intervention",
            owner_id=int_id,
        )
        self.assertEqual([v["version_no"] for v in proto_versions], [2, 1])
        self.assertEqual(proto_versions[0]["status"], "active")
        self.assertEqual(proto_versions[1]["status"], "retired")

        # Generarea folosește v2 și păstrează r_fonts, sz și sz_cs în documentul generat.
        generated = document_generation.generate_documents(self.connection, int_id, {})
        detail = get_generation_session(self.connection, generated.session_id)
        assert detail is not None
        self.assertEqual(detail.operative_template_version_id, int(proto_versions[0]["id"]))
        pasted_inline = detail.operative_protocol.paragraphs[1].inlines[0]
        self.assertEqual(
            pasted_inline.r_fonts,
            {
                "ascii": "Courier New",
                "hAnsi": "Courier New",
                "cs": "Courier New",
                "eastAsiaTheme": "minorEastAsia",
            },
        )
        self.assertEqual(pasted_inline.font_size_half_points, 20)
        self.assertEqual(pasted_inline.complex_font_size_half_points, 20)


if __name__ == "__main__":
    unittest.main()
