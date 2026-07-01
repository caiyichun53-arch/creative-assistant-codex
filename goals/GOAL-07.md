# GOAL-07 Formal ModelGateway and Run Envelope Baseline

status: `GOAL-07_IN_PROGRESS`

## Scope

Implement the first formal GOAL-07 baseline for model execution without starting GOAL-08 content production.

GOAL-07 scope is anchored by `MODULE_REUSE_MATRIX.yaml` entries:

- `prompt_skill_binding_model_routes`
- `model_gateway`

## Checkpoints

1. Create GOAL-07 task/progress baseline from existing GOAL-06 handoff and reuse matrix anchors.
2. Add formal `ModelGateway` with deterministic provider selection and traceable Run Envelope persistence.
3. Add Portable Skill contract and clean-room validation with no table, ORM, Host UUID or formal state write leaks.
4. Add Host Binding separation and Runner execution contract over the Portable Skill boundary.
5. Prove Runner, Binding and ModelGateway integrate without bypassing Core/Materializer state boundaries.

## Current Round Limits

- No independent generic Agent Runtime.
- No full GOAL-08 production chain.
- No real model credential requirement.
- No Redis, Celery, Temporal, Kafka, vector database, generic DAG or multi-agent system.
- No direct formal state write from Skill, Binding, Runner or provider adapter.

## Local Acceptance

- Fake model provider returns deterministic output with usage and cost metadata.
- Every model call creates a Run Envelope containing provider, model, prompt hash, skill/binding identity, config version or hash, input/output hash, duration and cost fields.
- Provider failure still records a failed Run Envelope.
- Missing route/provider is rejected before provider execution.
