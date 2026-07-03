# GOAL-V0.6.2-LEGACY-REMOVAL-01

Status: COMPLETED

Current Phase: Legacy removal complete

Current branch: maintenance/goal-v0.6.2-legacy-removal-01

Engineering-ready baseline:

- Commit: `a1cd03e6dfa721c48cf5eb58937fad80993147ea`
- Tag: `v0.6.2-engineering-ready`
- Baseline status: `ENGINEERING_READY`
- Production activation: not started
- Business model: Mimo
- Production schedules: disabled

Governing sources read for this run:

- `AGENTS.md`
- `BUILD_PLAN.md`
- `C:\Users\15891\.codex\attachments\dede932a-fa57-4ce8-b816-4d745bd99f26\pasted-text-1.txt`

Unavailable governing sources:

- `target-architecture.md` - not found in repo search for this run
- `rebuild-direction.md` - not found in repo search for this run

Completed checkpoints:

- Confirmed current repository HEAD was `a1cd03e6dfa721c48cf5eb58937fad80993147ea`.
- Created annotated tag `v0.6.2-engineering-ready` with the required engineering-ready message.
- Created branch `maintenance/goal-v0.6.2-legacy-removal-01`.
- Generated `LEGACY_REMOVAL_INVENTORY.yaml` with 69 legacy executable/runtime items and required contract fields.
- Removed legacy active runtime paths from the repository index:
  - old analyze/collect/content/db/feishu/humanize/language_fuel/llm/music/research/reverse/setup/topics paths
  - `scripts/project_check.py`
  - `scripts/run_daily.py`
  - `tools/asr/*`
  - legacy `.bat` and `.vbs` launchers
- Added `scripts/validation/legacy_removal_gate.py`.
- Added `tests/validation/test_legacy_removal_gate.py`.
- Updated example config and docs so active examples point at formal V0.6.2 runtime boundaries.
- Updated adapter/test references away from removed legacy script entrypoints.
- Confirmed cold backup references are not runtime-reachable and remain only in historical approval/mapping files.

Safety constraints observed:

- Did not modify the completed `GOAL-V0.6.2-PRODUCTION-COMPLETION-01` commit.
- Did not activate production.
- Did not switch GPT.
- Did not call GPT or DeepSeek.
- Did not start real collection.
- Did not send real Feishu messages.
- Did not enable production scheduled tasks.
- Did not delete cold backups outside the repository.

Validation evidence:

- `python -m unittest tests.validation.test_legacy_removal_gate tests.validation.test_clean_room_readiness tests.validation.test_live_gates` - PASS, 31 tests.
- `python -m unittest tests.core.test_remaining_formal_skill_graph tests.core.test_business_route_registry tests.core.test_formal_skill_adapter tests.core.test_phase5_business_workflow tests.core.test_phase6_hermes_whitelist_tool tests.core.test_phase7_synthetic_acceptance tests.core.test_external_executor_adapters` - PASS, 89 tests.
- `python -m unittest tests.core.test_source_to_topic_skill tests.core.test_sample_deep_analyze_skill tests.core.test_tactic_extract_skill tests.core.test_research_evidence_extract_skill tests.core.test_production_research_plan_skill tests.core.test_content_plan_skill tests.core.test_script_generate_skill tests.core.test_script_review_skill tests.core.test_experiment_review_skill tests.core.test_experience_revision_propose_skill tests.core.test_phase8_engineering_readiness` - PASS, 85 tests.
- `python scripts\validation\production_startup_smoke.py --require-legacy-absent` - PASS, 20 clean-room tables, no legacy paths present, fixtures disabled.
- `python scripts\validation\clean_room_empty_db.py --health` - PASS, 20 tables, 0 rows, FK check passed.
- `python scripts\validation\legacy_removal_gate.py` - PASS, production legacy executable paths 0, production legacy references 0, inventory item count 69.
- `python scripts\core\staging\verify_goal_v062_phase8_readiness.py` - PASS, `ENGINEERING_READY`, production schedules disabled, GPT/DeepSeek/fallback false.
- `python -m py_compile` on changed Python files - PASS.
- YAML parse for changed YAML/inventory files - PASS.
- `git diff --check` - PASS.

Completion state:

- Inventory complete: yes.
- Active legacy executable paths: 0.
- Active production legacy references: 0.
- Old direct model CLI production references: 0 in tracked active surfaces; ignored local `config/settings.yaml` is not committed.
- Old DB production references: 0 in tracked active surfaces; historical documentation references remain separated by gate output.
- Cold-backup runtime references: 0; remaining cold-backup mentions are historical approval/mapping records.
- Formal skill mapping: 12 of 12 intact.
- Clean-room state: 20 tables, 0 rows.
- Production task state: Disabled / not started.
- Production activation: not started.
