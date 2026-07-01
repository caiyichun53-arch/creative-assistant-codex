# GOAL-11 ExecPlan

Goal: GOAL-11 Hermes Host Binding and Feishu Thin Interaction

## Spec Basis

- `goals/GOAL-11.md`, restored because no formal GOAL-11 file was present.
- User-approved GOAL-11 entry prompt.
- GOAL-10 delivery interface: correction propagation uses existing Core/Materializer, command receipt, audit, outbox and Scheduler idempotency; GOAL-11 was not entered.
- GOAL-02 Core command envelope and Materializer.
- GOAL-03 Scheduler/Worker retry foundation.
- GOAL-07 Portable Skill and Host Binding separation.

## Scope

Build the local Hermes production-host binding contract and Feishu thin-interaction adapter contract. Use fake/fixture/replay validation only in this round.

## Non-goals

- No GOAL-12.
- No live Hermes process, live Feishu listener or external credentials.
- No Feishu-owned business state.
- No Hermes direct writes to formal state tables.
- No independent Agent Runtime or new middleware.
- No changes to Portable Skill semantics.

## Files

Expected files are limited to GOAL-11 direct scope:

- `goals/GOAL-11.md`
- `implementation_progress/GOAL-11.md`
- `GOAL-11_EXEC_PLAN.md`
- `scripts/core/hermes/__init__.py`
- `scripts/core/hermes/goal11_host_binding.py`
- `scripts/core/hermes/verify_goal_11.py`

## Migrations

No migration changes are planned in checkpoints 1-2. Later checkpoints may only add schema if command/message receipt state cannot be represented safely by existing GOAL-01/02/03 primitives.

## Atomic Checkpoints

1. Restore formal GOAL-11 control package.
   - Create `goals/GOAL-11.md`.
   - Create `implementation_progress/GOAL-11.md`.
   - Create this ExecPlan.
   - Record missing V0.6.2 source as a recovery note, not a blocker.

2. Add fixture-first Hermes/Feishu binding contracts and minimal local bridge.
   - Fake Feishu binding maps external events into Hermes messages only.
   - Hermes bridge translates messages into `CoreCommandEnvelope`.
   - Formal state changes happen only through `CoreMaterializer.execute`.
   - Duplicate events replay through Core receipts.
   - Changed payload with the same idempotency key is rejected.

3. Add explicit host message receipt, response/outbox and replay records.
   - Add traceable host message receipt without duplicating Core command receipt authority.
   - Record response routing metadata for Feishu without live I/O.

4. Add Scheduler/Worker retry and recovery coverage for Hermes temporary offline behavior.
   - Ensure queued formal jobs continue through GOAL-03/04 Worker primitives.
   - Recover host response jobs without duplicate side effects.

5. Add Feishu response binding replay/fault coverage without live external I/O.
   - Fake Feishu response dispatch is idempotent.
   - Injected send failures recover through Scheduler.

6. Add validation report and clean-room closeout.
   - Prove no live external credentials or GOAL-12 bindings.
   - Run GOAL-11 scoped validation and direct predecessor gates.

## Minimum Test Per Checkpoint

- Checkpoint 1: `git diff --check`.
- Checkpoint 2: `python scripts/core/hermes/verify_goal_11.py`, py_compile for GOAL-11 files, `git diff --check`.
- Later checkpoints: GOAL-11 scoped fake/replay/fault tests and directly related predecessor gates only.

## Validation Modes

Use fixtures, fakes, contract tests, replay checks and fault injection. Real Hermes and real Feishu are external live gates only and are not local core blockers.

## Stop Conditions

- A new business state contract is required beyond GOAL-11.
- A real external credential or irreversible external operation is required.
- Existing Core/Materializer contracts conflict with GOAL-11 host binding requirements.
- Any implementation requires GOAL-12.

## Resume Entry

Start from the first unfinished checkpoint in `implementation_progress/GOAL-11.md`.

## Results

- Checkpoint 1 restored the GOAL-11 control package.
- Checkpoint 2 added the local Hermes/Feishu fake binding contract and replay tests.
