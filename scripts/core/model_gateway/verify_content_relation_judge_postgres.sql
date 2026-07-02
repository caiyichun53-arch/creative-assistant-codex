\echo GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01 PostgreSQL verification started
BEGIN;

CREATE OR REPLACE FUNCTION pg_temp.gate_assert(ok boolean, message text) RETURNS void AS $$
BEGIN
    IF ok IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION '%', message;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.claim_relation_job(
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

CREATE OR REPLACE FUNCTION pg_temp.enqueue_relation_job(
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
        p_receipt_id, 'content_relation_judge.postgres.create', p_idempotency_key,
        md5(p_payload::text), '{"accepted": true}'::jsonb, p_correlation_id, 'succeeded'
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

CREATE OR REPLACE FUNCTION pg_temp.materialize_relation_success(
    p_job_id uuid,
    p_attempt_id uuid,
    p_request_id text,
    p_correlation_id text,
    p_root_id uuid,
    p_version_id uuid,
    p_skill_run_id uuid,
    p_outbox_id uuid,
    p_relation_type text DEFAULT 'contains',
    p_direction text DEFAULT 'left_contains_right',
    p_left_evidence jsonb DEFAULT '["通勤集中"]'::jsonb,
    p_right_evidence jsonb DEFAULT '["通勤集中"]'::jsonb,
    p_output_hash text DEFAULT 'relation-output-hash'
) RETURNS void AS $$
BEGIN
    INSERT INTO trace_root(root_id, object_kind)
    VALUES(p_root_id, 'formal_business_skill_result');

    INSERT INTO trace_version(
        version_id, root_id, version_no, content_hash, business_hash,
        projection_version, payload_json
    ) VALUES(
        p_version_id, p_root_id, 1, p_output_hash,
        p_output_hash || '-business', 'formal_business_skill_result.v1',
        jsonb_build_object(
            'goal', 'GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01',
            'formal_skill_id', 'content_relation_judge',
            'request_id', p_request_id,
            'correlation_id', p_correlation_id,
            'schema_version', 'formal_business_skill_result.v1',
            'model_route', 'business.content_relation_judgement',
            'model_port', 'formal_business_skill_test_port',
            'output', jsonb_build_object(
                'relation_type', p_relation_type,
                'relation_direction', p_direction,
                'confidence', 'high',
                'evidence_from_left', p_left_evidence,
                'evidence_from_right', p_right_evidence,
                'compared_dimensions', jsonb_build_array('core facts'),
                'missing_evidence', CASE WHEN p_relation_type = 'insufficient_evidence' THEN jsonb_build_array('missing body') ELSE '[]'::jsonb END,
                'rationale', 'PostgreSQL gate fixture relation.',
                'schema_version', 'content_relation_judge.output.v1'
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
        'content_relation_judge', '1.0.0', p_request_id, p_correlation_id,
        'skill-hash', 'content_relation_judge_public_input_to_relation_judgement',
        '1.0.0', 'binding-hash', 'business.content_relation_judgement',
        'formal_business_skill_test_port', 'input-hash-' || p_request_id,
        'model-input-hash-' || p_request_id, p_output_hash
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
            'formal_skill_id', 'content_relation_judge',
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
        p_job_id, 'content_relation_judge', p_request_id, p_correlation_id,
        p_root_id, p_version_id, p_skill_run_id, '1.0.0',
        'input-hash-' || p_request_id, 'model-input-hash-' || p_request_id, p_output_hash,
        'formal_business_skill_result.v1', 'business.content_relation_judgement',
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
    v_job_id uuid := '018f0000-0000-7000-8000-00000000f101';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000f102';
BEGIN
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f100',
        v_job_id,
        'content-relation-success',
        '018f0000-0000-7000-8000-00000000f199',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-relation-success","left_content":"A includes B"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-a', v_attempt_id) = v_job_id, 'worker-a did not claim success job');
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-b', '018f0000-0000-7000-8000-00000000f103') IS NULL, 'worker-b claimed leased job');
    PERFORM pg_temp.materialize_relation_success(
        v_job_id, v_attempt_id, 'pg-relation-success', '018f0000-0000-7000-8000-00000000f199',
        '018f0000-0000-7000-8000-00000000f104',
        '018f0000-0000-7000-8000-00000000f105',
        '018f0000-0000-7000-8000-00000000f106',
        '018f0000-0000-7000-8000-00000000f107'
    );
    UPDATE scheduler_job_attempt SET status = 'succeeded', finished_at = clock_timestamp() WHERE attempt_id = v_attempt_id;
    UPDATE scheduler_job SET status = 'succeeded', updated_at = clock_timestamp() WHERE job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM formal_business_skill_result_index WHERE job_id = v_job_id) = 1, 'success result count mismatch');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE topic = 'formal_business_skill.result.materialized' AND causation_id = '018f0000-0000-7000-8000-00000000f106') = 1, 'success outbox count mismatch');
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000f201';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000f202';
BEGIN
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f200',
        v_job_id,
        'content-relation-insufficient',
        '018f0000-0000-7000-8000-00000000f299',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-relation-insufficient","left_content":""}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-insufficient', v_attempt_id) = v_job_id, 'insufficient job was not claimed');
    PERFORM pg_temp.materialize_relation_success(
        v_job_id, v_attempt_id, 'pg-relation-insufficient', '018f0000-0000-7000-8000-00000000f299',
        '018f0000-0000-7000-8000-00000000f204',
        '018f0000-0000-7000-8000-00000000f205',
        '018f0000-0000-7000-8000-00000000f206',
        '018f0000-0000-7000-8000-00000000f207',
        'insufficient_evidence',
        'not_applicable',
        '[]'::jsonb,
        '[]'::jsonb,
        'relation-insufficient-output-hash'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT payload_json #>> '{output,relation_type}' FROM trace_version WHERE version_id = '018f0000-0000-7000-8000-00000000f205') = 'insufficient_evidence',
        'insufficient_evidence semantics not materialized'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT jsonb_array_length(payload_json #> '{output,missing_evidence}') FROM trace_version WHERE version_id = '018f0000-0000-7000-8000-00000000f205') > 0,
        'insufficient_evidence missing_evidence was not materialized'
    );
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000f301';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000f302';
BEGIN
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f300',
        v_job_id,
        'content-relation-no-relation',
        '018f0000-0000-7000-8000-00000000f399',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-relation-no-relation"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-no-relation', v_attempt_id) = v_job_id, 'no_relation job was not claimed');
    PERFORM pg_temp.materialize_relation_success(
        v_job_id, v_attempt_id, 'pg-relation-no-relation', '018f0000-0000-7000-8000-00000000f399',
        '018f0000-0000-7000-8000-00000000f304',
        '018f0000-0000-7000-8000-00000000f305',
        '018f0000-0000-7000-8000-00000000f306',
        '018f0000-0000-7000-8000-00000000f307',
        'no_relation',
        'symmetric',
        '[]'::jsonb,
        '[]'::jsonb,
        'relation-no-relation-output-hash'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT payload_json #>> '{output,relation_type}' FROM trace_version WHERE version_id = '018f0000-0000-7000-8000-00000000f305') = 'no_relation',
        'no_relation semantics not materialized'
    );
END $$;

DO $$
BEGIN
    BEGIN
        INSERT INTO command_receipt(
            receipt_id, command_scope, idempotency_key, request_hash, result_json,
            correlation_id, status
        ) VALUES(
            '018f0000-0000-7000-8000-00000000f408',
            'content_relation_judge.postgres.create',
            'content-relation-success',
            'different-request-hash',
            '{"accepted": true}'::jsonb,
            '018f0000-0000-7000-8000-00000000f199',
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
            '018f0000-0000-7000-8000-00000000f409',
            'formal_business_skill.result.materialized',
            '{"duplicate": true}'::jsonb,
            '018f0000-0000-7000-8000-00000000f199',
            '018f0000-0000-7000-8000-00000000f106'
        );
        RAISE EXCEPTION 'duplicate outbox event was accepted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;
END $$;

DO $$
DECLARE
    v_job_id uuid := '018f0000-0000-7000-8000-00000000f501';
    v_attempt_id uuid := '018f0000-0000-7000-8000-00000000f502';
BEGIN
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f500',
        v_job_id,
        'content-relation-provider-failure',
        '018f0000-0000-7000-8000-00000000f599',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-provider-failure"}}'::jsonb,
        2
    );
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-failure', v_attempt_id) = v_job_id, 'provider failure job was not claimed');
    INSERT INTO formal_business_skill_run(
        skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
        request_id, correlation_id, skill_hash, binding_name, binding_version,
        binding_hash, model_route, model_port, input_hash, error_json
    ) VALUES(
        '018f0000-0000-7000-8000-00000000f503',
        v_job_id, v_attempt_id, 'failed', 'content_relation_judge', '1.0.0',
        'pg-provider-failure', '018f0000-0000-7000-8000-00000000f599',
        'skill-hash', 'content_relation_judge_public_input_to_relation_judgement',
        '1.0.0', 'binding-hash', 'business.content_relation_judgement',
        'formal_business_skill_test_port', 'input-hash',
        '{"code":"SyntheticProviderFailure"}'::jsonb
    );
    UPDATE scheduler_job_attempt SET status = 'failed', finished_at = clock_timestamp(), error_json = '{"code":"SyntheticProviderFailure"}'::jsonb WHERE attempt_id = v_attempt_id;
    UPDATE scheduler_job SET status = 'queued', current_attempt_id = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = clock_timestamp() WHERE job_id = v_job_id;
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM formal_business_skill_result_index WHERE job_id = v_job_id) = 0, 'provider failure created formal result');
    PERFORM pg_temp.gate_assert((SELECT count(*) FROM outbox_message WHERE correlation_id = '018f0000-0000-7000-8000-00000000f599') = 0, 'provider failure created outbox');
    PERFORM pg_temp.gate_assert(pg_temp.claim_relation_job('worker-retry', '018f0000-0000-7000-8000-00000000f504') = v_job_id, 'retry did not reclaim failed job');
END $$;

DO $$
DECLARE
    failure_case record;
BEGIN
    FOR failure_case IN
        SELECT *
          FROM (VALUES
            ('schema-error', '018f0000-0000-7000-8000-00000000fb00'::uuid, '018f0000-0000-7000-8000-00000000fb01'::uuid, '018f0000-0000-7000-8000-00000000fb02'::uuid, '018f0000-0000-7000-8000-00000000fb03'::uuid, 'SchemaError'),
            ('invalid-enum', '018f0000-0000-7000-8000-00000000fb10'::uuid, '018f0000-0000-7000-8000-00000000fb11'::uuid, '018f0000-0000-7000-8000-00000000fb12'::uuid, '018f0000-0000-7000-8000-00000000fb13'::uuid, 'IllegalRelationEnum'),
            ('direction-error', '018f0000-0000-7000-8000-00000000fb20'::uuid, '018f0000-0000-7000-8000-00000000fb21'::uuid, '018f0000-0000-7000-8000-00000000fb22'::uuid, '018f0000-0000-7000-8000-00000000fb23'::uuid, 'InvalidDirection')
          ) AS t(label, receipt_id, job_id, attempt_id, skill_run_id, error_code)
    LOOP
        PERFORM pg_temp.enqueue_relation_job(
            failure_case.receipt_id,
            failure_case.job_id,
            'content-relation-' || failure_case.label,
            '018f0000-0000-7000-8000-00000000fc99',
            jsonb_build_object('formal_skill_id', 'content_relation_judge', 'input', jsonb_build_object('request_id', failure_case.label)),
            1
        );
        UPDATE scheduler_job
           SET status='leased',
               attempt_count=1,
               current_attempt_id=failure_case.attempt_id,
               lease_owner='failure-worker'
         WHERE job_id=failure_case.job_id;
        INSERT INTO scheduler_job_attempt(
            attempt_id, job_id, attempt_no, worker_id, status,
            started_at, heartbeat_at, lease_expires_at, finished_at, correlation_id, causation_id, error_json
        ) VALUES(
            failure_case.attempt_id, failure_case.job_id, 1, 'failure-worker', 'failed',
            clock_timestamp(), clock_timestamp(), clock_timestamp() + interval '60 seconds', clock_timestamp(),
            '018f0000-0000-7000-8000-00000000fc99',
            failure_case.job_id,
            jsonb_build_object('code', failure_case.error_code)
        );
        INSERT INTO formal_business_skill_run(
            skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
            request_id, correlation_id, skill_hash, binding_name, binding_version,
            binding_hash, model_route, model_port, input_hash, error_json
        ) VALUES(
            failure_case.skill_run_id,
            failure_case.job_id, failure_case.attempt_id, 'failed',
            'content_relation_judge', '1.0.0', failure_case.label,
            '018f0000-0000-7000-8000-00000000fc99',
            'skill-hash', 'content_relation_judge_public_input_to_relation_judgement',
            '1.0.0', 'binding-hash', 'business.content_relation_judgement',
            'formal_business_skill_test_port', 'input-hash-' || failure_case.label,
            jsonb_build_object('code', failure_case.error_code)
        );
        UPDATE scheduler_job SET status='dead', updated_at=clock_timestamp() WHERE job_id=failure_case.job_id;
        PERFORM pg_temp.gate_assert(
            (SELECT count(*) FROM formal_business_skill_result_index WHERE job_id=failure_case.job_id) = 0,
            failure_case.label || ' created formal result'
        );
        PERFORM pg_temp.gate_assert(
            (SELECT count(*) FROM outbox_message WHERE correlation_id='018f0000-0000-7000-8000-00000000fc99') = 0,
            failure_case.label || ' created success outbox'
        );
    END LOOP;
END $$;

DO $$
DECLARE
    before_results bigint;
    before_outbox bigint;
BEGIN
    SELECT count(*) INTO before_results FROM formal_business_skill_result_index;
    SELECT count(*) INTO before_outbox FROM outbox_message;
    BEGIN
        PERFORM pg_temp.materialize_relation_success(
            '018f0000-0000-7000-8000-00000000f101',
            '018f0000-0000-7000-8000-00000000f102',
            'pg-materializer-fault',
            '018f0000-0000-7000-8000-00000000f199',
            '018f0000-0000-7000-8000-00000000f601',
            '018f0000-0000-7000-8000-00000000f602',
            '018f0000-0000-7000-8000-00000000f603',
            '018f0000-0000-7000-8000-00000000f604'
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
    v_job_a uuid := '018f0000-0000-7000-8000-00000000f701';
    v_job_b uuid := '018f0000-0000-7000-8000-00000000f711';
BEGIN
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f700',
        v_job_a,
        'content-relation-ab-contains',
        '018f0000-0000-7000-8000-00000000f799',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-ab-contains","left":"full","right":"part"}}'::jsonb
    );
    PERFORM pg_temp.enqueue_relation_job(
        '018f0000-0000-7000-8000-00000000f710',
        v_job_b,
        'content-relation-ab-contained-by',
        '018f0000-0000-7000-8000-00000000f798',
        '{"formal_skill_id":"content_relation_judge","input":{"request_id":"pg-ab-contained-by","left":"part","right":"full"}}'::jsonb
    );
    PERFORM pg_temp.gate_assert(
        pg_temp.claim_relation_job('worker-ab-a', '018f0000-0000-7000-8000-00000000f702') = v_job_a,
        'A/B original job was not claimed'
    );
    PERFORM pg_temp.gate_assert(
        pg_temp.claim_relation_job('worker-ab-b', '018f0000-0000-7000-8000-00000000f712') = v_job_b,
        'A/B swapped job was not claimed'
    );
    PERFORM pg_temp.materialize_relation_success(
        v_job_a, '018f0000-0000-7000-8000-00000000f702', 'pg-ab-contains',
        '018f0000-0000-7000-8000-00000000f799',
        '018f0000-0000-7000-8000-00000000f704',
        '018f0000-0000-7000-8000-00000000f705',
        '018f0000-0000-7000-8000-00000000f706',
        '018f0000-0000-7000-8000-00000000f707',
        'contains', 'left_contains_right', '["left full"]'::jsonb, '["right part"]'::jsonb,
        'relation-ab-contains-output-hash'
    );
    PERFORM pg_temp.materialize_relation_success(
        v_job_b, '018f0000-0000-7000-8000-00000000f712', 'pg-ab-contained-by',
        '018f0000-0000-7000-8000-00000000f798',
        '018f0000-0000-7000-8000-00000000f714',
        '018f0000-0000-7000-8000-00000000f715',
        '018f0000-0000-7000-8000-00000000f716',
        '018f0000-0000-7000-8000-00000000f717',
        'contained_by', 'left_contained_by_right', '["right part"]'::jsonb, '["left full"]'::jsonb,
        'relation-ab-contained-by-output-hash'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT payload_json #>> '{output,relation_type}' FROM trace_version WHERE version_id='018f0000-0000-7000-8000-00000000f705') = 'contains',
        'A/B original relation not contains'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT payload_json #>> '{output,relation_type}' FROM trace_version WHERE version_id='018f0000-0000-7000-8000-00000000f715') = 'contained_by',
        'A/B swapped relation not contained_by'
    );
    PERFORM pg_temp.gate_assert(
        (SELECT input_hash FROM formal_business_skill_result_index WHERE job_id=v_job_a)
        <>
        (SELECT input_hash FROM formal_business_skill_result_index WHERE job_id=v_job_b),
        'A/B swapped jobs must have different input hashes'
    );
END $$;

DO $$
DECLARE
    mutation_rejected boolean := false;
BEGIN
    BEGIN
        UPDATE formal_business_skill_result_index
           SET output_hash = 'changed'
         WHERE job_id = '018f0000-0000-7000-8000-00000000f101';
    EXCEPTION
        WHEN others THEN mutation_rejected := true;
    END;
    PERFORM pg_temp.gate_assert(mutation_rejected, 'result index mutation was accepted');
END $$;

ROLLBACK;
\echo GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01 PostgreSQL verification passed
