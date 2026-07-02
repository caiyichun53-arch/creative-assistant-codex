# GOAL-DATA-RESET-01 Validation Report

status: `PHASE_1_COMPLETE_SECOND_STAGE_NOT_APPROVED`
updated_at: `2026-07-02T08:42:20Z`

## Phase Boundary

This checkpoint generated the cleanup plan and policy updates only. It did not delete files, create cold backups, create a new database, run migrations, run DNA, load fixtures, or change production runtime behavior.

## Disposition Counts

- discard_from_target: `21`
- fixture_only: `7`
- cold_archive_only: `8`
- retain_as_current_asset: `8`

## Vault Classification

- retain_as_current_asset: `vault/??????.md`, `vault/???????.md`, `vault/??`, `vault/??`, `vault/??`.
- discard_from_target: `vault/????`, `vault/????` and old model-derived vault projections.
- cold_archive_only: old `vault/??` and `vault/???` unless later manually re-approved as current assets with clean provenance.
- fixture_only: none by default; vault text should not seed production fixtures.

## Planned Cleanup Scope

Data tables/directories planned for phase-2 removal from formal runtime include old SQLite business tables, `data/creation.db`, old transcripts, old DNA notes, old reverse prep outputs, old topics/drafts, old language fuel/model outputs, old logs and old runtime progress files. Current formal docs, Skills, schema, templates, domain config, writing foundations and technical implementation are protected.

## Remaining Dependency Scan

Phase-1 static scan found remaining references to legacy data paths:

- `config/settings.yaml` still points at `data/creation.db`.
- legacy scripts under `scripts/collect`, `scripts/analyze`, `scripts/topics`, `scripts/reverse`, `scripts/research`, `scripts/content`, `scripts/language_fuel` still read/write old SQLite tables or old artifact paths.
- formal `scripts/core/**` is not the main dependency source in this scan; the blockers are legacy runtime/config paths that must be disabled or isolated before physical cleanup.

## Second Stage Safety

`safe_to_execute_phase_2`: `false`

Reason: production/local runtime config and legacy scripts still have direct fallback paths to `data/creation.db`, `data/topics`, transcript paths and DNA note paths. Phase 2 needs an explicit remediation step that switches production config to the new empty formal DB and prevents production execution of legacy readers/writers before any deletion.

## Validation Performed

- Parsed and validated `LEGACY_DATA_INVENTORY.yaml` disposition enum coverage.
- Parsed and validated `FIXTURE_RETENTION_MANIFEST.yaml` coverage for 19/20 baseline, discovery/publish gap, duplicate ID, comments, ASR, performance bands, ???, ????, third domain, model failure/retry and idempotency.
- Parsed and validated `CLEAN_ROOM_ACCEPTANCE.yaml` hard gates.
- Confirmed data-reset rules were added to `BUSINESS_RULE_CATALOG.yaml` and traceability/policy files were synchronized.
- `git diff --check` passed, with only line-ending warnings.
