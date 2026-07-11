CREATE TABLE IF NOT EXISTS core_permission (
    actor        text NOT NULL,
    command_type text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(actor, command_type)
);

CREATE TABLE IF NOT EXISTS core_command_envelope (
    command_id                uuid PRIMARY KEY,
    command_type              text NOT NULL CHECK(command_type IN ('create_state', 'transition_state')),
    actor                     text NOT NULL,
    object_kind               text NOT NULL CHECK(object_kind IN ('production_task', 'topic', 'claim', 'experiment', 'tactic')),
    object_id                 uuid,
    idempotency_key           text NOT NULL,
    expected_basis_version_id uuid,
    confirmed                 boolean NOT NULL DEFAULT false,
    payload_json              jsonb NOT NULL,
    correlation_id            uuid NOT NULL,
    causation_id              uuid,
    command_receipt_id        uuid NOT NULL REFERENCES command_receipt(receipt_id) ON DELETE RESTRICT,
    status                    text NOT NULL CHECK(status IN ('succeeded', 'failed', 'rejected')),
    created_at                timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(expected_basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS production_task_state (
    production_task_id uuid PRIMARY KEY REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    state              text NOT NULL CHECK(state IN ('draft', 'queued', 'running', 'completed', 'cancelled')),
    state_rank         integer NOT NULL,
    basis_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    row_revision       bigint NOT NULL DEFAULT 0,
    updated_by_command_id uuid NOT NULL REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS topic_state (
    topic_id           uuid PRIMARY KEY REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    state              text NOT NULL CHECK(state IN ('candidate', 'selected', 'researching', 'planned', 'approved', 'cancelled')),
    state_rank         integer NOT NULL,
    basis_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    row_revision       bigint NOT NULL DEFAULT 0,
    updated_by_command_id uuid NOT NULL REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_state (
    claim_id           uuid PRIMARY KEY REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    state              text NOT NULL CHECK(state IN ('proposed', 'checking', 'verified', 'rejected')),
    state_rank         integer NOT NULL,
    basis_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    row_revision       bigint NOT NULL DEFAULT 0,
    updated_by_command_id uuid NOT NULL REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experiment_state (
    experiment_id      uuid PRIMARY KEY REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    state              text NOT NULL CHECK(state IN ('planned', 'active', 'completed', 'cancelled')),
    state_rank         integer NOT NULL,
    basis_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    row_revision       bigint NOT NULL DEFAULT 0,
    updated_by_command_id uuid NOT NULL REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tactic_state (
    tactic_id          uuid PRIMARY KEY REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    state              text NOT NULL CHECK(state IN ('candidate', 'active', 'watch', 'paused', 'deprecated')),
    state_rank         integer NOT NULL,
    basis_version_id   uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    row_revision       bigint NOT NULL DEFAULT 0,
    updated_by_command_id uuid NOT NULL REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_goal02_immutable_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_backward_state() RETURNS trigger AS $$
BEGIN
    IF NEW.state_rank < OLD.state_rank THEN
        RAISE EXCEPTION '% cannot move backward', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS immutable_core_command_envelope_update ON core_command_envelope;
CREATE TRIGGER immutable_core_command_envelope_update
BEFORE UPDATE OR DELETE ON core_command_envelope
FOR EACH ROW EXECUTE FUNCTION reject_goal02_immutable_update();

DROP TRIGGER IF EXISTS production_task_state_one_way ON production_task_state;
CREATE TRIGGER production_task_state_one_way
BEFORE UPDATE ON production_task_state
FOR EACH ROW EXECUTE FUNCTION reject_backward_state();

DROP TRIGGER IF EXISTS topic_state_one_way ON topic_state;
CREATE TRIGGER topic_state_one_way
BEFORE UPDATE ON topic_state
FOR EACH ROW EXECUTE FUNCTION reject_backward_state();

DROP TRIGGER IF EXISTS claim_state_one_way ON claim_state;
CREATE TRIGGER claim_state_one_way
BEFORE UPDATE ON claim_state
FOR EACH ROW EXECUTE FUNCTION reject_backward_state();

DROP TRIGGER IF EXISTS experiment_state_one_way ON experiment_state;
CREATE TRIGGER experiment_state_one_way
BEFORE UPDATE ON experiment_state
FOR EACH ROW EXECUTE FUNCTION reject_backward_state();

-- tactic_state 不用通用的"只能递增" reject_backward_state():active/watch/paused
-- 原设计是互相可以来回流转的,只有 candidate 和 deprecated 才是真正的单向边界
-- (2026-07-13, BR-EXPERIENCE-004, 原文档"7 推荐状态状态机"; sqlite 版本
-- goal02_schema.sqlite.sql 有更详细的说明)。
CREATE OR REPLACE FUNCTION reject_tactic_state_reentry() RETURNS trigger AS $$
BEGIN
    IF OLD.state = 'deprecated' OR NEW.state = 'candidate' THEN
        RAISE EXCEPTION 'tactic_state: deprecated is terminal and candidate cannot be re-entered';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tactic_state_one_way ON tactic_state;
DROP TRIGGER IF EXISTS tactic_state_terminal_and_no_candidate_reentry ON tactic_state;
CREATE TRIGGER tactic_state_terminal_and_no_candidate_reentry
BEFORE UPDATE ON tactic_state
FOR EACH ROW EXECUTE FUNCTION reject_tactic_state_reentry();
