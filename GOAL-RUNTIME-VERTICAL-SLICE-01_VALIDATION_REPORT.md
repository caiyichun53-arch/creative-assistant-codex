# GOAL-RUNTIME-VERTICAL-SLICE-01 Validation Report

status: `VALIDATED`
updated_at: `2026-07-02T11:45:00Z`

## Summary

GOAL-RUNTIME-VERTICAL-SLICE-01 implements a clean-room runtime_probe vertical slice using synthetic input and a deterministic fake Model Port only. It does not migrate old data, call real models, start collection/ASR, send Feishu messages or use legacy DNA paths.

## Implementation Artifacts

- `RUNTIME_VERTICAL_SLICE_CONTRACT.yaml`
- `runtime_skills/runtime_probe/skill.yaml`
- `scripts/core/runtime/goal_runtime_vertical_slice.py`
- `scripts/core/runtime/goal_runtime_vertical_slice_schema.sqlite.sql`
- `scripts/core/runtime/goal_runtime_vertical_slice_schema.postgres.sql`
- `scripts/core/runtime/verify_runtime_vertical_slice_postgres.sql`
- `scripts/core/runtime/run_runtime_vertical_slice_postgres_gate.ps1`
- `tests/core/test_runtime_vertical_slice.py`

## Component Status

- Core API: implemented as `RuntimeProbeCoreAPI` with health, readiness, create job, query job, query result and query outbox.
- Job/Worker: implemented through `Goal03Scheduler` plus `RuntimeProbeWorker`; supports persisted queue, lease, retry, dead-letter and lease recovery.
- Input Binding: implemented as `RuntimeProbeInputBinding`; exposes only public runtime_probe fields.
- Portable Skill: `runtime_probe`, synthetic runtime-only package with input/output contract.
- Runner: implemented as `RuntimeProbeRunner`; loads skill package, validates input and output, calls ModelGateway.
- ModelGateway: uses existing GOAL-07 gateway with deterministic `runtime_probe_test_port`; no real provider calls.
- Materializer: implemented as `RuntimeProbeMaterializer`; writes immutable result version, skill run record, result index and outbox event only on success.
- Outbox: event is recorded in local store and never externally dispatched.

## Validation Results

- `python -m unittest tests.core.test_runtime_vertical_slice tests.validation.test_clean_room_readiness`: 21 tests passed.
- `python scripts/validation/clean_room_empty_db.py --health`: passed; 20 formal local validation tables, all 0 rows.
- `python scripts/validation/production_startup_smoke.py --require-legacy-absent`: passed.
- `python scripts/validation/clean_room_readiness.py --require-safe`: passed.
- `python scripts/project_check.py`: passed.
- YAML contract parse: passed.
- Runtime grep for forbidden legacy/model paths: no real model CLI, old DB, cold backup, MediaCrawler, ASR, Feishu or DNA usage in the runtime slice.

## PostgreSQL Gate

- Formal PostgreSQL schema file exists: `scripts/core/runtime/goal_runtime_vertical_slice_schema.postgres.sql`.
- PostgreSQL verification SQL exists: `scripts/core/runtime/verify_runtime_vertical_slice_postgres.sql`.
- Gate dry-run passed with the schema chain in order.
- Real PostgreSQL execution was not run because `psql` is not available in PATH. The gate fails closed with exit code 1/2 and remains an external environment gate.

## Clean-Room Data

- Runtime tests use in-memory SQLite stores only.
- `data/formal/clean_room_v0_6_2.sqlite3` remains empty: all formal tables are 0 rows after tests.
- Production mode rejects fixture loading.
- Old runtime paths are absent from production smoke.
