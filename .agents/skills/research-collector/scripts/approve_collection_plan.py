#!/usr/bin/env python3
"""Approve a reviewed collection_plan.

This script only records approval. If research_depth or tasks need changes,
edit the original request and regenerate the plan instead of patching the plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def approve(plan: dict[str, Any], reviewer: str, note: str, human_review_ack: bool) -> dict[str, Any]:
    if not plan.get("plan_id"):
        raise SystemExit("input does not look like a collection_plan: missing plan_id")
    if not plan.get("collection_tasks"):
        raise SystemExit("cannot approve collection_plan without collection_tasks")
    if plan.get("domain_conflict"):
        raise SystemExit("cannot approve collection_plan with unresolved domain_conflict")
    if not human_review_ack:
        raise SystemExit("approval requires --human-review-ack after explicit user review")
    plan["approval"] = {
        "status": "approved",
        "approved_by": reviewer,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "approval_note": note,
        "human_review_acknowledged": True,
    }
    plan.setdefault("budget_review", {})
    plan["budget_review"]["status"] = "approved"
    plan["budget_review"]["approved_by"] = reviewer
    plan["budget_review"]["approved_at"] = plan["approval"]["approved_at"]
    plan.setdefault("review_gate", {})
    plan["review_gate"]["requires_human_review"] = False
    plan["review_gate"]["review_completed"] = True
    return plan


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--reviewer", default="user")
    parser.add_argument("--note", default="approved after user review")
    parser.add_argument("--human-review-ack", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    write_json(args.out, approve(load_json(args.plan), args.reviewer, args.note, args.human_review_ack))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
