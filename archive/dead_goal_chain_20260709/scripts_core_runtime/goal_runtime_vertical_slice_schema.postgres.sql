CREATE TABLE IF NOT EXISTS runtime_probe_skill_run (
    skill_run_id                  uuid PRIMARY KEY,
    job_id                        uuid NOT NULL REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    attempt_id                    uuid NOT NULL REFERENCES scheduler_job_attempt(attempt_id) ON DELETE RESTRICT,
    status                        text NOT NULL CHECK(status IN ('succeeded', 'failed')),
    request_id                    text NOT NULL,
    correlation_id                uuid NOT NULL,
    skill_name                    text NOT NULL,
    skill_version                 text NOT NULL,
    skill_hash                    text NOT NULL,
    binding_name                  text NOT NULL,
    binding_version               text NOT NULL,
    binding_hash                  text NOT NULL,
    model_route                   text NOT NULL,
    model_port                    text NOT NULL,
    input_hash                    text NOT NULL,
    output_hash                   text,
    model_run_envelope_version_id uuid REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    error_json                    jsonb,
    created_at                    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_probe_result_index (
    job_id            uuid PRIMARY KEY REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    request_id        text NOT NULL,
    correlation_id    uuid NOT NULL,
    result_root_id    uuid NOT NULL REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    result_version_id uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    skill_run_id      uuid NOT NULL REFERENCES runtime_probe_skill_run(skill_run_id) ON DELETE RESTRICT,
    skill_version     text NOT NULL,
    input_hash        text NOT NULL,
    output_hash       text NOT NULL,
    schema_version    text NOT NULL,
    model_route       text NOT NULL,
    model_port        text NOT NULL,
    completed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE(request_id, correlation_id)
);

CREATE OR REPLACE FUNCTION reject_runtime_probe_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS immutable_runtime_probe_skill_run_update ON runtime_probe_skill_run;
CREATE TRIGGER immutable_runtime_probe_skill_run_update
BEFORE UPDATE OR DELETE ON runtime_probe_skill_run
FOR EACH ROW EXECUTE FUNCTION reject_runtime_probe_mutation();

DROP TRIGGER IF EXISTS immutable_runtime_probe_result_index_update ON runtime_probe_result_index;
CREATE TRIGGER immutable_runtime_probe_result_index_update
BEFORE UPDATE OR DELETE ON runtime_probe_result_index
FOR EACH ROW EXECUTE FUNCTION reject_runtime_probe_mutation();
