\echo GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01 PostgreSQL verification started
BEGIN;

CREATE OR REPLACE FUNCTION pg_temp.gate_assert(ok boolean, message text) RETURNS void AS $$
BEGIN
    IF ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION '%', message;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.claim_content_classify(
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
         WHERE job_kind = 'formal_skill.execute'
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
     RETURNING j.job_id, j.attempt_count, j.correlation_id, j.causation_id
    )
    INSERT INTO scheduler_job_attempt(
        attempt_id, job_id, attempt_no, worker_id, status, started_at,
        heartbeat_at, lease_expires_at, correlation_id, causation_id
    )
    SELECT p_attempt_id, job_id, attempt_count, p_worker_id, 'leased', p_now,
           p_now, p_now + interval '60 seconds', correlation_id, causation_id
      FROM updated
    RETURNING job_id INTO claimed_job_id;

    RETURN claimed_job_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.enqueue_content_classify_job(
    p_receipt_id uuid,
    p_job_id uuid,
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
        p_receipt_id, 'content_classify.postgres.create', p_idempotency_key,
        'request-hash', '{"accepted": true}'::jsonb, p_correlation_id, 'succeeded'
    );

    INSERT INTO scheduler_job(
        job_id, job_kind, status, priority, run_after, max_attempts, payload_json,
        correlation_id, created_by_receipt_id
    ) VALUES(
        p_job_id, 'formal_skill.execute', 'queued', 0, clock_timestamp(),
        p_max_attempts, p_payload, p_correlation_id, p_receipt_id
    );
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.materialize_content_classify_success(
    p_job_id uuid,
    p_attempt_id uuid,
    p_request_id text,
    p_correlation_id text,
    p_root_id uuid,
    p_version_id uuid,
    p_skill_run_id uuid,
    p_outbox_id uuid,
    p_status text DEFAULT 'classified',
    p_primary_label text DEFAULT 'fan_kepu_social_life'
) RETURNS void AS $$
BEGIN
    INSERT INTO trace_root(root_id, object_kind)
    VALUES(p_root_id, 'formal_business_skill_result');

    INSERT INTO trace_version(
        version_id, root_id, version_no, content_hash, business_hash,
        projection_version, payload_json
    ) VALUES(
        p_version_id, p_root_id, 1, 'content-classify-output-hash',
        'content-classify-business-hash', 'formal_business_skill_result.v1',
        jsonb_build_object(
            'goal', 'GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01',
            'formal_skill_id', 'content_classify',
            'request_id', p_request_id,
            'correlation_id', p_correlation_id,
            'schema_version', 'formal_business_skill_result.v1',
            'model_route', 'business.topic_judgement',
            'model_port', 'formal_business_skill_test_port',
            'output', jsonb_build_object(
                'classification_status', p_status,
                'primary_label', p_primary_label,
                'candidate_labels', CASE WHEN p_status = 'no_result' THEN '[]'::jsonb ELSE jsonb_build_array(p_primary_label) END,
                'no_result_reason', CASE WHEN p_status = 'no_result' THEN 'insufficient_information' ELSE 'none' END,
                'uncertainty_reason', 'none',
                'confidence', CASE WHEN p_status = 'no_result' THEN 'none' ELSE 'high' END,
                'rationale', 'PostgreSQL gate fixture classification.',
                'evidence_used', CASE WHEN p_status = 'no_result' THEN '[]'::jsonb ELSE jsonb_build_array('社区电梯早高峰拥堵') END,
                'schema_version', 'content_classify.output.v1'
            )
        )
    );

    UPDATE trace_root
       SET current_version_id = p_version_id
     WHERE root_id = p_root_id;

    INSERT INTO formal_business_skill_run(
        skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
        request_id, correlation_id, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, model_input_hash, output_hash
    ) VALUES(
        p_skill_run_id, p_job_id, p_attempt_id, 'succeeded',
        'content_classify', '1.0.0', p_request_id, p_correlation_id,
        'skill-hash', 'content_classify_public_input_to_topic_judgement',
        '1.0.0', 'binding-hash', 'business.topic_judgement',
        'formal_business_skill_test_port', 'input-hash', 'model-input-hash',
        'output-hash'
    );

    INSERT INTO audit_event(
        audit_id, event_type, actor, object_kind, object_id, version_id,
        payload_json, correlation_id, causation_id
    ) VALUES(
        p_outbox_id, 'formal_business_skill.result.materialized',
        'formal_business_skill_materializer', 'formal_business_skill_result',
        p_root_id, p_version_id, jsonb_build_object('job_id', p_job_id),
        p_correlation_id::uuid, p_attempt_id
    );

    INSERT INTO outbox_message(
        outbox_id, topic, payload_json, correlation_id, causation_id
    ) VALUES(
        p_outbox_id, 'formal_business_skill.result.materialized',
        jsonb_build_object(
            'formal_skill_id', 'content_classify',
            'request_id', p_request_id,
            'job_id', p_job_id,
            'result_version_id', p_version_id,
            'schema_version', 'formal_business_skill_result.v1'
        ),
        p_correlation_id::uuid, p_skill_run_id
    );

    INSERT INTO formal_business_skill_result_index(
        job_id, formal_skill_id, request_id, correlation_id, result_root_id,
        result_version_id, skill_run_id, skill_version, input_hash,
        model_input_hash, output_hash, schema_version, model_route, model_port
    ) VALUES(
        p_job_id, 'content_classify', p_request_id, p_correlation_id,
        p_root_id, p_version_id, p_skill_run_id, '1.0.0',
        'input-hash', 'model-input-hash', 'output-hash',
        'formal_business_skill_result.v1', 'business.topic_judgement',
        'formal_business_skill_test_port'
    );
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    formal_rows bigint;
BEGIN
    PERFORM pg_temp.gate_assert(
        EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'formal_business_skill_run'),
        'missing formal_business_skill_run table'
    );
    PERFORM pg_temp.gate_assert(
        EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'formal_business_skill_result_index'),
        'missing formal_business_skill_result_index table'
    );
    EXECUTE 'SELECT count(*) FROM formal_business_skill_run' INTO formal_rows;
    PERFORM pg_temp.gate_assert(formal_rows = 0, 'formal_business_skill_run must start empty');
    EXECUTE 'SELECT count(*) FROM formal_business_skill_result_index' INTO formal_rows;
    PERFORM pg_temp.gate_assert(formal_rows = 0, 'formal_business_skill_result_index must start empty');
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000a101';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000a102';
BEGIN
    PERFORM pg_temp.enqueue_content_classify_job(
        '018f0000-0000-7000-8000-00000000a100',
        v_job_id,
        'content-classify-success',
        '018f0000-0000-7000-8000-00000000a199',
        '{"formal_skill_id":"content_classify","input":{"request_id":"pg-success","title":"社区电梯为什么拥堵"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_content_classify('worker-a', v_attempt_id) = v_job_id, 'worker-a did not claim success job');
    PERFORM pg_temp.gate_assert(pg_temp.claim_content_classify('worker-b', '018f0000-0000-7000-8000-00000000a103') IS NULL, 'worker-b claimed leased job');
    PERFORM pg_temp.materialize_content_classify_success(
        v_job_id, v_attempt_id, 'pg-success', '018f0000-0000-7000-8000-00000000a199',
        '018f0000-0000-7000-8000-00000000a104',
        '018f0000-0000-7000-8000-00000000a105',
        '018f0000-0000-7000-8000-00000000a106',
        '018f0000-0000-7000-8000-00000000a107'
    );
    UPDATE scheduler_job_attempt SET status = 'succeeded', finished_at = clock_timestamp() WHERE attempt_id = v_attempt_id;
    UPDATE scheduler_job SET status = 'succeeded', updated_at = clock_timestamp() WHERE job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM formal_business_skill_result_index WHERE job_id = v_job_id) = 1, 'success result count mismatch');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE topic = 'formal_business_skill.result.materialized' AND causation_id = '018f0000-0000-7000-8000-00000000a106') = 1, 'success outbox count mismatch');
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000b101';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000b102';
BEGIN
    PERFORM pg_temp.enqueue_content_classify_job(
        '018f0000-0000-7000-8000-00000000b100',
        v_job_id,
        'content-classify-no-result',
        '018f0000-0000-7000-8000-00000000b199',
        '{"formal_skill_id":"content_classify","input":{"request_id":"pg-no-result","title":""}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_content_classify('worker-no-result', v_attempt_id) = v_job_id, 'no-result job was not claimed');
    PERFORM pg_temp.materialize_content_classify_success(
        v_job_id, v_attempt_id, 'pg-no-result', '018f0000-0000-7000-8000-00000000b199',
        '018f0000-0000-7000-8000-00000000b104',
        '018f0000-0000-7000-8000-00000000b105',
        '018f0000-0000-7000-8000-00000000b106',
        '018f0000-0000-7000-8000-00000000b107',
        'no_result',
        'none'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT payload_json #>> '{output,no_result_reason}' FROM trace_version WHERE version_id = '018f0000-0000-7000-8000-00000000b105') = 'insufficient_information',
        'no-result semantics not materialized'
    );
END $$;

DO $$
BEGIN
    BEGIN
        INSERT INTO command_receipt(
            receipt_id, command_scope, idempotency_key, request_hash, result_json,
            correlation_id, status
        ) VALUES(
            '018f0000-0000-7000-8000-00000000c108',
            'content_classify.postgres.create',
            'content-classify-success',
            'different-request-hash',
            '{"accepted": true}'::jsonb,
            '018f0000-0000-7000-8000-00000000a199',
            'succeeded'
        );
        RAISE EXCEPTION 'duplicate idempotency key was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO outbox_message(
            outbox_id, topic, payload_json, correlation_id, causation_id
        ) VALUES(
            '018f0000-0000-7000-8000-00000000c109',
            'formal_business_skill.result.materialized',
            '{"duplicate": true}'::jsonb,
            '018f0000-0000-7000-8000-00000000a199',
            '018f0000-0000-7000-8000-00000000a106'
        );
        RAISE EXCEPTION 'duplicate outbox event was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000d201';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000d202';
BEGIN
    PERFORM pg_temp.enqueue_content_classify_job(
        '018f0000-0000-7000-8000-00000000d200',
        v_job_id,
        'content-classify-provider-failure',
        '018f0000-0000-7000-8000-00000000d299',
        '{"formal_skill_id":"content_classify","input":{"request_id":"pg-provider-failure"}}'::jsonb,
        2
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_content_classify('worker-failure', v_attempt_id) = v_job_id, 'provider failure job was not claimed');
    INSERT INTO formal_business_skill_run(
        skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
        request_id, correlation_id, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, error_json
    ) VALUES(
        '018f0000-0000-7000-8000-00000000d203',
        v_job_id, v_attempt_id, 'failed', 'content_classify', '1.0.0',
        'pg-provider-failure', '018f0000-0000-7000-8000-00000000d299',
        'skill-hash', 'content_classify_public_input_to_topic_judgement',
        '1.0.0', 'binding-hash', 'business.topic_judgement',
        'formal_business_skill_test_port', 'input-hash',
        '{"code":"SyntheticProviderFailure"}'::jsonb
    );
    UPDATE scheduler_job_attempt SET status = 'failed', finished_at = clock_timestamp(), error_json = '{"code":"SyntheticProviderFailure"}'::jsonb WHERE attempt_id = v_attempt_id;
    UPDATE scheduler_job SET status = 'queued', current_attempt_id = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = clock_timestamp() WHERE job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM formal_business_skill_result_index WHERE job_id = v_job_id) = 0, 'provider failure created formal result');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE correlation_id = '018f0000-0000-7000-8000-00000000d299') = 0, 'provider failure created outbox');
    PERFORM pg_temp.gate_assert(pg_temp.claim_content_classify('worker-retry', '018f0000-0000-7000-8000-00000000d204') = v_job_id, 'retry did not reclaim failed job');
END $$;

DO $$
DECLARE
    before_results bigint;
    before_outbox bigint;
BEGIN
    SELECT count(*) INTO before_results FROM formal_business_skill_result_index;
    SELECT count(*) INTO before_outbox FROM outbox_message;
    BEGIN
        PERFORM pg_temp.materialize_content_classify_success(
            '018f0000-0000-7000-8000-00000000a101',
            '018f0000-0000-7000-8000-00000000a102',
            'pg-materializer-fault',
            '018f0000-0000-7000-8000-00000000a199',
            '018f0000-0000-7000-8000-00000000e501',
            '018f0000-0000-7000-8000-00000000e502',
            '018f0000-0000-7000-8000-00000000e503',
            '018f0000-0000-7000-8000-00000000e504'
        );
        RAISE EXCEPTION 'materializer duplicate job was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM formal_business_skill_result_index) = before_results, 'materializer failure changed result count');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message) = before_outbox, 'materializer failure changed outbox count');
END $$;

DO $$
DECLARE
    mutation_rejected boolean := false;
BEGIN
    BEGIN
        UPDATE formal_business_skill_result_index
           SET output_hash = 'changed'
         WHERE job_id = '018f0000-0000-7000-8000-00000000a101';
    EXCEPTION
        WHEN others THEN mutation_rejected := true;
    END;
    PERFORM pg_temp.gate_assert(mutation_rejected, 'result index mutation was accepted');
END $$;

ROLLBACK;
\echo GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01 PostgreSQL verification passed
