"""Migrarea tranzacțională a bazei de date SQLite OrtoDocs."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .database import (
    DatabaseError,
    connect_database,
    get_database_path,
)


LATEST_SCHEMA_VERSION = 4


class MigrationError(DatabaseError):
    """Migrarea bazei de date nu a putut fi încheiată."""


class UnsupportedDatabaseVersionError(MigrationError):
    """Baza de date a fost creată de o versiune mai nouă a aplicației."""


class UnversionedDatabaseError(MigrationError):
    """Baza are obiecte SQL, dar user_version este zero."""


class MigrationIntegrityError(MigrationError):
    """Verificarea integrității a eșuat în timpul migrării."""


class MigrationBackupError(MigrationError):
    """Nu s-a putut crea copia de siguranță înaintea migrării."""


SCHEMA_V1_SQL = r"""
CREATE TABLE diagnoses (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE
                CHECK (code IS NULL OR length(trim(code)) > 0),
    name        TEXT NOT NULL
                CHECK (length(trim(name)) > 0),
    name_key    TEXT NOT NULL UNIQUE
                CHECK (length(trim(name_key)) > 0),
    is_active   INTEGER NOT NULL DEFAULT 1
                CHECK (is_active IN (0, 1)),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE interventions (
    id           INTEGER PRIMARY KEY,
    diagnosis_id INTEGER NOT NULL,
    code         TEXT
                 CHECK (code IS NULL OR length(trim(code)) > 0),
    name         TEXT NOT NULL
                 CHECK (length(trim(name)) > 0),
    name_key     TEXT NOT NULL
                 CHECK (length(trim(name_key)) > 0),
    is_active    INTEGER NOT NULL DEFAULT 1
                 CHECK (is_active IN (0, 1)),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    UNIQUE (diagnosis_id, name_key),
    UNIQUE (diagnosis_id, code),
    UNIQUE (id, diagnosis_id),

    FOREIGN KEY (diagnosis_id)
        REFERENCES diagnoses (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE field_definitions (
    id              INTEGER PRIMARY KEY,
    variable_key    TEXT NOT NULL
                    CHECK (
                        variable_key GLOB '[a-z]*'
                        AND variable_key NOT GLOB '*[^a-z0-9_]*'
                    ),
    version         INTEGER NOT NULL CHECK (version > 0),
    label           TEXT NOT NULL CHECK (length(trim(label)) > 0),
    data_type       TEXT NOT NULL
                    CHECK (data_type IN ('text', 'number', 'date', 'select')),
    scope           TEXT NOT NULL
                    CHECK (scope IN ('common', 'diagnosis', 'intervention')),
    diagnosis_id    INTEGER,
    intervention_id INTEGER,
    is_current      INTEGER NOT NULL DEFAULT 0
                    CHECK (is_current IN (0, 1)),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    UNIQUE (variable_key, version),
    UNIQUE (variable_key, id),

    CHECK (
        (scope = 'common'
            AND diagnosis_id IS NULL
            AND intervention_id IS NULL)
        OR
        (scope = 'diagnosis'
            AND diagnosis_id IS NOT NULL
            AND intervention_id IS NULL)
        OR
        (scope = 'intervention'
            AND diagnosis_id IS NULL
            AND intervention_id IS NOT NULL)
    ),

    FOREIGN KEY (diagnosis_id)
        REFERENCES diagnoses (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,

    FOREIGN KEY (intervention_id)
        REFERENCES interventions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE UNIQUE INDEX ux_field_definitions_one_current
    ON field_definitions (variable_key)
    WHERE is_current = 1;

CREATE TABLE field_options (
    field_definition_id INTEGER NOT NULL,
    option_value        TEXT NOT NULL
                        CHECK (length(trim(option_value)) > 0),
    option_label        TEXT NOT NULL
                        CHECK (length(trim(option_label)) > 0),
    sort_order          INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (field_definition_id, option_value),

    FOREIGN KEY (field_definition_id)
        REFERENCES field_definitions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE template_sets (
    id                 INTEGER PRIMARY KEY,
    intervention_id    INTEGER NOT NULL,
    version_no         INTEGER NOT NULL CHECK (version_no > 0),
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'active', 'retired', 'rejected')),
    created_at         TEXT NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    activated_at       TEXT,
    retired_at         TEXT,
    import_report_json TEXT NOT NULL DEFAULT '{}'
                       CHECK (json_valid(import_report_json)),

    UNIQUE (intervention_id, version_no),
    UNIQUE (id, intervention_id),

    CHECK (
        (status IN ('draft', 'rejected')
            AND activated_at IS NULL
            AND retired_at IS NULL)
        OR
        (status = 'active'
            AND activated_at IS NOT NULL
            AND retired_at IS NULL)
        OR
        (status = 'retired'
            AND activated_at IS NOT NULL
            AND retired_at IS NOT NULL)
    ),

    FOREIGN KEY (intervention_id)
        REFERENCES interventions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE UNIQUE INDEX ux_template_sets_one_active_per_intervention
    ON template_sets (intervention_id)
    WHERE status = 'active';

CREATE TABLE templates (
    id                     INTEGER PRIMARY KEY,
    template_set_id        INTEGER NOT NULL,
    document_type          TEXT NOT NULL
                           CHECK (document_type IN (
                               'epicrisis_recommendations',
                               'operative_protocol'
                           )),
    source_docx            BLOB NOT NULL
                           CHECK (length(source_docx) > 0),
    source_sha256          TEXT NOT NULL
                           CHECK (
                               length(source_sha256) = 64
                               AND source_sha256 NOT GLOB '*[^0-9a-f]*'
                           ),
    parsed_content_json    TEXT NOT NULL
                           CHECK (json_valid(parsed_content_json)),
    validation_status      TEXT NOT NULL DEFAULT 'pending'
                           CHECK (validation_status IN ('pending', 'valid', 'invalid')),
    validation_report_json TEXT NOT NULL DEFAULT '{}'
                           CHECK (json_valid(validation_report_json)),
    parser_version         TEXT NOT NULL
                           CHECK (length(trim(parser_version)) > 0),
    imported_at            TEXT NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    UNIQUE (template_set_id, document_type),
    UNIQUE (id, template_set_id, document_type),

    FOREIGN KEY (template_set_id)
        REFERENCES template_sets (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE template_variables (
    template_id         INTEGER NOT NULL,
    variable_key        TEXT NOT NULL,
    field_definition_id INTEGER NOT NULL,
    is_required         INTEGER NOT NULL DEFAULT 1
                        CHECK (is_required IN (0, 1)),

    PRIMARY KEY (template_id, variable_key),
    UNIQUE (template_id, field_definition_id),

    FOREIGN KEY (template_id)
        REFERENCES templates (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,

    FOREIGN KEY (variable_key, field_definition_id)
        REFERENCES field_definitions (variable_key, id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE generation_sessions (
    id                          INTEGER PRIMARY KEY,
    diagnosis_id                INTEGER NOT NULL,
    intervention_id             INTEGER NOT NULL,
    template_set_id             INTEGER NOT NULL,
    diagnosis_label_snapshot    TEXT NOT NULL
                                CHECK (length(trim(diagnosis_label_snapshot)) > 0),
    intervention_label_snapshot TEXT NOT NULL
                                CHECK (length(trim(intervention_label_snapshot)) > 0),
    created_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status                      TEXT NOT NULL DEFAULT 'building'
                                CHECK (status IN ('building', 'complete', 'deleting')),

    UNIQUE (id, template_set_id),

    FOREIGN KEY (intervention_id, diagnosis_id)
        REFERENCES interventions (id, diagnosis_id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,

    FOREIGN KEY (template_set_id, intervention_id)
        REFERENCES template_sets (id, intervention_id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_generation_sessions_history
    ON generation_sessions (status, created_at DESC, id DESC);

CREATE TABLE generated_documents (
    session_id      INTEGER NOT NULL,
    template_set_id INTEGER NOT NULL,
    document_type   TEXT NOT NULL
                    CHECK (document_type IN (
                        'epicrisis_recommendations',
                        'operative_protocol'
                    )),
    template_id     INTEGER NOT NULL,
    rich_text_json  TEXT NOT NULL
                    CHECK (json_valid(rich_text_json)),
    edited_at       TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (session_id, document_type),

    FOREIGN KEY (session_id, template_set_id)
        REFERENCES generation_sessions (id, template_set_id)
        ON DELETE CASCADE
        ON UPDATE RESTRICT,

    FOREIGN KEY (template_id, template_set_id, document_type)
        REFERENCES templates (id, template_set_id, document_type)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE app_settings (
    setting_key TEXT PRIMARY KEY
                CHECK (length(trim(setting_key)) > 0),
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TRIGGER field_definition_version_guard
BEFORE INSERT ON field_definitions
BEGIN
    SELECT CASE
        WHEN NEW.version <> (
            SELECT COALESCE(MAX(version), 0) + 1
            FROM field_definitions
            WHERE variable_key = NEW.variable_key
        )
        THEN RAISE(ABORT, 'field version must be the next version')
    END;

    SELECT CASE
        WHEN NEW.version = 1 AND NEW.is_current <> 1
        THEN RAISE(ABORT, 'first field version must be current')
    END;

    SELECT CASE
        WHEN NEW.version > 1 AND NEW.is_current <> 0
        THEN RAISE(ABORT, 'new field versions must start as non-current')
    END;
END;

CREATE TRIGGER field_definition_content_no_update
BEFORE UPDATE OF
    variable_key, version, label, data_type, scope,
    diagnosis_id, intervention_id, created_at
ON field_definitions
BEGIN
    SELECT RAISE(ABORT, 'field definitions are versioned and cannot be edited');
END;

CREATE TRIGGER field_definition_no_delete
BEFORE DELETE ON field_definitions
BEGIN
    SELECT RAISE(ABORT, 'field definition versions are retained');
END;

CREATE TRIGGER field_options_select_only_insert
BEFORE INSERT ON field_options
WHEN COALESCE(
    (SELECT data_type FROM field_definitions WHERE id = NEW.field_definition_id),
    ''
) <> 'select'
BEGIN
    SELECT RAISE(ABORT, 'options are allowed only for select fields');
END;

CREATE TRIGGER field_options_no_update
BEFORE UPDATE ON field_options
BEGIN
    SELECT RAISE(ABORT, 'field options are immutable; create a new field version');
END;

CREATE TRIGGER field_options_no_delete
BEFORE DELETE ON field_options
BEGIN
    SELECT RAISE(ABORT, 'field options are retained');
END;

CREATE TRIGGER field_options_no_insert_after_publication
BEFORE INSERT ON field_options
WHEN EXISTS (
    SELECT 1
    FROM template_variables tv
    JOIN templates t ON t.id = tv.template_id
    JOIN template_sets ts ON ts.id = t.template_set_id
    WHERE tv.field_definition_id = NEW.field_definition_id
      AND ts.status <> 'draft'
)
BEGIN
    SELECT RAISE(ABORT, 'published field options are immutable');
END;

CREATE TRIGGER template_set_initial_status_guard
BEFORE INSERT ON template_sets
WHEN NEW.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'template sets must be inserted as draft');
END;

CREATE TRIGGER template_set_status_guard
BEFORE UPDATE OF status ON template_sets
WHEN NEW.status <> OLD.status
BEGIN
    SELECT CASE
        WHEN NOT (
            (OLD.status = 'draft' AND NEW.status IN ('active', 'rejected'))
            OR
            (OLD.status = 'active' AND NEW.status = 'retired')
        )
        THEN RAISE(ABORT, 'invalid template set status transition')
    END;

    SELECT CASE
        WHEN OLD.status = 'draft'
         AND NEW.status = 'active'
         AND (
             NEW.activated_at IS NULL
             OR NEW.retired_at IS NOT NULL
             OR NOT EXISTS (
                 SELECT 1
                 FROM interventions i
                 JOIN diagnoses d ON d.id = i.diagnosis_id
                 WHERE i.id = NEW.intervention_id
                   AND i.is_active = 1
                   AND d.is_active = 1
             )
             OR (
                 SELECT COUNT(*)
                 FROM templates
                 WHERE template_set_id = NEW.id
             ) <> 2
             OR (
                 SELECT COUNT(*)
                 FROM templates
                 WHERE template_set_id = NEW.id
                   AND validation_status = 'valid'
             ) <> 2
             OR EXISTS (
                 SELECT 1
                 FROM template_variables tv
                 JOIN templates t ON t.id = tv.template_id
                 JOIN field_definitions fd ON fd.id = tv.field_definition_id
                 JOIN interventions i ON i.id = NEW.intervention_id
                 WHERE t.template_set_id = NEW.id
                   AND (
                       fd.is_current <> 1
                       OR (
                           fd.scope = 'diagnosis'
                           AND fd.diagnosis_id <> i.diagnosis_id
                       )
                       OR (
                           fd.scope = 'intervention'
                           AND fd.intervention_id <> NEW.intervention_id
                       )
                       OR (
                           fd.data_type = 'select'
                           AND NOT EXISTS (
                               SELECT 1
                               FROM field_options fo
                               WHERE fo.field_definition_id = fd.id
                           )
                       )
                   )
             )
         )
        THEN RAISE(ABORT, 'template set is incomplete, invalid, or belongs to inactive records')
    END;

    SELECT CASE
        WHEN OLD.status = 'draft'
         AND NEW.status = 'rejected'
         AND (NEW.activated_at IS NOT NULL OR NEW.retired_at IS NOT NULL)
        THEN RAISE(ABORT, 'rejected template set cannot have activation timestamps')
    END;

    SELECT CASE
        WHEN OLD.status = 'active'
         AND NEW.status = 'retired'
         AND (
             NEW.activated_at IS NOT OLD.activated_at
             OR NEW.retired_at IS NULL
         )
        THEN RAISE(ABORT, 'retired template set must retain activation timestamp')
    END;
END;

CREATE TRIGGER template_set_timestamp_guard
BEFORE UPDATE OF activated_at, retired_at ON template_sets
WHEN NOT (
    (OLD.status = 'draft'
        AND NEW.status = 'active'
        AND NEW.activated_at IS NOT NULL
        AND NEW.retired_at IS NULL)
    OR
    (OLD.status = 'draft'
        AND NEW.status = 'rejected'
        AND NEW.activated_at IS NULL
        AND NEW.retired_at IS NULL)
    OR
    (OLD.status = 'active'
        AND NEW.status = 'retired'
        AND NEW.activated_at IS OLD.activated_at
        AND NEW.retired_at IS NOT NULL)
    OR
    (NEW.status IS OLD.status
        AND NEW.activated_at IS OLD.activated_at
        AND NEW.retired_at IS OLD.retired_at)
)
BEGIN
    SELECT RAISE(ABORT, 'template set timestamps may change only during a valid transition');
END;

CREATE TRIGGER template_set_metadata_guard
BEFORE UPDATE OF intervention_id, version_no, created_at, import_report_json
ON template_sets
WHEN OLD.status <> 'draft' OR NEW.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published template set metadata is immutable');
END;

CREATE TRIGGER template_set_no_delete
BEFORE DELETE ON template_sets
BEGIN
    SELECT RAISE(ABORT, 'template set versions are retained');
END;

CREATE TRIGGER template_insert_only_into_draft
BEFORE INSERT ON templates
WHEN COALESCE(
    (SELECT status FROM template_sets WHERE id = NEW.template_set_id),
    ''
) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'templates can be added only to draft sets');
END;

CREATE TRIGGER template_initial_validation_guard
BEFORE INSERT ON templates
WHEN NEW.validation_status <> 'pending'
BEGIN
    SELECT RAISE(ABORT, 'templates must be inserted as pending');
END;

CREATE TRIGGER template_content_no_update
BEFORE UPDATE OF
    template_set_id, document_type, source_docx, source_sha256,
    parsed_content_json, parser_version, imported_at
ON templates
BEGIN
    SELECT RAISE(ABORT, 'imported template content cannot be edited');
END;

CREATE TRIGGER template_validation_no_update_after_publication
BEFORE UPDATE OF validation_status, validation_report_json ON templates
WHEN COALESCE(
    (SELECT status
     FROM template_sets
     WHERE id = OLD.template_set_id),
    ''
) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published template validation data is immutable');
END;

CREATE TRIGGER template_no_delete_after_publication
BEFORE DELETE ON templates
WHEN COALESCE(
    (SELECT status
     FROM template_sets
     WHERE id = OLD.template_set_id),
    ''
) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published templates are retained');
END;

CREATE TRIGGER template_variable_scope_guard_insert
BEFORE INSERT ON template_variables
WHEN NOT EXISTS (
    SELECT 1
    FROM templates t
    JOIN template_sets ts ON ts.id = t.template_set_id
    JOIN interventions i ON i.id = ts.intervention_id
    JOIN field_definitions fd ON fd.id = NEW.field_definition_id
    WHERE t.id = NEW.template_id
      AND ts.status = 'draft'
      AND fd.variable_key = NEW.variable_key
      AND (
          fd.scope = 'common'
          OR (fd.scope = 'diagnosis' AND fd.diagnosis_id = i.diagnosis_id)
          OR (fd.scope = 'intervention' AND fd.intervention_id = ts.intervention_id)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'variable mapping is invalid for this template');
END;

CREATE TRIGGER template_variable_scope_guard_update
BEFORE UPDATE ON template_variables
WHEN NOT EXISTS (
    SELECT 1
    FROM templates t
    JOIN template_sets ts ON ts.id = t.template_set_id
    JOIN interventions i ON i.id = ts.intervention_id
    JOIN field_definitions fd ON fd.id = NEW.field_definition_id
    WHERE t.id = NEW.template_id
      AND ts.status = 'draft'
      AND fd.variable_key = NEW.variable_key
      AND (
          fd.scope = 'common'
          OR (fd.scope = 'diagnosis' AND fd.diagnosis_id = i.diagnosis_id)
          OR (fd.scope = 'intervention' AND fd.intervention_id = ts.intervention_id)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'variable mapping is invalid for this template');
END;

CREATE TRIGGER template_variable_no_update_after_publication
BEFORE UPDATE ON template_variables
WHEN COALESCE(
    (SELECT ts.status
     FROM templates t
     JOIN template_sets ts ON ts.id = t.template_set_id
     WHERE t.id = OLD.template_id),
    ''
) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published variable mappings are immutable');
END;

CREATE TRIGGER template_variable_no_delete_after_publication
BEFORE DELETE ON template_variables
WHEN COALESCE(
    (SELECT ts.status
     FROM templates t
     JOIN template_sets ts ON ts.id = t.template_set_id
     WHERE t.id = OLD.template_id),
    ''
) <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published variable mappings are immutable');
END;

CREATE TRIGGER generation_session_insert_guard
BEFORE INSERT ON generation_sessions
WHEN NEW.status <> 'building'
  OR NOT EXISTS (
      SELECT 1
      FROM template_sets ts
      JOIN interventions i ON i.id = ts.intervention_id
      JOIN diagnoses d ON d.id = i.diagnosis_id
      WHERE ts.id = NEW.template_set_id
        AND ts.intervention_id = NEW.intervention_id
        AND i.id = NEW.intervention_id
        AND i.diagnosis_id = NEW.diagnosis_id
        AND d.id = NEW.diagnosis_id
        AND ts.status = 'active'
        AND i.is_active = 1
        AND d.is_active = 1
  )
BEGIN
    SELECT RAISE(ABORT, 'session requires an active diagnosis, intervention, and template set');
END;

CREATE TRIGGER generation_session_status_guard
BEFORE UPDATE OF status ON generation_sessions
WHEN NEW.status <> OLD.status
BEGIN
    SELECT CASE
        WHEN NOT (
            (OLD.status = 'building' AND NEW.status = 'complete')
            OR
            (OLD.status = 'complete' AND NEW.status = 'deleting')
        )
        THEN RAISE(ABORT, 'invalid generation session status transition')
    END;

    SELECT CASE
        WHEN OLD.status = 'building'
         AND NEW.status = 'complete'
         AND (
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id) <> 2
             OR
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id
                AND document_type = 'epicrisis_recommendations') <> 1
             OR
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id
                AND document_type = 'operative_protocol') <> 1
         )
        THEN RAISE(ABORT, 'a completed session must contain both documents')
    END;
END;

CREATE TRIGGER generation_session_identity_no_update
BEFORE UPDATE OF
    diagnosis_id, intervention_id, template_set_id,
    diagnosis_label_snapshot, intervention_label_snapshot, created_at
ON generation_sessions
BEGIN
    SELECT RAISE(ABORT, 'generation session identity is immutable');
END;

CREATE TRIGGER generation_session_delete_guard
BEFORE DELETE ON generation_sessions
WHEN OLD.status <> 'deleting'
BEGIN
    SELECT RAISE(ABORT, 'session must enter deleting state before removal');
END;

CREATE TRIGGER generated_document_insert_guard
BEFORE INSERT ON generated_documents
WHEN COALESCE(
    (SELECT status
     FROM generation_sessions
     WHERE id = NEW.session_id),
    ''
) <> 'building'
BEGIN
    SELECT RAISE(ABORT, 'documents can be added only while building a session');
END;

CREATE TRIGGER generated_document_identity_no_update
BEFORE UPDATE OF session_id, template_set_id, document_type, template_id
ON generated_documents
BEGIN
    SELECT RAISE(ABORT, 'generated document identity is immutable');
END;

CREATE TRIGGER generated_document_content_update_guard
BEFORE UPDATE OF rich_text_json, edited_at ON generated_documents
WHEN COALESCE(
    (SELECT status
     FROM generation_sessions
     WHERE id = OLD.session_id),
    ''
) <> 'complete'
BEGIN
    SELECT RAISE(ABORT, 'only completed session documents can be edited');
END;

CREATE TRIGGER generated_document_delete_guard
BEFORE DELETE ON generated_documents
WHEN EXISTS (
    SELECT 1
    FROM generation_sessions gs
    WHERE gs.id = OLD.session_id
      AND gs.status NOT IN ('building', 'deleting')
)
BEGIN
    SELECT RAISE(ABORT, 'documents in a completed session cannot be deleted individually');
END;

CREATE TRIGGER app_settings_updated_at
AFTER UPDATE OF value ON app_settings
WHEN NEW.value IS NOT OLD.value
BEGIN
    UPDATE app_settings
    SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE setting_key = NEW.setting_key;
END;
"""


SCHEMA_V2_SQL = r"""
-- Tabel aditiv pentru versiunile independente ale șabloanelor.
CREATE TABLE IF NOT EXISTS independent_template_versions (
    id                                  INTEGER PRIMARY KEY,
    owner_type                          TEXT NOT NULL
                                        CHECK (owner_type IN ('diagnosis', 'intervention')),
    diagnosis_id                        INTEGER,
    intervention_id                     INTEGER,
    document_type                       TEXT NOT NULL
                                        CHECK (document_type IN (
                                            'epicrisis_recommendations',
                                            'operative_protocol'
                                        )),
    version_no                          INTEGER NOT NULL CHECK (version_no > 0),
    status                              TEXT NOT NULL DEFAULT 'draft'
                                        CHECK (status IN ('draft', 'active', 'retired', 'rejected')),
    source_docx                         BLOB NOT NULL CHECK (length(source_docx) > 0),
    source_sha256                       TEXT NOT NULL
                                        CHECK (
                                            length(source_sha256) = 64
                                            AND source_sha256 NOT GLOB '*[^0-9a-f]*'
                                        ),
    parsed_content_json                 TEXT NOT NULL CHECK (json_valid(parsed_content_json)),
    parser_report_json                  TEXT NOT NULL CHECK (json_valid(parser_report_json)),
    parser_version                      TEXT NOT NULL
                                        CHECK (length(trim(parser_version)) > 0),
    validation_status                   TEXT NOT NULL DEFAULT 'pending'
                                        CHECK (validation_status IN ('pending', 'valid', 'invalid')),
    warnings_confirmed                  INTEGER NOT NULL DEFAULT 0
                                        CHECK (warnings_confirmed IN (0, 1)),
    warnings_confirmed_at               TEXT,
    patient_identification_reviewed     INTEGER NOT NULL DEFAULT 0
                                        CHECK (patient_identification_reviewed IN (0, 1)),
    patient_identification_reviewed_at  TEXT,
    created_at                          TEXT NOT NULL
                                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    imported_at                         TEXT NOT NULL
                                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    activated_at                        TEXT,
    retired_at                          TEXT,
    legacy_template_id                  INTEGER UNIQUE,

    CHECK (
        (owner_type = 'diagnosis'
            AND diagnosis_id IS NOT NULL
            AND intervention_id IS NULL
            AND document_type = 'epicrisis_recommendations')
        OR
        (owner_type = 'intervention'
            AND diagnosis_id IS NULL
            AND intervention_id IS NOT NULL
            AND document_type = 'operative_protocol')
    ),
    CHECK (
        (status IN ('draft', 'rejected')
            AND activated_at IS NULL
            AND retired_at IS NULL)
        OR
        (status = 'active'
            AND activated_at IS NOT NULL
            AND retired_at IS NULL)
        OR
        (status = 'retired'
            AND activated_at IS NOT NULL
            AND retired_at IS NOT NULL)
    ),
    CHECK (
        (warnings_confirmed = 0 AND warnings_confirmed_at IS NULL)
        OR (warnings_confirmed = 1 AND warnings_confirmed_at IS NOT NULL)
    ),
    CHECK (
        (patient_identification_reviewed = 0
            AND patient_identification_reviewed_at IS NULL)
        OR (patient_identification_reviewed = 1
            AND patient_identification_reviewed_at IS NOT NULL)
    ),

    FOREIGN KEY (diagnosis_id)
        REFERENCES diagnoses (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    FOREIGN KEY (intervention_id)
        REFERENCES interventions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    FOREIGN KEY (legacy_template_id)
        REFERENCES templates (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_independent_templates_diagnosis_version
    ON independent_template_versions (diagnosis_id, version_no)
    WHERE owner_type = 'diagnosis';

CREATE UNIQUE INDEX IF NOT EXISTS ux_independent_templates_intervention_version
    ON independent_template_versions (intervention_id, version_no)
    WHERE owner_type = 'intervention';

CREATE UNIQUE INDEX IF NOT EXISTS ux_independent_templates_one_active_diagnosis
    ON independent_template_versions (diagnosis_id)
    WHERE owner_type = 'diagnosis' AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS ux_independent_templates_one_active_intervention
    ON independent_template_versions (intervention_id)
    WHERE owner_type = 'intervention' AND status = 'active';

CREATE TRIGGER IF NOT EXISTS independent_template_initial_status_guard
BEFORE INSERT ON independent_template_versions
WHEN NEW.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'independent templates must be inserted as draft');
END;

CREATE TRIGGER IF NOT EXISTS independent_template_legacy_source_guard
BEFORE INSERT ON independent_template_versions
WHEN NEW.legacy_template_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM templates t
    JOIN template_sets ts ON ts.id = t.template_set_id
    JOIN interventions i ON i.id = ts.intervention_id
    WHERE t.id = NEW.legacy_template_id
      AND ts.status = 'active'
      AND t.validation_status = 'valid'
      AND t.document_type = NEW.document_type
      AND t.source_sha256 = NEW.source_sha256
      AND (
          (NEW.owner_type = 'diagnosis' AND i.diagnosis_id = NEW.diagnosis_id)
          OR
          (NEW.owner_type = 'intervention' AND i.id = NEW.intervention_id)
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'legacy template provenance does not match the new template');
END;

CREATE TRIGGER IF NOT EXISTS independent_template_content_immutable
BEFORE UPDATE OF
    owner_type, diagnosis_id, intervention_id, document_type, version_no,
    source_docx, source_sha256, parsed_content_json, parser_report_json,
    parser_version, created_at, imported_at, legacy_template_id
ON independent_template_versions
BEGIN
    SELECT RAISE(ABORT, 'independent template versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS independent_template_draft_metadata_guard
BEFORE UPDATE OF validation_status, warnings_confirmed, warnings_confirmed_at,
                 patient_identification_reviewed, patient_identification_reviewed_at
ON independent_template_versions
WHEN OLD.status <> 'draft' OR NEW.status <> 'draft'
BEGIN
    SELECT RAISE(ABORT, 'published independent template metadata is immutable');
END;

CREATE TRIGGER IF NOT EXISTS independent_template_status_guard
BEFORE UPDATE OF status ON independent_template_versions
WHEN NEW.status <> OLD.status
BEGIN
    SELECT CASE
        WHEN NOT (
            (OLD.status = 'draft' AND NEW.status IN ('active', 'rejected'))
            OR (OLD.status = 'active' AND NEW.status = 'retired')
        )
        THEN RAISE(ABORT, 'invalid independent template status transition')
    END;

    SELECT CASE
        WHEN OLD.status = 'draft'
         AND NEW.status = 'active'
         AND (
             NEW.activated_at IS NULL
             OR NEW.retired_at IS NOT NULL
             OR NEW.validation_status <> 'valid'
             OR NEW.patient_identification_reviewed <> 1
             OR (
                 json_extract(NEW.parser_report_json, '$.status') = 'valid_with_warnings'
                 AND NEW.warnings_confirmed <> 1
             )
             OR (
                 NEW.legacy_template_id IS NULL
                 AND (
                     (NEW.owner_type = 'diagnosis' AND NOT EXISTS (
                         SELECT 1 FROM diagnoses d
                         WHERE d.id = NEW.diagnosis_id AND d.is_active = 1
                     ))
                     OR
                     (NEW.owner_type = 'intervention' AND NOT EXISTS (
                         SELECT 1 FROM interventions i
                         WHERE i.id = NEW.intervention_id AND i.is_active = 1
                     ))
                 )
             )
         )
        THEN RAISE(ABORT, 'independent template is not ready for activation')
    END;

    SELECT CASE
        WHEN OLD.status = 'draft'
         AND NEW.status = 'rejected'
         AND (NEW.activated_at IS NOT NULL OR NEW.retired_at IS NOT NULL)
        THEN RAISE(ABORT, 'rejected independent template cannot have lifecycle timestamps')
    END;

    SELECT CASE
        WHEN OLD.status = 'active'
         AND NEW.status = 'retired'
         AND (
             NEW.activated_at IS NOT OLD.activated_at
             OR NEW.retired_at IS NULL
         )
        THEN RAISE(ABORT, 'retired independent template must retain activation timestamp')
    END;
END;

CREATE TRIGGER IF NOT EXISTS independent_template_timestamp_guard
BEFORE UPDATE OF activated_at, retired_at ON independent_template_versions
WHEN NOT (
    (OLD.status = 'draft' AND NEW.status = 'active'
        AND NEW.activated_at IS NOT NULL AND NEW.retired_at IS NULL)
    OR
    (OLD.status = 'draft' AND NEW.status = 'rejected'
        AND NEW.activated_at IS NULL AND NEW.retired_at IS NULL)
    OR
    (OLD.status = 'active' AND NEW.status = 'retired'
        AND NEW.activated_at IS OLD.activated_at AND NEW.retired_at IS NOT NULL)
    OR
    (NEW.status IS OLD.status
        AND NEW.activated_at IS OLD.activated_at
        AND NEW.retired_at IS OLD.retired_at)
)
BEGIN
    SELECT RAISE(ABORT, 'independent template timestamps may change only during a valid transition');
END;

CREATE TRIGGER IF NOT EXISTS independent_template_no_delete
BEFORE DELETE ON independent_template_versions
BEGIN
    SELECT RAISE(ABORT, 'independent template versions are retained');
END;

-- Copiază protocoalele valide din seturile active. Fiecare intervenție are cel
-- mult un set activ, iar proveniența face inserarea sigură la rerulare.
INSERT INTO independent_template_versions (
    owner_type, intervention_id, document_type, version_no, status,
    source_docx, source_sha256, parsed_content_json, parser_report_json,
    parser_version, validation_status, warnings_confirmed,
    warnings_confirmed_at, patient_identification_reviewed,
    patient_identification_reviewed_at, created_at, imported_at,
    legacy_template_id
)
SELECT
    'intervention', i.id, t.document_type,
    COALESCE((
        SELECT MAX(v.version_no) + 1
        FROM independent_template_versions v
        WHERE v.owner_type = 'intervention' AND v.intervention_id = i.id
    ), 1),
    'draft', t.source_docx, migration_verified_legacy_sha256(t.id), t.parsed_content_json,
    COALESCE(json_extract(t.validation_report_json, '$.import_report'), '{}'),
    t.parser_version, t.validation_status,
    CASE WHEN json_extract(t.validation_report_json, '$.warning_confirmation.confirmed') = 1
         THEN 1 ELSE 0 END,
    json_extract(t.validation_report_json, '$.warning_confirmation.confirmed_at'),
    CASE WHEN json_extract(ts.import_report_json, '$.patient_identification_review.confirmed') = 1
         THEN 1 ELSE 0 END,
    json_extract(ts.import_report_json, '$.patient_identification_review.confirmed_at'),
    t.imported_at, t.imported_at, t.id
FROM template_sets ts
JOIN interventions i ON i.id = ts.intervention_id
JOIN templates t ON t.template_set_id = ts.id
WHERE ts.status = 'active'
  AND t.document_type = 'operative_protocol'
  AND t.validation_status = 'valid'
  AND json_extract(t.validation_report_json, '$.import_report.status')
      IN ('valid', 'valid_with_warnings')
  AND length(t.source_docx) > 0
  AND length(t.source_sha256) = 64
  AND json_valid(t.parsed_content_json)
  AND NOT EXISTS (
      SELECT 1 FROM independent_template_versions v
      WHERE v.legacy_template_id = t.id
  );

-- O epicriză se copiază la nivelul diagnosticului doar dacă toate documentele
-- valide disponibile în seturile active ale diagnosticului au același SHA-256.
WITH available_epicrises AS (
    SELECT i.diagnosis_id, i.id AS intervention_id, t.*,
           migration_verified_legacy_sha256(t.id) AS verified_sha256,
           ts.activated_at AS source_activated_at,
           ts.import_report_json AS source_package_report
    FROM template_sets ts
    JOIN interventions i ON i.id = ts.intervention_id
    JOIN templates t ON t.template_set_id = ts.id
    WHERE ts.status = 'active'
      AND t.document_type = 'epicrisis_recommendations'
      AND t.validation_status = 'valid'
      AND json_extract(t.validation_report_json, '$.import_report.status')
          IN ('valid', 'valid_with_warnings')
      AND length(t.source_docx) > 0
      AND length(t.source_sha256) = 64
      AND json_valid(t.parsed_content_json)
),
consistent_diagnoses AS (
    SELECT diagnosis_id
    FROM available_epicrises
    GROUP BY diagnosis_id
    HAVING COUNT(DISTINCT verified_sha256) = 1
),
chosen_epicrises AS (
    SELECT a.*
    FROM available_epicrises a
    JOIN consistent_diagnoses c ON c.diagnosis_id = a.diagnosis_id
    WHERE a.id = (
        SELECT MIN(other.id)
        FROM available_epicrises other
        WHERE other.diagnosis_id = a.diagnosis_id
    )
)
INSERT INTO independent_template_versions (
    owner_type, diagnosis_id, document_type, version_no, status,
    source_docx, source_sha256, parsed_content_json, parser_report_json,
    parser_version, validation_status, warnings_confirmed,
    warnings_confirmed_at, patient_identification_reviewed,
    patient_identification_reviewed_at, created_at, imported_at,
    legacy_template_id
)
SELECT
    'diagnosis', c.diagnosis_id, t.document_type,
    COALESCE((
        SELECT MAX(v.version_no) + 1
        FROM independent_template_versions v
        WHERE v.owner_type = 'diagnosis' AND v.diagnosis_id = c.diagnosis_id
    ), 1),
    'draft', t.source_docx, c.verified_sha256, t.parsed_content_json,
    COALESCE(json_extract(t.validation_report_json, '$.import_report'), '{}'),
    t.parser_version, t.validation_status,
    CASE WHEN json_extract(t.validation_report_json, '$.warning_confirmation.confirmed') = 1
         THEN 1 ELSE 0 END,
    json_extract(t.validation_report_json, '$.warning_confirmation.confirmed_at'),
    CASE WHEN json_extract(c.source_package_report, '$.patient_identification_review.confirmed') = 1
         THEN 1 ELSE 0 END,
    json_extract(c.source_package_report, '$.patient_identification_review.confirmed_at'),
    t.imported_at, t.imported_at, t.id
FROM chosen_epicrises c
JOIN templates t ON t.id = c.id
WHERE NOT EXISTS (
    SELECT 1 FROM independent_template_versions v
    WHERE v.legacy_template_id = t.id
);

-- Publicarea rândurilor migrate se face numai dacă datele sursă confirmă
-- validarea, verificarea identificatorilor și eventualele avertismente.
UPDATE independent_template_versions
SET status = 'active',
    activated_at = (
        SELECT ts.activated_at
        FROM templates t JOIN template_sets ts ON ts.id = t.template_set_id
        WHERE t.id = independent_template_versions.legacy_template_id
    )
WHERE status = 'draft'
  AND legacy_template_id IS NOT NULL
  AND validation_status = 'valid'
  AND patient_identification_reviewed = 1
  AND (
      json_extract(parser_report_json, '$.status') <> 'valid_with_warnings'
      OR warnings_confirmed = 1
  )
  AND EXISTS (
      SELECT 1
      FROM templates t JOIN template_sets ts ON ts.id = t.template_set_id
      WHERE t.id = independent_template_versions.legacy_template_id
        AND ts.status = 'active'
  );
"""


_V2_LEGACY_HASH_PREFLIGHT_SQL = r"""
SELECT migration_check_legacy_sha256(
    t.id, t.document_type, t.source_docx, t.source_sha256
)
FROM template_sets ts
JOIN interventions i ON i.id = ts.intervention_id
JOIN templates t ON t.template_set_id = ts.id
WHERE ts.status = 'active'
  AND t.document_type IN ('epicrisis_recommendations', 'operative_protocol')
  AND t.validation_status = 'valid'
  AND json_extract(t.validation_report_json, '$.import_report.status')
      IN ('valid', 'valid_with_warnings')
  AND length(t.source_docx) > 0
  AND length(t.source_sha256) = 64
  AND json_valid(t.parsed_content_json)
ORDER BY t.id;
"""


SCHEMA_V3_SQL = r"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_independent_templates_id_doctype
    ON independent_template_versions (id, document_type);

DROP TRIGGER IF EXISTS generated_document_insert_guard;
DROP TRIGGER IF EXISTS generated_document_identity_no_update;
DROP TRIGGER IF EXISTS generated_document_content_update_guard;
DROP TRIGGER IF EXISTS generated_document_delete_guard;
DROP TRIGGER IF EXISTS generation_session_insert_guard;
DROP TRIGGER IF EXISTS generation_session_status_guard;
DROP TRIGGER IF EXISTS generation_session_identity_no_update;
DROP TRIGGER IF EXISTS generation_session_delete_guard;

DROP TABLE IF EXISTS _v3_staging_generated_documents;
CREATE TABLE _v3_staging_generated_documents (
    session_id                      INTEGER NOT NULL,
    template_set_id                 INTEGER NULL,
    document_type                   TEXT NOT NULL,
    template_id                     INTEGER NULL,
    independent_template_version_id INTEGER NULL,
    rich_text_json                  TEXT NOT NULL,
    edited_at                       TEXT NOT NULL
);

INSERT INTO _v3_staging_generated_documents (
    session_id, template_set_id, document_type, template_id,
    independent_template_version_id, rich_text_json, edited_at
)
SELECT
    session_id, template_set_id, document_type, template_id,
    NULL, rich_text_json, edited_at
FROM generated_documents;

DROP TABLE generated_documents;

DROP TABLE IF EXISTS generation_sessions_v3;
CREATE TABLE generation_sessions_v3 (
    id                          INTEGER PRIMARY KEY,
    diagnosis_id                INTEGER NOT NULL,
    intervention_id             INTEGER NOT NULL,
    template_set_id             INTEGER NULL,
    diagnosis_label_snapshot    TEXT NOT NULL
                                CHECK (length(trim(diagnosis_label_snapshot)) > 0),
    intervention_label_snapshot TEXT NOT NULL
                                CHECK (length(trim(intervention_label_snapshot)) > 0),
    created_at                  TEXT NOT NULL
                                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status                      TEXT NOT NULL DEFAULT 'building'
                                CHECK (status IN ('building', 'complete', 'deleting')),

    UNIQUE (id, template_set_id),

    FOREIGN KEY (intervention_id, diagnosis_id)
        REFERENCES interventions (id, diagnosis_id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,

    FOREIGN KEY (template_set_id, intervention_id)
        REFERENCES template_sets (id, intervention_id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

INSERT INTO generation_sessions_v3 (
    id, diagnosis_id, intervention_id, template_set_id,
    diagnosis_label_snapshot, intervention_label_snapshot,
    created_at, status
)
SELECT
    id, diagnosis_id, intervention_id, template_set_id,
    diagnosis_label_snapshot, intervention_label_snapshot,
    created_at, status
FROM generation_sessions;

DROP TABLE generation_sessions;
ALTER TABLE generation_sessions_v3 RENAME TO generation_sessions;

CREATE INDEX IF NOT EXISTS ix_generation_sessions_history
    ON generation_sessions (status, created_at DESC, id DESC);

CREATE TABLE generated_documents (
    session_id                      INTEGER NOT NULL,
    template_set_id                 INTEGER NULL,
    document_type                   TEXT NOT NULL
                                    CHECK (document_type IN (
                                        'epicrisis_recommendations',
                                        'operative_protocol'
                                    )),
    template_id                     INTEGER NULL,
    independent_template_version_id INTEGER NULL,
    rich_text_json                  TEXT NOT NULL
                                    CHECK (json_valid(rich_text_json)),
    edited_at                       TEXT NOT NULL
                                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CHECK (
        (
            template_set_id IS NOT NULL
            AND template_id IS NOT NULL
            AND independent_template_version_id IS NULL
        )
        OR
        (
            template_set_id IS NULL
            AND template_id IS NULL
            AND independent_template_version_id IS NOT NULL
        )
    ),

    PRIMARY KEY (session_id, document_type),

    FOREIGN KEY (session_id)
        REFERENCES generation_sessions (id)
        ON DELETE CASCADE
        ON UPDATE RESTRICT,

    FOREIGN KEY (session_id, template_set_id)
        REFERENCES generation_sessions (id, template_set_id)
        ON DELETE CASCADE
        ON UPDATE RESTRICT,

    FOREIGN KEY (template_id, template_set_id, document_type)
        REFERENCES templates (id, template_set_id, document_type)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,

    FOREIGN KEY (independent_template_version_id, document_type)
        REFERENCES independent_template_versions (id, document_type)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

INSERT INTO generated_documents (
    session_id, template_set_id, document_type, template_id,
    independent_template_version_id, rich_text_json, edited_at
)
SELECT
    session_id, template_set_id, document_type, template_id,
    independent_template_version_id, rich_text_json, edited_at
FROM _v3_staging_generated_documents;

DROP TABLE _v3_staging_generated_documents;

CREATE INDEX IF NOT EXISTS ix_generated_documents_independent_template
    ON generated_documents (independent_template_version_id);

CREATE TRIGGER generation_session_insert_guard
BEFORE INSERT ON generation_sessions
WHEN NEW.status <> 'building'
  OR (
      NEW.template_set_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM template_sets ts
          JOIN interventions i ON i.id = ts.intervention_id
          JOIN diagnoses d ON d.id = i.diagnosis_id
          WHERE ts.id = NEW.template_set_id
            AND ts.intervention_id = NEW.intervention_id
            AND i.id = NEW.intervention_id
            AND i.diagnosis_id = NEW.diagnosis_id
            AND d.id = NEW.diagnosis_id
            AND ts.status = 'active'
            AND i.is_active = 1
            AND d.is_active = 1
      )
  )
  OR (
      NEW.template_set_id IS NULL
      AND (
          NOT EXISTS (
              SELECT 1
              FROM interventions i
              JOIN diagnoses d ON d.id = i.diagnosis_id
              WHERE i.id = NEW.intervention_id
                AND i.diagnosis_id = NEW.diagnosis_id
                AND d.id = NEW.diagnosis_id
                AND i.is_active = 1
                AND d.is_active = 1
          )
          OR NOT EXISTS (
              SELECT 1
              FROM independent_template_versions itv_d
              WHERE itv_d.owner_type = 'diagnosis'
                AND itv_d.diagnosis_id = NEW.diagnosis_id
                AND itv_d.document_type = 'epicrisis_recommendations'
                AND itv_d.status = 'active'
                AND itv_d.validation_status = 'valid'
                AND itv_d.patient_identification_reviewed = 1
          )
          OR NOT EXISTS (
              SELECT 1
              FROM independent_template_versions itv_i
              WHERE itv_i.owner_type = 'intervention'
                AND itv_i.intervention_id = NEW.intervention_id
                AND itv_i.document_type = 'operative_protocol'
                AND itv_i.status = 'active'
                AND itv_i.validation_status = 'valid'
                AND itv_i.patient_identification_reviewed = 1
          )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'session requires an active diagnosis, intervention, and active template');
END;

CREATE TRIGGER generation_session_status_guard
BEFORE UPDATE OF status ON generation_sessions
WHEN NEW.status <> OLD.status
BEGIN
    SELECT CASE
        WHEN NOT (
            (OLD.status = 'building' AND NEW.status = 'complete')
            OR
            (OLD.status = 'complete' AND NEW.status = 'deleting')
        )
        THEN RAISE(ABORT, 'invalid generation session status transition')
    END;

    SELECT CASE
        WHEN OLD.status = 'building'
         AND NEW.status = 'complete'
         AND (
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id) <> 2
             OR
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id
                AND document_type = 'epicrisis_recommendations') <> 1
             OR
             (SELECT COUNT(*)
              FROM generated_documents
              WHERE session_id = NEW.id
                AND document_type = 'operative_protocol') <> 1
         )
        THEN RAISE(ABORT, 'a completed session must contain both documents')
    END;
END;

CREATE TRIGGER generation_session_identity_no_update
BEFORE UPDATE OF
    diagnosis_id, intervention_id, template_set_id,
    diagnosis_label_snapshot, intervention_label_snapshot, created_at
ON generation_sessions
BEGIN
    SELECT RAISE(ABORT, 'generation session identity is immutable');
END;

CREATE TRIGGER generation_session_delete_guard
BEFORE DELETE ON generation_sessions
WHEN OLD.status <> 'deleting'
BEGIN
    SELECT RAISE(ABORT, 'session must enter deleting state before removal');
END;

CREATE TRIGGER generated_document_insert_guard
BEFORE INSERT ON generated_documents
WHEN COALESCE(
    (SELECT status
     FROM generation_sessions
     WHERE id = NEW.session_id),
    ''
) <> 'building'
  OR (
      NEW.independent_template_version_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM generation_sessions gs
          JOIN independent_template_versions itv
              ON itv.id = NEW.independent_template_version_id
          WHERE gs.id = NEW.session_id
            AND gs.status = 'building'
            AND gs.template_set_id IS NULL
            AND itv.document_type = NEW.document_type
            AND itv.status = 'active'
            AND itv.validation_status = 'valid'
            AND itv.patient_identification_reviewed = 1
            AND (
                (
                    NEW.document_type = 'epicrisis_recommendations'
                    AND itv.owner_type = 'diagnosis'
                    AND itv.diagnosis_id = gs.diagnosis_id
                )
                OR
                (
                    NEW.document_type = 'operative_protocol'
                    AND itv.owner_type = 'intervention'
                    AND itv.intervention_id = gs.intervention_id
                )
            )
      )
  )
  OR (
      NEW.template_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM generation_sessions gs
          WHERE gs.id = NEW.session_id
            AND gs.status = 'building'
            AND gs.template_set_id IS NOT NULL
            AND gs.template_set_id = NEW.template_set_id
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'documents can be added only while building a matching active session');
END;

CREATE TRIGGER generated_document_identity_no_update
BEFORE UPDATE OF session_id, template_set_id, document_type, template_id, independent_template_version_id
ON generated_documents
BEGIN
    SELECT RAISE(ABORT, 'generated document identity is immutable');
END;

CREATE TRIGGER generated_document_content_update_guard
BEFORE UPDATE OF rich_text_json, edited_at ON generated_documents
WHEN COALESCE(
    (SELECT status
     FROM generation_sessions
     WHERE id = OLD.session_id),
    ''
) <> 'complete'
BEGIN
    SELECT RAISE(ABORT, 'only completed session documents can be edited');
END;

CREATE TRIGGER generated_document_delete_guard
BEFORE DELETE ON generated_documents
WHEN EXISTS (
    SELECT 1
    FROM generation_sessions gs
    WHERE gs.id = OLD.session_id
      AND gs.status NOT IN ('building', 'deleting')
)
BEGIN
    SELECT RAISE(ABORT, 'documents in a completed session cannot be deleted individually');
END;
"""


SCHEMA_V4_SQL = r"""
CREATE TABLE IF NOT EXISTS template_sync_entities (
    entity_type     TEXT NOT NULL
                    CHECK (entity_type IN ('diagnosis', 'intervention')),
    catalog_key     TEXT NOT NULL
                    CHECK (
                        length(trim(catalog_key)) > 0
                        AND catalog_key GLOB '[a-z0-9]*'
                        AND catalog_key NOT GLOB '*[^a-z0-9_-]*'
                    ),
    diagnosis_id    INTEGER,
    intervention_id INTEGER,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (entity_type, catalog_key),
    UNIQUE (diagnosis_id),
    UNIQUE (intervention_id),

    CHECK (
        (entity_type = 'diagnosis' AND diagnosis_id IS NOT NULL AND intervention_id IS NULL)
        OR
        (entity_type = 'intervention' AND diagnosis_id IS NULL AND intervention_id IS NOT NULL)
    ),

    FOREIGN KEY (diagnosis_id)
        REFERENCES diagnoses (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT,
    FOREIGN KEY (intervention_id)
        REFERENCES interventions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE TABLE IF NOT EXISTS template_sync_sources (
    template_version_id INTEGER PRIMARY KEY,
    catalog_key         TEXT NOT NULL
                        CHECK (
                            length(trim(catalog_key)) > 0
                            AND catalog_key GLOB '[a-z0-9]*'
                            AND catalog_key NOT GLOB '*[^a-z0-9_-]*'
                        ),
    relative_path       TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    synced_at           TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (template_version_id)
        REFERENCES independent_template_versions (id)
        ON DELETE RESTRICT
        ON UPDATE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_template_sync_sources_key_time
    ON template_sync_sources (catalog_key, synced_at DESC);

CREATE TABLE IF NOT EXISTS template_sync_runs (
    id                 INTEGER PRIMARY KEY,
    folder_path        TEXT NOT NULL CHECK (length(trim(folder_path)) > 0),
    status             TEXT NOT NULL
                       CHECK (status IN ('no_changes', 'applied', 'cancelled', 'failed')),
    scanned_count      INTEGER NOT NULL DEFAULT 0 CHECK (scanned_count >= 0),
    new_count          INTEGER NOT NULL DEFAULT 0 CHECK (new_count >= 0),
    modified_count     INTEGER NOT NULL DEFAULT 0 CHECK (modified_count >= 0),
    unchanged_count    INTEGER NOT NULL DEFAULT 0 CHECK (unchanged_count >= 0),
    imported_count     INTEGER NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
    activated_count    INTEGER NOT NULL DEFAULT 0 CHECK (activated_count >= 0),
    backup_path        TEXT CHECK (backup_path IS NULL OR length(trim(backup_path)) > 0),
    summary_message    TEXT NOT NULL CHECK (length(trim(summary_message)) > 0),
    created_at         TEXT NOT NULL
                       DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_template_sync_runs_latest
    ON template_sync_runs (created_at DESC, id DESC);
"""


MIGRATIONS: dict[int, tuple[int, str]] = {
    0: (1, SCHEMA_V1_SQL),
    1: (2, SCHEMA_V2_SQL),
    2: (3, SCHEMA_V3_SQL),
    3: (4, SCHEMA_V4_SQL),
}

REQUIRED_TABLES = {
    "diagnoses",
    "interventions",
    "field_definitions",
    "field_options",
    "template_sets",
    "templates",
    "template_variables",
    "generation_sessions",
    "generated_documents",
    "app_settings",
}

REQUIRED_INDEXES = {
    "ux_field_definitions_one_current": (
        "field_definitions",
        ("variable_key",),
        True,
        True,
    ),
    "ux_template_sets_one_active_per_intervention": (
        "template_sets",
        ("intervention_id",),
        True,
        True,
    ),
    "ix_generation_sessions_history": (
        "generation_sessions",
        ("status", "created_at", "id"),
        False,
        False,
    ),
}

# UNIQUE și cheile primare compuse create indexuri SQLite automate. Verificăm
# semnăturile lor, astfel încât o bază cu tabele reconstruite incomplet să nu
# fie acceptată doar pentru că numele tabelelor există.
REQUIRED_UNIQUE_INDEXES = {
    "diagnoses": {
        (("code",), False),
        (("name_key",), False),
    },
    "interventions": {
        (("diagnosis_id", "name_key"), False),
        (("diagnosis_id", "code"), False),
        (("id", "diagnosis_id"), False),
    },
    "field_definitions": {
        (("variable_key", "version"), False),
        (("variable_key", "id"), False),
        (("variable_key",), True),
    },
    "field_options": {
        (("field_definition_id", "option_value"), False),
    },
    "template_sets": {
        (("intervention_id", "version_no"), False),
        (("id", "intervention_id"), False),
        (("intervention_id",), True),
    },
    "templates": {
        (("template_set_id", "document_type"), False),
        (("id", "template_set_id", "document_type"), False),
    },
    "template_variables": {
        (("template_id", "variable_key"), False),
        (("template_id", "field_definition_id"), False),
    },
    "generation_sessions": {
        (("id", "template_set_id"), False),
    },
    "generated_documents": {
        (("session_id", "document_type"), False),
    },
    "app_settings": {
        (("setting_key",), False),
    },
}

REQUIRED_TRIGGERS = {
    "field_definition_version_guard",
    "field_definition_content_no_update",
    "field_definition_no_delete",
    "field_options_select_only_insert",
    "field_options_no_update",
    "field_options_no_delete",
    "field_options_no_insert_after_publication",
    "template_set_initial_status_guard",
    "template_set_status_guard",
    "template_set_timestamp_guard",
    "template_set_metadata_guard",
    "template_set_no_delete",
    "template_insert_only_into_draft",
    "template_initial_validation_guard",
    "template_content_no_update",
    "template_validation_no_update_after_publication",
    "template_no_delete_after_publication",
    "template_variable_scope_guard_insert",
    "template_variable_scope_guard_update",
    "template_variable_no_update_after_publication",
    "template_variable_no_delete_after_publication",
    "generation_session_insert_guard",
    "generation_session_status_guard",
    "generation_session_identity_no_update",
    "generation_session_delete_guard",
    "generated_document_insert_guard",
    "generated_document_identity_no_update",
    "generated_document_content_update_guard",
    "generated_document_delete_guard",
    "app_settings_updated_at",
}

REQUIRED_V2_TABLES = {"independent_template_versions"}

REQUIRED_V2_INDEXES = {
    "ux_independent_templates_diagnosis_version": (
        "independent_template_versions",
        ("diagnosis_id", "version_no"),
        True,
        True,
    ),
    "ux_independent_templates_intervention_version": (
        "independent_template_versions",
        ("intervention_id", "version_no"),
        True,
        True,
    ),
    "ux_independent_templates_one_active_diagnosis": (
        "independent_template_versions",
        ("diagnosis_id",),
        True,
        True,
    ),
    "ux_independent_templates_one_active_intervention": (
        "independent_template_versions",
        ("intervention_id",),
        True,
        True,
    ),
}

REQUIRED_V2_UNIQUE_INDEXES = {
    "independent_template_versions": {
        (("legacy_template_id",), False),
        (("diagnosis_id", "version_no"), True),
        (("intervention_id", "version_no"), True),
        (("diagnosis_id",), True),
        (("intervention_id",), True),
    },
}

REQUIRED_V2_TRIGGERS = {
    "independent_template_initial_status_guard",
    "independent_template_legacy_source_guard",
    "independent_template_content_immutable",
    "independent_template_draft_metadata_guard",
    "independent_template_status_guard",
    "independent_template_timestamp_guard",
    "independent_template_no_delete",
}

REQUIRED_V3_INDEXES = {
    "ux_independent_templates_id_doctype": (
        "independent_template_versions",
        ("id", "document_type"),
        True,
        False,
    ),
    "ix_generated_documents_independent_template": (
        "generated_documents",
        ("independent_template_version_id",),
        False,
        False,
    ),
}

REQUIRED_V3_UNIQUE_INDEXES = {
    "independent_template_versions": {
        (("id", "document_type"), False),
    },
}

REQUIRED_V4_TABLES = {
    "template_sync_entities",
    "template_sync_sources",
    "template_sync_runs",
}

REQUIRED_V4_INDEXES = {
    "ix_template_sync_sources_key_time": (
        "template_sync_sources",
        ("catalog_key", "synced_at"),
        False,
        False,
    ),
    "ix_template_sync_runs_latest": (
        "template_sync_runs",
        ("created_at", "id"),
        False,
        False,
    ),
}

REQUIRED_V4_UNIQUE_INDEXES = {
    "template_sync_entities": {
        (("entity_type", "catalog_key"), False),
        (("diagnosis_id",), False),
        (("intervention_id",), False),
    },
}


def _read_user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _has_preexisting_user_schema(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'view', 'index', 'trigger')
          AND name NOT LIKE 'sqlite_%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _verify_schema_objects(connection: sqlite3.Connection) -> None:
    schema_version = _read_user_version(connection)
    required_tables = set(REQUIRED_TABLES)
    required_indexes = dict(REQUIRED_INDEXES)
    required_unique_indexes = {
        table: set(signatures)
        for table, signatures in REQUIRED_UNIQUE_INDEXES.items()
    }
    required_triggers = set(REQUIRED_TRIGGERS)
    if schema_version >= 2:
        required_tables.update(REQUIRED_V2_TABLES)
        required_indexes.update(REQUIRED_V2_INDEXES)
        for table, signatures in REQUIRED_V2_UNIQUE_INDEXES.items():
            required_unique_indexes.setdefault(table, set()).update(signatures)
        required_triggers.update(REQUIRED_V2_TRIGGERS)
    if schema_version >= 3:
        required_indexes.update(REQUIRED_V3_INDEXES)
        for table, signatures in REQUIRED_V3_UNIQUE_INDEXES.items():
            required_unique_indexes.setdefault(table, set()).update(signatures)
    if schema_version >= 4:
        required_tables.update(REQUIRED_V4_TABLES)
        required_indexes.update(REQUIRED_V4_INDEXES)
        for table, signatures in REQUIRED_V4_UNIQUE_INDEXES.items():
            required_unique_indexes.setdefault(table, set()).update(signatures)

    found = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = sorted(required_tables - found)
    if missing:
        raise MigrationIntegrityError(
            "Schema bazei de date este incompletă; lipsesc tabele: "
            + ", ".join(missing)
        )

    trigger_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    missing_triggers = sorted(required_triggers - trigger_names)
    if missing_triggers:
        raise MigrationIntegrityError(
            "Schema bazei de date este incompletă; lipsesc triggere: "
            + ", ".join(missing_triggers)
        )

    for index_name, expected in required_indexes.items():
        table_name, expected_columns, expected_unique, expected_partial = expected
        actual = None
        for index_row in connection.execute(f'PRAGMA index_list("{table_name}")'):
            if index_row[1] == index_name:
                columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    )
                )
                actual = (
                    columns,
                    bool(index_row[2]),
                    bool(index_row[4]),
                )
                break
        expected_signature = (
            expected_columns,
            expected_unique,
            expected_partial,
        )
        if actual != expected_signature:
            raise MigrationIntegrityError(
                f"Schema bazei de date este incompletă; indexul {index_name} "
                "lipsește sau are o definiție incorectă."
            )

    for table_name, required_signatures in required_unique_indexes.items():
        actual_signatures = set()
        for index_row in connection.execute(f'PRAGMA index_list("{table_name}")'):
            if index_row[2]:
                index_name = index_row[1].replace('"', '""')
                columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    )
                )
                actual_signatures.add((columns, bool(index_row[4])))
        missing_signatures = required_signatures - actual_signatures
        if missing_signatures:
            formatted = sorted(
                ", ".join(columns) + (" [partial]" if partial else "")
                for columns, partial in missing_signatures
            )
            raise MigrationIntegrityError(
                f"Schema bazei de date este incompletă; lipsesc constrângeri "
                f"UNIQUE în {table_name}: {formatted!r}."
            )


def _verify_integrity(connection: sqlite3.Connection) -> None:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        details = [tuple(row) for row in foreign_key_errors]
        raise MigrationIntegrityError(
            f"PRAGMA foreign_key_check a raportat probleme: {details!r}"
        )

    integrity_results = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity_results != ["ok"]:
        raise MigrationIntegrityError(
            f"PRAGMA integrity_check a eșuat: {integrity_results!r}"
        )


def _main_database_path(connection: sqlite3.Connection) -> Path | None:
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return None
    return Path(row[2])


def create_database_backup(
    connection: sqlite3.Connection,
    source_path: str | Path | None = None,
    *,
    backup_directory: str | Path | None = None,
    file_prefix: str | None = None,
) -> Path:
    """Creează o copie consistentă folosind sqlite3.Connection.backup()."""

    if connection.in_transaction:
        raise MigrationBackupError(
            "Backup-ul trebuie creat înainte de deschiderea tranzacției de migrare."
        )

    original = Path(source_path) if source_path is not None else _main_database_path(connection)
    if original is None:
        raise MigrationBackupError(
            "Nu se poate crea backup pentru o bază SQLite fără fișier."
        )

    target_dir = Path(backup_directory) if backup_directory is not None else original.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    prefix = file_prefix if file_prefix is not None else f"{original.name}-backup"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    extension = ".db" if backup_directory is not None else ""
    backup_path = target_dir / f"{prefix}-{timestamp}{extension}"
    while backup_path.exists():
        backup_path = target_dir / f"{prefix}-{timestamp}-{uuid_token()}{extension}"

    destination: sqlite3.Connection | None = None
    try:
        destination = connect_database(backup_path)
        connection.backup(destination)
        _verify_integrity(destination)
        return backup_path
    except Exception as exc:
        if destination is not None:
            destination.close()
            destination = None
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, MigrationBackupError):
            raise
        raise MigrationBackupError(f"Nu s-a putut crea backup-ul SQLite: {exc}") from exc
    finally:
        if destination is not None:
            destination.close()


def uuid_token() -> str:
    """Token scurt pentru evitarea coliziunii numelor de backup."""

    import uuid

    return uuid.uuid4().hex[:8]


def _apply_migration(
    connection: sqlite3.Connection,
    target_version: int,
    migration_sql: str,
) -> None:
    if connection.in_transaction:
        raise MigrationError("Migrarea nu poate începe într-o tranzacție existentă.")

    verified_legacy_hashes: dict[int, str] = {}
    hash_mismatch: tuple[int, str] | None = None
    hash_functions_registered = False

    def check_legacy_sha256(
        template_id: int,
        document_type: str,
        source_docx: bytes,
        stored_sha256: str,
    ) -> int:
        nonlocal hash_mismatch
        normalized_id = int(template_id)
        if not isinstance(source_docx, (bytes, bytearray, memoryview)):
            hash_mismatch = (normalized_id, str(document_type))
            raise ValueError("DOCX-ul sursă nu este stocat ca BLOB.")

        calculated_sha256 = hashlib.sha256(bytes(source_docx)).hexdigest()
        if not isinstance(stored_sha256, str) or calculated_sha256 != stored_sha256:
            hash_mismatch = (normalized_id, str(document_type))
            raise ValueError("SHA-256 stocat nu corespunde bytes-ilor DOCX.")

        verified_legacy_hashes[normalized_id] = calculated_sha256
        return 1

    def get_verified_legacy_sha256(template_id: int) -> str:
        try:
            return verified_legacy_hashes[int(template_id)]
        except KeyError as exc:
            raise ValueError(
                f"Șablonul legacy {template_id} nu a trecut verificarea SHA-256."
            ) from exc

    try:
        # Preflight-ul și copierea rulează sub același lock/tranzacție. Astfel
        # hash-urile sunt verificate din BLOB-urile exacte care vor fi migrate.
        if target_version == 2:
            connection.create_function(
                "migration_check_legacy_sha256", 4, check_legacy_sha256
            )
            connection.create_function(
                "migration_verified_legacy_sha256", 1, get_verified_legacy_sha256
            )
            hash_functions_registered = True
            migration_sql = _V2_LEGACY_HASH_PREFLIGHT_SQL + migration_sql

        connection.executescript("BEGIN IMMEDIATE;\n" + migration_sql)
        if not connection.in_transaction:
            raise MigrationError("Tranzacția de migrare nu a rămas deschisă.")

        connection.execute(f"PRAGMA user_version = {target_version}")
        _verify_schema_objects(connection)
        _verify_integrity(connection)
        connection.execute("COMMIT")
    except BaseException as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if hash_mismatch is not None:
            template_id, document_type = hash_mismatch
            raise MigrationError(
                "Migrarea a fost anulată: SHA-256-ul DOCX nu corespunde pentru "
                f"documentul legacy {template_id} ({document_type})."
            ) from exc
        if isinstance(exc, MigrationError):
            raise
        if isinstance(exc, sqlite3.Error):
            raise MigrationError(f"Migrarea la versiunea {target_version} a eșuat: {exc}") from exc
        raise
    finally:
        if hash_functions_registered:
            connection.create_function("migration_check_legacy_sha256", 4, None)
            connection.create_function("migration_verified_legacy_sha256", 1, None)


def migrate_connection(
    connection: sqlite3.Connection,
    *,
    database_path: str | Path | None = None,
) -> int:
    """Aduce conexiunea la ultima schemă suportată și îi verifică integritatea."""

    if connection.in_transaction:
        raise MigrationError("Migrarea trebuie pornită în afara unei tranzacții.")

    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys is None or foreign_keys[0] != 1:
        raise MigrationError("Conexiunea trebuie să aibă foreign_keys activat.")

    version = _read_user_version(connection)
    if version > LATEST_SCHEMA_VERSION:
        raise UnsupportedDatabaseVersionError(
            f"Baza are versiunea {version}; această aplicație suportă până la "
            f"{LATEST_SCHEMA_VERSION}."
        )

    if version == 0 and _has_preexisting_user_schema(connection):
        raise UnversionedDatabaseError(
            "Baza are tabele sau alte obiecte SQL, dar user_version este 0. "
            "Migrarea a fost oprită pentru a nu modifica o bază necunoscută."
        )

    backup_created = False
    while version < LATEST_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise MigrationError(f"Nu există migrare definită din versiunea {version}.")
        target_version, migration_sql = migration
        if target_version <= version or target_version > LATEST_SCHEMA_VERSION:
            raise MigrationError(
                f"Definiție invalidă de migrare: {version} -> {target_version}."
            )

        if version > 0 and not backup_created:
            create_database_backup(connection, database_path)
            backup_created = True

        _apply_migration(connection, target_version, migration_sql)
        version = _read_user_version(connection)
        if version != target_version:
            raise MigrationError(
                f"Migrarea a declarat versiunea {target_version}, "
                f"dar baza raportează {version}."
            )

    _verify_schema_objects(connection)
    _verify_integrity(connection)
    return version


def initialize_database(
    database_path: str | Path | None = None,
) -> Path:
    """Inițializează sau migrează explicit baza de date indicată."""

    path = Path(database_path) if database_path is not None else get_database_path()
    connection = connect_database(path)
    try:
        migrate_connection(connection, database_path=path)
    finally:
        connection.close()
    return path
