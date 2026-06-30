# CURRENT_REPOSITORY_BASELINE

GOAL: GOAL-00 old-code audit only.

Design baseline:
- `I:\爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.docx`
- Read status: readable as DOCX. Main document extracted for search during audit; extraction was temporary.
- Extracted main document size: 203010 characters, 7327 paragraphs.

## Git Frozen Site

Read-only checks were performed first on `main`, then the clean worktree was switched to the requested audit branch before writing audit files.

- Initial branch: `main`
- Audit branch: `audit/goal-00-v0.6.2`
- HEAD: `a8787a439fd281b2d8cec7a7ffe2606426bebab4`
- Last commit: `a8787a4 Initial sanitized system upload`
- Tags at HEAD: none
- Initial `git status --short`: clean
- Initial `git ls-files --others --exclude-standard`: empty
- `git submodule status`: no submodules reported

Safety decision:
- Worktree was clean before audit files were written.
- No reset, stash, clean, dependency upgrade, migration run, database write, or business-code edit was performed.
- Branch creation was necessary before writing audit files to avoid modifying `main`.

## Repository Root

Root: `I:\Creation_assistant-codex`

Top-level tracked/working areas:
- `.agents/`: Codex-local skills, including creation workflow and project review advisor.
- `.claude/`: legacy Claude-side agents, commands and skills; `.claude/README_LEGACY.md` marks these as migration references.
- `.codex/`: Codex agent bindings.
- `config/`: settings, domain/account configs and banned words.
- `data/`: local runtime database, raw captures and generated artifacts.
- `logs/`: local runtime logs.
- `outputs/`: local test/output artifacts.
- `scripts/`: deterministic scripts for collection, analysis, topics, reverse engineering, research, content, Feishu and setup.
- `templates/`: writer dispatch and review templates.
- `tools/`: ASR and local startup helpers.
- `vault/`: junction to `I:\Obsidian\创作助手\经验库`.
- `vendor/`: local third-party MediaCrawler checkout.

Important top-level files:
- `AGENTS.md`
- `BUILD_PLAN.md`
- `CLAUDE.md`
- `README.md`
- `.env.example`
- `.env` exists locally and is not part of the safe audit surface.
- `启动逆向拆DNA.bat`
- `启动真人写作提炼.bat`

## Runtime Environment

- Shell: PowerShell
- Python on PATH: `Python 3.13.5`
- Python executable: `C:\Users\15891\anaconda3\python.exe`
- Pip executable: `C:\Users\15891\anaconda3\Scripts\pip.exe`
- Project self-check found Codex CLI: `codex-cli 0.140.0`
- ASR venv Python: `I:\Creation_assistant-codex\tools\asr\.venv\Scripts\python.exe`, `Python 3.11.13`
- MediaCrawler venv Python: `I:\Creation_assistant-codex\vendor\MediaCrawler\.venv312\Scripts\python.exe`, `Python 3.12.13`
- `uv`: `C:\Users\15891\.local\bin\uv.exe`, `uv 0.8.17`

## Dependency Declarations

Dependency declaration state:
- No root `pyproject.toml`, `requirements.txt`, `poetry.lock`, `uv.lock`, `Pipfile`, `package.json`, Dockerfile, or compose file was found at root.
- Runtime dependencies are currently implicit in local environments and scripts.
- `tools/asr/.venv/` exists locally.
- `vendor/MediaCrawler/.venv312/` exists locally.
- `config/settings.example.yaml` documents expected model, ASR, language fuel and platform configuration.

Risk:
- Reproducibility is weak because the root repo does not pin Python dependencies.
- V0.6.2 requires formal validation assets and replay/fake paths; current dependency setup is workstation-local.

## Database, Schema and Migrations

Database:
- Type: SQLite
- Main DB: `data/creation.db`
- Size observed: `13156352` bytes
- Access pattern in old code: many scripts call `sqlite3.connect(ROOT / "data" / "creation.db")` or shared `scripts/collect/common.py:79`.

Read-only database table counts:
- `accounts=1`
- `baselines=320`
- `competitor_accounts=20`
- `competitor_videos=2280`
- `content_atoms=0`
- `diffs=0`
- `drafts=1`
- `hit_comments=5713`
- `hits=106`
- `language_fuel_batches=27`
- `language_fuel_comments=5359`
- `language_fuel_insights=53`
- `language_fuel_items=1210`
- `language_texture_observations=51`
- `music_comment_batches=11`
- `music_comments=536`
- `music_tracks=11`
- `topics=103`
- `tracking=0`
- `video_checks=1040`

Schema file:
- `scripts/db/schema.sql`
- In-memory SQLite execution succeeded.
- Schema tables: `accounts`, `baselines`, `competitor_accounts`, `competitor_videos`, `content_atoms`, `diffs`, `drafts`, `hit_comments`, `hits`, `language_fuel_batches`, `language_fuel_comments`, `language_fuel_insights`, `language_fuel_items`, `language_texture_observations`, `music_comment_batches`, `music_comments`, `music_tracks`, `topics`, `tracking`, `video_checks`.

Migrations:
- `scripts/db/migrate_001_videos.py`
- `scripts/db/migrate_002_checks.py`
- `scripts/db/migrate_003_clusters.py`
- `scripts/db/migrate_004_music_sources.py`
- `scripts/db/migrate_005_language_fuel.py`
- `scripts/db/migrate_006_language_fuel_douban.py`
- `scripts/db/migrate_007_language_fuel_insights.py`
- `scripts/db/migrate_008_comments_and_atoms.py`
- `scripts/db/migrate_009_language_texture_observations.py`
- `scripts/db/migrate_010_topic_derive_source.py`

Migration risk:
- `migrate_001`, `migrate_002`, `migrate_003`, and `migrate_010` execute at top level and directly open the real DB if run/imported.
- No migration ledger table was found.
- No migration was executed during this audit.

## Config, Secrets and Local Assets

Config files:
- `config/settings.yaml` exists locally and may contain workstation paths.
- `config/settings.example.yaml` exists as non-secret template.
- `config/accounts/example.yaml`, `config/accounts/张芝士.yaml`
- `config/domains/example.yaml`, `config/domains/泛科普.yaml`
- `config/banned_words.yaml`

Secret-bearing or local-only areas:
- `.env`
- `data/`
- `logs/`
- `outputs/`
- `vault/` junction
- `vendor/`
- `.claude/settings.local.json`

## Startup, Worker and Operations Entrypoints

Startup/ops scripts:
- `scripts/run_daily.py`
- `scripts/feishu/listener.py`
- `tools/start_listener.vbs`
- `tools/asr/reverse_prep_worker.py`
- `tools/asr/transcribe.py`
- `启动逆向拆DNA.bat`
- `启动真人写作提炼.bat`

Known external call surfaces:
- Feishu OpenAPI via `scripts/feishu/client.py`.
- Feishu event stream via `lark-cli` in `scripts/feishu/listener.py`.
- MediaCrawler via subprocess in `scripts/collect/common.py`, `scripts/collect/crawl_competitors.py`, `scripts/language_fuel/collect_mediacrawler.py`, and `scripts/reverse/fetch_comments.py`.
- Douyin download links via `requests` in `tools/asr/transcribe.py`.
- NetEase Cloud Music APIs via `scripts/music/collect_netease.py`.
- Douban HTTP/Playwright collection via `scripts/language_fuel/douban/*`.
- Local `claude -p` subprocess via `scripts/llm/call.py`.

## Tests, Fixtures and Replay

Observed validation assets:
- `scripts/project_check.py`
- `vendor/MediaCrawler/test/`
- `vendor/MediaCrawler/tests/`
- Some local logs and outputs under `logs/` and `outputs/`

Missing or insufficient for V0.6.2:
- No root test runner configuration.
- No first-class fixture/replay suite for Core API, Materializer, ModelGateway, SearchProvider, Hermes binding, or DB migration.
- No FakeClock/fault-injection framework found in current repo.
- Existing validations are mostly smoke checks and live-environment readiness checks.

## Completeness Judgment

Current repo appears to be a complete local MVP/legacy working tree for the script-first system, not only a tiny detached fragment:
- Real SQLite DB exists and is readable.
- Vendor MediaCrawler is present.
- ASR venv is present.
- Logs and generated materials exist.
- Scripts cover collection, baseline/hits, topics, reverse, content, research, Feishu and humanizer flows.

However, relative to V0.6.2 it is not a complete production architecture:
- No Hermes production host binding.
- No Core API / Materializer boundary.
- No PostgreSQL persistence layer.
- No command receipt, outbox, correlation/causation ids, immutable version roots, or production-grade replay/fake validation layer.
