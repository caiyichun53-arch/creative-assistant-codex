# GOAL-10 ExecPlan

Goal: GOAL-10 Correction Propagation

## Spec Basis

- `goals/GOAL-10.md`, restored from V0.6.2 paragraphs 7072-7088.
- V0.6.2 TECH-005 direct references: paragraphs 5175-5222.
- V0.6.2 CR-005 safety and ownership references: paragraphs 4424-4480 and 4564-4590.
- V0.6.2 resume protocol: paragraphs 6723-6764.
- V0.6.2 templates: paragraphs 7131-7155.
- GOAL-09 inheritance: `GOAL-09_COMPLETE_WAITING_USER_APPROVAL`; validated code commit `d422e55fad83ea083d0c614b6cd615eff1458a70`; closeout commit `9c5427107dbb63d28743f3d1c7173beff79f48c4`.

## Scope

Implement correction registration, direct dependency indexing, impact creation, propagation convergence, blocked/resume protection, correction reporting and recovery.

## Non-goals

- No GOAL-11 Hermes/Feishu live binding.
- No generic invalid state.
- No per-object correction status fields.
- No LLM impact evaluator.
- No whole-system dependency graph database.
- No whole-history replay.
- No automatic rewrite of published content.
- No new Agent Runtime.
- No real credentials or irreversible external operations.

## Files

Expected files are limited to GOAL-10 direct scope:
- `goals/GOAL-10.md`
- `implementation_progress/GOAL-10.md`
- `implementation_progress/GOAL-10_EXEC_PLAN.md`
- GOAL-10 direct Core/Materializer/Job/version modules and tests, to be identified checkpoint by checkpoint.
- `GOAL-10_VALIDATION_REPORT.md`
- `GOAL-10_CLEAN_ROOM_PROOF.md`

## Migrations

Migration changes are not yet made. Any later migration must be justified by GOAL-10 objects: `correction_record`, `dependency_index`, `correction_impact`, `correction_report`, or supporting refs/jobs needed for recovery and audit.

## Atomic Checkpoints

1. Restore formal GOAL-10 control package.
   - Create `goals/GOAL-10.md`.
   - Create `implementation_progress/GOAL-10.md`.
   - Create this ExecPlan.
   - Record missing generic control files as completeness issues only.

2. Add fixture-first correction contract tests.
   - Cover immutable correction basis.
   - Cover identical correction idempotency.
   - Cover no historical overwrite.
   - Cover direct dependency impact creation from explicit refs only.

3. Implement correction registration and direct dependency impact planning through existing Core/Materializer patterns.
   - Use command receipt, audit, outbox and idempotency.
   - Do not let Skill, Runner, Adapter, Hermes, Feishu or Codex write formal state directly.

4. Add propagation processing and convergence.
   - Use `business_hash`.
   - Stop on equivalent unchanged refs.
   - Create replacement/new versions only through Materializer.
   - Preserve provenance and version lineage.

5. Add blocked/resume and recovery behavior.
   - Use existing GOAL-03 Job/retry/lease/recovery foundation.
   - Resume unfinished impacts without duplicate side effects.
   - Do not roll normal lifecycle backward.

6. Add reporting, fault, replay and clean-room closeout.
   - Create immutable `correction_report`.
   - Verify replay and injected failures.
   - Run GOAL-10 scoped validation and clean-room proof.

## Minimum Test Per Checkpoint

- Checkpoint 1: `git diff --check`.
- Checkpoints 2-5: GOAL-10 direct unit/contract/fixture/replay/fault tests only.
- Checkpoint 6: GOAL-10 scoped validation, Python compile, `git diff --check`, and required local database tests.

## Validation Modes

Use unit, contract, fixture, replay, fault injection and FakeClock first. Live Hermes, Feishu or external platform tests are not required for GOAL-10 local core implementation.

## Rollback

Do not use broad reset. If a checkpoint fails, revert only the checkpoint's explicit files or add a failure report and stop.

## Clean-room

Final GOAL-10 closeout must prove correction, invalidation/revalidation/recovery artifacts can be verified without formal external credentials and without GOAL-11 live bindings.

## Risks

- Implementing correction as historical overwrite.
- Updating only current pointers and missing old-version consumers.
- Inferring dependencies from semantic similarity instead of explicit direct refs.
- Duplicating Job/retry/runtime logic instead of reusing GOAL-03.
- Treating published content as unpublished.

## Stop Conditions

- New business state/table/user confirmation is required beyond V0.6.2.
- A real external credential or irreversible operation is required.
- An unknown worktree diff appears.
- GOAL-10 formal source conflicts with existing validated GOAL-00 to GOAL-09 contracts.

## Resume Entry

Start from the first unfinished checkpoint in `implementation_progress/GOAL-10.md`. Current first unfinished checkpoint after checkpoint 1 is checkpoint 2: fixture-first correction contract tests.

## Results

- Checkpoint 1 restored the GOAL-10 formal control package.
- Checkpoints 2 and 3 added fixture-first correction contracts and minimal correction registration / direct dependency impact planning.
- Checkpoint 4 added impact propagation processing and convergence:
  - equivalent business output with unchanged refs stops without replacement;
  - changed business output creates a replacement version with lineage through `CorrectionMaterializer`;
  - repeated propagation replays without duplicate outbox or replacement side effects.
- No migration changes yet.
