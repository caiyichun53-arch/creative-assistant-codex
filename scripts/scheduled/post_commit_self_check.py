"""Lightweight, non-conversational self-check meant to be wired as a git
post-commit hook -- runs automatically after every commit, without anyone
having to remember to ask "is everything still okay" in a conversation.

Addresses the audit finding that this project's only real QA mechanism was
the user manually asking pointed questions with real data. This does not
replace that judgment, but it catches the mechanical class of drift (a
threshold silently diverging from the catalog, a stuck production row, a
newly hardcoded machine path) without waiting for the next conversation to
happen to touch that exact area.

Deliberately fast (a few seconds), NOT the full pytest suite -- a hook that
makes every commit take 50+ seconds would get disabled out of friction, which
defeats the point. Runs:
  - the authoritative readiness gate
  - the ops-infrastructure checklist
  - the production-data sanity check
Does not touch git working tree state and never blocks the commit itself
(it runs post-commit, after the commit already exists) -- it only prints a
warning to stderr so a real problem is visible without silently succeeding.

Installing (opt-in, not automatic -- installing a hook is a real change to
your local git workflow, so this doesn't do it for you):
    See scripts/scheduled/install_post_commit_hook.ps1
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from scripts.core.staging.verify_goal_v062_phase8_readiness import verify_phase8_readiness
    from scripts.validation.ops_infra_checklist import run_checklist as run_ops_checklist
    from scripts.validation.production_data_sanity_check import run_checklist as run_data_checklist

    readiness = verify_phase8_readiness()
    ops = run_ops_checklist()
    data = run_data_checklist()

    problems = []
    if readiness["status"] != "ENGINEERING_READY":
        problems.append(f"readiness gate: {readiness['status']} (failures: {readiness.get('failures')})")
    if ops["status"] != "PASS":
        problems.append(f"ops infra checklist: {[c for c in ops['checks'] if not c['passed']]}")
    if data["status"] == "FAIL":
        problems.append(f"production data sanity: {[c for c in data['checks'] if not c['passed']]}")

    if problems:
        print("post-commit self-check found issues (commit already happened -- this is a heads-up, not a block):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("post-commit self-check: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
