PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS trace_root (
    root_id            TEXT PRIMARY KEY,
    object_kind        TEXT NOT NULL,
    current_version_id TEXT,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(root_id, current_version_id),
    FOREIGN KEY(root_id, current_version_id)
        REFERENCES trace_version(root_id, version_id)
);

CREATE TABLE IF NOT EXISTS trace_version (
    version_id          TEXT PRIMARY KEY,
    root_id             TEXT NOT NULL,
    version_no          INTEGER NOT NULL,
    based_on_version_id TEXT,
    blob_hash           TEXT,
    content_hash        TEXT NOT NULL,
    business_hash       TEXT,
    projection_version  TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(root_id, version_id),
    UNIQUE(root_id, version_no),
    FOREIGN KEY(root_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(based_on_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS object_reference (
    reference_id            TEXT PRIMARY KEY,
    source_version_id       TEXT NOT NULL,
    relation_role           TEXT NOT NULL,
    target_object_kind      TEXT NOT NULL,
    target_stable_id        TEXT NOT NULL,
    target_version_id       TEXT,
    target_content_hash     TEXT,
    locator_json            TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(target_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS binding_manifest (
    manifest_id       TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL,
    local_ref         TEXT NOT NULL,
    object_ref_json   TEXT NOT NULL,
    before_hash       TEXT NOT NULL,
    after_hash        TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_version_id, local_ref),
    FOREIGN KEY(source_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS command_receipt (
    receipt_id       TEXT PRIMARY KEY,
    command_scope    TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    correlation_id   TEXT NOT NULL,
    causation_id     TEXT,
    status           TEXT NOT NULL CHECK(status IN ('succeeded', 'failed', 'rejected')),
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(command_scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_event (
    audit_id       TEXT PRIMARY KEY,
    event_type     TEXT NOT NULL,
    actor          TEXT NOT NULL,
    object_kind    TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    version_id     TEXT,
    payload_json   TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id   TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS outbox_message (
    outbox_id      TEXT PRIMARY KEY,
    topic          TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sent', 'failed', 'cancelled')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT NOT NULL,
    causation_id   TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_preference_profile (
    profile_id          TEXT PRIMARY KEY,
    scope_type          TEXT NOT NULL CHECK(scope_type IN ('global', 'domain', 'account')),
    scope_id            TEXT,
    current_revision_id TEXT,
    row_revision        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope_type, scope_id),
    UNIQUE(profile_id, current_revision_id),
    FOREIGN KEY(profile_id, current_revision_id)
        REFERENCES content_preference_revision(profile_id, revision_id)
);

CREATE TABLE IF NOT EXISTS content_preference_revision (
    revision_id          TEXT PRIMARY KEY,
    profile_id           TEXT NOT NULL,
    revision_no          INTEGER NOT NULL,
    base_revision_id     TEXT,
    status               TEXT NOT NULL CHECK(status IN ('candidate', 'published', 'rejected', 'superseded')),
    preference_payload   TEXT NOT NULL,
    evidence_refs        TEXT NOT NULL,
    origin               TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at           TEXT,
    UNIQUE(profile_id, revision_id),
    UNIQUE(profile_id, revision_no),
    FOREIGN KEY(profile_id) REFERENCES content_preference_profile(profile_id) ON DELETE RESTRICT,
    FOREIGN KEY(base_revision_id) REFERENCES content_preference_revision(revision_id) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS immutable_trace_version_update
BEFORE UPDATE ON trace_version
BEGIN
    SELECT RAISE(ABORT, 'trace_version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_trace_version_delete
BEFORE DELETE ON trace_version
BEGIN
    SELECT RAISE(ABORT, 'trace_version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_reference_update
BEFORE UPDATE ON object_reference
BEGIN
    SELECT RAISE(ABORT, 'object_reference is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_reference_delete
BEFORE DELETE ON object_reference
BEGIN
    SELECT RAISE(ABORT, 'object_reference is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_binding_manifest_update
BEFORE UPDATE ON binding_manifest
BEGIN
    SELECT RAISE(ABORT, 'binding_manifest is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_binding_manifest_delete
BEFORE DELETE ON binding_manifest
BEGIN
    SELECT RAISE(ABORT, 'binding_manifest is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_audit_event_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_audit_event_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_preference_revision_update
BEFORE UPDATE ON content_preference_revision
BEGIN
    SELECT RAISE(ABORT, 'content_preference_revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_preference_revision_delete
BEFORE DELETE ON content_preference_revision
BEGIN
    SELECT RAISE(ABORT, 'content_preference_revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS content_preference_current_must_be_published
BEFORE UPDATE OF current_revision_id ON content_preference_profile
WHEN NEW.current_revision_id IS NOT NULL
BEGIN
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1
            FROM content_preference_revision r
            WHERE r.profile_id = NEW.profile_id
              AND r.revision_id = NEW.current_revision_id
              AND r.status = 'published'
        )
        THEN RAISE(ABORT, 'current preference revision must be published and from same profile')
    END;
END;
