"""self_owned 账号 + 发布追踪的 schema 安装 + 最小登记逻辑
(own_publications_schema.sqlite.sql 对应的 Python 侧,2026-07-13, 置顶规则总表
核对后, BR-EXPERIENCE-002)。

这次只做"建表 + 一个能跑的登记脚本",不做录入界面(照抄 review_queue.py"先跑通、
界面是另一次决定"的既有节奏)。真正的"用发布数据评估一条候选方法"逻辑属于
run_publication_experiment.py(另一个绑定,接线已经写好但没人调用的
goal09_experiments.py),这个文件只管"把真实发布事实存进去"这一步。

Usage (as a library, no CLI yet -- see module docstring above):
    from scripts.core.business_data.own_publications import install_schema, register_own_account, record_own_publication
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("own_publications_schema.sqlite.sql")


def install_schema(conn: sqlite3.Connection) -> None:
    """Idempotent (CREATE TABLE IF NOT EXISTS): safe to call every time,
    same convention as register_competitor_accounts.install_schema(). Must
    be called AFTER competitor_accounts_schema.sqlite.sql (own_publications
    references script_drafts) and AFTER goal01_schema.sqlite.sql (own_
    publications/own_experiments reference trace_root) are installed on the
    same connection -- this module does not install those itself, since it
    would create an import-order coupling this file shouldn't own."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def register_own_account(conn: sqlite3.Connection, *, account_id: str, platform: str, handle: str, domain_label: str) -> None:
    """Idempotent on (platform, handle) -- re-registering the same real
    account updates domain_label/account_id mapping rather than erroring,
    matching register_competitor_accounts.py's ON CONFLICT convention."""
    conn.execute(
        """
        INSERT INTO own_accounts(account_id, platform, handle, domain_label)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(platform, handle) DO UPDATE SET domain_label = excluded.domain_label
        """,
        (account_id, platform, handle, domain_label),
    )
    conn.commit()


def record_own_publication(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    account_id: str,
    script_draft_id: str,
    platform_url: str,
    published_at: str,
    human_confirmed_by: str,
    planned_tactic_id: str | None = None,
    actually_used_tactic_id: str | None = None,
    target_age_hours: float | None = None,
    actual_age_hours: float | None = None,
) -> str:
    """Records a real, human-confirmed publication event. human_confirmed_by
    has no default and is required -- this is a real-world fact the system
    cannot infer or assume, per BR-EXPERIENCE-002's "own publication
    tracking" trigger."""
    if not human_confirmed_by.strip():
        raise ValueError("human_confirmed_by is required -- a publication cannot be recorded without a real person confirming it happened")
    conn.execute(
        """
        INSERT INTO own_publications(
            publication_id, account_id, script_draft_id, planned_tactic_id, actually_used_tactic_id,
            platform_url, published_at, target_age_hours, actual_age_hours, human_confirmed_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            publication_id, account_id, script_draft_id, planned_tactic_id, actually_used_tactic_id,
            platform_url, published_at, target_age_hours, actual_age_hours, human_confirmed_by,
        ),
    )
    conn.commit()
    return publication_id


def record_own_publication_check(
    conn: sqlite3.Connection,
    *,
    check_id: str,
    publication_id: str,
    day_since_publish: int,
    like_count: int | None,
    comment_count: int | None,
    share_count: int | None,
    collect_count: int | None,
    run_id: str,
) -> str:
    """One row per real metric snapshot at a given day_since_publish (0 =
    publish day, 7 = the real-world 7-day mark BR-EXPERIENCE-002 requires
    before a tactic's Day7 evidence is eligible)."""
    conn.execute(
        """
        INSERT INTO own_publication_checks(
            check_id, publication_id, day_since_publish, like_count, comment_count, share_count, collect_count, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (check_id, publication_id, day_since_publish, like_count, comment_count, share_count, collect_count, run_id),
    )
    conn.commit()
    return check_id


def compute_own_publication_baseline(conn: sqlite3.Connection, *, account_id: str, metric: str, run_id: str) -> dict[str, Any]:
    """真实计算(不是猜测):从这个账号已有的、day_since_publish=0 那一天的历史
    own_publication_checks 样本里取中位数——原文档"29.1 当前默认"的正式启用
    门槛/滚动窗口都是20条完整P序列。样本不足20条时 is_formally_activated=0,
    sample_count 如实反映当前真实样本数,不假装已经够20条。"""
    if metric not in {"like_count", "comment_count", "collect_count", "share_count"}:
        raise ValueError(f"unsupported metric: {metric!r}")
    rows = conn.execute(
        f"""
        SELECT {metric} AS value
          FROM own_publication_checks
          JOIN own_publications ON own_publications.publication_id = own_publication_checks.publication_id
         WHERE own_publications.account_id = ?
           AND own_publication_checks.day_since_publish = 0
           AND own_publication_checks.{metric} IS NOT NULL
         ORDER BY own_publications.published_at DESC
         LIMIT 20
        """,  # noqa: S608 -- metric is validated against a fixed allowlist above, never user input
        (account_id,),
    ).fetchall()
    values = sorted(row["value"] for row in rows)
    sample_count = len(values)
    median_value = values[sample_count // 2] if sample_count % 2 == 1 else (
        (values[sample_count // 2 - 1] + values[sample_count // 2]) / 2 if sample_count else 0.0
    )
    is_formally_activated = 1 if sample_count >= 20 else 0
    baseline_id = f"{account_id}_{metric}_{run_id}"
    conn.execute(
        """
        INSERT INTO own_publication_baselines(baseline_id, account_id, metric, median_value, sample_count, is_formally_activated, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (baseline_id, account_id, metric, median_value, sample_count, is_formally_activated, run_id),
    )
    conn.commit()
    return {
        "baseline_id": baseline_id,
        "median_value": median_value,
        "sample_count": sample_count,
        "is_formally_activated": bool(is_formally_activated),
    }
