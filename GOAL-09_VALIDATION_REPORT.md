# GOAL-09 Validation Report

goal: GOAL-09 Experiments and Experience

status: `GOAL-09_COMPLETE_WAITING_USER_APPROVAL`

branch: `codex/goal-09-v0.6.2`

validated_code_commit: `d422e55fad83ea083d0c614b6cd615eff1458a70`

## Checkpoint Commits

- `1752215` - docs(goal-09): restore formal control package
- `a1e9961` - feat(goal-09): add pplus experiment metric gate
- `7c79939` - feat(goal-09): add experiment review boundary
- `db4a52e` - feat(goal-09): add cr002 experience triggers
- `07272dc` - feat(goal-09): add experience proposal gate
- `7e9755f` - feat(goal-09): add inferred preference gate
- `d422e55fad83ea083d0c614b6cd615eff1458a70` - test(goal-09): cover closeout gates

## Scope Validated

- Formal primary-used P+ experiments materialize immutable experiment result versions.
- P+ metric signal is deterministic and returns supported, not_supported, inconclusive or ineligible.
- `experiment_review` boundary is recorded only for ambiguous publication/attribution cases and does not override Core metric signal.
- CR-002 recomputes maturity, recommendation status and proposal triggers deterministically.
- `experience_revision_propose` output gate rejects host fields, no_proposal publication and diff-only payloads.
- Proposal publication requires current base versions, unused proposal hash, evidence refs, skill-run refs and regression, ablation, compatibility and provenance gates.
- Inferred content preference candidates stay separate from CR-002 tactics and remain unpublished candidates.
- Single manual edits and P+ high performance do not create durable current preference.
- Formal Skill and candidate Skill remain separated; GOAL-09 does not create, mutate or publish either repository.
- Formal writes are performed through `ExperimentMaterializer` and existing Core/Persistence materializer primitives.

## Commands

- `python scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
  - tests: 18
  - real_external_credentials_used: no
  - external_side_effects: none
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'goal09_pycache'; python -m py_compile scripts/core/experience/__init__.py scripts/core/experience/goal09_experiments.py scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
- `git diff --check`
  - exit_code: 0

## Fault / Replay / Clean-room

- fault_result: passed; injected proposal receipt failure rolled back trace roots, versions, refs, receipts and outbox side effects.
- replay_result: passed; experiment, proposal and inferred preference repeated idempotency keys replay original results without duplicate formal side effects.
- clean_room_result: passed; no formal_skill or candidate_skill roots were created, candidate preferences stayed unpublished, and GOAL-09 writes carried command receipt, audit and refs.

## Migration Gate

- migration_changes: none
- new_business_tables: none
- PostgreSQL gate: not run; GOAL-09 changed no migrations and used existing persistence/materializer boundaries.

## External Live Gate

- external_live_gate: not required for local GOAL-09 closeout.
- real credentials: not used.
- irreversible external operations: none.

## Result

GOAL-09 scoped validation passed. Stop at `GOAL-09_COMPLETE_WAITING_USER_APPROVAL`; `GOAL-10 Permission` remains false.
