"""Manually register one hotspot observation through the formal source store.

Manual observations and TrendRadar observations are separate source paths.
Both use the versioned domain pack for domain matching; neither reads the
platform-search topic library.

Usage:
    python -m scripts.core.business_data.run_hotspot_registration \
        --domain-label fan_kepu_social_life --text "..." --created-by "..." --db ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.domain_labels import FORMAL_DOMAIN_LABELS, get_discovery_policy  # noqa: E402
from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.persistence.goal01_store import uuid7  # noqa: E402


def validate_hotspot_registration_execution_contract() -> dict[str, Any]:
    return require_baseline_citations(["3", "17"])


def match_domain_policy_terms(conn: sqlite3.Connection, *, domain_label: str, raw_text: str) -> list[str]:
    """Match the versioned domain policy without consulting search tags."""
    del conn
    terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
    folded = raw_text.casefold()
    return [term for term in terms if term.casefold() in folded]


def register_hotspot_event(
    conn: sqlite3.Connection,
    *,
    domain_label: str,
    raw_text: str,
    created_by: str,
) -> dict[str, Any]:
    validate_hotspot_registration_execution_contract()
    if domain_label not in FORMAL_DOMAIN_LABELS:
        raise ValueError(f"unsupported domain_label: {domain_label!r}")
    if not raw_text.strip():
        raise ValueError("raw_text is required -- a hotspot with no real content cannot be registered")
    if not created_by.strip():
        raise ValueError("created_by is required -- this is a real human registering a real observation")

    matched_tags = match_domain_policy_terms(conn, domain_label=domain_label, raw_text=raw_text)
    event_id = uuid7()
    conn.execute(
        """
        INSERT INTO hotspot_events(event_id, domain_label, raw_text, matched_tags, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, domain_label, raw_text, json.dumps(matched_tags, ensure_ascii=False), created_by),
    )
    conn.commit()
    return {"event_id": event_id, "domain_label": domain_label, "matched_tags": matched_tags}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manually register one real hotspot observation.")
    parser.add_argument("--domain-label", required=True)
    parser.add_argument("--text", required=True, dest="raw_text")
    parser.add_argument("--created-by", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        report = register_hotspot_event(conn, domain_label=args.domain_label, raw_text=args.raw_text, created_by=args.created_by)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        import yaml as _yaml
        print(_yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
