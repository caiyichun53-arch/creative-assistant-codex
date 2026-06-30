# CURRENT_REPOSITORY_BASELINE

## 1. Baseline status

- Audit goal: `GOAL-00 — Legacy Code Audit`
- Design authority: `爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版`
- Repository: `caiyichun53-arch/creative-assistant-codex`
- Default branch: `main`
- Frozen source commit: `a8787a439fd281b2d8cec7a7ffe2606426bebab4`
- Frozen source commit message: `Initial sanitized system upload`
- Frozen source commit time: `2026-06-30T17:02:07Z`
- Independent audit branch: `audit/goal-00-v0.6.2`
- Audit status: `blocked_for_runtime_validation`
- Business-code modifications: none
- Migration-history modifications: none
- Deletions, reset, mass formatting, dependency upgrades: none

The remote source tree was frozen by creating the audit branch from the exact source commit. This freezes the repository content visible to GitHub, but it does **not** freeze or reveal the user's local working tree.

## 2. Git现场

| Item | Result | Confidence / limitation |
|---|---|---|
| Repository access | Accessible through GitHub contents/commit APIs | Confirmed |
| Default branch | `main` | Confirmed |
| HEAD | `a8787a439fd281b2d8cec7a7ffe2606426bebab4` | Confirmed |
| Independent audit branch | `audit/goal-00-v0.6.2` | Confirmed |
| Tags | No tag was exposed by the available repository metadata/search path | Not equivalent to a local `git tag --list` |
| Remote commit history | One visible initial sanitized upload was inspected | Search indexing was inconsistent; commit fetch is the source of truth |
| Remote uncommitted files | Not applicable | GitHub only stores committed state |
| Local uncommitted/untracked files | Unknown | The audit environment has no access to the user's local worktree |
| Submodules/vendor revisions | Not frozen in repository | `vendor/MediaCrawler` is ignored and absent |
| Local database/data/vault | Not frozen in repository | `data/` and `vault/` are ignored and absent |

### Safety conclusion

The committed remote tree is safely frozen. The **actual local production/development scene is not completely frozen** because local-only runtime assets and any uncommitted/untracked files are invisible. This is a formal GOAL-00 blocker, not a reason to discard the static audit.

## 3. Repository inventory

The frozen commit contains approximately 134 tracked files. Main groups:

- Governance and route documents: `README.md`, `AGENTS.md`, `CLAUDE.md`, `BUILD_PLAN.md`
- Codex/OpenAI skills and agents: `.agents/skills/**`, `.codex/agents/**`
- Claude legacy runtime/manuals: `.claude/**`
- Configuration examples: `.env.example`, `config/settings.example.yaml`, `config/accounts/example.yaml`, `config/domains/example.yaml`, `config/banned_words.yaml`
- SQLite schema and sequential scripts: `scripts/db/schema.sql`, `scripts/db/migrate_001_*.py` through `migrate_010_*.py`, `init_db.py`, `seed_mvp.py`
- Acquisition and normalization: `scripts/collect/**`, `scripts/language_fuel/**`, `scripts/music/collect_netease.py`
- Analysis and experience extraction: `scripts/analyze/**`, `scripts/reverse/**`, `scripts/humanize/**`
- Topic/research/content flow: `scripts/topics/**`, `scripts/research/**`, `scripts/content/**`
- Host and operations: `scripts/feishu/**`, `scripts/run_daily.py`, Windows BAT/VBS launchers
- ASR tools: `tools/asr/**`
- Templates: `templates/**`

Not present in the tracked tree:

- `pyproject.toml`, `requirements.txt`, lockfile or equivalent complete dependency manifest
- FastAPI application, SQLAlchemy models, Alembic environment/migration heads
- PostgreSQL schema/migration implementation
- Dockerfile, Docker Compose, WSL2 bootstrap or deployment manifests
- GitHub Actions or other CI workflow
- Dedicated `tests/` tree
- fixture/replay/FakeClock/fault-injection suites
- committed `vendor/MediaCrawler`
- committed runnable `config/settings.yaml`
- committed database, samples, vault material or Obsidian derived content
- a Core API, Materializer, Outbox, command receipt or durable Job/attempt implementation

## 4. Dependency and runtime baseline

### Declared/inferred runtime

The code is a Windows-local Python script system. Direct imports and scripts indicate at least:

- Python 3.x
- PyYAML
- SQLite from Python standard library
- `lark-oapi`
- `uv`
- MediaCrawler and its own environment
- local ASR environment and models
- FFmpeg/tooling used by ASR scripts
- `claude` CLI and/or `codex` CLI
- PowerShell and Windows Task Scheduler
- network access for Feishu, Douyin, web research, Douban, NetEase and other platforms

### Dependency reproducibility

Dependency reproducibility is **not established**. The repository contains configuration examples and environment probes, but no authoritative install manifest or lock. Several required dependencies are machine-specific and gitignored.

### Platform assumptions

Multiple paths and process-control routines are Windows-specific:

- `Scripts/python.exe`
- `pythonw.exe`
- `CREATE_NO_WINDOW`
- PowerShell process discovery/termination
- Windows Task Scheduler
- BAT/VBS launchers

The source is therefore not currently portable to the V0.6.2 deployment baseline without adaptation/replacement of the runtime layer.

## 5. Database and migration baseline

### Current implementation

- Primary store: SQLite at `data/creation.db`
- Access pattern: raw `sqlite3` calls and SQL distributed across scripts
- Schema bootstrap: `scripts/db/schema.sql`
- Migration mechanism: numbered standalone Python scripts `migrate_001`—`migrate_010`
- Initialization: `scripts/db/init_db.py`
- Seed: `scripts/db/seed_mvp.py`

### Current schema families

The schema includes:

- self-owned and competitor accounts
- competitor videos, observation view and snapshots
- baselines and hits
- topics and candidate view
- drafts and diffs
- self-owned tracking
- hit comments
- NetEase tracks/comments
- language-fuel items/comments/insights
- content atoms
- language-texture observations

### Migration safety findings

- No Alembic migration graph or PostgreSQL migration history exists.
- `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, while numbered scripts apply ad hoc SQLite changes.
- The committed repository does not include the actual production SQLite database, so real schema drift and migration completion cannot be verified.
- Migration scripts have not been executed in this audit environment.
- Existing data must be treated as migration-critical; no table may be dropped or rewritten merely to match new naming.

## 6. Configuration and secret baseline

Tracked:

- `.env.example`
- `config/settings.example.yaml`
- account/domain examples
- banned-word configuration
- `.gitignore`

Intentionally untracked:

- `.env`
- `config/settings.yaml`
- real account/domain configurations
- login state/cookies
- data, outputs, logs, vault and vendor trees

No real credential was read or requested. This is correct for secret hygiene, but it prevents full runtime validation and means the remote repository is a sanitized source package rather than a complete reproducible runtime package.

## 7. Existing tests, fixtures and validation tools

### Present

- `scripts/project_check.py`: local environment and asset probe
- `tools/asr/asr_test.py`: ASR-oriented test utility
- validators inside some newer skills, including research/topic output validators
- script-level `--status`, dry-run or idempotency logic in several modules
- historical validation claims in `BUILD_PLAN.md`

### Absent or not demonstrated

- unit-test framework/configuration
- integration-test suite
- isolated test database/schema/storage/vault
- fixture packs for every external adapter
- replay captures
- FakeClock
- deterministic fault injection
- crash/retry/idempotency tests for production handlers
- clean-room proof that production starts after validation assets are removed

Historical manual-test claims are useful evidence, but they are not a reproducible test suite in the frozen repository.

## 8. Startup paths

Documented entry points include:

```text
python scripts/db/init_db.py
python scripts/project_check.py
python scripts/run_daily.py
python scripts/reverse/dna.py --status
```

Other operational paths include:

- direct Feishu listener
- Windows Task Scheduler daily script
- background ASR/reverse-prep worker
- BAT/VBS launchers
- direct CLI model invocation

These paths are not the V0.6.2 target runtime. They are legacy implementation evidence to classify and migrate.

## 9. Runtime validation attempted in GOAL-00

| Validation | Result |
|---|---|
| GitHub repository and exact-file reads | Passed |
| Fetch frozen commit and complete initial diff/file inventory | Passed |
| Create isolated audit branch | Passed |
| Clone repository into executable container | Blocked: environment DNS could not resolve `github.com` |
| Inspect local user's dirty/untracked worktree | Blocked: remote connector has no local-machine access |
| Install dependencies | Not run: no checkout and no authoritative manifest |
| Initialize/inspect real database | Not run: data DB is absent and local-only |
| Run unit/integration tests | Not run: no tracked suite and no checkout |
| Run migration scripts | Not run: no checkout and no disposable copy of real DB |
| Run `project_check.py` | Not run: local-only settings/vendor/vault/data are absent |
| Run minimal startup | Not run: runtime assets and credentials are absent |
| Run external adapter smoke tests | Not run: real credentials/platform access are explicitly outside this audit |

## 10. Baseline verdict

The committed code is sufficient for a substantive static audit and a proposed reuse matrix. It is **not sufficient to declare the repository currently runnable or to formally complete GOAL-00**.

Formal completion requires, at minimum:

1. a local baseline export showing branch, HEAD, tags, `git status --short`, untracked files and relevant ignored runtime directories;
2. a reproducible source package or accessible worktree containing all non-secret code needed to run;
3. an authoritative dependency/install path;
4. a disposable copy or schema dump of the real SQLite database;
5. execution of the existing check/start/migration paths without modifying business code;
6. user confirmation of the proposed reuse matrix.
