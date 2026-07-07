"""One-time migration: rebuild competitor_videos/baselines/hits/video_checks
under the 2026-07-07 master-doc-realignment schema (BUSINESS_RULE_CATALOG.yaml
BR-HIT-001 amendment_2026_07_07_master_doc_realignment), reclassifying each
existing video's raw crawl data into the new first_contact_category model
instead of discarding it.

User decision (explicit, 2026-07-07): "清空重建" -- old hits/baselines computed
under the retired median*excess_threshold/P90/floor formula have no value under
the new design and are dropped; the underlying raw crawl data (like_count,
comment_count, publish_time, titles, raw_json, etc.) does have value and is
carried forward, reclassified.

This is a real production-data write operation -- per this project's engineering
constitution it must go through a single committed, reviewed script, never an
ad-hoc command. This script is that entry point. It always backs up the target DB
file (timestamped copy, never overwritten) before touching it, and refuses to
run against anything outside data/formal (same guard as
run_competitor_registration_full.py's _safe_db_path).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import install_schema  # noqa: E402
from scripts.core.business_data.run_competitor_registration_full import (  # noqa: E402
    _parse_datetime,
    _safe_db_path,
    classify_first_contact_category,
    stable_check_id,
)


def migrate(db_path: Path, *, observe_days: int = 7) -> dict[str, Any]:
    resolved = _safe_db_path(db_path)
    backup_path = resolved.with_name(
        resolved.stem + "_pre_master_doc_realignment_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + resolved.suffix
    )
    shutil.copy2(resolved, backup_path)

    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    try:
        old_rows = conn.execute("SELECT * FROM competitor_videos").fetchall()
        old_row_count = len(old_rows)
        old_baseline_count = conn.execute("SELECT count(*) FROM baselines").fetchone()[0]
        old_hit_count = conn.execute("SELECT count(*) FROM hits").fetchone()[0]

        for table in ("hits", "baselines", "video_checks", "competitor_videos"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("DROP VIEW IF EXISTS observation_pool")
        conn.commit()

        install_schema(conn)

        now = datetime.now(timezone.utc)
        run_id = "migration_master_doc_realignment_" + now.strftime("%Y%m%dT%H%M%SZ")
        migrated = 0
        missing_publish_time = 0
        category_counts = {"historical_mature": 0, "transition": 0}

        for row in old_rows:
            published_at = _parse_datetime(row["publish_time"])
            category = classify_first_contact_category(published_at, now, observe_days)
            excluded_reason = None if category is not None else "missing_publish_time"
            first_seen_at = _parse_datetime(row["first_seen_at"]) or now
            discovery_delay_hours = (
                round((first_seen_at - published_at).total_seconds() / 3600, 2) if published_at is not None else None
            )

            conn.execute(
                """
                INSERT INTO competitor_videos(
                    video_id, account_id, platform, platform_item_id, title, url, publish_time,
                    duration_sec, like_count, comment_count, share_count, collect_count,
                    first_contact_category, discovery_delay_hours, excluded_reason,
                    registration_run_id, raw_archive_ref, raw_json, first_seen_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["video_id"], row["account_id"], row["platform"], row["platform_item_id"],
                    row["title"], row["url"], row["publish_time"], row["duration_sec"],
                    row["like_count"], row["comment_count"], row["share_count"], row["collect_count"],
                    category, discovery_delay_hours, excluded_reason,
                    row["registration_run_id"], row["raw_archive_ref"], row["raw_json"],
                    row["first_seen_at"],
                ),
            )
            if category is not None:
                conn.execute(
                    """
                    INSERT INTO video_checks(check_id, video_id, discovery_batch_index, like_count, comment_count, share_count, collect_count, run_id)
                    VALUES(?, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (stable_check_id(row["video_id"], run_id), row["video_id"], row["like_count"], row["comment_count"], row["share_count"], row["collect_count"], run_id),
                )
                category_counts[category] += 1
            else:
                missing_publish_time += 1
            migrated += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "backup_path": str(backup_path),
        "old_row_count": old_row_count,
        "old_baseline_count": old_baseline_count,
        "old_hit_count": old_hit_count,
        "migrated_video_count": migrated,
        "category_counts": category_counts,
        "missing_publish_time": missing_publish_time,
        "run_id": run_id,
    }


def wipe_video_data(db_path: Path) -> dict[str, Any]:
    """True clean rebuild, step 1: empty competitor_videos/baselines/hits/
    video_checks entirely (competitor_accounts is left alone -- registration is
    idempotent and re-runs safely). Always backs up first. Unlike migrate(),
    this does NOT carry any old row forward -- the only way new rows appear
    afterward is a real crawl through run_full_registration(), so the
    resulting dataset is genuinely produced by the new design's own
    collection process, not inferred from old data.
    """
    resolved = _safe_db_path(db_path)
    backup_path = resolved.with_name(
        resolved.stem + "_pre_true_clean_rebuild_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + resolved.suffix
    )
    shutil.copy2(resolved, backup_path)

    conn = sqlite3.connect(resolved)
    try:
        old_video_count = conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0]
        old_baseline_count = conn.execute("SELECT count(*) FROM baselines").fetchone()[0]
        old_hit_count = conn.execute("SELECT count(*) FROM hits").fetchone()[0]
        for table in ("hits", "baselines", "video_checks", "competitor_videos"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()

    return {
        "backup_path": str(backup_path),
        "wiped_video_count": old_video_count,
        "wiped_baseline_count": old_baseline_count,
        "wiped_hit_count": old_hit_count,
    }


def reset_judgement_state(db_path: Path) -> dict[str, Any]:
    """2026-07-08 user decision: a genuine threshold/rule change (not routine
    day-to-day rejudging of fluctuating data) invalidates hits computed under
    the OLD rule -- BR-HIT-005 permanence protects against noise on an
    unchanged rule, it does not grandfather hits produced by a rule that has
    since been replaced. Clears hits/baselines and resets every candidate field
    on competitor_videos (first_trigger_at/first_trigger_observation/
    trigger_rules/peak_observation/baseline_mode/judgment_confidence back to
    NULL) so the next --rejudge-only computes a clean result purely from the
    newly-locked-in parameters. Raw crawl data (like_count, comment_count,
    publish_time, first_contact_category, tracking_completed, discovery_batch_
    index, video_checks, etc.) is untouched -- no re-crawl needed. Always backs
    up first.
    """
    resolved = _safe_db_path(db_path)
    backup_path = resolved.with_name(
        resolved.stem + "_pre_judgement_reset_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + resolved.suffix
    )
    shutil.copy2(resolved, backup_path)

    conn = sqlite3.connect(resolved)
    try:
        old_hit_count = conn.execute("SELECT count(*) FROM hits").fetchone()[0]
        old_baseline_count = conn.execute("SELECT count(*) FROM baselines").fetchone()[0]
        conn.execute("DELETE FROM hits")
        conn.execute("DELETE FROM baselines")
        conn.execute(
            """
            UPDATE competitor_videos
               SET first_trigger_at=NULL, first_trigger_observation=NULL,
                   trigger_rules=NULL, peak_observation=NULL,
                   baseline_mode=NULL, judgment_confidence=NULL
            """
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "backup_path": str(backup_path),
        "cleared_hit_count": old_hit_count,
        "cleared_baseline_count": old_baseline_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path under data/formal to migrate.")
    parser.add_argument("--confirm", action="store_true", help="Required: acknowledges this rebuilds videos/baselines/hits/video_checks (with a backup taken first).")
    parser.add_argument(
        "--mode", choices=("reclassify", "wipe", "reset_judgement"), default="reclassify",
        help="reclassify: relabel existing raw crawl data into the new schema (no data loss). "
             "wipe: empty video/baseline/hit/check tables entirely so a subsequent real crawl "
             "(run_full_registration) produces a genuinely fresh, natively-classified dataset. "
             "reset_judgement: clear hits/baselines and every candidate field, keeping all raw "
             "crawl data -- for when the hit-detection RULE itself changed (not the data), so a "
             "subsequent --rejudge-only reflects only the new rule, not hits grandfathered from "
             "a since-replaced threshold.",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print("Refusing to run without --confirm. This modifies competitor_videos/baselines/hits/video_checks (a timestamped backup of the whole DB file is taken first, nothing is deleted without one).")
        return 2

    if args.mode == "wipe":
        report = wipe_video_data(Path(args.db))
    elif args.mode == "reset_judgement":
        report = reset_judgement_state(Path(args.db))
    else:
        report = migrate(Path(args.db))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
