"""Versioned domain-pack registry used by every business route.

Formal domains come from the domain-pack registry.  Adding a domain therefore
adds data and regression samples; it does not require editing the common Stage
1 pipeline or another hard-coded enum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOMAIN_CONFIG_DIR = ROOT / "config" / "domain_packs"
_ACTIVE_DOMAIN_CONFIG_DIR = DOMAIN_CONFIG_DIR
INTERNAL_DOMAIN_LABELS = frozenset({"cross_domain", "unknown", "third_domain_neutral"})
CONTENT_TYPE_REGISTRY_STATUSES = frozenset({"draft", "not_frozen", "frozen"})


def load_domain_packs(config_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    config_dir = (config_dir or _ACTIVE_DOMAIN_CONFIG_DIR).resolve()
    packs: dict[str, dict[str, Any]] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        label = str(payload.get("formal_domain_label") or "").strip()
        if not label:
            continue
        if label in packs:
            raise ValueError(f"duplicate formal_domain_label {label!r} in {path}")
        packs[label] = {**payload, "config_path": str(path)}
    return packs


def configured_domain_packs() -> dict[str, dict[str, Any]]:
    return load_domain_packs()


def set_domain_pack_config_dir(config_dir: Path) -> dict[str, dict[str, Any]]:
    """Bind one process to an explicit registry directory (used by the local UI and isolated checks)."""
    global _ACTIVE_DOMAIN_CONFIG_DIR
    _ACTIVE_DOMAIN_CONFIG_DIR = config_dir.resolve()
    return load_domain_packs()


def formal_domain_labels() -> frozenset[str]:
    return frozenset(
        label
        for label, pack in configured_domain_packs().items()
        if str(pack.get("activation_status") or "").strip() != "fixture_only"
    )


def get_domain_pack(domain_label: str) -> dict[str, Any]:
    try:
        return configured_domain_packs()[domain_label]
    except KeyError as exc:
        raise ValueError(f"unknown formal domain_label: {domain_label}") from exc


def get_content_type_registry(domain_label: str) -> dict[str, Any]:
    """Return the domain-owned content-type registry without inventing types.

    A missing registry is deliberately treated as NOT_FROZEN.  This keeps old
    domain packs readable while preventing a production caller from silently
    treating observed historical labels as a whitelist.
    """
    pack = get_domain_pack(domain_label)
    raw = pack.get("content_type_registry")
    if raw is None:
        return {"status": "NOT_FROZEN", "version": "0", "types": []}
    if not isinstance(raw, dict):
        raise ValueError(f"domain {domain_label} content_type_registry must be a mapping")
    status = str(raw.get("status") or "NOT_FROZEN").strip().casefold()
    if status not in CONTENT_TYPE_REGISTRY_STATUSES:
        raise ValueError(
            f"domain {domain_label} content_type_registry has unsupported status {status!r}"
        )
    version = str(raw.get("version") or "0").strip()
    raw_types = raw.get("types") or []
    if not isinstance(raw_types, list):
        raise ValueError(f"domain {domain_label} content_type_registry.types must be a list")
    types: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = (
        "canonical_id", "name", "core_subject", "content_promise",
        "required_delivery", "scope_boundary",
    )
    for item in raw_types:
        if not isinstance(item, dict):
            raise ValueError(f"domain {domain_label} content type entry must be a mapping")
        entry = {key: str(item.get(key) or "").strip() for key in required}
        if any(not entry[key] for key in required):
            raise ValueError(f"domain {domain_label} content type entry is incomplete")
        canonical_id = entry["canonical_id"]
        if canonical_id in seen:
            raise ValueError(f"domain {domain_label} has duplicate content type {canonical_id!r}")
        seen.add(canonical_id)
        entry["status"] = str(item.get("status") or status).strip().upper()
        entry["version"] = str(item.get("version") or version).strip()
        if entry["status"].casefold() not in CONTENT_TYPE_REGISTRY_STATUSES:
            raise ValueError(
                f"domain {domain_label} content type {canonical_id!r} has unsupported status {entry['status']!r}"
            )
        if status == "frozen" and entry["status"] != "FROZEN":
            raise ValueError(
                f"domain {domain_label} frozen registry contains a non-frozen content type {canonical_id!r}"
            )
        types.append(entry)
    if status == "frozen" and not types:
        raise ValueError(f"domain {domain_label} cannot freeze an empty content type registry")
    result = {"status": status.upper(), "version": version, "types": types}
    provenance = raw.get("provenance")
    if isinstance(provenance, dict):
        result["provenance"] = dict(provenance)
    return result


def freeze_content_type_registry(
    domain_label: str,
    types: list[dict[str, Any]],
    *,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Write the one domain-owned frozen type registry after user approval.

    The production pipeline already reads ``content_type_registry`` from the
    domain pack.  This helper deliberately writes that same asset instead of
    creating a second database-only registry.  A domain with an existing
    frozen registry is protected from silent replacement; a later expansion
    flow is outside the first cold-start lifecycle.
    """
    if not isinstance(types, list) or not types:
        raise ValueError("a frozen content type registry needs at least one approved type")
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("a frozen content type registry needs freeze provenance")
    pack = get_domain_pack(domain_label)
    raw = pack.get("content_type_registry")
    if isinstance(raw, dict) and str(raw.get("status") or "").strip().casefold() == "frozen":
        raise ValueError(f"domain {domain_label} already has a frozen content type registry")
    required = (
        "canonical_id", "name", "core_subject", "content_promise",
        "required_delivery", "scope_boundary",
    )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in types:
        if not isinstance(item, dict):
            raise ValueError("frozen content type entries must be mappings")
        entry = {key: str(item.get(key) or "").strip() for key in required}
        if any(not entry[key] for key in required):
            raise ValueError("frozen content type entries are incomplete")
        if entry["canonical_id"] in seen:
            raise ValueError(f"duplicate frozen content type {entry['canonical_id']!r}")
        seen.add(entry["canonical_id"])
        entry["status"] = "FROZEN"
        normalized.append(entry)
    previous_version = str((raw or {}).get("version") or "0").strip() if isinstance(raw, dict) else "0"
    try:
        next_version = str(int(previous_version) + 1) if previous_version.isdigit() else "1"
    except ValueError:
        next_version = "1"
    path = Path(str(pack["config_path"])).resolve()
    payload = {key: value for key, value in pack.items() if key != "config_path"}
    payload["content_type_registry"] = {
        "status": "FROZEN",
        "version": next_version,
        "types": [{**item, "version": next_version} for item in normalized],
        "provenance": dict(provenance),
    }
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    temporary.replace(path)
    return get_content_type_registry(domain_label)


def require_frozen_content_type_registry(domain_label: str) -> dict[str, Any]:
    """Fail closed unless the domain configuration has a valid frozen registry.

    The common pipeline deliberately does not know which ids belong to a
    domain.  It only enforces the lifecycle gate and consumes the ids supplied
    by the domain pack.
    """
    registry = get_content_type_registry(domain_label)
    if str(registry["status"]).casefold() != "frozen":
        raise ValueError(
            f"domain {domain_label} content type registry is not FROZEN"
        )
    if not str(registry.get("version") or "").strip() or str(registry.get("version") or "").strip() == "0":
        raise ValueError(f"domain {domain_label} frozen content type registry needs a version")
    return registry


def get_frozen_content_types(domain_label: str) -> tuple[dict[str, Any], ...]:
    """Return only human-approved types; observed labels never enter here."""
    registry = get_content_type_registry(domain_label)
    if str(registry["status"]).casefold() != "frozen":
        return ()
    registry = require_frozen_content_type_registry(domain_label)
    return tuple(dict(item) for item in registry["types"])


def project_content_type(
    domain_label: str,
    *,
    lifecycle: str,
    canonical_id: str | None,
) -> dict[str, Any]:
    """Validate a model's type projection against the domain registry.

    DISCOVER records an observation and never turns it into an approved type.
    CLASSIFY accepts only a frozen registry id.  The two failure values are
    data, rather than a fallback to free-form generation.
    """
    mode = str(lifecycle or "").strip().casefold()
    if mode not in {"discover", "classify"}:
        raise ValueError(f"unsupported content type lifecycle {lifecycle!r}")
    candidate = str(canonical_id or "").strip()
    registry = get_content_type_registry(domain_label)
    if mode == "discover":
        return {
            "status": "observed",
            "canonical_id": candidate,
            "registry_status": registry["status"],
        }
    if str(registry["status"]).casefold() != "frozen":
        return {
            "status": "registry_not_frozen",
            "canonical_id": "NO_MATCH",
            "registry_status": registry["status"],
        }
    approved = {str(item["canonical_id"]): item for item in registry["types"]}
    if not candidate:
        return {
            "status": "no_match",
            "canonical_id": "NO_MATCH",
            "registry_status": registry["status"],
        }
    if candidate == "NO_MATCH":
        return {
            "status": "no_match",
            "canonical_id": "NO_MATCH",
            "registry_status": registry["status"],
        }
    if candidate == "OUT_OF_SCOPE":
        return {
            "status": "out_of_scope",
            "canonical_id": "OUT_OF_SCOPE",
            "registry_status": registry["status"],
        }
    if candidate not in approved:
        return {
            "status": "out_of_scope",
            "canonical_id": "OUT_OF_SCOPE",
            "registry_status": registry["status"],
        }
    return {
        "status": "matched",
        "canonical_id": candidate,
        "registry_status": registry["status"],
        "type": dict(approved[candidate]),
    }


def get_discovery_policy(domain_label: str) -> dict[str, Any]:
    pack = get_domain_pack(domain_label)
    policy = pack.get("discovery") or {}
    if not isinstance(policy, dict):
        raise ValueError(f"domain {domain_label} discovery policy must be a mapping")
    return policy


def get_exploration_policy(domain_label: str) -> dict[str, dict[str, Any]]:
    """Return explicit person/work/collection rules for one formal domain.

    Exploration is a shared business skeleton, but its material route and
    quantity requirements belong to the domain pack.  A domain without an
    explicit section fails closed instead of inheriting another domain's
    rules.
    """
    pack = get_domain_pack(domain_label)
    policy = pack.get("exploration")
    if not isinstance(policy, dict) or not policy:
        raise ValueError(
            f"domain {domain_label} must explicitly define an exploration policy"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for key in ("person", "work", "collection"):
        entry = policy.get(key)
        if not isinstance(entry, dict):
            raise ValueError(
                f"domain {domain_label} exploration policy must define {key}"
            )
        normalized[key] = entry
    return normalized


def get_content_workflow_mode(domain_label: str) -> str:
    """Return the human-controlled workflow switch for one formal domain.

    The switch is deliberately stored in the domain pack rather than inferred
    from quality metrics.  Only a human may change it from manual_guard to
    mature_automatic, or back again.
    """
    pack = get_domain_pack(domain_label)
    mode = str(pack.get("workflow_mode") or "").strip()
    if mode not in {"manual_guard", "mature_automatic"}:
        raise ValueError(
            f"domain {domain_label} must explicitly set workflow_mode to manual_guard or mature_automatic"
        )
    return mode


def hotspot_global_risk_block_terms() -> tuple[str, ...]:
    """Terms that block every hotspot before shared event judgement.

    Domain exclusion terms remain post-judgement candidate checks. They must
    never decide whether a raw hotspot reaches the shared judgement.
    """
    terms: set[str] = set()
    for pack in configured_domain_packs().values():
        policy = pack.get("discovery") or {}
        if isinstance(policy, dict):
            terms.update(str(term).strip() for term in policy.get("risk_block_terms", []) if str(term).strip())
    return tuple(sorted(terms))


FORMAL_DOMAIN_LABELS = formal_domain_labels()
ALLOWED_DOMAIN_LABELS = frozenset(FORMAL_DOMAIN_LABELS | INTERNAL_DOMAIN_LABELS)
