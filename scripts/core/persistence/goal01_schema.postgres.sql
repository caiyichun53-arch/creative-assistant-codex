CREATE TABLE IF NOT EXISTS trace_root (
    root_id            uuid PRIMARY KEY,
    object_kind        text NOT NULL,
    current_version_id uuid,
    row_revision       bigint NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE(root_id, current_version_id)
);

CREATE TABLE IF NOT EXISTS trace_version (
    version_id          uuid PRIMARY KEY,
    root_id             uuid NOT NULL REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    version_no          bigint NOT NULL,
    based_on_version_id uuid REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    blob_hash           text,
    content_hash        text NOT NULL,
    business_hash       text,
    projection_version  text NOT NULL,
    payload_json        jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE(root_id, version_id),
    UNIQUE(root_id, version_no)
);

ALTER TABLE trace_root
    ADD CONSTRAINT trace_root_current_version_same_root
    FOREIGN KEY (root_id, current_version_id)
    REFERENCES trace_version(root_id, version_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS object_reference (
    reference_id        uuid PRIMARY KEY,
    source_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    relation_role       text NOT NULL,
    target_object_kind  text NOT NULL,
    target_stable_id    uuid NOT NULL,
    target_version_id   uuid REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    target_content_hash text,
    locator_json        jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS binding_manifest (
    manifest_id       uuid PRIMARY KEY,
    source_version_id uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    local_ref         text NOT NULL,
    object_ref_json   jsonb NOT NULL,
    before_hash       text NOT NULL,
    after_hash        text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_version_id, local_ref)
);

CREATE TABLE IF NOT EXISTS command_receipt (
    receipt_id      uuid PRIMARY KEY,
    command_scope   text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash    text NOT NULL,
    result_json     jsonb NOT NULL,
    correlation_id  uuid NOT NULL,
    causation_id    uuid,
    status          text NOT NULL CHECK(status IN ('succeeded', 'failed', 'rejected')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE(command_scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_event (
    audit_id       uuid PRIMARY KEY,
    event_type     text NOT NULL,
    actor          text NOT NULL,
    object_kind    text NOT NULL,
    object_id      uuid NOT NULL,
    version_id     uuid REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    payload_json   jsonb NOT NULL,
    correlation_id uuid NOT NULL,
    causation_id   uuid,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox_message (
    outbox_id      uuid PRIMARY KEY,
    topic          text NOT NULL,
    payload_json   jsonb NOT NULL,
    status         text NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'sent', 'failed', 'cancelled')),
    attempts       integer NOT NULL DEFAULT 0,
    correlation_id uuid NOT NULL,
    causation_id   uuid,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS content_preference_profile (
    profile_id          uuid PRIMARY KEY,
    scope_type          text NOT NULL CHECK(scope_type IN ('global', 'domain', 'account')),
    scope_id            uuid,
    current_revision_id uuid,
    row_revision        bigint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE(scope_type, scope_id),
    UNIQUE(profile_id, current_revision_id)
);

CREATE TABLE IF NOT EXISTS content_preference_revision (
    revision_id        uuid PRIMARY KEY,
    profile_id         uuid NOT NULL REFERENCES content_preference_profile(profile_id) ON DELETE RESTRICT,
    revision_no        bigint NOT NULL,
    base_revision_id   uuid REFERENCES content_preference_revision(revision_id) ON DELETE RESTRICT,
    status             text NOT NULL CHECK(status IN ('candidate', 'published', 'rejected', 'superseded')),
    preference_payload jsonb NOT NULL,
    evidence_refs      jsonb NOT NULL,
    origin             text NOT NULL,
    content_hash       text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    decided_at         timestamptz,
    UNIQUE(profile_id, revision_id),
    UNIQUE(profile_id, revision_no)
);

ALTER TABLE content_preference_profile
    ADD CONSTRAINT content_preference_current_revision_same_profile
    FOREIGN KEY (profile_id, current_revision_id)
    REFERENCES content_preference_revision(profile_id, revision_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE OR REPLACE FUNCTION reject_immutable_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION check_current_preference_revision() RETURNS trigger AS $$
BEGIN
    IF NEW.current_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM content_preference_revision r
        WHERE r.profile_id = NEW.profile_id
          AND r.revision_id = NEW.current_revision_id
          AND r.status = 'published'
    ) THEN
        RAISE EXCEPTION 'current preference revision must be published and from same profile';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS immutable_trace_version_update ON trace_version;
CREATE TRIGGER immutable_trace_version_update
BEFORE UPDATE OR DELETE ON trace_version
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS immutable_object_reference_update ON object_reference;
CREATE TRIGGER immutable_object_reference_update
BEFORE UPDATE OR DELETE ON object_reference
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS immutable_binding_manifest_update ON binding_manifest;
CREATE TRIGGER immutable_binding_manifest_update
BEFORE UPDATE OR DELETE ON binding_manifest
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS immutable_audit_event_update ON audit_event;
CREATE TRIGGER immutable_audit_event_update
BEFORE UPDATE OR DELETE ON audit_event
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS immutable_preference_revision_update ON content_preference_revision;
CREATE TRIGGER immutable_preference_revision_update
BEFORE UPDATE OR DELETE ON content_preference_revision
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS content_preference_current_must_be_published ON content_preference_profile;
CREATE TRIGGER content_preference_current_must_be_published
BEFORE UPDATE OF current_revision_id ON content_preference_profile
FOR EACH ROW EXECUTE FUNCTION check_current_preference_revision();
