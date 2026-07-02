# Data Purge Plan - GOAL-DATA-RESET-01

status: `PHASE_2_EXECUTED_CLEAN_ROOM_VALIDATED`
updated_at: `2026-07-02T10:50:00Z`

## Binding Data Principle

The V0.6.2 formal system starts from an empty formal database. It does not migrate old project business data, old state, old derived analysis, old model output, old experience, old Hermes memory or old production retrieval context. Legacy data may only produce minimal isolated desensitized fixtures for tests.

## Clear From Formal Runtime Directories

Planned for phase 2 only, after manifest review and explicit approval:

- `data/creation.db` and SQLite sidecars if present.
- Legacy business output directories: `data/transcripts`, `data/爆款拆解`, `data/reverse`, `data/topics`, `data/drafts`, `data/language_fuel`, `data/music` business payloads, `data/raw` crawler outputs, `data/llm_state.json`.
- Runtime history not needed for production start: `logs/*.log`, `outputs/*`.
- Old vault derived/business outputs: `vault/爆款拆解`, `vault/语感燃料`, and unapproved old `vault/范例` / `vault/方法论` derived from old DNA or old model outputs.

Phase 1 performed no physical deletion. Phase 2 later moved the approved old business data out of formal runtime paths after config, empty DB and fixture gates passed.

## Protected Formal Assets

Do not delete by broad directory operations:

- Formal docs and Goal/audit reports.
- `scripts/core/**`, schema/source code, validation harnesses and tests.
- Formal/current Skills and operator Skill definitions until individually classified.
- `config/domains/**`, `config/accounts/**`, examples and non-secret config templates.
- Current effective writing foundations: `vault/真人写作基石.md`, `vault/评论真人味基石.md`.
- Current templates/persona/taxonomy assets: `vault/模板`, `vault/人设`, `vault/词表`, root templates if present.

## Vault Classification

- retain_as_current_asset: writing foundations, templates, persona, taxonomy and explicitly current design assets.
- discard_from_target: old DNA projections, old language fuel, old derived model outputs.
- cold_archive_only: old examples/methodology unless manually re-approved as current asset after provenance review.
- fixture_only: none by default; vault text should not seed production fixtures unless a parser/layout test cannot be synthetic.

## Checkpoint And Rollback

Before phase 2:

1. Create a read-only cold backup outside this repo and outside any production load path.
2. Include `data/creation.db`, old data directories, old vault derived outputs and logs.
3. Record SHA256 manifest and file counts.
4. Do not commit the backup or put it under project `data/`, `vault/`, `logs/`, `outputs/`, `validation_evidence/` or any production retrieval path.
5. Rollback is manual restore from cold backup into a quarantine path only, never directly into formal runtime.

## New Empty Database Creation

Phase 2 creates a new formal database from the formal V0.6.2 schema/migrations only. It imports zero rows from old SQLite business tables. Test fixtures are loaded only into an isolated test database under validation/test paths.

## Production Fallback Blocks

Production config must not reference `data/creation.db`. Production code must not read legacy directories or old vault derived outputs. Any remaining references are treated as migration blockers unless they are tests, docs, diagnostics, or explicit fixture loaders.

## Proof Old Data Cannot Enter Model Context

- ModelGateway/Skill input assembly must only accept formal artifacts or test fixtures in test mode.
- Search/retrieval indexes must be built from the empty formal DB and approved current assets only.
- Hermes memory/profile must not mount cold backup or legacy data directories.
- Removing fixture directories must not change production startup behavior.

## Phase 2 Stop Conditions

Stop before deletion if any production path still depends on `data/creation.db`, old `data/**` outputs, old DNA notes, old topics/drafts, or old vault derived content.

## Phase 2 Execution Result

- Cold backup root: `I:/Creation_assistant_cold_backups/GOAL-DATA-RESET-01/20260702T184708`.
- Manifest: `BACKUP_MANIFEST.json`, 596 entries.
- Formal runtime old paths removed or isolated:
  - `data/creation.db`, `data/creation.db-shm`, `data/creation.db-wal`
  - `data/transcripts`, `data/爆款拆解`, `data/reverse`, `data/topics`, `data/drafts`, `data/language_fuel`, `data/humanize`, `data/music`, `data/raw`, `data/llm_state.json`
  - `logs`, `outputs`
  - `vault/范例`, `vault/方法论`, `vault/语感燃料`, `vault/爆款拆解`
- Formal runtime paths retained:
  - `data/formal/clean_room_v0_6_2.sqlite3`
  - technical caches under `data/npm-cache` and `data/uv-cache`
  - current vault assets: `人设`, `模板`, `词表`, `真人写作基石.md`, `评论真人味基石.md`
- Verification: `python scripts/validation/production_startup_smoke.py --require-legacy-absent` passed.
