"""Catches a specific, real failure mode: a new module-level numeric constant
added to the real-data binding layer (scripts/core/business_data,
scripts/core/experience) with no indication of where the number came from.

2026-07-10: built after TOP_COMMENTS_PER_HIT = 3 was added to
run_source_to_topic.py with a comment explaining *why* comments are used as
evidence but never admitting the count itself was picked with no source --
the same failure mode as the historical baseline_min_samples=10 fabrication
(BR-BASELINE-003), reproduced in the same session that was fixing exactly
this class of problem elsewhere. Existing gates (test_business_rule_
traceability.py) only check numbers that are ALREADY registered in
BUSINESS_RULE_CATALOG.yaml against the catalog -- they cannot catch a new
number that was never registered anywhere in the first place. This closes
that specific gap: it does not require every constant to be "correct", only
that its provenance is stated one way or the other.

Rule: every module-level ALL_CAPS numeric constant in the scanned
directories must have, within a few lines above (or on the same line), a
comment containing one of:
  - a BR-*/GATE-* catalog citation
  - a reference to a schema/contract file (".yaml", "_CONTRACT", "_SCHEMA")
  - an explicit "UNSOURCED" admission that it has no source yet
A constant with no comment at all, or a comment that explains the *purpose*
of the number without ever saying where the *value* came from, fails this
check -- silence is not an acceptable third option.

Usage:
    python -m scripts.validation.unsourced_constant_check
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (
    ROOT / "scripts" / "core" / "business_data",
    ROOT / "scripts" / "core" / "experience",
)
LOOKBACK_LINES = 12

CONSTANT_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?::\s*[A-Za-z\[\], ]+)?=\s*(-?\d+(?:\.\d+)?)\s*(#.*)?$")
GROUNDING_MARKERS = re.compile(
    r"BR-[A-Z]+-\d+|GATE-[A-Z0-9-]+|\.yaml|_CONTRACT|_SCHEMA|CATALOG|UNSOURCED",
    re.IGNORECASE,
)

# Extend only when a constant is genuinely self-evident (e.g. a version
# number, an HTTP status code) -- the default is "explain it", not "assume
# it's fine".
ALLOWED_UNGROUNDED_NAMES: frozenset[str] = frozenset()


def _is_grounded(lines: list[str], line_index: int) -> bool:
    same_line = lines[line_index]
    if GROUNDING_MARKERS.search(same_line):
        return True
    start = max(0, line_index - LOOKBACK_LINES)
    block = "\n".join(lines[start:line_index])
    return bool(GROUNDING_MARKERS.search(block))


def scan_file(path: Path) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        match = CONSTANT_ASSIGNMENT.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in ALLOWED_UNGROUNDED_NAMES:
            continue
        if not _is_grounded(lines, i):
            hits.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"), "line": i + 1, "name": name, "text": line.strip()})
    return hits


def run_checklist() -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for root in SCANNED_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path.name.startswith("test_") or "__pycache__" in path.parts:
                continue
            hits.extend(scan_file(path))
    return {"status": "PASS" if not hits else "FAIL", "unsourced_constants": hits}


def main() -> int:
    result = run_checklist()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
