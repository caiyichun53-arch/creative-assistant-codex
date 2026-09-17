"""Stateless hotspot report boundary: no candidates, model calls or business writes."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import yaml

from scripts.core.external_adapters.windows_process import hidden_process_kwargs
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract, prepare_external_skill_task, validate_external_skill_output, validate_payload,
)

ROOT = Path(__file__).resolve().parents[3]
PREFERENCES = ROOT / "config/hotspot_report_preferences.yaml"


def _vendor(operation: str, payload: Any = None) -> dict:
    executable = ROOT / "vendor/TrendRadar/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        result = subprocess.run(
            [str(executable), "-B", "-m", "scripts.integrations.trendradar_report", operation],
            cwd=ROOT, input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            check=True, **hidden_process_kwargs(),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"TrendRadar {operation} failed: {exc.stderr}") from exc
    return json.loads(result.stdout)


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"collected_at", "platforms", "items"}:
        raise ValueError("snapshot must contain collected_at, platforms and items")
    if datetime.fromisoformat(value["collected_at"]).tzinfo is None:
        raise ValueError("collection time needs a timezone")
    platforms = value["platforms"]
    if not isinstance(platforms, list) or not platforms:
        raise ValueError("snapshot needs platform provenance")
    ids = set()
    for platform in platforms:
        if not isinstance(platform, dict) or set(platform) != {"id", "name", "updated_at", "status"}:
            raise ValueError("invalid platform provenance")
        if not platform["id"] or not platform["name"] or platform["id"] in ids:
            raise ValueError("platform identity must be nonempty and unique")
        ids.add(platform["id"])
        if platform["status"] not in {"success", "cache"}:
            raise ValueError("failed sources cannot be presented as a complete snapshot")
        if datetime.fromisoformat(platform["updated_at"]).tzinfo is None:
            raise ValueError("source time needs a timezone")
    if not isinstance(value["items"], list):
        raise ValueError("items must be a list")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"id", "platform_id", "title", "url", "ranks", "count"}:
            raise ValueError("invalid hotspot item")
        if any(not isinstance(item[k], str) for k in ("id", "platform_id", "title", "url")):
            raise ValueError("invalid hotspot text fields")
        if not item["id"] or item["id"] in seen or not item["title"].strip() or item["platform_id"] not in ids:
            raise ValueError("invalid or duplicate source identity")
        # This report uses one actual collection, not fabricated frequency history.
        if not isinstance(item["ranks"], list) or len(item["ranks"]) != 1 or type(item["ranks"][0]) is not int or item["ranks"][0] < 1:
            raise ValueError("snapshot items need one positive source rank")
        if type(item["count"]) is not int or item["count"] != 1:
            raise ValueError("one snapshot cannot invent repeat observations")
        seen.add(item["id"])
    return value


def prepare(snapshot: dict) -> dict:
    snapshot = _snapshot(snapshot)
    preferences = yaml.safe_load(PREFERENCES.read_text(encoding="utf-8"))
    task, _ = prepare_external_skill_task(
        FormalSkillContract.from_runtime_skill("hotspot_report"),
        {"snapshot": snapshot, "preferences": preferences},
        constraints={"model_selection": "current_agent", "topic_generation": False,
                     "formal_business_data_written": False, "return_via": "creation_assistant_hotspot_report.render"},
    )
    return {"task": task, "snapshot": snapshot, "formal_business_data_written": False}


def render(snapshot: dict, output: dict, top_n: int | None) -> dict:
    prepared = prepare(snapshot)
    validated = validate_external_skill_output(
        FormalSkillContract.from_runtime_skill("hotspot_report"), prepared["task"]["input"], output,
    )
    if top_n is not None and (type(top_n) is not int or top_n < 1):
        raise ValueError("top_n must be a positive user-requested integer or omitted")
    preferences = prepared["task"]["input"]["preferences"]
    rules = {r["id"]: r for r in preferences["rules"]}
    items = {item["id"]: item for item in snapshot["items"]}
    seen = set()
    title_groups = {}
    events = validated["events"]
    event_schema = FormalSkillContract.from_runtime_skill("hotspot_report").output_schema["properties"]["events"]["items"]
    for index, event in enumerate(events):
        validate_payload(event, event_schema)
        if type(event["uncertain"]) is not bool or any(
            not isinstance(k, str) for field in ("member_ids", "rule_ids") for k in event[field]
        ):
            raise ValueError("uncertain must be boolean and event references must be strings")
        members = event["member_ids"]
        if not members or any(k not in items for k in members) or len(set(members)) != len(members) or seen.intersection(members):
            raise ValueError("each source must belong to exactly one event")
        seen.update(members)
        if any(not event[k].strip() for k in ("title", "merge_reason", "reason")):
            raise ValueError("event title and decision/merge reasons are required")
        if any(rule not in rules for rule in event["rule_ids"]):
            raise ValueError("unknown preference rule")
        if event["decision"] == "exclude":
            if not event["rule_ids"] or event["uncertain"]:
                raise ValueError("exclusion requires an identified rule and cannot be uncertain")
        elif event["rule_ids"]:
            raise ValueError("kept events cannot claim exclusion rules")
        for key in members:
            title = items[key]["title"].strip()
            if title in title_groups and title_groups[title] != index:
                raise ValueError("identical source titles must be handled in the same event")
            title_groups[title] = index
    if seen != set(items):
        raise ValueError("every source must be accounted for, including excluded and below-Top items")

    scoring = _vendor("score", snapshot["items"])
    ranked, excluded = [], []
    for event in events:
        sources = [items[k] for k in event["member_ids"]]
        result = {**event, "sources": sources, "platform_count": len({s["platform_id"] for s in sources})}
        if event["decision"] == "exclude":
            result["matched_rules"] = [rules[k] for k in event["rule_ids"]]
            excluded.append(result)
        else:
            weights = [scoring["scores"][s["id"]] for s in sources]
            result["score"] = max(weights) + (sum(weights) - max(weights)) * 0.5
            ranked.append(result)
    ranked.sort(key=lambda e: -e["score"])
    previous = None
    rank = 0
    for position, event in enumerate(ranked, 1):
        if event["score"] != previous:
            rank = position
        event["rank"] = rank
        previous = event["score"]
    count = len(ranked) if top_n is None else top_n
    return {
        "collected_at": snapshot["collected_at"], "platforms": snapshot["platforms"],
        "preferences_version": preferences["version"], "scoring": {**scoring, "aggregate": "max + 0.5 * (sum - max)",
            "note": "同平台多条也累计；同分并列，展示顺序不代表差异；不是官方全网热度"},
        "top": ranked[:count], "below_top": ranked[count:], "excluded_by_preference": excluded,
        "counts": {"source_items": len(items), "retained_events": len(ranked), "excluded_events": len(excluded),
                   "excluded_source_items": sum(len(e["member_ids"]) for e in excluded)},
        "formal_business_data_written": False, "topic_generation": "not_triggered",
        "fact_status": "hotspot_titles_not_independently_verified",
    }


def execute(arguments: dict) -> dict:
    operation = arguments.get("operation")
    allowed = {"collect": {"operation"}, "prepare": {"operation", "snapshot"},
               "render": {"operation", "snapshot", "output", "top_n"}}
    if operation not in allowed or set(arguments) - allowed[operation]:
        raise ValueError("unsupported hotspot report operation or arguments")
    if operation == "collect":
        return prepare(_vendor("collect"))
    if operation == "prepare":
        return prepare(arguments["snapshot"])
    return render(arguments["snapshot"], arguments["output"], arguments.get("top_n"))
