"""账号发现复查(A5, 2026-07-13, 置顶规则总表核对后, 原文档7.1"标签搜索发现账号的
复查触发")。

Explicitly NOT implemented (documented limitation, not an oversight): the
source document's "拉这个账号最近10条可访问视频" step -- doing that for real
would need a fresh MediaCrawler creator-profile fetch (a second real network
call), which this pass does not make. video_count/统计 here uses only the
videos this account's tag-search discoveries have already produced (see
discovered_external_videos), which may be fewer or more than exactly 10 --
honest about using real data that exists, not pretending a "最近10条" pull
happened.

Usage:
    python -m scripts.core.business_data.run_account_discovery --domain-label fan_kepu_social_life --db ...
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_baseline_citations  # noqa: E402

# 原文档7.1 的真实数字:30天窗口内至少3条不同视频。
ACCOUNT_REVIEW_WINDOW_DAYS = 30
ACCOUNT_REVIEW_MIN_VIDEOS = 3


def validate_account_discovery_execution_contract() -> dict[str, Any]:
    return require_baseline_citations(["3"])


def find_accounts_due_for_review(conn: sqlite3.Connection, *, domain_label: str, run_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """原文档7.1:"同一非对标账号在30天内至少有3条不同视频通过当前领域的外部
    视频筛选" -- 才建复查任务,不是搜到一次就建。已经有一条复查记录(不管
    disposition 是什么)的账号不重复建,除非是 ignored_30d 且 ignored_until 已过。"""
    validate_account_discovery_execution_contract()
    now = now or datetime.now(timezone.utc)
    window_start = (now - timedelta(days=ACCOUNT_REVIEW_WINDOW_DAYS)).isoformat()
    now_iso = now.isoformat()

    candidates = conn.execute(
        """
        SELECT account_platform_id, account_handle, COUNT(DISTINCT platform_item_id) AS video_count
          FROM discovered_external_videos
         WHERE domain_label = ?
           AND is_tracked_account = 0
           AND discovered_at >= ?
         GROUP BY account_platform_id
        HAVING video_count >= ?
        """,
        (domain_label, window_start, ACCOUNT_REVIEW_MIN_VIDEOS),
    ).fetchall()

    created: list[dict[str, Any]] = []
    for row in candidates:
        existing = conn.execute(
            "SELECT * FROM discovered_account_review WHERE account_platform_id=? AND domain_label=?",
            (row["account_platform_id"], domain_label),
        ).fetchone()
        if existing is not None:
            still_ignored = existing["disposition"] == "ignored_30d" and existing["ignored_until"] and existing["ignored_until"] > now_iso
            already_resolved_other_way = existing["disposition"] in ("added", "ignored_permanently")
            if still_ignored or already_resolved_other_way or existing["disposition"] == "pending":
                continue
        review_id = f"{domain_label}_{row['account_platform_id']}_{run_id}"
        conn.execute(
            """
            INSERT INTO discovered_account_review(review_id, account_platform_id, account_handle, domain_label, video_count, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_platform_id, domain_label) DO UPDATE SET
                video_count = excluded.video_count,
                disposition = 'pending',
                ignored_until = NULL,
                triggered_at = CURRENT_TIMESTAMP,
                run_id = excluded.run_id
            """,
            (review_id, row["account_platform_id"], row["account_handle"], domain_label, row["video_count"], run_id),
        )
        created.append({"account_platform_id": row["account_platform_id"], "video_count": row["video_count"]})
    conn.commit()
    return created


def resolve_account_review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    disposition: str,
    note: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """三选一,不是 review_queue.py 的通过/拒绝二元机制:
    - added: 人工决定值得追踪,调用方之后应走现有 register_competitor_accounts.py 正式注册
      (这个函数本身不自动注册——注册账号必须真人确认,搜索命中不能绕过)。
    - ignored_30d: 30天内不再因为这个账号触发复查(ignored_until 设成30天后)。
    - ignored_permanently: 永久不再触发。"""
    if disposition not in ("added", "ignored_30d", "ignored_permanently"):
        raise ValueError(f"unsupported disposition: {disposition!r}")
    row = conn.execute("SELECT * FROM discovered_account_review WHERE review_id=?", (review_id,)).fetchone()
    if row is None:
        raise ValueError(f"no discovered_account_review row with id {review_id!r}")
    now = now or datetime.now(timezone.utc)
    ignored_until = (now + timedelta(days=ACCOUNT_REVIEW_WINDOW_DAYS)).isoformat() if disposition == "ignored_30d" else None
    conn.execute(
        """
        UPDATE discovered_account_review
           SET disposition = ?, ignored_until = ?, resolved_at = CURRENT_TIMESTAMP, resolved_note = ?
         WHERE review_id = ?
        """,
        (disposition, ignored_until, note, review_id),
    )
    conn.commit()
    return {"review_id": review_id, "disposition": disposition, "ignored_until": ignored_until}


def list_pending_account_reviews(conn: sqlite3.Connection, *, domain_label: str | None = None) -> list[dict[str, Any]]:
    """人工看的候选账号清单——disposition='pending' 三选一(加入/忽略30天/永久
    忽略)套不进 review_queue.py 现成的通过/拒绝二元机制(见 schema 文件里的
    说明),所以这里单独给一个只读列表函数,不是复用 review_queue.py 的
    STAGES 通用循环。"""
    query = "SELECT * FROM discovered_account_review WHERE disposition = 'pending'"
    params: tuple[Any, ...] = ()
    if domain_label is not None:
        query += " AND domain_label = ?"
        params = (domain_label,)
    query += " ORDER BY triggered_at"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Create/list account-discovery review tasks for accounts crossing the 30-day/3-video threshold.")
    parser.add_argument("--domain-label")
    parser.add_argument("--db", required=True)
    parser.add_argument("--list", action="store_true", help="list pending account reviews instead of scanning for new ones")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.list:
            report: dict[str, Any] = {"pending": list_pending_account_reviews(conn, domain_label=args.domain_label)}
        else:
            if not args.domain_label:
                raise SystemExit("--domain-label is required unless --list is given")
            run_id = "account_discovery_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            created = find_accounts_due_for_review(conn, domain_label=args.domain_label, run_id=run_id)
            report = {"run_id": run_id, "created": len(created), "accounts": created}
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
