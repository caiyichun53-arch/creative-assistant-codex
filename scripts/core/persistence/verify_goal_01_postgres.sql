\echo GOAL-01 PostgreSQL verification started

BEGIN;

INSERT INTO trace_root(root_id, object_kind)
VALUES
    ('018f0000-0000-7000-8000-000000000001', 'script'),
    ('018f0000-0000-7000-8000-000000000002', 'script');

INSERT INTO trace_version(
    version_id,
    root_id,
    version_no,
    based_on_version_id,
    blob_hash,
    content_hash,
    business_hash,
    projection_version,
    payload_json
) VALUES
    (
        '018f0000-0000-7000-8000-000000000011',
        '018f0000-0000-7000-8000-000000000001',
        1,
        NULL,
        'sha256:blob-a',
        'sha256:a',
        'sha256:business-a',
        'goal01.v1',
        '{"text":"a"}'::jsonb
    ),
    (
        '018f0000-0000-7000-8000-000000000012',
        '018f0000-0000-7000-8000-000000000002',
        1,
        NULL,
        NULL,
        'sha256:b',
        NULL,
        'goal01.v1',
        '{"text":"b"}'::jsonb
    );

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*)
      INTO matched
      FROM trace_version
     WHERE version_id = '018f0000-0000-7000-8000-000000000011'
       AND blob_hash = 'sha256:blob-a'
       AND content_hash = 'sha256:a'
       AND business_hash = 'sha256:business-a';

    IF matched <> 1 THEN
        RAISE EXCEPTION 'expected blob/content/business hashes';
    END IF;
END $$;

INSERT INTO object_reference(
    reference_id,
    source_version_id,
    relation_role,
    target_object_kind,
    target_stable_id,
    target_version_id,
    target_content_hash,
    locator_json
) VALUES (
    '018f0000-0000-7000-8000-000000000013',
    '018f0000-0000-7000-8000-000000000011',
    'uses_source',
    'evidence',
    '018f0000-0000-7000-8000-000000000002',
    '018f0000-0000-7000-8000-000000000012',
    'sha256:b',
    '{"local_ref":"source_1"}'::jsonb
);

INSERT INTO binding_manifest(
    manifest_id,
    source_version_id,
    local_ref,
    object_ref_json,
    before_hash,
    after_hash
) VALUES (
    '018f0000-0000-7000-8000-000000000014',
    '018f0000-0000-7000-8000-000000000011',
    'source_1',
    '{"reference_id":"018f0000-0000-7000-8000-000000000013"}'::jsonb,
    'sha256:before',
    'sha256:after'
);

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE object_reference
           SET relation_role = 'changed'
         WHERE reference_id = '018f0000-0000-7000-8000-000000000013';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected object_reference update to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        DELETE FROM binding_manifest
         WHERE manifest_id = '018f0000-0000-7000-8000-000000000014';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected binding_manifest delete to be rejected';
    END IF;
END $$;

UPDATE trace_root
   SET current_version_id = '018f0000-0000-7000-8000-000000000011'
 WHERE root_id = '018f0000-0000-7000-8000-000000000001';

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE trace_version
           SET payload_json = '{"text":"changed"}'::jsonb
         WHERE version_id = '018f0000-0000-7000-8000-000000000011';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected trace_version update to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        DELETE FROM trace_version
         WHERE version_id = '018f0000-0000-7000-8000-000000000011';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected trace_version delete to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE trace_root
           SET current_version_id = '018f0000-0000-7000-8000-000000000012'
         WHERE root_id = '018f0000-0000-7000-8000-000000000001';
        SET CONSTRAINTS trace_root_current_version_same_root IMMEDIATE;
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    SET CONSTRAINTS trace_root_current_version_same_root DEFERRED;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected cross-root current version pointer to be rejected';
    END IF;
END $$;

INSERT INTO command_receipt(
    receipt_id,
    command_scope,
    idempotency_key,
    request_hash,
    result_json,
    correlation_id,
    status
) VALUES (
    '018f0000-0000-7000-8000-000000000021',
    'goal01.verify',
    'same-key',
    'sha256:req-a',
    '{"ok":true}'::jsonb,
    '018f0000-0000-7000-8000-000000000022',
    'succeeded'
);

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        INSERT INTO command_receipt(
            receipt_id,
            command_scope,
            idempotency_key,
            request_hash,
            result_json,
            correlation_id,
            status
        ) VALUES (
            '018f0000-0000-7000-8000-000000000023',
            'goal01.verify',
            'same-key',
            'sha256:req-b',
            '{"ok":true}'::jsonb,
            '018f0000-0000-7000-8000-000000000024',
            'succeeded'
        );
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected duplicate command idempotency key to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE command_receipt
           SET status = 'failed'
         WHERE receipt_id = '018f0000-0000-7000-8000-000000000021';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected command_receipt update to be rejected';
    END IF;
END $$;

INSERT INTO content_preference_profile(profile_id, scope_type, scope_id)
VALUES
    ('018f0000-0000-7000-8000-000000000031', 'global', NULL),
    ('018f0000-0000-7000-8000-000000000032', 'domain', '018f0000-0000-7000-8000-000000000033');

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        INSERT INTO content_preference_profile(profile_id, scope_type, scope_id)
        VALUES ('018f0000-0000-7000-8000-000000000034', 'global', NULL);
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected duplicate global preference profile to be rejected';
    END IF;
END $$;

INSERT INTO content_preference_revision(
    revision_id,
    profile_id,
    revision_no,
    status,
    preference_payload,
    evidence_refs,
    origin,
    content_hash
) VALUES
    (
        '018f0000-0000-7000-8000-000000000041',
        '018f0000-0000-7000-8000-000000000031',
        1,
        'candidate',
        '{"voice":"plain spoken"}'::jsonb,
        '[{"kind":"human_edit","id":"sample-1"}]'::jsonb,
        'human_edit',
        'sha256:pref-candidate'
    ),
    (
        '018f0000-0000-7000-8000-000000000042',
        '018f0000-0000-7000-8000-000000000031',
        2,
        'published',
        '{"voice":"plain spoken"}'::jsonb,
        '[{"kind":"explicit_instruction","id":"sample-2"}]'::jsonb,
        'explicit_instruction',
        'sha256:pref-published'
    ),
    (
        '018f0000-0000-7000-8000-000000000043',
        '018f0000-0000-7000-8000-000000000032',
        1,
        'published',
        '{"voice":"different"}'::jsonb,
        '[]'::jsonb,
        'explicit_instruction',
        'sha256:pref-foreign'
    );

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE content_preference_profile
           SET current_revision_id = '018f0000-0000-7000-8000-000000000041'
         WHERE profile_id = '018f0000-0000-7000-8000-000000000031';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected candidate preference current pointer to be rejected';
    END IF;
END $$;

DO $$
DECLARE
    failed boolean := false;
BEGIN
    BEGIN
        UPDATE content_preference_profile
           SET current_revision_id = '018f0000-0000-7000-8000-000000000043'
         WHERE profile_id = '018f0000-0000-7000-8000-000000000031';
    EXCEPTION WHEN others THEN
        failed := true;
    END;

    IF NOT failed THEN
        RAISE EXCEPTION 'expected cross-profile preference current pointer to be rejected';
    END IF;
END $$;

UPDATE content_preference_profile
   SET current_revision_id = '018f0000-0000-7000-8000-000000000042'
 WHERE profile_id = '018f0000-0000-7000-8000-000000000031';

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
    '018f0000-0000-7000-8000-000000000051',
    'version_created',
    'goal01_verify',
    'script',
    '018f0000-0000-7000-8000-000000000001',
    '018f0000-0000-7000-8000-000000000011',
    '{"version":"018f0000-0000-7000-8000-000000000011"}'::jsonb,
    '018f0000-0000-7000-8000-000000000052'
);

INSERT INTO outbox_message(
    outbox_id,
    topic,
    payload_json,
    correlation_id,
    causation_id
) VALUES (
    '018f0000-0000-7000-8000-000000000053',
    'goal01.verify',
    '{"version":"018f0000-0000-7000-8000-000000000011"}'::jsonb,
    '018f0000-0000-7000-8000-000000000052',
    '018f0000-0000-7000-8000-000000000051'
);

DO $$
DECLARE
    matched integer;
BEGIN
    SELECT count(*)
      INTO matched
      FROM audit_event a
      JOIN outbox_message o
        ON o.correlation_id = a.correlation_id
       AND o.causation_id = a.audit_id
     WHERE a.audit_id = '018f0000-0000-7000-8000-000000000051'
       AND o.outbox_id = '018f0000-0000-7000-8000-000000000053';

    IF matched <> 1 THEN
        RAISE EXCEPTION 'expected audit/outbox correlation';
    END IF;
END $$;

ROLLBACK;

\echo GOAL-01 PostgreSQL verification passed
