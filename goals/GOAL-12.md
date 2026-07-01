# GOAL-12 - End-to-End Staging

status: `GOAL-12_COMPLETE_WITH_EXTERNAL_LIVE_GATES`

Source: `I:\爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`

Direct V0.6.2 references used:

- paragraphs 1723-1725: GOAL-12 summary and completion definition.
- paragraphs 2003-2019: formal `goals/GOAL-12.md` control text.
- paragraphs 1620-1629: `CODEX_GOAL_RESUME_PROTOCOL.md` required artifacts and recovery facts.
- paragraphs 1208-1209: FakeClock-verifiable Scheduler batches, backup and maintenance.
- paragraphs 1251-1257: validation isolation, clean-room deletion and same production handler path.
- paragraphs 1353-1366: Hermes production Host, Feishu thin boundary, formal Skill repository read-only and candidate isolation.

## Scope

Full-chain staging, backup/restore, clean-room deletion, fault injection and continuous running.

## Acceptance

- All traceability can be followed back through version, reference, audit, outbox and receipt rows.
- External gates are listed separately.
- Forced interruption/resume passes.
- Candidate and validation asset deletion does not affect production startup or formal Skill loading.
- Production independence passes: staging validation uses production Core, Scheduler, Runtime, Materializer, Research, ModelGateway, Portable Skill, Production Chain, Experience, Correction and Hermes binding handlers.

## Required Process

1. Read only GOAL-12 relevant V0.6.2/R07 sections and `CODEX_GOAL_RESUME_PROTOCOL.md`.
2. Create ExecPlan and initialize `implementation_progress/GOAL-12.md`.
3. Write fixture/replay/fault/FakeClock tests first.
4. Implement using production handlers.
5. Run two review loops and cross-chapter ownership check.
6. After every atomic checkpoint: run its minimum test, update progress and create a checkpoint commit.
7. Produce validation report and clean-room proof.
8. Stop for approval; do not automatically enter GOAL-13.

## Hard Stops

- No new business states, tables, agents, LLM calls or user confirmations beyond spec.
- No real credentials.
- No irreversible external operations.

## R07 Addition

Perform a forced interruption/resume exercise and prove deleting Hermes candidate/validation assets does not affect production startup or formal Skill loading.
