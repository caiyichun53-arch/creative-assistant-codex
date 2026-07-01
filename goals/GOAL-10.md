# GOAL-10 - Correction Propagation

Source: `I:\爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`

Recovered source sections:
- V0.6.2 paragraphs 7072-7088: formal `goals/GOAL-10.md` content.
- V0.6.2 paragraphs 5175-5222: TECH-005 correction, dependency propagation and recovery.
- V0.6.2 paragraphs 4424-4480: CR-005 correction safety fixes, layer ownership and explicit non-adoptions.
- V0.6.2 paragraphs 4564-4590: TECH subnode ownership for CR-005.
- V0.6.2 paragraphs 6723-6764: goal resume protocol.
- V0.6.2 paragraphs 7131-7155: ExecPlan and progress templates.

## Scope

Implement correction, direct dependency index, impact, convergence, blocked/resume, reporting and recovery.

## Acceptance

- The same correction creates no duplicates.
- History is not overwritten.

## Required Process

- Read relevant V0.6.1/R07 sections and `CODEX_GOAL_RESUME_PROTOCOL.md`.
- Create ExecPlan and initialize `implementation_progress/GOAL-10.md` from the template.
- Write fixture/replay/fault/FakeClock tests first.
- Implement using production handlers.
- Run two review loops and cross-chapter ownership check.
- After every atomic checkpoint: run its minimum test, update progress, and create a checkpoint commit.
- Produce validation report and clean-room proof.
- Stop for approval; do not automatically enter the next Goal.

## Direct Implementation Contract

Physical objects from TECH-005:
- `correction_record`: immutable correction basis recording target, old/new version or hash, change scope, evidence, reason, time and initiator; status reflects only processing progress.
- `dependency_index`: rebuildable direct dependency index from formal references, Input Assembly and Binding Manifest; not a business truth source and not a graph database.
- `correction_impact`: recoverable and idempotent propagation list for each target, with action kind, basis hash, processing status, result, stop reason and human action.
- `correction_report`: immutable summary of affected/no-impact objects, before/after results, stop points, failures, user confirmations and human attention.

Propagation algorithm:
- Register `correction_record`; identical corrections are idempotent and do not modify old objects.
- Resolve direct consumers from formal version refs, Input Assembly included/discarded refs, Binding Manifest `local_ref` and business mappings.
- Determine actions by object type and whether the model actually saw the input: `no_action`, `deterministic_recompute`, `semantic_rerun`, `replace_current_candidate`, `require_user_reconfirmation`, `historical_annotation`, or `human_attention_required`.
- Create one `correction_impact` and idempotent Job per target; process each aggregate in a small independent transaction.
- After recompute or rerun, compare `business_hash`.
- If business output is equivalent and refs are unchanged, stop propagation.
- If business output is equivalent but provenance changed, create a new version, decide whether to continue by dependency type, and check approval inheritance.
- If business output changed, create replacement version or next-generation result, switch current, and continue propagation.
- When all processing is complete, create `correction_report`.
- Failures resume from unfinished impact rows without duplicate side effects.

Lifecycle and user protection:
- `production_task`: block only when prerequisite invalidation requires it; resume after repair; normal lifecycle does not move backward.
- Approved but unpublished content: visible body changes require a new version and reconfirmation; strict provenance-only corrections may retain approval after re-review.
- Published or reviewed content: do not roll back, do not rewrite historical drafts; append correction relation, new result and required human attention.
- Frozen experiment baseline: recompute the corrected version using the original frozen time, rules and members; do not use current rolling windows.
- `manual_lock`: evidence and level/freshness may recompute, but actual recommendation migration remains frozen with risk audit.
- `deprecated`: do not auto-restore; create only restore proposals, and a published restore creates a new version.
- Component upgrades: normal Prompt/model/Binding upgrades affect only forward execution; replay historical scope only for confirmed defects.

Convergence and safety:
- Propagation access key is `correction_id + target_ref + expected_basis_hash + action_kind`; repeated execution returns the original result.
- Propagate only along explicit direct dependencies; do not infer impact from text similarity or whole-library semantic guessing.
- The same target and basis is not processed twice; a new basis creates a new impact.
- Every correction may configure technical safety limits for action count, depth and total cost; exceeding limits stops as `human_attention` and does not auto-expand scope.

## Hard Stops

- No new business states, tables, agents, LLM calls or user confirmations beyond spec.
- No real credentials or irreversible external operations without approval.
- Do not implement a generic invalid status.
- Do not add per-object correction status fields.
- Do not create a `data_correction` Skill.
- Do not add an LLM impact evaluator.
- Do not create a whole-system dependency graph database.
- Do not run whole-history replay by default.
- Do not automatically rewrite published content.
- Do not create a new Agent Runtime for correction.
