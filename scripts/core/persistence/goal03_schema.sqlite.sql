PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scheduler_job (
    job_id             TEXT PRIMARY KEY,
    job_kind           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'dead')),
    priority           INTEGER NOT NULL DEFAULT 0,
    run_after          TEXT NOT NULL,
    max_attempts       INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
    attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    current_attempt_id TEXT,
    lease_owner        TEXT,
    lease_expires_at   TEXT,
    payload_json       TEXT NOT NULL,
    source_outbox_id   TEXT,
    correlation_id     TEXT NOT NULL,
    causation_id       TEXT,
    created_by_receipt_id TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(source_outbox_id) REFERENCES outbox_message(outbox_id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_receipt_id) REFERENCES command_receipt(receipt_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS unique_scheduler_job_source_outbox
ON scheduler_job(source_outbox_id)
WHERE source_outbox_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS scheduler_job_claimable_idx
ON scheduler_job(status, run_after, priority, created_at);

CREATE TABLE IF NOT EXISTS scheduler_job_attempt (
    attempt_id       TEXT PRIMARY KEY,
    job_id           TEXT NOT NULL,
    attempt_no       INTEGER NOT NULL CHECK(attempt_no > 0),
    worker_id        TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('leased', 'succeeded', 'failed', 'cancelled')),
    started_at       TEXT NOT NULL,
    heartbeat_at     TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    finished_at      TEXT,
    error_json       TEXT,
    correlation_id   TEXT NOT NULL,
    causation_id     TEXT,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(job_id, attempt_no),
    FOREIGN KEY(job_id) REFERENCES scheduler_job(job_id) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS scheduler_job_attempt_count_no_decrease
BEFORE UPDATE ON scheduler_job
WHEN NEW.attempt_count < OLD.attempt_count
BEGIN
    SELECT RAISE(ABORT, 'scheduler_job attempt_count cannot decrease');
END;

CREATE TRIGGER IF NOT EXISTS scheduler_job_terminal_immutable
BEFORE UPDATE ON scheduler_job
WHEN OLD.status IN ('succeeded', 'failed', 'cancelled', 'dead')
BEGIN
    SELECT RAISE(ABORT, 'terminal scheduler_job is immutable');
END;

CREATE TRIGGER IF NOT EXISTS scheduler_job_attempt_terminal_immutable
BEFORE UPDATE ON scheduler_job_attempt
WHEN OLD.status IN ('succeeded', 'failed', 'cancelled')
BEGIN
    SELECT RAISE(ABORT, 'terminal scheduler_job_attempt is immutable');
END;
