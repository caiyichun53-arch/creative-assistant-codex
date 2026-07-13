"""Mechanical checks for the operational-infrastructure risk class that
the current design baseline cannot see,
because it's not a business rule -- it's things like hardcoded machine paths,
credential handling, and what's actually gitignored. The 2026-07-09 external
audit found a real instance of this: config/reverse_engine's ffmpeg path and
local ASR python interpreter were hardcoded absolute paths, duplicated in two
files, invisible to every business-rule gate because they aren't business
rules. This script generalizes that specific fix into a permanent check so
the same class of bug (a new hardcoded machine path landing in tracked
source) gets caught mechanically instead of waiting for someone to notice.

What this checks (mechanically, every run):
  1. no new hardcoded absolute machine paths (C:/..., I:/..., /Users/...,
     /home/...) in tracked non-test source -- these belong in
     config/settings.yaml, not in code.
  2. .gitignore actually excludes the sensitive/machine-local paths this
     project depends on staying local (.env, the real config/settings.yaml,
     *.sqlite3, vendor/, logs/, data/).
  3. no committed file other than the explicit .example templates matches
     a credentials-file naming pattern (.env, .env.*).

What this does NOT check (needs a human, documented in TECHNICAL_MANUAL.md
instead of faked here): whether the real Windows Scheduled Tasks are
healthy, whether a recent production-database backup exists, whether vendor/
dependency versions are pinned/current. Automating those needs either a
live Windows environment (CI doesn't have one) or a policy decision (how
fresh must a backup be) this script shouldn't invent unilaterally.

Usage:
    python -m scripts.validation.ops_infra_checklist
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

HARDCODED_ABSOLUTE_PATH_PATTERN = re.compile(
    r"[\"']([A-Za-z]:[/\\][^\"']{3,}|/(?:Users|home)/[^\"']{3,})[\"']"
)

# Extend this only for a genuinely unavoidable, reviewed exception -- the
# fix for a real hit is almost always "move it to config/settings.yaml",
# matching the ffmpeg_path/local_asr_python precedent, not allow-listing it.
ALLOWED_HARDCODED_PATH_FILES: frozenset[str] = frozenset()

REQUIRED_GITIGNORE_PATTERNS = (
    ".env",
    "config/settings.yaml",
    "*.sqlite3",
    "vendor/",
    "logs/",
    "data/",
)


def git_ls_files() -> list[str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]


def scan_hardcoded_paths(files: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for path in files:
        if not path.endswith(".py") or path.startswith("tests/") or path in ALLOWED_HARDCODED_PATH_FILES:
            continue
        full = ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matches = sorted({m.group(1) for m in HARDCODED_ABSOLUTE_PATH_PATTERN.finditer(text)})
        if matches:
            hits.append({"path": path, "hardcoded_paths": matches})
    return hits


def check_no_hardcoded_paths() -> dict[str, Any]:
    hits = scan_hardcoded_paths(git_ls_files())
    return {
        "name": "no_hardcoded_absolute_machine_paths",
        "passed": not hits,
        "detail": hits if hits else "clean",
    }


def check_gitignore_covers_sensitive_paths() -> dict[str, Any]:
    missing = []
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", pattern.rstrip("/") + ("/probe" if pattern.endswith("/") else "")],
            cwd=ROOT,
        )
        if completed.returncode != 0:
            missing.append(pattern)
    return {
        "name": "gitignore_covers_sensitive_paths",
        "passed": not missing,
        "detail": missing if missing else "all required patterns are ignored",
    }


def check_no_committed_real_env_files() -> dict[str, Any]:
    files = git_ls_files()
    suspicious = [
        f for f in files
        if (f == ".env" or f.startswith(".env.")) and not f.endswith(".example")
    ]
    return {
        "name": "no_committed_real_env_files",
        "passed": not suspicious,
        "detail": suspicious if suspicious else "only .example templates (or nothing) committed",
    }


def run_checklist() -> dict[str, Any]:
    checks = [
        check_no_hardcoded_paths(),
        check_gitignore_covers_sensitive_paths(),
        check_no_committed_real_env_files(),
    ]
    overall_passed = all(check["passed"] for check in checks)
    return {"status": "PASS" if overall_passed else "FAIL", "checks": checks}


def main() -> int:
    result = run_checklist()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
