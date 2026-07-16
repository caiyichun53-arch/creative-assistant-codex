# Upstream

- Original directory: `skills/verification-before-completion`
- Upstream commit: `d884ae04edebef577e82ff7c4e143debd0bbec99`
- Upstream repository: `https://github.com/obra/superpowers`

## Retained

- Fresh evidence before completion claims.
- Identify, run, read, and verify the full command.
- Check exit code and failure count.
- Do not rely on old output, partial checks, or agent reports.
- Code changes do not prove a bug is fixed.
- Verification becomes invalid after later edits.

## Removed Or Rewritten

- Commit and PR-oriented language that is not required for this local task.
- Generic upstream examples that could encourage duplicate project gates.

## Added Project Constraints

- Current business Stage completion is decided only by the existing real workflow acceptance entry.
- Governance, Skill, and Hook changes use mechanism self-tests rather than unrelated production runs.
- Missing authoritative acceptance is a hard block, not permission to invent a weaker command.
- Static checks, Mock, fixture-only tests, and unit tests do not prove real business workflow completion.

This project Skill is a local adaptation, not the upstream Superpowers original.
