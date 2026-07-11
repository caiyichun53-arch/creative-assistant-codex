"""One command to run before wrapping up a session (收工) or handing off to
another executor (Codex/Claude Code taking over from each other).

AGENTS.md/CLAUDE.md "执行纪律" already requires all of the following before
anything gets called done/completed/ENGINEERING_READY:
  1. the working tree is committed cleanly (no uncommitted changes left
     behind for a reset/handoff to silently drop)
  2. the full test suite is green
  3. the authoritative readiness gate (scripts/core/staging/
     verify_goal_v062_phase8_readiness.py) reports ENGINEERING_READY
  4. the operational-infrastructure checklist (scripts/validation/
     ops_infra_checklist.py) is clean -- no hardcoded machine paths,
     .gitignore still covers secrets, no real .env file committed
  5. the real production database (scripts/validation/
     production_data_sanity_check.py) has nothing visibly stuck (e.g. a
     hit stuck in reverse_status='pending' for days -- the exact 2026-07-08
     bug this check exists to catch mechanically instead of by luck)
  6. no new business-logic constant in the real-data binding layer
     (scripts/validation/unsourced_constant_check.py) was added without
     saying where its value came from -- catches a number picked with no
     source presented as if it were reasoned, the exact failure mode
     TOP_COMMENTS_PER_HIT=3 reproduced on 2026-07-10 in the same session
     that was fixing this class of problem elsewhere
  7. the 2026-07-11 clean-sweep (scripts/validation/dead_goal_chain_gate.py)
     still holds: no archived Goal01-12 module has been re-imported by a real
     entrypoint, no active code reaches back into archive/, the three model
     positions (dialogue_model/business_model/writing_model) still exist and
     are Mimo with fallback disabled, no engineering_execution route crept
     back in, .env.example has no dead model config, CLAUDE.md/AGENTS.md
     have not regressed to claiming creation runs on Claude/is complete, and
     REQUIREMENT_CODE_TRACEABILITY.yaml has no ContentWorkflow/ContentGuard
     ghost reference
  8. the production-activation control-package gate (scripts/validation/
     production_activation_gate.py, 2026-07-13, 置顶规则总表条目22/23) still
     holds: all five pipeline stages (topic/plan/draft/review/final) are
     wired with a real human-review gate, 文案优化 runs before 审核 (not the
     other way around), no auto-publish path exists, formal research still
     has no video-platform import, the 对标 collection pipeline is intact,
     no score/rank/weight topic-sorting config has crept back in, and every
     pipeline-stage table still carries real traceability fields
Until now these were separate manual steps someone had to remember to run,
in order, every time -- exactly the kind of "remembering" this project's own
incident history (see HANDOFF_STATE.md) shows doesn't hold up under
context-window/quota pressure at the end of a session. This script runs all
of them and prints one PASS/FAIL verdict per check plus a combined status, so
the check is "run one command", not "remember N things".

Usage:
    python -m scripts.validation.preflight_checkpoint_check
    python -m scripts.validation.preflight_checkpoint_check --skip-tests   # faster iteration
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def check_git_clean() -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    dirty_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return {
        "name": "git_working_tree_clean",
        "passed": not dirty_lines,
        "detail": dirty_lines if dirty_lines else "working tree is clean",
    }


def check_tests(*, skip: bool) -> dict[str, Any]:
    if skip:
        return {"name": "test_suite_green", "passed": True, "detail": "skipped (--skip-tests)"}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/core", "tests/validation", "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = "\n".join(completed.stdout.strip().splitlines()[-15:])
    return {"name": "test_suite_green", "passed": completed.returncode == 0, "detail": tail}


def check_readiness_gate() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from scripts.core.staging.verify_goal_v062_phase8_readiness import verify_phase8_readiness

    result = verify_phase8_readiness()
    return {
        "name": "readiness_gate_engineering_ready",
        "passed": result["status"] == "ENGINEERING_READY",
        "detail": result["status"] if result["status"] == "ENGINEERING_READY" else result.get("failures"),
    }


def check_ops_infra() -> dict[str, Any]:
    from scripts.validation.ops_infra_checklist import run_checklist

    result = run_checklist()
    return {
        "name": "ops_infra_checklist_clean",
        "passed": result["status"] == "PASS",
        "detail": "clean" if result["status"] == "PASS" else [c for c in result["checks"] if not c["passed"]],
    }


def check_production_data() -> dict[str, Any]:
    from scripts.validation.production_data_sanity_check import run_checklist

    result = run_checklist()
    return {
        "name": "production_data_sanity",
        "passed": result["status"] in ("PASS", "SKIPPED"),
        "detail": result["status"] if result["status"] != "FAIL" else [c for c in result["checks"] if not c["passed"]],
    }


def check_unsourced_constants() -> dict[str, Any]:
    from scripts.validation.unsourced_constant_check import run_checklist

    result = run_checklist()
    return {
        "name": "no_unsourced_constants",
        "passed": result["status"] == "PASS",
        "detail": "clean" if result["status"] == "PASS" else result["unsourced_constants"],
    }


def check_dead_goal_chain() -> dict[str, Any]:
    from scripts.validation.dead_goal_chain_gate import run_checklist

    result = run_checklist()
    return {
        "name": "dead_goal_chain_clean_sweep",
        "passed": result["status"] == "PASS",
        "detail": "clean" if result["status"] == "PASS" else [c for c in result["checks"] if not c["passed"]],
    }


def check_production_activation() -> dict[str, Any]:
    # 2026-07-13 (置顶规则总表核对后): the control-package Gate the pinned
    # rules doc names (条目22/23, 附表七) -- pipeline-stage completeness,
    # human review gates, no auto-publish, formal-research/对标 boundary,
    # traceability fields. Internally re-runs dead_goal_chain_gate too, so
    # this and check_dead_goal_chain() above overlap on that one piece --
    # harmless (same underlying check, called from two aggregation points),
    # not a re-implementation of its logic.
    from scripts.validation.production_activation_gate import run_production_activation_gate

    result = run_production_activation_gate()
    return {
        "name": "production_activation_gate",
        "passed": result["status"] == "PASS",
        "detail": "clean" if result["status"] == "PASS" else [c for c in result["checks"] if not c["passed"]],
    }


def run_all_checks(*, skip_tests: bool) -> dict[str, Any]:
    checks = [
        check_git_clean(),
        check_tests(skip=skip_tests),
        check_readiness_gate(),
        check_ops_infra(),
        check_production_data(),
        check_unsourced_constants(),
        check_dead_goal_chain(),
        check_production_activation(),
    ]
    overall_passed = all(check["passed"] for check in checks)
    return {"status": "READY_TO_CHECKPOINT" if overall_passed else "NOT_READY", "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-tests", action="store_true", help="skip the full test suite run (faster, for quick iteration only -- do not skip before a real checkpoint)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_all_checks(skip_tests=args.skip_tests)
    overall_passed = result["status"] == "READY_TO_CHECKPOINT"
    checks = result["checks"]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            mark = "PASS" if check["passed"] else "FAIL"
            print(f"[{mark}] {check['name']}")
            if not check["passed"]:
                print(f"       {check['detail']}")
        print(f"\noverall: {result['status']}")

    return 0 if overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
