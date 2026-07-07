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
from scripts.core.external_adapters import ExternalAdapterCommand  # noqa: E402
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor  # noqa: E402


DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
FALLBACK_SETTINGS = ROOT / "config" / "settings.example.yaml"
EXECUTION_GUARDRAIL_DOC = "docs/production_execution_guardrails.md"
# BR-BASELINE-003 (BUSINESS_RULE_CATALOG.yaml): the only documented sample target is
# baseline_min_samples=30 (legacy_supplement=true: backfill toward it when the 90-day
# window falls short). This is the sole hard mathematical floor below which a median/P90
# cannot be computed at all -- not a business threshold, so it is not configurable.
BASELINE_HARD_MINIMUM_SAMPLES = 2

# 2026-07-07 user decision: a day-specific reference median (see
# _account_day_reference_median) must not be trusted off too few historical
# video_checks points at that exact day-offset -- this is the floor below which the
# channel is skipped entirely rather than fabricating an unreliable reference.
DAY_REFERENCE_MIN_SAMPLES = 3

# BR-BASELINE-002 / BUILD_PLAN.md 阶段1 二次修正 (2026-06-13): a video excluded as
# 'younger_than_7_days' at ingest time is NOT permanently disqualified -- that flag only
# means its growth-curve capture is missing its first few frames (noise for future
# curve modeling), not that it should stop being refreshed/re-judged. "Its count still
# gets refreshed daily and re-judged every round, it doesn't lose out" (原文:"其计数照样
# 每日刷新+每轮重判,不吃亏"). select_baseline_sample() already re-checks each row's live
# age against observe_days, so once such a row has genuinely aged past the window it is
# safe to let back into this query -- only 'pinned' stays a real, permanent exclusion.
# Videos still in the 'watching' status are included too: select_baseline_sample()'s own
# age check keeps still-young ones out (no early judgement -- BUILD_PLAN.md 阶段1: "现在
# 只捕获不建模"), while ones that have aged past observe_days flow through and graduate.
SETTLED_SAMPLE_QUERY = """
    SELECT *
      FROM competitor_videos
     WHERE account_id=?
       AND like_count IS NOT NULL
       AND status IN ('archived', 'promoted', 'watching')
       AND (excluded_reason IS NULL OR excluded_reason IN ('older_than_90_days', 'younger_than_7_days'))
     ORDER BY publish_time DESC
"""


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
    db_path = _safe_db_path(Path(args.db))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        if args.rejudge_only:
            report = run_rejudge_only(conn, domain, hit_cfg=hit_cfg, account_limit=args.account_limit)
        else:
            max_notes, max_notes_source = resolve_max_notes(args.max_notes, settings)
            report = run_full_registration(
                conn,
                domain,
                domain_path=domain_path,
                hit_cfg=hit_cfg,
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


def run_full_registration(
    conn: sqlite3.Connection,
    domain: dict[str, Any],
    *,
    domain_path: Path,
    hit_cfg: dict[str, Any],
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
            "first_crawl": domain["collector_policy"]["first_crawl"],
            "video_status_for_first_crawl": "archived",
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
        "judgement": judgement,
        "summary": summary,
    }


def run_rejudge_only(
    conn: sqlite3.Connection,
    domain: dict[str, Any],
    *,
    hit_cfg: dict[str, Any],
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
            "contract": execution_contract,
        },
        "registration": None,
        "crawl": {"requested_accounts": len(accounts), "results": []},
        "judgement": judgement,
        "summary": summary,
    }


def ingest_stock_items(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    items: list[dict[str, Any]],
    *,
    hit_cfg: dict[str, Any],
    run_id: str,
    raw_archive_ref: str | None,
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for item in mark_pinned_items(items):
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_id = stable_video_id(account["account_id"], platform_item_id)
        excluded_reason = first_crawl_excluded_reason(item, hit_cfg)
        existed = conn.execute(
            "SELECT video_id FROM competitor_videos WHERE account_id=? AND platform_item_id=?",
            (account["account_id"], platform_item_id),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO competitor_videos(
                video_id, account_id, platform, platform_item_id, title, url, publish_time,
                duration_sec, like_count, comment_count, share_count, collect_count,
                is_pinned, excluded_reason, status, registration_run_id, raw_archive_ref, raw_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived', ?, ?, ?)
            ON CONFLICT(account_id, platform_item_id) DO UPDATE SET
                title=excluded.title,
                url=excluded.url,
                publish_time=excluded.publish_time,
                duration_sec=excluded.duration_sec,
                like_count=excluded.like_count,
                comment_count=excluded.comment_count,
                share_count=excluded.share_count,
                collect_count=excluded.collect_count,
                is_pinned=excluded.is_pinned,
                excluded_reason=excluded.excluded_reason,
                status=CASE WHEN competitor_videos.status='promoted' THEN 'promoted' ELSE 'archived' END,
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
                _optional_int(item.get("liked_count") or item.get("like_count")),
                _optional_int(item.get("comment_count")),
                _optional_int(item.get("share_count")),
                _optional_int(item.get("collected_count") or item.get("collect_count")),
                1 if bool(item.get("_is_pinned")) else 0,
                excluded_reason,
                run_id,
                raw_archive_ref,
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )
        if existed:
            updated += 1
        else:
            inserted += 1
    return inserted, updated


def ingest_daily_incremental_items(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    items: list[dict[str, Any]],
    *,
    hit_cfg: dict[str, Any],
    run_id: str,
    raw_archive_ref: str | None,
) -> dict[str, Any]:
    """Process one day's fetched batch for one account.

    BUILD_PLAN.md 阶段1/BR-COLLECT-002/BR-COLLECT-004: reconcile against videos already
    known to this account first (update metrics + append a video_checks snapshot), then
    treat whatever is left over as newly discovered. New discoveries within the
    observation window enter 'watching'; only an explicit platform pinned flag routes a
    new discovery straight to archived+excluded (no positional guessing on a small daily
    batch). A discovery whose publish_time is already past the window on arrival is
    treated like a stock item -- settled immediately, never enters watching.
    """
    observe_days = int(hit_cfg.get("observe_days", 7))
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=observe_days)

    existing_ids = {
        row["platform_item_id"]
        for row in conn.execute(
            "SELECT platform_item_id FROM competitor_videos WHERE account_id=?",
            (account["account_id"],),
        ).fetchall()
    }

    existing_items: list[dict[str, Any]] = []
    new_items: list[dict[str, Any]] = []
    for item in items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        (existing_items if platform_item_id in existing_ids else new_items).append(item)

    updated = 0
    checks_recorded = 0
    for item in existing_items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_id = stable_video_id(account["account_id"], platform_item_id)
        like_count = _optional_int(item.get("liked_count") or item.get("like_count"))
        comment_count = _optional_int(item.get("comment_count"))
        share_count = _optional_int(item.get("share_count"))
        collect_count = _optional_int(item.get("collected_count") or item.get("collect_count"))
        conn.execute(
            """
            UPDATE competitor_videos
               SET like_count=?, comment_count=?, share_count=?, collect_count=?,
                   last_checked_at=CURRENT_TIMESTAMP, check_count=check_count + 1
             WHERE video_id=?
            """,
            (like_count, comment_count, share_count, collect_count, video_id),
        )
        conn.execute(
            """
            INSERT INTO video_checks(check_id, video_id, like_count, comment_count, share_count, collect_count, run_id)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO NOTHING
            """,
            (stable_check_id(video_id, run_id), video_id, like_count, comment_count, share_count, collect_count, run_id),
        )
        updated += 1
        checks_recorded += 1

    inserted = 0
    for item in new_items:
        platform_item_id = _required_text(item.get("aweme_id") or item.get("source_id") or item.get("id"), "aweme_id")
        video_id = stable_video_id(account["account_id"], platform_item_id)
        published_at = _item_publish_datetime(item)
        explicit_pinned = _explicit_pinned_value(item)
        like_count = _optional_int(item.get("liked_count") or item.get("like_count"))
        comment_count = _optional_int(item.get("comment_count"))
        share_count = _optional_int(item.get("share_count"))
        collect_count = _optional_int(item.get("collected_count") or item.get("collect_count"))

        if explicit_pinned:
            status, excluded_reason = "archived", "pinned"
        elif published_at is None or published_at < window_start:
            status, excluded_reason = "archived", None
        else:
            status, excluded_reason = "watching", None

        conn.execute(
            """
            INSERT INTO competitor_videos(
                video_id, account_id, platform, platform_item_id, title, url, publish_time,
                duration_sec, like_count, comment_count, share_count, collect_count,
                is_pinned, excluded_reason, status, registration_run_id, raw_archive_ref, raw_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                1 if explicit_pinned else 0,
                excluded_reason,
                status,
                run_id,
                raw_archive_ref,
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT INTO video_checks(check_id, video_id, like_count, comment_count, share_count, collect_count, run_id)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO NOTHING
            """,
            (stable_check_id(video_id, run_id), video_id, like_count, comment_count, share_count, collect_count, run_id),
        )
        inserted += 1
        checks_recorded += 1

    return {"inserted": inserted, "updated": updated, "checks_recorded": checks_recorded}


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
        result["valid_baseline_candidates"] = 0
        return result
    inserted, updated = ingest_stock_items(
        conn, account, result["items"], hit_cfg=hit_cfg, run_id=run_id, raw_archive_ref=result["raw_archive_ref"]
    )
    conn.commit()
    result = dict(result)
    result["inserted_videos"] = inserted
    result["updated_videos"] = updated
    result["valid_baseline_candidates"] = settled_sample_count(conn, account["account_id"], hit_cfg)
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
    account_limit: int | None = None,
    executor: LocalMediaCrawlerExecutor | None = None,
) -> dict[str, Any]:
    """BUILD_PLAN.md 阶段1/3 (定时·快·不碰逆向转写): one daily invocation that discovers
    newly-published videos, refreshes the observation pool, and judges/graduates
    everything that has aged past the observation window -- all in one contract-gated
    entrypoint, same discipline as run_full_registration/run_rejudge_only.
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
        "summary": summary,
    }


def judge_domain(conn: sqlite3.Connection, domain_label: str, *, hit_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
    rows = _load_accounts(conn, domain_label, limit=None)
    results: list[dict[str, Any]] = []
    total_hits = 0
    total_retracted = 0
    total_graduated = 0
    total_promoted_early = 0
    for account in rows:
        account_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id=run_id)
        total_hits += account_result["promoted_count"]
        total_retracted += account_result.get("retracted_count", 0)
        total_graduated += account_result.get("graduated_count", 0)
        total_promoted_early += account_result.get("promoted_early_count", 0)
        results.append(account_result)
    return {
        "total_promoted": total_hits,
        "total_retracted": total_retracted,
        "total_graduated": total_graduated,
        "total_promoted_early": total_promoted_early,
        "accounts": results,
    }


def settled_sample_count(conn: sqlite3.Connection, account_id: str, hit_cfg: dict[str, Any]) -> int:
    rows = conn.execute(SETTLED_SAMPLE_QUERY, (account_id,)).fetchall()
    sample, _ = select_baseline_sample(rows, hit_cfg)
    return len(sample)


def _account_day_reference_median(conn: sqlite3.Connection, account_id: str, day_offset: int) -> float | None:
    """Median like_count across this account's OTHER videos' video_checks history,
    restricted to checks recorded exactly `day_offset` days after that video's own
    publish_time. Returns None (not zero, not a guess) when there are fewer than
    DAY_REFERENCE_MIN_SAMPLES such checks -- the caller must skip the channel, not
    fabricate a reference from too few points.
    """
    rows = conn.execute(
        """
        SELECT vc.like_count, vc.checked_at, cv.publish_time
          FROM video_checks vc
          JOIN competitor_videos cv ON cv.video_id = vc.video_id
         WHERE cv.account_id = ? AND vc.like_count IS NOT NULL AND cv.publish_time IS NOT NULL
        """,
        (account_id,),
    ).fetchall()
    values: list[int] = []
    for row in rows:
        published_at = _parse_datetime(row["publish_time"])
        checked_at = _parse_datetime(row["checked_at"])
        if published_at is None or checked_at is None:
            continue
        if (checked_at.date() - published_at.date()).days == day_offset:
            values.append(int(row["like_count"]))
    if len(values) < DAY_REFERENCE_MIN_SAMPLES:
        return None
    return float(statistics.median(values))


def _evaluate_hit_channels(
    row: sqlite3.Row, like_threshold: float, comment_like_ratio_threshold: float
) -> tuple[bool, bool]:
    like_count = int(row["like_count"] or 0)
    comment_like_ratio = (
        row["comment_count"] / like_count if row["comment_count"] is not None and like_count > 0 else None
    )
    like_channel_hit = like_count >= like_threshold
    ratio_channel_hit = comment_like_ratio is not None and comment_like_ratio >= comment_like_ratio_threshold
    return like_channel_hit, ratio_channel_hit


def _hit_channel_label(like_channel_hit: bool, ratio_channel_hit: bool) -> str:
    if like_channel_hit and ratio_channel_hit:
        return "both"
    if like_channel_hit:
        return "like_threshold"
    return "comment_like_ratio"


def _promote_hit(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    row: sqlite3.Row,
    *,
    median: float,
    baseline_id: str,
    hit_channel: str,
    evidence_status: str,
    run_id: str,
) -> None:
    like_count = int(row["like_count"] or 0)
    hit_id = stable_hit_id(account["account_id"], row["platform_item_id"])
    excess_ratio = round(like_count / median, 2) if median else None
    share_comment_ratio = (
        round(int(row["share_count"]) / int(row["comment_count"]), 2)
        if row["share_count"] and row["comment_count"]
        else None
    )
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            publish_time, like_count, comment_count, share_count, collect_count,
            excess_ratio, share_comment_ratio, baseline_id, hit_channel, evidence_status, run_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, platform_item_id) DO UPDATE SET
            like_count=excluded.like_count,
            comment_count=excluded.comment_count,
            share_count=excluded.share_count,
            collect_count=excluded.collect_count,
            excess_ratio=excluded.excess_ratio,
            share_comment_ratio=excluded.share_comment_ratio,
            baseline_id=excluded.baseline_id,
            hit_channel=excluded.hit_channel,
            evidence_status=excluded.evidence_status,
            run_id=excluded.run_id
        """,
        (
            hit_id,
            row["video_id"],
            account["account_id"],
            row["platform"],
            row["platform_item_id"],
            row["title"],
            row["url"],
            row["publish_time"],
            row["like_count"],
            row["comment_count"],
            row["share_count"],
            row["collect_count"],
            excess_ratio,
            share_comment_ratio,
            baseline_id,
            hit_channel,
            evidence_status,
            run_id,
        ),
    )
    conn.execute("UPDATE competitor_videos SET status='promoted' WHERE video_id=?", (row["video_id"],))


def judge_account(conn: sqlite3.Connection, account: sqlite3.Row, *, hit_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
    rows = conn.execute(SETTLED_SAMPLE_QUERY, (account["account_id"],)).fetchall()
    sample, sample_window = select_baseline_sample(rows, hit_cfg)
    target_samples = int(hit_cfg.get("baseline_min_samples", 30))
    if len(sample) < BASELINE_HARD_MINIMUM_SAMPLES:
        return {
            "account": account["account_name"],
            "sample_count": len(sample),
            "minimum_sample_count": BASELINE_HARD_MINIMUM_SAMPLES,
            "status": "skipped_insufficient_sample",
            "evidence_status": "insufficient_sample",
            "promoted_count": 0,
        }
    # BR-BASELINE-003: below the documented 30-sample target, judgement may proceed
    # (the account may never accumulate more without ongoing daily collection) but any
    # resulting hit must carry an explicit insufficient-evidence flag -- never promoted
    # silently as if the baseline were fully powered.
    evidence_status = "sufficient" if len(sample) >= target_samples else "insufficient_sample"
    likes = [int(row["like_count"]) for row in sample if row["like_count"] is not None]
    median = float(statistics.median(likes))
    p90_value = p90(likes)
    # BR-HIT-001 (2026-07-06 revision): threshold = max(median * excess_threshold,
    # min(P90, hit_floor_absolute_like_count)). P90 alone is close to tautological on a
    # small, self-referential sample -- ~10% of any account's videos clear its own P90
    # by definition, regardless of whether that account has a genuine standout. Capping
    # it at the configurable absolute floor stops it from mechanically outranking every
    # other account at the same fixed percentile, while still giving small accounts
    # (whose P90 never reaches the floor) a reachable relative bar instead of a
    # permanently unreachable absolute one. There is no separate p90_required toggle --
    # P90 always participates, just bounded.
    hit_floor = float(hit_cfg["hit_floor_absolute_like_count"])
    like_threshold = max(median * float(hit_cfg["excess_threshold"]), min(p90_value, hit_floor))
    # BR-HIT-001 (2026-07-07 revision): a second, independent channel -- comment_count /
    # like_count -- is OR-ed alongside the like_count channel. Unlike like_threshold,
    # this is NOT computed from this account's own baseline; it is a fixed threshold
    # sourced from published Douyin operator guidance on comment-to-like ratio (10%
    # floor, ~30% for a typical hit), verified against the real 28-account/1253-video
    # dataset before being adopted (see BUSINESS_RULE_CATALOG.yaml amendment). It exists
    # because a video can be a genuine hit through unusually deep comment engagement
    # even when its raw like_count never clears the account's own scale threshold --
    # exactly the case that left one real account (财经不眠姐) with zero hits under the
    # like-only formula. collect_count/share_count ratios were evaluated the same way
    # but no published threshold survived contact with the real dataset (see amendment
    # note), so those two dimensions are not judged yet.
    comment_like_ratio_threshold = float(hit_cfg["comment_like_ratio_threshold"])
    # 2026-07-07 user decision: a day-specific reference channel for still-watching
    # videos -- see _account_day_reference_median and its use below. Lower than
    # excess_threshold (3.0) on purpose: the user explicitly rejected reusing 3x here
    # because it pulls the bar too high for large accounts, same reasoning that shaped
    # excess_threshold itself, just applied to a day-of-life-specific reference instead
    # of the mature/settled one.
    early_excess_threshold = float(hit_cfg.get("early_excess_threshold", 2.0))
    baseline_id = stable_baseline_id(account["account_id"], run_id)
    conn.execute(
        """
        INSERT INTO baselines(
            baseline_id, account_id, metric, window_days, sample_count,
            median_value, p90_value, threshold_value, evidence_status, run_id
        )
        VALUES(?, ?, 'like_count', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(baseline_id) DO NOTHING
        """,
        (
            baseline_id,
            account["account_id"],
            sample_window,
            len(likes),
            median,
            p90_value,
            like_threshold,
            evidence_status,
            run_id,
        ),
    )
    promoted = 0
    retracted = 0
    graduated = 0
    promoted_via_ratio_channel = 0
    promoted_early = 0
    for row in sample:
        like_channel_hit, ratio_channel_hit = _evaluate_hit_channels(row, like_threshold, comment_like_ratio_threshold)
        if not (like_channel_hit or ratio_channel_hit):
            # BR-HIT-003 (idempotent judgement): a video re-evaluated in this same
            # judgement pass that no longer clears either recomputed channel must not
            # stay marked as a hit from an earlier, since-corrected judgement. Scoped
            # to the current sample only -- videos outside this run's sample are a
            # past judgement's point-in-time record and are left alone.
            if row["status"] == "promoted":
                conn.execute("DELETE FROM hits WHERE video_id=?", (row["video_id"],))
                conn.execute("UPDATE competitor_videos SET status='archived' WHERE video_id=?", (row["video_id"],))
                retracted += 1
            elif row["status"] == "watching":
                # BUILD_PLAN.md 阶段2: "watching 到期(>7天)-> archived 转基线材料" -- this
                # row only reached `sample` because select_baseline_sample's own age check
                # confirmed it has left the observation window, so this is its first-ever
                # judgement (graduation), not a retraction: it was never a hit, it settles
                # as ordinary baseline material and daily incremental stops touching it.
                conn.execute("UPDATE competitor_videos SET status='archived' WHERE video_id=?", (row["video_id"],))
                graduated += 1
            continue
        hit_channel = _hit_channel_label(like_channel_hit, ratio_channel_hit)
        _promote_hit(
            conn, account, row,
            median=median, baseline_id=baseline_id, hit_channel=hit_channel,
            evidence_status=evidence_status, run_id=run_id,
        )
        promoted += 1
        if hit_channel in ("comment_like_ratio", "both"):
            promoted_via_ratio_channel += 1

    # 2026-07-07 user decision: the point of capturing a still-watching video's own
    # 0-7 day video_checks curve is to actually use it, not just store it. A video
    # still inside the observation window that already clears the account's existing
    # hit threshold is promoted right now -- not held back until it ages out. This
    # uses the same already-validated hit criteria as the settled-sample loop above,
    # not a separate growth-curve/steepness model (no historical trajectory data
    # exists yet to calibrate one responsibly). A still-watching video that has NOT
    # cleared either channel is left untouched here -- it stays 'watching' and will
    # either clear the bar on a future day or graduate via the loop above once it
    # ages past observe_days without ever clearing it.
    promoted_via_day_reference = 0
    watching_rows = conn.execute(
        "SELECT * FROM competitor_videos WHERE account_id=? AND status='watching' AND like_count IS NOT NULL",
        (account["account_id"],),
    ).fetchall()
    for row in watching_rows:
        like_channel_hit, ratio_channel_hit = _evaluate_hit_channels(row, like_threshold, comment_like_ratio_threshold)
        day_reference_hit = False
        if not (like_channel_hit or ratio_channel_hit):
            # 2026-07-07: comparing a still-young video against the mature threshold
            # (calibrated for >=7 day-old videos) almost never fires this early, which
            # defeats the point of checking at all -- so also compare it against this
            # account's own historical median at the SAME day-of-life, computed from
            # video_checks. Skipped entirely (not a soft zero) when there isn't enough
            # day-specific history yet to trust that median.
            published_at = _parse_datetime(row["publish_time"])
            if published_at is not None:
                day_offset = (datetime.now(timezone.utc).date() - published_at.date()).days
                day_reference_median = _account_day_reference_median(conn, account["account_id"], day_offset)
                if day_reference_median is not None:
                    like_count = int(row["like_count"] or 0)
                    day_reference_hit = like_count >= day_reference_median * early_excess_threshold
        if not (like_channel_hit or ratio_channel_hit or day_reference_hit):
            continue
        if day_reference_hit and not (like_channel_hit or ratio_channel_hit):
            # Reuses the 'like_threshold' label (a magnitude-based like_count check,
            # same category, just against a day-specific reference instead of the
            # mature one) rather than adding a new hits.hit_channel enum value, which
            # would require migrating the CHECK constraint on the real production
            # table. promoted_via_day_reference_count is the source of truth for how
            # many hits this specific channel produced.
            hit_channel = "like_threshold"
            promoted_via_day_reference += 1
        else:
            hit_channel = _hit_channel_label(like_channel_hit, ratio_channel_hit)
        _promote_hit(
            conn, account, row,
            median=median, baseline_id=baseline_id, hit_channel=hit_channel,
            evidence_status=evidence_status, run_id=run_id,
        )
        promoted += 1
        promoted_early += 1
        if hit_channel in ("comment_like_ratio", "both"):
            promoted_via_ratio_channel += 1

    return {
        "account": account["account_name"],
        "sample_count": len(likes),
        "median": round(median, 2),
        "p90": round(p90_value, 2),
        "threshold": round(like_threshold, 2),
        "comment_like_ratio_threshold": comment_like_ratio_threshold,
        "promoted_count": promoted,
        "promoted_early_count": promoted_early,
        "promoted_via_ratio_channel": promoted_via_ratio_channel,
        "promoted_via_day_reference_count": promoted_via_day_reference,
        "retracted_count": retracted,
        "graduated_count": graduated,
        "status": "judged",
        "evidence_status": evidence_status,
    }


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
        "archived_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.status='archived'",
                params,
            ).fetchone()[0]
        ),
        "watching_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.status='watching'",
                params,
            ).fetchone()[0]
        ),
        "promoted_videos": int(
            conn.execute(
                "SELECT count(*) FROM competitor_videos v JOIN competitor_accounts a ON a.account_id=v.account_id WHERE a.domain_label=? AND v.status='promoted'",
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


def p90(values: list[int]) -> float:
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(sorted(values), n=10)[8])


def resolve_max_notes(cli_value: int | None, settings: dict[str, Any]) -> tuple[int, str]:
    if cli_value is not None:
        if cli_value < 1:
            raise ValueError("--max-notes must be positive")
        return cli_value, "cli"
    hit_cfg = settings.get("hit_detection") or {}
    target = int(hit_cfg.get("baseline_min_samples", 30))
    if target < 1:
        raise ValueError("hit_detection.baseline_min_samples must be positive")
    # Pinned and younger-than-observe-window videos are permanently ineligible for
    # baseline computation (never just temporarily excluded), so fetching exactly the
    # 30-sample target guarantees falling short after exclusions. Fetch a buffer beyond
    # the target so the first crawl alone can actually reach 30 eligible videos for
    # accounts with a realistic amount of pinned/young content, instead of every account
    # structurally capping below the target regardless of how many days pass.
    buffer = int(hit_cfg.get("first_crawl_fetch_buffer", 0))
    if buffer < 0:
        raise ValueError("hit_detection.first_crawl_fetch_buffer must not be negative")
    return target + buffer, "settings.hit_detection.baseline_min_samples+first_crawl_fetch_buffer"


def validate_registration_execution_contract(domain: dict[str, Any], hit_cfg: dict[str, Any]) -> dict[str, Any]:
    policy = domain.get("collector_policy") or {}
    errors: list[str] = []
    if policy.get("first_crawl") != "stock_snapshot_archived":
        errors.append("collector_policy.first_crawl must be stock_snapshot_archived")
    if policy.get("comments") != "reverse_prep_only_for_promoted_hits":
        errors.append("collector_policy.comments must defer comments to reverse prep")
    if int(hit_cfg.get("observe_days", 0)) != 7:
        errors.append("hit_detection.observe_days must be 7")
    if int(hit_cfg.get("baseline_window_days", 0)) != 90:
        errors.append("hit_detection.baseline_window_days must be 90")
    if int(hit_cfg.get("baseline_min_samples", 0)) != 30:
        errors.append("hit_detection.baseline_min_samples must stay the 30-sample target")
    hit_floor = hit_cfg.get("hit_floor_absolute_like_count")
    if not isinstance(hit_floor, (int, float)) or hit_floor <= 0:
        errors.append("hit_detection.hit_floor_absolute_like_count must be a positive number")
    comment_like_ratio_threshold = hit_cfg.get("comment_like_ratio_threshold")
    if not isinstance(comment_like_ratio_threshold, (int, float)) or not (0 < comment_like_ratio_threshold <= 1):
        errors.append("hit_detection.comment_like_ratio_threshold must be a number in (0, 1]")
    if errors:
        raise ValueError("registration execution contract mismatch: " + "; ".join(errors))
    return {
        "design_sources": ["BUSINESS_RULE_CATALOG.yaml", "AGENTS.md", "BUILD_PLAN.md", EXECUTION_GUARDRAIL_DOC],
        "first_crawl_stock_archived": True,
        "comments_deferred": True,
        "pinned_excluded": True,
        "young_videos_excluded_from_judgement_days": 7,
        "baseline_window_days": 90,
        "baseline_target_samples": 30,
        "baseline_hard_minimum_samples": BASELINE_HARD_MINIMUM_SAMPLES,
        "baseline_legacy_supplement": True,
        "hit_threshold_formula": (
            "max(median * excess_threshold, min(P90, hit_floor_absolute_like_count))"
            " OR comment_count/like_count >= comment_like_ratio_threshold"
        ),
        "hit_floor_absolute_like_count": hit_floor,
        "comment_like_ratio_threshold": comment_like_ratio_threshold,
    }


def select_baseline_sample(rows: list[sqlite3.Row], hit_cfg: dict[str, Any]) -> tuple[list[sqlite3.Row], int]:
    now = datetime.now(timezone.utc)
    observe_days = int(hit_cfg.get("observe_days", 7))
    baseline_window_days = int(hit_cfg.get("baseline_window_days", 90))
    target_samples = int(hit_cfg.get("baseline_min_samples", 30))
    settled_before = now - timedelta(days=observe_days)
    window_start = now - timedelta(days=baseline_window_days)
    usable: list[tuple[datetime, sqlite3.Row]] = []
    for row in rows:
        published_at = _parse_datetime(row["publish_time"])
        if published_at is None or published_at > settled_before:
            continue
        usable.append((published_at, row))
    usable.sort(key=lambda item: item[0], reverse=True)
    recent = [row for published_at, row in usable if published_at >= window_start]
    # BR-BASELINE-003 legacy_supplement=true: backfill toward the 30-sample target from
    # all usable (settled) videos, ignoring the 90-day window, whenever the in-window
    # sample falls short -- matching the pre-migration scripts/analyze/judge_hits.py
    # behavior exactly (there is no separate lower "minimum before expanding" number).
    if len(recent) >= target_samples:
        return recent[:target_samples], baseline_window_days
    return [row for _, row in usable[:target_samples]], 0


def mark_pinned_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marked = [dict(item) for item in items]
    if not marked:
        return marked
    explicit_found = False
    for item in marked:
        explicit = _explicit_pinned_value(item)
        if explicit is not None:
            item["_is_pinned"] = explicit
            explicit_found = True
    if explicit_found:
        for item in marked:
            item.setdefault("_is_pinned", False)
        return marked
    first_four = marked[:4]
    if len(first_four) < 4:
        for item in marked:
            item["_is_pinned"] = False
        return marked
    anchor_time = _item_publish_datetime(first_four[3])
    for idx, item in enumerate(marked):
        published_at = _item_publish_datetime(item)
        item["_is_pinned"] = bool(idx < 3 and anchor_time and published_at and published_at < anchor_time)
    return marked


def first_crawl_excluded_reason(item: dict[str, Any], hit_cfg: dict[str, Any]) -> str | None:
    published_at = _item_publish_datetime(item)
    now = datetime.now(timezone.utc)
    observe_days = int(hit_cfg.get("observe_days", 7))
    baseline_window_days = int(hit_cfg.get("baseline_window_days", 90))
    if bool(item.get("_is_pinned")):
        return "pinned"
    if published_at is None:
        return "missing_publish_time"
    if published_at > now - timedelta(days=observe_days):
        return "younger_than_7_days"
    if published_at < now - timedelta(days=baseline_window_days):
        return "older_than_90_days"
    return None


def _explicit_pinned_value(item: dict[str, Any]) -> bool | None:
    for key in ("is_pinned", "is_top", "is_stick", "stick_top", "is_sticky", "top"):
        if key in item:
            value = item.get(key)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y"}
            return bool(value)
    return None


def _item_publish_datetime(item: dict[str, Any]) -> datetime | None:
    return _parse_datetime(_timestamp_to_iso(item.get("create_time")) if item.get("create_time") not in (None, "") else item.get("publish_time"))


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
