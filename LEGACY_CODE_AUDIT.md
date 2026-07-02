# Legacy Code Audit - GOAL-00

status: `AUDIT_COMPLETE_CURRENT_BASELINE`

This audit classifies the current repository against the V0.6.2 target architecture. It intentionally does not modify business code, production config, database content, prompts, Skills, external credentials, or live account state.

## Scope

Audited surfaces:

- Python, PowerShell, BAT, YAML, Markdown and local Skill files under the repository.
- Legacy MVP scripts under `scripts/**` and `tools/**`.
- Formal V0.6.2 Core implementation under `scripts/core/**`.
- External live gate harness and evidence.
- Read-only SQLite data counts and DNA status.

Not executed: real business DNA batch, Feishu listener/push, MediaCrawler live collection, DB migration against `data/creation.db`, or any model call outside already completed gate evidence.

## Executive Result

The repository now contains two layers:

1. Formal V0.6.2 Core and live-gate code, which should be kept as the target runtime.
2. Legacy MVP scripts and local workflow Skills, which contain valuable collection, ASR, reverse-analysis, research, content and platform knowledge but do not conform to V0.6.2 authority boundaries.

The correct migration approach is selective reuse: keep formal Core, wrap real adapters, adapt deterministic algorithms and content workflow data, and replace legacy authority paths that mutate state or call models/platforms directly.

## High-Risk Findings

### Direct State Writes

Legacy scripts write SQLite state directly. Examples include:

- `scripts/collect/crawl_competitors.py`: upserts competitor videos, video checks and crawl timestamps.
- `scripts/analyze/judge_hits.py`: inserts baselines/hits and updates video status.
- `scripts/topics/daily_topics.py`: mutates topic pool status and heat.
- `scripts/topics/new_topic.py`, `scripts/topics/spinoff.py`: insert topics and trigger preparation.
- `scripts/reverse/dna.py`: writes DNA notes and updates hit reverse status.
- `scripts/content/save_draft.py`, `scripts/content/diff_draft.py`: insert drafts/diffs.
- `tools/asr/transcribe.py`: updates transcript fields and reverse status.

Conflict: V0.6.2 requires state transitions through Core command envelopes, Materializer persistence, command receipts, audit events, idempotency and outbox boundaries.

### Direct External Sends

Legacy scripts send to Feishu directly or depend on a live listener:

- `scripts/feishu/push.py` directly calls Feishu OpenAPI.
- `scripts/feishu/listener.py` consumes Feishu events through `lark-cli` and starts background subprocesses.
- `scripts/run_daily.py`, `scripts/topics/daily_topics.py`, `scripts/topics/prepare_topic.py`, `tools/asr/transcribe.py` call push helpers for notifications.

Conflict: V0.6.2 treats Feishu as a thin binding/view, not the source of truth or direct runtime authority. Sends must go through Hermes/Core/outbox/receipt handling.

### Legacy Model Routing Break

Current tree has no `scripts/reverse/call.py`. The legacy model call surface is `scripts/llm/call.py`.

Observed behavior:

- `config/settings.yaml` declares `llm.provider: codex` and semantic routes such as `reverse_dna: codex-mid`.
- `scripts/llm/call.py` maps only Claude aliases specially and shells out to `claude -p --model <route-or-alias>`.
- Consumers include `scripts/reverse/dna.py`, `scripts/reverse/reduce.py`, `scripts/research/research.py`, `scripts/humanize/**`, and `scripts/language_fuel/**`.
- The path does not record ModelGateway envelopes, provider/model metadata, input/output hashes, usage, cost, retries, or correlation IDs.

Conflict: V0.6.2 model calls must go through `scripts/core/model_gateway/goal07_model_gateway.py` and provider adapters such as `scripts/core/model_gateway/hermes_model_provider.py`.

### Skill Portability Gap

Local `.agents/skills/**` are useful but not yet formal portable Skills for production use.

Observed behavior:

- 23 local Skill folders were found.
- Many encode workflow instructions around `.claude/skills/**`, task-card files, local scripts, and local DB-backed outputs.
- Several mention or call scripts that write business state: `creation-save-draft`, `creation-prepare-topic`, `creation-workflow`, `create-domain-account`, `register-competitor-account`, `analyze-hit-dna`.
- Most short atomic creation skills lack explicit machine-checkable public input/output schema sections, though their prose describes inputs/outputs.

Conflict: V0.6.2 formal Skills must be portable, single-purpose, versioned, schema-bound, and invoked through the runtime boundary, not by host-local side effects.

### Migration Scripts Are Unsafe As Runtime

Legacy migration scripts under `scripts/db/migrate_*.py` use top-level or direct SQLite mutation patterns and target `data/creation.db`.

Conflict: They are migration inventory only. Production migration needs planned, reversible, validated migration jobs and backup/restore checks.

## Reuse Policy

- Keep: formal V0.6.2 Core modules and external live gate harness.
- Wrap: real platform/runtime adapters with valuable working code: ASR, MediaCrawler, Feishu client, Douban/NetEase/language collectors.
- Adapt: deterministic domain logic and content workflow data: hit detection, observation windows, candidate pool, topic/draft/check logic, prompt/manual content, Skill contracts.
- Replace: authority paths that directly mutate state, call models, send messages, or perform production scheduling outside Core.

## Forbidden In Production Until Migrated

- Running `scripts/run_daily.py` as production scheduler.
- Running `scripts/feishu/listener.py` as production command source.
- Running `scripts/llm/call.py` for formal model generation.
- Running `scripts/reverse/dna.py --batch` before model routing and persistence boundaries are migrated.
- Running DB migrations directly against `data/creation.db` as part of V0.6.2 production migration.
- Letting Skills advance business state except through Core/Materializer commands.

## Current Formal Gate Status

- ModelGateway exists and was live-validated through Hermes provider: `GATE-MODEL-PROVIDER LIVE_PASSED`, one call, no retry, visible output matched `MODEL_GATE_OK`.
- Hermes Host API exists and was live-validated: `GATE-HERMES-REAL-HOST LIVE_PASSED`, authenticated chat request, no dry-run fallback.
- ASR local runtime was live-validated: `GATE-ASR LIVE_PASSED`, external network call false.

Remaining gap: legacy business consumers still bypass these formal surfaces.

## Audit Conclusion

The repository is viable for staged V0.6.2 migration, but only if legacy business paths remain disabled until each path is moved behind Core, ModelGateway, Hermes Host binding, formal Scheduler/outbox and validated adapters. The next implementation work should not be another live DNA run; it should isolate legacy writes and route the next model-dependent legacy node through the formal gateway in shadow/test mode first.

## Formal Skill Mapping Addendum

Current repo does not contain a production formal Skill repository populated with all business Skills. The formal portable Skill runtime primitives exist in `scripts/core/model_gateway/goal07_skill_runner.py` (`PortableSkillSpec`, `PortableSkillRunner`) and are validated by GOAL-07/GOAL-12 tests, but local `.agents/skills/**` remain candidate/operator workflow assets until individually promoted.

Current mapping:

- `analyze-hit-dna` -> legacy `scripts/reverse/dna.py`; gap: no portable input/output schema, direct legacy model shim, direct DB/file write.
- DNA commonality/tactic extraction -> legacy `scripts/reverse/reduce.py`; gap: no formal Skill package and outputs are not Materializer-owned.
- `creation-prepare-topic` -> `scripts/topics/prepare_topic.py`; gap: directly orchestrates DNA/research/material package and can notify Feishu.
- `creation-banned-check` -> `scripts/content/check_banned.py`; lowest-risk candidate because it is deterministic and LLM-free, but needs machine-readable formal result schema.
- `creation-save-draft` -> `scripts/content/save_draft.py`; gap: direct draft DB insert.
- `register-competitor-account` -> `scripts/collect/register_competitor.py`; gap: direct SQLite write.
- `create-domain-account` -> `scripts/setup/create_domain_account.py`; gap: writes YAML/persona/SQLite directly.
- writing/review/polish workflow Skills -> `.claude/skills/**` manuals and local files; gap: host-bound paths, prose I/O, no standalone runtime package.
- `research-collector` and `topic-planner` -> richer local Skill docs with scripts; gap: not yet bound to formal Core/Materializer as production Skills.

Validation required before any Skill is considered formal: explicit public input schema, public output schema, version/hash, standalone fixture, no direct DB dependency, no state advancement except through Core command, no hidden call to another Skill, and ModelGateway/Runner envelope when LLM is used.

## Exhaustive Risk Classes Checked

Static scans covered Python, BAT, PowerShell-like scripts, YAML, Markdown, local Skill files, model calls, database writes, file/Vault writes, Feishu sends, subprocess launches, hardcoded route aliases, direct provider calls and formal Core bypasses. The detailed per-module result is in `MODULE_REUSE_MATRIX.yaml`.
