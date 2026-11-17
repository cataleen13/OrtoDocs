"""Import, mapare, validare și activare pentru pachetele de șabloane."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ortodocs.models.import_report import ImportReport
from ortodocs.models.rich_document import RichDocument
from ortodocs.services.docx_import import (
    DOCX_PARSER_VERSION,
    parse_docx_bytes,
)
from ortodocs.services.medical_library import (
    is_reserved_patient_name_variable_key,
)
from ortodocs.storage import (
    independent_template_repositories,
    repositories,
    template_repositories,
)
from ortodocs.storage.database import write_transaction


EPICRISIS_RECOMMENDATIONS = "epicrisis_recommendations"
OPERATIVE_PROTOCOL = "operative_protocol"
TEMPLATE_DOCUMENT_TYPES = (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
)
_VARIABLE_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_REPORT_SCHEMA_VERSION = 1


class TemplateSetError(Exception):
    """Eroare de validare sau persistență pentru pachetele de șabloane."""


class TemplateSetNotFoundError(TemplateSetError):
    """Pachetul sau șablonul solicitat nu există."""


class TemplateSetStateError(TemplateSetError):
    """Operația nu este permisă în starea curentă a pachetului."""


class TemplateSetValidationError(TemplateSetError, ValueError):
    """Parametrii operației sau datele persistate nu sunt valide."""


class TemplateSetDuplicateMappingError(TemplateSetError):
    """Cheia dintr-un șablon are deja o mapare."""


class TemplateSetIntegrityError(TemplateSetError):
    """SQLite a respins o operație de integrare a șabloanelor."""


@dataclass(frozen=True, slots=True)
class TemplateSetValidationResult:
    template_set_id: int
    can_activate: bool
    status: str
    issues: tuple[dict[str, Any], ...]
    report: dict[str, Any]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TemplateSetValidationError(f"{label} trebuie să fie un întreg pozitiv.")
    return value


def _validate_boolean(value: bool, label: str) -> bool:
    if not isinstance(value, bool):
        raise TemplateSetValidationError(f"{label} trebuie să fie True sau False.")
    return value


def _validate_document_type(document_type: str) -> str:
    if not isinstance(document_type, str) or document_type not in TEMPLATE_DOCUMENT_TYPES:
        raise TemplateSetValidationError(
            "Tipul documentului trebuie să fie epicrisis_recommendations sau operative_protocol."
        )
    return document_type


def _validate_variable_key(variable_key: str) -> str:
    if not isinstance(variable_key, str) or not _VARIABLE_KEY_PATTERN.fullmatch(
        variable_key
    ):
        raise TemplateSetValidationError("Cheia variabilei nu respectă sintaxa acceptată.")
    if is_reserved_patient_name_variable_key(variable_key):
        raise TemplateSetValidationError(
            "Cheile rezervate pentru numele pacientului nu pot fi mapate în șabloane."
        )
    return variable_key


def _require_no_active_transaction_for_docx_parse(
    connection: sqlite3.Connection,
) -> None:
    if connection.in_transaction:
        raise TemplateSetStateError(
            "DOCX-urile trebuie parsate în afara unei tranzacții SQLite active."
        )


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TemplateSetIntegrityError(f"{label} nu conține JSON valid.") from exc
    if not isinstance(decoded, dict):
        raise TemplateSetIntegrityError(f"{label} trebuie să fie un obiect JSON.")
    return decoded


def _empty_package_report(template_set_id: int) -> dict[str, Any]:
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "template_set_id": template_set_id,
        "status": "draft",
        "documents": {},
        "available_documents": [],
        "missing_documents": list(TEMPLATE_DOCUMENT_TYPES),
        "mappings_missing": {},
        "mappings_extra": {},
        "outdated_field_versions": [],
        "warning_confirmations": {},
        "patient_identification_review": {
            "confirmed": False,
            "confirmed_at": None,
        },
        "validation": {"status": "not_run", "can_activate": False, "issues": []},
    }


def _template_validation_report(
    import_report: ImportReport,
    *,
    warning_confirmed: bool = False,
    warning_confirmed_at: str | None = None,
) -> dict[str, Any]:
    parser_status = import_report.status
    validation_status = "invalid" if parser_status == "rejected" else "pending"
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "import_report": import_report.to_dict(),
        "warning_confirmation": {
            "required": parser_status == "valid_with_warnings",
            "confirmed": warning_confirmed,
            "confirmed_at": warning_confirmed_at,
        },
        "validation": {
            "status": validation_status,
            "issues": [],
            "validated_at": None,
        },
    }


def _get_set_or_raise(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    require_draft: bool = True,
) -> dict[str, Any]:
    template_set = template_repositories.get_template_set(connection, template_set_id)
    if template_set is None:
        raise TemplateSetNotFoundError(
            f"Pachetul de șabloane cu ID {template_set_id} nu există."
        )
    if require_draft and template_set["status"] != "draft":
        raise TemplateSetStateError(
            "Pachetele publicate sau respinse nu mai pot fi modificate."
        )
    return template_set


def _import_report_from_template(template: Mapping[str, Any]) -> dict[str, Any]:
    report = _read_json_object(
        str(template["validation_report_json"]),
        "Raportul șablonului",
    )
    import_report = report.get("import_report")
    if not isinstance(import_report, dict):
        raise TemplateSetIntegrityError(
            "Raportul șablonului nu conține secțiunea import_report."
        )
    return import_report


def _variables_from_report(
    import_report: Mapping[str, Any],
) -> dict[str, int]:
    raw_variables = import_report.get("variables", {})
    if not isinstance(raw_variables, dict):
        raise TemplateSetIntegrityError(
            "Lista variabilelor detectate din raport nu este validă."
        )
    variables: dict[str, int] = {}
    for key, count in raw_variables.items():
        if (
            not isinstance(key, str)
            or not _VARIABLE_KEY_PATTERN.fullmatch(key)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise TemplateSetIntegrityError(
                "Raportul conține o cheie sau un număr de apariții invalid."
            )
        variables[key] = count
    return variables


def _existing_package_report(
    template_set: Mapping[str, Any],
) -> dict[str, Any]:
    report = _read_json_object(
        str(template_set["import_report_json"]),
        "Raportul pachetului",
    )
    if report.get("schema_version") != _REPORT_SCHEMA_VERSION:
        return _empty_package_report(int(template_set["id"]))
    return report


def _build_package_report(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    reset_identity_review: bool = False,
) -> dict[str, Any]:
    template_set = _get_set_or_raise(connection, template_set_id, require_draft=False)
    previous = _existing_package_report(template_set)
    identity_review = previous.get("patient_identification_review")
    if reset_identity_review or not isinstance(identity_review, dict):
        identity_review = {"confirmed": False, "confirmed_at": None}

    templates = template_repositories.list_templates(connection, template_set_id)
    by_type = {str(item["document_type"]): item for item in templates}
    available = [kind for kind in TEMPLATE_DOCUMENT_TYPES if kind in by_type]
    missing = [kind for kind in TEMPLATE_DOCUMENT_TYPES if kind not in by_type]
    documents: dict[str, Any] = {}
    mappings_missing: dict[str, list[str]] = {}
    mappings_extra: dict[str, list[str]] = {}
    stale_versions: list[dict[str, Any]] = []
    warning_confirmations: dict[str, bool] = {}

    for kind in TEMPLATE_DOCUMENT_TYPES:
        template = by_type.get(kind)
        if template is None:
            continue
        import_report = _import_report_from_template(template)
        detected = _variables_from_report(import_report)
        mappings = template_repositories.list_template_variable_mappings(
            connection,
            int(template["id"]),
        )
        detected_keys = set(detected)
        mapped_keys = {str(mapping["variable_key"]) for mapping in mappings}
        missing_keys = sorted(detected_keys - mapped_keys)
        extra_keys = sorted(mapped_keys - detected_keys)
        if missing_keys:
            mappings_missing[kind] = missing_keys
        if extra_keys:
            mappings_extra[kind] = extra_keys

        report_json = _read_json_object(
            str(template["validation_report_json"]),
            "Raportul șablonului",
        )
        warning_confirmation = report_json.get("warning_confirmation", {})
        confirmed = bool(
            isinstance(warning_confirmation, dict)
            and warning_confirmation.get("confirmed") is True
        )
        warning_confirmations[kind] = confirmed
        for mapping in mappings:
            if int(mapping["is_current"]) != 1:
                stale_versions.append(
                    {
                        "document_type": kind,
                        "variable_key": str(mapping["variable_key"]),
                        "field_definition_id": int(mapping["field_definition_id"]),
                        "version": int(mapping["version"]),
                    }
                )

        documents[kind] = {
            "template_id": int(template["id"]),
            "import_status": import_report.get("status", "rejected"),
            "validation_status": template["validation_status"],
            "source_sha256": template["source_sha256"],
            "parser_version": template["parser_version"],
            "variables": detected,
            "mapped_variables": sorted(mapped_keys),
            "warning_confirmed": confirmed,
        }

    report = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "template_set_id": template_set_id,
        "status": "draft",
        "documents": documents,
        "available_documents": available,
        "missing_documents": missing,
        "mappings_missing": mappings_missing,
        "mappings_extra": mappings_extra,
        "outdated_field_versions": stale_versions,
        "warning_confirmations": warning_confirmations,
        "patient_identification_review": identity_review,
        "validation": {
            "status": "not_run",
            "can_activate": False,
            "issues": [],
        },
    }
    return report


def _save_package_report(
    connection: sqlite3.Connection,
    template_set_id: int,
    report: Mapping[str, Any],
) -> None:
    updated = template_repositories.update_template_set_report(
        connection,
        template_set_id,
        _json_text(report),
    )
    if updated != 1:
        raise TemplateSetStateError(
            "Raportul poate fi salvat numai cât timp pachetul este draft."
        )


def create_template_set_draft(
    connection: sqlite3.Connection,
    diagnosis_id: int,
    intervention_id: int,
) -> int:
    """Creează un draft nou și următorul număr de versiune pentru intervenție."""

    diagnosis_id = _validate_id(diagnosis_id, "diagnosis_id")
    intervention_id = _validate_id(intervention_id, "intervention_id")
    with write_transaction(connection):
        diagnosis = repositories.get_diagnosis(connection, diagnosis_id)
        if diagnosis is None:
            raise TemplateSetNotFoundError(
                f"Diagnosticul cu ID {diagnosis_id} nu există."
            )
        intervention = repositories.get_intervention(connection, intervention_id)
        if intervention is None:
            raise TemplateSetNotFoundError(
                f"Intervenția cu ID {intervention_id} nu există."
            )
        if int(intervention["diagnosis_id"]) != diagnosis_id:
            raise TemplateSetValidationError(
                "Intervenția selectată nu aparține diagnosticului selectat."
            )
        if not diagnosis["is_active"] or not intervention["is_active"]:
            raise TemplateSetStateError(
                "Un pachet draft poate fi creat numai pentru un diagnostic și o intervenție active."
            )

        version_no = template_repositories.get_next_template_set_version(
            connection,
            intervention_id,
        )
        initial_report = _empty_package_report(0)
        template_set_id = template_repositories.insert_template_set(
            connection,
            intervention_id=intervention_id,
            version_no=version_no,
            import_report_json=_json_text(initial_report),
        )
        initial_report["template_set_id"] = template_set_id
        _save_package_report(connection, template_set_id, initial_report)
    return template_set_id


def _parse_docx(document_bytes: bytes) -> tuple[RichDocument, ImportReport, str]:
    if not isinstance(document_bytes, bytes):
        raise TemplateSetValidationError("Fișierul DOCX trebuie furnizat ca bytes.")
    if not document_bytes:
        raise TemplateSetValidationError(
            "Fișierul DOCX este gol și nu a fost importat sau salvat."
        )
    result = parse_docx_bytes(document_bytes)
    if result[2] != hashlib.sha256(document_bytes).hexdigest():
        raise TemplateSetIntegrityError(
            "Hash-ul returnat de parser nu corespunde fișierului DOCX original."
        )
    return result


def _persist_parsed_template(
    connection: sqlite3.Connection,
    *,
    template_set_id: int,
    document_type: str,
    source_docx: bytes,
    parsed: tuple[RichDocument, ImportReport, str],
) -> int:
    template_set = _get_set_or_raise(connection, template_set_id)
    document, import_report, digest = parsed
    previous = next(
        (
            template
            for template in template_repositories.list_templates(connection, template_set_id)
            if template["document_type"] == document_type
        ),
        None,
    )
    if previous is not None:
        template_repositories.delete_draft_template(connection, int(previous["id"]))

    parser_status = import_report.status
    if parser_status == "rejected":
        # Un rezultat parțial nu devine conținut utilizabil. Originalul și raportul
        # complet rămân disponibile pentru diagnostic și corectarea draftului.
        document_json = RichDocument().to_json()
        validation_status = "invalid"
    else:
        document_json = document.to_json()
        validation_status = "pending"

    template_report = _template_validation_report(import_report)
    template_id = template_repositories.insert_template(
        connection,
        template_set_id=template_set_id,
        document_type=document_type,
        source_docx=source_docx,
        source_sha256=digest,
        parsed_content_json=document_json,
        validation_report_json=_json_text(template_report),
        parser_version=DOCX_PARSER_VERSION,
        # Triggerul schemei impune starea inițială pending. Importurile respinse
        # sunt trecute în invalid printr-o actualizare separată în aceeași tranzacție.
        validation_status="pending",
    )
    if validation_status == "invalid":
        updated = template_repositories.update_template_validation(
            connection,
            template_id,
            validation_status="invalid",
            validation_report_json=_json_text(template_report),
        )
        if updated != 1:
            raise TemplateSetIntegrityError(
                "Raportul documentului respins nu a putut fi salvat."
            )
    package_report = _build_package_report(
        connection,
        template_set_id,
        reset_identity_review=True,
    )
    package_report["validation"] = {
        "status": "not_run",
        "can_activate": False,
        "issues": [],
    }
    _save_package_report(connection, template_set_id, package_report)
    return template_id


def import_template(
    connection: sqlite3.Connection,
    template_set_id: int,
    document_type: str,
    document_bytes: bytes,
) -> int:
    """Importă sau înlocuiește un singur document într-un pachet draft."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    document_type = _validate_document_type(document_type)
    _require_no_active_transaction_for_docx_parse(connection)
    parsed = _parse_docx(document_bytes)  # parsarea se face înaintea tranzacției
    with write_transaction(connection):
        _get_set_or_raise(connection, template_set_id)
        return _persist_parsed_template(
            connection,
            template_set_id=template_set_id,
            document_type=document_type,
            source_docx=document_bytes,
            parsed=parsed,
        )


def import_template_pair(
    connection: sqlite3.Connection,
    template_set_id: int,
    documents: Mapping[str, bytes],
) -> dict[str, int]:
    """Parsează ambele DOCX-uri în afara tranzacției și le salvează atomic."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    if not isinstance(documents, Mapping) or set(documents) != set(
        TEMPLATE_DOCUMENT_TYPES
    ):
        raise TemplateSetValidationError(
            "Importul perechii trebuie să conțină exact ambele tipuri de document."
        )
    _require_no_active_transaction_for_docx_parse(connection)
    parsed_documents = {
        kind: _parse_docx(documents[kind]) for kind in TEMPLATE_DOCUMENT_TYPES
    }
    result: dict[str, int] = {}
    with write_transaction(connection):
        _get_set_or_raise(connection, template_set_id)
        for kind in TEMPLATE_DOCUMENT_TYPES:
            result[kind] = _persist_parsed_template(
                connection,
                template_set_id=template_set_id,
                document_type=kind,
                source_docx=documents[kind],
                parsed=parsed_documents[kind],
            )
    return result


def _get_template_or_raise(
    connection: sqlite3.Connection,
    template_id: int,
    *,
    require_draft: bool = True,
) -> dict[str, Any]:
    template_id = _validate_id(template_id, "template_id")
    template = template_repositories.get_template(connection, template_id)
    if template is None:
        raise TemplateSetNotFoundError(f"Șablonul cu ID {template_id} nu există.")
    if require_draft and template["template_set_status"] != "draft":
        raise TemplateSetStateError(
            "Mapările și confirmările pot fi modificate numai pentru șabloane draft."
        )
    return template


def _write_template_report(
    connection: sqlite3.Connection,
    template_id: int,
    report: Mapping[str, Any],
    *,
    validation_status: str | None = None,
) -> None:
    template = _get_template_or_raise(connection, template_id)
    status = validation_status or str(template["validation_status"])
    updated = template_repositories.update_template_validation(
        connection,
        template_id,
        validation_status=status,
        validation_report_json=_json_text(report),
    )
    if updated != 1:
        raise TemplateSetStateError("Raportul șablonului nu a putut fi actualizat.")


def _reset_template_validation(connection: sqlite3.Connection, template_id: int) -> None:
    template = _get_template_or_raise(connection, template_id)
    report = _read_json_object(
        str(template["validation_report_json"]),
        "Raportul șablonului",
    )
    import_report = _import_report_from_template(template)
    status = "invalid" if import_report.get("status") == "rejected" else "pending"
    report["validation"] = {
        "status": status,
        "issues": [],
        "validated_at": None,
    }
    _write_template_report(
        connection,
        template_id,
        report,
        validation_status=status,
    )


def confirm_template_warnings(
    connection: sqlite3.Connection,
    template_id: int,
    *,
    confirmed: bool,
) -> None:
    """Păstrează confirmarea explicită a avertizărilor pentru un document."""

    confirmed = _validate_boolean(confirmed, "confirmed")
    template_id = _validate_id(template_id, "template_id")
    with write_transaction(connection):
        template = _get_template_or_raise(connection, template_id)
        report = _read_json_object(
            str(template["validation_report_json"]),
            "Raportul șablonului",
        )
        import_report = _import_report_from_template(template)
        if import_report.get("status") != "valid_with_warnings":
            raise TemplateSetStateError(
                "Confirmarea avertizărilor se aplică numai documentelor valid_with_warnings."
            )
        confirmation = report.get("warning_confirmation")
        if not isinstance(confirmation, dict):
            confirmation = {"required": True, "confirmed": False, "confirmed_at": None}
        confirmation["required"] = True
        confirmation["confirmed"] = confirmed
        confirmation["confirmed_at"] = _now_utc() if confirmed else None
        report["warning_confirmation"] = confirmation
        report["validation"] = {
            "status": "pending",
            "issues": [],
            "validated_at": None,
        }
        _write_template_report(
            connection,
            template_id,
            report,
            validation_status="pending",
        )
        package_report = _build_package_report(
            connection,
            int(template["template_set_id"]),
        )
        _save_package_report(
            connection,
            int(template["template_set_id"]),
            package_report,
        )


def list_document_variables(
    connection: sqlite3.Connection,
    template_id: int,
) -> list[dict[str, Any]]:
    """Listează cheile detectate, numărul aparițiilor și maparea existentă."""

    template = _get_template_or_raise(connection, template_id, require_draft=False)
    import_report = _import_report_from_template(template)
    variables = _variables_from_report(import_report)
    mappings = {
        str(row["variable_key"]): row
        for row in template_repositories.list_template_variable_mappings(
            connection,
            template_id,
        )
    }
    result: list[dict[str, Any]] = []
    for key in sorted(variables):
        mapping = mappings.get(key)
        result.append(
            {
                "variable_key": key,
                "occurrence_count": variables[key],
                "is_mapped": mapping is not None,
                "field_definition_id": (
                    int(mapping["field_definition_id"]) if mapping is not None else None
                ),
                "field_version": int(mapping["version"]) if mapping is not None else None,
                "field_label": mapping["label"] if mapping is not None else None,
                "is_required": bool(mapping["is_required"]) if mapping is not None else None,
            }
        )
    return result


def _validate_mapping_target(
    connection: sqlite3.Connection,
    *,
    template: Mapping[str, Any],
    variable_key: str,
    field_definition_id: int,
) -> dict[str, Any]:
    definition = repositories.get_field_definition(connection, field_definition_id)
    if definition is None:
        raise TemplateSetNotFoundError(
            f"Definiția câmpului cu ID {field_definition_id} nu există."
        )
    if definition["variable_key"] != variable_key:
        raise TemplateSetValidationError(
            "Cheia tokenului trebuie să fie identică cu cheia definiției selectate."
        )
    if is_reserved_patient_name_variable_key(variable_key):
        raise TemplateSetValidationError(
            "Cheile rezervate pentru numele pacientului nu pot fi asociate șabloanelor."
        )
    scope = definition["scope"]
    if scope == "diagnosis" and definition["diagnosis_id"] != template["diagnosis_id"]:
        raise TemplateSetValidationError(
            "Câmpul specific diagnosticului nu aparține diagnosticului pachetului."
        )
    if (
        scope == "intervention"
        and definition["intervention_id"] != template["intervention_id"]
    ):
        raise TemplateSetValidationError(
            "Câmpul specific intervenției nu aparține intervenției pachetului."
        )
    if scope not in {"common", "diagnosis", "intervention"}:
        raise TemplateSetValidationError("Domeniul definiției câmpului nu este valid.")
    return definition


def add_variable_mapping(
    connection: sqlite3.Connection,
    template_id: int,
    variable_key: str,
    field_definition_id: int,
    *,
    is_required: bool,
) -> None:
    """Adaugă o mapare unică între token și versiunea exactă a unui câmp."""

    template_id = _validate_id(template_id, "template_id")
    field_definition_id = _validate_id(field_definition_id, "field_definition_id")
    variable_key = _validate_variable_key(variable_key)
    is_required = _validate_boolean(is_required, "is_required")
    with write_transaction(connection):
        template = _get_template_or_raise(connection, template_id)
        import_report = _import_report_from_template(template)
        if import_report.get("status") == "rejected":
            raise TemplateSetStateError(
                "Un document respins trebuie reimportat înainte de maparea variabilelor."
            )
        if variable_key not in _variables_from_report(import_report):
            raise TemplateSetValidationError(
                f"Variabila {variable_key!r} nu este prezentă în documentul importat."
            )
        _validate_mapping_target(
            connection,
            template=template,
            variable_key=variable_key,
            field_definition_id=field_definition_id,
        )
        if template_repositories.get_template_variable_mapping(
            connection,
            template_id,
            variable_key,
        ) is not None:
            raise TemplateSetDuplicateMappingError(
                f"Variabila {variable_key!r} are deja o mapare în acest document."
            )
        try:
            template_repositories.insert_template_variable_mapping(
                connection,
                template_id=template_id,
                variable_key=variable_key,
                field_definition_id=field_definition_id,
                is_required=is_required,
            )
        except sqlite3.IntegrityError as exc:
            raise TemplateSetIntegrityError(
                f"Maparea variabilei {variable_key!r} a fost respinsă de SQLite: {exc}"
            ) from exc
        _reset_template_validation(connection, template_id)
        package_report = _build_package_report(
            connection,
            int(template["template_set_id"]),
        )
        _save_package_report(
            connection,
            int(template["template_set_id"]),
            package_report,
        )


def update_variable_mapping(
    connection: sqlite3.Connection,
    template_id: int,
    variable_key: str,
    field_definition_id: int,
    *,
    is_required: bool,
) -> None:
    """Actualizează maparea existentă, inclusiv pentru remapare de versiune."""

    template_id = _validate_id(template_id, "template_id")
    field_definition_id = _validate_id(field_definition_id, "field_definition_id")
    variable_key = _validate_variable_key(variable_key)
    is_required = _validate_boolean(is_required, "is_required")
    with write_transaction(connection):
        template = _get_template_or_raise(connection, template_id)
        if template_repositories.get_template_variable_mapping(
            connection,
            template_id,
            variable_key,
        ) is None:
            raise TemplateSetNotFoundError(
                f"Maparea variabilei {variable_key!r} nu există în acest document."
            )
        import_report = _import_report_from_template(template)
        if import_report.get("status") == "rejected":
            raise TemplateSetStateError(
                "Un document respins trebuie reimportat înainte de maparea variabilelor."
            )
        if variable_key not in _variables_from_report(import_report):
            raise TemplateSetValidationError(
                f"Variabila {variable_key!r} nu este prezentă în documentul importat."
            )
        _validate_mapping_target(
            connection,
            template=template,
            variable_key=variable_key,
            field_definition_id=field_definition_id,
        )
        try:
            updated = template_repositories.update_template_variable_mapping(
                connection,
                template_id=template_id,
                variable_key=variable_key,
                field_definition_id=field_definition_id,
                is_required=is_required,
            )
        except sqlite3.IntegrityError as exc:
            raise TemplateSetIntegrityError(
                f"Maparea variabilei {variable_key!r} a fost respinsă de SQLite: {exc}"
            ) from exc
        if updated != 1:
            raise TemplateSetNotFoundError(
                f"Maparea variabilei {variable_key!r} nu a putut fi actualizată."
            )
        _reset_template_validation(connection, template_id)
        package_report = _build_package_report(
            connection,
            int(template["template_set_id"]),
        )
        _save_package_report(
            connection,
            int(template["template_set_id"]),
            package_report,
        )


def remove_variable_mapping(
    connection: sqlite3.Connection,
    template_id: int,
    variable_key: str,
) -> None:
    template_id = _validate_id(template_id, "template_id")
    variable_key = _validate_variable_key(variable_key)
    with write_transaction(connection):
        template = _get_template_or_raise(connection, template_id)
        removed = template_repositories.delete_template_variable_mapping(
            connection,
            template_id=template_id,
            variable_key=variable_key,
        )
        if removed != 1:
            raise TemplateSetNotFoundError(
                f"Maparea variabilei {variable_key!r} nu există în acest document."
            )
        _reset_template_validation(connection, template_id)
        package_report = _build_package_report(
            connection,
            int(template["template_set_id"]),
        )
        _save_package_report(
            connection,
            int(template["template_set_id"]),
            package_report,
        )


def _add_issue(
    issues: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    document_type: str | None = None,
    variable_key: str | None = None,
) -> None:
    issue: dict[str, Any] = {"code": code, "message": message}
    if document_type is not None:
        issue["document_type"] = document_type
    if variable_key is not None:
        issue["variable_key"] = variable_key
    issues.append(issue)


def _validate_template_set_locked(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    patient_identification_review_confirmed: bool,
) -> TemplateSetValidationResult:
    template_set = _get_set_or_raise(connection, template_set_id)
    templates = template_repositories.list_templates(connection, template_set_id)
    by_type = {str(template["document_type"]): template for template in templates}
    issues: list[dict[str, Any]] = []
    mappings_by_document: dict[str, dict[str, tuple[int, int]]] = {}

    for kind in TEMPLATE_DOCUMENT_TYPES:
        if kind not in by_type:
            _add_issue(
                issues,
                "missing_document",
                f"Lipsește documentul {kind}.",
                document_type=kind,
            )
    for kind in sorted(set(by_type) - set(TEMPLATE_DOCUMENT_TYPES)):
        _add_issue(
            issues,
            "unexpected_document_type",
            f"Tip de document neașteptat: {kind}.",
            document_type=kind,
        )

    for kind in TEMPLATE_DOCUMENT_TYPES:
        template = by_type.get(kind)
        if template is None:
            continue
        template_id = int(template["id"])
        report = _read_json_object(
            str(template["validation_report_json"]),
            "Raportul șablonului",
        )
        import_report = _import_report_from_template(template)
        parser_status = import_report.get("status")
        local_issues: list[dict[str, Any]] = []
        if parser_status == "rejected":
            _add_issue(
                local_issues,
                "docx_rejected",
                "Documentul Word a fost respins de validator și trebuie înlocuit.",
                document_type=kind,
            )
        elif parser_status not in {"valid", "valid_with_warnings"}:
            _add_issue(
                local_issues,
                "invalid_import_status",
                "Raportul importului are un statut necunoscut.",
                document_type=kind,
            )

        confirmation = report.get("warning_confirmation", {})
        warning_confirmed = bool(
            isinstance(confirmation, dict)
            and confirmation.get("confirmed") is True
        )
        if parser_status == "valid_with_warnings" and not warning_confirmed:
            _add_issue(
                local_issues,
                "warnings_not_confirmed",
                "Avertizările documentului trebuie verificate și confirmate explicit.",
                document_type=kind,
            )

        try:
            detected = _variables_from_report(import_report)
        except TemplateSetIntegrityError as exc:
            detected = {}
            _add_issue(
                local_issues,
                "invalid_variable_report",
                str(exc),
                document_type=kind,
            )
        mappings = template_repositories.list_template_variable_mappings(
            connection,
            template_id,
        )
        mappings_by_document[kind] = {}
        detected_keys = set(detected)
        mapped_keys = {str(mapping["variable_key"]) for mapping in mappings}
        for key in sorted(detected_keys - mapped_keys):
            _add_issue(
                local_issues,
                "unmapped_variable",
                f"Variabila {key!r} nu este asociată cu un câmp din bibliotecă.",
                document_type=kind,
                variable_key=key,
            )
        for key in sorted(mapped_keys - detected_keys):
            _add_issue(
                local_issues,
                "extra_variable_mapping",
                f"Maparea pentru {key!r} nu corespunde unei variabile din document.",
                document_type=kind,
                variable_key=key,
            )

        for mapping in mappings:
            key = str(mapping["variable_key"])
            definition = repositories.get_field_definition(
                connection,
                int(mapping["field_definition_id"]),
            )
            if definition is None:
                _add_issue(
                    local_issues,
                    "field_definition_missing",
                    "Definiția câmpului mapat nu mai există.",
                    document_type=kind,
                    variable_key=key,
                )
                continue
            if key in detected_keys:
                mappings_by_document[kind][key] = (
                    int(mapping["field_definition_id"]),
                    int(mapping["version"]),
                )
            if definition["variable_key"] != key:
                _add_issue(
                    local_issues,
                    "field_key_mismatch",
                    "Cheia definiției nu corespunde tokenului din document.",
                    document_type=kind,
                    variable_key=key,
                )
            if is_reserved_patient_name_variable_key(key):
                _add_issue(
                    local_issues,
                    "reserved_patient_name_variable",
                    "Cheile rezervate pentru numele pacientului nu sunt permise.",
                    document_type=kind,
                    variable_key=key,
                )
            if (
                definition["scope"] == "diagnosis"
                and definition["diagnosis_id"] != template_set["diagnosis_id"]
            ):
                _add_issue(
                    local_issues,
                    "diagnosis_scope_mismatch",
                    "Câmpul specific diagnosticului nu aparține diagnosticului selectat.",
                    document_type=kind,
                    variable_key=key,
                )
            if (
                definition["scope"] == "intervention"
                and definition["intervention_id"] != template_set["intervention_id"]
            ):
                _add_issue(
                    local_issues,
                    "intervention_scope_mismatch",
                    "Câmpul specific intervenției nu aparține intervenției selectate.",
                    document_type=kind,
                    variable_key=key,
                )
            if int(definition["is_current"]) != 1:
                _add_issue(
                    local_issues,
                    "outdated_field_version",
                    "Versiunea mapată nu mai este curentă; remapați variabila.",
                    document_type=kind,
                    variable_key=key,
                )
            if definition["data_type"] == "select" and not repositories.list_field_options(
                connection,
                int(definition["id"]),
            ):
                _add_issue(
                    local_issues,
                    "select_options_missing",
                    "Câmpul select nu are opțiuni configurate pentru această versiune.",
                    document_type=kind,
                    variable_key=key,
                )

        for issue in local_issues:
            issues.append(issue)
        parser_rejected = parser_status == "rejected"
        template_status = (
            "invalid"
            if parser_rejected
            else "valid"
            if not local_issues
            else "pending"
        )
        report["validation"] = {
            "status": template_status,
            "issues": local_issues,
            "validated_at": _now_utc(),
        }
        _write_template_report(
            connection,
            template_id,
            report,
            validation_status=template_status,
        )

    for variable_key in sorted(
        set(mappings_by_document.get(TEMPLATE_DOCUMENT_TYPES[0], {}))
        & set(mappings_by_document.get(TEMPLATE_DOCUMENT_TYPES[1], {}))
    ):
        first_mapping = mappings_by_document[TEMPLATE_DOCUMENT_TYPES[0]][variable_key]
        second_mapping = mappings_by_document[TEMPLATE_DOCUMENT_TYPES[1]][variable_key]
        if first_mapping != second_mapping:
            _add_issue(
                issues,
                "shared_variable_mapping_mismatch",
                f"Variabila comună {variable_key!r} indică definiții sau versiuni diferite "
                "în cele două șabloane.",
                variable_key=variable_key,
            )

    if not template_set["diagnosis_is_active"]:
        _add_issue(
            issues,
            "inactive_diagnosis",
            "Diagnosticul este inactiv; reactivați-l înainte de activarea pachetului.",
        )
    if not template_set["intervention_is_active"]:
        _add_issue(
            issues,
            "inactive_intervention",
            "Intervenția este inactivă; reactivați-o înainte de activarea pachetului.",
        )
    if not patient_identification_review_confirmed:
        _add_issue(
            issues,
            "patient_identification_review_not_confirmed",
            "Confirmați că ambele șabloane au fost verificate și nu conțin nume sau identificatori de pacienți.",
        )

    can_activate = not issues and len(templates) == 2 and set(by_type) == set(
        TEMPLATE_DOCUMENT_TYPES
    )
    package_report = _build_package_report(connection, template_set_id)
    package_report["status"] = "validated" if can_activate else "draft"
    package_report["patient_identification_review"] = {
        "confirmed": patient_identification_review_confirmed,
        "confirmed_at": _now_utc() if patient_identification_review_confirmed else None,
        "scope": list(TEMPLATE_DOCUMENT_TYPES),
    }
    package_report["validation"] = {
        "status": "valid" if can_activate else "blocked",
        "can_activate": can_activate,
        "validated_at": _now_utc(),
        "issues": issues,
    }
    _save_package_report(connection, template_set_id, package_report)
    return TemplateSetValidationResult(
        template_set_id=template_set_id,
        can_activate=can_activate,
        status="valid" if can_activate else "blocked",
        issues=tuple(issues),
        report=package_report,
    )


def validate_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    patient_identification_review_confirmed: bool,
) -> TemplateSetValidationResult:
    """Validează complet un draft și persistă rezultatul pentru interfață."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    patient_identification_review_confirmed = _validate_boolean(
        patient_identification_review_confirmed,
        "patient_identification_review_confirmed",
    )
    with write_transaction(connection):
        return _validate_template_set_locked(
            connection,
            template_set_id,
            patient_identification_review_confirmed=patient_identification_review_confirmed,
        )


def activate_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    patient_identification_review_confirmed: bool,
) -> TemplateSetValidationResult:
    """Activează perechea completă și retrage atomic pachetul precedent."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    patient_identification_review_confirmed = _validate_boolean(
        patient_identification_review_confirmed,
        "patient_identification_review_confirmed",
    )
    with write_transaction(connection):
        validation = _validate_template_set_locked(
            connection,
            template_set_id,
            patient_identification_review_confirmed=patient_identification_review_confirmed,
        )
        if not validation.can_activate:
            return validation

        template_set = _get_set_or_raise(connection, template_set_id)
        now = _now_utc()
        previous = template_repositories.get_active_template_set(
            connection,
            int(template_set["intervention_id"]),
        )
        if previous is not None:
            retired = template_repositories.retire_template_set(
                connection,
                int(previous["id"]),
                now,
            )
            if retired != 1:
                raise TemplateSetIntegrityError(
                    "Pachetul activ precedent nu a putut fi retras."
                )

        package_report = dict(validation.report)
        package_report["status"] = "active"
        package_report["activated_at"] = now
        _save_package_report(connection, template_set_id, package_report)
        try:
            activated = template_repositories.activate_template_set(
                connection,
                template_set_id,
                now,
            )
        except sqlite3.IntegrityError as exc:
            raise TemplateSetIntegrityError(
                f"SQLite a respins activarea pachetului: {exc}"
            ) from exc
        if activated != 1:
            raise TemplateSetStateError("Pachetul nu mai este în starea draft.")

        return TemplateSetValidationResult(
            template_set_id=template_set_id,
            can_activate=True,
            status="active",
            issues=(),
            report=package_report,
        )


def reject_template_set(
    connection: sqlite3.Connection,
    template_set_id: int,
    *,
    reason: str,
) -> None:
    """Marchează definitiv un draft abandonat, păstrând raportul și versiunea."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    if not isinstance(reason, str):
        raise TemplateSetValidationError("Motivul respingerii trebuie să fie text.")
    with write_transaction(connection):
        template_set = _get_set_or_raise(connection, template_set_id)
        package_report = _build_package_report(connection, template_set_id)
        package_report["status"] = "rejected"
        package_report["rejection_reason"] = reason.strip()
        package_report["rejected_at"] = _now_utc()
        _save_package_report(connection, template_set_id, package_report)
        rejected = template_repositories.reject_template_set(
            connection,
            template_set_id,
        )
        if rejected != 1:
            raise TemplateSetStateError("Pachetul draft nu a putut fi respins.")


def get_template_set_details(
    connection: sqlite3.Connection,
    template_set_id: int,
) -> dict[str, Any]:
    """Returnează pachetul, șabloanele, mapările și rapoartele asociate."""

    template_set_id = _validate_id(template_set_id, "template_set_id")
    template_set = template_repositories.get_template_set(
        connection,
        template_set_id,
    )
    if template_set is None:
        raise TemplateSetNotFoundError(
            f"Pachetul de șabloane cu ID {template_set_id} nu există."
        )
    templates = template_repositories.list_templates(connection, template_set_id)
    for template in templates:
        template["validation_report"] = _read_json_object(
            str(template.pop("validation_report_json")),
            "Raportul șablonului",
        )
        template["mappings"] = template_repositories.list_template_variable_mappings(
            connection,
            int(template["id"]),
        )
    template_set["import_report"] = _read_json_object(
        str(template_set.pop("import_report_json")),
        "Raportul pachetului",
    )
    template_set["templates"] = templates
    return template_set


def list_template_sets(
    connection: sqlite3.Connection,
    *,
    intervention_id: int | None = None,
) -> list[dict[str, Any]]:
    """Listează rezumatele pachetelor folosind rapoartele persistate."""

    if intervention_id is not None:
        intervention_id = _validate_id(intervention_id, "intervention_id")

    template_sets = template_repositories.list_template_sets(
        connection,
        intervention_id=intervention_id,
    )
    for template_set in template_sets:
        report = _read_json_object(
            str(template_set.pop("import_report_json")),
            "Raportul pachetului",
        )
        if "validation" in report:
            template_set["validation"] = report["validation"]
    return template_sets


def get_template_content(
    connection: sqlite3.Connection,
    template_id: int,
) -> dict[str, Any]:
    """Returnează bytes originali și modelul Rich Text al unui șablon păstrat."""

    template_id = _validate_id(template_id, "template_id")
    _get_template_or_raise(connection, template_id, require_draft=False)
    content = template_repositories.get_template_source(connection, template_id)
    if content is None:
        raise TemplateSetNotFoundError(f"Șablonul cu ID {template_id} nu există.")
    try:
        document = RichDocument.from_json(str(content["parsed_content_json"]))
    except (TypeError, ValueError) as exc:
        raise TemplateSetIntegrityError(
            f"Conținutul Rich Text al șablonului {template_id} este invalid."
        ) from exc
    return {
        "template_id": template_id,
        "source_docx": bytes(content["source_docx"]),
        "source_sha256": str(content["source_sha256"]),
        "document": document,
    }


def _validate_owner_and_document_type(
    owner_type: str,
    document_type: str | None = None,
) -> tuple[str, str]:
    if owner_type == "diagnosis":
        expected = EPICRISIS_RECOMMENDATIONS
    elif owner_type == "intervention":
        expected = OPERATIVE_PROTOCOL
    else:
        raise TemplateSetValidationError(
            "owner_type trebuie să fie 'diagnosis' sau 'intervention'."
        )
    if document_type is None:
        return owner_type, expected
    validated_type = _validate_document_type(document_type)
    if validated_type != expected:
        raise TemplateSetValidationError(
            f"Proprietarul '{owner_type}' acceptă exclusiv tipul '{expected}'."
        )
    return owner_type, validated_type


def _enrich_independent_template_row(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    owner_id = (
        item["diagnosis_id"]
        if item.get("owner_type") == "diagnosis"
        else item.get("intervention_id")
    )
    item["owner_id"] = int(owner_id)
    parser_report = _read_json_object(
        str(item.get("parser_report_json") or "{}"),
        "Raportul parserului",
    )
    item["parser_report"] = parser_report
    try:
        item["variables"] = sorted(_variables_from_report(parser_report))
    except TemplateSetIntegrityError:
        item["variables"] = []
    return item


def _get_independent_version_or_raise(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    require_draft: bool = False,
) -> dict[str, Any]:
    row = independent_template_repositories.get_independent_template_details(
        connection,
        template_version_id,
    )
    if row is None:
        raise TemplateSetNotFoundError(
            f"Versiunea de șablon independent cu ID {template_version_id} nu există."
        )
    enriched = _enrich_independent_template_row(row)
    if require_draft and enriched["status"] != "draft":
        raise TemplateSetStateError(
            "Doar versiunile în starea draft mai pot fi modificate sau validate."
        )
    return enriched


def _persist_independent_template(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_bytes: bytes,
    parsed: tuple[RichDocument, ImportReport, str],
    document_type: str | None = None,
) -> dict[str, Any]:
    validated_owner_type, resolved_document_type = _validate_owner_and_document_type(
        owner_type,
        document_type,
    )
    validated_owner_id = _validate_id(owner_id, "owner_id")
    if not isinstance(document_bytes, bytes) or not document_bytes:
        raise TemplateSetValidationError(
            "Fișierul DOCX este gol și nu a fost importat sau salvat."
        )

    document, import_report, digest = parsed
    if digest != hashlib.sha256(document_bytes).hexdigest():
        raise TemplateSetIntegrityError(
            "Hash-ul returnat de parser nu corespunde fișierului DOCX original."
        )

    if validated_owner_type == "diagnosis":
        owner = repositories.get_diagnosis(connection, validated_owner_id)
        if owner is None:
            raise TemplateSetNotFoundError(
                f"Diagnosticul cu ID {validated_owner_id} nu există."
            )
    else:
        owner = repositories.get_intervention(connection, validated_owner_id)
        if owner is None:
            raise TemplateSetNotFoundError(
                f"Intervenția cu ID {validated_owner_id} nu există."
            )

    has_reserved_variable = any(
        is_reserved_patient_name_variable_key(key)
        for key in import_report.variables
    )
    if import_report.status == "rejected" or has_reserved_variable:
        document_json = RichDocument().to_json()
        validation_status = "invalid"
    else:
        document_json = document.to_json()
        validation_status = "pending"

    with write_transaction(connection):
        version_no = (
            independent_template_repositories.get_next_independent_template_version(
                connection,
                owner_type=validated_owner_type,
                owner_id=validated_owner_id,
                document_type=resolved_document_type,
            )
        )
        version_id = independent_template_repositories.insert_independent_template_version(
            connection,
            owner_type=validated_owner_type,
            owner_id=validated_owner_id,
            document_type=resolved_document_type,
            version_no=version_no,
            source_docx=document_bytes,
            source_sha256=digest,
            parsed_content_json=document_json,
            parser_report_json=_json_text(import_report.to_dict()),
            parser_version=DOCX_PARSER_VERSION,
            validation_status=validation_status,
        )

    return get_independent_template_details(connection, version_id)


def import_preparsed_independent_template(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_bytes: bytes,
    parsed: tuple[RichDocument, ImportReport, str],
    document_type: str | None = None,
) -> dict[str, Any]:
    """Persistă un șablon independent deja parsat, participând la o tranzacție externă."""

    return _persist_independent_template(
        connection,
        owner_type=owner_type,
        owner_id=owner_id,
        document_bytes=document_bytes,
        parsed=parsed,
        document_type=document_type,
    )


def import_independent_template(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_bytes: bytes,
    document_type: str | None = None,
) -> dict[str, Any]:
    """Importă o versiune nouă de șablon independent în starea draft."""

    _require_no_active_transaction_for_docx_parse(connection)
    parsed = _parse_docx(document_bytes)
    return _persist_independent_template(
        connection,
        owner_type=owner_type,
        owner_id=owner_id,
        document_bytes=document_bytes,
        parsed=parsed,
        document_type=document_type,
    )


def list_independent_templates(
    connection: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: int,
    document_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Listează versiunile independente pentru un diagnostic sau o intervenție."""

    validated_owner_type, resolved_document_type = _validate_owner_and_document_type(
        owner_type,
        document_type,
    )
    validated_owner_id = _validate_id(owner_id, "owner_id")
    if status is not None and status not in {"draft", "active", "retired", "rejected"}:
        raise TemplateSetValidationError("Statutul solicitat nu este valid.")
    rows = independent_template_repositories.list_independent_template_versions(
        connection,
        owner_type=validated_owner_type,
        owner_id=validated_owner_id,
        document_type=resolved_document_type,
        status=status,
    )
    return [_enrich_independent_template_row(row) for row in rows]


def get_independent_template_details(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any]:
    """Returnează detaliile și raportul parserului pentru o versiune independentă."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    return _get_independent_version_or_raise(
        connection,
        validated_id,
        require_draft=False,
    )


def get_independent_template_content(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any]:
    """Returnează DOCX-ul original, conținutul Rich Text și raportul parserului."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    content = independent_template_repositories.get_independent_template_content(
        connection,
        validated_id,
    )
    if content is None:
        raise TemplateSetNotFoundError(
            f"Versiunea de șablon independent cu ID {validated_id} nu există."
        )
    parser_report = _read_json_object(
        str(content["parser_report_json"]),
        "Raportul parserului",
    )
    try:
        document = RichDocument.from_json(str(content["parsed_content_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TemplateSetIntegrityError(
            "Conținutul parsat al șablonului independent este corupt."
        ) from exc
    return {
        "template_version_id": validated_id,
        "source_docx": bytes(content["source_docx"]),
        "source_sha256": str(content["source_sha256"]),
        "document": document,
        "parser_report": parser_report,
    }


def confirm_independent_template_warnings(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Confirmă sau resetează revizuirea avertismentelor parserului pentru un draft."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    validated_confirmed = _validate_boolean(confirmed, "confirmed")
    with write_transaction(connection):
        row = _get_independent_version_or_raise(
            connection,
            validated_id,
            require_draft=True,
        )
        updated = independent_template_repositories.update_independent_template_confirmations(
            connection,
            validated_id,
            warnings_confirmed=validated_confirmed,
            warnings_confirmed_at=_now_utc() if validated_confirmed else None,
            patient_identification_reviewed=bool(row["patient_identification_reviewed"]),
            patient_identification_reviewed_at=row["patient_identification_reviewed_at"],
        )
        if updated != 1:
            raise TemplateSetStateError(
                "Confirmarea avertismentelor nu a putut fi actualizată."
            )
        if (
            not validated_confirmed
            and row["validation_status"] == "valid"
            and row["parser_report"].get("status") == "valid_with_warnings"
        ):
            independent_template_repositories.update_independent_template_validation(
                connection,
                validated_id,
                "pending",
            )
    return get_independent_template_details(connection, validated_id)


def confirm_independent_template_patient_review(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    confirmed: bool = True,
) -> dict[str, Any]:
    """Confirmă sau resetează verificarea identificatorilor de pacient pe draft."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    validated_confirmed = _validate_boolean(confirmed, "confirmed")
    with write_transaction(connection):
        row = _get_independent_version_or_raise(
            connection,
            validated_id,
            require_draft=True,
        )
        updated = independent_template_repositories.update_independent_template_confirmations(
            connection,
            validated_id,
            warnings_confirmed=bool(row["warnings_confirmed"]),
            warnings_confirmed_at=row["warnings_confirmed_at"],
            patient_identification_reviewed=validated_confirmed,
            patient_identification_reviewed_at=_now_utc()
            if validated_confirmed
            else None,
        )
        if updated != 1:
            raise TemplateSetStateError(
                "Confirmarea absenței datelor de pacient nu a putut fi actualizată."
            )
        if not validated_confirmed and row["validation_status"] == "valid":
            independent_template_repositories.update_independent_template_validation(
                connection,
                validated_id,
                "pending",
            )
    return get_independent_template_details(connection, validated_id)


def _validate_independent_template_locked(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    patient_identification_review_confirmed: bool | None = None,
) -> dict[str, Any]:
    row = _get_independent_version_or_raise(
        connection,
        template_version_id,
        require_draft=True,
    )
    if patient_identification_review_confirmed is not None:
        confirmed_flag = _validate_boolean(
            patient_identification_review_confirmed,
            "patient_identification_review_confirmed",
        )
        independent_template_repositories.update_independent_template_confirmations(
            connection,
            template_version_id,
            warnings_confirmed=bool(row["warnings_confirmed"]),
            warnings_confirmed_at=row["warnings_confirmed_at"],
            patient_identification_reviewed=confirmed_flag,
            patient_identification_reviewed_at=_now_utc() if confirmed_flag else None,
        )
        row = _get_independent_version_or_raise(
            connection,
            template_version_id,
            require_draft=True,
        )

    issues: list[dict[str, Any]] = []
    document_type = str(row["document_type"])
    parser_report = row["parser_report"]
    parser_status = parser_report.get("status")
    has_blocking_invalid = False

    if parser_status == "rejected":
        has_blocking_invalid = True
        _add_issue(
            issues,
            "docx_rejected",
            "Documentul Word a fost respins de validator și nu poate fi activat.",
            document_type=document_type,
        )
    elif parser_status not in {"valid", "valid_with_warnings"}:
        has_blocking_invalid = True
        _add_issue(
            issues,
            "invalid_import_status",
            "Raportul parserului are un statut necunoscut.",
            document_type=document_type,
        )

    try:
        detected = _variables_from_report(parser_report)
    except TemplateSetIntegrityError as exc:
        detected = {}
        has_blocking_invalid = True
        _add_issue(
            issues,
            "invalid_variable_report",
            str(exc),
            document_type=document_type,
        )

    for key in sorted(detected):
        if is_reserved_patient_name_variable_key(key):
            has_blocking_invalid = True
            _add_issue(
                issues,
                "reserved_patient_name_variable",
                f"Variabila rezervată {key!r} pentru numele pacientului nu este permisă.",
                document_type=document_type,
                variable_key=key,
            )

    content = independent_template_repositories.get_independent_template_content(
        connection,
        template_version_id,
    )
    if content is None or not bytes(content["source_docx"]):
        has_blocking_invalid = True
        _add_issue(
            issues,
            "missing_source_docx",
            "Fișierul DOCX original lipsește din baza de date.",
            document_type=document_type,
        )
    elif parser_status != "rejected":
        try:
            RichDocument.from_json(str(content["parsed_content_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            has_blocking_invalid = True
            _add_issue(
                issues,
                "corrupted_parsed_content",
                "Conținutul Rich Text parsat al versiunii este corupt.",
                document_type=document_type,
            )

    if parser_status == "valid_with_warnings" and not bool(row["warnings_confirmed"]):
        _add_issue(
            issues,
            "warnings_not_confirmed",
            "Avertismentele parserului trebuie confirmate explicit înainte de activare.",
            document_type=document_type,
        )
    if not bool(row["owner_is_active"]):
        _add_issue(
            issues,
            "inactive_owner",
            "Diagnosticul sau intervenția proprietară este inactivă.",
            document_type=document_type,
        )
    if not bool(row["patient_identification_reviewed"]):
        _add_issue(
            issues,
            "patient_identification_review_not_confirmed",
            "Confirmați că șablonul nu conține nume sau identificatori de pacienți.",
            document_type=document_type,
        )

    if has_blocking_invalid:
        new_status = "invalid"
    elif not issues:
        new_status = "valid"
    else:
        new_status = "pending"

    independent_template_repositories.update_independent_template_validation(
        connection,
        template_version_id,
        new_status,
    )
    details = _get_independent_version_or_raise(
        connection,
        template_version_id,
        require_draft=True,
    )
    details["can_activate"] = new_status == "valid" and not issues
    details["validation_issues"] = issues
    return details


def validate_independent_template(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    patient_identification_review_confirmed: bool | None = None,
) -> dict[str, Any]:
    """Validează o versiune draft independentă și persistă validation_status."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    if patient_identification_review_confirmed is not None:
        _validate_boolean(
            patient_identification_review_confirmed,
            "patient_identification_review_confirmed",
        )
    with write_transaction(connection):
        return _validate_independent_template_locked(
            connection,
            validated_id,
            patient_identification_review_confirmed=patient_identification_review_confirmed,
        )


def activate_independent_template(
    connection: sqlite3.Connection,
    template_version_id: int,
    *,
    patient_identification_review_confirmed: bool | None = None,
) -> dict[str, Any]:
    """Validează și activează atomic versiunea, retrăgând versiunea activă anterioară."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    if patient_identification_review_confirmed is not None:
        _validate_boolean(
            patient_identification_review_confirmed,
            "patient_identification_review_confirmed",
        )
    with write_transaction(connection):
        details = _validate_independent_template_locked(
            connection,
            validated_id,
            patient_identification_review_confirmed=patient_identification_review_confirmed,
        )
        if not details["can_activate"]:
            reasons = "; ".join(
                str(issue["message"]) for issue in details["validation_issues"]
            )
            raise TemplateSetStateError(
                f"Versiunea nu îndeplinește condițiile de activare: {reasons}"
            )

        now = _now_utc()
        previous = independent_template_repositories.get_active_independent_template_version(
            connection,
            owner_type=str(details["owner_type"]),
            owner_id=int(details["owner_id"]),
            document_type=str(details["document_type"]),
        )
        try:
            if previous is not None and int(previous["id"]) != validated_id:
                retired = independent_template_repositories.retire_independent_template_version(
                    connection,
                    int(previous["id"]),
                    now,
                )
                if retired != 1:
                    raise TemplateSetStateError(
                        "Versiunea activă anterioară nu a putut fi retrasă."
                    )
            activated = independent_template_repositories.activate_independent_template_version(
                connection,
                validated_id,
                now,
            )
        except sqlite3.IntegrityError as exc:
            raise TemplateSetIntegrityError(
                f"SQLite a respins activarea versiunii independente: {exc}"
            ) from exc
        if activated != 1:
            raise TemplateSetStateError("Versiunea nu a putut fi activată.")

    return get_independent_template_details(connection, validated_id)


def reject_independent_template(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any]:
    """Marchează definitiv un draft independent drept respins."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    with write_transaction(connection):
        _get_independent_version_or_raise(
            connection,
            validated_id,
            require_draft=True,
        )
        rejected = independent_template_repositories.reject_independent_template_version(
            connection,
            validated_id,
        )
        if rejected != 1:
            raise TemplateSetStateError("Versiunea draft nu a putut fi respinsă.")
    return get_independent_template_details(connection, validated_id)


def retire_independent_template(
    connection: sqlite3.Connection,
    template_version_id: int,
) -> dict[str, Any]:
    """Retrage explicit o versiune independentă aflată în starea activă."""

    validated_id = _validate_id(template_version_id, "template_version_id")
    with write_transaction(connection):
        row = _get_independent_version_or_raise(
            connection,
            validated_id,
            require_draft=False,
        )
        if row["status"] != "active":
            raise TemplateSetStateError("Doar o versiune activă poate fi retrasă.")
        retired = independent_template_repositories.retire_independent_template_version(
            connection,
            validated_id,
            _now_utc(),
        )
        if retired != 1:
            raise TemplateSetStateError("Versiunea activă nu a putut fi retrasă.")
    return get_independent_template_details(connection, validated_id)
