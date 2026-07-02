# Current Repository Baseline - GOAL-00

status: `FROZEN_AUDIT_COMPLETE_CURRENT_FACTS_RECONCILED`

This baseline freezes the repository state for V0.6.2 migration planning. It is audit-only: no business code, production configuration, runtime data, external account, model prompt, Skill behavior, or database record is changed by GOAL-00.

## Authority

- Highest product baseline: `爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版`.
- Local constitution: `AGENTS.md`.
- Build route: `BUILD_PLAN.md`.
- Current branch: `audit/goal-00-v0.6.2`.
- Source validation branch fast-forwarded into this audit branch: `validation/v0.6.2-live-gates`.
- Current HEAD: `63966cbfa7bd3036968517aaa81e01edee68a130` (`feat(hermes-host): validate live API server gate`).
- RC freeze remains: `release/v0.6.2-rc1` exists in history; original tag `v0.6.2-rc1` targets `2c6b48bfcfe01d2a4cf617c2b6ec158fc51f47d7`.

## Git Site

- Working tree before document updates: clean.
- Existing GOAL-00 branch was not duplicated. `audit/goal-00-v0.6.2` existed and was a direct ancestor of `validation/v0.6.2-live-gates`; it was fast-forwarded to the current validated baseline.
- Recent baseline commits include Host Gate, Model Provider Gate, ASR Gate and the external live-gate harness commits.

## Current Data Baseline

Read-only inspection of `data/creation.db`:

- SHA256: `71023BB01D185BA520D21056C35B9882BAA0F06E49EE8F649CC814E8B5549512`
- Size: `13,242,368` bytes.
- Tables: `20`.
- Counts: `accounts=1`, `competitor_accounts=20`, `competitor_videos=2307`, `video_checks=1226`, `baselines=360`, `hits=109`, `hit_comments=5713`, `topics=106`, `drafts=1`.
- DNA status from `python scripts/reverse/dna.py --status`: `81/95` completed, `14` remaining.
- `hits.reverse_status`: `done=81`, `transcribed=17`, `none=11`.
- No DNA batch or business write was run in this GOAL-00 pass.

## Runtime Baseline

`python scripts/project_check.py` passed and reported:

- Codex CLI: `codex-cli 0.140.0`.
- Main Python visible to MediaCrawler tooling: `Python 3.13.5`.
- ASR venv Python: `Python 3.11.13`.
- MediaCrawler `.venv312` Python: `Python 3.12.13`.
- `uv`: `0.8.17`.
- `lark-oapi` SDK installed.
- `vendor/MediaCrawler` present.

## External Live Gate Baseline

From `EXTERNAL_LIVE_GATE_STATUS.yaml` and validation evidence:

- `GATE-ASR`: `LIVE_PASSED`, evidence `validation_evidence/GATE-ASR/20260701T203209`.
- `GATE-MODEL-PROVIDER`: `LIVE_PASSED`, evidence `validation_evidence/GATE-MODEL-PROVIDER/20260701T235527`; provider `hermes`, model `xiaomi/mimo-v2.5-pro`, `actual_call_count=1`, `retry_count=0`, `gate_max_output_tokens=128`, visible output matched `MODEL_GATE_OK`.
- `GATE-HERMES-REAL-HOST`: `LIVE_PASSED`, evidence `validation_evidence/GATE-HERMES-REAL-HOST/20260702T004807`; local host `http://127.0.0.1:8642`, authenticated chat succeeded, `actual_call_count=1`, `retry_count=0`, `dry_run_fallback=false`.
- Other external gates remain dry-run only unless explicitly opened later.
- `EXTERNAL_LIVE_GATE_STATUS.yaml` top-level status still reads `DRY_RUN_PASSED`; per-gate statuses are authoritative for opened gates.

## Existing V0.6.2 Core Surfaces

Formal V0.6.2 implementation exists under `scripts/core/**`: persistence, Core state, scheduler, runtime host, workflow, formal research, ModelGateway, Hermes host binding, production/correction/experience/staging verifiers.

These are the preferred migration targets. Legacy scripts should not regain production authority where a formal Core surface exists.

## Legacy Execution Surfaces Still Present

Legacy MVP entrypoints remain as data-compatible source material:

- Daily chain: `scripts/run_daily.py`, `scripts/collect/**`, `scripts/analyze/**`, `scripts/topics/**`.
- Feishu live listener/direct push: `scripts/feishu/listener.py`, `scripts/feishu/push.py`, `scripts/feishu/client.py`.
- ASR implementation: `tools/asr/transcribe.py`, `tools/asr/reverse_prep_worker.py`.
- Legacy model shim: `scripts/llm/call.py`.
- Reverse/experience/language-fuel scripts: `scripts/reverse/**`, `scripts/humanize/**`, `scripts/language_fuel/**`, `scripts/music/**`, `scripts/research/**`.
- Local Skill wrappers: `.agents/skills/**` and legacy Claude manuals under `.claude/skills/**`.

## Key Baseline Risks

1. Legacy scripts directly mutate SQLite/files without Core command envelopes, immutable versions, outbox idempotency, or formal audit receipts.
2. `scripts/llm/call.py` still shells out to `claude -p` while `config/settings.yaml` declares `provider: codex` and routes such as `reverse_dna: codex-mid`. Current tree has no `scripts/reverse/call.py`; the model routing break is in the shared legacy shim.
3. Several scripts push directly to Feishu or start long-running Feishu/listener subprocesses. They must remain disabled for production migration until wrapped behind Hermes/Core/outbox.
4. Skill files are useful workflow contracts but many are host-bound and not formal portable Skills with strict public input/output schemas.
5. Current SQLite data is the historical truth source for migration, but not the target production persistence boundary for V0.6.2.

## GOAL-00 Check Status

- `python scripts/project_check.py`: passed.
- AST parse over project `scripts/` and `tools/` Python files excluding virtual environments: `105` files, `0` parse errors.
- SQLite schema dry-run in memory: legacy `scripts/db/schema.sql` plus formal GOAL-01/02/03 SQLite schemas passed.
- `python scripts/reverse/dna.py --status`: read-only status succeeded.
- No live business chain, DNA write, Feishu send, production model request, collector crawl, migration, or DB mutation was executed.

## Environment And Ignored File Addendum

- Playwright command found: `C:\Users\15891\anaconda3\Scripts\playwright.exe`.
- Node launcher found: `C:\Program Files\nodejs\npx.cmd`.
- `chrome`, `chrome.exe`, `msedge`, and `msedge.exe` were not found in PATH during this audit; browser availability should be treated as environment-specific and validated again before any browser-dependent Goal.
- `.playwright-cli/` contains historical console/page snapshots from 2026-06-18 and is not a production runtime source.
- `.env.live-gates` remains ignored and was not diffed or committed.
- `config/live_gates.yaml` is ignored in the local working tree; committed examples remain placeholder-only.
- Local ignored Hermes test-profile material under the workspace mirror is treated as gate evidence/runtime residue, not repository source.

## Execution Entrypoint Coverage

Entrypoints were identified by static search for `argparse`, `def main`, `if __name__ == "__main__"`, BAT launchers and validation runners. They fall into these groups:

- Formal V0.6.2 verifiers: `scripts/core/**/verify_goal_*.py` and `scripts/validation/live_gates.py`.
- Legacy business scripts: collect/analyze/topic/reverse/research/content/language-fuel/music/humanize modules under `scripts/**`.
- Legacy runtime launchers: `scripts/run_daily.py`, `scripts/feishu/listener.py`, root BAT files.
- Local tools: `tools/asr/transcribe.py`, `tools/asr/reverse_prep_worker.py`.
- Diagnostics: `scripts/project_check.py`.

No entrypoint outside documentation/validation was executed for business effect in this GOAL-00 pass.
