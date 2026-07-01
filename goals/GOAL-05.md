# GOAL-05 - Local Workflow Orchestration

Source: GOAL-04 handoff boundary. GOAL-04 explicitly stopped before GOAL-05 workflow orchestration.

Scope:
- Build a minimal local deterministic workflow orchestrator on top of GOAL-03 scheduler jobs.
- Workflow orchestration creates scheduler jobs for ordered local workflow steps.
- Workflow jobs are dispatched by the GOAL-04 runtime host through locally registered deterministic handlers.
- Workflow start is idempotent through GOAL-03 scheduler enqueue idempotency.

Non-scope:
- No Hermes or Feishu runtime.
- No external adapter.
- No ModelGateway, LLM call, provider call or external credential.
- No production Portable Skill execution.
- No legacy `data/creation.db` mutation.
- No PostgreSQL-specific runtime behavior in the initial checkpoint.

Initial checkpoints:
1. Create GOAL-05 task/progress baseline.
2. Add minimal local workflow orchestration surface on top of GOAL-03 scheduler.
3. Verify workflow enqueue, idempotency and GOAL-04 runtime dispatch with local in-memory tests.
