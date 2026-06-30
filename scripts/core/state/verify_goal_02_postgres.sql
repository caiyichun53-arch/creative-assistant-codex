\echo GOAL-02 PostgreSQL verification started

BEGIN;

INSERT INTO core_permission(actor, command_type)
VALUES
    ('core_tester', 'create_state'),
    ('core_tester', 'transition_state')
ON CONFLICT DO NOTHING;

INSERT INTO trace_root(root_id, object_kind)
VALUES
    ('018f0000-0000-7000-8000-000000000201', 'production_task'),
    ('018f0000-0000-7000-8000-000000000202', 'topic'),
    ('018f0000-0000-7000-8000-000000000203', 'claim'),
    ('018f0000-0000-7000-8000-000000000204', 'experiment'),
    ('018f0000-0000-7000-8000-000000000205', 'tactic');

INSERT INTO trace_version(
    version_id,
    root_id,
    version_no,
    content_hash,
    projection_version,
    payload_json
) VALUES
    ('018f0000-0000-7000-8000-000000000211', '018f0000-0000-7000-8000-000000000201', 1, 'sha256:prod', 'goal02.v1', '{"state":"draft"}'::jsonb),
    ('018f0000-0000-7000-8000-000000000212', '018f0000-0000-7000-8000-000000000202', 1, 'sha256:topic', 'goal02.v1', '{"state":"candidate"}'::jsonb),
    ('018f0000-0000-7000-8000-000000000213', '018f0000-0000-7000-8000-000000000203', 1, 'sha256:claim', 'goal02.v1', '{"state":"proposed"}'::jsonb),
    ('018f0000-0000-7000-8000-000000000214', '018f0000-0000-7000-8000-000000000204', 1, 'sha256:experiment', 'goal02.v1', '{"state":"planned"}'::jsonb),
    ('018f0000-0000-7000-8000-000000000215', '018f0000-0000-7000-8000-000000000205', 1, 'sha256:tactic', 'goal02.v1', '{"state":"candidate"}'::jsonb);

INSERT INTO command_receipt(
    receipt_id,
    command_scope,
    idempotency_key,
    request_hash,
    result_json,
    correlation_id,
    status
) VALUES (
    '018f0000-0000-7000-8000-000000000221',
    'goal02.core',
    'postgres-create-states',
    'sha256:req-create',
    '{"ok":true}'::jsonb,
    '018f0000-0000-7000-8000-000000000222',
    'succeeded'
);

INSERT INTO core_command_envelope(
    command_id,
    command_type,
    actor,
    object_kind,
    object_id,
    idempotency_key,
    payload_json,
    correlation_id,
    command_receipt_id,
    status
) VALUES (
    '018f0000-0000-7000-8000-000000000223',
    'create_state',
    'core_tester',
    'topic',
    '018f0000-0000-7000-8000-000000000202',
    'postgres-create-states',
    '{"state":"candidate"}'::jsonb,
    '018f0000-0000-7000-8000-000000000222',
    '018f0000-0000-7000-8000-000000000221',
    'succeeded'
);

INSERT INTO production_task_state(production_task_id, state, state_rank, basis_version_id, updated_by_command_id)
VALUES ('018f0000-0000-7000-8000-000000000201', 'draft', 0, '018f0000-0000-7000-8000-000000000211', '018f0000-0000-7000-8000-000000000223');

INSERT INTO topic_state(topic_id, state, state_rank, basis_version_id, updated_by_command_id)
VALUES ('018f0000-0000-7000-8000-000000000202', 'candidate', 0, '018f0000-0000-7000-8000-000000000212', '018f0000-0000-7000-8000-000000000223');

INSERT INTO claim_state(claim_id, state, state_rank, basis_version_id, updated_by_command_id)
VALUES ('018f0000-0000-7000-8000-000000000203', 'proposed', 0, '018f0000-0000-7000-8000-000000000213', '018f0000-0000-7000-8000-000000000223');

INSERT INTO experiment_state(experiment_id, state, state_rank, basis_version_id, updated_by_command_id)
VALUES ('018f0000-0000-7000-8000-000000000204', 'planned', 0, '018f0000-0000-7000-8000-000000000214', '018f0000-0000-7000-8000-000000000223');

INSERT INTO tactic_state(tactic_id, state, state_rank, basis_version_id, updated_by_command_id)
VALUES ('018f0000-0000-7000-8000-000000000205', 'candidate', 0, '018f0000-0000-7000-8000-000000000215', '018f0000-0000-7000-8000-000000000223');

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*) INTO matched FROM production_task_state;
    IF matched <> 1 THEN RAISE EXCEPTION 'expected production_task_state'; END IF;
    SELECT count(*) INTO matched FROM topic_state;
    IF matched <> 1 THEN RAISE EXCEPTION 'expected topic_state'; END IF;
    SELECT count(*) INTO matched FROM claim_state;
    IF matched <> 1 THEN RAISE EXCEPTION 'expected claim_state'; END IF;
    SELECT count(*) INTO matched FROM experiment_state;
    IF matched <> 1 THEN RAISE EXCEPTION 'expected experiment_state'; END IF;
    SELECT count(*) INTO matched FROM tactic_state;
    IF matched <> 1 THEN RAISE EXCEPTION 'expected tactic_state'; END IF;
END $$;

UPDATE topic_state
   SET state = 'selected',
       state_rank = 10
 WHERE topic_id = '018f0000-0000-7000-8000-000000000202';

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE topic_state
           SET state = 'candidate',
               state_rank = 0
         WHERE topic_id = '018f0000-0000-7000-8000-000000000202';
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected topic_state backward transition to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE core_command_envelope
           SET status = 'failed'
         WHERE command_id = '018f0000-0000-7000-8000-000000000223';
    EXCEPTION WHEN others THEN
        failed := true;
    END;
    IF NOT failed THEN
        RAISE EXCEPTION 'expected command envelope update to be rejected';
    END IF;
END $$;

INSERT INTO audit_event(
    audit_id,
    event_type,
    actor,
    object_kind,
    object_id,
    version_id,
    payload_json,
    correlation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000231',
    'core_state_materialized',
    'core_tester',
    'topic',
    '018f0000-0000-7000-8000-000000000202',
    '018f0000-0000-7000-8000-000000000212',
    '{"state":"candidate"}'::jsonb,
    '018f0000-0000-7000-8000-000000000222'
);

INSERT INTO outbox_message(
    outbox_id,
    topic,
    payload_json,
    correlation_id,
    causation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000232',
    'core.state.changed',
    '{"object_kind":"topic"}'::jsonb,
    '018f0000-0000-7000-8000-000000000222',
    '018f0000-0000-7000-8000-000000000231'
);

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*)
      INTO matched
      FROM core_command_envelope c
      JOIN audit_event a ON a.correlation_id = c.correlation_id
      JOIN outbox_message o ON o.correlation_id = c.correlation_id
                            AND o.causation_id = a.audit_id
     WHERE c.command_id = '018f0000-0000-7000-8000-000000000223';

    IF matched <> 1 THEN
        RAISE EXCEPTION 'expected command/audit/outbox correlation';
    END IF;
END $$;

ROLLBACK;

\echo GOAL-02 PostgreSQL verification passed
