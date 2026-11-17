"""Modele de date pentru formulare, sesiuni și istoricul generării."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ortodocs.models.rich_document import RichDocument


GenerationDocumentType = Literal[
    "epicrisis_recommendations",
    "operative_protocol",
]
GenerationFieldType = Literal["text", "number", "date", "select"]
GenerationFieldScope = Literal["common", "diagnosis", "intervention"]
GenerationSessionStatus = Literal["complete"]

_DOCUMENT_TYPE_ORDER: tuple[GenerationDocumentType, ...] = (
    "epicrisis_recommendations",
    "operative_protocol",
)
_DOCUMENT_TYPES = frozenset(_DOCUMENT_TYPE_ORDER)
_FIELD_TYPES = frozenset({"text", "number", "date", "select"})
_FIELD_SCOPES = frozenset({"common", "diagnosis", "intervention"})


def _validate_positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} trebuie să fie un întreg pozitiv.")


def _validate_nonempty_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} trebuie să fie text nevid.")


def _validate_document_types(
    value: tuple[GenerationDocumentType, ...],
    label: str,
) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} trebuie să fie un tuplu.")
    if any(document_type not in _DOCUMENT_TYPES for document_type in value):
        raise ValueError(f"{label} conține un tip de document nesuportat.")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} nu poate conține tipuri de document duplicate.")


@dataclass(frozen=True, slots=True)
class GenerationFieldOption:
    """O opțiune afișabilă pentru un câmp select."""

    option_value: str
    option_label: str
    sort_order: int = 0

    def __post_init__(self) -> None:
        _validate_nonempty_text(self.option_value, "option_value")
        _validate_nonempty_text(self.option_label, "option_label")
        if isinstance(self.sort_order, bool) or not isinstance(self.sort_order, int):
            raise ValueError("sort_order trebuie să fie un întreg.")


@dataclass(frozen=True, slots=True)
class GenerationField:
    """Definiția exactă a unui câmp folosit de șabloane."""

    variable_key: str
    field_definition_id: int | None
    label: str
    data_type: GenerationFieldType
    scope: GenerationFieldScope
    version: int
    options: tuple[GenerationFieldOption, ...] = ()
    required_in: tuple[GenerationDocumentType, ...] = ()
    used_in: tuple[GenerationDocumentType, ...] = ()

    def __post_init__(self) -> None:
        _validate_nonempty_text(self.variable_key, "variable_key")
        if self.field_definition_id is not None:
            _validate_positive_id(self.field_definition_id, "field_definition_id")
        _validate_nonempty_text(self.label, "label")
        if self.data_type not in _FIELD_TYPES:
            raise ValueError(f"Tip de câmp nesuportat: {self.data_type!r}.")
        if self.scope not in _FIELD_SCOPES:
            raise ValueError(f"Domeniu de câmp nesuportat: {self.scope!r}.")
        _validate_positive_id(self.version, "version")

        if not isinstance(self.options, tuple):
            raise TypeError("options trebuie să fie un tuplu de GenerationFieldOption.")
        if any(not isinstance(option, GenerationFieldOption) for option in self.options):
            raise TypeError("options poate conține doar GenerationFieldOption.")
        option_values = tuple(option.option_value for option in self.options)
        if len(option_values) != len(set(option_values)):
            raise ValueError("Un câmp nu poate avea valori de opțiune duplicate.")
        if self.data_type == "select" and not self.options:
            raise ValueError("Un câmp select trebuie să aibă cel puțin o opțiune.")
        if self.data_type != "select" and self.options:
            raise ValueError("Numai câmpurile select pot avea opțiuni.")

        _validate_document_types(self.required_in, "required_in")
        if not self.used_in and self.required_in:
            # Compatibilitate cu instanțele construite înainte ca utilizarea
            # opțională într-un document să fie reprezentată explicit.
            object.__setattr__(self, "used_in", self.required_in)
        _validate_document_types(self.used_in, "used_in")
        if not set(self.required_in).issubset(self.used_in):
            raise ValueError("required_in trebuie să fie inclus în used_in.")


@dataclass(frozen=True, slots=True)
class GenerationForm:
    """Contextul activ și câmpurile distincte necesare pentru generare."""

    diagnosis_id: int
    intervention_id: int
    template_set_id: int | None = None
    fields: tuple[GenerationField, ...] = ()
    epicrisis_template_version_id: int | None = None
    operative_template_version_id: int | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.diagnosis_id, "diagnosis_id")
        _validate_positive_id(self.intervention_id, "intervention_id")
        if self.template_set_id is not None:
            _validate_positive_id(self.template_set_id, "template_set_id")
        if self.epicrisis_template_version_id is not None:
            _validate_positive_id(
                self.epicrisis_template_version_id,
                "epicrisis_template_version_id",
            )
        if self.operative_template_version_id is not None:
            _validate_positive_id(
                self.operative_template_version_id,
                "operative_template_version_id",
            )
        if not isinstance(self.fields, tuple):
            raise TypeError("fields trebuie să fie un tuplu de GenerationField.")
        if any(not isinstance(item, GenerationField) for item in self.fields):
            raise TypeError("fields poate conține doar GenerationField.")
        variable_keys = tuple(item.variable_key for item in self.fields)
        if len(variable_keys) != len(set(variable_keys)):
            raise ValueError("GenerationForm nu poate conține chei de variabilă duplicate.")


@dataclass(frozen=True, slots=True)
class FieldValidationIssue:
    """Problemă de validare asociată unui câmp și documentelor care îl folosesc."""

    variable_key: str
    reason: str
    document_types: tuple[GenerationDocumentType, ...]
    message: str

    def __post_init__(self) -> None:
        _validate_nonempty_text(self.variable_key, "variable_key")
        _validate_nonempty_text(self.reason, "reason")
        _validate_document_types(self.document_types, "document_types")
        if not self.document_types:
            raise ValueError("document_types trebuie să indice cel puțin un document.")
        _validate_nonempty_text(self.message, "message")


@dataclass(frozen=True, slots=True)
class GeneratedSession:
    """Sesiunea salvată și cele două documente Rich Text rezultate."""

    session_id: int
    epicrisis_recommendations: RichDocument
    operative_protocol: RichDocument

    def __post_init__(self) -> None:
        _validate_positive_id(self.session_id, "session_id")
        if not isinstance(self.epicrisis_recommendations, RichDocument):
            raise TypeError("epicrisis_recommendations trebuie să fie RichDocument.")
        if not isinstance(self.operative_protocol, RichDocument):
            raise TypeError("operative_protocol trebuie să fie RichDocument.")
        if self.epicrisis_recommendations is self.operative_protocol:
            raise ValueError("Cele două documente trebuie să fie instanțe RichDocument distincte.")


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    """Rezultatele validate, pregătite pentru persistare atomică."""

    diagnosis_id: int
    intervention_id: int
    template_set_id: int | None
    diagnosis_label_snapshot: str
    intervention_label_snapshot: str
    epicrisis_recommendations_template_id: int
    epicrisis_recommendations_json: str
    operative_protocol_template_id: int
    operative_protocol_json: str
    epicrisis_template_version_id: int | None = None
    operative_template_version_id: int | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.diagnosis_id, "diagnosis_id")
        _validate_positive_id(self.intervention_id, "intervention_id")
        if self.template_set_id is not None:
            _validate_positive_id(self.template_set_id, "template_set_id")
        _validate_nonempty_text(self.diagnosis_label_snapshot, "diagnosis_label_snapshot")
        _validate_nonempty_text(self.intervention_label_snapshot, "intervention_label_snapshot")
        _validate_positive_id(
            self.epicrisis_recommendations_template_id,
            "epicrisis_recommendations_template_id",
        )
        _validate_positive_id(
            self.operative_protocol_template_id, "operative_protocol_template_id"
        )
        if self.template_set_id is None:
            if self.epicrisis_template_version_id is None:
                object.__setattr__(
                    self,
                    "epicrisis_template_version_id",
                    self.epicrisis_recommendations_template_id,
                )
            else:
                _validate_positive_id(
                    self.epicrisis_template_version_id,
                    "epicrisis_template_version_id",
                )
            if self.operative_template_version_id is None:
                object.__setattr__(
                    self,
                    "operative_template_version_id",
                    self.operative_protocol_template_id,
                )
            else:
                _validate_positive_id(
                    self.operative_template_version_id,
                    "operative_template_version_id",
                )
        else:
            if (
                self.epicrisis_template_version_id is not None
                or self.operative_template_version_id is not None
            ):
                raise ValueError(
                    "O pregătire legacy nu poate specifica independent_template_version_id."
                )
        for label, value in (
            ("epicrisis_recommendations_json", self.epicrisis_recommendations_json),
            ("operative_protocol_json", self.operative_protocol_json),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{label} trebuie să fie un șir JSON Rich Text.")
            try:
                RichDocument.from_json(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} trebuie să conțină un RichDocument valid.") from exc


@dataclass(frozen=True, slots=True)
class GenerationHistoryItem:
    """Datele sesiunii disponibile pentru rândul din istoricul generărilor."""

    session_id: int
    diagnosis_id: int
    intervention_id: int
    template_set_id: int | None
    diagnosis_label_snapshot: str
    intervention_label_snapshot: str
    created_at: str
    status: GenerationSessionStatus = "complete"

    def __post_init__(self) -> None:
        _validate_positive_id(self.session_id, "session_id")
        _validate_positive_id(self.diagnosis_id, "diagnosis_id")
        _validate_positive_id(self.intervention_id, "intervention_id")
        if self.template_set_id is not None:
            _validate_positive_id(self.template_set_id, "template_set_id")
        _validate_nonempty_text(self.diagnosis_label_snapshot, "diagnosis_label_snapshot")
        _validate_nonempty_text(self.intervention_label_snapshot, "intervention_label_snapshot")
        _validate_nonempty_text(self.created_at, "created_at")
        if self.status != "complete":
            raise ValueError("În istoricul generărilor sunt permise numai sesiuni complete.")


@dataclass(frozen=True, slots=True)
class GenerationSessionDetail:
    """O sesiune din istoric împreună cu documentele sale editabile."""

    history_item: GenerationHistoryItem
    epicrisis_recommendations: RichDocument
    operative_protocol: RichDocument
    epicrisis_template_id: int
    epicrisis_edited_at: str
    operative_template_id: int
    operative_edited_at: str
    epicrisis_template_version_id: int | None = None
    operative_template_version_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.history_item, GenerationHistoryItem):
            raise TypeError("history_item trebuie să fie GenerationHistoryItem.")
        if not isinstance(self.epicrisis_recommendations, RichDocument):
            raise TypeError("epicrisis_recommendations trebuie să fie RichDocument.")
        if not isinstance(self.operative_protocol, RichDocument):
            raise TypeError("operative_protocol trebuie să fie RichDocument.")
        if self.epicrisis_recommendations is self.operative_protocol:
            raise ValueError("Cele două documente trebuie să fie instanțe RichDocument distincte.")
        _validate_positive_id(self.epicrisis_template_id, "epicrisis_template_id")
        _validate_nonempty_text(self.epicrisis_edited_at, "epicrisis_edited_at")
        _validate_positive_id(self.operative_template_id, "operative_template_id")
        _validate_nonempty_text(self.operative_edited_at, "operative_edited_at")
        if self.epicrisis_template_version_id is not None:
            _validate_positive_id(
                self.epicrisis_template_version_id,
                "epicrisis_template_version_id",
            )
        elif self.history_item.template_set_id is None:
            object.__setattr__(
                self,
                "epicrisis_template_version_id",
                self.epicrisis_template_id,
            )
        if self.operative_template_version_id is not None:
            _validate_positive_id(
                self.operative_template_version_id,
                "operative_template_version_id",
            )
        elif self.history_item.template_set_id is None:
            object.__setattr__(
                self,
                "operative_template_version_id",
                self.operative_template_id,
            )
