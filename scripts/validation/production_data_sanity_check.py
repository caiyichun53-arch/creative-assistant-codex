"""Lightweight sanity checks against the real production database
(data/formal/production_activation.sqlite3), separate from the business-rule
correctness tests (which run against synthetic fixtures, not real data).

Why this exists: this project's own incident history has a real example of
exactly what this guards against -- 2026-07-08's "preparation_status stuck at the
table's old default" bug meant newly-promoted hits sat
forever in a stuck state and nothing noticed until a human manually checked
the real database days later. This script makes "is anything stuck" a
5-second mechanical check instead of something only a human curiosity-check
catches.

Deliberately NOT a replacement for tests/core (those check logic against
controlled fixtures) or the BR-* traceability tests (those check config
against the catalog). This only checks the real database's own internal
consistency -- does it exist, do the tables it should have exist, is
anything visibly stuck.

Usage:
    python -m scripts.validation.production_data_sanity_check
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "formal" / "production_activation.sqlite3"
REQUIRED_TABLES = ("competitor_accounts", "competitor_videos", "hits", "video_checks", "hit_transcripts", "hit_comments")
STUCK_REVERSE_STATUS_DAYS = 3


def check_db_exists() -> dict[str, Any]:
    return {"name": "db_file_exists", "passed": DB_PATH.exists(), "detail": str(DB_PATH)}


def check_required_tables_present(conn: sqlite3.Connection) -> dict[str, Any]:
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in REQUIRED_TABLES if t not in existing]
    return {"name": "required_tables_present", "passed": not missing, "detail": missing if missing else "all present"}


def check_no_stuck_reverse_prep(conn: sqlite3.Connection) -> dict[str, Any]:
    # Regression guard for the exact 2026-07-08 bug: hits that never actually
    # got picked up by reverse-prep because they landed on a stale default
    # instead of 'pending'. A row genuinely 'running'/'pending' for more than
    # a few days (normal processing is minutes) means something is stuck.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "hits" not in tables:
        return {"name": "no_stuck_reverse_prep_hits", "passed": True, "detail": "hits table absent, nothing to check"}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hits)")}
    if "preparation_status" not in columns or "promoted_at" not in columns:
        return {"name": "no_stuck_reverse_prep_hits", "passed": True, "detail": "preparation_status/promoted_at column absent, nothing to check"}
    stuck = conn.execute(
        "SELECT hit_id, preparation_status, promoted_at FROM hits "
        "WHERE preparation_status IN ('pending', 'running') "
        f"AND promoted_at <= datetime('now', '-{STUCK_REVERSE_STATUS_DAYS} days')"
    ).fetchall()
    return {
        "name": "no_stuck_reverse_prep_hits",
        "passed": not stuck,
        "detail": [dict(zip(("hit_id", "preparation_status", "promoted_at"), row)) for row in stuck] if stuck else "none stuck",
    }


def check_no_orphaned_hits(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "hits" not in tables or "competitor_accounts" not in tables:
        return {"name": "no_orphaned_hits", "passed": True, "detail": "hits/competitor_accounts table absent, nothing to check"}
    orphans = conn.execute(
        "SELECT hit_id, account_id FROM hits WHERE account_id NOT IN (SELECT account_id FROM competitor_accounts)"
    ).fetchall()
    return {
        "name": "no_orphaned_hits",
        "passed": not orphans,
        "detail": [{"hit_id": r[0], "account_id": r[1]} for r in orphans] if orphans else "none orphaned",
    }


def run_checklist() -> dict[str, Any]:
    db_check = check_db_exists()
    if not db_check["passed"]:
        return {"status": "SKIPPED", "checks": [db_check], "reason": "no local production database to check"}

    conn = sqlite3.connect(DB_PATH)
    try:
        checks = [
            db_check,
            check_required_tables_present(conn),
            check_no_stuck_reverse_prep(conn),
            check_no_orphaned_hits(conn),
        ]
    finally:
        conn.close()
    overall_passed = all(check["passed"] for check in checks)
    return {"status": "PASS" if overall_passed else "FAIL", "checks": checks}


def main() -> int:
    result = run_checklist()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("PASS", "SKIPPED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
