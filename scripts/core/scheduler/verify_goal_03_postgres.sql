\echo GOAL-03 PostgreSQL verification started

BEGIN;

INSERT INTO command_receipt(
    receipt_id,
    command_scope,
    idempotency_key,
    request_hash,
    result_json,
    correlation_id,
    status
) VALUES (
    '018f0000-0000-7000-8000-000000000401',
    'goal03.scheduler.enqueue',
    'postgres-enqueue-job',
    'sha256:req-job',
    '{"job_id":"018f0000-0000-7000-8000-000000000402","status":"queued"}'::jsonb,
    '018f0000-0000-7000-8000-000000000403',
    'succeeded'
);

INSERT INTO outbox_message(
    outbox_id,
    topic,
    payload_json,
    correlation_id,
    causation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000404',
    'core.state.changed',
    '{"object_kind":"topic"}'::jsonb,
    '018f0000-0000-7000-8000-000000000403',
    '018f0000-0000-7000-8000-000000000405'
);

INSERT INTO scheduler_job(
    job_id,
    job_kind,
    status,
    priority,
    run_after,
    max_attempts,
    attempt_count,
    payload_json,
    source_outbox_id,
    correlation_id,
    causation_id,
    created_by_receipt_id
) VALUES (
    '018f0000-0000-7000-8000-000000000402',
    'outbox.dispatch',
    'queued',
    5,
    now(),
    2,
    0,
    '{"outbox_id":"018f0000-0000-7000-8000-000000000404"}'::jsonb,
    '018f0000-0000-7000-8000-000000000404',
    '018f0000-0000-7000-8000-000000000403',
    '018f0000-0000-7000-8000-000000000405',
    '018f0000-0000-7000-8000-000000000401'
);

UPDATE scheduler_job
   SET status = 'leased',
       attempt_count = 1,
       current_attempt_id = '018f0000-0000-7000-8000-000000000406',
       lease_owner = 'postgres-worker',
       lease_expires_at = now() + interval '60 seconds'
 WHERE job_id = '018f0000-0000-7000-8000-000000000402';

INSERT INTO scheduler_job_attempt(
    attempt_id,
    job_id,
    attempt_no,
    worker_id,
    status,
    started_at,
    heartbeat_at,
    lease_expires_at,
    correlation_id,
    causation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000406',
    '018f0000-0000-7000-8000-000000000402',
    1,
    'postgres-worker',
    'leased',
    now(),
    now(),
    now() + interval '60 seconds',
    '018f0000-0000-7000-8000-000000000403',
    '018f0000-0000-7000-8000-000000000405'
);

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*) INTO matched
      FROM scheduler_job j
      JOIN scheduler_job_attempt a ON a.job_id = j.job_id
     WHERE j.status = 'leased'
       AND a.status = 'leased'
       AND j.current_attempt_id = a.attempt_id;
    IF matched <> 1 THEN
        RAISE EXCEPTION 'expected one leased job and attempt';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE scheduler_job
           SET attempt_count = 0
         WHERE job_id = '018f0000-0000-7000-8000-000000000402';
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected attempt_count decrease to be rejected';
    END IF;
END $$;

UPDATE scheduler_job_attempt
   SET status = 'succeeded',
       finished_at = now()
 WHERE attempt_id = '018f0000-0000-7000-8000-000000000406';

UPDATE scheduler_job
   SET status = 'succeeded',
       lease_owner = NULL,
       lease_expires_at = NULL
 WHERE job_id = '018f0000-0000-7000-8000-000000000402';

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE scheduler_job
           SET status = 'queued'
         WHERE job_id = '018f0000-0000-7000-8000-000000000402';
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected terminal scheduler_job update to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE scheduler_job_attempt
           SET status = 'failed'
         WHERE attempt_id = '018f0000-0000-7000-8000-000000000406';
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected terminal scheduler_job_attempt update to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        INSERT INTO scheduler_job(
            job_id,
            job_kind,
            status,
            priority,
            run_after,
            max_attempts,
            payload_json,
            source_outbox_id,
            correlation_id,
            created_by_receipt_id
        ) VALUES (
            '018f0000-0000-7000-8000-000000000407',
            'outbox.dispatch',
            'queued',
            0,
            now(),
            1,
            '{}'::jsonb,
            '018f0000-0000-7000-8000-000000000404',
            '018f0000-0000-7000-8000-000000000403',
            '018f0000-0000-7000-8000-000000000401'
        );
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected duplicate source_outbox_id to be rejected';
    END IF;
END $$;

INSERT INTO audit_event(
    audit_id,
    event_type,
    actor,
    object_kind,
    object_id,
    payload_json,
    correlation_id,
    causation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000408',
    'scheduler_job_succeeded',
    'postgres-worker',
    'scheduler_job',
    '018f0000-0000-7000-8000-000000000402',
    '{"attempt_id":"018f0000-0000-7000-8000-000000000406"}'::jsonb,
    '018f0000-0000-7000-8000-000000000403',
    '018f0000-0000-7000-8000-000000000405'
);

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*) INTO matched
      FROM scheduler_job j
      JOIN audit_event a ON a.correlation_id = j.correlation_id
     WHERE j.job_id = '018f0000-0000-7000-8000-000000000402'
       AND a.object_kind = 'scheduler_job';
    IF matched <> 1 THEN
        RAISE EXCEPTION 'expected scheduler job audit correlation';
    END IF;
END $$;

ROLLBACK;

\echo GOAL-03 PostgreSQL verification passed
