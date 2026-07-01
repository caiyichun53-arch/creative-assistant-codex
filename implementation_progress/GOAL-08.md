# GOAL-08 Progress

goal: GOAL-08 Formal content production chain versioning and provenance

status: `GOAL-08_IN_PROGRESS`

source_commit: `4aaa90a4374da20604e1fc77bc21060ac9297b1e`

branch: `codex/goal-08-v0.6.2`

GOAL-07_inherited_commit:
  `4aaa90a4374da20604e1fc77bc21060ac9297b1e`

GOAL-07_validated_code_commit:
  `6cd1a93f0bfcfb57b8b06f96b8fffcbf20f71bc1`

## Starting Checks

- User approved GOAL-07 and allowed GOAL-08.
- Starting branch was `codex/goal-07-v0.6.2`.
- Starting HEAD was `4aaa90a4374da20604e1fc77bc21060ac9297b1e`.
- Starting worktree was clean.
- Created and switched to `codex/goal-08-v0.6.2`.
- GOAL-07 status was `GOAL-07_COMPLETE_WAITING_USER_APPROVAL`.
- GOAL-07 remaining checkpoints were none.
- GOAL-07 real blockers were none.
- Current HEAD contains GOAL-07 validated code commit `6cd1a93f0bfcfb57b8b06f96b8fffcbf20f71bc1` and closeout commit `4aaa90a4374da20604e1fc77bc21060ac9297b1e`.

## GOAL-08 Definition Search

- Checked allowed locations:
  - repository root
  - `goals/`
  - `docs/`
  - `implementation_progress/`
- Previous finding: no formal GOAL-08 task definition was present in the repository.
- Resolved: restored `goals/GOAL-08.md` from the V0.6.2 formal GOAL-08 definition provided by the user.
- The previous formal definition missing condition is retained here as history and is no longer an active blocker.

## Control Package Check

- `goals/GOAL-08.md`: restored in this round.
- `IMPLEMENTATION_PLAN.md`: missing.
- `MODULE_MANIFEST.yaml`: missing.
- `CODEX_GOAL_RESUME_PROTOCOL.md`: missing.
- Missing control package files are recorded as completeness issues only; they do not block GOAL-08 implementation under the restored V0.6.2 definition.
- Follow-on control files checked for existence only:
  - `goals/GOAL-09.md`: missing.
  - `goals/GOAL-10.md`: missing.
  - `goals/GOAL-11.md`: missing.
  - `goals/GOAL-12.md`: missing.
- GOAL-09 through GOAL-12 control file absence is a control package completeness issue only; GOAL-08 remains in progress and no later Goal was started.

## Completed Checkpoints

- Restored formal GOAL-08 task definition from V0.6.2.
- Corrected GOAL-08 status from `GOAL-08_BLOCKED_BY_SPEC_DECISION` to `GOAL-08_IN_PROGRESS`.
- Created GOAL-08 ExecPlan.
- Checkpoint 1 complete: production artifact version-chain materializer.
  - `research_output`, `content_plan`, `script`, `review`, `approval`, `approved_draft`, `publication_capture` and `manual_edit` artifacts materialize as immutable `trace_version` records.
  - New body changes append a new version under the same root and keep old versions readable.
  - Evidence refs and GOAL-07 model run envelope refs are concrete `object_reference` records.
- Checkpoint 2 complete: approval/publication/preference evidence boundary.
  - Approved draft and actual publication capture use separate roots and versions.
  - Publication capture may differ from the approved draft while both versions remain readable.
  - Manual edit evidence can create only a candidate preference revision.
  - A single manual edit does not set `content_preference_profile.current_revision_id`.

## Remaining Checkpoints

- Review and rejection provenance checkpoint.
- Fault and replay gates for changed idempotency payloads and injected failures.
- GOAL-08 validation report.
- GOAL-08 clean-room proof.

## Current Resume Point

- Resume at GOAL-08 ExecPlan checkpoint 3: review and rejection provenance.

## Modified Files

- `goals/GOAL-08.md`
- `implementation_progress/GOAL-08_EXEC_PLAN.md`
- `implementation_progress/GOAL-08.md`
- `scripts/core/production/__init__.py`
- `scripts/core/production/goal08_production_chain.py`
- `scripts/core/production/verify_goal_08.py`

## Migration Changes

- None.
- No new business table was added.

## Test Commands

- `git diff --check`
  - exit_code: 0
- `python scripts/core/production/verify_goal_08.py`
  - exit_code: 0
  - tests: 2
  - real_external_credentials_used: no
  - external_side_effects: none
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'goal08_pycache'; python -m py_compile scripts/core/production/__init__.py scripts/core/production/goal08_production_chain.py scripts/core/production/verify_goal_08.py`
  - exit_code: 0

## Fixture/Replay Coverage

- FakeClock-backed UUIDv7 fixture store used.
- GOAL-07 fake ModelGateway/PortableSkillRunner path used to produce a concrete `model_run_envelope` version referenced by a GOAL-08 script version.
- Idempotent replay of repeated script materialization returns the original command receipt/version and does not duplicate audit/outbox/formal versions.
- Approved draft and publication capture separation verified with fixture payloads.
- Manual edit creates a candidate preference revision without publishing it as current preference.

## PostgreSQL Gate

- Not run in this checkpoint round.
- No migration changes were made; current GOAL-08 verification uses the existing in-memory persistence schema and materializer boundary.

## External Live Gate

- None evaluated in this round.

## Real Blockers

- None.
- Historical note: the earlier `GOAL-08_BLOCKED_BY_SPEC_DECISION` entry was caused by missing repository control file material, not by an unresolved GOAL-08 business specification decision.

## Checkpoint Commits

- `b9cf4d16364616fc9bc15ba7a7c57ce4a37700fe` - docs(goal-08): record formal definition blocker
- `d4e496034e43b934584bc7ec21cc594ccb7de39b` - docs(goal-08): restore formal task definition
- `af4ecddfc1e08428036d434b824c56ec0c8fbc82` - feat(goal-08): add production version chain materializer

## Next First Unfinished Checkpoint

- Review and rejection provenance.

## GOAL-09 Permission

- `false`
