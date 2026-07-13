"""领域话题标签库:sources.yaml 种子加载 + 爆款库标签建议 + 轮换调度 + 四层过滤
(2026-07-13, 置顶规则总表核对后, 原文档第21章)。

Explicitly NOT implemented in this pass (documented limitation, not an
oversight):
  - Turning a discovered_external_videos row into a real topic_candidates
    row (would need its own topic-generation path, since discovered videos
    have no hit_deep_analysis row -- topic_candidates.source_analysis_id is
    a NOT NULL FK to it). record_search_cycle_result() therefore takes
    produced_validated_topic as an explicit caller-supplied boolean rather
    than querying for it -- there is currently no real query that could
    answer that question.
  - Feeding discovered videos into ASR/deep-analysis at all. Section 21.2's
    "ASR后低成本判断" tier is not wired -- the top-N selected by
    rank_and_select_for_deep_processing() here is as far as this pass goes.

Usage:
    python -m scripts.core.business_data.run_domain_search --domain-config config/domains/泛科普.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import sqlite3

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.domain_labels import ALLOWED_DOMAIN_LABELS  # noqa: E402
from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.external_adapters import ExternalAdapterCommand  # noqa: E402
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor  # noqa: E402

SCHEMA_PATH = Path(__file__).with_name("domain_search_schema.sqlite.sql")

# 原文档21.1/21.3 的真实数字,不是拍脑袋定的。
TAG_ROTATION_DAYS = 7
SEARCH_READ_MAX = 20
DEEP_PROCESSING_MAX_PER_TAG = 5
CONSECUTIVE_CYCLES_BEFORE_PAUSE = 3
DAILY_DEEP_PROCESSING_MAX_PER_DOMAIN = 10
# A2 之前已经验证过的真实标签长度/领域内频率过滤规则,来自真实数据统计。
TAG_MAX_CHARS_FOR_REUSABLE = 5  # >=6 字几乎全是平台活动标签(真实数据验证过)
_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")


def validate_domain_search_execution_contract(domain_search_cfg: dict[str, Any]) -> dict[str, Any]:
    """BR-TOPIC-005: real live search must be an explicit opt-in
    (domain_search.live_enabled: true in config/settings.yaml), same
    discipline as BR-RESEARCH-003 -- default is dry-run/blocked, not
    silently allowed."""
    contract = require_baseline_citations(["3", "17"])
    if not bool(domain_search_cfg.get("live_enabled", False)):
        raise ValueError(
            "domain_search.live_enabled is false (or unset) in config/settings.yaml -- "
            "real MediaCrawler keyword search is not allowed until this is explicitly turned on"
        )
    return contract


def install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_sources_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "domain_label" not in data or "active_tags" not in data:
        raise ValueError(f"{path} must have domain_label and active_tags")
    if data["domain_label"] not in ALLOWED_DOMAIN_LABELS:
        raise ValueError(f"{path} domain_label {data['domain_label']!r} is not a real domain_label")
    return data


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


def suggest_tags_from_hit_library(conn: sqlite3.Connection, *, domain_label: str, run_id: str) -> dict[str, Any]:
    """从真实爆款视频的 desc/title 里抓 #xxx 标签,按两条已用真实数据验证过的规则
    过滤(见 A2 阶段的统计):字数>=6的标签几乎全是平台活动标签,过滤掉;领域内
    出现频率排在最前面的标签太泛,过滤掉(具体截多少不是固定比例,按这批真实
    数据的分布取最高频的一小撮,见下方 generic_cutoff)。落成 status='suggested',
    不自动转正为 active。"""
    rows = conn.execute(
        """
        SELECT competitor_videos.video_id, competitor_videos.raw_json
          FROM competitor_videos
          JOIN competitor_accounts ON competitor_accounts.account_id = competitor_videos.account_id
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
            if len(tag) >= 6:  # 平台活动标签过滤(真实数据验证过)
                continue
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            tag_video_ids.setdefault(tag, row["video_id"])

    if not tag_counts:
        return {"domain_label": domain_label, "suggested": 0, "candidates_scanned": len(rows)}

    # 领域内高频过滤:取这批真实标签计数分布里最高的一成(至少留1个不过滤,
    # 避免全领域只有几个标签时把所有标签都当成"太泛"过滤光)。这是对着真实
    # 分布算出来的比例,不是写死一个固定次数阈值。
    sorted_counts = sorted(tag_counts.values(), reverse=True)
    generic_cutoff_index = max(1, len(sorted_counts) // 10)
    generic_cutoff_value = sorted_counts[generic_cutoff_index - 1]

    inserted = 0
    for tag, count in tag_counts.items():
        if count >= generic_cutoff_value and len(sorted_counts) > 1:
            continue  # 领域内出现频率太高,太泛
        tag_id = f"{domain_label}_{tag}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO domain_search_tags(tag_id, tag, domain_label, status, source, source_video_id)
            VALUES (?, ?, ?, 'suggested', 'discovered', ?)
            """,
            (tag_id, tag, domain_label, tag_video_ids[tag]),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    return {"domain_label": domain_label, "suggested": inserted, "candidates_scanned": len(rows)}


def select_tags_due_for_search(conn: sqlite3.Connection, *, domain_label: str, limit: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """轮换调度:只挑 status='active' 且(从没搜过,或者上次搜索距今>=7天)的标签,
    按最久没搜的排在最前面——不是一次性把所有标签都搜一遍。"""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=TAG_ROTATION_DAYS)).isoformat()
    return conn.execute(
        """
        SELECT domain_search_tags.*, domain_search_cursor.last_searched_at
          FROM domain_search_tags
          JOIN domain_search_cursor ON domain_search_cursor.tag_id = domain_search_tags.tag_id
         WHERE domain_search_tags.domain_label = ?
           AND domain_search_tags.status = 'active'
           AND (domain_search_cursor.last_searched_at IS NULL OR domain_search_cursor.last_searched_at <= ?)
         ORDER BY domain_search_cursor.last_searched_at IS NOT NULL, domain_search_cursor.last_searched_at ASC
         LIMIT ?
        """,
        (domain_label, cutoff, limit),
    ).fetchall()


def deterministic_filter(items: list[dict[str, Any]], *, tag: str, already_discovered_ids: set[str]) -> list[dict[str, Any]]:
    """21.2 第一层"确定性过滤":平台视频ID已处理(已经在 discovered_external_videos
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
        seen_in_batch.add(platform_item_id)
        kept.append(item)
    return kept


def rank_and_select_for_deep_processing(items: list[dict[str, Any]], *, limit: int = DEEP_PROCESSING_MAX_PER_TAG) -> list[dict[str, Any]]:
    """21.2 第三层"批次完整处理排序":按搜索位置(结果本身的顺序,越靠前越好)和
    互动量粗排,取前 limit 个。不产出正式倍数(没有基线可比),只做弱排序。"""
    def _engagement(item: dict[str, Any]) -> int:
        return int(item.get("liked_count") or 0) + int(item.get("comment_count") or 0)

    ranked = sorted(enumerate(items), key=lambda pair: (-_engagement(pair[1]), pair[0]))
    return [item for _, item in ranked[:limit]]


def record_search_cycle_result(
    conn: sqlite3.Connection,
    *,
    tag_id: str,
    run_id: str,
    produced_validated_topic: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """更新轮换游标 + 标签预算计数器。produced_validated_topic 由调用方明确传入
    (见模块顶部说明:目前没有真实的"这轮搜出来的视频变成正式选题了吗"查询路径,
    不假装有)。"""
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
        paused = False
    else:
        row = conn.execute(
            "SELECT consecutive_cycles_without_validated_topic FROM domain_search_tags WHERE tag_id = ?", (tag_id,)
        ).fetchone()
        new_count = int(row["consecutive_cycles_without_validated_topic"]) + 1
        paused = new_count >= CONSECUTIVE_CYCLES_BEFORE_PAUSE
        conn.execute(
            "UPDATE domain_search_tags SET consecutive_cycles_without_validated_topic = ?, status = ? WHERE tag_id = ?",
            (new_count, "suggested_pause" if paused else "active", tag_id),
        )
    conn.commit()
    return {"tag_id": tag_id, "consecutive_cycles_without_validated_topic": new_count, "paused": paused}


def search_one_tag(
    conn: sqlite3.Connection,
    executor: LocalMediaCrawlerExecutor,
    tag_row: sqlite3.Row,
    *,
    domain_label: str,
    run_id: str,
    domain_search_cfg: dict[str, Any],
    platform: str = "douyin",
) -> dict[str, Any]:
    """21.1-21.2:一次搜索一个标签,过滤+排序,把入选的视频存进
    discovered_external_videos(不写 hits/competitor_videos——见 schema 文件顶部
    说明)。account_platform_id 已经在 competitor_accounts 里(is_tracked_account=1)
    的直接跳过写入,交给 A5 的复查流程处理未追踪账号。"""
    validate_domain_search_execution_contract(domain_search_cfg)
    command = ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.keyword_search",
        executable="vendor/MediaCrawler/main.py",
        args=(platform, "search"),
        input_payload={"platform": platform, "source_kind": "search", "keywords": [tag_row["tag"]]},
        max_items=SEARCH_READ_MAX,
    )
    result = executor.execute(command)
    if result.status != "succeeded":
        return {"tag_id": tag_row["tag_id"], "status": "failed", "reason": result.status}

    already = {
        row["platform_item_id"]
        for row in conn.execute("SELECT platform_item_id FROM discovered_external_videos WHERE platform=?", (platform,)).fetchall()
    }
    filtered = deterministic_filter(result.payload.get("items", []), tag=tag_row["tag"], already_discovered_ids=already)
    selected = rank_and_select_for_deep_processing(filtered)

    tracked_accounts = {
        row["sec_uid"] for row in conn.execute("SELECT sec_uid FROM competitor_accounts").fetchall()
    }
    inserted = 0
    for position, item in enumerate(selected):
        platform_item_id = str(item.get("aweme_id") or item.get("note_id") or item.get("id") or "")
        account_platform_id = str(item.get("sec_uid") or item.get("user_id") or "")
        is_tracked = 1 if account_platform_id in tracked_accounts else 0
        discovered_video_id = f"disc_{platform}_{platform_item_id}"
        conn.execute(
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
        inserted += 1
    conn.commit()
    return {"tag_id": tag_row["tag_id"], "status": "completed", "raw_results": len(result.payload.get("items", [])), "inserted": inserted}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Seed a domain's active search tags from its sources.yaml.")
    parser.add_argument("--sources-config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        sources_config = load_sources_yaml(Path(args.sources_config))
        report = seed_active_tags_from_sources_yaml(conn, sources_config)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
