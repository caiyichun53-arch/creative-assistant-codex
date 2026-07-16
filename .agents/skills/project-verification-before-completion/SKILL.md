---
name: project-verification-before-completion
description: Use immediately before claiming that code, configuration, installation, integration, a workflow, or a Stage is complete, fixed, working, or passing. Run the current task's authoritative real acceptance command, read the complete result and exit code, and provide fresh evidence. Static checks, mocks, partial tests, old output, and agent reports are not sufficient.
---

# Project Verification Before Completion

## Goal

Prevent completion claims unless this turn has fresh evidence from the authoritative command for the current task.

## Verification Gate

Before saying code, config, dependencies, installation, integration, workflow, or a Stage is complete, fixed, working, or passing:

1. Identify the command that has authority for this claim.
2. Run the complete command fresh in this turn.
3. Read the complete output, exit code, failure count, and key result lines.
4. Decide whether the output proves the exact claim.
5. If the command fails or proves less than the claim, report the actual status and stop.
6. If any controlled code, config, dependency, or hook file changes after verification, the evidence is stale and must be rerun.

## Project Authority Rules

- For a business Stage, the final authority is the existing real workflow acceptance entry selected by the current project control source. Unit tests and static checks can help during development but cannot replace it.
- Do not add framework validation, Mock acceptance, fixture acceptance, integration review, or a second real-test loop just to use this skill.
- If the task only changes local Skills, Hooks, or governance files, run the task-specific mechanism self-test instead of an unrelated production chain.
- If no unique authoritative command exists, report `验收入口缺失`; do not wrap a weak unit test and call it final acceptance.
- If the authoritative command is blocked by missing user authorization, provider credentials, formal data-write permission, external-call permission, or unclear cost, report that block and do not run a substitute.
- Do not trust another agent's success report. Verify the files, command, and output directly.

## Not Sufficient

- Old test output.
- Lint when build or workflow execution is the claim.
- A passing unit test when real workflow acceptance is required.
- Mock, fixture-only, fake provider, or static file evidence.
- "The code looks correct."
- `accept` output from before the last controlled file change.

## Required Report

When using this skill, include:

```text
权威验收命令：
执行时间：
退出码：
关键输出：
工作区指纹是否与证据匹配：
结论：
```

## Failure Handling

If the command fails, state the failing command, exit code, and first actionable failure. If verification cannot run, state the exact missing authorization or missing authoritative entry. Do not claim completion.
