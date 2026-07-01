# GOAL-09 Progress

goal: GOAL-09 Experiments and Experience

status: `GOAL-09_IN_PROGRESS`

source_commit: `6003df81707913bd42808f04b3334b96f3c71c57`

branch: `codex/goal-09-v0.6.2`

GOAL-08_inherited_commit:
  `6003df81707913bd42808f04b3334b96f3c71c57`

## Starting Checks

- User approved GOAL-08 and allowed GOAL-09.
- Starting branch was `codex/goal-08-v0.6.2`.
- Starting HEAD was `6003df81707913bd42808f04b3334b96f3c71c57`.
- Starting worktree was clean.
- Created and switched to `codex/goal-09-v0.6.2`.
- Current HEAD contains GOAL-08 closeout commit `6003df81707913bd42808f04b3334b96f3c71c57`.
- GOAL-08 status was `GOAL-08_COMPLETE_WAITING_USER_APPROVAL`.
- GOAL-08 remaining checkpoints were none.
- GOAL-08 real blockers were none.

## GOAL-09 Definition Recovery

- `goals/GOAL-09.md`: restored from V0.6.2.
- `implementation_progress/GOAL-09.md`: created.
- `implementation_progress/GOAL-09_EXEC_PLAN.md`: created.
- `IMPLEMENTATION_PLAN.md`: missing.
- `MODULE_MANIFEST.yaml`: missing.
- `CODEX_GOAL_RESUME_PROTOCOL.md`: missing.
- `MODULE_REUSE_MATRIX.yaml`: present; GOAL-09 relevant entry is `prompt_skill_binding_model_routes`.
- Missing generic control package files are recorded as completeness issues only; V0.6.2 contains enough direct GOAL-09 task definition and this is not a specification blocker.

## GOAL-08 Dependencies

- GOAL-07 provides the formal `ModelGateway`, `PortableSkillSpec`, `HostBindingSpec`, `PortableSkillRunner` and immutable Run Envelope baseline.
- GOAL-08 provides immutable production artifact versions for research output, content plan, script, review, rejection, approval, approved draft, publication capture and manual edit.
- GOAL-08 provides candidate content preference revision creation with evidence references; single manual edits remain candidates and do not become current preference.
- GOAL-08 provides idempotent command receipt, audit, outbox and rollback patterns through `ProductionVersionChainMaterializer`.

## Completed Checkpoints

- Checkpoint 1 complete: restored GOAL-09 formal control package from V0.6.2 direct GOAL-09 and directly referenced CR-002/R07 sections.
- Checkpoint 2 complete: added deterministic P+ metric signal and formal experiment eligibility materialization.
  - Formal primary-used experiments materialize immutable `goal09_experiment_result` versions.
  - Metric signal returns `supported`, `not_supported`, `inconclusive` or `ineligible` without model calls.
  - P+ success cannot override an ineligible experiment where the primary hypothesis was not actually used.
  - P+ does not publish content preference; GOAL-08 candidate preference revisions remain candidates.
  - Repeated idempotency keys replay the original result; changed payloads are rejected.
- Checkpoint 3 complete: added `experiment_review` boundary.
  - Clean deterministic experiments do not require review.
  - Ambiguous publication changes, unknown actual use, attribution conflicts and major confounders are recorded as review-required boundaries.
  - Deterministic invalid P+ inputs remain Core inconclusive results and are not sent to review/model.
  - The review boundary is materialized with the immutable experiment result and does not override the metric signal.
- Checkpoint 4 complete: added CR-002 deterministic experience recomputation and proposal eligibility triggers.
  - Eligible formal primary-used P+ evidence recomputes maturity level.
  - Formal failures and independent external counterexamples recompute recommendation status without direct deprecate/restore publication.
  - Repeated formal failures, independent external counterexamples, structural revision signals and human revision requests produce proposal triggers only.
  - Manual lock and deprecated status are not silently overridden.
- Checkpoint 5 complete: added `experience_revision_propose` output gate and proposal publication path.
  - Proposal output rejects host persistent fields and `no_proposal` publication.
  - Proposal output must be a complete schema with evidence mapping, not a diff-only payload.
  - Proposal publication checks base versions are still current.
  - Published proposal hashes cannot be consumed twice.
  - Published proposals are immutable trace versions with base/evidence/skill-run refs, command receipt, audit and outbox.

## Remaining Checkpoints

- Checkpoint 6: Add inferred content preference candidate gate separated from CR-002 tactics.
- Checkpoint 7: Add fault/replay/clean-room validation and closeout report.

## Current Resume Point

- Continue with Checkpoint 6.

## Modified Files

- `goals/GOAL-09.md`
- `implementation_progress/GOAL-09.md`
- `implementation_progress/GOAL-09_EXEC_PLAN.md`
- `scripts/core/experience/__init__.py`
- `scripts/core/experience/goal09_experiments.py`
- `scripts/core/experience/verify_goal_09.py`

## Migration Changes

- None.

## Test Commands

- `python scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
  - tests: 12
  - real_external_credentials_used: no
  - external_side_effects: none
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'goal09_pycache'; python -m py_compile scripts/core/experience/__init__.py scripts/core/experience/goal09_experiments.py scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
- `git diff --check`
  - exit_code: 0

## External Live Gate

- None evaluated in this checkpoint.

## Real Blockers

- None.

## Checkpoint Commits

- `1752215` - docs(goal-09): restore formal control package
- This round checkpoint commit subject: `feat(goal-09): add pplus experiment metric gate`
- `7c79939` - feat(goal-09): add experiment review boundary
- This round checkpoint commit subject: `feat(goal-09): add cr002 experience triggers`
- This round checkpoint commit subject: `feat(goal-09): add experience proposal gate`

## Next First Unfinished Checkpoint

- Checkpoint 6: Add inferred content preference candidate gate separated from CR-002 tactics.

## GOAL-10 Permission

- `false`.
