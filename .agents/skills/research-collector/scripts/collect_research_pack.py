#!/usr/bin/env python3
"""Execute a collection_plan and write a raw research_pack.

This script runs fixed collection tasks from plan_research_collection.py using
the same web evidence collector as targeted supplements. It produces raw
evidence grouped by collection task. Agent synthesis may summarize the evidence,
but must not bypass validation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from collect_targeted_research import collect as collect_targeted  # noqa: E402


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def task_to_request(plan: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": task["task_id"],
        "task_object": plan["task_object"],
        "topic_id": None,
        "triggered_by": "research-collector.initial_pack",
        "missing_decision": task.get("purpose"),
        "target_questions": task.get("target_questions", []),
        "source_types": task.get("source_priority", []),
        "search_queries": task.get("search_queries", []),
        "seed_urls": task.get("seed_urls", []),
        "collection_limit": task.get("collection_limit", {}),
        "expected_output": [
            "evidence_by_question",
            "sources",
            "still_missing",
        ],
    }


def collector_args(defaults: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        max_results=defaults.max_results,
        max_pages=defaults.max_pages,
        max_chars=defaults.max_chars,
        timeout=defaults.timeout,
        delay=defaults.delay,
        evidence_per_question=defaults.evidence_per_question,
        preview_chars=defaults.preview_chars,
        search_backend=defaults.search_backend,
    )


def source_bucket(layer: str) -> str:
    if "fact" in layer:
        return "fact_sources"
    if "viewpoint" in layer:
        return "viewpoint_sources"
    if "reception" in layer:
        return "reception_sources"
    return "other_sources"


def collect_pack(plan: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if plan.get("domain_conflict"):
        raise SystemExit("collection_plan has unresolved domain_conflict; regenerate the plan after user resolves the domain.")
    approval = plan.get("approval") or {}
    review_gate = plan.get("review_gate") or {}
    budget_review = plan.get("budget_review") or {}
    approved = approval.get("status") in {"approved", "auto_approved"}
    can_run = bool(review_gate.get("can_run_without_human_approval"))
    human_ack = bool(approval.get("human_review_acknowledged"))
    if not (approved or can_run or args.allow_unapproved):
        raise SystemExit(
            "collection_plan is not approved. Review the plan first, then run "
            "approve_collection_plan.py, or pass --allow-unapproved only for internal tests."
        )
    if approved and not can_run and not human_ack:
        raise SystemExit("collection_plan approval is missing human_review_acknowledged=true; do not collect without explicit user approval.")
    if budget_review.get("requires_user_confirmation") and budget_review.get("status") != "approved":
        raise SystemExit("collection budget has not been approved by user; stop at collection_plan review.")

    evidence_by_task: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    source_seen: set[str] = set()
    source_layers = {
        "fact_sources": [],
        "viewpoint_sources": [],
        "reception_sources": [],
        "other_sources": [],
    }
    gaps: list[dict[str, Any]] = []

    for task in plan.get("collection_tasks", []):
        request = task_to_request(plan, task)
        patch = collect_targeted(request, collector_args(args))
        research_patch = patch.get("research_patch", {})
        task_sources = research_patch.get("sources", [])
        ok_sources = [s for s in task_sources if s.get("ok")]
        if not ok_sources:
            gaps.append(
                {
                    "task_id": task.get("task_id"),
                    "gap": "no_ok_sources_collected",
                    "purpose": task.get("purpose"),
                }
            )
        for source in task_sources:
            url = source.get("url")
            if url and url not in source_seen:
                source_seen.add(url)
                sources.append(source)
            bucket = source_bucket(str(task.get("layer") or ""))
            if url and url not in {x.get("url") for x in source_layers[bucket]}:
                source_layers[bucket].append(
                    {
                        "title": source.get("title"),
                        "url": url,
                        "ok": source.get("ok"),
                    }
                )

        evidence_by_task.append(
            {
                "task_id": task.get("task_id"),
                "layer": task.get("layer"),
                "purpose": task.get("purpose"),
                "target_questions": task.get("target_questions", []),
                "evidence_by_question": research_patch.get("evidence_by_question", []),
                "sources_count": len(task_sources),
                "ok_sources_count": len(ok_sources),
                "domain_collectors": task.get("domain_collectors", []),
                "collector_note": research_patch.get("collector_note"),
            }
        )

    return {
        "task_object": plan.get("task_object"),
        "object_type": plan.get("object_type"),
        "mode": "initial_pack",
        "domain": plan.get("domain"),
        "research_depth": plan.get("research_depth"),
        "collection_plan_id": plan.get("plan_id"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "foundation_status": plan.get("foundation_status", {}),
        "approval": approval,
        "budget_review": budget_review,
        "review_gate": review_gate,
        "summary": "",
        "timeline_or_evolution": [],
        "key_facts": [],
        "representative_materials": [],
        "main_viewpoints": [],
        "common_misreadings": [],
        "usable_materials": [],
        "background_only_materials": [],
        "high_risk_materials": [],
        "audience_or_public_reception": [],
        "evidence_by_task": evidence_by_task,
        "sources": sources,
        "source_layers": source_layers,
        "research_gaps": gaps,
        "domain_notes": [
            "Raw research_pack collected by script. Agent synthesis may fill summary fields from evidence, but must preserve evidence_by_task and sources.",
        ],
        "next_allowed_steps": [
            "run validate_research_output.py --kind research_pack",
            "after validation, agent may synthesize summary fields",
            "do not generate topics until research_pack passes compliance review",
        ],
    }


def write_markdown(path: Path, pack: dict[str, Any]) -> None:
    lines = [
        "# Research Pack",
        "",
        f"- task_object: {pack.get('task_object')}",
        f"- domain: {pack.get('domain')}",
        f"- collection_plan_id: {pack.get('collection_plan_id')}",
        "",
        "## Evidence By Task",
        "",
    ]
    for task in pack.get("evidence_by_task", []):
        lines.append(f"### {task.get('task_id')} / {task.get('layer')}")
        lines.append(f"- purpose: {task.get('purpose')}")
        lines.append(f"- ok_sources: {task.get('ok_sources_count')}")
        for item in task.get("evidence_by_question", []):
            lines.append(f"- question: {item.get('question')}")
            for evidence in item.get("evidence", [])[:3]:
                lines.append(f"  - {evidence.get('source_title')}  ")
                lines.append(f"    {evidence.get('url')}")
                for snippet in evidence.get("snippets", [])[:2]:
                    lines.append(f"    - {snippet}")
        lines.append("")
    lines.append("## Research Gaps")
    for gap in pack.get("research_gaps", []):
        lines.append(f"- {gap.get('task_id')}: {gap.get('gap')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--max-results", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=50000)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--evidence-per-question", type=int, default=5)
    parser.add_argument("--preview-chars", type=int, default=1200)
    parser.add_argument("--search-backend", choices=["auto", "ddgs", "bing", "none"], default="auto")
    parser.add_argument("--allow-unapproved", action="store_true", help="internal tests only; bypasses human approval gate")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    pack = collect_pack(load_json(args.plan), args)
    write_json(args.out, pack)
    if args.out_md:
        write_markdown(args.out_md, pack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
