PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS core_permission (
    actor        TEXT NOT NULL,
    command_type TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(actor, command_type)
);

CREATE TABLE IF NOT EXISTS core_command_envelope (
    command_id                TEXT PRIMARY KEY,
    command_type              TEXT NOT NULL CHECK(command_type IN ('create_state', 'transition_state')),
    actor                     TEXT NOT NULL,
    object_kind               TEXT NOT NULL CHECK(object_kind IN ('production_task', 'topic', 'claim', 'experiment', 'tactic')),
    object_id                 TEXT,
    idempotency_key           TEXT NOT NULL,
    expected_basis_version_id TEXT,
    confirmed                 INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0, 1)),
    payload_json              TEXT NOT NULL,
    correlation_id            TEXT NOT NULL,
    causation_id              TEXT,
    command_receipt_id        TEXT NOT NULL,
    status                    TEXT NOT NULL CHECK(status IN ('succeeded', 'failed', 'rejected')),
    created_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(command_receipt_id) REFERENCES command_receipt(receipt_id) ON DELETE RESTRICT,
    FOREIGN KEY(expected_basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS production_task_state (
    production_task_id TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK(state IN ('draft', 'queued', 'running', 'completed', 'cancelled')),
    state_rank         INTEGER NOT NULL,
    basis_version_id   TEXT NOT NULL,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    updated_by_command_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(production_task_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_command_id) REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS topic_state (
    topic_id           TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK(state IN ('candidate', 'selected', 'researching', 'planned', 'approved', 'cancelled')),
    state_rank         INTEGER NOT NULL,
    basis_version_id   TEXT NOT NULL,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    updated_by_command_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_command_id) REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS claim_state (
    claim_id           TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK(state IN ('proposed', 'checking', 'verified', 'rejected')),
    state_rank         INTEGER NOT NULL,
    basis_version_id   TEXT NOT NULL,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    updated_by_command_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(claim_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_command_id) REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS experiment_state (
    experiment_id      TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK(state IN ('planned', 'active', 'completed', 'cancelled')),
    state_rank         INTEGER NOT NULL,
    basis_version_id   TEXT NOT NULL,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    updated_by_command_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(experiment_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_command_id) REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS tactic_state (
    tactic_id          TEXT PRIMARY KEY,
    state              TEXT NOT NULL CHECK(state IN ('candidate', 'active', 'watch', 'paused', 'deprecated')),
    state_rank         INTEGER NOT NULL,
    basis_version_id   TEXT NOT NULL,
    row_revision       INTEGER NOT NULL DEFAULT 0,
    updated_by_command_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(tactic_id) REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    FOREIGN KEY(basis_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_command_id) REFERENCES core_command_envelope(command_id) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS immutable_core_command_envelope_update
BEFORE UPDATE ON core_command_envelope
BEGIN
    SELECT RAISE(ABORT, 'core_command_envelope is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_core_command_envelope_delete
BEFORE DELETE ON core_command_envelope
BEGIN
    SELECT RAISE(ABORT, 'core_command_envelope is immutable');
END;

CREATE TRIGGER IF NOT EXISTS production_task_state_one_way
BEFORE UPDATE ON production_task_state
WHEN NEW.state_rank < OLD.state_rank
BEGIN
    SELECT RAISE(ABORT, 'production_task_state cannot move backward');
END;

CREATE TRIGGER IF NOT EXISTS topic_state_one_way
BEFORE UPDATE ON topic_state
WHEN NEW.state_rank < OLD.state_rank
BEGIN
    SELECT RAISE(ABORT, 'topic_state cannot move backward');
END;

CREATE TRIGGER IF NOT EXISTS claim_state_one_way
BEFORE UPDATE ON claim_state
WHEN NEW.state_rank < OLD.state_rank
BEGIN
    SELECT RAISE(ABORT, 'claim_state cannot move backward');
END;

CREATE TRIGGER IF NOT EXISTS experiment_state_one_way
BEFORE UPDATE ON experiment_state
WHEN NEW.state_rank < OLD.state_rank
BEGIN
    SELECT RAISE(ABORT, 'experiment_state cannot move backward');
END;

-- tactic_state 不是单向的(2026-07-13, 置顶规则总表核对后, BR-EXPERIENCE-004,
-- 原文档"7 推荐状态状态机"):active/watch/paused 三态原设计就是互相可以来回
-- 流转的(active<->watch<->paused),只有 candidate(还没被正式纳入循环的初始态,
-- 只能离开一次不能回去)和 deprecated(唯一真正的终点,只能靠人工提案离开)
-- 这两种是真正的单向边界。上一轮(c3f8e70)把这张表跟其它四张 *_state 表
-- 共用同一个"state_rank 只能递增"触发器,是没查到这份文档就建错的约束——
-- watch<->paused/watch<->active 这些原本合法的流转全部会被那个触发器挡掉。
CREATE TRIGGER IF NOT EXISTS tactic_state_terminal_and_no_candidate_reentry
BEFORE UPDATE ON tactic_state
WHEN OLD.state = 'deprecated' OR NEW.state = 'candidate'
BEGIN
    SELECT RAISE(ABORT, 'tactic_state: deprecated is terminal and candidate cannot be re-entered');
END;
