PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runtime_probe_skill_run (
    skill_run_id                 TEXT PRIMARY KEY,
    job_id                       TEXT NOT NULL REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    attempt_id                   TEXT NOT NULL REFERENCES scheduler_job_attempt(attempt_id) ON DELETE RESTRICT,
    status                       TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
    request_id                   TEXT NOT NULL,
    correlation_id               TEXT NOT NULL,
    skill_name                   TEXT NOT NULL,
    skill_version                TEXT NOT NULL,
    skill_hash                   TEXT NOT NULL,
    binding_name                 TEXT NOT NULL,
    binding_version              TEXT NOT NULL,
    binding_hash                 TEXT NOT NULL,
    model_route                  TEXT NOT NULL,
    model_port                   TEXT NOT NULL,
    input_hash                   TEXT NOT NULL,
    output_hash                  TEXT,
    model_run_envelope_version_id TEXT,
    error_json                   TEXT,
    created_at                   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(model_run_envelope_version_id) REFERENCES trace_version(version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS runtime_probe_result_index (
    job_id          TEXT PRIMARY KEY REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    request_id      TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    result_root_id  TEXT NOT NULL REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    result_version_id TEXT NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    skill_run_id    TEXT NOT NULL REFERENCES runtime_probe_skill_run(skill_run_id) ON DELETE RESTRICT,
    skill_version   TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    output_hash     TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    model_route     TEXT NOT NULL,
    model_port      TEXT NOT NULL,
    completed_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(request_id, correlation_id)
);

CREATE TRIGGER IF NOT EXISTS immutable_runtime_probe_skill_run_update
BEFORE UPDATE ON runtime_probe_skill_run
BEGIN
    SELECT RAISE(ABORT, 'runtime_probe_skill_run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_runtime_probe_skill_run_delete
BEFORE DELETE ON runtime_probe_skill_run
BEGIN
    SELECT RAISE(ABORT, 'runtime_probe_skill_run is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_runtime_probe_result_index_update
BEFORE UPDATE ON runtime_probe_result_index
BEGIN
    SELECT RAISE(ABORT, 'runtime_probe_result_index is immutable');
END;

CREATE TRIGGER IF NOT EXISTS immutable_runtime_probe_result_index_delete
BEFORE DELETE ON runtime_probe_result_index
BEGIN
    SELECT RAISE(ABORT, 'runtime_probe_result_index is immutable');
END;
