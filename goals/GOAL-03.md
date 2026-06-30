# GOAL-03 - Scheduler, Job and Worker Lease

Source: V0.6.2, GOAL-03 inferred from the accepted GOAL-02 boundary and the V0.6.2 audit categories for Scheduler, Job, Worker, retry, recovery and idempotency.

Scope:
- Scheduler job persistence.
- Worker lease and heartbeat.
- Job attempt tracking.
- Retry and dead-letter handling.
- Cancellation gate.
- Expired lease recovery.
- Outbox-to-job scheduling bridge.
- Command receipt idempotency for enqueue.

Non-scope:
- No Hermes or Feishu runtime.
- No external queue, Redis, Celery, Temporal or message broker.
- No ModelGateway, LLM call, provider call or external credential.
- No production Portable Skill execution.
- No mutation of legacy `data/creation.db`.
- No GOAL-04 runtime host, adapter or skill runner implementation.

Acceptance:
- Job enqueue is idempotent by command receipt.
- Worker can claim only queued and due jobs.
- Leased jobs cannot be claimed by another worker until failed, completed or recovered.
- Heartbeat extends the active lease.
- Completion marks job terminal and marks linked outbox sent.
- Failure retries until max attempts, then moves to dead-letter state.
- Cancellation prevents future claim and cancels linked pending outbox.
- Expired leases are recoverable and requeue or dead-letter deterministically.
- Terminal jobs and terminal attempts are immutable at the database gate.
- Failed enqueue transaction does not leave partial command receipts or jobs.

Implementation plan:
1. Add GOAL-03 SQLite validation schema and target PostgreSQL schema.
2. Add Scheduler/Job/Worker lease handler.
3. Add fixture/replay/fault/FakeClock verification against the local handler.
4. Add PostgreSQL acceptance SQL and no-Docker runner wiring.
5. Record validation evidence in `GOAL-03_VALIDATION_REPORT.md`.

Current validation mode:
- SQLite in-memory validation is the executable local proof for GOAL-03 handler behavior.
- Target PostgreSQL DDL, acceptance SQL and no-Docker runner are authored.
- PostgreSQL runtime execution still requires a later environment gate or explicit user acceptance of deferral for this stage.
