# Dead Goal01-12 chain — archived 2026-07-09

Moved here by a "清场式保留重构" pass, per user instruction, after an implementation-credibility
audit found the repo held two parallel architectures that had never been reconciled:

1. A lightweight layer (`scripts/core/business_data/`, `scripts/core/experience/`) that is
   actually running on real production data.
2. A "formal" Goal01-12 pipeline (`persistence`/`scheduler`/`production`/`correction`/`workflow`/
   `host`/`hermes`/`state`/`research`) that was only ever exercised by its own `verify_goal_*.py`
   self-tests, with zero real production rows (`clean_room_formal_db.total_rows: 0` in
   `MODEL_GATE_STATUS.yaml`).

**How the split was decided**: not by filename or by trusting prior audit prose, but by computing
the actual transitive import closure starting from the real production entrypoints
(`run_competitor_registration_full.py`, `run_reverse_prep.py`, `run_source_to_topic.py`,
`run_content_plan.py`, `run_script_generate.py`, `run_sample_deep_analyze.py`, `review_queue.py`,
`compare_asr_providers.py`). Anything reachable from those entrypoints was kept in place, even if
its filename suggested it was part of the old Goal-numbered family. Two modules survived this
check that a filename-only read would have wrongly archived:

- `scripts/core/persistence/goal01_store.py` — `content_hash`/`PersistenceStore` are used inside
  `scripts/core/model_gateway/formal_skill_adapter.py`'s `make_*_harness()` factories, which power
  every real Skill binding.
- `scripts/core/scheduler/goal03_scheduler.py` — `Goal03Scheduler` is instantiated directly inside
  those same harness factories.
- `scripts/core/research/goal06_formal_research.py` and `scripts/core/workflow/goal05_workflow.py`
  — `scripts/core/external_adapters/goal_phase4_external_adapters.py` (which IS imported by
  `run_competitor_registration_full.py`/`run_reverse_prep.py`) imports dataclasses from
  `goal06_formal_research.py`, which in turn imports `Goal05WorkflowOrchestrator` from
  `goal05_workflow.py`. Both stayed in `scripts/core/`, not here.

## What's actually in this folder

- `scripts_core_correction/` — `goal10_corrections.py` (1402 lines) + `verify_goal_10.py`. Zero
  callers outside this pair and `scripts_core_staging/verify_goal_12.py`.
- `scripts_core_production/` — `goal08_production_chain.py` (defines `VersionRef`) + `verify_goal_08.py`.
- `scripts_core_host/` — `production_host.py`.
- `scripts_core_hermes/` — `goal11_host_binding.py`, `goal_phase6_whitelist_tool.py`, `verify_goal_11.py`.
- `scripts_core_state/` — `goal02_core.py` (`CoreCommandEnvelope`/`CoreMaterializer`) + its verify
  script and Postgres gate sidecars.
- `scripts_core_workflow/goal_phase5_business_workflow.py` — only the Phase-5 file; the sibling
  `scripts/core/workflow/goal05_workflow.py` stayed (it's real, see above).
- `scripts_core_staging/` — `verify_goal_12.py`, `verify_goal_v062_phase7.py`: self-certification
  scripts whose only subject is the modules above. `verify_goal_v062_phase8_readiness.py` (used by
  the real `preflight_checkpoint_check.py` gate) stayed in `scripts/core/staging/`.
- `scripts_core_scheduler/verify_goal_03.py` — this one test script imported the now-archived
  `state/goal02_core.py`; `scheduler/goal03_scheduler.py` itself (real) is unaffected and stayed.
- `tests_core/` — four pytest files (`test_phase5_business_workflow.py`,
  `test_phase6_hermes_whitelist_tool.py`, `test_phase7_synthetic_acceptance.py`,
  `test_production_host_boundary.py`) that exclusively exercised the modules above. They are no
  longer collected by `pytest tests/core tests/validation`.

**Known coverage gap this move creates**: `test_phase5_business_workflow.py` had one test
(`test_frozen_workflow_steps_enqueue_idempotently_through_goal05_orchestrator`) that incidentally
covered the real `Goal05WorkflowOrchestrator`. That coverage is gone with this move. If
`goal05_workflow.py` needs test coverage again, write a new, narrowly-scoped test against the
current real code — do not resurrect this file.

## Rules for this archive

- Nothing under `scripts/`, `runtime_skills/`, `tests/`, or any active `.claude/skills/`/
  `.agents/skills/`/`config/*.yaml` may import from `archive/`. If you find a new reference back
  into this folder, that is a regression — remove the reference, don't un-archive the code.
- This code is kept for history only. It is not run, not tested, not imported, and not a design
  reference for new work — see `BUSINESS_RULE_CATALOG.yaml`/`REQUIREMENT_CODE_TRACEABILITY.yaml`
  for the current design authority instead.
- If a future need genuinely requires reviving something here (e.g. `goal10_corrections.py`'s
  richer correction/provenance model), that is an explicit, separate architectural decision for
  the user to make — not something to silently re-import.
