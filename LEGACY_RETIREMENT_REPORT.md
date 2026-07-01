# LEGACY-RETIREMENT-AUDIT

status: `AUDIT_ONLY_NO_CODE_REMOVAL`

baseline: `v0.6.2-rc1` frozen code commit `2c6b48bfcfe01d2a4cf617c2b6ec158fc51f47d7`

audit branch/head: `release/v0.6.2-rc1` at `15caed9f4eb515b2e5abb9936e0c55cf52c0d34f`

## Scope

This pass only audits retirement readiness. It does not delete, move, rename or modify business code.

Generated artifacts:

- `LEGACY_RETIREMENT_MATRIX.yaml`
- `LEGACY_RETIREMENT_REPORT.md`

No full test suite was rerun. No live Hermes, Feishu, model, platform or production database connection was made.

## Method

- Checked tracked repository files from the RC branch.
- Compared old MVP code against the V0.6.2 Core implementation under `scripts/core/**`.
- Searched imports/calls, startup references, scheduler mentions, config references, direct SQLite writes, migration files, adapter reuse surfaces, fixture/replay value and rollback value.
- Reused prior GOAL-00 evidence only as context, then rechecked current file references with targeted scans.

## High-Level Result

No tracked legacy code is classified as `safe_to_remove` in this audit.

Reason: every removal candidate still has at least one disqualifying responsibility:

- referenced by local skill/docs,
- needed to interpret or migrate legacy SQLite/file data,
- useful as an Adapter implementation source,
- useful for replay/fixture construction,
- or valuable as rollback/diagnostic evidence after RC freeze.

## Classification Summary

- `active`
  - `scripts/core/**`
  - `scripts/content/check_banned.py`
  - `config/banned_words.yaml`
- `wrapped_dependency`
  - MediaCrawler/Douyin adapter family.
  - ASR adapter family.
  - Feishu low-level client/push adapter.
  - Obsidian projection writers.
  - NetEase/Douban/language material collectors.
- `migration_only`
  - Legacy SQLite schema and migrations.
  - Legacy account/domain seed tools and config examples.
  - Legacy topic/content/draft/diff tools.
  - Legacy reverse/experience/language-fuel tools.
- `rollback_only`
  - Host-bound prompt/skill assets.
  - Project readiness and legacy audit docs.
- `disabled_legacy`
  - Old daily scoring/topic chain.
  - Feishu listener and Windows launchers.
  - Old CLI model gateway.
- `safe_to_remove`
  - none.

## Key Findings

1. New Core does not call old MVP scripts.
   Targeted scans found `scripts/core/**` imports only V0.6.2 modules and standard libraries, not old `scripts.collect`, `scripts.analyze`, `scripts.topics`, `scripts.reverse`, `scripts.language_fuel`, `scripts.music`, `scripts.feishu`, `scripts.db`, `scripts.llm` or `tools/asr`.

2. Local skills and docs still reference old scripts.
   Examples include `register-competitor-account`, `create-domain-account`, `analyze-hit-dna`, `creation-prepare-topic`, `creation-save-draft` and `creation-banned-check`. These references prevent simple deletion.

3. Legacy startup paths are still documented but should remain disabled for production.
   `README.md` and `BUILD_PLAN.md` still mention `scripts/db/init_db.py`, `scripts/run_daily.py`, `scripts/reverse/dna.py`, Feishu listener behavior, ASR worker and BAT launchers. These are rollback/reference paths, not RC production entrypoints.

4. Legacy Scheduler responsibility has been superseded.
   V0.6.2 Scheduler registration is in `scripts/core/**`. Old `run_daily.py` and Windows Task Scheduler references are classified as `disabled_legacy` or `rollback_only`.

5. Legacy database/migration files are not safe to remove.
   `scripts/db/schema.sql`, `init_db.py`, `seed_mvp.py` and `migrate_*.py` remain the old data model inventory. They are required for historical data compatibility and migration replay, even though they are not the V0.6.2 target persistence layer.

6. Adapter families should be wrapped, not deleted.
   MediaCrawler, ASR, Feishu client/push, NetEase and Douban collectors contain real platform/runtime knowledge. Their direct DB/state authority must stay disabled, but their parsing and integration behavior remains useful beneath future formal Ports/Adapters.

7. No safe deletion candidate met the required evidence bar.
   The audit found no item with all of: no calls, no config references, no migration duty, no rollback duty, an existing replacement and a clear post-delete test set.

## Safe-to-Remove Items

None.

Because there are no `safe_to_remove` items, there are no deletion test commands to prescribe in this round. Any future delete proposal must add per-file evidence for:

- no invocation/import/reference,
- no configuration reference,
- no data migration responsibility,
- no rollback responsibility,
- replacement implementation,
- and tests to run after deletion.

## Current Retirement Rules

- Do not run disabled legacy live entrypoints from RC production:
  - `scripts/run_daily.py`
  - `scripts/feishu/listener.py`
  - `tools/start_listener.vbs`
  - launch BAT files
  - `scripts/llm/call.py` live model path
- Treat legacy DB scripts as read-only migration inventory unless an explicit migration task is opened.
- Treat old collectors and ASR as adapter source only; they must not write formal V0.6.2 state directly.
- Treat host-bound `.claude`, `.codex/agents` and `.agents/skills` assets as local workflow/source material, not production-loaded formal Skills.

## Deliverables

- Matrix: `LEGACY_RETIREMENT_MATRIX.yaml`
- Report: `LEGACY_RETIREMENT_REPORT.md`
