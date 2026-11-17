"""Teste pentru operațiile serviciului bibliotecii medicale OrtoDocs."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.services import medical_library as library
from ortodocs.services.medical_library import (
    FieldOptionError,
    MedicalLibraryDuplicateError,
    MedicalLibraryIntegrityError,
    MedicalLibraryNotFoundError,
    MedicalLibraryRelationshipError,
    MedicalLibraryValidationError,
    add_diagnosis,
    add_field_option,
    add_field_options,
    add_intervention,
    create_variable,
    create_variable_version,
    list_diagnoses,
    list_field_options,
    list_interventions,
    list_variables,
    normalize_name,
    normalize_name_key,
    rollback_variable_version,
    set_current_variable_version,
    set_diagnosis_active,
    set_diagnosis_sort_order,
    set_intervention_active,
    set_intervention_sort_order,
    update_diagnosis,
    update_intervention,
)
from ortodocs.storage.database import connect_database
from ortodocs.storage.migrations import initialize_database


class MedicalLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "medical-library-test.db"
        initialize_database(self.database_path)
        self.connection = connect_database(self.database_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def make_diagnosis(self, name: str = "Fractură col femural") -> int:
        return add_diagnosis(self.connection, name)

    def make_intervention(
        self,
        diagnosis_id: int,
        name: str = "Proteza bipolară",
    ) -> int:
        return add_intervention(self.connection, diagnosis_id, name)

    def test_name_normalization_collapses_spaces_and_casefolds_key(self) -> None:
        self.assertEqual(normalize_name("  Fractură   COL femural  "), "Fractură COL femural")
        self.assertEqual(normalize_name_key("  Fractură   COL femural  "), "fractură col femural")
        with self.assertRaises(MedicalLibraryValidationError):
            normalize_name(" \t\n ")

    def test_unicode_canonical_diagnosis_names_are_duplicates(self) -> None:
        composed = "Fractură col femural"
        decomposed_uppercase = unicodedata.normalize("NFD", composed.upper())

        self.assertEqual(normalize_name(decomposed_uppercase), composed.upper())
        self.assertEqual(
            normalize_name_key(composed),
            normalize_name_key(decomposed_uppercase),
        )
        add_diagnosis(self.connection, composed)
        with self.assertRaises(MedicalLibraryDuplicateError):
            add_diagnosis(self.connection, decomposed_uppercase)

        no_diacritics = "Fractura col femural"
        self.assertNotEqual(normalize_name_key(composed), normalize_name_key(no_diacritics))
        add_diagnosis(self.connection, no_diacritics)
        self.assertEqual(len(list_diagnoses(self.connection)), 2)

    def test_unicode_canonical_intervention_names_are_duplicates(self) -> None:
        diagnosis_id = self.make_diagnosis()
        composed = "Tijă cefalomedulară"
        decomposed_uppercase = unicodedata.normalize("NFD", composed.upper())

        self.assertEqual(normalize_name(decomposed_uppercase), composed.upper())
        self.assertEqual(
            normalize_name_key(composed),
            normalize_name_key(decomposed_uppercase),
        )
        add_intervention(self.connection, diagnosis_id, composed)
        with self.assertRaises(MedicalLibraryDuplicateError):
            add_intervention(self.connection, diagnosis_id, decomposed_uppercase)

        no_diacritics = "Tija cefalomedulara"
        self.assertNotEqual(normalize_name_key(composed), normalize_name_key(no_diacritics))
        add_intervention(self.connection, diagnosis_id, no_diacritics)
        self.assertEqual(len(list_interventions(self.connection, diagnosis_id=diagnosis_id)), 2)

    def test_diagnosis_add_and_listing(self) -> None:
        later_id = add_diagnosis(
            self.connection,
            "  Fractură   Col femural ",
            code=" S72 ",
            sort_order=20,
        )
        earlier_id = add_diagnosis(self.connection, "Luxație", sort_order=10)

        diagnoses = list_diagnoses(self.connection)
        self.assertEqual([row["id"] for row in diagnoses], [earlier_id, later_id])
        self.assertEqual(diagnoses[1]["name"], "Fractură Col femural")
        self.assertEqual(diagnoses[1]["name_key"], "fractură col femural")
        self.assertEqual(diagnoses[1]["code"], "S72")

    def test_diagnosis_update_and_sort_order(self) -> None:
        diagnosis_id = add_diagnosis(self.connection, "Diagnostic inițial", code="D1")
        update_diagnosis(self.connection, diagnosis_id, "Diagnostic nou", code=" D2 ")
        set_diagnosis_sort_order(self.connection, diagnosis_id, -3)

        row = list_diagnoses(self.connection)[0]
        self.assertEqual(row["name"], "Diagnostic nou")
        self.assertEqual(row["name_key"], "diagnostic nou")
        self.assertEqual(row["code"], "D2")
        self.assertEqual(row["sort_order"], -3)

    def test_update_diagnosis_code_omission_none_and_value(self) -> None:
        diagnosis_id = add_diagnosis(self.connection, "Diagnostic inițial", code="D1")

        update_diagnosis(self.connection, diagnosis_id, "Diagnostic redenumit")
        self.assertEqual(list_diagnoses(self.connection)[0]["code"], "D1")

        update_diagnosis(self.connection, diagnosis_id, "Diagnostic fără cod", code=None)
        self.assertIsNone(list_diagnoses(self.connection)[0]["code"])

        update_diagnosis(self.connection, diagnosis_id, "Diagnostic cu cod", code="D2")
        self.assertEqual(list_diagnoses(self.connection)[0]["code"], "D2")

    def test_diagnosis_duplicates_are_detected_after_normalization(self) -> None:
        add_diagnosis(self.connection, "Fractură   col femural")
        with self.assertRaises(MedicalLibraryDuplicateError):
            add_diagnosis(self.connection, "  FRACTURĂ col FEMURAL  ")

        diagnosis_id = add_diagnosis(self.connection, "Luxație")
        with self.assertRaises(MedicalLibraryDuplicateError):
            update_diagnosis(self.connection, diagnosis_id, " FRACTURĂ   COL FEMURAL ")

    def test_diagnosis_can_be_deactivated_and_reactivated_without_breaking_links(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = self.make_intervention(diagnosis_id)

        set_diagnosis_active(self.connection, diagnosis_id, False)
        self.assertEqual(list_diagnoses(self.connection, active_only=True), [])
        set_diagnosis_active(self.connection, diagnosis_id, True)
        self.assertEqual(len(list_diagnoses(self.connection, active_only=True)), 1)
        self.assertEqual(
            list_interventions(self.connection, diagnosis_id=diagnosis_id)[0]["id"],
            intervention_id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM diagnoses WHERE id = ?", (diagnosis_id,))

    def test_intervention_listing_can_be_filtered_by_diagnosis(self) -> None:
        first_diagnosis = self.make_diagnosis("Diagnostic unu")
        second_diagnosis = self.make_diagnosis("Diagnostic doi")
        first_intervention = self.make_intervention(first_diagnosis, "Intervenție A")
        second_intervention = self.make_intervention(second_diagnosis, "Intervenție B")

        all_rows = list_interventions(self.connection)
        filtered = list_interventions(self.connection, diagnosis_id=first_diagnosis)
        self.assertEqual(len(all_rows), 2)
        self.assertEqual([row["id"] for row in filtered], [first_intervention])
        self.assertEqual(all_rows[1]["id"], second_intervention)
        with self.assertRaises(MedicalLibraryNotFoundError):
            list_interventions(self.connection, diagnosis_id=9999)

    def test_intervention_duplicates_are_rejected_within_diagnosis(self) -> None:
        diagnosis_id = self.make_diagnosis()
        self.make_intervention(diagnosis_id, "Tijă   cefalomedulară")
        with self.assertRaises(MedicalLibraryDuplicateError):
            self.make_intervention(diagnosis_id, " TIJĂ cefalomedulară ")

    def test_same_intervention_name_is_allowed_for_different_diagnoses(self) -> None:
        first_diagnosis = self.make_diagnosis("Diagnostic unu")
        second_diagnosis = self.make_diagnosis("Diagnostic doi")
        first_id = self.make_intervention(first_diagnosis, "Reducere deschisă")
        second_id = self.make_intervention(second_diagnosis, "Reducere deschisă")
        self.assertNotEqual(first_id, second_id)

    def test_intervention_update_and_sort_order(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = add_intervention(
            self.connection,
            diagnosis_id,
            "Intervenție veche",
            code="I1",
        )
        update_intervention(self.connection, intervention_id, "Intervenție nouă", code=" I2 ")
        set_intervention_sort_order(self.connection, intervention_id, 7)
        row = list_interventions(self.connection, diagnosis_id=diagnosis_id)[0]
        self.assertEqual(row["name"], "Intervenție nouă")
        self.assertEqual(row["name_key"], "intervenție nouă")
        self.assertEqual(row["code"], "I2")
        self.assertEqual(row["sort_order"], 7)

    def test_update_intervention_code_omission_none_and_value(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = add_intervention(
            self.connection,
            diagnosis_id,
            "Intervenție inițială",
            code="I1",
        )

        update_intervention(self.connection, intervention_id, "Intervenție redenumită")
        self.assertEqual(
            list_interventions(self.connection, diagnosis_id=diagnosis_id)[0]["code"],
            "I1",
        )

        update_intervention(
            self.connection,
            intervention_id,
            "Intervenție fără cod",
            code=None,
        )
        self.assertIsNone(
            list_interventions(self.connection, diagnosis_id=diagnosis_id)[0]["code"]
        )

        update_intervention(
            self.connection,
            intervention_id,
            "Intervenție cu cod",
            code="I2",
        )
        self.assertEqual(
            list_interventions(self.connection, diagnosis_id=diagnosis_id)[0]["code"],
            "I2",
        )

    def test_intervention_can_be_deactivated_and_reactivated(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = self.make_intervention(diagnosis_id)
        set_intervention_active(self.connection, intervention_id, False)
        self.assertEqual(list_interventions(self.connection, active_only=True), [])
        set_intervention_active(self.connection, intervention_id, True)
        self.assertEqual(
            [row["id"] for row in list_interventions(self.connection, active_only=True)],
            [intervention_id],
        )

    def test_existing_intervention_cannot_move_to_another_diagnosis(self) -> None:
        first_diagnosis = self.make_diagnosis("Diagnostic unu")
        second_diagnosis = self.make_diagnosis("Diagnostic doi")
        intervention_id = self.make_intervention(first_diagnosis)

        with self.assertRaises(MedicalLibraryRelationshipError):
            update_intervention(
                self.connection,
                intervention_id,
                "Proteza bipolară",
                diagnosis_id=second_diagnosis,
            )

        row = list_interventions(self.connection, diagnosis_id=first_diagnosis)[0]
        self.assertEqual(row["id"], intervention_id)
        self.assertEqual(row["diagnosis_id"], first_diagnosis)
        self.assertEqual(list_interventions(self.connection, diagnosis_id=second_diagnosis), [])

    def test_first_variable_version_is_current(self) -> None:
        field_id = create_variable(
            self.connection,
            "data_interventiei",
            " Data intervenției ",
            "date",
            "common",
        )
        rows = list_variables(self.connection, variable_key="data_interventiei")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], field_id)
        self.assertEqual(rows[0]["version"], 1)
        self.assertEqual(rows[0]["is_current"], 1)
        self.assertEqual(rows[0]["label"], "Data intervenției")
        self.assertIsNone(rows[0]["diagnosis_id"])
        self.assertIsNone(rows[0]["intervention_id"])

    def test_variable_versions_are_sequential_immutable_and_one_remains_current(self) -> None:
        first_id = create_variable(
            self.connection,
            "camp_test",
            "Eticheta unu",
            "text",
            "common",
        )
        second_id = create_variable_version(
            self.connection,
            "camp_test",
            "Eticheta doi",
            "number",
            "common",
        )
        rows = list_variables(self.connection, variable_key="camp_test")
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual([row["is_current"] for row in rows], [1, 0])
        self.assertEqual(rows[0]["label"], "Eticheta unu")
        self.assertEqual(rows[0]["data_type"], "text")
        self.assertEqual(rows[1]["id"], second_id)
        self.assertEqual(
            [row["id"] for row in list_variables(self.connection, current_only=True)],
            [first_id],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE field_definitions SET label = 'Editare interzisă' WHERE id = ?",
                (first_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM field_definitions WHERE id = ?", (first_id,))

    def test_variable_version_can_be_switched_and_rolled_back(self) -> None:
        create_variable(self.connection, "versiune", "Eticheta 1", "text", "common")
        create_variable_version(
            self.connection,
            "versiune",
            "Eticheta 2",
            "number",
            "common",
        )
        create_variable_version(
            self.connection,
            "versiune",
            "Eticheta 3",
            "date",
            "common",
        )

        set_current_variable_version(self.connection, "versiune", 3)
        current = list_variables(self.connection, variable_key="versiune", current_only=True)
        self.assertEqual([(row["version"], row["is_current"]) for row in current], [(3, 1)])

        rollback_variable_version(self.connection, "versiune", 1)
        rows = list_variables(self.connection, variable_key="versiune")
        self.assertEqual([row["is_current"] for row in rows], [1, 0, 0])
        self.assertEqual([row["version"] for row in list_variables(
            self.connection, variable_key="versiune", current_only=True
        )], [1])
        with self.assertRaises(MedicalLibraryValidationError):
            rollback_variable_version(self.connection, "versiune", 2)

    def test_failed_current_version_change_rolls_back(self) -> None:
        first_id = create_variable(self.connection, "atomic", "Eticheta 1", "text", "common")
        create_variable_version(self.connection, "atomic", "Eticheta 2", "date", "common")
        self.connection.execute(
            """
            CREATE TRIGGER fail_current_field_update
            BEFORE UPDATE OF is_current ON field_definitions
            WHEN NEW.is_current = 1 AND OLD.is_current = 0
            BEGIN
                SELECT RAISE(ABORT, 'injected current-version failure');
            END
            """
        )

        with self.assertRaises(MedicalLibraryIntegrityError):
            set_current_variable_version(self.connection, "atomic", 2)
        rows = list_variables(self.connection, variable_key="atomic")
        self.assertEqual([row["id"] for row in rows if row["is_current"]], [first_id])
        self.assertFalse(self.connection.in_transaction)

    def test_new_variable_version_refuses_a_key_without_current_version(self) -> None:
        create_variable(self.connection, "fara_curent", "Eticheta", "text", "common")
        self.connection.execute(
            "UPDATE field_definitions SET is_current = 0 WHERE variable_key = 'fara_curent'"
        )

        with self.assertRaises(MedicalLibraryIntegrityError):
            create_variable_version(
                self.connection,
                "fara_curent",
                "Eticheta nouă",
                "number",
                "common",
            )
        self.assertEqual(len(list_variables(self.connection, variable_key="fara_curent")), 1)

    def test_common_diagnosis_and_intervention_scopes_are_distinct(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = self.make_intervention(diagnosis_id)
        common_id = create_variable(self.connection, "camp_comun", "Comun", "text", "common")
        diagnosis_field_id = create_variable(
            self.connection,
            "camp_diagnostic",
            "Diagnostic",
            "text",
            "diagnosis",
            diagnosis_id=diagnosis_id,
        )
        intervention_field_id = create_variable(
            self.connection,
            "camp_interventie",
            "Intervenție",
            "text",
            "intervention",
            intervention_id=intervention_id,
        )

        rows = {row["id"]: row for row in list_variables(self.connection)}
        self.assertEqual((rows[common_id]["diagnosis_id"], rows[common_id]["intervention_id"]), (None, None))
        self.assertEqual(
            (rows[diagnosis_field_id]["diagnosis_id"], rows[diagnosis_field_id]["intervention_id"]),
            (diagnosis_id, None),
        )
        self.assertEqual(
            (rows[intervention_field_id]["diagnosis_id"], rows[intervention_field_id]["intervention_id"]),
            (None, intervention_id),
        )
        self.assertEqual(
            {row["id"] for row in list_variables(self.connection, scope="diagnosis")},
            {diagnosis_field_id},
        )

    def test_scope_change_is_saved_as_a_new_field_version(self) -> None:
        diagnosis_id = self.make_diagnosis()
        create_variable(self.connection, "scope_versionat", "Câmp comun", "text", "common")
        create_variable_version(
            self.connection,
            "scope_versionat",
            "Câmp specific",
            "text",
            "diagnosis",
            diagnosis_id=diagnosis_id,
        )

        rows = list_variables(self.connection, variable_key="scope_versionat")
        self.assertEqual([row["scope"] for row in rows], ["common", "diagnosis"])
        self.assertIsNone(rows[0]["diagnosis_id"])
        self.assertEqual(rows[1]["diagnosis_id"], diagnosis_id)
        self.assertEqual([row["is_current"] for row in rows], [1, 0])

    def test_invalid_scope_reference_combinations_are_rejected(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = self.make_intervention(diagnosis_id)
        invalid_cases = [
            ("common", diagnosis_id, None),
            ("common", None, intervention_id),
            ("diagnosis", None, None),
            ("diagnosis", diagnosis_id, intervention_id),
            ("intervention", diagnosis_id, None),
            ("intervention", None, None),
        ]
        for index, (scope, current_diagnosis, current_intervention) in enumerate(invalid_cases):
            with self.subTest(scope=scope, diagnosis_id=current_diagnosis, intervention_id=current_intervention):
                with self.assertRaises(MedicalLibraryValidationError):
                    create_variable(
                        self.connection,
                        f"invalid_{index}",
                        "Etichetă",
                        "text",
                        scope,
                        diagnosis_id=current_diagnosis,
                        intervention_id=current_intervention,
                    )

    def test_invalid_variable_key_type_and_scope_are_rejected(self) -> None:
        invalid_cases = [
            ("Data", "text", "common"),
            ("_data", "text", "common"),
            ("data-cu-cratima", "text", "common"),
            ("data", "unknown", "common"),
            ("data", "text", "unknown"),
        ]
        for index, (key, data_type, scope) in enumerate(invalid_cases):
            with self.subTest(key=key, data_type=data_type, scope=scope):
                with self.assertRaises(MedicalLibraryValidationError):
                    create_variable(
                        self.connection,
                        f"invalid{index}" if key == "data" else key,
                        "Etichetă",
                        data_type,
                        scope,
                    )

    def test_patient_name_variable_keys_are_reserved_for_all_versions(self) -> None:
        reserved_keys = (
            "nume_pacient",
            "numele_pacientului",
            "prenume_pacient",
            "nume_prenume_pacient",
            "nume_si_prenume_pacient",
            "numele_si_prenumele_pacientului",
            "patient_name",
            "patient_full_name",
            "full_patient_name",
            "patient_first_name",
            "patient_last_name",
            "patient_given_name",
            "patient_surname",
        )
        for key in reserved_keys:
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    MedicalLibraryValidationError,
                    "numele pacientului",
                ):
                    create_variable(
                        self.connection,
                        key,
                        "Nume",
                        "text",
                        "common",
                    )

        with self.assertRaisesRegex(
            MedicalLibraryValidationError,
            "numele pacientului",
        ):
            create_variable_version(
                self.connection,
                "nume_pacient",
                "Nume",
                "text",
                "common",
            )

    def test_scope_references_must_exist(self) -> None:
        with self.assertRaises(MedicalLibraryNotFoundError):
            create_variable(
                self.connection,
                "camp_diag_inexistent",
                "Diagnostic",
                "text",
                "diagnosis",
                diagnosis_id=9999,
            )
        with self.assertRaises(MedicalLibraryNotFoundError):
            create_variable(
                self.connection,
                "camp_interv_inexistent",
                "Intervenție",
                "text",
                "intervention",
                intervention_id=9999,
            )

    def test_select_field_options_are_version_specific_and_ordered(self) -> None:
        first_id = create_variable(
            self.connection,
            "tip_selectie",
            "Tip",
            "select",
            "common",
        )
        add_field_option(self.connection, first_id, "b", "Opțiunea B", sort_order=2)
        add_field_option(self.connection, first_id, "a", "Opțiunea A", sort_order=1)
        options = list_field_options(self.connection, first_id)
        self.assertEqual([row["option_value"] for row in options], ["a", "b"])

        second_id = create_variable_version(
            self.connection,
            "tip_selectie",
            "Tip nou",
            "select",
            "common",
        )
        self.assertEqual(list_field_options(self.connection, second_id), [])
        self.assertEqual(len(list_field_options(self.connection, first_id)), 2)

    def test_options_are_rejected_for_text_number_and_date_fields(self) -> None:
        for data_type in ("text", "number", "date"):
            with self.subTest(data_type=data_type):
                field_id = create_variable(
                    self.connection,
                    f"camp_{data_type}",
                    f"Câmp {data_type}",
                    data_type,
                    "common",
                )
                with self.assertRaises(FieldOptionError):
                    add_field_option(self.connection, field_id, "x", "Opțiune")
                self.assertEqual(list_field_options(self.connection, field_id), [])

    def test_empty_option_value_or_label_is_rejected(self) -> None:
        field_id = create_variable(self.connection, "alegere", "Alegere", "select", "common")
        with self.assertRaises(MedicalLibraryValidationError):
            add_field_option(self.connection, field_id, "   ", "Etichetă")
        with self.assertRaises(MedicalLibraryValidationError):
            add_field_option(self.connection, field_id, "valoare", " \t ")
        self.assertEqual(list_field_options(self.connection, field_id), [])

    def test_options_cannot_be_added_after_template_set_is_activated(self) -> None:
        diagnosis_id = self.make_diagnosis()
        intervention_id = self.make_intervention(diagnosis_id)
        field_id = create_variable(
            self.connection,
            "tip_interventie",
            "Tip intervenție",
            "select",
            "common",
        )
        original_options = add_field_options(
            self.connection,
            field_id,
            [("standard", "Standard", 0), ("revizie", "Revizie", 1)],
        )

        template_set_id = self.connection.execute(
            "INSERT INTO template_sets (intervention_id, version_no) VALUES (?, 1)",
            (intervention_id,),
        ).lastrowid
        template_ids: dict[str, int] = {}
        for document_type in (
            "epicrisis_recommendations",
            "operative_protocol",
        ):
            source_docx = f"test-template-{document_type}".encode("utf-8")
            source_sha256 = hashlib.sha256(source_docx).hexdigest()
            template_id = self.connection.execute(
                """
                INSERT INTO templates (
                    template_set_id, document_type, source_docx, source_sha256,
                    parsed_content_json, parser_version
                )
                VALUES (?, ?, ?, ?, '{"paragraphs":[]}', 'test-parser-1')
                """,
                (template_set_id, document_type, source_docx, source_sha256),
            ).lastrowid
            self.connection.execute(
                """
                UPDATE templates
                SET validation_status = 'valid', validation_report_json = '{}'
                WHERE id = ?
                """,
                (template_id,),
            )
            template_ids[document_type] = int(template_id)

        self.connection.execute(
            """
            INSERT INTO template_variables (
                template_id, variable_key, field_definition_id, is_required
            )
            VALUES (?, 'tip_interventie', ?, 1)
            """,
            (template_ids["epicrisis_recommendations"], field_id),
        )
        self.connection.execute(
            """
            UPDATE template_sets
            SET status = 'active', activated_at = '2026-01-01T00:00:00.000Z'
            WHERE id = ?
            """,
            (template_set_id,),
        )

        with self.assertRaises(FieldOptionError):
            add_field_option(self.connection, field_id, "noua", "Nouă")

        self.assertEqual(list_field_options(self.connection, field_id), original_options)

    def test_duplicate_option_batch_rolls_back_all_new_rows(self) -> None:
        field_id = create_variable(self.connection, "alegere", "Alegere", "select", "common")
        add_field_option(self.connection, field_id, "existent", "Existent")

        with self.assertRaises(MedicalLibraryDuplicateError):
            add_field_options(
                self.connection,
                field_id,
                [
                    ("nou", "Nou", 1),
                    ("existent", "Dublură", 2),
                ],
            )

        self.assertEqual(
            [row["option_value"] for row in list_field_options(self.connection, field_id)],
            ["existent"],
        )
        self.assertFalse(self.connection.in_transaction)

    def test_duplicate_option_values_in_one_batch_are_rejected(self) -> None:
        field_id = create_variable(self.connection, "alegere", "Alegere", "select", "common")
        with self.assertRaises(MedicalLibraryDuplicateError):
            add_field_options(
                self.connection,
                field_id,
                [("valoare", "Eticheta 1", 0), ("valoare", "Eticheta 2", 1)],
            )
        self.assertEqual(list_field_options(self.connection, field_id), [])

    def test_variable_key_must_be_unique_and_versions_are_not_deleted(self) -> None:
        field_id = create_variable(self.connection, "unic", "Eticheta", "text", "common")
        with self.assertRaises(MedicalLibraryDuplicateError):
            create_variable(self.connection, "unic", "Etichetă duplicată", "date", "common")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM field_definitions WHERE id = ?", (field_id,))
        self.assertEqual(len(list_variables(self.connection, variable_key="unic")), 1)


if __name__ == "__main__":
    unittest.main()
