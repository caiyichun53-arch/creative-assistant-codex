# Phase 2 Remaining Skill Blockers

goal: `GOAL-V0.6.2-PRODUCTION-COMPLETION-01`

status: `BLOCKED_ON_FORMAL_BUSINESS_ROUTE_DECISION`

## Blocked Skills

- `experiment_review`
  - current mapping status: `planned_no_existing_business_node`
  - missing requirement: no approved `business.*` ModelGateway route exists in `BUSINESS_MODEL_ROUTE_REGISTRY.yaml`
  - why blocked: the Skill is declared as model-required, but there is no formal model node to own the review semantics

- `experience_revision_propose`
  - current mapping status: `planned_no_existing_business_node`
  - missing requirement: no approved `business.*` ModelGateway route exists in `BUSINESS_MODEL_ROUTE_REGISTRY.yaml`
  - why blocked: the Skill is declared as model-required, but there is no formal model node to own candidate experience revision semantics

## Minimal Decision Needed

Approve one of these formal routes:

- Add `business.experiment_review` for `experiment_review`.
- Add `business.experience_revision_propose` for `experience_revision_propose`.
- Or explicitly change either Skill to deterministic/non-model or remove it from the V0.6.2 production completion scope.

## Boundary

- Old business scripts and old data were not used to define these missing semantics.
- No direct Codex/Claude/legacy CLI path was introduced.
- No fake route was added to make tests pass.
