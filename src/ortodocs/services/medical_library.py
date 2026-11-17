"""Validare și operații de bibliotecă medicală, fără dependență de UI."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

from ortodocs.storage import repositories
from ortodocs.storage.database import (
    DatabaseIntegrityError,
    switch_current_field_version,
    write_transaction,
)


FIELD_DATA_TYPES = frozenset({"text", "number", "date", "select"})
FIELD_SCOPES = frozenset({"common", "diagnosis", "intervention"})
RESERVED_PATIENT_NAME_VARIABLE_KEYS = frozenset(
    {
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
    }
)
_VARIABLE_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)


class _UnsetType:
    """Tip pentru argumentele opționale care disting omisiunea de None."""

    __slots__ = ()


_UNSET = _UnsetType()


class MedicalLibraryError(Exception):
    """Eroare de validare sau persistență în biblioteca medicală."""


class MedicalLibraryValidationError(MedicalLibraryError, ValueError):
    """Un parametru nu respectă regulile bibliotecii medicale."""


class MedicalLibraryNotFoundError(MedicalLibraryError):
    """Înregistrarea cerută nu există."""


class MedicalLibraryDuplicateError(MedicalLibraryError):
    """O cheie unică este deja folosită."""


class MedicalLibraryRelationshipError(MedicalLibraryError):
    """O relație imuabilă nu poate fi schimbată."""


class MedicalLibraryIntegrityError(MedicalLibraryError):
    """Baza de date a respins operația dintr-un motiv de integritate."""


class FieldOptionError(MedicalLibraryError):
    """Opțiunile nu sunt valide pentru definiția de câmp indicată."""


def normalize_name(value: str, *, field_label: str = "denumirea") -> str:
    """Curăță spațiile de la capete și reduce spațiile consecutive la unul."""

    if not isinstance(value, str):
        raise MedicalLibraryValidationError(f"{field_label.capitalize()} trebuie să fie text.")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        raise MedicalLibraryValidationError(f"{field_label.capitalize()} nu poate fi goală.")
    return normalized


def normalize_name_key(value: str) -> str:
    """Cheie consecventă pentru duplicate care diferă prin spații sau majuscule."""

    return unicodedata.normalize("NFC", normalize_name(value).casefold())


def _normalize_code(code: str | None) -> str | None:
    if code is None:
        return None
    if not isinstance(code, str):
        raise MedicalLibraryValidationError("Codul trebuie să fie text sau None.")
    normalized = code.strip()
    return normalized or None


def _validate_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MedicalLibraryValidationError(f"{label} trebuie să fie un întreg pozitiv.")
    return value


def _validate_sort_order(sort_order: int) -> int:
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise MedicalLibraryValidationError("Ordinea de afișare trebuie să fie un întreg.")
    return sort_order


def _validate_boolean(value: bool, label: str) -> bool:
    if not isinstance(value, bool):
        raise MedicalLibraryValidationError(f"{label} trebuie să fie True sau False.")
    return value


def _normalize_variable_key(variable_key: str) -> str:
    if not isinstance(variable_key, str) or not _VARIABLE_KEY_PATTERN.fullmatch(
        variable_key
    ):
        raise MedicalLibraryValidationError(
            "Cheia variabilei trebuie să înceapă cu o literă mică și să conțină "
            "doar litere mici ASCII, cifre sau underscore."
        )
    if is_reserved_patient_name_variable_key(variable_key):
        raise MedicalLibraryValidationError(
            "Cheile rezervate pentru numele pacientului nu pot fi definite în bibliotecă."
        )
    return variable_key


def is_reserved_patient_name_variable_key(variable_key: str) -> bool:
    """Indică dacă cheia este una dintre denumirile rezervate pentru numele pacientului."""

    return variable_key in RESERVED_PATIENT_NAME_VARIABLE_KEYS


def _validate_data_type(data_type: str) -> str:
    if not isinstance(data_type, str) or data_type not in FIELD_DATA_TYPES:
        raise MedicalLibraryValidationError(
            "Tipul câmpului trebuie să fie text, number, date sau select."
        )
    return data_type


def _validate_scope(scope: str) -> str:
    if not isinstance(scope, str) or scope not in FIELD_SCOPES:
        raise MedicalLibraryValidationError(
            "Domeniul câmpului trebuie să fie common, diagnosis sau intervention."
        )
    return scope


def _validate_scope_references(
    connection: sqlite3.Connection,
    *,
    scope: str,
    diagnosis_id: int | None,
    intervention_id: int | None,
) -> None:
    if scope == "common":
        if diagnosis_id is not None or intervention_id is not None:
            raise MedicalLibraryValidationError(
                "Un câmp common nu poate avea diagnostic sau intervenție asociată."
            )
        return

    if scope == "diagnosis":
        if diagnosis_id is None or intervention_id is not None:
            raise MedicalLibraryValidationError(
                "Un câmp diagnosis trebuie să aibă doar diagnosis_id."
            )
        _validate_id(diagnosis_id, "diagnosis_id")
        if repositories.get_diagnosis(connection, diagnosis_id) is None:
            raise MedicalLibraryNotFoundError(
                f"Diagnosticul cu ID {diagnosis_id} nu există."
            )
        return

    if diagnosis_id is not None or intervention_id is None:
        raise MedicalLibraryValidationError(
            "Un câmp intervention trebuie să aibă doar intervention_id."
        )
    _validate_id(intervention_id, "intervention_id")
    if repositories.get_intervention(connection, intervention_id) is None:
        raise MedicalLibraryNotFoundError(
            f"Intervenția cu ID {intervention_id} nu există."
        )


def _raise_integrity_error(
    error: sqlite3.IntegrityError,
    *,
    duplicate_message: str,
    reference_message: str,
    integrity_message: str,
) -> None:
    detail = str(error)
    if "UNIQUE constraint failed" in detail or "PRIMARY KEY constraint failed" in detail:
        raise MedicalLibraryDuplicateError(duplicate_message) from error
    if "FOREIGN KEY constraint failed" in detail:
        raise MedicalLibraryNotFoundError(reference_message) from error
    raise MedicalLibraryIntegrityError(integrity_message) from error


# Diagnostice

def list_diagnoses(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    _validate_boolean(active_only, "active_only")
    return repositories.list_diagnoses(connection, active_only=active_only)


def add_diagnosis(
    connection: sqlite3.Connection,
    name: str,
    *,
    code: str | None = None,
    sort_order: int = 0,
) -> int:
    display_name = normalize_name(name)
    name_key = normalize_name_key(display_name)
    normalized_code = _normalize_code(code)
    order = _validate_sort_order(sort_order)
    try:
        with write_transaction(connection):
            return repositories.insert_diagnosis(
                connection,
                name=display_name,
                name_key=name_key,
                code=normalized_code,
                sort_order=order,
            )
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message="Există deja un diagnostic cu aceeași denumire normalizată sau același cod.",
            reference_message="Un diagnostic asociat nu mai există.",
            integrity_message="Diagnosticul nu a putut fi salvat din cauza unei reguli de integritate.",
        )


def update_diagnosis(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    name: str,
    *,
    code: str | None | _UnsetType = _UNSET,
) -> None:
    diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    display_name = normalize_name(name)
    name_key = normalize_name_key(display_name)
    normalized_code = _UNSET if code is _UNSET else _normalize_code(code)
    try:
        with write_transaction(connection):
            existing = repositories.get_diagnosis(connection, diagnosis_id)
            if existing is None:
                raise MedicalLibraryNotFoundError(
                    f"Diagnosticul cu ID {diagnosis_id} nu există."
                )
            code_to_save = (
                existing["code"] if normalized_code is _UNSET else normalized_code
            )
            repositories.update_diagnosis(
                connection,
                diagnosis_id,
                name=display_name,
                name_key=name_key,
                code=code_to_save,
            )
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message="Există deja un diagnostic cu aceeași denumire normalizată sau același cod.",
            reference_message="O înregistrare asociată nu mai există.",
            integrity_message="Diagnosticul nu a putut fi modificat din cauza unei reguli de integritate.",
        )


def set_diagnosis_sort_order(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    sort_order: int,
) -> None:
    diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    sort_order = _validate_sort_order(sort_order)
    with write_transaction(connection):
        if repositories.set_diagnosis_sort_order(
            connection, diagnosis_id, sort_order
        ) != 1:
            raise MedicalLibraryNotFoundError(
                f"Diagnosticul cu ID {diagnosis_id} nu există."
            )


def set_diagnosis_active(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    is_active: bool,
) -> None:
    diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    is_active = _validate_boolean(is_active, "is_active")
    with write_transaction(connection):
        if repositories.set_diagnosis_active(connection, diagnosis_id, is_active) != 1:
            raise MedicalLibraryNotFoundError(
                f"Diagnosticul cu ID {diagnosis_id} nu există."
            )


# Intervenții

def list_interventions(
    connection: sqlite3.Connection,
    *,
    diagnosis_id: int | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    _validate_boolean(active_only, "active_only")
    if diagnosis_id is not None:
        diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
        if repositories.get_diagnosis(connection, diagnosis_id) is None:
            raise MedicalLibraryNotFoundError(
                f"Diagnosticul cu ID {diagnosis_id} nu există."
            )
    return repositories.list_interventions(
        connection,
        diagnosis_id=diagnosis_id,
        active_only=active_only,
    )


def add_intervention(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    name: str,
    *,
    code: str | None = None,
    sort_order: int = 0,
) -> int:
    diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    display_name = normalize_name(name)
    name_key = normalize_name_key(display_name)
    normalized_code = _normalize_code(code)
    order = _validate_sort_order(sort_order)
    try:
        with write_transaction(connection):
            if repositories.get_diagnosis(connection, diagnosis_id) is None:
                raise MedicalLibraryNotFoundError(
                    f"Diagnosticul cu ID {diagnosis_id} nu există."
                )
            return repositories.insert_intervention(
                connection,
                diagnosis_id=diagnosis_id,
                name=display_name,
                name_key=name_key,
                code=normalized_code,
                sort_order=order,
            )
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message="Există deja o intervenție cu aceeași denumire normalizată sau același cod pentru acest diagnostic.",
            reference_message="Diagnosticul asociat nu mai există.",
            integrity_message="Intervenția nu a putut fi salvată din cauza unei reguli de integritate.",
        )


def update_intervention(
    connection: sqlite3.Connection,
    intervention_id: int,
    name: str,
    *,
    code: str | None | _UnsetType = _UNSET,
    diagnosis_id: int | None = None,
) -> None:
    intervention_id = _validate_id(intervention_id, "intervention_id")
    display_name = normalize_name(name)
    name_key = normalize_name_key(display_name)
    normalized_code = _UNSET if code is _UNSET else _normalize_code(code)
    if diagnosis_id is not None:
        diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")

    try:
        with write_transaction(connection):
            existing = repositories.get_intervention(connection, intervention_id)
            if existing is None:
                raise MedicalLibraryNotFoundError(
                    f"Intervenția cu ID {intervention_id} nu există."
                )
            if diagnosis_id is not None and diagnosis_id != existing["diagnosis_id"]:
                raise MedicalLibraryRelationshipError(
                    "O intervenție creată nu poate fi mutată la alt diagnostic."
                )
            code_to_save = (
                existing["code"] if normalized_code is _UNSET else normalized_code
            )
            repositories.update_intervention(
                connection,
                intervention_id,
                name=display_name,
                name_key=name_key,
                code=code_to_save,
            )
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message="Există deja o intervenție cu aceeași denumire normalizată sau același cod pentru acest diagnostic.",
            reference_message="O înregistrare asociată nu mai există.",
            integrity_message="Intervenția nu a putut fi modificată din cauza unei reguli de integritate.",
        )


def set_intervention_sort_order(
    connection: sqlite3.Connection,
    intervention_id: int,
    sort_order: int,
) -> None:
    intervention_id = _validate_id(intervention_id, "intervention_id")
    sort_order = _validate_sort_order(sort_order)
    with write_transaction(connection):
        if repositories.set_intervention_sort_order(
            connection, intervention_id, sort_order
        ) != 1:
            raise MedicalLibraryNotFoundError(
                f"Intervenția cu ID {intervention_id} nu există."
            )


def set_intervention_active(
    connection: sqlite3.Connection,
    intervention_id: int,
    is_active: bool,
) -> None:
    intervention_id = _validate_id(intervention_id, "intervention_id")
    is_active = _validate_boolean(is_active, "is_active")
    with write_transaction(connection):
        if repositories.set_intervention_active(
            connection, intervention_id, is_active
        ) != 1:
            raise MedicalLibraryNotFoundError(
                f"Intervenția cu ID {intervention_id} nu există."
            )


# Definiții și opțiuni de câmp

def list_variables(
    connection: sqlite3.Connection,
    *,
    variable_key: str | None = None,
    scope: str | None = None,
    current_only: bool = False,
) -> list[dict[str, Any]]:
    _validate_boolean(current_only, "current_only")
    if variable_key is not None:
        variable_key = _normalize_variable_key(variable_key)
    if scope is not None:
        scope = _validate_scope(scope)
    return repositories.list_field_definitions(
        connection,
        variable_key=variable_key,
        scope=scope,
        current_only=current_only,
    )


def _prepare_field_definition(
    connection: sqlite3.Connection,
    *,
    variable_key: str,
    label: str,
    data_type: str,
    scope: str,
    diagnosis_id: int | None,
    intervention_id: int | None,
) -> tuple[str, str, str, str, int | None, int | None]:
    key = _normalize_variable_key(variable_key)
    normalized_label = normalize_name(label, field_label="eticheta")
    normalized_type = _validate_data_type(data_type)
    normalized_scope = _validate_scope(scope)
    if diagnosis_id is not None:
        diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    if intervention_id is not None:
        intervention_id = _validate_id(intervention_id, "intervention_id")
    _validate_scope_references(
        connection,
        scope=normalized_scope,
        diagnosis_id=diagnosis_id,
        intervention_id=intervention_id,
    )
    return (
        key,
        normalized_label,
        normalized_type,
        normalized_scope,
        diagnosis_id,
        intervention_id,
    )


def create_variable(
    connection: sqlite3.Connection,
    variable_key: str,
    label: str,
    data_type: str,
    scope: str,
    *,
    diagnosis_id: int | None = None,
    intervention_id: int | None = None,
) -> int:
    """Creează versiunea 1, automat curentă, a unei variabile noi."""

    key, normalized_label, normalized_type, normalized_scope, diagnosis_id, intervention_id = (
        _prepare_field_definition(
            connection,
            variable_key=variable_key,
            label=label,
            data_type=data_type,
            scope=scope,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
        )
    )
    try:
        with write_transaction(connection):
            if repositories.get_latest_field_definition(connection, key) is not None:
                raise MedicalLibraryDuplicateError(
                    f"Cheia variabilei {key!r} există deja; creați o versiune nouă."
                )
            return repositories.insert_field_definition(
                connection,
                variable_key=key,
                version=1,
                label=normalized_label,
                data_type=normalized_type,
                scope=normalized_scope,
                diagnosis_id=diagnosis_id,
                intervention_id=intervention_id,
                is_current=True,
            )
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message=f"Cheia variabilei {key!r} există deja.",
            reference_message="Diagnosticul sau intervenția asociată nu mai există.",
            integrity_message="Variabila nu a putut fi creată din cauza unei reguli de integritate.",
        )


def create_variable_version(
    connection: sqlite3.Connection,
    variable_key: str,
    label: str,
    data_type: str,
    scope: str,
    *,
    diagnosis_id: int | None = None,
    intervention_id: int | None = None,
) -> int:
    """Adaugă o versiune nouă necurentă, fără a modifica versiunile vechi."""

    key, normalized_label, normalized_type, normalized_scope, diagnosis_id, intervention_id = (
        _prepare_field_definition(
            connection,
            variable_key=variable_key,
            label=label,
            data_type=data_type,
            scope=scope,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
        )
    )
    try:
        with write_transaction(connection):
            latest = repositories.get_latest_field_definition(connection, key)
            if latest is None:
                raise MedicalLibraryNotFoundError(
                    f"Variabila {key!r} nu există; creați-o înainte de versiune nouă."
                )
            existing_versions = repositories.list_field_definitions(
                connection,
                variable_key=key,
            )
            if sum(row["is_current"] == 1 for row in existing_versions) != 1:
                raise MedicalLibraryIntegrityError(
                    f"Variabila {key!r} trebuie să aibă exact o versiune curentă înainte de creare."
                )
            new_id = repositories.insert_field_definition(
                connection,
                variable_key=key,
                version=latest["version"] + 1,
                label=normalized_label,
                data_type=normalized_type,
                scope=normalized_scope,
                diagnosis_id=diagnosis_id,
                intervention_id=intervention_id,
                is_current=False,
            )
            current_count = sum(
                row["is_current"] == 1
                for row in repositories.list_field_definitions(
                    connection,
                    variable_key=key,
                )
            )
            if current_count != 1:
                raise MedicalLibraryIntegrityError(
                    f"Variabila {key!r} trebuie să aibă exact o versiune curentă."
                )
            return new_id
    except sqlite3.IntegrityError as exc:
        _raise_integrity_error(
            exc,
            duplicate_message=f"Versiunea variabilei {key!r} există deja.",
            reference_message="Diagnosticul sau intervenția asociată nu mai există.",
            integrity_message="Versiunea variabilei nu a putut fi creată din cauza unei reguli de integritate.",
        )


def set_current_variable_version(
    connection: sqlite3.Connection,
    variable_key: str,
    version: int,
) -> None:
    key = _normalize_variable_key(variable_key)
    version = _validate_id(version, "version")
    if repositories.get_field_definition_version(connection, key, version) is None:
        raise MedicalLibraryNotFoundError(
            f"Versiunea {version} a variabilei {key!r} nu există."
        )
    try:
        switch_current_field_version(connection, key, version)
    except (DatabaseIntegrityError, sqlite3.IntegrityError) as exc:
        raise MedicalLibraryIntegrityError(
            f"Nu s-a putut schimba versiunea curentă pentru {key!r}."
        ) from exc


def rollback_variable_version(
    connection: sqlite3.Connection,
    variable_key: str,
    version: int,
) -> None:
    key = _normalize_variable_key(variable_key)
    version = _validate_id(version, "version")
    with write_transaction(connection):
        definitions = repositories.list_field_definitions(
            connection,
            variable_key=key,
        )
        target = next((row for row in definitions if row["version"] == version), None)
        current = next((row for row in definitions if row["is_current"] == 1), None)
        if target is None:
            raise MedicalLibraryNotFoundError(
                f"Versiunea {version} a variabilei {key!r} nu există."
            )
        if current is None:
            raise MedicalLibraryIntegrityError(
                f"Variabila {key!r} nu are o versiune curentă validă."
            )
        if version >= current["version"]:
            raise MedicalLibraryValidationError(
                "Revenirea este permisă numai la o versiune mai veche decât cea curentă."
            )
        try:
            switch_current_field_version(connection, key, version)
        except (DatabaseIntegrityError, sqlite3.IntegrityError) as exc:
            raise MedicalLibraryIntegrityError(
                f"Nu s-a putut reveni la versiunea {version} pentru {key!r}."
            ) from exc


def list_field_options(
    connection: sqlite3.Connection,
    field_definition_id: int,
) -> list[dict[str, Any]]:
    field_definition_id = _validate_id(field_definition_id, "field_definition_id")
    if repositories.get_field_definition(connection, field_definition_id) is None:
        raise MedicalLibraryNotFoundError(
            f"Definiția de câmp cu ID {field_definition_id} nu există."
        )
    return repositories.list_field_options(connection, field_definition_id)


def _normalize_option_text(value: str, label: str) -> str:
    return normalize_name(value, field_label=label)


def add_field_options(
    connection: sqlite3.Connection,
    field_definition_id: int,
    options: Iterable[tuple[str, str, int]],
) -> list[dict[str, Any]]:
    """Adaugă atomic opțiunile (value, label, sort_order) ale unui câmp select."""

    field_definition_id = _validate_id(field_definition_id, "field_definition_id")
    if isinstance(options, (str, bytes)):
        raise MedicalLibraryValidationError(
            "Opțiunile trebuie furnizate ca listă de triplete (value, label, sort_order)."
        )
    try:
        raw_options = list(options)
    except TypeError as exc:
        raise MedicalLibraryValidationError(
            "Opțiunile trebuie furnizate ca listă de triplete (value, label, sort_order)."
        ) from exc
    if not raw_options:
        raise MedicalLibraryValidationError("Adăugați cel puțin o opțiune.")

    normalized_options: list[tuple[str, str, int]] = []
    seen_values: set[str] = set()
    for option in raw_options:
        if not isinstance(option, Sequence) or isinstance(option, (str, bytes)) or len(option) != 3:
            raise MedicalLibraryValidationError(
                "Fiecare opțiune trebuie să conțină value, label și sort_order."
            )
        option_value = _normalize_option_text(option[0], "valoarea opțiunii")
        option_label = _normalize_option_text(option[1], "eticheta opțiunii")
        sort_order = _validate_sort_order(option[2])
        if option_value in seen_values:
            raise MedicalLibraryDuplicateError(
                f"Valoarea opțiunii {option_value!r} apare de mai multe ori în listă."
            )
        seen_values.add(option_value)
        normalized_options.append((option_value, option_label, sort_order))

    try:
        with write_transaction(connection):
            definition = repositories.get_field_definition(
                connection,
                field_definition_id,
            )
            if definition is None:
                raise MedicalLibraryNotFoundError(
                    f"Definiția de câmp cu ID {field_definition_id} nu există."
                )
            if definition["data_type"] != "select":
                raise FieldOptionError(
                    "Opțiunile pot fi adăugate numai câmpurilor de tip select."
                )
            for option_value, option_label, sort_order in normalized_options:
                repositories.insert_field_option(
                    connection,
                    field_definition_id=field_definition_id,
                    option_value=option_value,
                    option_label=option_label,
                    sort_order=sort_order,
                )
    except sqlite3.IntegrityError as exc:
        detail = str(exc)
        if "UNIQUE constraint failed" in detail or "PRIMARY KEY constraint failed" in detail:
            raise MedicalLibraryDuplicateError(
                "Valoarea unei opțiuni există deja pentru această versiune de câmp."
            ) from exc
        if "FOREIGN KEY constraint failed" in detail:
            raise MedicalLibraryNotFoundError(
                f"Definiția de câmp cu ID {field_definition_id} nu există."
            ) from exc
        if "published field options are immutable" in detail:
            raise FieldOptionError(
                "Opțiunile unui câmp folosit de un pachet publicat sunt imuabile."
            ) from exc
        raise MedicalLibraryIntegrityError(
            "Opțiunile nu au putut fi salvate din cauza unei reguli de integritate."
        ) from exc

    return repositories.list_field_options(connection, field_definition_id)


def add_field_option(
    connection: sqlite3.Connection,
    field_definition_id: int,
    option_value: str,
    option_label: str,
    *,
    sort_order: int = 0,
) -> list[dict[str, Any]]:
    return add_field_options(
        connection,
        field_definition_id,
        [(option_value, option_label, sort_order)],
    )
