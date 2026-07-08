from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import (  # noqa: E402
    DEFAULT_DB,
    install_schema,
    register_from_domain,
    stable_account_id,
)
from scripts.core.business_data.run_reverse_prep import run_reverse_prep  # noqa: E402
from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.external_adapters import ExternalAdapterCommand  # noqa: E402
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor  # noqa: E402


DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
FALLBACK_SETTINGS = ROOT / "config" / "settings.example.yaml"
EXECUTION_GUARDRAIL_DOC = "docs/production_execution_guardrails.md"
# 2026-07-07: the single design authority for everything in this file. See
# BUSINESS_RULE_CATALOG.yaml BR-HIT-001 amendment_2026_07_07_master_doc_realignment
# for the full decision trail -- this module was rewritten from scratch against it,
# not incrementally patched.
MASTER_DESIGN_DOC = "爆款口播内容经验库系统_最终完整执行总控文档_V0.6.2_无损汇编版.md"

# BR-HIT-001 section D: the four metrics every single-metric and multi-indicator
# channel is evaluated over, and the rule name each one fires under.
METRICS = ("like_count", "comment_count", "collect_count", "share_count")
METRIC_RULE_NAME = {
    "like_count": "like_anomaly",
    "comment_count": "comment_anomaly",
    "collect_count": "collect_anomaly",
    "share_count": "share_anomaly",
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run full competitor registration: account pool, stock crawl, baseline, hits.")
    parser.add_argument("--domain-config", default="config/domains/泛科普.yaml")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--max-notes", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--account-limit", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--rejudge-only",
        action="store_true",
        help="Skip registration and crawl; only re-run baseline/hit judgement against existing data under the same execution contract.",
    )
    args = parser.parse_args(argv)

    domain_path = _repo_path(args.domain_config)
    domain = _read_yaml(domain_path)
    settings = _read_yaml(DEFAULT_SETTINGS if DEFAULT_SETTINGS.exists() else FALLBACK_SETTINGS)
    hit_cfg = settings["hit_detection"]
    reverse_cfg = settings["reverse_engine"]
    db_path = _safe_db_path(Path(args.db))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        if args.rejudge_only:
            report = run_rejudge_only(conn, domain, hit_cfg=hit_cfg, reverse_cfg=reverse_cfg, account_limit=args.account_limit)
        else:
            max_notes, max_notes_source = resolve_max_notes(args.max_notes, settings)
            report = run_full_registration(
                conn,
                domain,
                domain_path=domain_path,
                hit_cfg=hit_cfg,
                reverse_cfg=reverse_cfg,
                max_notes=max_notes,
                max_notes_source=max_notes_source,
                account_limit=args.account_limit,
                timeout_seconds=args.timeout_seconds,
            )
    finally:
        conn.close()

    report["database"] = _relative_ref(db_path)
    report_dir = ROOT / "data" / "formal" / "competitor_registration" / report["run_id"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "registration_full_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = _relative_ref(report_path)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0 if report["status"] == "succeeded" else 2


def _auto_reverse_prep(conn: sqlite3.Connection, *, judgement_run_id: str, reverse_cfg: dict[str, Any]) -> dict[str, Any]:
    """Synchronous auto-trigger (2026-07-08, explicit user decision -- see
    run_reverse_prep.py's module docstring): a hit judge_domain() just
    promoted is not yet useful for DNA/experience extraction until it has a
    transcript, so every judgement entry point backfills its own newly
    promoted hits before returning. Scoped to judgement_run_id so this stays
    bounded by what THIS run promoted, not the whole historical backlog."""
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM hits WHERE reverse_status='pending' AND run_id=?", (judgement_run_id,)
    ).fetchone()[0]
    if pending_count == 0:
        return {"status": "succeeded", "attempted": 0, "completed": 0, "failed": 0, "results": []}
    return run_reverse_prep(conn, limit=pending_count, reverse_cfg=reverse_cfg, judgement_run_id=judgement_run_id)


def run_full_registration(
    conn: sqlite3.Connection,
    domain: dict[str, Any],
    *,
    domain_path: Path,
    hit_cfg: dict[str, Any],
    reverse_cfg: dict[str, Any],
    max_notes: int,
    max_notes_source: str,
    account_limit: int | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    execution_contract = validate_registration_execution_contract(domain, hit_cfg)
    run_id = "competitor_registration_full_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    domain_label = domain["formal_domain_label"]
    report_dir = ROOT / "data" / "formal" / "competitor_registration" / run_id

    register_report = register_from_domain(conn, domain, source_config_ref=_relative_ref(domain_path))
    conn.commit()

    accounts = _load_accounts(conn, domain_label, limit=account_limit)
    executor = LocalMediaCrawlerExecutor(archive_root=report_dir / "mediacrawler", timeout_seconds=timeout_seconds)
    crawl_results: list[dict[str, Any]] = []
    for account in accounts:
        result = crawl_registration_stock_once(
            conn,
            executor,
            account,
            max_notes=max_notes,
            hit_cfg=hit_cfg,
            run_id=run_id,
        )
        crawl_results.append({key: value for key, value in result.items() if key != "items"})

    judgement = judge_domain(conn, domain_label, hit_cfg=hit_cfg, run_id=run_id)
    conn.commit()
    reverse_prep = _auto_reverse_prep(conn, judgement_run_id=run_id, reverse_cfg=reverse_cfg)
    summary = summarize(conn, domain_label)

    return {
        "status": "succeeded" if all(item["status"] == "succeeded" for item in crawl_results) else "partial_failed",
        "mode": "full",
        "run_id": run_id,
        "domain": domain["name"],
        "domain_label": domain_label,
        "platform": domain["platform"],
        "policy": {
            "execution_guardrail": EXECUTION_GUARDRAIL_DOC,
            "design_authority": MASTER_DESIGN_DOC,
            "first_crawl": domain["collector_policy"]["first_crawl"],
            "comments_collected": False,
            "llm_used": False,
            "feishu_sent": False,
            "contract": execution_contract,
        },
        "registration": register_report,
        "crawl": {
            "requested_accounts": len(accounts),
            "max_notes_per_account": max_notes,
            "max_notes_source": max_notes_source,
            "results": crawl_results,
        },
        "reverse_prep": reverse_prep,
        "judgement": judgement,
        "summary": summary,
    }


def run_rejudge_only(
    conn: sqlite3.Connection,
    domain: dict[str, Any],
    *,
    hit_cfg: dict[str, Any],
    reverse_cfg: dict[str, Any],
    account_limit: int | None = None,
) -> dict[str, Any]:
    """Re-run baseline/hit judgement against already-collected data only.

    Does not register accounts and does not crawl. Still runs the same
    validate_registration_execution_contract() gate as the full run, so a
    rejudge can never bypass the design contract the way an ad-hoc script
    invocation could.
    """
    execution_contract = validate_registration_execution_contract(domain, hit_cfg)
    run_id = "competitor_registration_rejudge_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    domain_label = domain["formal_domain_label"]

    accounts = _load_accounts(conn, domain_label, limit=account_limit)
    judgement = judge_domain(conn, domain_label, hit_cfg=hit_cfg, run_id=run_id)
    conn.commit()
    reverse_prep = _auto_reverse_prep(conn, judgement_run_id=run_id, reverse_cfg=reverse_cfg)
    summary = summarize(conn, domain_label)

    return {
        "status": "succeeded",
        "mode": "rejudge_only",
        "run_id": run_id,
        "domain": domain["name"],
        "domain_label": domain_label,
        "platform": domain["platform"],
        "policy": {
            "execution_guardrail": EXECUTION_GUARDRAIL_DOC,
            "design_authority": MASTER_DESIGN_DOC,
            "contract": execution_contract,
        },
        "registration": None,
        "crawl": {"requested_accounts": len(accounts), "results": []},
        "judgement": judgement,
        "reverse_prep": reverse_prep,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# BR-HIT-001 section B: three-category first-contact classification
# ---------------------------------------------------------------------------


def classify_first_contact_category(published_at: datetime | None, now: datetime, observe_days: int) -> str | None:
    """First-registration classification only -- never returns 'formal_new'.

    'formal_new' only applies to videos discovered via the ongoing daily
    batch after an account is under formal tracking (see
    ingest_daily_incremental_items), not at first registration. Returns None
    when published_at is unusable; the caller records
    excluded_reason='missing_publish_time' in that case rather than guessing
    a category.
    """
    if published_at is None:
        return None
    if published_at <= now - timedelta(days=observe_days):
        return "historical_mature"
    return "transition"


def ingest_stock_items(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    items: list[dict[str, Any]],
    *,
    hit_cfg: dict[str, Any],
    run_id: str,
    raw_archive_ref: str | None,
) -> tuple[int, int]:
    """First-crawl ingestion (BR-HIT-001 section B).

    Every item is classified into historical_mature or transition based on
    its age at first sight. One cumulative snapshot is taken now for both
    categories; transition videos get a second "matured" snapshot later once
    they turn 7 days published (see _mature_transition_samples, called from
    judge_account). No D-series is fabricated for either category -- that is
    reserved for formal_new videos discovered via the ongoing daily batch.
    """
    now = datetime.now(timezone.utc)
    observe_days = int(hit_cfg["observe_days"])
    inserted = 0
    updated = 0
    for item in items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_id = stable_video_id(account["account_id"], platform_item_id)
        published_at = _item_publish_datetime(item)
        category = classify_first_contact_category(published_at, now, observe_days)
        excluded_reason = None if category is not None else "missing_publish_time"
        discovery_delay_hours = (
            round((now - published_at).total_seconds() / 3600, 2) if published_at is not None else None
        )
        like_count = _optional_int(item.get("liked_count") or item.get("like_count"))
        comment_count = _optional_int(item.get("comment_count"))
        share_count = _optional_int(item.get("share_count"))
        collect_count = _optional_int(item.get("collected_count") or item.get("collect_count"))
        existed = conn.execute(
            "SELECT video_id FROM competitor_videos WHERE account_id=? AND platform_item_id=?",
            (account["account_id"], platform_item_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO competitor_videos(
                video_id, account_id, platform, platform_item_id, title, url, publish_time,
                duration_sec, like_count, comment_count, share_count, collect_count,
                first_contact_category, discovery_delay_hours, excluded_reason,
                registration_run_id, raw_archive_ref, raw_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, platform_item_id) DO UPDATE SET
                title=excluded.title,
                url=excluded.url,
                publish_time=excluded.publish_time,
                duration_sec=excluded.duration_sec,
                registration_run_id=excluded.registration_run_id,
                raw_archive_ref=excluded.raw_archive_ref,
                raw_json=excluded.raw_json,
                last_checked_at=CURRENT_TIMESTAMP,
                check_count=competitor_videos.check_count + 1
            """,
            (
                video_id,
                account["account_id"],
                account["platform"],
                platform_item_id,
                str(item.get("title") or item.get("desc") or ""),
                str(item.get("aweme_url") or item.get("url") or f"https://www.douyin.com/video/{platform_item_id}"),
                _timestamp_to_iso(item.get("create_time")),
                _optional_int(item.get("duration_sec")),
                like_count,
                comment_count,
                share_count,
                collect_count,
                category,
                discovery_delay_hours,
                excluded_reason,
                run_id,
                raw_archive_ref,
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )
        if not existed:
            inserted += 1
            if category is not None:
                conn.execute(
                    """
                    INSERT INTO video_checks(check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, share_count, collect_count, run_id)
                    VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(check_id) DO NOTHING
                    """,
                    (
                        stable_check_id(video_id, run_id), video_id, _day_since_publish(published_at, now),
                        like_count, comment_count, share_count, collect_count, run_id,
                    ),
                )
        else:
            updated += 1
    return inserted, updated


def _day_since_publish(published_at: datetime | None, now: datetime) -> int | None:
    """Calendar days since publish_time -- deliberately distinct from
    discovery_batch_index (discovery-batch-anchored, for formal_new only).
    Labels historical_mature/transition video_checks rows with which day of
    the video's own 0-7 lifecycle a given observation happened on.
    """
    if published_at is None:
        return None
    return (now - published_at).days


def ingest_daily_incremental_items(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    items: list[dict[str, Any]],
    *,
    hit_cfg: dict[str, Any],
    run_id: str,
    raw_archive_ref: str | None,
) -> dict[str, Any]:
    """Process one day's fetched batch for one account (BR-HIT-001 sections A/B).

    Reconciles against videos already known to this account first: a
    transition video that has now turned 7 days published gets its second
    "matured" snapshot; a formal_new video that is not yet tracking_completed
    gets its next D-point snapshot (discovery-anchored: the Nth time this
    video has ever been checked, not a calendar-day count since publish).
    Anything left over is a brand new discovery -- classified as
    first_contact_category='formal_new' regardless of its actual publish
    age (BR-HIT-001 section B: 'formal_new' means discovered via the ongoing
    daily batch, not "recently published"), with D0 recorded now.
    """
    now = datetime.now(timezone.utc)

    known_rows = {
        row["platform_item_id"]: row
        for row in conn.execute(
            "SELECT * FROM competitor_videos WHERE account_id=?",
            (account["account_id"],),
        ).fetchall()
    }

    existing_items: list[dict[str, Any]] = []
    new_items: list[dict[str, Any]] = []
    for item in items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        (existing_items if platform_item_id in known_rows else new_items).append(item)

    updated = 0
    checks_recorded = 0
    matured = 0
    d_points_recorded = 0

    for item in existing_items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_row = known_rows[platform_item_id]
        video_id = video_row["video_id"]
        like_count = _optional_int(item.get("liked_count") or item.get("like_count"))
        comment_count = _optional_int(item.get("comment_count"))
        share_count = _optional_int(item.get("share_count"))
        collect_count = _optional_int(item.get("collected_count") or item.get("collect_count"))

        if video_row["first_contact_category"] == "transition" and video_row["mature_snapshot_taken_at"] is None:
            # 2026-07-08 user decision: to avoid the complexity of an age gate,
            # ALL videos participate in judgement every day -- a transition video
            # gets its numbers refreshed and a video_checks row recorded on EVERY
            # daily pass while still observing (not just once at day 7, which
            # previously left it frozen at its first-contact snapshot for up to 6
            # days), matching the daily cadence formal_new videos already get.
            # mature_snapshot_taken_at is still only set once it actually reaches
            # day 7 -- that freeze point (and what feeds the mature_history
            # baseline) is unchanged.
            published_at = _parse_datetime(video_row["publish_time"])
            observe_days = int(hit_cfg["observe_days"])
            has_matured = published_at is not None and published_at <= now - timedelta(days=observe_days)
            conn.execute(
                """
                UPDATE competitor_videos
                   SET like_count=?, comment_count=?, share_count=?, collect_count=?,
                       mature_snapshot_taken_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE mature_snapshot_taken_at END,
                       last_checked_at=CURRENT_TIMESTAMP, check_count=check_count + 1
                 WHERE video_id=?
                """,
                (like_count, comment_count, share_count, collect_count, 1 if has_matured else 0, video_id),
            )
            conn.execute(
                """
                INSERT INTO video_checks(check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, share_count, collect_count, run_id)
                VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(check_id) DO NOTHING
                """,
                (
                    stable_check_id(video_id, run_id), video_id, _day_since_publish(published_at, now),
                    like_count, comment_count, share_count, collect_count, run_id,
                ),
            )
            checks_recorded += 1
            if has_matured:
                matured += 1
            updated += 1
            continue

        if video_row["first_contact_category"] == "formal_new" and not video_row["tracking_completed"]:
            next_index = _next_discovery_batch_index(conn, video_id)
            tracking_completed = 1 if next_index >= 7 else 0
            conn.execute(
                """
                UPDATE competitor_videos
                   SET like_count=?, comment_count=?, share_count=?, collect_count=?,
                       tracking_completed=?, last_checked_at=CURRENT_TIMESTAMP, check_count=check_count + 1
                 WHERE video_id=?
                """,
                (like_count, comment_count, share_count, collect_count, tracking_completed, video_id),
            )
            conn.execute(
                """
                INSERT INTO video_checks(check_id, video_id, discovery_batch_index, like_count, comment_count, share_count, collect_count, run_id)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(check_id) DO NOTHING
                """,
                (stable_check_id(video_id, run_id), video_id, next_index, like_count, comment_count, share_count, collect_count, run_id),
            )
            d_points_recorded += 1
            updated += 1
            continue

        # historical_mature (or an already-matured transition / already-tracking_completed
        # formal_new): the one-time snapshot(s) are frozen by design, daily incremental
        # does not keep refreshing them.
        updated += 1

    inserted = 0
    for item in new_items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_id = stable_video_id(account["account_id"], platform_item_id)
        published_at = _item_publish_datetime(item)
        discovery_delay_hours = (
            round((now - published_at).total_seconds() / 3600, 2) if published_at is not None else None
        )
        like_count = _optional_int(item.get("liked_count") or item.get("like_count"))
        comment_count = _optional_int(item.get("comment_count"))
        share_count = _optional_int(item.get("share_count"))
        collect_count = _optional_int(item.get("collected_count") or item.get("collect_count"))
        excluded_reason = None if published_at is not None else "missing_publish_time"

        conn.execute(
            """
            INSERT INTO competitor_videos(
                video_id, account_id, platform, platform_item_id, title, url, publish_time,
                duration_sec, like_count, comment_count, share_count, collect_count,
                first_contact_category, discovery_delay_hours, excluded_reason,
                registration_run_id, raw_archive_ref, raw_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'formal_new', ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                account["account_id"],
                account["platform"],
                platform_item_id,
                str(item.get("title") or item.get("desc") or ""),
                str(item.get("aweme_url") or item.get("url") or f"https://www.douyin.com/video/{platform_item_id}"),
                _timestamp_to_iso(item.get("create_time")),
                _optional_int(item.get("duration_sec")),
                like_count,
                comment_count,
                share_count,
                collect_count,
                discovery_delay_hours,
                excluded_reason,
                run_id,
                raw_archive_ref,
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT INTO video_checks(check_id, video_id, discovery_batch_index, like_count, comment_count, share_count, collect_count, run_id)
            VALUES(?, ?, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO NOTHING
            """,
            (stable_check_id(video_id, run_id), video_id, like_count, comment_count, share_count, collect_count, run_id),
        )
        inserted += 1
        checks_recorded += 1
        d_points_recorded += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "checks_recorded": checks_recorded,
        "transition_matured": matured,
        "d_points_recorded": d_points_recorded,
    }


def _next_discovery_batch_index(conn: sqlite3.Connection, video_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(discovery_batch_index) AS max_index FROM video_checks WHERE video_id=? AND discovery_batch_index IS NOT NULL",
        (video_id,),
    ).fetchone()
    current = row["max_index"]
    return 0 if current is None else int(current) + 1


def crawl_registration_stock_once(
    conn: sqlite3.Connection,
    executor: LocalMediaCrawlerExecutor,
    account: sqlite3.Row,
    *,
    max_notes: int,
    hit_cfg: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    result = _crawl_account(executor, account, max_notes=max_notes, run_id=run_id)
    if result["status"] != "succeeded":
        result["inserted_videos"] = 0
        result["updated_videos"] = 0
        result["mature_history_sample_count"] = 0
        return result
    inserted, updated = ingest_stock_items(
        conn, account, result["items"], hit_cfg=hit_cfg, run_id=run_id, raw_archive_ref=result["raw_archive_ref"]
    )
    conn.commit()
    result = dict(result)
    result["inserted_videos"] = inserted
    result["updated_videos"] = updated
    result["mature_history_sample_count"] = len(
        _mature_history_pool(conn, account["account_id"], hit_cfg, datetime.now(timezone.utc))[0]
    )
    result.pop("items", None)
    return result


def crawl_daily_incremental_once(
    conn: sqlite3.Connection,
    executor: LocalMediaCrawlerExecutor,
    account: sqlite3.Row,
    *,
    max_notes: int,
    hit_cfg: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    result = _crawl_account(executor, account, max_notes=max_notes, run_id=run_id)
    if result["status"] != "succeeded":
        result["inserted"] = 0
        result["updated"] = 0
        result["checks_recorded"] = 0
        return result
    ingest_result = ingest_daily_incremental_items(
        conn, account, result["items"], hit_cfg=hit_cfg, run_id=run_id, raw_archive_ref=result["raw_archive_ref"]
    )
    conn.commit()
    result = dict(result)
    result.update(ingest_result)
    result.pop("items", None)
    return result


def run_daily_incremental(
    conn: sqlite3.Connection,
    domain: dict[str, Any],
    *,
    hit_cfg: dict[str, Any],
    crawler_cfg: dict[str, Any],
    reverse_cfg: dict[str, Any],
    account_limit: int | None = None,
    executor: LocalMediaCrawlerExecutor | None = None,
) -> dict[str, Any]:
    """One daily invocation: discover new videos, advance every formal_new
    video's D-series, mature any transition videos that have turned 7 days
    published, and judge -- all under the same execution-contract gate as
    run_full_registration/run_rejudge_only.
    """
    execution_contract = validate_registration_execution_contract(domain, hit_cfg)
    daily_max_notes = crawler_cfg.get("daily_max_notes")
    if not isinstance(daily_max_notes, int) or daily_max_notes < 1:
        raise ValueError("crawler.daily_max_notes must be a positive integer")

    run_id = "competitor_daily_incremental_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    domain_label = domain["formal_domain_label"]
    accounts = _load_accounts(conn, domain_label, limit=account_limit)
    executor = executor or LocalMediaCrawlerExecutor()

    crawl_results: list[dict[str, Any]] = []
    for account in accounts:
        result = crawl_daily_incremental_once(
            conn, executor, account, max_notes=daily_max_notes, hit_cfg=hit_cfg, run_id=run_id,
        )
        crawl_results.append(result)

    judgement = judge_domain(conn, domain_label, hit_cfg=hit_cfg, run_id=run_id)
    conn.commit()
    reverse_prep = _auto_reverse_prep(conn, judgement_run_id=run_id, reverse_cfg=reverse_cfg)
    summary = summarize(conn, domain_label)

    return {
        "status": "succeeded" if all(item["status"] == "succeeded" for item in crawl_results) else "partial_failed",
        "mode": "daily_incremental",
        "run_id": run_id,
        "domain": domain["name"],
        "domain_label": domain_label,
        "platform": domain["platform"],
        "policy": {
            "execution_guardrail": EXECUTION_GUARDRAIL_DOC,
            "design_authority": MASTER_DESIGN_DOC,
            "comments_collected": False,
            "llm_used": False,
            "contract": execution_contract,
        },
        "crawl": {
            "requested_accounts": len(accounts),
            "daily_max_notes": daily_max_notes,
            "results": crawl_results,
        },
        "judgement": judgement,
        "reverse_prep": reverse_prep,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# BR-HIT-001 section C: four baselines
# ---------------------------------------------------------------------------


def _mature_history_pool(
    conn: sqlite3.Connection, account_id: str, hit_cfg: dict[str, Any], now: datetime
) -> tuple[list[sqlite3.Row], bool]:
    """Rolling pool: this account's historical_mature videos, plus matured
    transition videos, published within the trailing window, capped at the
    configured max -- re-evaluated fresh each time, not a stored/maintained set.
    Returns (pool, used_backfill).

    2026-07-08 user decision: reinstates a narrower version of the master
    document's retired legacy_supplement/backfill-outside-the-90-day-window
    behavior (see BR-BASELINE-003's status note on why it was originally
    dropped). Scoped ONLY to accounts that are both high-magnitude and have
    enough lifetime content for backfilling to be meaningful -- a slow-posting
    account whose typical numbers are small stays exactly as underpowered as
    before (no backfill). A slow-posting account whose lifetime content is
    clearly high-magnitude, and which has at least unified_min_samples videos
    across its FULL history (not just the 90-day window), gets its pool
    topped up with older videos from beyond the window, capped at
    mature_history_max_samples -- the same real-world case that surfaced this
    (an account with 55 lifetime videos, high six-figure like counts, but only
    17 within the last 90 days, which could never otherwise get a baseline).
    """
    window_start = now - timedelta(days=int(hit_cfg["mature_history_window_days"]))
    cap = int(hit_cfg["mature_history_max_samples"])
    unified_min_samples = int(hit_cfg["unified_min_samples"])

    query = """
        SELECT * FROM competitor_videos
         WHERE account_id=?
           AND publish_time IS NOT NULL
           {window_clause}
           AND (
                first_contact_category = 'historical_mature'
                OR (first_contact_category = 'transition' AND mature_snapshot_taken_at IS NOT NULL)
           )
         ORDER BY publish_time DESC
    """
    in_window_rows = conn.execute(
        query.format(window_clause="AND publish_time >= ?"), (account_id, window_start.isoformat())
    ).fetchall()
    in_window_pool = in_window_rows[:cap]
    if len(in_window_pool) >= unified_min_samples:
        return in_window_pool, False

    full_rows = conn.execute(query.format(window_clause=""), (account_id,)).fetchall()
    full_pool = full_rows[:cap]
    if len(full_pool) < unified_min_samples:
        return in_window_pool, False

    like_values = [row["like_count"] for row in full_pool if row["like_count"] is not None]
    if not like_values:
        return in_window_pool, False
    full_like_median = statistics.median(like_values)
    single_threshold = float(hit_cfg["cold_start_d7_single_metric_threshold"])
    absolute_floor = float(hit_cfg["mature_history_absolute_like_floor"])
    if full_like_median * single_threshold < absolute_floor:
        return in_window_pool, False

    return full_pool, True


def _mature_history_medians(pool: list[sqlite3.Row], unified_min_samples: int) -> dict[str, float]:
    if len(pool) < unified_min_samples:
        return {}
    medians: dict[str, float] = {}
    for metric in METRICS:
        values = [row[metric] for row in pool if row[metric] is not None]
        if values:
            medians[metric] = float(statistics.median(values))
    return medians


def _formal_d_predecessor_pool(conn: sqlite3.Connection, account_id: str, hit_cfg: dict[str, Any]) -> list[sqlite3.Row]:
    """All formal_new videos eligible to be someone else's baseline predecessor:
    tracking_completed (full D0-D7) AND within the discovery-delay gate.
    """
    delay_max = float(hit_cfg["discovery_delay_hours_max"])
    rows = conn.execute(
        """
        SELECT * FROM competitor_videos
         WHERE account_id=?
           AND first_contact_category='formal_new'
           AND tracking_completed=1
           AND discovery_delay_hours IS NOT NULL
           AND discovery_delay_hours <= ?
         ORDER BY publish_time DESC
        """,
        (account_id, delay_max),
    ).fetchall()
    return rows


def _formal_d_baseline_medians(
    conn: sqlite3.Connection, predecessor_pool: list[sqlite3.Row], observation_point: str, window: int
) -> dict[str, float]:
    """Median per metric, at one D-point, over the most recent `window`
    predecessor videos (BR-HIT-001 section C(3): the ongoing computation
    window is corrected to align with mature_history_max_samples, not fixed
    at the activation-gate size of 20).
    """
    candidates = predecessor_pool[:window]
    if not candidates:
        return {}
    video_ids = [row["video_id"] for row in candidates]
    placeholders = ",".join("?" for _ in video_ids)
    checks = conn.execute(
        f"""
        SELECT video_id, like_count, comment_count, share_count, collect_count
          FROM video_checks
         WHERE video_id IN ({placeholders}) AND discovery_batch_index = ?
        """,
        (*video_ids, int(observation_point[1:])),
    ).fetchall()
    medians: dict[str, float] = {}
    for metric in METRICS:
        values = [row[metric] for row in checks if row[metric] is not None]
        if len(values) >= 1:
            medians[metric] = float(statistics.median(values))
    return medians


# ---------------------------------------------------------------------------
# BR-HIT-001 section D: trigger channels
# ---------------------------------------------------------------------------


def _evaluate_magnitude_channels(
    current: dict[str, int | None], median: dict[str, float], single_threshold: float, multi_threshold: float
) -> list[str]:
    """formal_d_series channel only. Every entry states the actual multiple
    achieved (e.g. 'like_anomaly:3.24x'), not just the bare channel name, so a
    reader can see exactly why a video qualified without cross-referencing the
    baseline separately.
    """
    fired: list[str] = []
    cleared_multi: list[str] = []
    for metric in METRICS:
        value = current.get(metric)
        med = median.get(metric)
        if value is None or med is None or med <= 0:
            continue
        ratio = value / med
        if ratio >= single_threshold:
            fired.append(f"{METRIC_RULE_NAME[metric]}:{ratio:.2f}x")
        if ratio >= multi_threshold:
            cleared_multi.append(f"{METRIC_RULE_NAME[metric]}={ratio:.2f}x")
    if len(cleared_multi) >= 2:
        fired.append("multi_indicator:" + ";".join(cleared_multi))
    return fired


def _percentile(values: list[int], pct: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return float(ordered[index])


def _account_small_status(
    mature_pool: list[sqlite3.Row],
    mature_medians: dict[str, float],
    single_threshold: float,
    absolute_floor: float,
    p90_percentile: float,
) -> tuple[bool, float | None]:
    """2026-07-08 user decision: an account is 'small' for the mature-history
    channel when even its best-case comparison (median * single_threshold)
    could never reach the absolute like floor -- the fixed floor would lock it
    out forever, so it gets an alternate bar: its own like_count distribution's
    P90, instead of the normal ratio+floor path.
    """
    like_median = mature_medians.get("like_count")
    if like_median is None or like_median * single_threshold >= absolute_floor:
        return False, None
    like_values = [row["like_count"] for row in mature_pool if row["like_count"] is not None]
    if not like_values:
        return True, None
    return True, _percentile(like_values, p90_percentile)


def _evaluate_mature_history_channel(
    current: dict[str, int | None],
    medians: dict[str, float],
    single_threshold: float,
    multi_threshold: float,
    absolute_floor: float,
    is_small_account: bool,
    account_like_p90: float | None,
) -> list[str]:
    """The mature-history/rough channel (BR-HIT-001 section C(2)): used for
    historical_mature/transition videos (the only magnitude-based channel
    available at first registration, since those never get a D-series) and for
    a formal_new video at D7 while the formal D baseline is not yet active.
    Small accounts (see _account_small_status) use their own P90 as the bar
    instead of the normal ratio+absolute-floor path -- always labelled
    p90_small_account so it is never confused with the normal path's evidence.
    """
    like_count = current.get("like_count")
    if is_small_account:
        if like_count is not None and account_like_p90 is not None and like_count >= account_like_p90:
            return [f"p90_small_account:like={like_count}>=p90:{account_like_p90:.0f}"]
        return []
    if like_count is None or like_count < absolute_floor:
        return []
    fired: list[str] = []
    cleared_multi: list[str] = []
    for metric in METRICS:
        value = current.get(metric)
        med = medians.get(metric)
        if value is None or med is None or med <= 0:
            continue
        ratio = value / med
        if ratio >= single_threshold:
            fired.append(f"{METRIC_RULE_NAME[metric]}:{ratio:.2f}x")
        if ratio >= multi_threshold:
            cleared_multi.append(f"{METRIC_RULE_NAME[metric]}={ratio:.2f}x")
    if len(cleared_multi) >= 2:
        fired.append("multi_indicator:" + ";".join(cleared_multi))
    return fired


def _comment_like_ratio_label(like_count: int | None, comment_count: int | None, threshold: float) -> str | None:
    if not like_count or comment_count is None:
        return None
    ratio = comment_count / like_count
    if ratio >= threshold:
        return f"comment_like_ratio:{ratio:.3f}"
    return None


# ---------------------------------------------------------------------------
# Judgement
# ---------------------------------------------------------------------------


def judge_domain(conn: sqlite3.Connection, domain_label: str, *, hit_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
    rows = _load_accounts(conn, domain_label, limit=None)
    results: list[dict[str, Any]] = []
    total_promoted = 0
    total_formal = 0
    total_rough = 0
    total_accounts_using_backfill = 0
    for account in rows:
        account_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id=run_id)
        total_promoted += account_result["promoted_count"]
        total_formal += account_result["formal_hit_count"]
        total_rough += account_result["rough_hit_count"]
        if account_result["mature_history_used_backfill"]:
            total_accounts_using_backfill += 1
        results.append(account_result)
    return {
        "total_promoted": total_promoted,
        "total_formal_hits": total_formal,
        "total_rough_hits": total_rough,
        "total_accounts_using_mature_history_backfill": total_accounts_using_backfill,
        "accounts": results,
    }


def judge_account(conn: sqlite3.Connection, account: sqlite3.Row, *, hit_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    account_id = account["account_id"]
    unified_min_samples = int(hit_cfg["unified_min_samples"])
    single_threshold = float(hit_cfg["single_metric_excess_threshold"])
    multi_threshold = float(hit_cfg["multi_indicator_excess_threshold"])
    cold_start_single_threshold = float(hit_cfg["cold_start_d7_single_metric_threshold"])
    cold_start_multi_threshold = float(hit_cfg["cold_start_d7_multi_indicator_threshold"])
    mature_history_absolute_like_floor = float(hit_cfg["mature_history_absolute_like_floor"])
    small_account_p90_percentile = float(hit_cfg["small_account_p90_percentile"])
    comment_like_ratio_threshold = float(hit_cfg["comment_like_ratio_threshold"])
    activation_min_samples = int(hit_cfg["formal_baseline_activation_min_samples"])
    computation_window = int(hit_cfg["formal_baseline_computation_window"])

    # --- (1) mature history baseline ---
    mature_pool, mature_history_used_backfill = _mature_history_pool(conn, account_id, hit_cfg, now)
    mature_medians = _mature_history_medians(mature_pool, unified_min_samples)
    if mature_medians:
        baseline_id_mature = stable_baseline_id(account_id, run_id + ":mature_history")
        for metric, value in mature_medians.items():
            sample_count = len([row for row in mature_pool if row[metric] is not None])
            _insert_baseline(
                conn, baseline_id_mature + ":" + metric, account_id, "mature_history", metric, None, sample_count, value, run_id
            )
    is_small_account, account_like_p90 = _account_small_status(
        mature_pool, mature_medians, cold_start_single_threshold, mature_history_absolute_like_floor, small_account_p90_percentile
    )

    # --- (2) formal D baseline activation ---
    predecessor_pool = _formal_d_predecessor_pool(conn, account_id, hit_cfg)
    formal_d_active = len(predecessor_pool) >= activation_min_samples

    promoted = 0
    formal_hits = 0
    rough_hits = 0

    formal_new_rows = conn.execute(
        "SELECT * FROM competitor_videos WHERE account_id=? AND first_contact_category='formal_new'",
        (account_id,),
    ).fetchall()

    for video_row in formal_new_rows:
        latest_check = conn.execute(
            """
            SELECT * FROM video_checks
             WHERE video_id=? AND discovery_batch_index IS NOT NULL
             ORDER BY discovery_batch_index DESC LIMIT 1
            """,
            (video_row["video_id"],),
        ).fetchone()
        if latest_check is None:
            continue
        observation_point = f"D{latest_check['discovery_batch_index']}"
        current = {metric: latest_check[metric] for metric in METRICS}
        fired: list[str] = []
        baseline_mode: str | None = None
        judgment_confidence: str | None = None
        baseline_id_for_hit: str | None = None

        if formal_d_active:
            eligible_predecessors = [row for row in predecessor_pool if row["video_id"] != video_row["video_id"]]
            d_medians = _formal_d_baseline_medians(conn, eligible_predecessors, observation_point, computation_window)
            if d_medians:
                fired = _evaluate_magnitude_channels(current, d_medians, single_threshold, multi_threshold)
                if fired:
                    baseline_mode = "formal_d_series"
                    judgment_confidence = "formal"
                    baseline_id_for_hit = stable_baseline_id(account_id, run_id + ":formal_d:" + observation_point)
                    for metric, value in d_medians.items():
                        _insert_baseline(
                            conn, baseline_id_for_hit + ":" + metric, account_id, "formal_d_series", metric,
                            observation_point, len(eligible_predecessors), value, run_id,
                        )
        elif observation_point == "D7" and mature_medians:
            # BR-HIT-001 section C(2) / master doc chapter 20.2 "存量高信号": this
            # IS a real hit -- the document explicitly allows it to be called
            # "历史爆款" -- just tagged judgment_confidence=rough (a different
            # evidence caliber from formal_d_series, not "not a hit").
            cold_start_fired = _evaluate_mature_history_channel(
                current, mature_medians, cold_start_single_threshold, cold_start_multi_threshold,
                mature_history_absolute_like_floor, is_small_account, account_like_p90,
            )
            if cold_start_fired:
                fired = cold_start_fired
                baseline_mode = "mature_history"
                judgment_confidence = "rough"

        ratio_label = _comment_like_ratio_label(video_row["like_count"], video_row["comment_count"], comment_like_ratio_threshold)
        if ratio_label:
            fired = list(fired) + [ratio_label]
            judgment_confidence = "formal"

        if fired:
            was_new = _record_trigger(
                conn, account, video_row, observation_point, fired,
                baseline_mode=baseline_mode, judgment_confidence=judgment_confidence or "formal",
                baseline_id=baseline_id_for_hit, run_id=run_id,
            )
            if was_new:
                promoted += 1
                # 2026-07-08 bug fix: count by which channel(s) actually fired, not
                # by judgment_confidence -- that field gets overwritten to "formal"
                # whenever comment_like_ratio ALSO fires on a video that already
                # cleared the mature_history/formal_d_series channel, which was
                # silently hiding those videos from rough_hits (baseline_mode itself
                # is never overwritten, so it is the reliable signal here). A video
                # that fires two channels increments both counters -- these are
                # per-channel counts, not a partition, so rough+formal can exceed
                # promoted_count.
                if baseline_mode == "mature_history":
                    rough_hits += 1
                if baseline_mode == "formal_d_series" or ratio_label:
                    formal_hits += 1

    # --- historical_mature / transition videos: they never have a D-series, so
    # neither ever uses the formal D baseline. Two independent checks:
    # (1) comment_like_ratio -- needs no baseline, always formal-confidence.
    # (2) BR-HIT-001 section C(2) / master doc chapter 20.2 "存量高信号": this
    # video's own numbers vs the mature_history baseline -- a real
    # history_high_signal hit (informally "历史爆款" per the document), tagged
    # judgment_confidence=rough to keep its evidence caliber distinct from
    # formal_d_series hits, per the document's explicit instruction not to mix
    # the two calibers -- not excluded from the hits table.
    other_rows = conn.execute(
        """
        SELECT * FROM competitor_videos
         WHERE account_id=? AND first_contact_category IN ('historical_mature', 'transition')
        """,
        (account_id,),
    ).fetchall()
    # Bug fix 2026-07-08: mature_medians is computed from mature_pool (the
    # trailing 90-day/50-cap rolling window). A video outside that window (too
    # old, or beyond the 50-cap) is NOT part of the baseline that would judge
    # it -- checking it against a baseline it does not belong to inflates the
    # hit count with comparisons that were never actually apples-to-apples.
    # Only videos that are themselves members of mature_pool are eligible for
    # the mature-history magnitude channel; comment_like_ratio still applies to
    # every historical_mature/transition video regardless, since it needs no
    # baseline at all.
    mature_pool_video_ids = {row["video_id"] for row in mature_pool}
    for video_row in other_rows:
        observation_point = video_row["first_contact_category"]
        fired: list[str] = []
        baseline_mode: str | None = None
        judgment_confidence: str | None = None

        if mature_medians and video_row["video_id"] in mature_pool_video_ids:
            current = {metric: video_row[metric] for metric in METRICS}
            history_fired = _evaluate_mature_history_channel(
                current, mature_medians, cold_start_single_threshold, cold_start_multi_threshold,
                mature_history_absolute_like_floor, is_small_account, account_like_p90,
            )
            if history_fired:
                fired = history_fired
                baseline_mode = "mature_history"
                judgment_confidence = "rough"

        ratio_label = _comment_like_ratio_label(video_row["like_count"], video_row["comment_count"], comment_like_ratio_threshold)
        if ratio_label:
            fired = list(fired) + [ratio_label]
            judgment_confidence = "formal"

        if fired:
            was_new = _record_trigger(
                conn, account, video_row, observation_point, fired,
                baseline_mode=baseline_mode, judgment_confidence=judgment_confidence or "formal",
                baseline_id=None, run_id=run_id,
            )
            if was_new:
                promoted += 1
                # See the matching comment in the formal_new_rows loop above --
                # count by which channel(s) actually fired, not by the
                # possibly-overwritten judgment_confidence.
                if baseline_mode == "mature_history":
                    rough_hits += 1
                if baseline_mode == "formal_d_series" or ratio_label:
                    formal_hits += 1

    return {
        "account": account["account_name"],
        "mature_history_sample_count": len(mature_pool),
        "mature_history_used_backfill": mature_history_used_backfill,
        "formal_d_baseline_active": formal_d_active,
        "formal_d_predecessor_count": len(predecessor_pool),
        "promoted_count": promoted,
        "formal_hit_count": formal_hits,
        "rough_hit_count": rough_hits,
        "status": "judged",
    }


def _record_trigger(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    video_row: sqlite3.Row,
    observation_point: str,
    fired_rules: list[str],
    *,
    baseline_mode: str | None,
    judgment_confidence: str,
    baseline_id: str | None,
    run_id: str,
) -> bool:
    """BR-HIT-005: permanent candidate record. Returns True only the first
    time this video ever triggers (the moment a hits row is created) -- every
    later call for the same video only appends to trigger_rules, it never
    creates a second hits row and never touches first_trigger_at/
    first_trigger_observation again.
    """
    existing_trigger_rules = json.loads(video_row["trigger_rules"]) if video_row["trigger_rules"] else []
    new_entries = [f"{observation_point}:{rule}" for rule in fired_rules]
    combined = existing_trigger_rules + [entry for entry in new_entries if entry not in existing_trigger_rules]

    is_first_trigger = video_row["first_trigger_at"] is None
    if is_first_trigger:
        conn.execute(
            """
            UPDATE competitor_videos
               SET first_trigger_observation=?, first_trigger_at=CURRENT_TIMESTAMP,
                   trigger_rules=?, peak_observation=?, baseline_mode=?, judgment_confidence=?
             WHERE video_id=?
            """,
            (
                observation_point, json.dumps(combined, ensure_ascii=False), observation_point,
                baseline_mode, judgment_confidence, video_row["video_id"],
            ),
        )
        hit_id = stable_hit_id(account["account_id"], video_row["platform_item_id"])
        conn.execute(
            """
            INSERT INTO hits(
                hit_id, video_id, account_id, platform, platform_item_id, title, url,
                publish_time, like_count, comment_count, share_count, collect_count,
                hit_channel, judgment_confidence, baseline_id, run_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, platform_item_id) DO NOTHING
            """,
            (
                hit_id, video_row["video_id"], account["account_id"], video_row["platform"], video_row["platform_item_id"],
                video_row["title"], video_row["url"], video_row["publish_time"],
                video_row["like_count"], video_row["comment_count"], video_row["share_count"], video_row["collect_count"],
                ",".join(fired_rules), judgment_confidence, baseline_id, run_id,
            ),
        )
        return True

    # BR-HIT-005 permanence: never retract, never overwrite first_trigger_at/
    # first_trigger_observation/the existing hits row -- only append newly
    # fired rules to the cumulative trigger_rules list, and advance
    # peak_observation to track the most recent trigger point.
    conn.execute(
        "UPDATE competitor_videos SET trigger_rules=?, peak_observation=? WHERE video_id=?",
        (json.dumps(combined, ensure_ascii=False), observation_point, video_row["video_id"]),
    )
    return False


def _insert_baseline(
    conn: sqlite3.Connection,
    baseline_id: str,
    account_id: str,
    baseline_mode: str,
    metric: str,
    observation_point: str | None,
    sample_count: int,
    median_value: float,
    run_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO baselines(baseline_id, account_id, baseline_mode, metric, observation_point, sample_count, median_value, run_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(baseline_id) DO NOTHING
        """,
        (baseline_id, account_id, baseline_mode, metric, observation_point, sample_count, median_value, run_id),
    )


def summarize(conn: sqlite3.Connection, domain_label: str) -> dict[str, Any]:
    params = (domain_label,)
    return {
        "accounts": int(conn.execute("SELECT count(*) FROM competitor_accounts WHERE domain_label=? AND registration_status='active'", params).fetchone()[0]),
        "videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=?",
                params,
            ).fetchone()[0]
        ),
        "historical_mature_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.first_contact_category='historical_mature'",
                params,
            ).fetchone()[0]
        ),
        "transition_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.first_contact_category='transition'",
                params,
            ).fetchone()[0]
        ),
        "formal_new_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.first_contact_category='formal_new'",
                params,
            ).fetchone()[0]
        ),
        "baselines": int(
            conn.execute(
                "SELECT count(*) FROM baselines b JOIN competitor_accounts a ON a.account_id=b.account_id WHERE a.domain_label=?",
                params,
            ).fetchone()[0]
        ),
        "hits": int(
            conn.execute(
                "SELECT count(*) FROM hits h JOIN competitor_accounts a ON a.account_id=h.account_id WHERE a.domain_label=?",
                params,
            ).fetchone()[0]
        ),
    }


def _crawl_account(executor: LocalMediaCrawlerExecutor, account: sqlite3.Row, *, max_notes: int, run_id: str) -> dict[str, Any]:
    command = ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.video_snapshot",
        executable="vendor/MediaCrawler/main.py",
        args=(account["platform"], "creator", "--get_comment", "no"),
        input_payload={
            "platform": account["platform"],
            "source_url": account["homepage_url"],
            "source_kind": "creator",
            "with_comments": False,
            "headless": True,
            "registration_run_id": run_id,
        },
        max_items=max_notes,
        timeout_seconds=900,
    )
    result = executor.execute(command)
    if result.status != "succeeded":
        return {
            "account": account["account_name"],
            "account_id": account["account_id"],
            "status": result.status,
            "raw_archive_ref": result.raw_archive_ref,
            "items": [],
        }
    items = result.payload.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "account": account["account_name"],
        "account_id": account["account_id"],
        "status": "succeeded",
        "raw_archive_ref": result.raw_archive_ref,
        "fetched_items": len(items),
        "items": items,
    }


def resolve_max_notes(cli_value: int | None, settings: dict[str, Any]) -> tuple[int, str]:
    if cli_value is not None:
        if cli_value < 1:
            raise ValueError("--max-notes must be positive")
        return cli_value, "cli"
    hit_cfg = settings.get("hit_detection") or {}
    target = int(hit_cfg.get("first_crawl_max_notes", 50))
    if target < 1:
        raise ValueError("hit_detection.first_crawl_max_notes must be positive")
    buffer = int(hit_cfg.get("first_crawl_fetch_buffer", 0))
    if buffer < 0:
        raise ValueError("hit_detection.first_crawl_fetch_buffer must not be negative")
    return target + buffer, "settings.hit_detection.first_crawl_max_notes+first_crawl_fetch_buffer"


def validate_registration_execution_contract(domain: dict[str, Any], hit_cfg: dict[str, Any]) -> dict[str, Any]:
    # 2026-07-08: every threshold checked below is governed by BR-HIT-001 (see
    # BUSINESS_RULE_CATALOG.yaml) -- require_catalog_citations() raises if that
    # requirement_id does not actually exist in the catalog, so this function
    # cannot silently keep gating against a rule that has been deleted/renamed.
    require_catalog_citations(["BR-HIT-001"])
    policy = domain.get("collector_policy") or {}
    errors: list[str] = []
    if policy.get("first_crawl") != "stock_snapshot_archived":
        errors.append("collector_policy.first_crawl must be stock_snapshot_archived")
    if policy.get("comments") != "reverse_prep_only_for_promoted_hits":
        errors.append("collector_policy.comments must defer comments to reverse prep")
    if int(hit_cfg.get("observe_days", 0)) != 7:
        errors.append("hit_detection.observe_days must be 7")
    if int(hit_cfg.get("mature_history_window_days", 0)) != 90:
        errors.append("hit_detection.mature_history_window_days must be 90")
    if int(hit_cfg.get("mature_history_max_samples", 0)) != 50:
        errors.append("hit_detection.mature_history_max_samples must be 50")
    if int(hit_cfg.get("unified_min_samples", 0)) != 20:
        errors.append("hit_detection.unified_min_samples must be 20")
    if int(hit_cfg.get("formal_baseline_activation_min_samples", 0)) != 20:
        errors.append("hit_detection.formal_baseline_activation_min_samples must be 20")
    if int(hit_cfg.get("formal_baseline_computation_window", 0)) != 50:
        errors.append("hit_detection.formal_baseline_computation_window must be 50")
    if float(hit_cfg.get("discovery_delay_hours_max", 0)) != 36:
        errors.append("hit_detection.discovery_delay_hours_max must be 36")
    if float(hit_cfg.get("cold_start_d7_single_metric_threshold", 0)) != 3.0:
        errors.append("hit_detection.cold_start_d7_single_metric_threshold must be 3.0")
    if float(hit_cfg.get("cold_start_d7_multi_indicator_threshold", 0)) != 2.0:
        errors.append("hit_detection.cold_start_d7_multi_indicator_threshold must be 2.0")
    mature_history_absolute_like_floor = hit_cfg.get("mature_history_absolute_like_floor")
    if not isinstance(mature_history_absolute_like_floor, (int, float)) or mature_history_absolute_like_floor <= 0:
        errors.append("hit_detection.mature_history_absolute_like_floor must be a positive number")
    small_account_p90_percentile = hit_cfg.get("small_account_p90_percentile")
    if not isinstance(small_account_p90_percentile, (int, float)) or not (0 < small_account_p90_percentile < 1):
        errors.append("hit_detection.small_account_p90_percentile must be a number in (0, 1)")
    comment_like_ratio_threshold = hit_cfg.get("comment_like_ratio_threshold")
    if not isinstance(comment_like_ratio_threshold, (int, float)) or not (0 < comment_like_ratio_threshold <= 1):
        errors.append("hit_detection.comment_like_ratio_threshold must be a number in (0, 1]")
    if errors:
        raise ValueError("registration execution contract mismatch: " + "; ".join(errors))
    return {
        "design_sources": [MASTER_DESIGN_DOC, "BUSINESS_RULE_CATALOG.yaml", EXECUTION_GUARDRAIL_DOC],
        "first_crawl_stock_archived": True,
        "comments_deferred": True,
        "observe_days": 7,
        "discovery_delay_hours_max": float(hit_cfg["discovery_delay_hours_max"]),
        "mature_history_window_days": 90,
        "mature_history_max_samples": 50,
        "unified_min_samples": 20,
        "formal_baseline_activation_min_samples": 20,
        "formal_baseline_computation_window": 50,
        "cold_start_d7_single_metric_threshold": 3.0,
        "cold_start_d7_multi_indicator_threshold": 2.0,
        "mature_history_absolute_like_floor": mature_history_absolute_like_floor,
        "small_account_p90_percentile": small_account_p90_percentile,
        "hit_trigger_channels": [
            "like_anomaly (>=single_metric_excess_threshold)",
            "comment_anomaly (>=single_metric_excess_threshold)",
            "collect_anomaly (>=single_metric_excess_threshold)",
            "share_anomaly (>=single_metric_excess_threshold)",
            "multi_indicator (>=2 of 4 metrics clear multi_indicator_excess_threshold)",
            "comment_like_ratio (comment_count/like_count >= comment_like_ratio_threshold, no baseline required)",
        ],
        "comment_like_ratio_threshold": comment_like_ratio_threshold,
    }


def mark_pinned_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BR-HIT-001 section E: no pinned-detection step exists in this design.

    Kept as a no-op passthrough (rather than deleted outright) only because
    older fixtures/callers may still import it; age eligibility everywhere in
    this module is decided purely from real publish timestamps, never from a
    platform pinned flag or list-position heuristic.
    """
    return [dict(item) for item in items]


def stable_video_id(account_id: str, platform_item_id: str) -> str:
    return "cv_" + _digest(f"{account_id}:{platform_item_id}", 20)


def stable_baseline_id(account_id: str, run_id: str) -> str:
    return "bl_" + _digest(f"{account_id}:{run_id}", 20)


def stable_hit_id(account_id: str, platform_item_id: str) -> str:
    return "hit_" + _digest(f"{account_id}:{platform_item_id}", 20)


def stable_check_id(video_id: str, run_id: str) -> str:
    return "vck_" + _digest(f"{video_id}:{run_id}", 20)


def _digest(value: str, length: int) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _load_accounts(conn: sqlite3.Connection, domain_label: str, limit: int | None) -> list[sqlite3.Row]:
    sql = """
        SELECT account_id, account_name, platform, domain_label, sec_uid, homepage_url
          FROM competitor_accounts
         WHERE domain_label=? AND registration_status='active'
         ORDER BY created_at, account_name
    """
    rows = conn.execute(sql, (domain_label,)).fetchall()
    return rows[:limit] if limit else rows


def _timestamp_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _item_publish_datetime(item: dict[str, Any]) -> datetime | None:
    return _parse_datetime(_timestamp_to_iso(item.get("create_time")) if item.get("create_time") not in (None, "") else item.get("publish_time"))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"yaml must be an object: {path}")
    return data


def _repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _relative_ref(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_db_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    allowed = (ROOT / "data" / "formal").resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError(f"full registration DB must stay under data/formal: {resolved}")
    if resolved.name == "creation.db":
        raise ValueError("refusing to write old data/creation.db")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
