# Installs a git post-commit hook that runs
# scripts/scheduled/post_commit_self_check.py automatically after every
# commit. Opt-in: this script does nothing until you run it yourself --
# .git/hooks/ is local/untracked, so no one gets this hook just by cloning
# or pulling the repo.
#
# What it does after install: every `git commit` in this repo will, right
# after the commit completes, run a few seconds of mechanical self-checks
# (readiness gate, ops-infra checklist, production-data sanity check) and
# print a warning to the terminal if anything looks wrong. It never blocks
# or undoes the commit -- it's a heads-up, not a gate.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\scheduled\install_post_commit_hook.ps1
# To remove:
#   Remove-Item .git\hooks\post-commit

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$HookPath = Join-Path $RepoRoot ".git\hooks\post-commit"
$PythonExe = "C:\Users\15891\anaconda3\python.exe"

$HookContent = @"
#!/bin/sh
"$PythonExe" -m scripts.scheduled.post_commit_self_check
exit 0
"@

Set-Content -Path $HookPath -Value $HookContent -Encoding ASCII -NoNewline
Write-Output "Installed post-commit hook at $HookPath"
Write-Output "Test it now with: git commit --allow-empty -m 'test hook'"
