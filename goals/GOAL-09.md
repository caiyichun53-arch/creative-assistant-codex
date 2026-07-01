# GOAL-09 Experiments and Experience

status: `GOAL-09_IN_PROGRESS`

## Design Source

Restored from `I:\爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`.

Direct source paragraphs read for this restoration:

- V0.6.2 paragraphs 2031-2033: GOAL-09 summary and completion definition.
- V0.6.2 paragraphs 2269-2285: formal `goals/GOAL-09.md` control text.
- V0.6.2 paragraphs 590-735: CR-002 minimum data model, formal P experiment eligibility, evidence maturity, recommendation state and proposal publish rules.
- V0.6.2 paragraphs 990-999: publication capture, P+ and experience attribution.
- V0.6.2 paragraphs 1197-1209: `experiment_review` boundary.
- V0.6.2 paragraphs 1222-1248: `experience_revision_propose` trigger, input and output boundary.
- V0.6.2 paragraphs 1678-1683: content preference, CR-002 experience and Skill candidate separation.
- V0.6.2 paragraphs 2428-2438: resume and hard-stop rules.

## Scope

Implement P+, metric signal, experiment review, the CR-002 deterministic engine and proposal publication.

## Acceptance

- Only formal primary used experiments can qualify as experience evidence.
- P+ does not directly become user preference.
- Uncalibrated inferred preference candidates are not automatically published.
- P+ numerical success cannot override an ineligible experiment where the primary experience was not actually used.
- `experiment_review` does not override Core's deterministic metric signal.
- `experience_revision_propose` remains separate from tactic extraction, deterministic recomputation and human publication authority.

## Required Process

1. Read only GOAL-09 relevant V0.6.2/R07 sections and the available resume/progress material.
2. Create ExecPlan and initialize `implementation_progress/GOAL-09.md`.
3. Write fixture/replay/fault/FakeClock tests first.
4. Implement using production handlers and existing Core/Materializer boundaries.
5. After every atomic checkpoint: run its minimum test, update progress and create a checkpoint commit.
6. Produce validation report and clean-room proof.
7. Stop for approval; do not automatically enter GOAL-10.

## Checkpoints

1. Restore the GOAL-09 control package from V0.6.2.
2. Add deterministic P+ metric signal and formal experiment eligibility materialization.
3. Add `experiment_review` boundary for ambiguous publication/attribution cases without overriding deterministic Core outcomes.
4. Add CR-002 deterministic experience recomputation and proposal eligibility triggers.
5. Add `experience_revision_propose` output gate and proposal publication path.
6. Add inferred content preference candidate gate separated from CR-002 tactics.
7. Add fault/replay/clean-room validation and closeout report.

## Hard Stops

- No GOAL-10 correction propagation chain.
- No new generic Agent Runtime, Redis, Celery, Temporal, Kafka, vector database or generic DAG.
- No real external credentials, provider calls or irreversible external operations in local checkpoints.
- No silent modification or direct publication of formal Skill.
- No production loading of candidate Skill.
- No direct formal state writes from Skill, Runner, Adapter or Hermes.
- No durable user preference from a single manual edit.
- No durable experience solely from one high P+ performance.
- No user preference and tactic experience merging.
