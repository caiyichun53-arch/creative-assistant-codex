# GOAL-08 ExecPlan

status: `GOAL-08_IN_PROGRESS`

source_definition: `goals/GOAL-08.md`

## Atomic Checkpoints

1. Production artifact version-chain materializer
   - research output, content plan, script, review, approval, approved draft, publication capture and manual edit artifacts are immutable `trace_version` records.
   - New body changes append a new version and never update an old version.
   - Specific evidence refs and GOAL-07 model run envelope refs are version-specific `object_reference` records.

2. Approval, publication capture and preference evidence boundary
   - Approved draft and actual published artifact are separate roots and versions.
   - Publication capture can differ from the approved draft while both remain readable.
   - Explicit preference evidence from user instruction, manual edit, approval, rejection or publication capture is recorded as candidate evidence only.
   - A single manual edit does not become the current permanent preference.

3. Review and rejection provenance
   - Review and rejection artifacts point to the exact script or approved draft version being reviewed.
   - Rejections record evidence refs without mutating prior content versions.

4. Fault and replay gates
   - Repeated idempotency keys replay without duplicate formal side effects.
   - Changed payload under the same idempotency key is rejected.
   - Injected failures do not leave partial formal publication state.

5. GOAL-08 closeout
   - Generate validation report and clean-room proof.
   - Run GOAL-08 scoped verification only.
   - Stop at `GOAL-08_COMPLETE_WAITING_USER_APPROVAL` or `GOAL-08_COMPLETE_WITH_EXTERNAL_LIVE_GATES`.
   - Keep `GOAL-09 Permission` false.

## This Round

- Implement checkpoint 3.
- Next resume point after this checkpoint is checkpoint 4.
- Do not enter GOAL-09.
