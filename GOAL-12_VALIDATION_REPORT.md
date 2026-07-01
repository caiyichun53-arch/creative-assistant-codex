# GOAL-12 Validation Report

status: `GOAL-12_COMPLETE_WITH_EXTERNAL_LIVE_GATES`

branch: `codex/goal-12-v0.6.2`

validated_code_commit: `a9a28ec`

## Scope

- GOAL-12 End-to-End Staging only.
- Full local staging chain, replay, fault injection, FakeClock recovery, backup/restore, clean-room deletion, forced interruption/resume and continuous-run fixture.
- No GOAL-13.
- No real credentials or irreversible external operations.

## Validation Evidence

- `python scripts/core/staging/verify_goal_12.py`
  - exit_code: 0
  - coverage:
    - Hermes/Feishu thin inbound binding maps to Core only.
    - Production Core state transitions and Materializer pass with audit, outbox and idempotency.
    - Formal research workflow runs through Scheduler and RuntimeHost.
    - ModelGateway and PortableSkillRunner run with a fake no-I/O provider.
    - Research evidence, content plan, script, review, approval, approved draft and publication capture form a traceable version chain.
    - Experiment result, preference candidate, inferred preference candidate and experience proposal stay separated.
    - Correction registration, propagation, job retry and report generation pass.
    - Replay returns original results without duplicate side effects.
    - Stale-basis rejection is durable.
    - Injected materialization fault rolls back partial script side effects.
    - SQLite backup/restore preserves required trace versions.
    - Clean-room deletion of validation and candidate assets does not affect production startup or formal skill allowlist loading.
    - Forced interruption via expired lease recovers with FakeClock.
    - Continuous-run fixture dispatches five deterministic jobs.
    - Boundary review finds no external I/O, no new business table and no candidate loader.
- `python -m py_compile scripts/core/staging/__init__.py scripts/core/staging/verify_goal_12.py`
  - exit_code: 0
- `python scripts/core/persistence/verify_goal_01.py`
  - exit_code: 0
- `python scripts/core/state/verify_goal_02.py`
  - exit_code: 0
- `python scripts/core/scheduler/verify_goal_03.py`
  - exit_code: 0
- `python scripts/core/runtime/verify_goal_04.py`
  - exit_code: 0
- `python scripts/core/workflow/verify_goal_05.py`
  - exit_code: 0
- `python scripts/core/research/verify_goal_06.py`
  - exit_code: 0
- `python scripts/core/model_gateway/verify_goal_07.py`
  - exit_code: 0
- `python scripts/core/production/verify_goal_08.py`
  - exit_code: 0
- `python scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
- `python scripts/core/correction/verify_goal_10.py`
  - exit_code: 0
- `python scripts/core/hermes/verify_goal_11.py`
  - exit_code: 0
- `git diff --check`
  - exit_code: 0

## Checkpoint Results

- Checkpoint 1: PASS - GOAL-12 control package restored from direct V0.6.2 references.
- Checkpoint 2: PASS - staging verifier added using production handlers.
- Checkpoint 3: PASS - end-to-end staging path completed.
- Checkpoint 4: PASS - replay, fault injection, FakeClock recovery and continuous-run fixture completed.
- Checkpoint 5: PASS - backup/restore and clean-room deletion completed.
- Checkpoint 6: PASS - regression smoke GOAL-01 through GOAL-11 completed.
- Checkpoint 7: PASS - validation report, clean-room proof and final acceptance report produced.

## External Live Gate

- Real Hermes/Feishu credentials, permissions, attachment behavior, active reply path and live recovery are external.
- Live SearchProvider, Fetcher/Extractor, ASR, model/provider and platform adapter shadow validation are external.
- Formal 14-day production continuous-run with production credentials is external.

## Real Blockers

- None for local forced gates.
