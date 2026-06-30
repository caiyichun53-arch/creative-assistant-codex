CREATE TABLE IF NOT EXISTS scheduler_job (
    job_id             uuid PRIMARY KEY,
    job_kind           text NOT NULL,
    status             text NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'dead')),
    priority           integer NOT NULL DEFAULT 0,
    run_after          timestamptz NOT NULL,
    max_attempts       integer NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
    attempt_count      integer NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    current_attempt_id uuid,
    lease_owner        text,
    lease_expires_at   timestamptz,
    payload_json       jsonb NOT NULL,
    source_outbox_id   uuid REFERENCES outbox_message(outbox_id) ON DELETE RESTRICT,
    correlation_id     uuid NOT NULL,
    causation_id       uuid,
    created_by_receipt_id uuid NOT NULL REFERENCES command_receipt(receipt_id) ON DELETE RESTRICT,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS unique_scheduler_job_source_outbox
ON scheduler_job(source_outbox_id)
WHERE source_outbox_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS scheduler_job_claimable_idx
ON scheduler_job(status, run_after, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS scheduler_job_attempt (
    attempt_id       uuid PRIMARY KEY,
    job_id           uuid NOT NULL REFERENCES scheduler_job(job_id) ON DELETE RESTRICT,
    attempt_no       integer NOT NULL CHECK(attempt_no > 0),
    worker_id        text NOT NULL,
    status           text NOT NULL CHECK(status IN ('leased', 'succeeded', 'failed', 'cancelled')),
    started_at       timestamptz NOT NULL,
    heartbeat_at     timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    finished_at      timestamptz,
    error_json       jsonb,
    correlation_id   uuid NOT NULL,
    causation_id     uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id, attempt_no)
);

CREATE OR REPLACE FUNCTION reject_goal03_attempt_count_decrease() RETURNS trigger AS $$
BEGIN
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'scheduler_job attempt_count cannot decrease';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_goal03_terminal_job_update() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('succeeded', 'failed', 'cancelled', 'dead') THEN
        RAISE EXCEPTION 'terminal scheduler_job is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_goal03_terminal_attempt_update() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('succeeded', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'terminal scheduler_job_attempt is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS scheduler_job_attempt_count_no_decrease ON scheduler_job;
CREATE TRIGGER scheduler_job_attempt_count_no_decrease
BEFORE UPDATE ON scheduler_job
FOR EACH ROW EXECUTE FUNCTION reject_goal03_attempt_count_decrease();

DROP TRIGGER IF EXISTS scheduler_job_terminal_immutable ON scheduler_job;
CREATE TRIGGER scheduler_job_terminal_immutable
BEFORE UPDATE ON scheduler_job
FOR EACH ROW EXECUTE FUNCTION reject_goal03_terminal_job_update();

DROP TRIGGER IF EXISTS scheduler_job_attempt_terminal_immutable ON scheduler_job_attempt;
CREATE TRIGGER scheduler_job_attempt_terminal_immutable
BEFORE UPDATE ON scheduler_job_attempt
FOR EACH ROW EXECUTE FUNCTION reject_goal03_terminal_attempt_update();
