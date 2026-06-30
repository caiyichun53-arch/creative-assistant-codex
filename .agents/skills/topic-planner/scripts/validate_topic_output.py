#!/usr/bin/env python3
"""Validate topic-planner outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_KEYS = {"outline", "draft", "article", "script"}


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


def validate_initial_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    required = ["mode", "task_object", "object_type", "domain", "topics", "topic_state", "next_allowed_steps"]
    for key in required:
        add(r, f"topic.required.{key}", "pass" if data.get(key) else "fail", f"{key} must exist")
    topics = data.get("topics") or []
    state = data.get("topic_state") or {}
    object_type = data.get("object_type")
    add(r, "topic.mode.initial", "pass" if data.get("mode") == "initial_plan" else "fail", "mode must be initial_plan")
    add(
        r,
        "topic.planner_mode.evidence_scaffold",
        "pass" if data.get("planner_mode") == "evidence_scaffold" else "fail",
        "planner_mode must be evidence_scaffold",
    )
    add(
        r,
        "topic.agent_judgment_required",
        "pass" if data.get("agent_judgment_required") is True else "fail",
        "initial topic planning must require agent/user judgment after deterministic scaffolding",
    )
    add(
        r,
        "topic.human_review_required",
        "pass" if data.get("human_review_required") is True else "fail",
        "initial topic planning must keep a human review stop point",
    )
    add(
        r,
        "topic.material_profile",
        "pass" if data.get("topic_material_profile") else "fail",
        "topic_material_profile must preserve evidence inventory for later judgment",
    )
    add(
        r,
        "topic.shared_request_policy",
        "pass" if (data.get("targeted_research_request_policy") or {}).get("mode") == "shared_by_missing_decision" else "fail",
        "targeted research requests must be shared by missing_decision",
    )
    topic_ids: set[str] = set()
    for topic in topics:
        tid = topic.get("topic_id")
        add(r, f"topic.{tid}.topic_id", "pass" if tid and tid not in topic_ids else "fail", "topic_id must be stable and unique")
        if tid:
            topic_ids.add(tid)
        for key in [
            "topic_title",
            "core_question",
            "status",
            "evidence_used",
            "missing_evidence",
            "risk",
            "score",
            "source_task_ids",
            "topic_development_brief",
            "why_it_may_work",
            "material_support",
            "targeted_research_request_ids",
        ]:
            add(r, f"topic.{tid}.{key}", "pass" if key in topic else "fail", f"{tid} must include {key}")
        brief = topic.get("topic_development_brief") or {}
        axis = str(brief.get("candidate_axis") or "")
        invalid_topic_direction_axes = ["能否用具体作品打开对象", "大众记忆与核心价值是否错位"]
        add(
            r,
            f"topic.{tid}.semantic_axis_matches_object_type",
            "fail" if object_type == "topic_direction" and axis in invalid_topic_direction_axes else "pass",
            f"{tid} axis must match object_type={object_type}: {axis}",
        )
        add(
            r,
            f"topic.{tid}.not_final_title",
            "pass" if brief.get("not_final_title") is True else "fail",
            f"{tid} must mark candidate title as non-final",
        )
        add(
            r,
            f"topic.{tid}.agent_judgment_items",
            "pass" if brief.get("agent_judgment_required") else "fail",
            f"{tid} must list judgment checks for the agent/user",
        )
    state_ids = {t.get("topic_id") for t in state.get("topics", [])}
    add(r, "topic_state.ids_match", "pass" if topic_ids and topic_ids.issubset(state_ids) else "fail", "topic_state must preserve all topic IDs")
    for req in data.get("targeted_research_requests", []) or []:
        rid = req.get("request_id")
        for key in [
            "request_id",
            "topic_id",
            "related_topic_ids",
            "triggered_by",
            "missing_decision",
            "target_questions",
            "source_types",
            "search_queries",
            "collection_limit",
            "expected_output",
        ]:
            add(r, f"targeted_request.{rid}.{key}", "pass" if req.get(key) else "fail", f"{rid} must include {key}")
        queries_text = " ".join(str(q) for q in req.get("search_queries", []) or [])
        leaked_music_work_terms = ["代表作", "热门歌曲", "专辑"]
        add(
            r,
            f"targeted_request.{rid}.semantic_queries_match_object_type",
            "fail" if object_type == "topic_direction" and any(term in queries_text for term in leaked_music_work_terms) else "pass",
            f"{rid} search_queries must not leak music work terms for topic_direction: {queries_text}",
        )
    open_requests = (state.get("open_requests") or [])
    for req in open_requests:
        rid = req.get("request_id")
        add(
            r,
            f"topic_state.open_request.{rid}.topic_ids",
            "pass" if req.get("topic_ids") else "fail",
            f"{rid} must keep all topic_ids affected by a shared supplement",
        )
    forbidden = has_forbidden_keys(data)
    add(r, "topic.no_generation_outputs", "pass" if not forbidden else "fail", f"forbidden keys: {forbidden}")
    return r


def validate_recheck(data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    required = ["mode", "based_on_state_id", "new_state_id", "recheck_results", "topic_state", "next_allowed_steps"]
    for key in required:
        add(r, f"recheck.required.{key}", "pass" if data.get(key) else "fail", f"{key} must exist")
    add(r, "recheck.mode", "pass" if data.get("mode") == "incremental_recheck" else "fail", "mode must be incremental_recheck")
    for result in data.get("recheck_results", []) or []:
        tid = result.get("topic_id")
        for key in ["topic_id", "recheck_action", "reason"]:
            add(r, f"recheck.{tid}.{key}", "pass" if result.get(key) else "fail", f"{tid} must include {key}")
    forbidden = has_forbidden_keys(data)
    add(r, "recheck.no_generation_outputs", "pass" if not forbidden else "fail", f"forbidden keys: {forbidden}")
    return r


def summarize(kind: str, results: list[dict[str, Any]], source: Path) -> dict[str, Any]:
    failed = [x for x in results if x["status"] == "fail"]
    return {
        "kind": kind,
        "source": str(source),
        "passed": not failed,
        "counts": {
            "pass": sum(1 for x in results if x["status"] == "pass"),
            "warn": sum(1 for x in results if x["status"] == "warn"),
            "fail": len(failed),
        },
        "results": results,
        "next_allowed": "continue" if not failed else "fix_output_before_continue",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=["initial_plan", "incremental_recheck"])
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data = load_json(args.input)
    results = validate_initial_plan(data) if args.kind == "initial_plan" else validate_recheck(data)
    report = summarize(args.kind, results, args.input)
    if args.out:
        write_json(args.out, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
