# GOAL-RUNTIME-POSTGRES-GATE-01 Validation Report

status: COMPLETED

## Runtime
- PostgreSQL version: PostgreSQL 16.14 on x86_64-pc-linux-musl.
- Isolation: disposable Docker container using `postgres:16-alpine`, no host port exposure.
- New system software installed: no.
- Docker image preexisting before this run: yes.
- Disposable database: `goal_runtime_postgres_gate_01`.
- Test database destroyed: yes.
- Container removed: yes.

## Schema and migration
- Schema chain executed from an empty PostgreSQL database.
- Schema chain executed twice to verify repeat safety.
- Formal public tables after initialization: 20.
- Formal rows after initialization: 0.
- JSONB, timestamptz, foreign key, unique, index, status check, immutable trigger and trace-current-version constraints verified.

## Runtime probe gate
- End-to-end runtime_probe PostgreSQL loop: passed.
- Core API equivalent job creation and idempotency receipt: passed.
- Worker atomic claim with `FOR UPDATE SKIP LOCKED`: passed.
- Two-session concurrent claim: passed; worker B claimed 0 rows while worker A held the row lock.
- Lease expiry and recovery: passed.
- Fake model-port failure retry path: passed.
- Output schema error path: passed; no formal result or outbox event created.
- Retry after failure: passed; exactly one result and one outbox event created.
- Materializer failure rollback: passed; result and outbox counts unchanged.
- Runtime result immutability: passed.
- Outbox topic/causation dedupe: passed.

## Data isolation
- Old accounts, videos, comments, ASR, DNA, topics, drafts, cold backups and production fixtures used: none.
- External LLM calls: none.
- Codex or Claude CLI as model backend: none.
- Feishu calls: none.
- MediaCrawler or ASR runs: none.
- Legacy data imports: none.
- Clean-room formal SQLite database after gate: 20 tables, 0 total rows.

## Commands
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\core\runtime\run_goal_runtime_postgres_gate_01.ps1`
- `python -m unittest tests.core.test_runtime_vertical_slice`
- `python -m py_compile scripts\core\runtime\goal_runtime_vertical_slice.py scripts\core\persistence\goal01_store.py`
- clean-room health check via `scripts.validation.clean_room_empty_db.health_check`

## Git
- Branch: `validation/goal-runtime-postgres-gate-01-v0.6.2`
- Base commit: `6b329c6`
- Validated code commit: pending
- Final HEAD/status: pending final commit

## Next goal
Recommended next goal: wait for user approval before starting any successor goal.
