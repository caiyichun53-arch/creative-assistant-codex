# Upstream

- Original directory: `skills/systematic-debugging`
- Upstream commit: `d884ae04edebef577e82ff7c4e143debd0bbec99`
- Upstream repository: `https://github.com/obra/superpowers`

## Retained

- Root-cause-first method.
- Reproduce before fixing.
- Read full errors, paths, stack traces, and exit codes.
- Check recent changes.
- Verify actual entrypoints, config propagation, and component boundaries.
- Trace to the first bad value.
- Compare with working patterns.
- Use one hypothesis and one minimal experiment at a time.
- Fix only the root-cause location.
- Stop after repeated failed patches and re-check architecture.
- Supporting reference files for tracing, defense in depth, and condition-based waiting.

## Removed Or Rewritten

- Mandatory dependency on `superpowers:test-driven-development`.
- References to brainstorming, planning, worktrees, subagents, code review, branch finishing, and Git commit workflows.
- Upstream creation/test notes that are not needed by this project skill.

## Added Project Constraints

- `docs/EFFECTIVE_DESIGN_BASELINE.md` is the only business design authority.
- No old Goal, ROADMAP, BUILD_PLAN, archived audit, or chat-memory design authority.
- Confirm Windows/WSL paths before path-sensitive changes.
- No new fallback provider/model/database or silent bypass.
- No weakening of formal acceptance.
- No Mock or fixture-only proof for real workflow repair.

This project Skill is a local adaptation, not the upstream Superpowers original.
