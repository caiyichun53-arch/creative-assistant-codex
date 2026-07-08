"""Shared execution-contract gate: the one mechanism every script that produces
a real external side effect (DB write, network request, software install, paid
API call) must go through before acting.

2026-07-08: extracted from run_competitor_registration_full.py's
validate_registration_execution_contract(), which only ever gated production
database writes. The same session, ad hoc Bash/PowerShell commands (installing
ffmpeg, calling MediaCrawler for a live download, storing a real API key) were
run directly against the real world for the ASR/reverse-prep exploration, with
no contract check at all -- the same class of drift as an ungated DB write,
just in a different module. See BUSINESS_RULE_CATALOG.yaml's amendment history
and the project constitution files' "执行纪律" section for the full incident.

This module does not know what "correct" looks like for any particular domain
-- that stays with each caller (e.g. validate_registration_execution_contract's
own threshold checks). What this module enforces is narrower but load-bearing:
a script cannot claim to be contract-gated by citing a BUSINESS_RULE_CATALOG.yaml
requirement_id that does not actually exist, and cannot skip citing anything at
all. Real correctness checking (thresholds, defaults, forbidden_behavior) is
still the caller's job, using the entries this returns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "BUSINESS_RULE_CATALOG.yaml"


class UncitedRequirementError(ValueError):
    """Raised when a caller cites a requirement_id that is not in the catalog,
    or cites nothing at all -- either way, the action is not actually
    contract-gated."""


def _load_catalog() -> list[dict[str, Any]]:
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise ValueError(f"BUSINESS_RULE_CATALOG.yaml must have a top-level 'rules' list: {CATALOG_PATH}")
    return data["rules"]


def load_requirement(requirement_id: str) -> dict[str, Any]:
    """Load one requirement's full catalog entry. Raises UncitedRequirementError
    if it does not exist -- citing a requirement_id that is not real is exactly
    the kind of unverified claim this module exists to catch."""
    for entry in _load_catalog():
        if entry.get("requirement_id") == requirement_id:
            return entry
    raise UncitedRequirementError(
        f"'{requirement_id}' is not a requirement_id in BUSINESS_RULE_CATALOG.yaml -- "
        "cannot gate an execution on a rule that does not exist."
    )


def require_catalog_citations(requirement_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Entry point every contract-gated script must call before doing anything
    with a real external side effect. Returns {requirement_id: full_entry} for
    every id, so the caller can then assert its actual behavior/config matches
    each entry's expected_behavior/defaults/thresholds.

    Raises UncitedRequirementError if requirement_ids is empty (an action with
    no cited rule is not contract-gated at all, it just looks like it is) or if
    any id does not exist in the catalog.
    """
    if not requirement_ids:
        raise UncitedRequirementError(
            "require_catalog_citations() called with no requirement_ids -- an execution "
            "with nothing cited from BUSINESS_RULE_CATALOG.yaml is not contract-gated."
        )
    return {requirement_id: load_requirement(requirement_id) for requirement_id in requirement_ids}
