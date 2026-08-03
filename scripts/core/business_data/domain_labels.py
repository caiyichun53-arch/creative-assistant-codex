"""Versioned domain-pack registry used by every business route.

Formal domains come from the domain-pack registry.  Adding a domain therefore
adds data and regression samples; it does not require editing the common Stage
1 pipeline or another hard-coded enum.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOMAIN_CONFIG_DIR = ROOT / "config" / "domain_packs"
_ACTIVE_DOMAIN_CONFIG_DIR = DOMAIN_CONFIG_DIR
INTERNAL_DOMAIN_LABELS = frozenset({"cross_domain", "unknown", "third_domain_neutral"})


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


@lru_cache(maxsize=1)
def configured_domain_packs() -> dict[str, dict[str, Any]]:
    return load_domain_packs()


def refresh_domain_packs() -> dict[str, dict[str, Any]]:
    """Reload domain packs after a confirmed domain configuration change."""
    configured_domain_packs.cache_clear()
    return configured_domain_packs()


def set_domain_pack_config_dir(config_dir: Path) -> dict[str, dict[str, Any]]:
    """Bind one process to an explicit registry directory (used by the local UI and isolated checks)."""
    global _ACTIVE_DOMAIN_CONFIG_DIR
    _ACTIVE_DOMAIN_CONFIG_DIR = config_dir.resolve()
    return refresh_domain_packs()


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


def get_discovery_policy(domain_label: str) -> dict[str, Any]:
    pack = get_domain_pack(domain_label)
    policy = pack.get("discovery") or {}
    if not isinstance(policy, dict):
        raise ValueError(f"domain {domain_label} discovery policy must be a mapping")
    return policy


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
