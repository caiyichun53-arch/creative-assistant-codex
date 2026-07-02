CREATE TABLE IF NOT EXISTS formal_business_skill_run (
    skill_run_id                  uuid PRIMARY KEY,
    job_id                        uuid NOT NULL REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    attempt_id                    uuid NOT NULL REFERENCES scheduler_job_attempt(attempt_id) ON DELETE RESTRICT,
    status                        text NOT NULL CHECK(status IN ('succeeded', 'failed')),
    formal_skill_id               text NOT NULL,
    skill_version                 text NOT NULL,
    request_id                    text NOT NULL,
    correlation_id                text NOT NULL,
    skill_hash                    text NOT NULL,
    binding_name                  text NOT NULL,
    binding_version               text NOT NULL,
    binding_hash                  text NOT NULL,
    model_route                   text NOT NULL,
    model_port                    text NOT NULL,
    input_hash                    text NOT NULL,
    model_input_hash              text,
    output_hash                   text,
    model_run_envelope_version_id uuid REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    error_json                    jsonb,
    created_at                    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS formal_business_skill_result_index (
    job_id            uuid PRIMARY KEY REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    formal_skill_id   text NOT NULL,
    request_id        text NOT NULL,
    correlation_id    text NOT NULL,
    result_root_id    uuid NOT NULL REFERENCES trace_root(root_id) ON DELETE RESTRICT,
    result_version_id uuid NOT NULL REFERENCES trace_version(version_id) ON DELETE RESTRICT,
    skill_run_id      uuid NOT NULL REFERENCES formal_business_skill_run(skill_run_id) ON DELETE RESTRICT,
    skill_version     text NOT NULL,
    input_hash        text NOT NULL,
    model_input_hash  text NOT NULL,
    output_hash       text NOT NULL,
    schema_version    text NOT NULL,
    model_route       text NOT NULL,
    model_port        text NOT NULL,
    completed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE(formal_skill_id, request_id, correlation_id)
);

DROP TRIGGER IF EXISTS immutable_formal_business_skill_run_update ON formal_business_skill_run;
CREATE TRIGGER immutable_formal_business_skill_run_update
BEFORE UPDATE OR DELETE ON formal_business_skill_run
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

DROP TRIGGER IF EXISTS immutable_formal_business_skill_result_index_update ON formal_business_skill_result_index;
CREATE TRIGGER immutable_formal_business_skill_result_index_update
BEFORE UPDATE OR DELETE ON formal_business_skill_result_index
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
