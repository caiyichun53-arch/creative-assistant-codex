#!/usr/bin/env python3
"""Incrementally recheck topics from topic_state and research_patch."""

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


def evidence_count(patch: dict[str, Any]) -> int:
    total = 0
    for item in patch.get("research_patch", {}).get("evidence_by_question", []) or []:
        total += len(item.get("evidence", []) or [])
    return total


def snippets(patch: dict[str, Any], limit: int = 5) -> list[str]:
    out: list[str] = []
    for item in patch.get("research_patch", {}).get("evidence_by_question", []) or []:
        for evidence in item.get("evidence", []) or []:
            for snippet in evidence.get("snippets", []) or []:
                text = str(snippet).strip()
                if text and text not in out:
                    out.append(text[:260])
                if len(out) >= limit:
                    return out
    return out


def recheck(topic_state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    request_id = patch.get("request_id")
    count = evidence_count(patch)
    old_topics = topic_state.get("topics", []) or []
    new_topics = []
    results = []
    changed_topic_ids: set[str] = set()

    for req in topic_state.get("open_requests", []) or []:
        if req.get("request_id") == request_id:
            if req.get("topic_ids"):
                changed_topic_ids.update(str(topic_id) for topic_id in req.get("topic_ids", []))
            elif req.get("related_topic_ids"):
                changed_topic_ids.update(str(topic_id) for topic_id in req.get("related_topic_ids", []))
            elif req.get("topic_id"):
                changed_topic_ids.add(str(req.get("topic_id")))
            break

    if not changed_topic_ids and patch.get("related_topic_ids"):
        changed_topic_ids.update(str(topic_id) for topic_id in patch.get("related_topic_ids", []))
    if not changed_topic_ids and patch.get("topic_id"):
        changed_topic_ids.add(str(patch.get("topic_id")))

    for topic in old_topics:
        updated = dict(topic)
        if str(topic.get("topic_id")) in changed_topic_ids:
            if count >= 3:
                action = "confirm"
                updated["status"] = "candidate" if topic.get("status") == "needs_research" else topic.get("status", "candidate")
                updated["evidence_used"] = list(topic.get("evidence_used", [])) + snippets(patch, 5)
                updated["missing_evidence"] = [
                    x for x in topic.get("missing_evidence", []) if str(x) not in str(patch.get("missing_decision"))
                ][:2]
                updated["score"] = dict(topic.get("score", {}))
                updated["score"]["material_support"] = min(5, int(updated["score"].get("material_support", 3)) + 1)
                reason = "补采返回了足够证据片段，原选题材料支撑增强。"
            elif count > 0:
                action = "downgrade"
                updated["status"] = "needs_research"
                reason = "补采有少量证据，但不足以支撑原判断。"
            else:
                action = "reject"
                updated["status"] = "weak"
                reason = "补采没有返回可用证据，原判断暂不成立。"
            results.append(
                {
                    "topic_id": topic.get("topic_id"),
                    "recheck_action": action,
                    "reason": reason,
                    "evidence_added": snippets(patch, 5),
                    "remaining_gaps": updated.get("missing_evidence", []),
                    "score_change": updated.get("score", {}),
                }
            )
        else:
            results.append(
                {
                    "topic_id": topic.get("topic_id"),
                    "recheck_action": "keep",
                    "reason": "本次补采不针对该选题，保持原状态。",
                    "evidence_added": [],
                    "remaining_gaps": topic.get("missing_evidence", []),
                    "score_change": {},
                }
            )
        new_topics.append(updated)

    old_ranking = topic_state.get("ranking", []) or [t.get("topic_id") for t in old_topics]
    new_ranking = sorted(
        old_ranking,
        key=lambda tid: next((t.get("score", {}).get("material_support", 0) + t.get("score", {}).get("depth", 0) for t in new_topics if t.get("topic_id") == tid), 0),
        reverse=True,
    )
    new_state_id = f"{topic_state.get('state_id', 'topic_state')}_recheck_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    closed = [request_id] if request_id else []
    open_requests = [r for r in topic_state.get("open_requests", []) if r.get("request_id") not in closed]

    return {
        "mode": "incremental_recheck",
        "based_on_state_id": topic_state.get("state_id"),
        "new_state_id": new_state_id,
        "research_patch_used": request_id,
        "recheck_results": results,
        "topic_state": {
            "state_id": new_state_id,
            "based_on_state_id": topic_state.get("state_id"),
            "task_object": topic_state.get("task_object"),
            "domain": topic_state.get("domain"),
            "version": int(topic_state.get("version") or 1) + 1,
            "topics": new_topics,
            "ranking": new_ranking,
            "closed_requests": closed,
            "open_requests": open_requests,
        },
        "next_allowed_steps": [
            "等待用户确认复判结果",
            "确认后可选择某个选题进入 outline-planner",
        ],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic-state", required=True, type=Path)
    parser.add_argument("--research-patch", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    state_wrapper = load_json(args.topic_state)
    topic_state = state_wrapper.get("topic_state", state_wrapper)
    write_json(args.out, recheck(topic_state, load_json(args.research_patch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
