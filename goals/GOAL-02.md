# GOAL-02 - Core State and Materializer

Source: V0.6.2, GOAL-02.

Scope:
- `production_task`, `topic`, `claim`, `experiment`, and `tactic` state.
- Core Command Envelope.
- Permission checks.
- Materializer transaction boundary.
- State transition validation.
- Stale basis rejection.
- Confirmation gate verification.

Non-scope:
- No Hermes or Feishu runtime.
- No Job/Scheduler/Worker implementation.
- No ModelGateway, LLM call, provider call or external credential.
- No production Skill runtime.
- No mutation of legacy `data/creation.db`.
- No GOAL-03 queue, lease, heartbeat, retry or cancel implementation.

Acceptance:
- State transitions are one-way and cannot move backward.
- Stale basis cannot activate or publish a state transition.
- Confirmation-required transitions fail without an explicit confirmation flag and are auditable.
- Permissions are checked before state mutation.
- Accepted commands produce command receipt, audit and outbox rows with correlation id.
- Failed commands do not partially materialize versions, pointers or state updates.

Implementation plan:
1. Add GOAL-02 SQLite validation schema and target PostgreSQL schema.
2. Add Core Command Envelope and Materializer handler.
3. Add fixture/replay/fault/FakeClock verification against the local handler.
4. Add PostgreSQL acceptance SQL and no-Docker runner wiring where practical.
5. Record validation evidence in `GOAL-02_VALIDATION_REPORT.md`.

Current validation mode:
- SQLite in-memory validation is the accepted executable local proof for GOAL-02 handler behavior at this stage.
- Target PostgreSQL DDL, acceptance SQL and no-Docker runner are authored.
- PostgreSQL runtime execution is deferred to the later environment gate by user decision.
