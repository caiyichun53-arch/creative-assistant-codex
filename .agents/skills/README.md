# `.agents/skills/` — not a production-loaded path

Per `LEGACY_RETIREMENT_REPORT.md` ("Treat host-bound `.claude`, `.codex/agents` and
`.agents/skills` assets as local workflow/source material, not production-loaded formal
Skills") and `LEGACY_RETIREMENT_MATRIX.yaml`: the SKILL.md files under this directory are
kept as historical/reference material only. They are **not** scanned or loaded by any
runtime path.

**The one production-loading path for business Skills is `runtime_skills/`** (12 business
Skills + 1 test probe, `runtime_probe`), invoked through `scripts/core/model_gateway/`. See
`AGENTS.md` and `HANDOFF_STATE.md` for current binding status.

`.claude/skills/*` is a second, still-actively-used-but-not-production-loaded path: those
are interactive, Claude-Code-session-driven Skills (选题/大纲/钩子/成稿/审稿/etc.),
scheduled for eventual retirement in favor of `runtime_skills/` per the 2026-07-09 decision
recorded in `AGENTS.md`, but not yet migrated.

Several SKILL.md files here (e.g. `analyze-hit-dna`) reference scripts
(`scripts/reverse/dna.py`, `scripts/llm/call.py`, `scripts/collect/*`, `scripts/topics/*`,
`scripts/content/*`) that were deleted in the 2026-07-04 legacy removal (commit
`46b421c`). Their documented trigger chains no longer resolve to a real script. Do not
treat any file in this directory as evidence that a capability is currently running in
production — check `runtime_skills/` and `scripts/core/experience/run_*.py` instead.
