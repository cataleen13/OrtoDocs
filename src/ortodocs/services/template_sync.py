"""Serviciul intern OrtoDocs pentru sincronizarea automată a template-urilor DOCX."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from ortodocs.models.import_report import ImportIssue, ImportReport
from ortodocs.models.rich_document import RichDocument
from ortodocs.services import medical_library, template_sets
from ortodocs.services.docx_import import parse_docx_bytes
from ortodocs.services.medical_library import (
    MedicalLibraryError,
    is_reserved_patient_name_variable_key,
    normalize_name,
    normalize_name_key,
)
from ortodocs.services.template_sets import (
    EPICRISIS_RECOMMENDATIONS,
    OPERATIVE_PROTOCOL,
    TemplateSetError,
)
from ortodocs.storage import (
    independent_template_repositories,
    repositories,
    template_sync_repositories,
)
from ortodocs.storage.database import DatabaseError, write_transaction
from ortodocs.storage.migrations import (
    MigrationBackupError,
    _main_database_path,
    create_database_backup,
)


CATALOG_FILENAME = "catalog.json"
SUPPORTED_CATALOG_VERSION = 1
_CATALOG_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z", re.ASCII)

PATIENT_CONFIRMATION_TEXT = (
    "Confirm că am verificat documentele DOCX noi sau modificate și că nu "
    "conțin date de identificare ale pacienților."
)

ChangeKind = Literal["new", "modified", "unchanged"]
IssueSeverity = Literal["error", "warning"]
SyncStatus = Literal[
    "not_configured",
    "folder_unavailable",
    "no_changes",
    "validation_failed",
    "cancelled",
    "applied",
    "backup_failed",
    "failed",
]


class TemplateSyncError(RuntimeError):
    """Eroare în procesul de scanare, validare sau sincronizare a șabloanelor."""


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _normalize_optional_code(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} trebuie să fie un șir de caractere sau null.")
    stripped = value.strip()
    return stripped if stripped else None


def _validate_catalog_key(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _CATALOG_KEY_PATTERN.fullmatch(value):
        raise ValueError(
            f"{label} trebuie să fie o cheie stabilă ASCII (litere mici, cifre, '_' sau '-')."
        )
    return value


def _validate_relative_docx_path(root_dir: Path, raw_path: object) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Calea DOCX din catalog nu poate fi goală.")
    if "\x00" in raw_path:
        raise ValueError("Calea DOCX conține caractere invalide.")

    stripped = raw_path.strip()
    win_path = PureWindowsPath(stripped)
    posix_path = PurePosixPath(stripped)
    if (
        win_path.is_absolute()
        or posix_path.is_absolute()
        or stripped.startswith(("/", "\\"))
        or win_path.drive
    ):
        raise ValueError(
            f"Calea {raw_path!r} trebuie să fie relativă față de folderul rădăcină."
        )

    segments = [part for part in stripped.replace("\\", "/").split("/")]
    if any(part in {"", ".", ".."} for part in segments):
        raise ValueError(
            f"Calea {raw_path!r} conține segmente interzise (traversare sau cale goală)."
        )

    normalized_rel = "/".join(segments)
    current = root_dir
    for segment in segments:
        current = current / segment
        if current.is_symlink():
            raise ValueError(
                f"Accesul prin symlink este interzis pentru calea {normalized_rel!r}."
            )

    if current.suffix.lower() != ".docx":
        raise ValueError(
            f"Fișierul {normalized_rel!r} trebuie să aibă extensia .docx."
        )

    resolved_root = root_dir.resolve(strict=True)
    resolved_target = current.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(
            f"Calea {normalized_rel!r} iese în afara folderului de șabloane."
        )

    if not current.exists():
        raise ValueError(
            f"Fișierul referit în catalog lipsește de pe disc: {normalized_rel}."
        )
    if not current.is_file():
        raise ValueError(
            f"Calea referită nu indică un fișier DOCX valid: {normalized_rel}."
        )
    return normalized_rel, current


@dataclass(frozen=True, slots=True)
class DiagnosisCatalogEntry:
    key: str
    name: str
    name_key: str
    code: str | None
    epicrisis_relpath: str | None
    epicrisis_path: Path | None


@dataclass(frozen=True, slots=True)
class InterventionCatalogEntry:
    key: str
    diagnosis_key: str
    name: str
    name_key: str
    code: str | None
    protocol_relpath: str | None
    protocol_path: Path | None


@dataclass(frozen=True, slots=True)
class TemplateCatalog:
    version: int
    root_dir: Path
    diagnoses: tuple[DiagnosisCatalogEntry, ...]
    interventions: tuple[InterventionCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class SyncIssue:
    severity: IssueSeverity
    source: str
    message: str


@dataclass(frozen=True, slots=True)
class ScannedTemplateItem:
    catalog_key: str
    diagnosis_key: str | None
    owner_type: str
    owner_name: str
    owner_code: str | None
    document_type: str
    relative_path: str
    change_kind: ChangeKind
    sha256: str
    document_bytes: bytes = field(repr=False)
    parsed: tuple[RichDocument, ImportReport, str] | None = field(repr=False)
    existing_owner_id: int | None
    existing_active_version_id: int | None
    needs_sync_link: bool = False


@dataclass(frozen=True, slots=True)
class SyncScanReport:
    folder_path: Path
    folder_available: bool
    catalog: TemplateCatalog | None
    items: tuple[ScannedTemplateItem, ...]
    issues: tuple[SyncIssue, ...]
    scanned_count: int
    new_count: int
    modified_count: int
    unchanged_count: int
    linked_count: int = 0
    resolved_diagnosis_ids: dict[str, int | None] = field(default_factory=dict)
    resolved_intervention_ids: dict[str, int | None] = field(default_factory=dict)
    error_message: str | None = None

    @property
    def has_changes(self) -> bool:
        return (self.new_count + self.modified_count) > 0

    @property
    def has_pending_links(self) -> bool:
        return self.linked_count > 0

    @property
    def has_blocking_issues(self) -> bool:
        return (not self.folder_available) or bool(self.issues)

    @property
    def can_apply(self) -> bool:
        return (
            self.folder_available
            and not self.issues
            and (self.has_changes or self.has_pending_links)
        )

    def pending_items(self) -> tuple[ScannedTemplateItem, ...]:
        return tuple(
            item for item in self.items if item.change_kind in {"new", "modified"}
        )

    def link_items(self) -> tuple[ScannedTemplateItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.change_kind == "unchanged" and item.needs_sync_link
        )

    def format_error_report(self) -> str:
        if not self.folder_available:
            detail = self.error_message or "Folderul configurat nu este accesibil."
            return (
                "Sincronizarea nu a fost aplicată.\n\n"
                f"Avertisment: {detail}\n\n"
                "Baza de date nu a fost modificată.\n"
                "Template-urile anterioare rămân active."
            )

        lines = ["Sincronizarea nu a fost aplicată.", "", "Documente cu probleme:"]
        grouped: dict[str, list[SyncIssue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.source, []).append(issue)

        for source, source_issues in grouped.items():
            lines.append(f"- {source}")
            for issue in source_issues:
                prefix = "Avertisment" if issue.severity == "warning" else "Eroare"
                lines.append(f"  {prefix}: {issue.message}")
            lines.append("")

        lines.append("Baza de date nu a fost modificată.")
        lines.append("Template-urile anterioare rămân active.")
        return "\n".join(lines)

    def format_confirmation_summary(self) -> str:
        lines = [
            "Au fost detectate șabloane DOCX noi sau modificate și toate au trecut validarea fără avertismente.",
            "",
            f"Folder: {self.folder_path}",
            f"Template-uri scanate: {self.scanned_count}",
            f"Noi: {self.new_count}",
            f"Modificate: {self.modified_count}",
            f"Neschimbate: {self.unchanged_count}",
            "",
            "Documente care vor fi actualizate:",
        ]
        for item in self.pending_items():
            kind_label = "Nou" if item.change_kind == "new" else "Modificat"
            lines.append(f"- [{kind_label}] {item.relative_path} ({item.owner_name})")
        lines.extend(["", PATIENT_CONFIRMATION_TEXT])
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    status: SyncStatus
    folder_path: str | None
    scanned_count: int = 0
    new_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    imported_count: int = 0
    activated_count: int = 0
    linked_count: int = 0
    backup_path: str | None = None
    summary_message: str = ""
    issues: tuple[SyncIssue, ...] = ()
    created_at: str = field(default_factory=_now_utc)


def _format_success_summary(
    *,
    scanned_count: int,
    new_count: int,
    modified_count: int,
    unchanged_count: int,
    imported_count: int,
    activated_count: int,
    linked_count: int = 0,
    backup_path: Path,
) -> str:
    lines = [
        "Sincronizare template-uri finalizată",
        "",
        f"Template-uri scanate: {scanned_count}",
        f"Noi: {new_count}",
        f"Modificate: {modified_count}",
        f"Neschimbate: {unchanged_count}",
        "",
        f"Versiuni noi înregistrate: {imported_count}",
        f"Versiuni activate: {activated_count}",
    ]
    if linked_count > 0:
        lines.append(f"Versiuni existente asociate sursei DOCX: {linked_count}")
    lines.extend(["", "Backup:", str(backup_path)])
    return "\n".join(lines)


def format_sync_status_label(status: str) -> str:
    labels = {
        "not_configured": "Neconfigurată",
        "folder_unavailable": "Folder inaccesibil",
        "no_changes": "Nicio modificare",
        "validation_failed": "Oprită (erori/avertismente)",
        "cancelled": "Anulată de utilizator",
        "applied": "Reușită",
        "backup_failed": "Eșuată (backup)",
        "failed": "Eșuată (tranzacție anulată)",
    }
    return labels.get(status, status)


def load_template_catalog(root_dir: Path) -> tuple[TemplateCatalog | None, list[SyncIssue]]:
    """Încarcă și validează complet manifestul catalog.json și căile referite."""

    issues: list[SyncIssue] = []
    catalog_path = root_dir / CATALOG_FILENAME
    if catalog_path.is_symlink():
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                "Manifestul catalog.json nu poate fi un symlink.",
            )
        )
        return None, issues
    if not catalog_path.exists() or not catalog_path.is_file():
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                f"Lipsește fișierul {CATALOG_FILENAME} din folderul configurat.",
            )
        )
        return None, issues

    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                f"Fișierul {CATALOG_FILENAME} nu este un JSON UTF-8 valid: {exc}",
            )
        )
        return None, issues

    if not isinstance(payload, dict):
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                "Conținutul rădăcină din catalog.json trebuie să fie un obiect JSON.",
            )
        )
        return None, issues

    allowed_top_keys = {"catalog_version", "diagnoses", "interventions"}
    unknown_top = sorted(set(payload) - allowed_top_keys)
    if unknown_top:
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                f"Câmpuri necunoscute în catalog.json: {', '.join(unknown_top)}.",
            )
        )

    catalog_version = payload.get("catalog_version")
    if isinstance(catalog_version, bool) or catalog_version != SUPPORTED_CATALOG_VERSION:
        issues.append(
            SyncIssue(
                "error",
                CATALOG_FILENAME,
                f"catalog_version trebuie să fie {SUPPORTED_CATALOG_VERSION}.",
            )
        )

    raw_diagnoses = payload.get("diagnoses")
    raw_interventions = payload.get("interventions")
    if not isinstance(raw_diagnoses, list):
        issues.append(
            SyncIssue("error", CATALOG_FILENAME, "Secțiunea 'diagnoses' trebuie să fie o listă.")
        )
        raw_diagnoses = []
    if not isinstance(raw_interventions, list):
        issues.append(
            SyncIssue("error", CATALOG_FILENAME, "Secțiunea 'interventions' trebuie să fie o listă.")
        )
        raw_interventions = []

    diagnoses: list[DiagnosisCatalogEntry] = []
    diagnosis_keys: set[str] = set()
    diagnosis_name_keys: set[str] = set()
    diagnosis_codes: set[str] = set()
    used_paths: set[str] = set()

    for index, raw_entry in enumerate(raw_diagnoses):
        source_label = f"diagnoses[{index}]"
        if not isinstance(raw_entry, dict):
            issues.append(
                SyncIssue("error", source_label, "Intrarea de diagnostic trebuie să fie un obiect JSON.")
            )
            continue

        unknown_keys = sorted(set(raw_entry) - {"key", "name", "code", "epicrisis_path"})
        if unknown_keys:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Câmpuri necunoscute pentru diagnostic: {', '.join(unknown_keys)}.",
                )
            )
            continue

        try:
            key = _validate_catalog_key(raw_entry.get("key"), label="Cheia diagnosticului")
            source_label = f"diagnoses[{key}]"
            raw_name = raw_entry.get("name")
            if not isinstance(raw_name, str):
                raise ValueError("Numele diagnosticului trebuie să fie text nevid.")
            display_name = normalize_name(raw_name, field_label="denumirea diagnosticului")
            name_key = normalize_name_key(display_name)
            code = _normalize_optional_code(raw_entry.get("code"), label="Codul diagnosticului")
        except (ValueError, MedicalLibraryError) as exc:
            issues.append(SyncIssue("error", source_label, str(exc)))
            continue

        if key in diagnosis_keys:
            issues.append(
                SyncIssue("error", source_label, f"Cheie duplicată în diagnostice: {key!r}.")
            )
        if name_key in diagnosis_name_keys:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Denumire duplicată în diagnostice: {display_name!r}.",
                )
            )
        if code is not None and code in diagnosis_codes:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Cod duplicat în diagnostice: {code!r}.",
                )
            )

        raw_epicrisis = raw_entry.get("epicrisis_path")
        rel_path: str | None = None
        full_path: Path | None = None
        if raw_epicrisis is not None:
            try:
                rel_path, full_path = _validate_relative_docx_path(root_dir, raw_epicrisis)
            except ValueError as exc:
                issue_source = (
                    str(raw_epicrisis).strip()
                    if isinstance(raw_epicrisis, str) and raw_epicrisis.strip()
                    else source_label
                )
                issues.append(SyncIssue("error", issue_source, str(exc)))
            else:
                if rel_path in used_paths:
                    issues.append(
                        SyncIssue(
                            "error",
                            rel_path,
                            f"Fișierul {rel_path!r} este asociat de mai multe ori în catalog.",
                        )
                    )
                used_paths.add(rel_path)

        diagnosis_keys.add(key)
        diagnosis_name_keys.add(name_key)
        if code is not None:
            diagnosis_codes.add(code)
        diagnoses.append(
            DiagnosisCatalogEntry(
                key=key,
                name=display_name,
                name_key=name_key,
                code=code,
                epicrisis_relpath=rel_path,
                epicrisis_path=full_path,
            )
        )

    interventions: list[InterventionCatalogEntry] = []
    intervention_keys: set[str] = set()
    intervention_names_by_diag: set[tuple[str, str]] = set()
    intervention_codes_by_diag: set[tuple[str, str]] = set()

    for index, raw_entry in enumerate(raw_interventions):
        source_label = f"interventions[{index}]"
        if not isinstance(raw_entry, dict):
            issues.append(
                SyncIssue("error", source_label, "Intrarea de intervenție trebuie să fie un obiect JSON.")
            )
            continue

        unknown_keys = sorted(
            set(raw_entry) - {"key", "diagnosis_key", "name", "code", "protocol_path"}
        )
        if unknown_keys:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Câmpuri necunoscute pentru intervenție: {', '.join(unknown_keys)}.",
                )
            )
            continue

        try:
            key = _validate_catalog_key(raw_entry.get("key"), label="Cheia intervenției")
            source_label = f"interventions[{key}]"
            diagnosis_key = _validate_catalog_key(
                raw_entry.get("diagnosis_key"), label="diagnosis_key"
            )
            raw_name = raw_entry.get("name")
            if not isinstance(raw_name, str):
                raise ValueError("Numele intervenției trebuie să fie text nevid.")
            display_name = normalize_name(raw_name, field_label="denumirea intervenției")
            name_key = normalize_name_key(display_name)
            code = _normalize_optional_code(raw_entry.get("code"), label="Codul intervenției")
        except (ValueError, MedicalLibraryError) as exc:
            issues.append(SyncIssue("error", source_label, str(exc)))
            continue

        if key in intervention_keys:
            issues.append(
                SyncIssue("error", source_label, f"Cheie duplicată în intervenții: {key!r}.")
            )
        if diagnosis_key not in diagnosis_keys:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Intervenția {key!r} referă un diagnostic inexistent în catalog: {diagnosis_key!r}.",
                )
            )
        if (diagnosis_key, name_key) in intervention_names_by_diag:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Denumire duplicată pentru diagnosticul {diagnosis_key!r}: {display_name!r}.",
                )
            )
        if code is not None and (diagnosis_key, code) in intervention_codes_by_diag:
            issues.append(
                SyncIssue(
                    "error",
                    source_label,
                    f"Cod duplicat pentru diagnosticul {diagnosis_key!r}: {code!r}.",
                )
            )

        raw_protocol = raw_entry.get("protocol_path")
        rel_path = None
        full_path = None
        if raw_protocol is not None:
            try:
                rel_path, full_path = _validate_relative_docx_path(root_dir, raw_protocol)
            except ValueError as exc:
                issue_source = (
                    str(raw_protocol).strip()
                    if isinstance(raw_protocol, str) and raw_protocol.strip()
                    else source_label
                )
                issues.append(SyncIssue("error", issue_source, str(exc)))
            else:
                if rel_path in used_paths:
                    issues.append(
                        SyncIssue(
                            "error",
                            rel_path,
                            f"Fișierul {rel_path!r} este asociat de mai multe ori în catalog.",
                        )
                    )
                used_paths.add(rel_path)

        intervention_keys.add(key)
        intervention_names_by_diag.add((diagnosis_key, name_key))
        if code is not None:
            intervention_codes_by_diag.add((diagnosis_key, code))
        interventions.append(
            InterventionCatalogEntry(
                key=key,
                diagnosis_key=diagnosis_key,
                name=display_name,
                name_key=name_key,
                code=code,
                protocol_relpath=rel_path,
                protocol_path=full_path,
            )
        )

    return (
        TemplateCatalog(
            version=SUPPORTED_CATALOG_VERSION,
            root_dir=root_dir,
            diagnoses=tuple(diagnoses),
            interventions=tuple(interventions),
        ),
        issues,
    )


def _collect_import_report_issues(
    relative_path: str,
    report: ImportReport,
) -> list[SyncIssue]:
    collected: list[SyncIssue] = []
    error_groups: tuple[list[ImportIssue], ...] = (
        report.formatting_errors,
        report.unsupported_elements,
        report.variable_errors,
        report.content_errors,
        report.archive_errors,
        report.xml_errors,
        report.structure_errors,
        report.resource_errors,
        report.parsing_errors,
    )
    for group in error_groups:
        for issue in group:
            collected.append(SyncIssue("error", relative_path, issue.message))

    for warning in report.warnings:
        collected.append(SyncIssue("warning", relative_path, warning.message))

    if report.status == "rejected" and not any(error_groups):
        collected.append(
            SyncIssue(
                "error",
                relative_path,
                "Documentul Word a fost respins de parser și nu poate fi sincronizat.",
            )
        )
    if report.status == "valid_with_warnings" and not report.warnings:
        collected.append(
            SyncIssue(
                "warning",
                relative_path,
                "Documentul Word are avertismente nesoluționate și nu poate fi sincronizat automat.",
            )
        )

    for key in sorted(report.variables):
        if is_reserved_patient_name_variable_key(key):
            collected.append(
                SyncIssue(
                    "error",
                    relative_path,
                    f"Variabila rezervată {key!r} pentru numele pacientului nu este permisă.",
                )
            )
    return collected


class TemplateSyncService:
    """Coordonează scanarea, validarea strictă, backup-ul și sincronizarea atomică DOCX."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: str | Path | None = None,
        backup_directory: str | Path | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection trebuie să fie o conexiune sqlite3.Connection.")
        self.connection = connection
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else _main_database_path(connection)
        )
        self.backup_directory = (
            Path(backup_directory) if backup_directory is not None else None
        )

    def get_configured_folder(self) -> Path | None:
        raw = template_sync_repositories.get_app_setting(
            self.connection,
            template_sync_repositories.SYNC_FOLDER_SETTING_KEY,
        )
        if not raw or not raw.strip():
            return None
        return Path(raw.strip())

    def set_configured_folder(self, folder_path: str | Path | None) -> Path | None:
        if folder_path is None or not str(folder_path).strip():
            with write_transaction(self.connection):
                template_sync_repositories.delete_app_setting(
                    self.connection,
                    template_sync_repositories.SYNC_FOLDER_SETTING_KEY,
                )
            return None

        expanded = Path(str(folder_path).strip()).expanduser()
        if expanded.is_symlink():
            raise TemplateSyncError("Folderul de template-uri nu poate fi un symlink.")
        if not expanded.exists() or not expanded.is_dir():
            raise TemplateSyncError(
                f"Folderul selectat nu există sau nu este un director valid: {expanded}"
            )
        resolved = expanded.resolve()
        if resolved.is_symlink():
            raise TemplateSyncError("Folderul de template-uri nu poate fi un symlink.")
        with write_transaction(self.connection):
            template_sync_repositories.upsert_app_setting(
                self.connection,
                template_sync_repositories.SYNC_FOLDER_SETTING_KEY,
                str(resolved),
            )
        return resolved

    @staticmethod
    def check_folder_availability(folder_path: Path | None) -> tuple[bool, str]:
        if folder_path is None:
            return False, "Folder neconfigurat"
        try:
            if folder_path.is_symlink():
                return False, "Folder indisponibil (symlink nepermis)"
            if not folder_path.exists():
                return False, "Folder indisponibil (calea nu există)"
            if not folder_path.is_dir():
                return False, "Folder indisponibil (calea nu este un director)"
            catalog_path = folder_path / CATALOG_FILENAME
            if not catalog_path.exists():
                return True, f"Disponibil (lipsește {CATALOG_FILENAME})"
        except OSError as exc:
            return False, f"Folder inaccesibil ({exc})"
        return True, "Disponibil"

    def resolve_backup_directory(self) -> Path:
        if self.backup_directory is not None:
            return self.backup_directory
        db_path = self.database_path or _main_database_path(self.connection)
        if db_path is None:
            raise MigrationBackupError(
                "Nu există o cale pe disc pentru baza SQLite, deci nu se poate salva backup-ul."
            )
        return db_path.parent / "backups"

    def create_backup(self) -> Path:
        target_dir = self.resolve_backup_directory()
        db_path = self.database_path or _main_database_path(self.connection)
        return create_database_backup(
            self.connection,
            source_path=db_path,
            backup_directory=target_dir,
            file_prefix="ortodocs-sync-backup",
        )

    def _resolve_existing_entities(
        self,
        catalog: TemplateCatalog,
        issues: list[SyncIssue],
    ) -> tuple[dict[str, dict[str, Any] | None], dict[str, dict[str, Any] | None]]:
        all_diagnoses = repositories.list_diagnoses(self.connection, active_only=False)
        diag_by_id = {int(row["id"]): row for row in all_diagnoses}
        diag_by_name_key = {str(row["name_key"]): row for row in all_diagnoses}
        diag_by_code = {
            str(row["code"]): row for row in all_diagnoses if row.get("code")
        }

        resolved_diagnoses: dict[str, dict[str, Any] | None] = {}
        claimed_diag_ids: dict[int, str] = {}

        for entry in catalog.diagnoses:
            mapped = template_sync_repositories.get_sync_entity_by_key(
                self.connection,
                entity_type="diagnosis",
                catalog_key=entry.key,
            )
            matched: dict[str, Any] | None = None
            if mapped is not None:
                matched = diag_by_id.get(int(mapped["diagnosis_id"]))
                if matched is None:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"diagnoses[{entry.key}]",
                            f"Maparea de sincronizare indică un diagnostic inexistent (ID {mapped['diagnosis_id']}).",
                        )
                    )
                    continue
            else:
                matched = diag_by_name_key.get(entry.name_key)
                if matched is None and entry.code is not None:
                    matched = diag_by_code.get(entry.code)
                if matched is not None:
                    existing_map = template_sync_repositories.get_sync_entity_by_owner(
                        self.connection,
                        entity_type="diagnosis",
                        owner_id=int(matched["id"]),
                    )
                    if existing_map is not None and existing_map["catalog_key"] != entry.key:
                        issues.append(
                            SyncIssue(
                                "error",
                                f"diagnoses[{entry.key}]",
                                f"Diagnosticul {entry.name!r} este deja asociat cu altă cheie ({existing_map['catalog_key']!r}).",
                            )
                        )
                        continue

            if matched is not None:
                matched_id = int(matched["id"])
                if matched_id in claimed_diag_ids:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"diagnoses[{entry.key}]",
                            f"Diagnosticul ID {matched_id} este referit de mai multe chei din catalog.",
                        )
                    )
                    continue
                claimed_diag_ids[matched_id] = entry.key
                # Verificăm conflicte de redenumire sau cod față de alte rânduri din DB.
                conflict_name = diag_by_name_key.get(entry.name_key)
                if conflict_name is not None and int(conflict_name["id"]) != matched_id:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"diagnoses[{entry.key}]",
                            f"Denumirea {entry.name!r} aparține deja altui diagnostic din baza de date.",
                        )
                    )
                if entry.code is not None:
                    conflict_code = diag_by_code.get(entry.code)
                    if conflict_code is not None and int(conflict_code["id"]) != matched_id:
                        issues.append(
                            SyncIssue(
                                "error",
                                f"diagnoses[{entry.key}]",
                                f"Codul {entry.code!r} aparține deja altui diagnostic din baza de date.",
                            )
                        )
            resolved_diagnoses[entry.key] = matched

        all_interventions = repositories.list_interventions(
            self.connection,
            active_only=False,
        )
        interv_by_id = {int(row["id"]): row for row in all_interventions}
        resolved_interventions: dict[str, dict[str, Any] | None] = {}
        claimed_interv_ids: dict[int, str] = {}

        for entry in catalog.interventions:
            parent_diag = resolved_diagnoses.get(entry.diagnosis_key)
            parent_diag_id = int(parent_diag["id"]) if parent_diag is not None else None
            mapped = template_sync_repositories.get_sync_entity_by_key(
                self.connection,
                entity_type="intervention",
                catalog_key=entry.key,
            )
            matched_interv: dict[str, Any] | None = None
            if mapped is not None:
                matched_interv = interv_by_id.get(int(mapped["intervention_id"]))
                if matched_interv is None:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"interventions[{entry.key}]",
                            f"Maparea indică o intervenție inexistentă (ID {mapped['intervention_id']}).",
                        )
                    )
                    continue
                if parent_diag_id is None or int(matched_interv["diagnosis_id"]) != parent_diag_id:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"interventions[{entry.key}]",
                            "O intervenție existentă nu poate fi mutată sub alt diagnostic.",
                        )
                    )
                    continue
            elif parent_diag_id is not None:
                diag_intervs = [
                    row
                    for row in all_interventions
                    if int(row["diagnosis_id"]) == parent_diag_id
                ]
                by_name = {str(row["name_key"]): row for row in diag_intervs}
                by_code = {str(row["code"]): row for row in diag_intervs if row.get("code")}
                matched_interv = by_name.get(entry.name_key)
                if matched_interv is None and entry.code is not None:
                    matched_interv = by_code.get(entry.code)
                if matched_interv is not None:
                    existing_map = template_sync_repositories.get_sync_entity_by_owner(
                        self.connection,
                        entity_type="intervention",
                        owner_id=int(matched_interv["id"]),
                    )
                    if existing_map is not None and existing_map["catalog_key"] != entry.key:
                        issues.append(
                            SyncIssue(
                                "error",
                                f"interventions[{entry.key}]",
                                f"Intervenția {entry.name!r} este deja asociată cu altă cheie ({existing_map['catalog_key']!r}).",
                            )
                        )
                        continue

            if matched_interv is not None and parent_diag_id is not None:
                matched_interv_id = int(matched_interv["id"])
                if matched_interv_id in claimed_interv_ids:
                    issues.append(
                        SyncIssue(
                            "error",
                            f"interventions[{entry.key}]",
                            f"Intervenția ID {matched_interv_id} este referită de mai multe chei din catalog.",
                        )
                    )
                    continue
                claimed_interv_ids[matched_interv_id] = entry.key
                diag_intervs = [
                    row
                    for row in all_interventions
                    if int(row["diagnosis_id"]) == parent_diag_id
                ]
                for other in diag_intervs:
                    other_id = int(other["id"])
                    if other_id == matched_interv_id:
                        continue
                    if str(other["name_key"]) == entry.name_key:
                        issues.append(
                            SyncIssue(
                                "error",
                                f"interventions[{entry.key}]",
                                f"Denumirea {entry.name!r} aparține deja altei intervenții a diagnosticului.",
                            )
                        )
                    if entry.code is not None and other.get("code") == entry.code:
                        issues.append(
                            SyncIssue(
                                "error",
                                f"interventions[{entry.key}]",
                                f"Codul {entry.code!r} aparține deja altei intervenții a diagnosticului.",
                            )
                        )
            resolved_interventions[entry.key] = matched_interv

        return resolved_diagnoses, resolved_interventions

    def _inspect_document_entry(
        self,
        *,
        catalog_key: str,
        diagnosis_key: str | None,
        owner_type: str,
        owner_name: str,
        owner_code: str | None,
        document_type: str,
        relative_path: str,
        file_path: Path,
        existing_owner: Mapping[str, Any] | None,
        parent_owner: Mapping[str, Any] | None = None,
    ) -> tuple[ScannedTemplateItem | None, list[SyncIssue]]:
        issues: list[SyncIssue] = []
        try:
            document_bytes = file_path.read_bytes()
        except OSError as exc:
            issues.append(
                SyncIssue("error", relative_path, f"Fișierul nu a putut fi citit: {exc}")
            )
            return None, issues

        if not document_bytes:
            issues.append(
                SyncIssue("error", relative_path, "Fișierul DOCX este gol (0 octeți).")
            )
            return None, issues

        digest = hashlib.sha256(document_bytes).hexdigest()
        existing_owner_id = int(existing_owner["id"]) if existing_owner is not None else None
        active_version = (
            independent_template_repositories.get_active_independent_template_version(
                self.connection,
                owner_type=owner_type,
                owner_id=existing_owner_id,
                document_type=document_type,
            )
            if existing_owner_id is not None
            else None
        )
        active_version_id = int(active_version["id"]) if active_version is not None else None

        if active_version is not None and str(active_version["source_sha256"]) == digest:
            assert active_version_id is not None
            mapped_entity = template_sync_repositories.get_sync_entity_by_key(
                self.connection,
                entity_type=owner_type,
                catalog_key=catalog_key,
            )
            existing_source = template_sync_repositories.get_sync_source_for_version(
                self.connection,
                active_version_id,
            )
            needs_sync_link = (
                mapped_entity is None
                or existing_source is None
                or str(existing_source["catalog_key"]) != catalog_key
                or str(existing_source["relative_path"]) != relative_path
            )
            return (
                ScannedTemplateItem(
                    catalog_key=catalog_key,
                    diagnosis_key=diagnosis_key,
                    owner_type=owner_type,
                    owner_name=owner_name,
                    owner_code=owner_code,
                    document_type=document_type,
                    relative_path=relative_path,
                    change_kind="unchanged",
                    sha256=digest,
                    document_bytes=document_bytes,
                    parsed=None,
                    existing_owner_id=existing_owner_id,
                    existing_active_version_id=active_version_id,
                    needs_sync_link=needs_sync_link,
                ),
                issues,
            )

        change_kind: ChangeKind = "modified" if active_version is not None else "new"

        if existing_owner is not None and not bool(existing_owner.get("is_active", 1)):
            issues.append(
                SyncIssue(
                    "error",
                    relative_path,
                    f"Proprietarul {owner_name!r} este inactiv în baza de date; reactivați-l înainte de sincronizare.",
                )
            )
        if parent_owner is not None and not bool(parent_owner.get("is_active", 1)):
            issues.append(
                SyncIssue(
                    "error",
                    relative_path,
                    "Diagnosticul părinte este inactiv în baza de date; reactivați-l înainte de sincronizare.",
                )
            )

        parsed: tuple[RichDocument, ImportReport, str] | None = None
        try:
            parsed = parse_docx_bytes(document_bytes)
        except Exception as exc:
            issues.append(
                SyncIssue("error", relative_path, f"Documentul DOCX nu a putut fi parsat: {exc}")
            )
        else:
            parsed_doc, import_report, parsed_digest = parsed
            if parsed_digest != digest:
                issues.append(
                    SyncIssue(
                        "error",
                        relative_path,
                        "Hash-ul calculat de parser diferă de SHA-256-ul fișierului.",
                    )
                )
            issues.extend(_collect_import_report_issues(relative_path, import_report))
            try:
                RichDocument.from_json(parsed_doc.to_json())
            except Exception as exc:
                issues.append(
                    SyncIssue(
                        "error",
                        relative_path,
                        f"Structura RichDocument rezultată nu este validă: {exc}",
                    )
                )

        return (
            ScannedTemplateItem(
                catalog_key=catalog_key,
                diagnosis_key=diagnosis_key,
                owner_type=owner_type,
                owner_name=owner_name,
                owner_code=owner_code,
                document_type=document_type,
                relative_path=relative_path,
                change_kind=change_kind,
                sha256=digest,
                document_bytes=document_bytes,
                parsed=parsed,
                existing_owner_id=existing_owner_id,
                existing_active_version_id=active_version_id,
            ),
            issues,
        )

    def scan_and_validate(self, folder_path: str | Path | None = None) -> SyncScanReport:
        target = Path(folder_path) if folder_path is not None else self.get_configured_folder()
        if target is None:
            return SyncScanReport(
                folder_path=Path(),
                folder_available=False,
                catalog=None,
                items=(),
                issues=(),
                scanned_count=0,
                new_count=0,
                modified_count=0,
                unchanged_count=0,
                error_message="Nu este configurat niciun folder pentru șabloane DOCX.",
            )

        available, availability_reason = self.check_folder_availability(target)
        if not available:
            return SyncScanReport(
                folder_path=target,
                folder_available=False,
                catalog=None,
                items=(),
                issues=(SyncIssue("error", str(target), availability_reason),),
                scanned_count=0,
                new_count=0,
                modified_count=0,
                unchanged_count=0,
                error_message=availability_reason,
            )

        catalog, issues = load_template_catalog(target)
        if catalog is None:
            return SyncScanReport(
                folder_path=target,
                folder_available=True,
                catalog=None,
                items=(),
                issues=tuple(issues),
                scanned_count=0,
                new_count=0,
                modified_count=0,
                unchanged_count=0,
            )

        resolved_diags, resolved_intervs = self._resolve_existing_entities(catalog, issues)
        items: list[ScannedTemplateItem] = []

        for diag_entry in catalog.diagnoses:
            if diag_entry.epicrisis_path is None or diag_entry.epicrisis_relpath is None:
                continue
            item, doc_issues = self._inspect_document_entry(
                catalog_key=diag_entry.key,
                diagnosis_key=diag_entry.key,
                owner_type="diagnosis",
                owner_name=diag_entry.name,
                owner_code=diag_entry.code,
                document_type=EPICRISIS_RECOMMENDATIONS,
                relative_path=diag_entry.epicrisis_relpath,
                file_path=diag_entry.epicrisis_path,
                existing_owner=resolved_diags.get(diag_entry.key),
            )
            issues.extend(doc_issues)
            if item is not None:
                items.append(item)

        for interv_entry in catalog.interventions:
            if interv_entry.protocol_path is None or interv_entry.protocol_relpath is None:
                continue
            item, doc_issues = self._inspect_document_entry(
                catalog_key=interv_entry.key,
                diagnosis_key=interv_entry.diagnosis_key,
                owner_type="intervention",
                owner_name=interv_entry.name,
                owner_code=interv_entry.code,
                document_type=OPERATIVE_PROTOCOL,
                relative_path=interv_entry.protocol_relpath,
                file_path=interv_entry.protocol_path,
                existing_owner=resolved_intervs.get(interv_entry.key),
                parent_owner=resolved_diags.get(interv_entry.diagnosis_key),
            )
            issues.extend(doc_issues)
            if item is not None:
                items.append(item)

        new_count = sum(1 for item in items if item.change_kind == "new")
        modified_count = sum(1 for item in items if item.change_kind == "modified")
        unchanged_count = sum(1 for item in items if item.change_kind == "unchanged")
        linked_count = sum(
            1 for item in items if item.change_kind == "unchanged" and item.needs_sync_link
        )

        return SyncScanReport(
            folder_path=target,
            folder_available=True,
            catalog=catalog,
            items=tuple(items),
            issues=tuple(issues),
            scanned_count=len(items),
            new_count=new_count,
            modified_count=modified_count,
            unchanged_count=unchanged_count,
            linked_count=linked_count,
            resolved_diagnosis_ids={
                key: (int(row["id"]) if row is not None else None)
                for key, row in resolved_diags.items()
            },
            resolved_intervention_ids={
                key: (int(row["id"]) if row is not None else None)
                for key, row in resolved_intervs.items()
            },
        )

    def _ensure_catalog_entities_locked(
        self,
        catalog: TemplateCatalog,
        scan_report: SyncScanReport,
        *,
        timestamp: str,
    ) -> tuple[dict[str, int], dict[str, int]]:
        diagnosis_ids: dict[str, int] = {}
        for diag in catalog.diagnoses:
            existing_id = scan_report.resolved_diagnosis_ids.get(diag.key)
            if existing_id is None:
                diag_id = repositories.insert_diagnosis(
                    self.connection,
                    name=diag.name,
                    name_key=diag.name_key,
                    code=diag.code,
                    sort_order=0,
                )
            else:
                diag_id = existing_id
                current_row = repositories.get_diagnosis(self.connection, diag_id)
                if current_row is None:
                    raise TemplateSyncError(
                        f"Diagnosticul ID {diag_id} a dispărut în timpul sincronizării."
                    )
                target_code = diag.code if diag.code is not None else current_row["code"]
                if (
                    str(current_row["name"]) != diag.name
                    or current_row["code"] != target_code
                ):
                    medical_library.update_diagnosis(
                        self.connection,
                        diag_id,
                        diag.name,
                        code=target_code,
                    )

            mapped = template_sync_repositories.get_sync_entity_by_key(
                self.connection,
                entity_type="diagnosis",
                catalog_key=diag.key,
            )
            if mapped is None:
                template_sync_repositories.insert_sync_entity(
                    self.connection,
                    entity_type="diagnosis",
                    catalog_key=diag.key,
                    diagnosis_id=diag_id,
                    timestamp=timestamp,
                )
            else:
                template_sync_repositories.touch_sync_entity(
                    self.connection,
                    entity_type="diagnosis",
                    catalog_key=diag.key,
                    updated_at=timestamp,
                )
            diagnosis_ids[diag.key] = diag_id

        intervention_ids: dict[str, int] = {}
        for interv in catalog.interventions:
            parent_diag_id = diagnosis_ids[interv.diagnosis_key]
            existing_id = scan_report.resolved_intervention_ids.get(interv.key)
            if existing_id is None:
                interv_id = repositories.insert_intervention(
                    self.connection,
                    diagnosis_id=parent_diag_id,
                    name=interv.name,
                    name_key=interv.name_key,
                    code=interv.code,
                    sort_order=0,
                )
            else:
                interv_id = existing_id
                current_row = repositories.get_intervention(self.connection, interv_id)
                if current_row is None:
                    raise TemplateSyncError(
                        f"Intervenția ID {interv_id} a dispărut în timpul sincronizării."
                    )
                if int(current_row["diagnosis_id"]) != parent_diag_id:
                    raise TemplateSyncError(
                        "Relația diagnostic-intervenție s-a modificat incompatibil."
                    )
                target_code = interv.code if interv.code is not None else current_row["code"]
                if (
                    str(current_row["name"]) != interv.name
                    or current_row["code"] != target_code
                ):
                    medical_library.update_intervention(
                        self.connection,
                        interv_id,
                        interv.name,
                        code=target_code,
                        diagnosis_id=parent_diag_id,
                    )

            mapped = template_sync_repositories.get_sync_entity_by_key(
                self.connection,
                entity_type="intervention",
                catalog_key=interv.key,
            )
            if mapped is None:
                template_sync_repositories.insert_sync_entity(
                    self.connection,
                    entity_type="intervention",
                    catalog_key=interv.key,
                    intervention_id=interv_id,
                    timestamp=timestamp,
                )
            else:
                template_sync_repositories.touch_sync_entity(
                    self.connection,
                    entity_type="intervention",
                    catalog_key=interv.key,
                    updated_at=timestamp,
                )
            intervention_ids[interv.key] = interv_id

        return diagnosis_ids, intervention_ids

    def apply_sync(
        self,
        scan_report: SyncScanReport,
        *,
        patient_identification_confirmed: bool,
    ) -> SyncOutcome:
        """Execută backup-ul și tranzacția atomică pentru lotul validat."""

        if not scan_report.folder_available:
            return SyncOutcome(
                status="folder_unavailable",
                folder_path=str(scan_report.folder_path) if scan_report.folder_path else None,
                summary_message=scan_report.format_error_report(),
                issues=scan_report.issues,
            )

        if scan_report.issues or scan_report.catalog is None:
            return SyncOutcome(
                status="validation_failed",
                folder_path=str(scan_report.folder_path),
                scanned_count=scan_report.scanned_count,
                new_count=scan_report.new_count,
                modified_count=scan_report.modified_count,
                unchanged_count=scan_report.unchanged_count,
                summary_message=scan_report.format_error_report(),
                issues=scan_report.issues,
            )

        if not scan_report.has_changes and not scan_report.has_pending_links:
            return SyncOutcome(
                status="no_changes",
                folder_path=str(scan_report.folder_path),
                scanned_count=scan_report.scanned_count,
                unchanged_count=scan_report.unchanged_count,
                summary_message=(
                    "Toate șabloanele DOCX sunt deja sincronizate.\n\n"
                    f"Template-uri scanate: {scan_report.scanned_count}\n"
                    f"Neschimbate: {scan_report.unchanged_count}"
                ),
            )

        if scan_report.has_changes and not patient_identification_confirmed:
            return SyncOutcome(
                status="cancelled",
                folder_path=str(scan_report.folder_path),
                scanned_count=scan_report.scanned_count,
                new_count=scan_report.new_count,
                modified_count=scan_report.modified_count,
                unchanged_count=scan_report.unchanged_count,
                summary_message=(
                    "Sincronizarea a fost anulată de utilizator.\n\n"
                    "Baza de date nu a fost modificată.\n"
                    "Template-urile anterioare rămân active."
                ),
            )

        if self.connection.in_transaction:
            raise TemplateSyncError(
                "Sincronizarea trebuie pornită în afara unei tranzacții SQLite active."
            )

        try:
            backup_path = self.create_backup()
        except Exception as exc:
            return SyncOutcome(
                status="backup_failed",
                folder_path=str(scan_report.folder_path),
                scanned_count=scan_report.scanned_count,
                new_count=scan_report.new_count,
                modified_count=scan_report.modified_count,
                unchanged_count=scan_report.unchanged_count,
                summary_message=(
                    "Sincronizarea a fost oprită deoarece backup-ul SQLite nu a putut fi creat sau verificat.\n\n"
                    f"Detalii: {exc}\n\n"
                    "Baza de date nu a fost modificată."
                ),
                issues=(SyncIssue("error", "Backup SQLite", str(exc)),),
            )

        timestamp = _now_utc()
        imported_count = 0
        activated_count = 0
        linked_count = 0

        try:
            with write_transaction(self.connection):
                diagnosis_ids, intervention_ids = self._ensure_catalog_entities_locked(
                    scan_report.catalog,
                    scan_report,
                    timestamp=timestamp,
                )

                for item in scan_report.pending_items():
                    if item.parsed is None:
                        raise TemplateSyncError(
                            f"Lipsește rezultatul parsării pentru {item.relative_path}."
                        )
                    owner_id = (
                        diagnosis_ids[item.catalog_key]
                        if item.owner_type == "diagnosis"
                        else intervention_ids[item.catalog_key]
                    )
                    created = template_sets.import_preparsed_independent_template(
                        self.connection,
                        owner_type=item.owner_type,
                        owner_id=owner_id,
                        document_bytes=item.document_bytes,
                        parsed=item.parsed,
                        document_type=item.document_type,
                    )
                    imported_count += 1

                    template_sets.activate_independent_template(
                        self.connection,
                        int(created["id"]),
                        patient_identification_review_confirmed=True,
                    )
                    activated_count += 1

                    template_sync_repositories.insert_sync_source(
                        self.connection,
                        template_version_id=int(created["id"]),
                        catalog_key=item.catalog_key,
                        relative_path=item.relative_path,
                        synced_at=timestamp,
                    )

                for item in scan_report.link_items():
                    owner_id = (
                        diagnosis_ids[item.catalog_key]
                        if item.owner_type == "diagnosis"
                        else intervention_ids[item.catalog_key]
                    )
                    active_version = (
                        independent_template_repositories.get_active_independent_template_version(
                            self.connection,
                            owner_type=item.owner_type,
                            owner_id=owner_id,
                            document_type=item.document_type,
                        )
                    )
                    if (
                        active_version is None
                        or str(active_version["source_sha256"]) != item.sha256
                    ):
                        raise TemplateSyncError(
                            f"Versiunea activă pentru {item.relative_path} s-a modificat înainte de asocierea sursei DOCX."
                        )
                    template_sync_repositories.upsert_sync_source(
                        self.connection,
                        template_version_id=int(active_version["id"]),
                        catalog_key=item.catalog_key,
                        relative_path=item.relative_path,
                        synced_at=timestamp,
                    )
                    linked_count += 1

                fk_errors = self.connection.execute("PRAGMA foreign_key_check").fetchall()
                if fk_errors:
                    raise TemplateSyncError(
                        f"Verificarea cheilor străine a raportat erori: {[tuple(row) for row in fk_errors]!r}"
                    )

                summary_message = _format_success_summary(
                    scanned_count=scan_report.scanned_count,
                    new_count=scan_report.new_count,
                    modified_count=scan_report.modified_count,
                    unchanged_count=scan_report.unchanged_count,
                    imported_count=imported_count,
                    activated_count=activated_count,
                    linked_count=linked_count,
                    backup_path=backup_path,
                )
                template_sync_repositories.insert_sync_run(
                    self.connection,
                    folder_path=str(scan_report.folder_path),
                    status="applied",
                    scanned_count=scan_report.scanned_count,
                    new_count=scan_report.new_count,
                    modified_count=scan_report.modified_count,
                    unchanged_count=scan_report.unchanged_count,
                    imported_count=imported_count,
                    activated_count=activated_count,
                    backup_path=str(backup_path),
                    summary_message=summary_message,
                    created_at=timestamp,
                )
        except Exception as exc:
            return SyncOutcome(
                status="failed",
                folder_path=str(scan_report.folder_path),
                scanned_count=scan_report.scanned_count,
                new_count=scan_report.new_count,
                modified_count=scan_report.modified_count,
                unchanged_count=scan_report.unchanged_count,
                backup_path=str(backup_path),
                summary_message=(
                    "Sincronizarea nu a fost aplicată deoarece actualizarea bazei de date a eșuat; "
                    "toate modificările au fost anulate (rollback).\n\n"
                    f"Eroare: {exc}\n\n"
                    "Template-urile anterioare rămân active."
                ),
                issues=(SyncIssue("error", "Tranzacție SQLite", str(exc)),),
            )

        return SyncOutcome(
            status="applied",
            folder_path=str(scan_report.folder_path),
            scanned_count=scan_report.scanned_count,
            new_count=scan_report.new_count,
            modified_count=scan_report.modified_count,
            unchanged_count=scan_report.unchanged_count,
            imported_count=imported_count,
            activated_count=activated_count,
            linked_count=linked_count,
            backup_path=str(backup_path),
            summary_message=summary_message,
            created_at=timestamp,
        )

    def synchronize(
        self,
        *,
        folder_path: str | Path | None = None,
        confirm_batch: Callable[[SyncScanReport], bool] | None = None,
    ) -> SyncOutcome:
        """Execută întregul flux de sincronizare pornind de la folderul configurat."""

        if folder_path is None and self.get_configured_folder() is None:
            return SyncOutcome(
                status="not_configured",
                folder_path=None,
                summary_message="Nu este configurat niciun folder pentru template-urile DOCX.",
            )

        scan_report = self.scan_and_validate(folder_path=folder_path)
        if scan_report.has_blocking_issues:
            return self.apply_sync(scan_report, patient_identification_confirmed=False)
        if not scan_report.has_changes:
            return self.apply_sync(scan_report, patient_identification_confirmed=False)

        if confirm_batch is None:
            raise ValueError(
                "confirm_batch este obligatoriu atunci când există modificări valide."
            )
        confirmed = bool(confirm_batch(scan_report))
        return self.apply_sync(
            scan_report,
            patient_identification_confirmed=confirmed,
        )

    def get_settings_summary(
        self,
        *,
        last_outcome: SyncOutcome | None = None,
    ) -> dict[str, Any]:
        """Returnează starea curentă pentru secțiunea Setări -> Template-uri DOCX."""

        folder = self.get_configured_folder()
        available, availability_text = self.check_folder_availability(folder)
        active_counts = template_sync_repositories.count_active_independent_templates(
            self.connection
        )
        persisted_run = template_sync_repositories.get_latest_sync_run(self.connection)

        last_sync_at: str | None = None
        last_sync_status: str = "Niciodată"
        if last_outcome is not None and last_outcome.status != "not_configured":
            last_sync_at = last_outcome.created_at
            last_sync_status = format_sync_status_label(last_outcome.status)
            if last_outcome.status == "applied":
                parts = [f"{last_outcome.activated_count} activate"]
                if last_outcome.linked_count > 0:
                    parts.append(f"{last_outcome.linked_count} asociate")
                parts.append(f"{last_outcome.unchanged_count} neschimbate")
                last_sync_status += f" ({', '.join(parts)})"
            elif last_outcome.status == "no_changes":
                last_sync_status += f" ({last_outcome.scanned_count} scanate)"
        elif persisted_run is not None:
            last_sync_at = str(persisted_run["created_at"])
            last_sync_status = format_sync_status_label(str(persisted_run["status"]))
            if persisted_run["status"] == "applied":
                last_sync_status += (
                    f" ({persisted_run['activated_count']} activate, "
                    f"{persisted_run['unchanged_count']} neschimbate)"
                )

        return {
            "folder_path": str(folder) if folder is not None else "",
            "folder_configured": folder is not None,
            "folder_available": available,
            "folder_availability_text": availability_text,
            "active_templates_total": active_counts["total"],
            "active_epicrisis_count": active_counts["epicrisis_recommendations"],
            "active_protocol_count": active_counts["operative_protocol"],
            "last_sync_at": last_sync_at,
            "last_sync_status": last_sync_status,
        }


__all__ = [
    "CATALOG_FILENAME",
    "DiagnosisCatalogEntry",
    "InterventionCatalogEntry",
    "PATIENT_CONFIRMATION_TEXT",
    "SUPPORTED_CATALOG_VERSION",
    "ScannedTemplateItem",
    "SyncIssue",
    "SyncOutcome",
    "SyncScanReport",
    "TemplateCatalog",
    "TemplateSyncError",
    "TemplateSyncService",
    "format_sync_status_label",
    "load_template_catalog",
]
