"""The one domain-owned registry for formal production boundaries.

This registry is deliberately separate from content types, search tags and the
legacy domain description.  It stores abstract production principles and their
provenance; it never turns sample titles or keywords into a whitelist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.core.business_data.domain_labels import (
    get_domain_pack,
)


PRODUCTION_BOUNDARY_REGISTRY_STATUSES = frozenset({"not_frozen", "frozen"})


def get_production_boundary_registry(domain_label: str) -> dict[str, Any]:
    """Return a validated boundary registry; absence is explicitly not frozen."""

    pack = get_domain_pack(domain_label)
    raw = pack.get("production_boundary_registry")
    if raw is None:
        return {
            "status": "NOT_FROZEN",
            "version": "0",
            "in_boundary_principles": [],
            "out_boundary_principles": [],
            "unknown_topic_rule": {},
        }
    if not isinstance(raw, dict):
        raise ValueError(f"domain {domain_label} production_boundary_registry must be a mapping")
    status = str(raw.get("status") or "NOT_FROZEN").strip().casefold()
    if status not in PRODUCTION_BOUNDARY_REGISTRY_STATUSES:
        raise ValueError(
            f"domain {domain_label} production boundary has unsupported status {status!r}"
        )
    version = str(raw.get("version") or "0").strip()
    in_principles = raw.get("in_boundary_principles") or []
    out_principles = raw.get("out_boundary_principles") or []
    unknown_rule = raw.get("unknown_topic_rule") or {}
    provenance = raw.get("provenance") or {}
    if not isinstance(in_principles, list) or not isinstance(out_principles, list):
        raise ValueError(f"domain {domain_label} production boundary principles must be arrays")
    if not isinstance(unknown_rule, dict) or not isinstance(provenance, dict):
        raise ValueError(f"domain {domain_label} production boundary metadata is invalid")
    if status == "frozen" and (not in_principles or not out_principles or not unknown_rule):
        raise ValueError(f"domain {domain_label} frozen production boundary is incomplete")
    normalized: dict[str, Any] = {
        "status": status.upper(),
        "version": version,
        "in_boundary_principles": [dict(item) for item in in_principles if isinstance(item, dict)],
        "out_boundary_principles": [dict(item) for item in out_principles if isinstance(item, dict)],
        "unknown_topic_rule": dict(unknown_rule),
    }
    if provenance:
        normalized["provenance"] = dict(provenance)
    return normalized


def require_frozen_production_boundary(domain_label: str) -> dict[str, Any]:
    registry = get_production_boundary_registry(domain_label)
    if str(registry.get("status") or "").casefold() != "frozen":
        raise ValueError(f"domain {domain_label} production boundary is not FROZEN")
    if not str(registry.get("version") or "").strip() or str(registry["version"]) == "0":
        raise ValueError(f"domain {domain_label} production boundary needs a version")
    return registry


def freeze_production_boundary_registry(
    domain_label: str,
    *,
    in_boundary_principles: list[dict[str, Any]],
    out_boundary_principles: list[dict[str, Any]],
    unknown_topic_rule: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Write the independent, user-approved boundary asset once."""

    if not in_boundary_principles or not out_boundary_principles:
        raise ValueError("a frozen production boundary needs both in and out principles")
    if not isinstance(unknown_topic_rule, dict) or not str(unknown_topic_rule.get("rule") or "").strip():
        raise ValueError("a frozen production boundary needs an unknown-topic rule")
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("a frozen production boundary needs provenance")
    pack = get_domain_pack(domain_label)
    raw = pack.get("production_boundary_registry")
    if isinstance(raw, dict) and str(raw.get("status") or "").strip().casefold() == "frozen":
        raise ValueError(f"domain {domain_label} already has a frozen production boundary")

    required = ("boundary_id", "principle", "rationale")
    normalized_sets: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for principles in (in_boundary_principles, out_boundary_principles):
        normalized: list[dict[str, Any]] = []
        for item in principles:
            if not isinstance(item, dict):
                raise ValueError("production boundary principles must be mappings")
            entry = {key: str(item.get(key) or "").strip() for key in required}
            if any(not entry[key] for key in required):
                raise ValueError("production boundary principle is incomplete")
            if entry["boundary_id"] in seen:
                raise ValueError(f"duplicate production boundary {entry['boundary_id']!r}")
            seen.add(entry["boundary_id"])
            entry["origin"] = str(item.get("origin") or "sample_inference").strip()
            evidence_refs = item.get("evidence_refs") or []
            if not isinstance(evidence_refs, list):
                raise ValueError("production boundary evidence_refs must be an array")
            entry["evidence_refs"] = [str(value).strip() for value in evidence_refs if str(value).strip()]
            merged_from = item.get("merged_from") or []
            entry["merged_from"] = [str(value).strip() for value in merged_from if str(value).strip()]
            normalized.append(entry)
        normalized_sets.append(normalized)

    previous_version = str((raw or {}).get("version") or "0").strip() if isinstance(raw, dict) else "0"
    next_version = str(int(previous_version) + 1) if previous_version.isdigit() else "1"
    path = Path(str(pack["config_path"])).resolve()
    payload = {key: value for key, value in pack.items() if key != "config_path"}
    payload["production_boundary_registry"] = {
        "status": "FROZEN",
        "version": next_version,
        "in_boundary_principles": normalized_sets[0],
        "out_boundary_principles": normalized_sets[1],
        "unknown_topic_rule": {
            "rule": str(unknown_topic_rule.get("rule") or "").strip(),
            "uncertain_action": str(unknown_topic_rule.get("uncertain_action") or "待用户确认").strip(),
            "origin": str(unknown_topic_rule.get("origin") or "sample_inference").strip(),
        },
        "provenance": dict(provenance),
    }
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temporary.replace(path)
    return get_production_boundary_registry(domain_label)


def evaluate_production_boundary(
    domain_label: str,
    *,
    outcome: str,
    matched_principle_ids: list[str] | tuple[str, ...] = (),
    violated_principle_ids: list[str] | tuple[str, ...] = (),
    basis: str,
) -> dict[str, Any]:
    """Validate a production judgement against the frozen principle IDs.

    The evaluator deliberately accepts principle matches, not topic keywords.
    The caller/model must explain which abstract principles apply; sample title
    or object-name matching is never performed here.
    """

    registry = require_frozen_production_boundary(domain_label)
    allowed_in = {str(item.get("boundary_id")) for item in registry["in_boundary_principles"]}
    allowed_out = {str(item.get("boundary_id")) for item in registry["out_boundary_principles"]}
    matched = [str(value).strip() for value in matched_principle_ids if str(value).strip()]
    violated = [str(value).strip() for value in violated_principle_ids if str(value).strip()]
    if any(value not in allowed_in for value in matched):
        raise ValueError("production boundary judgement cites an unknown in-boundary principle")
    if any(value not in allowed_out for value in violated):
        raise ValueError("production boundary judgement cites an unknown out-boundary principle")
    normalized_outcome = str(outcome or "").strip()
    if normalized_outcome not in {"in_boundary", "out_of_boundary", "needs_user_review"}:
        raise ValueError("production boundary judgement has an unsupported outcome")
    if normalized_outcome == "in_boundary" and not matched:
        raise ValueError("an in-boundary judgement needs an applicable principle")
    if normalized_outcome == "out_of_boundary" and not violated:
        raise ValueError("an out-of-boundary judgement needs a violated principle")
    if not str(basis or "").strip():
        raise ValueError("production boundary judgement needs a basis")
    return {
        "status": normalized_outcome,
        "boundary_version": str(registry["version"]),
        "matched_principle_ids": matched,
        "violated_principle_ids": violated,
        "basis": str(basis).strip(),
        "used_keyword_whitelist": False,
    }
