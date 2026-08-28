"""领域话题标签库、爆款标签建议、每日轮换和单页搜索来源留存。

搜索只产生可追溯来源记录，不直接建立候选、研究或经验对象。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import sqlite3

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.domain_labels import (  # noqa: E402
    ALLOWED_DOMAIN_LABELS,
    formal_domain_labels,
    get_discovery_policy,
)
from scripts.core.external_adapters import ExternalAdapterCommand, ExternalCommandExecutor  # noqa: E402

SCHEMA_PATH = Path(__file__).with_name("domain_search_schema.sqlite.sql")

DAILY_TAG_SEARCH_COUNT = 3
SEARCH_PAGE_COUNT = 1
TAG_CANDIDATE_LIKE_FLOOR = 10000
# MediaCrawler 的抖音搜索每页为 10 条；max_items=10 只触发第一页。
DOUYIN_FIRST_PAGE_MAX_ITEMS = 10
CONSECUTIVE_CYCLES_BEFORE_REVIEW = 3
_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")


def validate_domain_search_execution_contract(domain_search_cfg: dict[str, Any]) -> None:
    """BR-TOPIC-005: real live search must be an explicit opt-in
    (domain_search.live_enabled: true in config/external_collection.yaml), same
    discipline as BR-RESEARCH-003 -- default is dry-run/blocked, not
    silently allowed."""
    if not bool(domain_search_cfg.get("live_enabled", False)):
        raise ValueError(
            "domain_search.live_enabled is false (or unset) in config/external_collection.yaml -- "
            "real MediaCrawler keyword search is not allowed until this is explicitly turned on"
        )
    if int(domain_search_cfg.get("daily_tag_count", DAILY_TAG_SEARCH_COUNT)) != DAILY_TAG_SEARCH_COUNT:
        raise ValueError("domain_search.daily_tag_count must remain 3 under the effective baseline")
    if int(domain_search_cfg.get("page_count_per_tag", SEARCH_PAGE_COUNT)) != SEARCH_PAGE_COUNT:
        raise ValueError("domain_search.page_count_per_tag must remain 1 under the effective baseline")
    if int(domain_search_cfg.get("candidate_like_floor", TAG_CANDIDATE_LIKE_FLOOR)) != TAG_CANDIDATE_LIKE_FLOOR:
        raise ValueError("domain_search.candidate_like_floor must remain 10000 under the effective baseline")


def install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_sources_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    domain_label = str(data.get("formal_domain_label") or data.get("domain_label") or "").strip()
    topic_search = ((data.get("discovery") or {}).get("topic_search") or {})
    active_tags = data.get("active_tags", topic_search.get("active_tags"))
    if not domain_label or not isinstance(active_tags, list):
        raise ValueError(f"{path} must define formal_domain_label and discovery.topic_search.active_tags")
    if domain_label not in formal_domain_labels():
        raise ValueError(f"{path} domain_label {domain_label!r} is not a configured formal domain")
    return {**data, "domain_label": domain_label, "active_tags": active_tags}


def register_activity_tag_exclusion(
    conn: sqlite3.Connection,
    *,
    registry_id: str,
    domain_label: str,
    tag: str,
    platform: str,
    activity_identity: str,
    evidence_ref: str,
    valid_from: str,
    valid_until: str,
    exclusion_reason: str,
) -> None:
    """Register the evidence required for an exact platform-activity exclusion."""
    if domain_label not in formal_domain_labels():
        raise ValueError("activity tag exclusion requires a configured formal domain")
    required = (registry_id, tag, platform, activity_identity, evidence_ref, valid_from, valid_until, exclusion_reason)
    if any(not str(value).strip() for value in required):
        raise ValueError("activity tag exclusion requires identity, evidence, dates, and reason")
    if datetime.fromisoformat(valid_until) < datetime.fromisoformat(valid_from):
        raise ValueError("activity tag exclusion valid_until must not precede valid_from")
    conn.execute(
        """
        INSERT INTO domain_search_activity_tag_registry(
            registry_id, domain_label, tag, platform, activity_identity, evidence_ref,
            valid_from, valid_until, exclusion_reason, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (registry_id, domain_label, tag, platform, activity_identity, evidence_ref,
         valid_from, valid_until, exclusion_reason),
    )
    conn.commit()


def _has_active_activity_evidence(
    conn: sqlite3.Connection,
    *,
    domain_label: str,
    tag: str,
    platform: str,
    now: datetime,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM domain_search_activity_tag_registry
         WHERE domain_label=? AND tag=? AND platform=? AND status='active'
           AND valid_from<=? AND valid_until>=?
         LIMIT 1
        """,
        (domain_label, tag, platform, now.isoformat(), now.isoformat()),
    ).fetchone()
    return row is not None


def seed_active_tags_from_sources_yaml(conn: sqlite3.Connection, sources_config: dict[str, Any]) -> dict[str, Any]:
    """幂等:同一个 (domain_label, tag) 重复导入不会产生第二行。这是"读取配置、
    建立初始active标签"的加载逻辑,不是自动提取——标签内容完全来自人工在
    sources.yaml 里写的 active_tags 列表。"""
    domain_label = sources_config["domain_label"]
    inserted = 0
    for tag in sources_config["active_tags"]:
        tag_id = f"{domain_label}_{tag}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO domain_search_tags(tag_id, tag, domain_label, status, source, human_review_status)
            VALUES (?, ?, ?, 'active', 'sources_yaml', 'approved')
            """,
            (tag_id, tag, domain_label),
        )
        if cur.rowcount:
            inserted += 1
            conn.execute("INSERT OR IGNORE INTO domain_search_cursor(tag_id) VALUES (?)", (tag_id,))
    conn.commit()
    return {"domain_label": domain_label, "inserted": inserted, "total_configured": len(sources_config["active_tags"])}


def extract_hashtags(text: str) -> list[str]:
    return [tag for tag in _HASHTAG_PATTERN.findall(text) if tag.strip()]


def _like_count(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("liked_count") or item.get("like_count") or 0))
    except (TypeError, ValueError):
        return 0


def suggest_tags_from_hit_library(
    conn: sqlite3.Connection,
    *,
    domain_label: str,
    run_id: str,
    platform: str = "douyin",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extract domain tags; activity words only trigger review, never exclusion."""
    del run_id
    policy = get_discovery_policy(domain_label)
    topic_policy = policy.get("topic_search") or {}
    generic_tags = frozenset(str(tag) for tag in topic_policy.get("generic_tags", []))
    activity_review_terms = tuple(str(term) for term in topic_policy.get("activity_review_terms", []))
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """
        SELECT competitor_videos.video_id, competitor_videos.raw_json
           FROM hits
           JOIN competitor_videos ON competitor_videos.video_id = hits.video_id
           JOIN competitor_accounts ON competitor_accounts.account_id = competitor_videos.account_id
           JOIN stage0_content_account formal_account
             ON formal_account.content_account_id = competitor_accounts.account_id
            AND formal_account.data_identity = 'production'
            AND formal_account.account_role = 'competitor'
            AND formal_account.status = 'active'
         WHERE competitor_accounts.domain_label = ?
        """,
        (domain_label,),
    ).fetchall()

    tag_video_ids: dict[str, str] = {}
    tag_counts: dict[str, int] = {}
    for row in rows:
        raw = json.loads(row["raw_json"])
        text = raw.get("desc") or raw.get("title") or ""
        for tag in extract_hashtags(text):
            tag = tag.strip("，。！？、,.!? ")
            if not tag or tag in generic_tags:
                continue
            if _has_active_activity_evidence(
                conn, domain_label=domain_label, tag=tag, platform=platform, now=now
            ):
                continue
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            tag_video_ids.setdefault(tag, row["video_id"])

    if not tag_counts:
        return {"domain_label": domain_label, "suggested": 0, "pending_review": 0, "candidates_scanned": len(rows)}

    inserted = 0
    pending_review = 0
    for tag, count in tag_counts.items():
        del count
        requires_activity_review = any(term in tag for term in activity_review_terms)
        status = "pending_review" if requires_activity_review else "suggested"
        tag_id = f"{domain_label}_{tag}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO domain_search_tags(tag_id, tag, domain_label, status, source, source_video_id)
            VALUES (?, ?, ?, ?, 'discovered', ?)
            """,
            (tag_id, tag, domain_label, status, tag_video_ids[tag]),
        )
        if cur.rowcount:
            inserted += 1
            pending_review += int(requires_activity_review)
    conn.commit()
    return {
        "domain_label": domain_label,
        "suggested": inserted - pending_review,
        "pending_review": pending_review,
        "candidates_scanned": len(rows),
    }
def select_tags_due_for_search(conn: sqlite3.Connection, *, domain_label: str, limit: int = DAILY_TAG_SEARCH_COUNT, now: datetime | None = None) -> list[sqlite3.Row]:
    """每天按从未搜索、最久未搜索的稳定顺序轮换最多三个活跃标签。"""
    del now
    effective_limit = min(max(int(limit), 0), DAILY_TAG_SEARCH_COUNT)
    return conn.execute(
        """
        SELECT domain_search_tags.*, domain_search_cursor.last_searched_at
          FROM domain_search_tags
          JOIN domain_search_cursor ON domain_search_cursor.tag_id = domain_search_tags.tag_id
         WHERE domain_search_tags.domain_label = ?
           AND domain_search_tags.status = 'active'
         ORDER BY domain_search_cursor.last_searched_at IS NOT NULL,
                  domain_search_cursor.last_searched_at ASC,
                  domain_search_tags.tag_id ASC
         LIMIT ?
        """,
        (domain_label, effective_limit),
    ).fetchall()


def deterministic_filter(items: list[dict[str, Any]], *, tag: str, already_discovered_ids: set[str]) -> list[dict[str, Any]]:
    """确定性过滤：平台视频ID已处理(已经在 discovered_external_videos
    或本批次内重复)则跳过;标题/描述完全没命中当前标签本身也跳过(搜索结果偶尔
    会有跑题的)。"""
    kept: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()
    for item in items:
        platform_item_id = str(item.get("aweme_id") or item.get("note_id") or item.get("id") or "")
        if not platform_item_id or platform_item_id in already_discovered_ids or platform_item_id in seen_in_batch:
            continue
        text = str(item.get("desc") or item.get("title") or "")
        if tag not in text:
            continue
        if _like_count(item) < TAG_CANDIDATE_LIKE_FLOOR:
            continue
        seen_in_batch.add(platform_item_id)
        kept.append(item)
    return kept


def record_search_cycle_result(
    conn: sqlite3.Connection,
    *,
    tag_id: str,
    run_id: str,
    produced_validated_topic: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """更新轮换游标和人工查看计数器；标签绝不因计数被自动暂停。"""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    conn.execute(
        "UPDATE domain_search_cursor SET last_searched_at = ?, run_id = ? WHERE tag_id = ?",
        (now_iso, run_id, tag_id),
    )
    if produced_validated_topic:
        conn.execute(
            "UPDATE domain_search_tags SET consecutive_cycles_without_validated_topic = 0 WHERE tag_id = ?",
            (tag_id,),
        )
        new_count = 0
        requires_human_review = False
    else:
        row = conn.execute(
            "SELECT consecutive_cycles_without_validated_topic FROM domain_search_tags WHERE tag_id = ?", (tag_id,)
        ).fetchone()
        new_count = int(row["consecutive_cycles_without_validated_topic"]) + 1
        requires_human_review = new_count >= CONSECUTIVE_CYCLES_BEFORE_REVIEW
        conn.execute(
            "UPDATE domain_search_tags SET consecutive_cycles_without_validated_topic = ? WHERE tag_id = ?",
            (new_count, tag_id),
        )
    conn.commit()
    return {
        "tag_id": tag_id,
        "consecutive_cycles_without_validated_topic": new_count,
        "requires_human_review": requires_human_review,
    }


def create_discovered_account_reviews(
    conn: sqlite3.Connection,
    *,
    domain_label: str,
    run_id: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Create one human-review item when an unregistered account has three real videos in 30 days."""
    observed_at = now or datetime.now(timezone.utc)
    cutoff = (observed_at - timedelta(days=30)).isoformat()
    rows = conn.execute(
        """
        SELECT account_platform_id, MAX(account_handle) AS account_handle,
               COUNT(DISTINCT platform_item_id) AS video_count
          FROM discovered_external_videos
         WHERE domain_label=? AND is_tracked_account=0
           AND account_platform_id IS NOT NULL AND account_platform_id<>''
           AND discovered_at>=?
         GROUP BY account_platform_id
        HAVING COUNT(DISTINCT platform_item_id)>=3
        """,
        (domain_label, cutoff),
    ).fetchall()
    created: list[dict[str, Any]] = []
    for row in rows:
        existing = conn.execute(
            "SELECT disposition FROM discovered_account_review WHERE account_platform_id=? AND domain_label=?",
            (row["account_platform_id"], domain_label),
        ).fetchone()
        if existing is not None:
            continue
        review_id = f"account_review_{domain_label}_{row['account_platform_id']}"
        conn.execute(
            """
            INSERT INTO discovered_account_review(
                review_id, account_platform_id, account_handle, domain_label, video_count, run_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (review_id, row["account_platform_id"], row["account_handle"], domain_label, row["video_count"], run_id),
        )
        created.append({"review_id": review_id, "account_platform_id": row["account_platform_id"], "video_count": row["video_count"]})
    conn.commit()
    return created


def list_discovered_account_review_material(conn: sqlite3.Connection, *, review_id: str) -> dict[str, Any]:
    """Show the triggering count and the latest ten accessible videos for one human decision."""
    review = conn.execute("SELECT * FROM discovered_account_review WHERE review_id=?", (review_id,)).fetchone()
    if review is None:
        raise ValueError("discovered account review was not found")
    videos = conn.execute(
        """
        SELECT platform_item_id, title, url, discovered_at
          FROM discovered_external_videos
         WHERE account_platform_id=? AND domain_label=? AND is_tracked_account=0
           AND COALESCE(url, '')<>''
         ORDER BY discovered_at DESC, platform_item_id ASC
         LIMIT 10
        """,
        (review["account_platform_id"], review["domain_label"]),
    ).fetchall()
    return {
        "review_id": review["review_id"],
        "account_platform_id": review["account_platform_id"],
        "video_count": review["video_count"],
        "videos": [dict(video) for video in videos],
    }


def search_one_tag(
    conn: sqlite3.Connection,
    executor: ExternalCommandExecutor,
    tag_row: sqlite3.Row,
    *,
    domain_label: str,
    run_id: str,
    domain_search_cfg: dict[str, Any],
    platform: str = "douyin",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """一次搜索一个标签的第 1 页，先留存整页，再把合格视频写入
    discovered_external_videos(不写 hits/competitor_videos——见 schema 文件顶部
    说明)。account_platform_id 已经在 competitor_accounts 里(is_tracked_account=1)
    的直接跳过写入,交给 A5 的复查流程处理未追踪账号。"""
    validate_domain_search_execution_contract(domain_search_cfg)
    command = ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.keyword_search",
        executable="vendor/MediaCrawler/main.py",
        args=(platform, "search"),
        input_payload={"platform": platform, "source_kind": "search", "keywords": [tag_row["tag"]], "page_count": SEARCH_PAGE_COUNT},
        max_items=DOUYIN_FIRST_PAGE_MAX_ITEMS,
        timeout_seconds=max(int(timeout_seconds), 1),
    )
    result = executor.execute(command)
    if result.status != "succeeded":
        return {"tag_id": tag_row["tag_id"], "status": "failed", "reason": result.status}

    already = {
        row["platform_item_id"]
        for row in conn.execute("SELECT platform_item_id FROM discovered_external_videos WHERE platform=?", (platform,)).fetchall()
    }
    page_items = result.payload.get("items", [])
    if not isinstance(page_items, list):
        return {"tag_id": tag_row["tag_id"], "status": "failed", "reason": "invalid_items_payload"}

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    tracked_accounts = (
        {row["sec_uid"] for row in conn.execute(
            "SELECT account.sec_uid FROM competitor_accounts account "
            "JOIN stage0_content_account formal_account "
            "ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity='production' "
            "AND formal_account.account_role='competitor' AND formal_account.status='active'"
        ).fetchall()}
        if "competitor_accounts" in tables else set()
    )
    eligible_ids = {
        str(item.get("aweme_id") or item.get("note_id") or item.get("id") or "")
        for item in deterministic_filter(page_items, tag=tag_row["tag"], already_discovered_ids=already)
        if str(item.get("sec_uid") or item.get("user_id") or "") not in tracked_accounts
    }
    seen_in_page: set[str] = set()
    for position, item in enumerate(page_items):
        platform_item_id = str(item.get("aweme_id") or item.get("note_id") or item.get("id") or "")
        account_platform_id = str(item.get("sec_uid") or item.get("user_id") or "")
        text = str(item.get("desc") or item.get("title") or "")
        if not platform_item_id:
            outcome, reason = "excluded", "missing_platform_item_id"
        elif platform_item_id in already:
            outcome, reason = "excluded", "already_discovered"
        elif platform_item_id in seen_in_page:
            outcome, reason = "excluded", "duplicate_in_page"
        elif account_platform_id and account_platform_id in tracked_accounts:
            outcome, reason = "excluded", "tracked_competitor_account"
        elif tag_row["tag"] not in text:
            outcome, reason = "excluded", "tag_mismatch"
        elif _like_count(item) < TAG_CANDIDATE_LIKE_FLOOR:
            outcome, reason = "excluded", "below_tag_candidate_like_floor"
        else:
            outcome, reason = "eligible", "eligible"
        seen_in_page.add(platform_item_id)
        observation_id = f"search_obs_{run_id}_{tag_row['tag_id']}_{position}"
        conn.execute(
            """
            INSERT OR IGNORE INTO domain_search_page_observation(
                observation_id, platform, platform_item_id, tag_id, domain_label, page_number,
                search_position, title, url, filter_outcome, filter_reason, raw_json, run_id
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (observation_id, platform, platform_item_id or None, tag_row["tag_id"], domain_label,
             position, item.get("title") or item.get("desc"), item.get("aweme_url"), outcome,
             reason, json.dumps(item, ensure_ascii=False), run_id),
        )

    inserted = 0
    for position, item in enumerate(page_items):
        platform_item_id = str(item.get("aweme_id") or item.get("note_id") or item.get("id") or "")
        if platform_item_id not in eligible_ids:
            continue
        account_platform_id = str(item.get("sec_uid") or item.get("user_id") or "")
        is_tracked = 1 if account_platform_id in tracked_accounts else 0
        if is_tracked:
            continue
        discovered_video_id = f"disc_{platform}_{platform_item_id}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO discovered_external_videos(
                discovered_video_id, platform, platform_item_id, account_handle, account_platform_id,
                is_tracked_account, tag_id, domain_label, title, url, like_count, comment_count,
                share_count, collect_count, search_position, raw_json, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discovered_video_id, platform, platform_item_id, item.get("nickname"), account_platform_id,
                is_tracked, tag_row["tag_id"], domain_label, item.get("title") or item.get("desc"), item.get("aweme_url"),
                item.get("liked_count"), item.get("comment_count"), item.get("share_count"), item.get("collected_count"),
                position, json.dumps(item, ensure_ascii=False), run_id,
            ),
        )
        inserted += int(cur.rowcount > 0)
    conn.commit()
    account_reviews = create_discovered_account_reviews(conn, domain_label=domain_label, run_id=run_id)
    return {
        "tag_id": tag_row["tag_id"], "status": "completed", "page_number": 1,
        "raw_results": len(page_items), "inserted": inserted, "account_reviews_created": account_reviews,
    }


def run_daily_tag_searches(
    conn: sqlite3.Connection,
    executor: ExternalCommandExecutor,
    *,
    domain_label: str,
    run_id: str,
    domain_search_cfg: dict[str, Any],
    now: datetime | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Run at most three one-page tag searches sequentially, without retries or padding."""
    # Accepted tags from the cold-start library are written to
    # domain_search_tags, while the search cursor is created only for tags
    # coming from the static domain configuration.  Repair the missing cursor
    # rows at the actual production search boundary so the approved library
    # participates in daily rotation as intended.  INSERT OR IGNORE keeps this
    # idempotent and preserves existing last_searched_at values.
    conn.execute(
        """
        INSERT OR IGNORE INTO domain_search_cursor(tag_id)
        SELECT tag_id
          FROM domain_search_tags
         WHERE domain_label=? AND status='active'
        """,
        (domain_label,),
    )
    conn.commit()
    selected = select_tags_due_for_search(conn, domain_label=domain_label, limit=DAILY_TAG_SEARCH_COUNT, now=now)
    results: list[dict[str, Any]] = []
    for tag_row in selected:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            results.append({"tag_id": tag_row["tag_id"], "status": "timed_out", "reason": "batch deadline reached; no request was sent or retried"})
            break
        remaining_seconds = 60
        if deadline_monotonic is not None:
            remaining_seconds = max(1, min(60, int(deadline_monotonic - time.monotonic())))
        result = search_one_tag(
            conn, executor, tag_row, domain_label=domain_label, run_id=run_id,
            domain_search_cfg=domain_search_cfg,
            timeout_seconds=remaining_seconds,
        )
        results.append(result)
        conn.execute(
            "UPDATE domain_search_cursor SET last_searched_at=?, run_id=? WHERE tag_id=?",
            ((now or datetime.now(timezone.utc)).isoformat(), run_id, tag_row["tag_id"]),
        )
        conn.commit()
    timed_out = any(result["status"] == "timed_out" or result.get("reason") == "failed_timeout" for result in results)
    failed = sum(result["status"] != "completed" for result in results)
    return {
        "status": "timed_out" if timed_out else ("completed_with_failures" if failed else "completed"),
        "domain_label": domain_label,
        "selected_tag_count": len(selected),
        "tags": [row["tag"] for row in selected],
        "results": results,
        "failed": failed,
    }
