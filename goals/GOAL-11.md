# GOAL-11 Hermes Host Binding and Feishu Thin Interaction

status: `GOAL-11_COMPLETE_WAITING_USER_APPROVAL`

## Design Source

`goals/GOAL-11.md`, `implementation_progress/GOAL-11.md` and `GOAL-11_EXEC_PLAN.md` were missing at GOAL-11 entry.

Targeted recovery found no repository V0.6.2 source file or historical GOAL-11 control files. This task definition is restored from:

- the user-approved GOAL-11 entry prompt;
- GOAL-10 delivery interface and stop rules;
- existing GOAL-02 Core command envelope and Materializer;
- existing GOAL-03 Scheduler/Worker recovery foundation;
- existing GOAL-07 Portable Skill / Host Binding separation.

The missing task file is not a specification blocker.

## Scope

Implement the local contract baseline for Hermes as the production Host and Feishu as a thin interaction binding.

GOAL-11 must preserve these boundaries:

1. Hermes is the production Host.
2. Feishu is only a thin interaction binding and does not own business state.
3. Feishu and Hermes must not bypass Core or Materializer when formal state is changed.
4. Portable Skill and Host Binding stay separate.
5. Commands, messages, receipts, retries and recovery must be idempotent and traceable.
6. Temporary Hermes offline behavior must not break Scheduler/Worker formal execution.
7. Do not enter GOAL-12.
8. Do not introduce an independent Agent Runtime or unnecessary middleware.

## Acceptance

- Fake Feishu events produce Hermes-bound messages without writing formal state.
- Hermes dispatches formal state changes only through `CoreMaterializer.execute`.
- Duplicate host messages replay through Core command receipts and do not duplicate business state.
- Idempotency key reuse with changed command payload is rejected.
- Hermes and Feishu paths remain local fixture/fake/contract verified; no real Hermes or Feishu credentials are required.
- Scheduler/Worker remains the execution authority for queued work; Hermes unavailability is not treated as a local core blocker.

## Checkpoints

1. Restore the GOAL-11 control package.
2. Add fixture-first Hermes/Feishu binding contracts and minimal local bridge.
3. Add explicit host message receipt, response/outbox and replay records.
4. Add Scheduler/Worker retry and recovery coverage for Hermes temporary offline behavior.
5. Add Feishu response binding replay/fault coverage without live external I/O.
6. Add validation report and clean-room closeout.

## Hard Stops

- No GOAL-12 implementation.
- No live Hermes or Feishu dependency as a local blocker.
- No Feishu direct business-state write.
- No Hermes direct business-state write outside Core/Materializer.
- No Portable Skill mutation or host-specific leak into Portable Skill.
- No independent Agent Runtime, Redis, Celery, Temporal, Kafka, vector database or generic DAG.
