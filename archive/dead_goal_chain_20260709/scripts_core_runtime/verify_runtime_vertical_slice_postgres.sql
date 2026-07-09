\echo GOAL-RUNTIME-VERTICAL-SLICE-01 PostgreSQL verification started
BEGIN;

INSERT INTO command_receipt(
    receipt_id, command_scope, idempotency_key, request_hash, result_json,
    correlation_id, status
) VALUES(
    '018f0000-0000-7000-8000-000000000001',
    'runtime_probe.postgres.verify',
    'runtime-probe-postgres-schema',
    'request-hash',
    '{"ok": true}'::jsonb,
    '018f0000-0000-7000-8000-000000000002',
    'succeeded'
);

INSERT INTO scheduler_job(
    job_id, job_kind, status, priority, run_after, max_attempts, payload_json,
    correlation_id, created_by_receipt_id
) VALUES(
    '018f0000-0000-7000-8000-000000000003',
    'runtime_probe.execute',
    'queued',
    0,
    now(),
    3,
    '{"request_id":"pg-probe","correlation_id":"018f0000-0000-7000-8000-000000000002","text":"hello","requested_operation":"normalize","metadata":{}}'::jsonb,
    '018f0000-0000-7000-8000-000000000002',
    '018f0000-0000-7000-8000-000000000001'
);

INSERT INTO scheduler_job_attempt(
    attempt_id, job_id, attempt_no, worker_id, status, started_at,
    heartbeat_at, lease_expires_at, correlation_id
) VALUES(
    '018f0000-0000-7000-8000-000000000004',
    '018f0000-0000-7000-8000-000000000003',
    1,
    'pg-worker',
    'leased',
    now(),
    now(),
    now() + interval '60 seconds',
    '018f0000-0000-7000-8000-000000000002'
);

INSERT INTO trace_root(root_id, object_kind)
VALUES('018f0000-0000-7000-8000-000000000005', 'runtime_probe_result');

INSERT INTO trace_version(
    version_id, root_id, version_no, content_hash, projection_version, payload_json
) VALUES(
    '018f0000-0000-7000-8000-000000000006',
    '018f0000-0000-7000-8000-000000000005',
    1,
    'content-hash',
    'runtime_probe.output.v1',
    '{"output":{"result_code":"ok"}}'::jsonb
);

UPDATE trace_root
   SET current_version_id='018f0000-0000-7000-8000-000000000006'
 WHERE root_id='018f0000-0000-7000-8000-000000000005';

INSERT INTO runtime_probe_skill_run(
    skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
    skill_name, skill_version, skill_hash, binding_name, binding_version,
    binding_hash, model_route, model_port, input_hash, output_hash
) VALUES(
    '018f0000-0000-7000-8000-000000000007',
    '018f0000-0000-7000-8000-000000000003',
    '018f0000-0000-7000-8000-000000000004',
    'succeeded',
    'pg-probe',
    '018f0000-0000-7000-8000-000000000002',
    'runtime_probe',
    '0.1.0',
    'skill-hash',
    'runtime_probe_public_input',
    '0.1.0',
    'binding-hash',
    'runtime_probe.test',
    'runtime_probe_test_port',
    'input-hash',
    'output-hash'
);

INSERT INTO runtime_probe_result_index(
    job_id, request_id, correlation_id, result_root_id, result_version_id,
    skill_run_id, skill_version, input_hash, output_hash, schema_version,
    model_route, model_port
) VALUES(
    '018f0000-0000-7000-8000-000000000003',
    'pg-probe',
    '018f0000-0000-7000-8000-000000000002',
    '018f0000-0000-7000-8000-000000000005',
    '018f0000-0000-7000-8000-000000000006',
    '018f0000-0000-7000-8000-000000000007',
    '0.1.0',
    'input-hash',
    'output-hash',
    'runtime_probe.output.v1',
    'runtime_probe.test',
    'runtime_probe_test_port'
);

DO $$
BEGIN
    BEGIN
        UPDATE runtime_probe_result_index
           SET output_hash='changed'
         WHERE job_id='018f0000-0000-7000-8000-000000000003';
        RAISE EXCEPTION 'runtime_probe_result_index mutation was not rejected';
    EXCEPTION
        WHEN raise_exception THEN
            RAISE;
        WHEN others THEN
            NULL;
    END;
END $$;

ROLLBACK;
\echo GOAL-RUNTIME-VERTICAL-SLICE-01 PostgreSQL verification passed
