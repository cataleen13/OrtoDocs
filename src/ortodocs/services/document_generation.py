"""Încărcarea formularului și generarea atomică a documentelor OrtoDocs."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ortodocs.models.generation import (
    FieldValidationIssue,
    GeneratedSession,
    GenerationDocumentType,
    GenerationField,
    GenerationFieldOption,
    GenerationForm,
    PreparedGeneration,
)
from ortodocs.models.rich_document import RichDocument, TextInline
from ortodocs.services import template_sets
from ortodocs.services.medical_library import (
    is_reserved_patient_name_variable_key,
)
from ortodocs.services.template_sets import TemplateSetError
from ortodocs.services.token_substitution import (
    TokenSubstitutionError,
    substitute_variables,
)
from ortodocs.storage import (
    independent_template_repositories,
    repositories,
    template_repositories,
)
from ortodocs.storage.database import (
    DatabaseConfigurationError,
    DatabaseIntegrityError,
    save_generation_session,
)
from ortodocs.storage.generation_repositories import GenerationPersistenceError


EPICRISIS_RECOMMENDATIONS = "epicrisis_recommendations"
OPERATIVE_PROTOCOL = "operative_protocol"
GENERATION_DOCUMENT_TYPES: tuple[GenerationDocumentType, ...] = (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
)
_DOCUMENT_TYPES = frozenset(GENERATION_DOCUMENT_TYPES)
_DOCUMENT_LABELS = {
    EPICRISIS_RECOMMENDATIONS: "Epicriză + Recomandări",
    OPERATIVE_PROTOCOL: "Protocol operator",
}
_VARIABLE_KEY = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_NUMBER_VALUE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z", re.ASCII)
_DATE_VALUE = re.compile(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}\Z", re.ASCII)
_BUILTIN_V3_FIELDS: tuple[
    tuple[
        str,
        str,
        str,
        tuple[GenerationFieldOption, ...],
        tuple[GenerationDocumentType, ...],
    ],
    ...,
] = (
    (
        "varsta",
        "Vârstă",
        "text",
        (),
        (EPICRISIS_RECOMMENDATIONS,),
    ),
    (
        "sex",
        "Sex (Pacient / Pacientă)",
        "select",
        (
            GenerationFieldOption("Pacient", "Pacient", 1),
            GenerationFieldOption("Pacientă", "Pacientă", 2),
        ),
        (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
    ),
    (
        "lateralitate",
        "Lateralitate",
        "select",
        (
            GenerationFieldOption("dreapta", "dreapta", 1),
            GenerationFieldOption("stânga", "stânga", 2),
            GenerationFieldOption("bilateral", "bilateral", 3),
        ),
        (EPICRISIS_RECOMMENDATIONS, OPERATIVE_PROTOCOL),
    ),
    (
        "regim_prezentare",
        "Regim de prezentare",
        "select",
        (
            GenerationFieldOption("programat", "programat", 1),
            GenerationFieldOption("urgență", "urgență", 2),
        ),
        (EPICRISIS_RECOMMENDATIONS,),
    ),
    (
        "data_operatiei",
        "Data operației",
        "date",
        (),
        (EPICRISIS_RECOMMENDATIONS,),
    ),
    (
        "medic_anestezist",
        "Medic anestezist",
        "text",
        (),
        (EPICRISIS_RECOMMENDATIONS,),
    ),
)


class GenerationError(Exception):
    """Eroare de domeniu pentru pregătirea sau generarea documentelor."""


class GenerationContextError(GenerationError):
    """Intervenția sau diagnosticul nu permite generarea în acest moment."""


class GenerationTemplateError(GenerationError):
    """Perechea activă ori mapările sale nu sunt utilizabile."""


class GenerationConflictError(GenerationError):
    """Contextul de generare s-a schimbat sau conexiunea nu poate fi folosită."""


class GenerationConfigurationError(GenerationError):
    """Conexiunea SQLite nu respectă configurația obligatorie pentru scrieri."""


class GenerationValidationError(GenerationError, ValueError):
    """Valorile formularului sunt invalide; ``issues`` conține toate problemele."""

    def __init__(self, issues: tuple[FieldValidationIssue, ...]) -> None:
        self.issues = tuple(issues)
        message = "; ".join(issue.message for issue in self.issues)
        super().__init__(message or "Valorile formularului nu sunt valide.")


def _validate_connection(connection: sqlite3.Connection) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection trebuie să fie o conexiune sqlite3.Connection.")


def _validate_intervention_id(intervention_id: int) -> None:
    if (
        isinstance(intervention_id, bool)
        or not isinstance(intervention_id, int)
        or intervention_id < 1
    ):
        raise ValueError("intervention_id trebuie să fie un întreg pozitiv.")


def _template_variable_keys(template: Mapping[str, Any]) -> set[str]:
    report = template.get("validation_report")
    if not isinstance(report, dict):
        raise GenerationTemplateError("Raportul de validare al șablonului este invalid.")
    import_report = report.get("import_report")
    if not isinstance(import_report, dict):
        raise GenerationTemplateError("Raportul șablonului nu conține import_report valid.")
    if import_report.get("status") not in {"valid", "valid_with_warnings"}:
        raise GenerationTemplateError("Raportul importului nu este într-o stare acceptată.")
    if import_report.get("status") == "valid_with_warnings":
        confirmation = report.get("warning_confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
            raise GenerationTemplateError(
                "Avertizările șablonului nu au fost confirmate înainte de activare."
            )

    raw_variables = import_report.get("variables")
    if not isinstance(raw_variables, dict):
        raise GenerationTemplateError("Lista variabilelor din raport este invalidă.")
    variable_keys: set[str] = set()
    for key, occurrence_count in raw_variables.items():
        if (
            not isinstance(key, str)
            or not _VARIABLE_KEY.fullmatch(key)
            or isinstance(occurrence_count, bool)
            or not isinstance(occurrence_count, int)
            or occurrence_count < 1
        ):
            raise GenerationTemplateError(
                "Raportul importului conține o cheie sau o numărătoare invalidă."
            )
        variable_keys.add(key)
    return variable_keys


def _validate_mapping_definition(
    mapping: Mapping[str, Any],
    *,
    variable_key: str,
    diagnosis_id: int,
    intervention_id: int,
    options: tuple[GenerationFieldOption, ...],
) -> tuple[int, int, str, str, str, tuple[GenerationFieldOption, ...], bool]:
    if mapping.get("variable_key") != variable_key:
        raise GenerationTemplateError(
            f"Maparea pentru {variable_key!r} indică o cheie diferită în bibliotecă."
        )
    field_id = mapping.get("field_definition_id")
    version = mapping.get("version")
    if (
        isinstance(field_id, bool)
        or not isinstance(field_id, int)
        or field_id < 1
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
    ):
        raise GenerationTemplateError(
            f"Definiția mapată pentru {variable_key!r} nu are identificator sau versiune validă."
        )

    label = mapping.get("label")
    data_type = mapping.get("data_type")
    scope = mapping.get("scope")
    if not isinstance(label, str) or not label.strip():
        raise GenerationTemplateError(f"Eticheta câmpului {variable_key!r} este invalidă.")
    if data_type not in {"text", "number", "date", "select"}:
        raise GenerationTemplateError(f"Tipul câmpului {variable_key!r} este invalid.")

    definition_diagnosis_id = mapping.get("diagnosis_id")
    definition_intervention_id = mapping.get("intervention_id")
    if scope == "common":
        scope_is_valid = (
            definition_diagnosis_id is None and definition_intervention_id is None
        )
    elif scope == "diagnosis":
        scope_is_valid = (
            definition_diagnosis_id == diagnosis_id
            and definition_intervention_id is None
        )
    elif scope == "intervention":
        scope_is_valid = (
            definition_diagnosis_id is None
            and definition_intervention_id == intervention_id
        )
    else:
        scope_is_valid = False
    if not scope_is_valid:
        raise GenerationTemplateError(
            f"Domeniul câmpului {variable_key!r} nu corespunde intervenției selectate."
        )

    raw_required = mapping.get("is_required")
    if isinstance(raw_required, bool) or raw_required not in (0, 1):
        raise GenerationTemplateError(
            f"Regula obligatoriu/opțional pentru {variable_key!r} este invalidă."
        )

    if data_type == "select" and not options:
        raise GenerationTemplateError(
            f"Câmpul select {variable_key!r} nu are opțiuni pentru versiunea mapată."
        )
    if data_type != "select" and options:
        raise GenerationTemplateError(
            f"Câmpul {variable_key!r} are opțiuni, dar nu este de tip select."
        )

    return (
        field_id,
        version,
        label,
        data_type,
        scope,
        options,
        bool(raw_required),
    )


def _has_any_independent_templates(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM independent_template_versions LIMIT 1"
    ).fetchone()
    return row is not None


def _load_select_options(
    connection: sqlite3.Connection,
    field_definition_id: int,
    *,
    variable_key: str,
) -> tuple[GenerationFieldOption, ...]:
    raw_options = [
        option
        for option in repositories.list_field_options(connection, field_definition_id)
        if option.get("is_active", 1) not in (0, False)
    ]
    try:
        return tuple(
            GenerationFieldOption(
                option_value=option["option_value"],
                option_label=option["option_label"],
                sort_order=option["sort_order"],
            )
            for option in raw_options
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationTemplateError(
            f"Opțiunile pentru câmpul {variable_key!r} sunt invalide."
        ) from exc


def _matching_current_field_definition(
    connection: sqlite3.Connection,
    variable_key: str,
    *,
    diagnosis_id: int,
    intervention_id: int,
) -> Mapping[str, Any] | None:
    definitions = repositories.list_field_definitions(
        connection,
        variable_key=variable_key,
        current_only=True,
    )
    by_priority: list[tuple[int, Mapping[str, Any]]] = []
    for definition in definitions:
        scope = definition.get("scope")
        if (
            scope == "intervention"
            and definition.get("diagnosis_id") is None
            and definition.get("intervention_id") == intervention_id
        ):
            by_priority.append((0, definition))
        elif (
            scope == "diagnosis"
            and definition.get("diagnosis_id") == diagnosis_id
            and definition.get("intervention_id") is None
        ):
            by_priority.append((1, definition))
        elif (
            scope == "common"
            and definition.get("diagnosis_id") is None
            and definition.get("intervention_id") is None
        ):
            by_priority.append((2, definition))
    if not by_priority:
        return None
    by_priority.sort(key=lambda item: item[0])
    return by_priority[0][1]


def _validate_active_independent_template(
    connection: sqlite3.Connection,
    row: Mapping[str, Any] | None,
    *,
    expected_document_type: GenerationDocumentType,
) -> tuple[int, RichDocument, tuple[str, ...]]:
    label_ro = _DOCUMENT_LABELS[expected_document_type]
    if row is None:
        raise GenerationTemplateError(
            f"Nu există șablon activ «{label_ro}»."
        )
    if row.get("status") != "active" or row.get("document_type") != expected_document_type:
        raise GenerationTemplateError(
            f"Șablonul activ pentru «{label_ro}» nu are tipul sau statutul așteptat."
        )
    if row.get("validation_status") != "valid":
        raise GenerationTemplateError(
            f"Șablonul «{label_ro}» nu are statut de validare valid."
        )
    if row.get("patient_identification_reviewed") != 1:
        raise GenerationTemplateError(
            f"Șablonul «{label_ro}» nu are confirmată absența datelor de identificare a pacientului."
        )

    try:
        parser_report = json.loads(str(row.get("parser_report_json") or ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise GenerationTemplateError(
            f"Raportul parserului pentru «{label_ro}» nu conține JSON valid."
        ) from exc
    if not isinstance(parser_report, dict):
        raise GenerationTemplateError(
            f"Raportul parserului pentru «{label_ro}» are o structură invalidă."
        )
    parser_status = parser_report.get("status")
    if parser_status not in {"valid", "valid_with_warnings"}:
        raise GenerationTemplateError(
            f"Șablonul «{label_ro}» nu are un raport de parsare utilizabil."
        )
    if parser_status == "valid_with_warnings" and row.get("warnings_confirmed") != 1:
        raise GenerationTemplateError(
            f"Avertismentele parserului pentru «{label_ro}» nu au fost confirmate."
        )

    raw_variables = parser_report.get("variables", {})
    if not isinstance(raw_variables, dict):
        raise GenerationTemplateError(
            f"Lista variabilelor din raportul pentru «{label_ro}» este invalidă."
        )
    detected_keys: list[str] = []
    for key, occurrence_count in raw_variables.items():
        if (
            not isinstance(key, str)
            or not _VARIABLE_KEY.fullmatch(key)
            or isinstance(occurrence_count, bool)
            or not isinstance(occurrence_count, int)
            or occurrence_count < 1
        ):
            raise GenerationTemplateError(
                f"Raportul pentru «{label_ro}» conține o variabilă invalidă."
            )
        if is_reserved_patient_name_variable_key(key):
            raise GenerationTemplateError(
                f"Variabila rezervată {key!r} pentru numele pacientului este interzisă în «{label_ro}»."
            )
        detected_keys.append(key)

    version_id = int(row["id"])
    content = independent_template_repositories.get_independent_template_content(
        connection,
        version_id,
    )
    if (
        content is None
        or not bytes(content["source_docx"])
        or len(str(content["source_sha256"])) != 64
    ):
        raise GenerationTemplateError(
            f"Datele sursă pentru șablonul activ «{label_ro}» sunt incomplete."
        )
    try:
        document = RichDocument.from_json(str(content["parsed_content_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GenerationTemplateError(
            f"Conținutul Rich Text al șablonului «{label_ro}» este corupt: {exc}"
        ) from exc

    return version_id, document, tuple(detected_keys)


def _field_from_db_definition(
    connection: sqlite3.Connection,
    *,
    variable_key: str,
    definition: Mapping[str, Any],
    used_in: tuple[GenerationDocumentType, ...],
) -> GenerationField:
    field_id = definition.get("id")
    version = definition.get("version")
    label = definition.get("label")
    data_type = definition.get("data_type")
    scope = definition.get("scope")
    if (
        isinstance(field_id, bool)
        or not isinstance(field_id, int)
        or field_id < 1
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(label, str)
        or not label.strip()
        or data_type not in {"text", "number", "date", "select"}
        or scope not in {"common", "diagnosis", "intervention"}
    ):
        raise GenerationTemplateError(
            f"Definiția din bibliotecă pentru variabila {variable_key!r} este invalidă."
        )
    options: tuple[GenerationFieldOption, ...] = ()
    if data_type == "select":
        options = _load_select_options(
            connection,
            field_id,
            variable_key=variable_key,
        )
        if not options:
            raise GenerationTemplateError(
                f"Câmpul select {variable_key!r} nu are opțiuni active configurate."
            )
    return GenerationField(
        variable_key=variable_key,
        field_definition_id=field_id,
        label=label,
        data_type=data_type,
        scope=scope,
        version=version,
        options=options,
        required_in=(),
        used_in=used_in,
    )


def _load_independent_generation_context(
    connection: sqlite3.Connection,
    *,
    diagnosis: Mapping[str, Any],
    intervention: Mapping[str, Any],
) -> tuple[
    GenerationForm,
    dict[str, RichDocument],
    dict[str, int],
    str,
    str,
    dict[GenerationDocumentType, tuple[str, ...]],
]:
    diagnosis_id = int(diagnosis["id"])
    intervention_id = int(intervention["id"])

    epicrisis_row = (
        independent_template_repositories.get_active_independent_template_version(
            connection,
            owner_type="diagnosis",
            owner_id=diagnosis_id,
            document_type=EPICRISIS_RECOMMENDATIONS,
        )
    )
    protocol_row = (
        independent_template_repositories.get_active_independent_template_version(
            connection,
            owner_type="intervention",
            owner_id=intervention_id,
            document_type=OPERATIVE_PROTOCOL,
        )
    )
    if epicrisis_row is None and protocol_row is None:
        raise GenerationTemplateError(
            "Nu există șabloane active pentru «Epicriză + Recomandări» (diagnostic) "
            "și «Protocol operator» (intervenție)."
        )

    epi_id, epi_doc, epi_keys = _validate_active_independent_template(
        connection,
        epicrisis_row,
        expected_document_type=EPICRISIS_RECOMMENDATIONS,
    )
    proto_id, proto_doc, proto_keys = _validate_active_independent_template(
        connection,
        protocol_row,
        expected_document_type=OPERATIVE_PROTOCOL,
    )

    detected_by_doc: dict[GenerationDocumentType, tuple[str, ...]] = {
        EPICRISIS_RECOMMENDATIONS: epi_keys,
        OPERATIVE_PROTOCOL: proto_keys,
    }
    detected_sets = {
        doc_type: frozenset(keys)
        for doc_type, keys in detected_by_doc.items()
    }

    fields: list[GenerationField] = []
    seen_keys: set[str] = set()
    for (
        key,
        default_label,
        default_data_type,
        default_options,
        _default_used_in,
    ) in _BUILTIN_V3_FIELDS:
        actual_used_in = tuple(
            doc_type
            for doc_type in GENERATION_DOCUMENT_TYPES
            if key in detected_sets[doc_type]
        )
        fields.append(
            GenerationField(
                variable_key=key,
                field_definition_id=None,
                label=default_label,
                data_type=default_data_type,  # type: ignore[arg-type]
                scope="common",
                version=1,
                options=default_options,
                required_in=(),
                used_in=actual_used_in,
            )
        )
        seen_keys.add(key)

    ordered_dynamic_keys: list[str] = []
    for key in (*epi_keys, *proto_keys):
        if key not in seen_keys and key not in ordered_dynamic_keys:
            ordered_dynamic_keys.append(key)

    for key in ordered_dynamic_keys:
        actual_used_in = tuple(
            doc_type
            for doc_type in GENERATION_DOCUMENT_TYPES
            if key in detected_sets[doc_type]
        )
        all_definitions = repositories.list_field_definitions(
            connection,
            variable_key=key,
        )
        if not all_definitions:
            fields.append(
                GenerationField(
                    variable_key=key,
                    field_definition_id=None,
                    label=key.replace("_", " ").capitalize(),
                    data_type="text",
                    scope="common",
                    version=1,
                    options=(),
                    required_in=(),
                    used_in=actual_used_in,
                )
            )
            seen_keys.add(key)
            continue

        definition = _matching_current_field_definition(
            connection,
            key,
            diagnosis_id=diagnosis_id,
            intervention_id=intervention_id,
        )
        if definition is None:
            if not any(item.get("is_current") == 1 for item in all_definitions):
                raise GenerationTemplateError(
                    f"Variabila {key!r} are definiții în bibliotecă, dar nicio versiune nu este activă."
                )
            raise GenerationTemplateError(
                f"Definiția variabilei {key!r} are un domeniu incompatibil cu diagnosticul "
                "sau intervenția selectată."
            )

        fields.append(
            _field_from_db_definition(
                connection,
                variable_key=key,
                definition=definition,
                used_in=actual_used_in,
            )
        )
        seen_keys.add(key)

    form = GenerationForm(
        diagnosis_id=diagnosis_id,
        intervention_id=intervention_id,
        template_set_id=None,
        fields=tuple(fields),
        epicrisis_template_version_id=epi_id,
        operative_template_version_id=proto_id,
    )
    return (
        form,
        {
            EPICRISIS_RECOMMENDATIONS: epi_doc,
            OPERATIVE_PROTOCOL: proto_doc,
        },
        {
            EPICRISIS_RECOMMENDATIONS: epi_id,
            OPERATIVE_PROTOCOL: proto_id,
        },
        str(diagnosis["name"]),
        str(intervention["name"]),
        detected_by_doc,
    )


def _load_generation_context(
    connection: sqlite3.Connection,
    intervention_id: int,
) -> tuple[
    GenerationForm,
    dict[str, RichDocument],
    dict[str, int],
    str,
    str,
    dict[GenerationDocumentType, tuple[str, ...]],
]:
    _validate_connection(connection)
    _validate_intervention_id(intervention_id)

    intervention = repositories.get_intervention(connection, intervention_id)
    if intervention is None:
        raise GenerationContextError(f"Intervenția cu ID {intervention_id} nu există.")
    if intervention["is_active"] != 1:
        raise GenerationContextError(f"Intervenția cu ID {intervention_id} este inactivă.")
    diagnosis_id = intervention["diagnosis_id"]
    diagnosis = repositories.get_diagnosis(connection, diagnosis_id)
    if diagnosis is None:
        raise GenerationContextError(
            f"Diagnosticul asociat intervenției {intervention_id} nu mai există."
        )
    if diagnosis["is_active"] != 1:
        raise GenerationContextError(f"Diagnosticul cu ID {diagnosis_id} este inactiv.")

    if _has_any_independent_templates(connection):
        return _load_independent_generation_context(
            connection,
            diagnosis=diagnosis,
            intervention=intervention,
        )

    active_set = template_repositories.get_active_template_set(
        connection,
        intervention_id,
    )
    if active_set is None:
        return _load_independent_generation_context(
            connection,
            diagnosis=diagnosis,
            intervention=intervention,
        )

    return _load_legacy_generation_context(
        connection,
        intervention_id=intervention_id,
        diagnosis_id=int(diagnosis_id),
        diagnosis=diagnosis,
        intervention=intervention,
        active_set=active_set,
    )


def _load_legacy_generation_context(
    connection: sqlite3.Connection,
    *,
    intervention_id: int,
    diagnosis_id: int,
    diagnosis: Mapping[str, Any],
    intervention: Mapping[str, Any],
    active_set: Mapping[str, Any],
) -> tuple[
    GenerationForm,
    dict[str, RichDocument],
    dict[str, int],
    str,
    str,
    dict[GenerationDocumentType, tuple[str, ...]],
]:
    template_set_id = int(active_set["id"])

    try:
        details = template_sets.get_template_set_details(connection, template_set_id)
    except TemplateSetError as exc:
        raise GenerationTemplateError(
            f"Setul activ {template_set_id} nu poate fi citit: {exc}"
        ) from exc

    if (
        details.get("status") != "active"
        or details.get("intervention_id") != intervention_id
        or details.get("diagnosis_id") != diagnosis_id
    ):
        raise GenerationTemplateError(
            f"Setul de șabloane {template_set_id} nu aparține contextului activ selectat."
        )
    if details.get("diagnosis_is_active") != 1 or details.get("intervention_is_active") != 1:
        raise GenerationContextError(
            "Diagnosticul și intervenția trebuie să fie active pentru generare."
        )

    templates = details.get("templates")
    if not isinstance(templates, list) or len(templates) != 2:
        raise GenerationTemplateError(
            f"Setul activ {template_set_id} nu conține exact două șabloane."
        )
    templates_by_type: dict[str, dict[str, Any]] = {}
    for template in templates:
        if not isinstance(template, dict):
            raise GenerationTemplateError("Setul conține metadate de șablon invalide.")
        document_type = template.get("document_type")
        if document_type not in _DOCUMENT_TYPES or document_type in templates_by_type:
            raise GenerationTemplateError(
                f"Setul activ {template_set_id} conține tipuri de documente invalide sau duplicate."
            )
        templates_by_type[document_type] = template
    if set(templates_by_type) != _DOCUMENT_TYPES:
        raise GenerationTemplateError(
            f"Setul activ {template_set_id} nu are perechea corectă de documente."
        )

    template_documents: dict[str, RichDocument] = {}
    template_ids: dict[str, int] = {}
    field_specs: dict[str, dict[str, Any]] = {}
    options_cache: dict[int, tuple[GenerationFieldOption, ...]] = {}

    for document_type in GENERATION_DOCUMENT_TYPES:
        template = templates_by_type[document_type]
        if template.get("validation_status") != "valid":
            raise GenerationTemplateError(
                f"Șablonul {document_type!r} din setul {template_set_id} nu este validat."
            )
        validation_report = template.get("validation_report")
        if (
            not isinstance(validation_report, dict)
            or not isinstance(validation_report.get("validation"), dict)
            or validation_report["validation"].get("status") != "valid"
        ):
            raise GenerationTemplateError(
                f"Raportul de validare pentru {document_type!r} nu este valid."
            )

        expected_keys = _template_variable_keys(template)
        mappings = template.get("mappings")
        if not isinstance(mappings, list):
            raise GenerationTemplateError(
                f"Mapările pentru {document_type!r} nu au structura așteptată."
            )
        mapping_by_key: dict[str, Mapping[str, Any]] = {}
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise GenerationTemplateError(
                    f"Setul conține o mapare invalidă pentru {document_type!r}."
                )
            key = mapping.get("variable_key")
            if not isinstance(key, str) or not _VARIABLE_KEY.fullmatch(key):
                raise GenerationTemplateError(
                    f"Setul conține o cheie invalidă pentru {document_type!r}."
                )
            if key in mapping_by_key:
                raise GenerationTemplateError(
                    f"Variabila {key!r} are mapări duplicate în {document_type!r}."
                )
            mapping_by_key[key] = mapping
        if set(mapping_by_key) != expected_keys:
            raise GenerationTemplateError(
                f"Mapările și token-urile din {document_type!r} nu corespund exact."
            )
        try:
            content = template_sets.get_template_content(
                connection,
                int(template["id"]),
            )
        except TemplateSetError as exc:
            raise GenerationTemplateError(
                f"Conținutul șablonului {document_type!r} este invalid: {exc}"
            ) from exc
        if content.get("template_id") != template.get("id"):
            raise GenerationTemplateError(
                f"Conținutul recuperat pentru {document_type!r} are alt identificator."
            )
        document = content.get("document")
        if not isinstance(document, RichDocument):
            raise GenerationTemplateError(
                f"Șablonul {document_type!r} nu conține un RichDocument valid."
            )
        template_documents[document_type] = document
        template_ids[document_type] = int(template["id"])

        for key, mapping in mapping_by_key.items():
            field_id = mapping.get("field_definition_id")
            if isinstance(field_id, bool) or not isinstance(field_id, int) or field_id < 1:
                raise GenerationTemplateError(
                    f"Definiția mapată pentru {key!r} nu are ID valid."
                )
            if field_id not in options_cache:
                raw_options = repositories.list_field_options(connection, field_id)
                try:
                    options_cache[field_id] = tuple(
                        GenerationFieldOption(
                            option_value=option["option_value"],
                            option_label=option["option_label"],
                            sort_order=option["sort_order"],
                        )
                        for option in raw_options
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise GenerationTemplateError(
                        f"Opțiunile pentru câmpul {key!r} sunt invalide."
                    ) from exc
            validated = _validate_mapping_definition(
                mapping,
                variable_key=key,
                diagnosis_id=int(diagnosis_id),
                intervention_id=intervention_id,
                options=options_cache[field_id],
            )
            field_id, version, label, data_type, scope, options, is_required = validated
            previous = field_specs.get(key)
            if previous is None:
                field_specs[key] = {
                    "field_definition_id": field_id,
                    "version": version,
                    "label": label,
                    "data_type": data_type,
                    "scope": scope,
                    "options": options,
                    "required_in": set(),
                    "used_in": set(),
                }
            else:
                if (
                    previous["field_definition_id"] != field_id
                    or previous["version"] != version
                ):
                    raise GenerationTemplateError(
                        f"Variabila comună {key!r} indică definiții sau versiuni diferite "
                        "în cele două șabloane."
                    )
                if any(
                    previous[name] != value
                    for name, value in (
                        ("label", label),
                        ("data_type", data_type),
                        ("scope", scope),
                        ("options", options),
                    )
                ):
                    raise GenerationTemplateError(
                        f"Definiția mapată pentru {key!r} are metadate inconsistente."
                    )
            field_specs[key]["used_in"].add(document_type)
            if is_required:
                field_specs[key]["required_in"].add(document_type)

    fields = tuple(
        GenerationField(
            variable_key=key,
            field_definition_id=spec["field_definition_id"],
            label=spec["label"],
            data_type=spec["data_type"],
            scope=spec["scope"],
            version=spec["version"],
            options=spec["options"],
            required_in=tuple(
                document_type
                for document_type in GENERATION_DOCUMENT_TYPES
                if document_type in spec["required_in"]
            ),
            used_in=tuple(
                document_type
                for document_type in GENERATION_DOCUMENT_TYPES
                if document_type in spec["used_in"]
            ),
        )
        for key, spec in sorted(field_specs.items())
    )
    form = GenerationForm(
        diagnosis_id=int(diagnosis_id),
        intervention_id=intervention_id,
        template_set_id=template_set_id,
        fields=fields,
    )
    expected_keys_by_doc: dict[GenerationDocumentType, tuple[str, ...]] = {
        document_type: tuple(
            field.variable_key
            for field in fields
            if document_type in field.used_in
        )
        for document_type in GENERATION_DOCUMENT_TYPES
    }
    return (
        form,
        template_documents,
        template_ids,
        str(diagnosis["name"]),
        str(intervention["name"]),
        expected_keys_by_doc,
    )


def get_generation_form(
    connection: sqlite3.Connection,
    intervention_id: int,
) -> GenerationForm:
    """Construiește formularul din șabloanele active."""

    form, _, _, _, _, _ = _load_generation_context(connection, intervention_id)
    return form


def _make_issue(
    variable_key: str,
    reason: str,
    document_types: tuple[GenerationDocumentType, ...],
    message: str,
) -> FieldValidationIssue:
    return FieldValidationIssue(
        variable_key=variable_key,
        reason=reason,
        document_types=document_types,
        message=message,
    )


def _validate_value(field: GenerationField, value: str) -> str | None:
    try:
        TextInline(value)
    except (TypeError, ValueError) as exc:
        return f"Valoarea câmpului {field.label!r} nu poate fi reprezentată în document: {exc}"

    if field.data_type == "text":
        return None
    if field.data_type == "number":
        if not _NUMBER_VALUE.fullmatch(value):
            return f"Câmpul {field.label!r} trebuie să conțină un număr zecimal valid."
        try:
            number = Decimal(value)
        except InvalidOperation:
            return f"Câmpul {field.label!r} trebuie să conțină un număr zecimal valid."
        if not number.is_finite():
            return f"Câmpul {field.label!r} nu poate conține NaN sau Infinity."
        return None
    if field.data_type == "date":
        if not _DATE_VALUE.fullmatch(value):
            return f"Câmpul {field.label!r} trebuie să folosească formatul DD.MM.YYYY."
        try:
            datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            return f"Data din câmpul {field.label!r} nu este o dată calendaristică validă."
        return None
    if field.data_type == "select":
        allowed_values = {option.option_value for option in field.options}
        if value not in allowed_values:
            return f"Valoarea câmpului {field.label!r} nu este o opțiune configurată."
        return None
    return f"Tipul câmpului {field.label!r} nu este acceptat."


def _validate_values(
    form: GenerationForm,
    values: Mapping[str, str],
) -> dict[str, str]:
    if not isinstance(values, Mapping):
        issue = _make_issue(
            "values",
            "invalid_mapping",
            GENERATION_DOCUMENT_TYPES,
            "Valorile trebuie furnizate ca mapare de chei text la șiruri.",
        )
        raise GenerationValidationError((issue,))

    try:
        entries = list(values.items())
    except (AttributeError, TypeError, ValueError) as exc:
        issue = _make_issue(
            "values",
            "invalid_mapping",
            GENERATION_DOCUMENT_TYPES,
            "Valorile formularului nu pot fi citite ca mapare.",
        )
        raise GenerationValidationError((issue,)) from exc

    fields_by_key = {field.variable_key: field for field in form.fields}
    supplied: dict[str, Any] = {}
    issues: list[FieldValidationIssue] = []
    for key, value in entries:
        if not isinstance(key, str):
            display_key = repr(key) or "<cheie ne-text>"
            issues.append(
                _make_issue(
                    display_key,
                    "invalid_key_type",
                    GENERATION_DOCUMENT_TYPES,
                    f"Cheia {display_key} trebuie să fie text.",
                )
            )
            continue
        if key not in fields_by_key:
            issues.append(
                _make_issue(
                    key or "<cheie goală>",
                    "unknown_key",
                    GENERATION_DOCUMENT_TYPES,
                    f"Cheia {key!r} nu aparține formularului activ.",
                )
            )
            continue
        supplied[key] = value

    normalized: dict[str, str] = {}
    for field in form.fields:
        key = field.variable_key
        if key not in supplied:
            if field.required_in:
                labels = ", ".join(field.required_in)
                issues.append(
                    _make_issue(
                        key,
                        "required_missing",
                        field.required_in,
                        f"Câmpul obligatoriu {field.label!r} lipsește pentru: {labels}.",
                    )
                )
            else:
                normalized[key] = ""
            continue

        value = supplied[key]
        if not isinstance(value, str):
            issues.append(
                _make_issue(
                    key,
                    "invalid_value_type",
                    field.used_in or GENERATION_DOCUMENT_TYPES,
                    f"Valoarea câmpului {field.label!r} trebuie să fie text.",
                )
            )
            continue
        if field.required_in and not value.strip():
            labels = ", ".join(field.required_in)
            issues.append(
                _make_issue(
                    key,
                    "required_empty",
                    field.required_in,
                    f"Câmpul obligatoriu {field.label!r} nu poate fi gol pentru: {labels}.",
                )
            )
            continue
        if not field.required_in and value == "":
            normalized[key] = ""
            continue
        error_message = _validate_value(field, value)
        if error_message is not None:
            issues.append(
                _make_issue(
                    key,
                    "invalid_value",
                    field.used_in or GENERATION_DOCUMENT_TYPES,
                    error_message,
                )
            )
            continue
        normalized[key] = value

    if issues:
        raise GenerationValidationError(tuple(issues))
    return normalized


def prepare_documents(
    connection: sqlite3.Connection,
    intervention_id: int,
    values: Mapping[str, str],
) -> PreparedGeneration:
    """Validează valorile și pregătește ambele documente fără scrieri SQLite."""

    _validate_connection(connection)
    _validate_intervention_id(intervention_id)
    if connection.in_transaction:
        raise GenerationConflictError(
            "Generarea trebuie să înceapă în afara unei tranzacții SQLite active."
        )

    (
        form,
        template_documents,
        template_ids,
        diagnosis_label,
        intervention_label,
        expected_keys_by_doc,
    ) = _load_generation_context(connection, intervention_id)
    validated_values = _validate_values(form, values)

    documents_to_save: dict[str, tuple[int, str]] = {}
    for document_type in GENERATION_DOCUMENT_TYPES:
        expected_keys = set(expected_keys_by_doc[document_type])
        document_values = {
            key: validated_values[key]
            for key in expected_keys
        }
        try:
            generated = substitute_variables(
                template_documents[document_type],
                document_values,
                expected_keys,
            )
            generated = RichDocument.from_json(generated.to_json())
        except (TokenSubstitutionError, TypeError, ValueError) as exc:
            raise GenerationTemplateError(
                f"Șablonul {document_type!r} nu a putut fi completat fără pierderi: {exc}"
            ) from exc
        documents_to_save[document_type] = (
            template_ids[document_type],
            generated.to_json(),
        )

    return PreparedGeneration(
        diagnosis_id=form.diagnosis_id,
        intervention_id=form.intervention_id,
        template_set_id=form.template_set_id,
        diagnosis_label_snapshot=diagnosis_label,
        intervention_label_snapshot=intervention_label,
        epicrisis_recommendations_template_id=documents_to_save[EPICRISIS_RECOMMENDATIONS][0],
        epicrisis_recommendations_json=documents_to_save[EPICRISIS_RECOMMENDATIONS][1],
        operative_protocol_template_id=documents_to_save[OPERATIVE_PROTOCOL][0],
        operative_protocol_json=documents_to_save[OPERATIVE_PROTOCOL][1],
        epicrisis_template_version_id=form.epicrisis_template_version_id,
        operative_template_version_id=form.operative_template_version_id,
    )


def persist_prepared_documents(
    connection: sqlite3.Connection,
    prepared: PreparedGeneration,
) -> GeneratedSession:
    """Persistă atomic documentele pregătite fără a reîncărca șabloanele."""

    _validate_connection(connection)
    if not isinstance(prepared, PreparedGeneration):
        raise TypeError("prepared trebuie să fie PreparedGeneration.")
    if connection.in_transaction:
        raise GenerationConflictError(
            "Persistarea trebuie să înceapă în afara unei tranzacții SQLite active."
        )

    try:
        generated_documents = {
            EPICRISIS_RECOMMENDATIONS: RichDocument.from_json(
                prepared.epicrisis_recommendations_json
            ),
            OPERATIVE_PROTOCOL: RichDocument.from_json(prepared.operative_protocol_json),
        }
    except (TypeError, ValueError) as exc:
        raise GenerationTemplateError(
            f"Documentele pregătite nu mai conțin JSON Rich Text valid: {exc}"
        ) from exc

    documents_to_save = {
        EPICRISIS_RECOMMENDATIONS: (
            prepared.epicrisis_recommendations_template_id,
            prepared.epicrisis_recommendations_json,
        ),
        OPERATIVE_PROTOCOL: (
            prepared.operative_protocol_template_id,
            prepared.operative_protocol_json,
        ),
    }

    try:
        # The insert trigger rechecks active context inside the write transaction;
        # composite foreign keys validate each template ID, set, and document type.
        session_id = save_generation_session(
            connection,
            diagnosis_id=prepared.diagnosis_id,
            intervention_id=prepared.intervention_id,
            template_set_id=prepared.template_set_id,
            diagnosis_label_snapshot=prepared.diagnosis_label_snapshot,
            intervention_label_snapshot=prepared.intervention_label_snapshot,
            documents=documents_to_save,
        )
    except DatabaseConfigurationError as exc:
        raise GenerationConfigurationError(
            "Conexiunea SQLite nu este configurată corect; activați "
            "PRAGMA foreign_keys = ON înainte de generare."
        ) from exc
    except sqlite3.IntegrityError as exc:
        error_text = str(exc)
        if (
            "session requires an active diagnosis" in error_text
            or "documents can be added only while building a matching active session"
            in error_text
        ):
            raise GenerationConflictError(
                "Diagnosticul, intervenția sau șablonul activ s-a schimbat înainte de salvare."
            ) from exc
        raise GenerationPersistenceError(
            f"SQLite a respins salvarea atomică a sesiunii: {exc}"
        ) from exc
    except DatabaseIntegrityError as exc:
        raise GenerationPersistenceError(
            f"Sesiunea nu a putut fi salvată complet: {exc}"
        ) from exc

    return GeneratedSession(
        session_id=session_id,
        epicrisis_recommendations=generated_documents[EPICRISIS_RECOMMENDATIONS],
        operative_protocol=generated_documents[OPERATIVE_PROTOCOL],
    )


def generate_documents(
    connection: sqlite3.Connection,
    intervention_id: int,
    values: Mapping[str, str],
) -> GeneratedSession:
    """Pregătește documentele și le salvează atomic (API compatibil)."""

    prepared = prepare_documents(connection, intervention_id, values)
    return persist_prepared_documents(connection, prepared)
