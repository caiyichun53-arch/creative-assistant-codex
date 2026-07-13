"""Shared execution-contract gate for real external side effects.

Every script that can write a real database, request a network resource,
install software, or call a paid API must cite one or more sections of the
effective design baseline before acting.  The gate intentionally verifies only
that the cited section exists; each caller remains responsible for enforcing
the concrete configuration and data invariants it implements.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "docs" / "EFFECTIVE_DESIGN_BASELINE.md"
_SECTION_HEADING = re.compile(r"^## (?P<number>\d+)\. (?P<title>.+?)\s*$", re.MULTILINE)


class UncitedBaselineError(ValueError):
    """Raised when an action cites no effective-baseline section, or a missing one."""


def _load_sections() -> dict[str, dict[str, str]]:
    if not BASELINE_PATH.is_file():
        raise UncitedBaselineError(f"effective design baseline is missing: {BASELINE_PATH}")
    sections = {
        match.group("number"): {
            "section_id": match.group("number"),
            "title": match.group("title"),
            "path": str(BASELINE_PATH.relative_to(ROOT)).replace("\\", "/"),
        }
        for match in _SECTION_HEADING.finditer(BASELINE_PATH.read_text(encoding="utf-8"))
    }
    if not sections:
        raise UncitedBaselineError(f"no numbered sections found in {BASELINE_PATH}")
    return sections


def load_baseline_section(section_id: str) -> dict[str, str]:
    """Load one cited section of the sole effective design baseline."""
    normalized = str(section_id).removeprefix("§")
    try:
        return _load_sections()[normalized]
    except KeyError as exc:
        raise UncitedBaselineError(
            f"section '{section_id}' does not exist in {BASELINE_PATH.name}; "
            "the action cannot be treated as contract-gated."
        ) from exc


def require_baseline_citations(section_ids: list[str]) -> dict[str, dict[str, str]]:
    """Require real effective-baseline citations before an external action.

    ``section_ids`` uses the numbered headings in
    ``docs/EFFECTIVE_DESIGN_BASELINE.md`` (for example ``[\"16\"]``).
    """
    if not section_ids:
        raise UncitedBaselineError(
            "require_baseline_citations() received no baseline section; "
            "an uncited external action is not contract-gated."
        )
    return {str(section_id): load_baseline_section(str(section_id)) for section_id in section_ids}
