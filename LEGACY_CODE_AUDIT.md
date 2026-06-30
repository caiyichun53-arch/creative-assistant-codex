# LEGACY_CODE_AUDIT

Status: `STATIC_AUDIT_COMPLETE_RUNTIME_LIMITED`

This audit covers GOAL-00 only. It does not start GOAL-01 and does not modify formal business code.

## Baseline Authority

Priority order used:
1. Current user instruction for GOAL-00.
2. `I:\爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`.
3. Current repository code and legacy documents as old-system facts only.

Frozen V0.6.2 facts applied:
- Hermes is production Host.
- OpenClaw is only a fallback Host candidate.
- Codex is development/audit/migration/validation only.
- No independent Agent Runtime should be developed.
- Feishu must not bypass Hermes to drive business state.
- Core API is the formal state-change entry.
- Materializer owns formal persistence, versioning, pointers, audit, outbox and idempotency.
- Portable Skill, Host Binding, Runner, ModelGateway, Core and Materializer must remain separate.
- Adapter, Skill, Hermes, Feishu and Codex must not directly operate the formal DB/ORM/state machine.
- Obsidian is only derived view/knowledge projection, not business-state truth.
- Formal research must not mix in video-platform search, ASR, comments or video analysis.
- Drafts, research, experience, evidence, config and prompts must keep immutable versions, not in-place overwrite.
- Approved script and actual published script must be separated.
- Formal Skill must not silently modify itself or directly publish.

## Architecture Diagnosis

The repository is a working legacy MVP, not a V0.6.2 production architecture. Its useful assets are real: MediaCrawler integration, ASR pipeline, baseline/hit formulas, local database, comments, language fuel, reverse-DNA artifacts, deterministic banned-word checker, writer dispatch lessons and operational logs.

The dominant architectural conflict is authority boundary: old scripts directly mutate SQLite and files. V0.6.2 requires formal state changes through Core API and persistence through Materializer, with Hermes as production host and adapters/skills unable to write business state directly.

## Required Module Coverage

The full module classification is in `MODULE_REUSE_MATRIX.yaml`.

Coverage mapping:
- Core API, business services, state changes, DB access: `core_api_state_materializer`
- Account/domain/account-domain binding: `account_domain_binding`
- Video/snapshot/discovery/publish/D0-D7/P+ model: `video_snapshot_time_model`
- MediaCrawler collection: `mediacrawler_collection`
- NetEase Cloud collection: `netease_music_collection`
- Douban/Zhihu/Bilibili/XHS and other language/material collection: `douban_zhihu_bilibili_xhs_language_fuel`
- ASR/audio/transcription: `asr_audio_transcription`
- Comment collection/early/final/filtering: `comment_collection_filtering`
- Baseline/hit/candidate/clustering: `baseline_hit_candidate_clustering`
- Formal research SearchProvider/Fetcher: `formal_research_search_provider_fetcher`
- Topic/research/content plan/brief/draft/review: `topic_plan_brief_draft_review`
- Prompt/Skill/Binding/config/model routing: `prompt_skill_binding_model_routes`
- Experience/evidence/content atoms/preferences/P+/experiments/correction: `experience_evidence_atoms_preferences`
- ModelGateway and model call entrypoints: `model_gateway`
- Feishu/Hermes/OpenClaw/Codex interfaces: `feishu_hermes_openclaw_codex_interfaces`
- Scheduler/Job/Worker/retry/recovery/idempotency: `scheduler_job_worker_retry_idempotency`
- Obsidian and file projections: `obsidian_file_projection`
- DB migrations and historical migration: `database_migrations_history_migration`
- Tests/fixture/replay/FakeClock/fault injection: `tests_fixtures_replay_fakeclock_faults`
- Docker/WSL2/Windows scripts/ops: `docker_wsl_windows_ops_tools`
- Deterministic banned-word checker: `banned_words_deterministic_checker`
- Existing runtime data/artifacts: `existing_runtime_data_and_artifacts`

## Forbidden Item Check

Each item is classified as `found`, `not_found`, or `uncertain`.

| Check | Status | Evidence | Risk | Migration decision |
| --- | --- | --- | --- | --- |
| Feishu directly replaces/bypasses Hermes | found | `scripts/feishu/listener.py:66-82`, `scripts/feishu/listener.py:93-118`, `scripts/feishu/listener.py:194-200`; V0.6.2 requires `飞书 -> Hermes -> Core API` at `V0.6.2:3484-3512` | High: Feishu can mutate topics/hits and start workers without Hermes/Core boundary | replace production listener with Hermes Host Binding; keep Feishu client/card code as adapter reference |
| Self-built generic Agent Runtime | not_found | No local runtime comparable to Hermes/OpenClaw found; `.codex/agents/*.toml` and `.claude/agents/写手.md` are host-bound agent configs | Medium: old creation command still assumes subagent orchestration | do not build runtime; migrate only thin Host Binding and Portable Skill Runner |
| Codex or Claude treated as production host | found | `AGENTS.md:6`, `.claude/commands/创作.md:8-31`, `.codex/agents/写手.toml:6-15`, `scripts/llm/call.py:111-114`; V0.6.2 says Codex development-only at `V0.6.2:6240-6243` | High: production content path inherits engineering host context | replace host-bound production path with Core Runner/ModelGateway and Hermes thin host |
| Adapter directly writes database | found | `scripts/music/collect_netease.py:288-358`, `scripts/language_fuel/collect_mediacrawler.py:300-348`, `scripts/language_fuel/douban/common.py:278-343`, `tools/asr/transcribe.py:166-172` | High: external adapters own formal state | wrap adapters; Materializer owns persistence |
| Skill directly operates ORM/SQLite/state machine/business tables | found | `scripts/reverse/dna.py:102`, `scripts/reverse/reduce.py:153-258`, `scripts/language_fuel/store_atoms.py:115-146`; `.claude` skills are prompt sources but script runners write state | High: generation/reverse skills can become state authorities | adapt skill logic into Portable Skills; Core/Materializer owns state writes |
| Hermes or Feishu entry directly writes business state | found for Feishu, not applicable for Hermes | `scripts/feishu/listener.py:73`, `scripts/feishu/listener.py:96`, `scripts/feishu/listener.py:112`; no Hermes implementation present | High | replace Feishu listener; implement Hermes thin tool allowlist |
| subprocess/local CLI used as ModelGateway | found | `scripts/llm/call.py:111-114`, `scripts/project_check.py:96-104` | High: no gateway manifest, budget, cache, audit or provider boundary | replace with formal ModelGateway; keep route vocabulary |
| Formal Skill can silently modify or directly publish | found/uncertain | `scripts/reverse/upgrade_examples.py:144` writes upgraded examples; `.agents/skills/*` are editable local files; no production loader allowlist found | High: candidate/formal Skill separation absent | formal Skill repository must be read-only to production; candidate workspace isolated |
| Formal research reads video/ASR/comment/video-analysis artifacts | uncertain/found by design coupling | `scripts/topics/prepare_topic.py:136-140` requires source_hit/DNA; `scripts/research/research.py:24-44` uses topic/hit context; V0.6.2 forbids formal research mixing video chain at `V0.6.2:7009` | High: research and reverse material can blend | build formal SearchProvider/Fetcher with explicit evidence boundaries |
| Drafts overwritten in place | found | `scripts/topics/prepare_topic.py:123-130` writes `brief_full.md` and `brief.md`; `scripts/content/build_writer_dispatch.py:368-370` writes dispatch files; `scripts/content/save_draft.py:33-46` uses latest `draft_v*.md` convention | High: script_version immutability missing | create immutable `script_version` chain; no in-place formal overwrites |
| Research results overwritten in place | found | `scripts/research/research.py:42-44` writes `research.md`; `scripts/topics/prepare_topic.py` reruns can overwrite brief/research artifacts | High | create immutable research_plan/claim/evidence versions |
| Prompt/config/Skill/experience overwritten in place | found | `scripts/reverse/upgrade_examples.py:144`, `scripts/reverse/reduce.py:231-258`, `scripts/language_fuel/obsidian.py:144`, `config/settings.yaml` unversioned local runtime config | High | formal version roots and projection-only vault writes |
| Only references `current` object, no frozen version | found | `scripts/db/schema.sql:132-135` current_heat/status; `scripts/db/schema.sql:147-158` drafts without immutable root/pointer; V0.6.2 requires version/current pointer at `V0.6.2:4544-4599` | High | introduce roots/revisions/current pointers with audit |
| Approved script and actual published script mixed | found | `scripts/db/schema.sql:147-158` draft status has draft/approved/published only; no publication_capture model; V0.6.2 requires separation at `V0.6.2:3671-3673` | High | add approved vs publication capture script versions |
| Missing traceability for model/prompt/skill/binding/config/input/output/human edits | found | `scripts/llm/call.py` returns raw text only; `scripts/content/diff_draft.py:112-113` records diff path and edit counts but no component manifest | High | ModelGateway/Runner must emit input_hash/output_hash/component_manifest |
| Missing command receipt/audit/outbox/correlation_id/causation_id/idempotency | found | No `correlation_id`, `causation_id`, `outbox`, `command receipt` hits in repo code; V0.6.2 requires these at `V0.6.2:5284-5287`, `V0.6.2:6568` | High | implement command receipt, audit_event, outbox, idempotency |
| Tests use different business logic from production | uncertain | No comprehensive tests found; `scripts/project_check.py` is environment/readiness check, not business replay; vendor tests cover MediaCrawler internals | Medium | build fake/replay tests through same Core handlers |
| Production cannot start after deleting fixtures/test assets | uncertain | No first-class fixture/replay directories found; production currently depends on local `data/`, `.venv`, `vendor`, `.env`; no clean-room deletion test exists | Medium | add clean-room startup validation and fixture deletion safety |
| Code changes V0.6.2 boundary to preserve legacy | not_found | No GOAL-01 changes performed; audit uses V0.6.2 as authority | Low | keep audit-only discipline |
| Unnecessary Redis/Celery/Temporal/vector DB/message queue/multi-agent system | not_found for infra; found for host-bound subagent workflow | No Redis/Celery/Temporal/vector DB dependencies found; `.claude/commands/创作.md:24-31` assumes writer subagent workflow | Medium | do not add infra; migrate writing to Portable Skill/Runner |
| Domain differences hardcoded in core code | found | `scripts/content/build_writer_dispatch.py:311-346` hardcodes `张芝士` and `泛科普`; `scripts/collect/register_competitor.py:55` default `泛科普`; `scripts/collect/register_full.py:67` default `泛科普` | Medium | convert domain/account to data inputs; remove core hardcoding |
| Historical data migration/file reference migration omitted | found | No migration manifest; direct SQLite DB and many file paths under `data/`, `vault`, logs; migrations have no ledger | High | create read-only extraction, file reference scan, migration manifest |

## Key Findings

1. Highest-risk conflict: there is no Core API/Materializer boundary.
   Evidence: direct writes in `scripts/analyze/judge_hits.py:81-104`, `scripts/topics/daily_topics.py:42-73`, `scripts/feishu/listener.py:73-112`, `scripts/reverse/dna.py:102`.

2. Feishu is currently a production control surface, contrary to V0.6.2.
   Evidence: `scripts/feishu/listener.py` directly sets topic state, writes transcript path/reverse status and starts local workers. V0.6.2 freezes Hermes as production host and Feishu as a Hermes-mediated interface.

3. ModelGateway is absent as a formal boundary.
   Evidence: `scripts/llm/call.py:111-114` shells out to `claude -p`; `config/settings.yaml:14-35` declares Codex routes, but the call implementation still maps Claude aliases and does not record provider/model/input/output manifests.

4. Existing collection and ASR capabilities are valuable but must be wrapped.
   Evidence: MediaCrawler and ASR scripts contain real Windows/runtime fixes and normalization, but they directly write SQLite, files and states.

5. Immutable versions are mostly absent.
   Evidence: drafts, research, brief files, Obsidian methods/examples and config are file/path oriented, with only lightweight draft version numbers. V0.6.2 requires immutable roots/revisions/pointers and published/actual separation.

6. Validation foundation is too weak for migration.
   Evidence: `project_check.py` passes environment readiness, but no fake Core/Host/ModelGateway, replay fixture, FakeClock, migration ledger or clean-room deletion suite exists.

## Two-Round Review

### Round 1: Prevent Over-Rewrite

Checked all `replace` decisions:

- `core_api_state_materializer`: replace retained. There is no existing Core/Materializer implementation to keep/adapt/wrap. Direct-write scripts cannot be wrapped without preserving wrong state authority.
- `model_gateway`: replace retained. The old CLI subprocess pattern can inspire provider adapters, but as a gateway it lacks required audit, budget, cache and manifests.
- `feishu_hermes_openclaw_codex_interfaces`: replace retained for production entrypoint. Feishu push/card code can be reused as adapter reference, but direct listener state writes cannot remain.
- `database_migrations_history_migration`: replace retained for target migration system. Existing SQLite schema/data are migration inputs, not the formal persistence architecture.

No replace decision is based merely on code style or technology taste. Each is rooted in production authority boundary conflict.

### Round 2: Prevent Wrong Reuse and Over-Design

Checked all `keep`/`adapt`/`wrap` decisions:

- `keep` is used only for deterministic banned-word checker logic, with added requirement to record config version/hash in the formal pipeline.
- `wrap` modules are external abilities: MediaCrawler, NetEase, ASR and Obsidian projection. Wrap does not grant DB/state authority.
- `adapt` modules keep algorithms, field knowledge, workflow lessons and data, but require Core/Materializer, immutable versions and replay validation before production use.
- No recommendation adds Redis, Celery, Temporal, vector DB, message queue, multi-agent runtime or new business states outside V0.6.2.
- Domain-specific hardcoding is explicitly marked for removal, not preserved.
- Formal research is explicitly separated from video/ASR/comment material.

## Reuse Decision Summary

Counts from `MODULE_REUSE_MATRIX.yaml`:
- keep: 1
- adapt: 13
- wrap: 4
- replace: 4

High-risk migration areas:
- Core API / Materializer
- Feishu/Hermes binding
- ModelGateway
- SQLite to PostgreSQL and migration history
- MediaCrawler/ASR/comment adapters
- immutable content/research/experience versioning
- validation/replay foundation

Directly reusable:
- `scripts/content/check_banned.py` logic and `config/banned_words.yaml`, after version/hash integration.

Reusable with adapt:
- baseline/hit formulas
- D0-D7 observation concepts
- candidate decay/reheat
- topic/brief lessons
- diff-based human edit learning
- reduce dimensions and example material
- account/domain seed data
- existing runtime data as migration input

Reusable with wrap:
- MediaCrawler execution capability
- NetEase collection
- ASR pipeline
- Obsidian projection templates

Must replace as production authority:
- Core/API/Materializer absence and direct writes
- local CLI subprocess as ModelGateway
- Feishu direct listener as production host
- SQLite migration system as formal persistence

## GOAL-01 Gate

GOAL-01 is not allowed to start automatically.

Reason:
- V0.6.2 says GOAL-00 has a user approval gate for the reuse matrix.
- Several high-risk migration items require explicit confirmation before implementation.
