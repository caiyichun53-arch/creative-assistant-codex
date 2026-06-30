# GOAL-04 - Runtime Host and Local Handler Dispatch

Source: GOAL-03 handoff boundary. GOAL-03 explicitly stopped before runtime host, adapter and skill runner implementation.

Scope:
- Runtime host consumes GOAL-03 scheduler jobs.
- Runtime host dispatches jobs to locally registered deterministic handlers.
- Runtime host reports success/failure through GOAL-03 scheduler completion APIs.
- Unknown job kind fails through scheduler failure path.

Non-scope:
- No Hermes or Feishu runtime.
- No external adapter.
- No ModelGateway, LLM call, provider call or external credential.
- No production Portable Skill execution.
- No GOAL-05 workflow orchestration.
- No mutation of legacy `data/creation.db`.

Initial checkpoints:
1. Create GOAL-04 task/progress baseline.
2. Add minimal runtime host dispatch surface on top of GOAL-03 scheduler.
3. Verify success, failure and unknown-handler paths with local in-memory tests.
