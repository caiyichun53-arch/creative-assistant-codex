---
name: project-systematic-debugging
description: Use before changing code or configuration when a test, installation, integration, runtime workflow, path, provider, database, dependency, or expected result fails or behaves unexpectedly. First reproduce and locate the root cause; do not guess, patch symptoms, add fallback paths, or expand scope.
---

# Project Systematic Debugging

## Goal

Find the first real cause of a project failure before changing code, configuration, dependencies, paths, providers, databases, hooks, or workflow controls.

## Required Result Before Editing

Before any fix, produce this short working note in the conversation. Do not save it as a long-lived report.

```text
故障现象：
复现命令：
实际执行入口：
实际配置来源：
首次异常位置：
根因证据：
单一根因假设：
准备修改的文件：
明确不修改的范围：
```

## Method

1. Reproduce the failure first. Record the exact command, cwd, environment boundary, exit code, and whether it fails consistently.
2. Read the complete error, stack trace, path, command output, and return code. Do not summarize before locating the failing line or component.
3. Check recent changes with Git status, diff, and relevant recent commits. Do not revert unrelated user changes.
4. Confirm the actual runtime entrypoint. Do not modify a similar file until proving it is the code path being called.
5. For multi-component paths, inspect each boundary: input, output, config, environment variables, file paths, database target, provider route, and persisted state.
6. Trace data flow backward from the first bad value or first failing side effect until the source is found.
7. Compare with a known working implementation in this repository when one exists.
8. State one concrete root-cause hypothesis. Test it with the smallest experiment that can prove or disprove it.
9. Modify only the file or config that produces the root cause. Avoid bundled refactors and adjacent cleanup.
10. Verify the original symptom through the relevant command. If the fix fails, replace the hypothesis; do not pile on patches.
11. After three failed fix attempts, stop and re-check the architectural assumption instead of trying another patch.

## Project Constraints

- Use `docs/EFFECTIVE_DESIGN_BASELINE.md` as the only business design authority.
- Do not use old control documents, ROADMAP, BUILD_PLAN, old Goal files, archived audits, or chat memory to decide current design.
- Confirm Windows and WSL path equivalence before changing path-sensitive logic.
- Do not invent a new installation directory, backup provider, backup model, backup database, fallback, silent bypass, or temporary side entry.
- Do not change formal acceptance criteria to fit the current implementation.
- Do not treat Mock, fixture-only tests, unit tests, or "code looks correct" as proof that a real workflow is fixed.
- Do not install failing components into the project root or user directory unless the active task explicitly authorizes that location.
- Do not add a parallel Stage, gate, ROADMAP, BUILD_PLAN, or GOAL system while debugging.

## Supporting References

Read these only when the issue needs the technique:

- `root-cause-tracing.md`: backward tracing through call stacks and data flow.
- `defense-in-depth.md`: validation placement after root cause is known.
- `condition-based-waiting.md`: replacing arbitrary sleeps with condition checks.
- `condition-based-waiting-example.ts`: example implementation for condition waits.
- `find-polluter.sh`: helper for isolating state pollution when applicable.

## Not Included From Upstream

This project skill does not require the upstream Superpowers TDD, brainstorming, planning, worktree, subagent, code-review, branch-finishing, or Git commit workflows. Create a minimal reproduction or regression check when useful, but do not create a separate TDD process unless the current task already calls for one.

## Failure Handling

If the failure cannot be reproduced, stop and report what was checked and what evidence is missing. If the actual entrypoint, config source, provider, database, or acceptance target cannot be identified, do not edit around the uncertainty.
