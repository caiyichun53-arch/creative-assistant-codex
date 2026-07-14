"""Versioned domain-pack registry used by every business route.

Formal domains come from ``config/domains/*.yaml``.  Adding a domain therefore
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
INTERNAL_DOMAIN_LABELS = frozenset({"cross_domain", "unknown", "third_domain_neutral"})


def load_domain_packs(config_dir: Path = DOMAIN_CONFIG_DIR) -> dict[str, dict[str, Any]]:
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


FORMAL_DOMAIN_LABELS = formal_domain_labels()
ALLOWED_DOMAIN_LABELS = frozenset(FORMAL_DOMAIN_LABELS | INTERNAL_DOMAIN_LABELS)
