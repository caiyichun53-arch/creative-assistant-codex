# GOAL-12 ExecPlan

## Goal

Complete GOAL-12 End-to-End Staging from the current GOAL-11 closeout head without reimplementing GOAL-00 through GOAL-11.

## Plan

1. Restore the missing GOAL-12 control package from direct V0.6.2 references.
2. Build a staging verifier that exercises the existing production handlers:
   - Hermes/Feishu thin inbound binding.
   - Core state and Materializer.
   - Scheduler, RuntimeHost, lease, retry and FakeClock recovery.
   - Formal research workflow and local fake provider/fetcher/extractor.
   - ModelGateway and PortableSkillRunner with a fake no-I/O provider.
   - Production version chain from research through script, review, approval and publication capture.
   - Experience, experiment, preference candidate and correction propagation.
   - Backup/restore, clean-room deletion and forced resume.
3. Run replay and fault injection checks.
4. Run final local gates and legacy GOAL verifier smoke checks where relevant.
5. Produce final validation report, clean-room proof and final system acceptance report.
6. Mark GOAL-12 final status according to local gates and external live gates.

## Non-Goals

- No GOAL-13.
- No new business tables or states.
- No real Hermes, Feishu, SearchProvider, ASR, platform, model or production credentials.
- No irreversible external operations.

## Verification

- `python scripts/core/staging/verify_goal_12.py`
- `python -m py_compile scripts/core/staging/__init__.py scripts/core/staging/verify_goal_12.py`
- Selected prior GOAL verifiers for regression smoke.
- `git diff --check`

## Stop Conditions

- Unknown worktree changes that would be overwritten.
- Real V0.6.2 business-boundary conflict.
- Required irreversible external operation.
- Safe local isolated environment cannot be constructed.
