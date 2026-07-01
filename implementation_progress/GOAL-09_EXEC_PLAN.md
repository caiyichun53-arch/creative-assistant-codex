# GOAL-09 ExecPlan

status: `GOAL-09_COMPLETE_WAITING_USER_APPROVAL`

## Source

This ExecPlan is restored from V0.6.2 GOAL-09 paragraphs 2269-2285 plus direct CR-002/R07 references listed in `goals/GOAL-09.md`.

## Atomic Checkpoints

1. Restore GOAL-09 control package.
   - Status: complete.
   - Create `goals/GOAL-09.md`.
   - Create `implementation_progress/GOAL-09.md`.
   - Create this ExecPlan.
   - Record missing generic control package files without marking a spec blocker.
2. Deterministic P+ metric signal and formal experiment eligibility.
   - Status: complete.
   - Use fixture/FakeClock inputs only.
   - Accept only formal experiments where the primary tactic was actually used.
   - Compute deterministic `supported`, `not_supported`, `inconclusive` or `ineligible` metric signal.
   - Materialize the experiment result through Core/Materializer only.
   - Prove P+ does not write current user preference.
3. `experiment_review` boundary.
   - Status: complete.
   - Invoke only for ambiguous publication changes, attribution conflicts or recorded confounders.
   - Do not call the model for clean deterministic experiments.
   - Do not let review override deterministic technical invalidity.
4. CR-002 deterministic engine and proposal trigger.
   - Status: complete.
   - Recompute evidence maturity and recommendation status from formal evidence.
   - Detect repeated formal failures, independent external counterexamples, structural revision signals and human revision requests.
   - Keep deprecate/restore behind formal proposal rules.
5. Proposal output and publication.
   - Status: complete.
   - Accept at most one complete proposal per run.
   - Reject proposal outputs containing tactic IDs, version IDs, proposal IDs or transaction commands.
   - Publish only after base version and proposal hash checks.
6. Inferred preference candidate gate.
   - Status: complete.
   - Keep inferred preference candidates separate from CR-002 tactics.
   - Keep inferred candidates unpublished until calibrated.
   - Preserve evidence links to edits, approvals/rejections, publication captures and P+.
7. Closeout gates.
   - Status: complete.
   - Run focused fixture/replay/fault/Clean-room tests.
   - Generate GOAL-09 validation report and clean-room proof.
   - Stop at approval gate and do not enter GOAL-10.

## Current Round Limit

- Complete at most two tightly related checkpoints.
- Do not run full repository tests.
- Do not add external live provider/platform gates.

## Final Resume Point

- GOAL-09 complete; wait for user approval before GOAL-10.
