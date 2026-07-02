# Migration Execution Plan - GOAL-00

status: `PLAN_READY_NO_BUSINESS_EXECUTION`

This plan is the GOAL-00 handoff. It defines the order for migrating the legacy MVP into the V0.6.2 architecture without running business writes, live external sends, production model calls, or irreversible data changes during the planning step.

## Non-Negotiable Rules

- Do not continue DNA batch writes before the model and persistence paths are migrated.
- Do not run `scripts/run_daily.py` as production runtime.
- Do not run `scripts/feishu/listener.py` as production command source.
- Do not call `scripts/llm/call.py` for formal V0.6.2 model work.
- Do not migrate `data/creation.db` in place.
- Do not copy gate-only settings such as `gate_max_output_tokens=128` into production defaults.
- Do not turn local `.agents` or `.claude` workflow notes into production Skills without schema/version validation.

## Phase 0 - Freeze And Guardrails

Keep `release/v0.6.2-rc1` frozen, keep `audit/goal-00-v0.6.2` as the audit/planning branch, treat `data/creation.db` as read-only source data, keep `.env.live-gates` ignored, and keep external gates closed unless a dedicated Gate task explicitly opens one.

Validation: clean git status, read-only DB hash/count capture, and no production logs or DB mtimes changed by planning tasks.

## Phase 1 - Legacy Runtime Disablement Map

Prevent accidental production execution of old authority paths while preserving source value.

Disabled legacy entrypoints until migrated:

- `scripts/run_daily.py`
- `scripts/feishu/listener.py`
- `scripts/llm/call.py` live path
- root BAT launchers
- direct DB migration scripts

Validation: static reference scan proves new Core does not import old authority scripts; no deletion until a future retirement audit identifies a safe-to-remove candidate.


## GOAL-DATA-RESET-01 Binding Update - Empty Formal Database

V0.6.2 formal production starts from an empty formal database. Old project business data, old state, old DNA/model outputs, old topics/drafts and old derived experience are not migrated into production. Legacy data may only be used for minimal isolated desensitized fixtures, preferably synthetic, and cold archive evidence outside project and production load paths.

This supersedes any earlier migration wording that implied old SQLite business records would be converted into formal production objects. Future migration goals must implement clean-room startup, fixture-only test loading and production fallback blocking before any business migration work.

Additional stop conditions: stop if production config references `data/creation.db`, production code reads legacy data directories, a Skill/model context can access cold archive or fixtures, or old DNA/experience outputs appear in formal production artifacts.

## Phase 2 - Model Path Migration

Recommended first implementation Goal:

`GOAL-MIGRATION-01: ModelGateway shadow bridge for legacy reverse DNA fixture`

Scope:

1. Use one isolated fixture, not production hit writes.
2. Route one reverse-DNA-style request through `ModelGateway -> HermesModelProviderAdapter` or a fake provider in unit tests.
3. Persist a model run envelope with provider, model, correlation ID, input/output hashes, usage and cost metadata.
4. Keep production output budgets task/profile-specific; gate token caps remain gate-only.
5. Do not resume `scripts/reverse/dna.py --batch` until this Goal passes and the persistence boundary is defined.

Validation: unit tests with fake provider, optional live model gate only if explicitly opened, no `data/llm_state.json` cooldown write, and no legacy DB update.

## Phase 3 - Persistence And State Migration

Move legacy direct writes behind Core and Materializer.

Actions:

1. Define source-to-target mapping from legacy SQLite tables to formal trace/state objects.
2. Build migration rehearsal against a copy of `data/creation.db`.
3. Convert direct state transitions into Core commands for competitor registration, video ingestion, hit promotion, topic selection/rejection, reverse prep/DNA completion, and draft review/diff.
4. Keep old row IDs as source references, not new object IDs unless explicitly mapped.

Validation: pre/post row counts on copied DB, source DB hash unchanged, idempotent replay, and forced failure leaves no partial current state.

## Phase 4 - Adapter Wrapping

Retain working platform/runtime code while removing direct authority.

Adapters to wrap first:

1. ASR runtime output -> Core transcript artifact command.
2. MediaCrawler/Douyin raw output -> normalized collector payload.
3. Feishu OpenAPI client -> outbox dispatch worker only.
4. Douban/NetEase/language-fuel collectors -> normalized source artifacts.

Validation: fixture replay before live calls, adapter outputs have hashes and capability metadata, Materializer owns persistence, and external sends require outbox receipts/idempotency keys.

## Phase 5 - Skill Formalization

Convert local workflow Skills into portable V0.6.2 Skills one at a time.

Order:

1. `creation-banned-check`
2. `creation-prepare-topic`
3. `analyze-hit-dna`
4. writing/review Skills
5. `creation-workflow`

Validation: explicit input schema, output schema, version/hash, allowed side effects and failure modes for each Skill; no DB/file writes except through declared runtime bindings.

## Phase 6 - External Gate Completion

Current state:

- Passed: ASR, Model Provider, Hermes Real Host.
- Not yet live: Feishu thin binding, Search Provider, External Collector Adapter, Shadow E2E, Continuous Fault Recovery.

Order: Feishu thin binding, Search Provider, External Collector Adapter, Shadow E2E, Continuous Fault Recovery.

Validation: each gate has preflight, one-gate live run, evidence hashes, leak checks and no unrelated gate execution.

## Stop Conditions For Future Goals

Stop and ask before proceeding if any task requires production account token in chat, real Feishu production message send, production database mutation, batch DNA generation/write, new unapproved business state model/table, or broad refactor/deletion outside the opened Goal.

## GOAL-00 Exit Criteria

GOAL-00 is complete when the baseline, legacy audit, module matrix, migration plan and progress file are current; static/doc/schema checks pass; and Git confirms only audit artifacts changed before commit.

## Required Architecture Dependency Order

Implementation order for migration work:

1. Core API and immutable persistence boundary.
2. PostgreSQL target schema plus SQLite source-data migration rehearsal.
3. Job/Scheduler/Worker lease and RuntimeHost execution boundary.
4. Materializer for all business artifacts and state projections.
5. Outbox, command receipt, correlation/causation and idempotency.
6. Binding layer: Feishu/Hermes/Host payloads map into portable commands only.
7. Model Port and ModelGateway, with provider choice outside business Skills.
8. Portable Skill Runtime and formal Skill packages.
9. Adapter wrapping for ASR, collectors, search and Feishu sends.
10. Shadow E2E and continuous fault recovery gates.

Hermes production chain rule: Hermes may call only allowlisted tools/Core API boundaries for production business state. It must not depend on arbitrary Shell access for formal production state advancement.

## DNA Mapping And Old Result Strategy

Current code does not expose functions named `sample_deep_analyze` or `tactic_extract`. The observed legacy responsibilities map as follows:

- `sample_deep_analyze`: legacy `scripts/reverse/dna.py` single-hit map step, which reads transcript/data and produces one DNA note.
- `tactic_extract`: legacy `scripts/reverse/reduce.py` reduce step, which extracts common tactics, route tables and examples from multiple DNA notes.

The 81 existing DNA results are migration source artifacts, not proof of a formal Skill implementation. Strategy:

1. Preserve the 81 outputs as read-only source evidence.
2. Record note paths, content hashes, source hit IDs and legacy route metadata during migration rehearsal.
3. Import them as historical artifacts only after target schema and Materializer mapping are approved.
4. Do not rewrite or regenerate them silently.
5. Do not process the remaining 14 pending DNA items until formal `sample_deep_analyze`/`tactic_extract`-equivalent Skills pass standalone, boundary, replay and model-envelope validation.

## Required Gate Families For Later Goals

- Fixture gates: deterministic fixtures for DB mappings, model inputs, adapter outputs and Skill I/O.
- Standalone gates: each formal Skill runs without local host assumptions.
- Boundary gates: no Skill/Adapter/Runner direct DB write or direct external send.
- Fault gates: provider failure, DB failure, duplicate event, retry and interruption recovery.
- Ablation gates: remove optional context/examples and prove graceful degradation.
- Third-domain gates: prove domain config is data, not code.
- Replay gates: same input/event replays idempotently.
- Shadow gates: full chain runs with shadow-only sink.
- Clean-room gates: no candidate/validation asset required for production startup.
- Live gates: one external dependency at a time, with explicit preflight and evidence.

## Future Goal Sequence And Commit Points

1. `GOAL-MIGRATION-01`: ModelGateway shadow bridge for one reverse-DNA fixture. Commit after fake-provider tests and isolated envelope persistence pass.
2. `GOAL-MIGRATION-02`: source-data migration rehearsal for old SQLite to formal objects. Commit after copied-DB counts/hashes and rollback checks pass.
3. `GOAL-MIGRATION-03`: wrap ASR and collector adapters so they return artifacts without owning state. Commit after fixture replay and isolated adapter gates pass.
4. `GOAL-MIGRATION-04`: formalize `creation-banned-check` and one DNA-analysis Skill contract. Commit after standalone/boundary/fault gates pass.
5. `GOAL-MIGRATION-05`: Feishu thin binding and outbox send path with test-only target. Commit after fake duplicate/retry tests and explicit live gate pass.
6. `GOAL-MIGRATION-06`: shadow E2E and continuous recovery. Commit only after replay, shadow and recovery gates pass.

Failure recovery for every Goal: stop on first boundary violation, keep production DB untouched, preserve evidence, report changed files, and resume from the last committed checkpoint.
