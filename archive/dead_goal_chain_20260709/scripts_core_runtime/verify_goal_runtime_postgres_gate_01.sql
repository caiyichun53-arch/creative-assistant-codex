\echo GOAL-RUNTIME-POSTGRES-GATE-01 PostgreSQL verification started
BEGIN;

CREATE OR REPLACE FUNCTION pg_temp.gate_assert(ok boolean, message text) RETURNS void AS $$
BEGIN
    IF ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION '%', message;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.claim_runtime_probe(
    p_worker_id text,
    p_attempt_id uuid,
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS uuid AS $$
DECLARE
    claimed_job_id uuid;
BEGIN
    WITH picked AS (
        SELECT job_id
          FROM scheduler_job
         WHERE job_kind = 'runtime_probe.execute'
           AND status = 'queued'
           AND run_after <= p_now
         ORDER BY priority DESC, created_at, job_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    ),
    updated AS (
        UPDATE scheduler_job j
           SET status = 'leased',
               attempt_count = j.attempt_count + 1,
               current_attempt_id = p_attempt_id,
               lease_owner = p_worker_id,
               lease_expires_at = p_now + interval '60 seconds',
               updated_at = p_now
          FROM picked p
         WHERE j.job_id = p.job_id
     RETURNING j.job_id, j.attempt_count, j.correlation_id
    )
    INSERT INTO scheduler_job_attempt(
        attempt_id, job_id, attempt_no, worker_id, status, started_at,
        heartbeat_at, lease_expires_at, correlation_id
    )
    SELECT p_attempt_id, job_id, attempt_count, p_worker_id, 'leased', p_now,
           p_now, p_now + interval '60 seconds', correlation_id
      FROM updated
    RETURNING job_id INTO claimed_job_id;

    RETURN claimed_job_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.enqueue_probe_job(
    p_receipt_id uuid,
    p_job_id uuid,
    p_scope text,
    p_idempotency_key text,
    p_correlation_id uuid,
    p_payload jsonb,
    p_max_attempts integer DEFAULT 3
) RETURNS void AS $$
BEGIN
    INSERT INTO command_receipt(
        receipt_id, command_scope, idempotency_key, request_hash, result_json,
        correlation_id, status
    ) VALUES(
        p_receipt_id, p_scope, p_idempotency_key, 'request-hash',
        '{"accepted": true}'::jsonb, p_correlation_id, 'succeeded'
    );

    INSERT INTO scheduler_job(
        job_id, job_kind, status, priority, run_after, max_attempts, payload_json,
        correlation_id, created_by_receipt_id
    ) VALUES(
        p_job_id, 'runtime_probe.execute', 'queued', 0, clock_timestamp(),
        p_max_attempts, p_payload, p_correlation_id, p_receipt_id
    );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.materialize_probe_success(
    p_job_id uuid,
    p_attempt_id uuid,
    p_request_id text,
    p_correlation_id uuid,
    p_root_id uuid,
    p_version_id uuid,
    p_skill_run_id uuid,
    p_outbox_id uuid
) RETURNS void AS $$
BEGIN
    INSERT INTO trace_root(root_id, object_kind)
    VALUES(p_root_id, 'runtime_probe_result');

    INSERT INTO trace_version(
        version_id, root_id, version_no, content_hash, projection_version, payload_json
    ) VALUES(
        p_version_id, p_root_id, 1, 'runtime-output-hash',
        'runtime_probe.output.v1',
        jsonb_build_object(
            'goal', 'GOAL-RUNTIME-POSTGRES-GATE-01',
            'output', jsonb_build_object(
                'normalized_text', 'hello runtime',
                'operation', 'normalize',
                'result_code', 'ok',
                'deterministic_summary', 'normalize:13:postgres',
                'trace', jsonb_build_object(
                    'request_id', p_request_id,
                    'correlation_id', p_correlation_id,
                    'model_route', 'runtime_probe.test',
                    'model_port', 'runtime_probe_test_port'
                ),
                'schema_version', 'runtime_probe.output.v1'
            )
        )
    );

    UPDATE trace_root
       SET current_version_id = p_version_id
     WHERE root_id = p_root_id;

    INSERT INTO runtime_probe_skill_run(
        skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
        skill_name, skill_version, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, output_hash
    ) VALUES(
        p_skill_run_id, p_job_id, p_attempt_id, 'succeeded', p_request_id,
        p_correlation_id, 'runtime_probe', '0.1.0', 'skill-hash',
        'runtime_probe_public_input', '0.1.0', 'binding-hash',
        'runtime_probe.test', 'runtime_probe_test_port', 'input-hash',
        'output-hash'
    );

    INSERT INTO audit_event(
        audit_id, event_type, actor, object_kind, object_id, version_id,
        payload_json, correlation_id, causation_id
    ) VALUES(
        p_outbox_id, 'runtime_probe.result.materialized',
        'runtime_probe_materializer', 'runtime_probe_result', p_root_id,
        p_version_id, jsonb_build_object('job_id', p_job_id), p_correlation_id,
        p_attempt_id
    );

    INSERT INTO outbox_message(
        outbox_id, topic, payload_json, correlation_id, causation_id
    ) VALUES(
        p_outbox_id,
        'runtime_probe.result.materialized',
        jsonb_build_object(
            'request_id', p_request_id,
            'job_id', p_job_id,
            'result_version_id', p_version_id,
            'schema_version', 'runtime_probe.output.v1'
        ),
        p_correlation_id,
        p_skill_run_id
    );

    INSERT INTO runtime_probe_result_index(
        job_id, request_id, correlation_id, result_root_id, result_version_id,
        skill_run_id, skill_version, input_hash, output_hash, schema_version,
        model_route, model_port
    ) VALUES(
        p_job_id, p_request_id, p_correlation_id, p_root_id, p_version_id,
        p_skill_run_id, '0.1.0', 'input-hash', 'output-hash',
        'runtime_probe.output.v1', 'runtime_probe.test', 'runtime_probe_test_port'
    );
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_count integer;
    total_rows bigint := 0;
    table_row record;
BEGIN
    SELECT count(*) INTO table_count
      FROM information_schema.tables
     WHERE table_schema = 'public'
       AND table_type = 'BASE TABLE';
    PERFORM pg_temp.gate_assert(table_count = 20, 'expected exactly 20 formal public tables');

    FOR table_row IN
        SELECT quote_ident(table_schema) || '.' || quote_ident(table_name) AS table_ref
          FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_type = 'BASE TABLE'
    LOOP
        EXECUTE format('SELECT $1 + count(*) FROM %s', table_row.table_ref)
           INTO total_rows
          USING total_rows;
    END LOOP;
    PERFORM pg_temp.gate_assert(total_rows = 0, 'expected all formal tables to start empty');

    PERFORM pg_temp.gate_assert(
        EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname = 'trace_root_current_version_same_root'
        ),
        'missing trace root current-version foreign key'
    );
    PERFORM pg_temp.gate_assert(
        EXISTS (
            SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public'
               AND indexname = 'unique_outbox_topic_causation'
        ),
        'missing outbox topic/causation dedupe index'
    );
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-000000000101';
    v_attempt_id uuid := '018f0000-0000-7000-8000-000000000102';
    claimed uuid;
BEGIN
    PERFORM pg_temp.enqueue_probe_job(
        '018f0000-0000-7000-8000-000000000100',
        v_job_id,
        'runtime_probe.postgres.create',
        'runtime-probe-success',
        '018f0000-0000-7000-8000-000000000199',
        '{"request_id":"pg-success","correlation_id":"018f0000-0000-7000-8000-000000000199","text":"Hello Runtime","requested_operation":"normalize","metadata":{"case_id":"success"}}'::jsonb
    );
    claimed := pg_temp.claim_runtime_probe('worker-a', v_attempt_id);
    PERFORM pg_temp.gate_assert(claimed = v_job_id, 'worker-a did not claim success job');
    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-b', '018f0000-0000-7000-8000-000000000103') IS NULL, 'worker-b claimed already leased job');

    PERFORM pg_temp.materialize_probe_success(
        v_job_id, v_attempt_id, 'pg-success', '018f0000-0000-7000-8000-000000000199',
        '018f0000-0000-7000-8000-000000000104',
        '018f0000-0000-7000-8000-000000000105',
        '018f0000-0000-7000-8000-000000000106',
        '018f0000-0000-7000-8000-000000000107'
    );

    UPDATE scheduler_job_attempt
       SET status = 'succeeded', finished_at = clock_timestamp()
     WHERE scheduler_job_attempt.attempt_id = v_attempt_id;
    UPDATE scheduler_job
       SET status = 'succeeded', updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id;

    PERFORM pg_temp.gate_assert(
        (SELECT count(*) FROM runtime_probe_result_index WHERE runtime_probe_result_index.job_id = v_job_id) = 1,
        'success did not create exactly one result'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT count(*) FROM outbox_message WHERE topic = 'runtime_probe.result.materialized' AND causation_id = '018f0000-0000-7000-8000-000000000106') = 1,
        'success did not create exactly one outbox event'
    );
END $$;

DO $$
BEGIN
    BEGIN
        INSERT INTO command_receipt(
            receipt_id, command_scope, idempotency_key, request_hash, result_json,
            correlation_id, status
        ) VALUES(
            '018f0000-0000-7000-8000-000000000108',
            'runtime_probe.postgres.create',
            'runtime-probe-success',
            'different-request-hash',
            '{"accepted": true}'::jsonb,
            '018f0000-0000-7000-8000-000000000199',
            'succeeded'
        );
        RAISE EXCEPTION 'duplicate idempotency key was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            NULL;
    END;

    BEGIN
        INSERT INTO outbox_message(
            outbox_id, topic, payload_json, correlation_id, causation_id
        ) VALUES(
            '018f0000-0000-7000-8000-000000000109',
            'runtime_probe.result.materialized',
            '{"duplicate": true}'::jsonb,
            '018f0000-0000-7000-8000-000000000199',
            '018f0000-0000-7000-8000-000000000106'
        );
        RAISE EXCEPTION 'duplicate outbox event was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            NULL;
    END;

    BEGIN
        INSERT INTO runtime_probe_result_index(
            job_id, request_id, correlation_id, result_root_id, result_version_id,
            skill_run_id, skill_version, input_hash, output_hash, schema_version,
            model_route, model_port
        ) VALUES(
            '018f0000-0000-7000-8000-000000000101',
            'pg-success-duplicate',
            '018f0000-0000-7000-8000-000000000199',
            '018f0000-0000-7000-8000-000000000104',
            '018f0000-0000-7000-8000-000000000105',
            '018f0000-0000-7000-8000-000000000106',
            '0.1.0',
            'input-hash',
            'output-hash',
            'runtime_probe.output.v1',
            'runtime_probe.test',
            'runtime_probe_test_port'
        );
        RAISE EXCEPTION 'duplicate result index was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            NULL;
    END;
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-000000000201';
    v_attempt_id uuid := '018f0000-0000-7000-8000-000000000202';
BEGIN
    PERFORM pg_temp.enqueue_probe_job(
        '018f0000-0000-7000-8000-000000000200',
        v_job_id,
        'runtime_probe.postgres.create',
        'runtime-probe-provider-failure',
        '018f0000-0000-7000-8000-000000000299',
        '{"request_id":"pg-provider-failure","correlation_id":"018f0000-0000-7000-8000-000000000299","text":"Hello Runtime","requested_operation":"normalize","metadata":{"case_id":"failure","provider_behavior":"failure"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-failure', v_attempt_id) = v_job_id, 'provider failure job was not claimed');
    INSERT INTO runtime_probe_skill_run(
        skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
        skill_name, skill_version, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, error_json
    ) VALUES(
        '018f0000-0000-7000-8000-000000000203',
        v_job_id, v_attempt_id, 'failed', 'pg-provider-failure',
        '018f0000-0000-7000-8000-000000000299',
        'runtime_probe', '0.1.0', 'skill-hash',
        'runtime_probe_public_input', '0.1.0', 'binding-hash',
        'runtime_probe.test', 'runtime_probe_test_port', 'input-hash',
        '{"code":"SyntheticProviderFailure"}'::jsonb
    );
    UPDATE scheduler_job_attempt
       SET status = 'failed', finished_at = clock_timestamp(), error_json = '{"code":"SyntheticProviderFailure"}'::jsonb
     WHERE scheduler_job_attempt.attempt_id = v_attempt_id;
    UPDATE scheduler_job
       SET status = 'queued', current_attempt_id = NULL, lease_owner = NULL,
           lease_expires_at = NULL, updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM runtime_probe_result_index WHERE runtime_probe_result_index.job_id = v_job_id) = 0, 'provider failure created a formal result');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE correlation_id = '018f0000-0000-7000-8000-000000000299') = 0, 'provider failure created outbox');

    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-retry', '018f0000-0000-7000-8000-000000000204') = v_job_id, 'retry did not reclaim provider failure job');
    PERFORM pg_temp.materialize_probe_success(
        v_job_id, '018f0000-0000-7000-8000-000000000204',
        'pg-provider-failure',
        '018f0000-0000-7000-8000-000000000299',
        '018f0000-0000-7000-8000-000000000205',
        '018f0000-0000-7000-8000-000000000206',
        '018f0000-0000-7000-8000-000000000207',
        '018f0000-0000-7000-8000-000000000208'
    );
    UPDATE scheduler_job_attempt
       SET status = 'succeeded', finished_at = clock_timestamp()
     WHERE attempt_id = '018f0000-0000-7000-8000-000000000204';
    UPDATE scheduler_job
       SET status = 'succeeded', updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM runtime_probe_result_index WHERE runtime_probe_result_index.job_id = v_job_id) = 1, 'retry did not create exactly one result');
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-000000000301';
    v_attempt_id uuid := '018f0000-0000-7000-8000-000000000302';
BEGIN
    PERFORM pg_temp.enqueue_probe_job(
        '018f0000-0000-7000-8000-000000000300',
        v_job_id,
        'runtime_probe.postgres.create',
        'runtime-probe-output-schema-error',
        '018f0000-0000-7000-8000-000000000399',
        '{"request_id":"pg-output-error","correlation_id":"018f0000-0000-7000-8000-000000000399","text":"Hello Runtime","requested_operation":"normalize","metadata":{"case_id":"bad-output","provider_behavior":"invalid_structure"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-output-error', v_attempt_id) = v_job_id, 'output schema error job was not claimed');
    INSERT INTO runtime_probe_skill_run(
        skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
        skill_name, skill_version, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, error_json
    ) VALUES(
        '018f0000-0000-7000-8000-000000000303',
        v_job_id, v_attempt_id, 'failed', 'pg-output-error',
        '018f0000-0000-7000-8000-000000000399',
        'runtime_probe', '0.1.0', 'skill-hash',
        'runtime_probe_public_input', '0.1.0', 'binding-hash',
        'runtime_probe.test', 'runtime_probe_test_port', 'input-hash',
        '{"code":"RuntimeProbeValidationError"}'::jsonb
    );
    UPDATE scheduler_job_attempt
       SET status = 'failed', finished_at = clock_timestamp(), error_json = '{"code":"RuntimeProbeValidationError"}'::jsonb
     WHERE scheduler_job_attempt.attempt_id = v_attempt_id;
    UPDATE scheduler_job
       SET status = 'queued', current_attempt_id = NULL, lease_owner = NULL,
           lease_expires_at = NULL, updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM runtime_probe_result_index WHERE runtime_probe_result_index.job_id = v_job_id) = 0, 'output schema error created a formal result');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE correlation_id = '018f0000-0000-7000-8000-000000000399') = 0, 'output schema error created outbox');
    UPDATE scheduler_job
       SET status = 'failed', updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id;
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-000000000401';
    first_attempt uuid := '018f0000-0000-7000-8000-000000000402';
    second_attempt uuid := '018f0000-0000-7000-8000-000000000403';
BEGIN
    PERFORM pg_temp.enqueue_probe_job(
        '018f0000-0000-7000-8000-000000000400',
        v_job_id,
        'runtime_probe.postgres.create',
        'runtime-probe-lease-recovery',
        '018f0000-0000-7000-8000-000000000499',
        '{"request_id":"pg-lease-recovery","correlation_id":"018f0000-0000-7000-8000-000000000499","text":"Hello Runtime","requested_operation":"normalize","metadata":{"case_id":"lease-recovery"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-stale', first_attempt) = v_job_id, 'lease recovery job was not claimed');
    UPDATE scheduler_job_attempt
       SET started_at = clock_timestamp() - interval '120 seconds',
           heartbeat_at = clock_timestamp() - interval '120 seconds',
           lease_expires_at = clock_timestamp() - interval '60 seconds'
     WHERE scheduler_job_attempt.attempt_id = first_attempt;
    UPDATE scheduler_job
       SET lease_expires_at = clock_timestamp() - interval '60 seconds'
     WHERE scheduler_job.job_id = v_job_id;
    UPDATE scheduler_job_attempt
       SET status = 'failed',
           finished_at = clock_timestamp(),
           error_json = '{"code":"lease_expired"}'::jsonb
     WHERE attempt_id = first_attempt
       AND lease_expires_at < clock_timestamp();
    UPDATE scheduler_job
       SET status = 'queued',
           current_attempt_id = NULL,
           lease_owner = NULL,
           lease_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE scheduler_job.job_id = v_job_id
       AND lease_expires_at < clock_timestamp();
    PERFORM pg_temp.gate_assert(pg_temp.claim_runtime_probe('worker-recovered', second_attempt) = v_job_id, 'expired lease was not reclaimable');
    PERFORM pg_temp.materialize_probe_success(
        v_job_id, second_attempt, 'pg-lease-recovery',
        '018f0000-0000-7000-8000-000000000499',
        '018f0000-0000-7000-8000-000000000404',
        '018f0000-0000-7000-8000-000000000405',
        '018f0000-0000-7000-8000-000000000406',
        '018f0000-0000-7000-8000-000000000407'
    );
END $$;

DO $$
DECLARE
    before_results bigint;
    before_outbox bigint;
BEGIN
    SELECT count(*) INTO before_results FROM runtime_probe_result_index;
    SELECT count(*) INTO before_outbox FROM outbox_message;
    BEGIN
        PERFORM pg_temp.materialize_probe_success(
            '018f0000-0000-7000-8000-000000000101',
            '018f0000-0000-7000-8000-000000000102',
            'pg-materializer-fault',
            '018f0000-0000-7000-8000-000000000199',
            '018f0000-0000-7000-8000-000000000501',
            '018f0000-0000-7000-8000-000000000502',
            '018f0000-0000-7000-8000-000000000503',
            '018f0000-0000-7000-8000-000000000504'
        );
        RAISE EXCEPTION 'materializer duplicate job was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            NULL;
    END;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM runtime_probe_result_index) = before_results, 'materializer failure changed result count');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message) = before_outbox, 'materializer failure changed outbox count');
END $$;

DO $$
DECLARE
    mutation_rejected boolean := false;
BEGIN
    BEGIN
        UPDATE runtime_probe_result_index
           SET output_hash = 'changed'
         WHERE job_id = '018f0000-0000-7000-8000-000000000101';
    EXCEPTION
        WHEN others THEN
            mutation_rejected := true;
    END;
    PERFORM pg_temp.gate_assert(mutation_rejected, 'runtime_probe_result_index mutation was accepted');
END $$;

ROLLBACK;
\echo GOAL-RUNTIME-POSTGRES-GATE-01 PostgreSQL verification passed
