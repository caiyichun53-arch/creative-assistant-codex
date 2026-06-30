# GOAL-01 — Persistence and Traceability

Source: V0.6.2, GOAL-01.

Scope:
- UUIDv7
- root/version model
- content/blob/business hashes
- typed object references
- immutable audit events
- command receipt
- outbox
- immutability gates
- `content_preference_profile` / immutable `content_preference_revision`

Non-scope:
- No Core API state machine.
- No production task/topic/claim/experiment/tactic states.
- No Hermes or Feishu runtime.
- No ModelGateway.
- No live external credentials.
- No mutation of legacy `data/creation.db`.

Acceptance:
- Versions cannot be overwritten or deleted.
- A current pointer cannot point to a version from another root.
- Command idempotency returns the same receipt for the same request and rejects a different request with the same key.
- A content preference `candidate` revision cannot become current; current can point only to a `published` revision from the same profile.

Implementation plan:
1. Add target PostgreSQL schema for GOAL-01 objects.
2. Add SQLite validation schema with equivalent constraints/triggers for local no-service verification.
3. Add a small persistence handler that exercises the same object rules and idempotency semantics.
4. Add a verification script that runs the acceptance checks against an isolated in-memory database.
5. Record validation evidence in `GOAL-01_VALIDATION_REPORT.md`.

Current validation mode:
- `psql`/PostgreSQL server is not available in PATH in the current environment.
- Docker is not a project dependency and is not part of the GOAL-01 route.
- PostgreSQL DDL is authored as the target contract; `verify_goal_01_postgres.sql` is the no-Docker PostgreSQL acceptance script for any disposable PostgreSQL instance.
- The SQLite schema remains the executable local proof when no PostgreSQL instance is available.
