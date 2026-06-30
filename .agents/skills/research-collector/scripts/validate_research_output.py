#!/usr/bin/env python3
"""Validate research-collector outputs against fixed contracts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_KEYS = {"topics", "outline", "draft", "article", "script"}
LOW_QUALITY_SNIPPET_MARKERS = [
    "No Results for",
    "Reach Us Now",
    "federally insured",
    "Digital Asset Center",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add(results: list[dict[str, Any]], check_id: str, status: str, message: str) -> None:
    results.append({"check_id": check_id, "status": status, "message": message})


def has_forbidden_keys(data: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            current = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                found.append(current)
            found.extend(has_forbidden_keys(value, current))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(has_forbidden_keys(item, f"{path}[{index}]"))
    return found


def low_quality_snippet_paths(data: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            current = f"{path}.{key}"
            if key in {"snippet", "snippets", "text_preview"}:
                values = value if isinstance(value, list) else [value]
                for item in values:
                    text = str(item or "")
                    if any(marker in text for marker in LOW_QUALITY_SNIPPET_MARKERS):
                        found.append(current)
                        break
            found.extend(low_quality_snippet_paths(value, current))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(low_quality_snippet_paths(item, f"{path}[{index}]"))
    return found


def evidence_item_count(evidence_by_question: list[dict[str, Any]]) -> int:
    total = 0
    for item in evidence_by_question or []:
        total += len(item.get("evidence") or [])
    return total


def validate_collection_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    required = [
        "plan_id",
        "mode",
        "task_object",
        "object_type",
        "domain",
        "research_depth_decision",
        "review_gate",
        "approval",
        "budget_review",
        "collection_tasks",
    ]
    for key in required:
        add(r, f"plan.required.{key}", "pass" if data.get(key) else "fail", f"{key} must exist")
    tasks = data.get("collection_tasks") or []
    add(r, "plan.tasks.non_empty", "pass" if tasks else "fail", "collection_tasks must not be empty")
    object_type = data.get("object_type")
    task_ids = [str(task.get("task_id") or "") for task in tasks]
    person_task_ids = [task_id for task_id in task_ids if task_id.startswith("music_person_")]
    add(
        r,
        "plan.domain.no_conflict",
        "fail" if data.get("domain_conflict") else "pass",
        f"domain_conflict must be resolved before collection: {data.get('domain_conflict')}",
    )
    add(
        r,
        "plan.semantic.no_person_tasks_for_non_person",
        "fail" if object_type not in {"person", "person_or_entity"} and person_task_ids else "pass",
        f"non-person object_type must not use music_person tasks: {person_task_ids}",
    )
    if object_type == "topic_direction":
        has_decision_task = any("decision" in task_id or str(task.get("layer")) == "decision_support" for task_id, task in zip(task_ids, tasks))
        add(
            r,
            "plan.semantic.topic_direction_has_decision_support",
            "pass" if has_decision_task else "fail",
            "topic_direction must include decision_support collection tasks",
        )
    for task in tasks:
        tid = task.get("task_id") or "<missing>"
        for key in ["layer", "purpose", "target_questions", "search_queries", "collection_limit"]:
            status = "pass" if task.get(key) else "fail"
            add(r, f"plan.task.{tid}.{key}", status, f"{tid} must include {key}")
    strategy = data.get("collection_strategy") or {}
    has_layer = any(strategy.get(k) for k in ["fact_layer", "viewpoint_layer", "reception_layer"])
    add(r, "plan.strategy.layers", "pass" if has_layer else "fail", "plan should include at least one collection layer")
    depth_decision = data.get("research_depth_decision") or {}
    add(
        r,
        "plan.depth.options",
        "pass" if depth_decision.get("options") and depth_decision.get("selected") else "fail",
        "plan must expose research depth options and selected depth",
    )
    approval = data.get("approval") or {}
    approval_status = approval.get("status")
    add(
        r,
        "plan.approval.status",
        "pass" if approval_status in {"pending_user_review", "approved", "auto_approved"} else "fail",
        "approval.status must be pending_user_review, approved, or auto_approved",
    )
    if approval_status == "pending_user_review":
        add(r, "plan.approval.waiting", "warn", "collection_plan is structurally valid but must be reviewed before collection")
    budget_review = data.get("budget_review") or {}
    for key in ["estimated_tasks", "estimated_search_queries", "estimated_max_pages", "cost_level", "status"]:
        add(
            r,
            f"plan.budget.{key}",
            "pass" if key in budget_review else "fail",
            f"budget_review.{key} must exist",
        )
    add(
        r,
        "plan.budget.requires_user_confirmation",
        "pass" if budget_review.get("requires_user_confirmation") is True else "fail",
        "budget_review must require user confirmation before collection",
    )
    forbidden = has_forbidden_keys(data)
    add(r, "plan.no_generation_outputs", "pass" if not forbidden else "fail", f"forbidden keys: {forbidden}")
    return r


def validate_research_pack(data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    required = [
        "task_object",
        "object_type",
        "mode",
        "domain",
        "collection_plan_id",
        "evidence_by_task",
        "sources",
        "source_layers",
        "research_gaps",
        "approval",
        "review_gate",
        "next_allowed_steps",
    ]
    for key in required:
        present = key in data and data.get(key) not in (None, "")
        add(r, f"pack.required.{key}", "pass" if present else "fail", f"{key} must exist")
    evidence = data.get("evidence_by_task") or []
    gap_task_ids = {str(gap.get("task_id")) for gap in data.get("research_gaps", []) or [] if gap.get("task_id")}
    add(r, "pack.evidence.non_empty", "pass" if evidence else "fail", "evidence_by_task must not be empty")
    for task in evidence:
        tid = task.get("task_id") or "<missing>"
        ok_count = int(task.get("ok_sources_count") or 0)
        item_count = evidence_item_count(task.get("evidence_by_question") or [])
        add(
            r,
            f"pack.task.{tid}.ok_sources",
            "pass" if ok_count > 0 else "warn",
            f"{tid} should have at least one ok source or be listed as a gap",
        )
        add(
            r,
            f"pack.task.{tid}.evidence_by_question",
            "pass" if task.get("evidence_by_question") else "fail",
            f"{tid} must preserve evidence_by_question",
        )
        add(
            r,
            f"pack.task.{tid}.evidence_items",
            "pass" if item_count > 0 or (ok_count == 0 and str(tid) in gap_task_ids) else "fail",
            f"{tid} must include evidence items when ok sources exist, otherwise be listed as a gap",
        )
    layers = data.get("source_layers") or {}
    add(
        r,
        "pack.source_layers.separated",
        "pass" if any(layers.get(k) for k in ["fact_sources", "viewpoint_sources", "reception_sources"]) else "fail",
        "sources must be separated by layer",
    )
    approval = data.get("approval") or {}
    add(
        r,
        "pack.approval.status",
        "pass" if approval.get("status") in {"approved", "auto_approved"} else "fail",
        "research_pack must come from an approved or auto-approved collection_plan",
    )
    forbidden = has_forbidden_keys(data)
    add(r, "pack.no_generation_outputs", "pass" if not forbidden else "fail", f"forbidden keys: {forbidden}")
    low_quality = low_quality_snippet_paths(data)
    add(r, "pack.no_low_quality_snippets", "pass" if not low_quality else "fail", f"low quality snippet paths: {low_quality[:10]}")
    return r


def validate_research_patch(data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    required = ["task_object", "mode", "missing_decision", "target_questions", "research_patch"]
    for key in required:
        add(r, f"patch.required.{key}", "pass" if data.get(key) else "fail", f"{key} must exist")
    patch = data.get("research_patch") or {}
    for key in ["evidence_by_question", "sources"]:
        add(r, f"patch.required.research_patch.{key}", "pass" if patch.get(key) else "fail", f"research_patch.{key} must exist")
    add(
        r,
        "patch.evidence_items",
        "pass" if evidence_item_count(patch.get("evidence_by_question") or []) > 0 else "fail",
        "research_patch must include at least one actual evidence item",
    )
    forbidden = has_forbidden_keys(data)
    add(r, "patch.no_generation_outputs", "pass" if not forbidden else "fail", f"forbidden keys: {forbidden}")
    low_quality = low_quality_snippet_paths(data)
    add(r, "patch.no_low_quality_snippets", "pass" if not low_quality else "fail", f"low quality snippet paths: {low_quality[:10]}")
    return r


def summarize(kind: str, results: list[dict[str, Any]], source: Path) -> dict[str, Any]:
    failed = [x for x in results if x["status"] == "fail"]
    warnings = [x for x in results if x["status"] == "warn"]
    return {
        "kind": kind,
        "source": str(source),
        "passed": not failed,
        "counts": {
            "pass": sum(1 for x in results if x["status"] == "pass"),
            "warn": len(warnings),
            "fail": len(failed),
        },
        "results": results,
        "next_allowed": "continue" if not failed else "fix_output_before_continue",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=["collection_plan", "research_pack", "research_patch"])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data = load_json(args.input)
    if args.kind == "collection_plan":
        results = validate_collection_plan(data)
    elif args.kind == "research_pack":
        results = validate_research_pack(data)
    else:
        results = validate_research_patch(data)
    report = summarize(args.kind, results, args.input)
    if args.out:
        write_json(args.out, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
