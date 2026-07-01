# GOAL-09 Clean-room Proof

goal: GOAL-09 Experiments and Experience

validated_code_commit: `d422e55fad83ea083d0c614b6cd615eff1458a70`

## Validation Mode

- Fixture-only in-memory persistence store.
- FakeClock-backed UUIDv7 IDs.
- No real model, platform, provider, Feishu, Skill repository or external credential.
- No GOAL-10 correction propagation.

## Proof Points

- Clean formal experiment: creates one immutable `goal09_experiment_result` version with command receipt, audit, outbox and concrete object refs.
- Replay: repeated experiment/proposal/preference idempotency keys return original results and do not duplicate formal side effects.
- Fault injection: injected proposal receipt failure rolls back partial proposal roots, versions, refs, receipts and outbox messages.
- Proposal gate: requires regression, ablation, compatibility and provenance gate evidence before publication.
- Skill separation: proposal publication rejects formal/candidate Skill object refs and creates no `formal_skill` or `candidate_skill` roots.
- Preference separation: inferred preference writes only candidate revisions and never sets `content_preference_profile.current_revision_id`.
- Evidence traceability: experiment, proposal and inferred preference outputs all retain concrete source refs.

## Commands

- `python scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'goal09_pycache'; python -m py_compile scripts/core/experience/__init__.py scripts/core/experience/goal09_experiments.py scripts/core/experience/verify_goal_09.py`
  - exit_code: 0
- `git diff --check`
  - exit_code: 0

## Result

Clean-room proof passed for GOAL-09 local scope. GOAL-10 remains disallowed until user approval.
