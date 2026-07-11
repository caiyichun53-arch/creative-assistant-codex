"""Core-side wiring between script_reviews (script_review's real output, see
run_script_review.py) and final_drafts (BR-CONTENT-005).

2026-07-13 (置顶规则总表 V0.6.3 卷首核对后): "最终稿" (final draft) did not exist
as a concept anywhere in the catalog before this -- the chain stopped at
script_drafts/script_reviews, with review approval implicitly standing in for
"this is done." The pinned rules doc requires final-draft confirmation to be
its OWN explicit user action (条目5/56: "最终稿必须由用户确认"), separate from
review approval. This binding is deliberately the simplest possible shape:
copy the approved review's polished_text into a new final_drafts row at
human_review_status='pending_review', and let review_queue.py's existing
approve/reject mechanism be that separate confirmation -- no new machinery,
no content transformation, just a real extra gate the document requires.

Usage:
    python -m scripts.core.experience.run_final_draft --limit 1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _safe_db_path  # noqa: E402


def validate_final_draft_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-CONTENT-005"])


def select_reviews_pending_final(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real script_reviews rows with human_review_status='approved' that have
    never been promoted to final_drafts."""
    return conn.execute(
        """
        SELECT script_reviews.*
          FROM script_reviews
         WHERE script_reviews.human_review_status = 'approved'
           AND NOT EXISTS (
               SELECT 1 FROM final_drafts WHERE final_drafts.source_review_id = script_reviews.review_id
           )
         ORDER BY script_reviews.created_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _persist_final_draft(conn: sqlite3.Connection, review_id: str, polished_text: str, *, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM final_drafts WHERE source_review_id=?", (review_id,)).fetchone()[0]) + 1
    final_draft_id = f"{review_id}_final_v{version}"
    conn.execute(
        """
        INSERT INTO final_drafts(final_draft_id, source_review_id, version, final_text, run_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (final_draft_id, review_id, version, polished_text, run_id),
    )
    conn.commit()
    return final_draft_id


def propose_one_final_draft(conn: sqlite3.Connection, review_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    final_draft_id = _persist_final_draft(conn, review_row["review_id"], review_row["polished_text"], run_id=run_id)
    return {"review_id": review_row["review_id"], "status": "completed", "final_draft_id": final_draft_id}


def run_final_draft(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    validate_final_draft_execution_contract()
    run_id = "final_draft_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_reviews_pending_final(conn, limit=limit)
    results = [propose_one_final_draft(conn, row, run_id=run_id) for row in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Propose pending final_drafts rows from approved script_reviews.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    db_path = _safe_db_path(Path(args.db))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        report = run_final_draft(conn, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        import yaml as _yaml
        print(_yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0 if report["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
