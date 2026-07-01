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

## Remaining Checkpoints

- Checkpoint 2: Add deterministic P+ metric signal and formal experiment eligibility materialization.
- Checkpoint 3: Add `experiment_review` boundary for ambiguous publication/attribution cases without overriding deterministic Core outcomes.
- Checkpoint 4: Add CR-002 deterministic experience recomputation and proposal eligibility triggers.
- Checkpoint 5: Add `experience_revision_propose` output gate and proposal publication path.
- Checkpoint 6: Add inferred content preference candidate gate separated from CR-002 tactics.
- Checkpoint 7: Add fault/replay/clean-room validation and closeout report.

## Current Resume Point

- Continue with Checkpoint 2.

## Modified Files

- `goals/GOAL-09.md`
- `implementation_progress/GOAL-09.md`
- `implementation_progress/GOAL-09_EXEC_PLAN.md`

## Migration Changes

- None.

## Test Commands

- Pending for Checkpoint 2.

## External Live Gate

- None evaluated in this checkpoint.

## Real Blockers

- None.

## Checkpoint Commits

- Pending.

## Next First Unfinished Checkpoint

- Checkpoint 2: Add deterministic P+ metric signal and formal experiment eligibility materialization.

## GOAL-10 Permission

- `false`.
