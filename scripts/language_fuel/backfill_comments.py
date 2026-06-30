"""Backfill the shared real-comment corpus from existing raw tables.

This is a deterministic normalization layer. It does not crawl platforms and
does not promote comments into examples. It only makes short human reactions
searchable through language_fuel_comments while keeping source pointers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"

sys.stdout.reconfigure(encoding="utf-8")

COMMENT_SOURCE_KINDS = {
    "short_comment",
    "comment",
    "reply",
    "group_reply",
    "sub_comment",
}


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def content_hash(text: str) -> str:
    key = re.sub(r"\s+", "", text)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def quality_status(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 8:
        return "too_short"
    if len(set(compact)) <= 3:
        return "low_signal"
    return "raw"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def insert_comment(conn: sqlite3.Connection, item: dict) -> bool:
    content = normalize_text(item.get("content"))
    if not content:
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO language_fuel_comments
           (platform, channel, domain, source_table, source_id, parent_source_id,
            parent_platform_id, platform_comment_id, source_kind, source_url,
            author, user_id, publish_time, like_count, reply_count, content,
            content_hash, quality_status, privacy_status, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item["platform"],
            item.get("channel"),
            item.get("domain") or "general",
            item["source_table"],
            str(item["source_id"]),
            item.get("parent_source_id"),
            item.get("parent_platform_id"),
            item.get("platform_comment_id"),
            item.get("source_kind") or "comment",
            item.get("source_url"),
            item.get("author"),
            item.get("user_id"),
            item.get("publish_time"),
            item.get("like_count"),
            item.get("reply_count"),
            content,
            content_hash(content),
            item.get("quality_status") or quality_status(content),
            item.get("privacy_status") or "clean",
            json.dumps(item.get("raw_json") or {}, ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


def rows_from_language_fuel_items(conn: sqlite3.Connection) -> Iterable[dict]:
    rows = conn.execute(
        """SELECT * FROM language_fuel_items
           WHERE source_kind IN ({})
              OR (platform='douban' AND source_kind='short_comment')""".format(
            ",".join("?" for _ in COMMENT_SOURCE_KINDS)
        ),
        tuple(COMMENT_SOURCE_KINDS),
    ).fetchall()
    for row in rows:
        yield {
            "platform": row["platform"],
            "channel": row["channel"],
            "domain": row["domain"],
            "source_table": "language_fuel_items",
            "source_id": row["id"],
            "parent_source_id": row["source_url"],
            "parent_platform_id": row["platform_item_id"],
            "platform_comment_id": row["platform_item_id"],
            "source_kind": row["source_kind"],
            "source_url": row["source_url"] or row["url"],
            "author": row["author"],
            "publish_time": row["publish_time"],
            "like_count": row["like_count"],
            "reply_count": row["comment_count"],
            "content": row["content"],
            "quality_status": row["quality_status"],
            "privacy_status": row["privacy_status"],
            "raw_json": {"source_row": dict(row)},
        }


def rows_from_music_comments(conn: sqlite3.Connection) -> Iterable[dict]:
    rows = conn.execute(
        """SELECT c.*, t.platform_track_id, t.name AS track_name, t.url AS track_url
           FROM music_comments c
           JOIN music_tracks t ON t.id = c.track_id"""
    ).fetchall()
    for row in rows:
        yield {
            "platform": row["platform"],
            "channel": "netease",
            "domain": "music",
            "source_table": "music_comments",
            "source_id": row["id"],
            "parent_source_id": row["track_id"],
            "parent_platform_id": row["platform_track_id"],
            "platform_comment_id": row["platform_comment_id"],
            "source_kind": "short_comment",
            "source_url": row["track_url"],
            "author": row["user_nickname"],
            "user_id": row["user_id"],
            "publish_time": row["comment_time"],
            "like_count": row["like_count"],
            "reply_count": row["reply_count"],
            "content": row["content"],
            "raw_json": json.loads(row["raw_json"] or "{}"),
        }


def rows_from_hit_comments(conn: sqlite3.Connection) -> Iterable[dict]:
    rows = conn.execute(
        """SELECT c.*, h.id AS source_hit_id, h.platform_item_id, h.url,
                  ca.domain AS account_domain
           FROM hit_comments c
           JOIN hits h ON h.id = c.hit_id
           JOIN competitor_accounts ca ON ca.id = h.competitor_id"""
    ).fetchall()
    for row in rows:
        yield {
            "platform": "douyin",
            "channel": "hit_comments",
            "domain": row["account_domain"] or "general",
            "source_table": "hit_comments",
            "source_id": row["id"],
            "parent_source_id": row["source_hit_id"],
            "parent_platform_id": row["platform_item_id"],
            "platform_comment_id": row["comment_id"],
            "source_kind": "short_comment",
            "source_url": row["url"],
            "publish_time": row["comment_time"],
            "like_count": row["like_count"],
            "reply_count": row["sub_comment_count"],
            "content": row["content"],
            "raw_json": {"ip_location": row["ip_location"], "parent_comment_id": row["parent_comment_id"]},
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=["all", "language_fuel_items", "music_comments", "hit_comments"],
        default="all",
    )
    args = parser.parse_args()

    conn = connect()
    loaders = {
        "language_fuel_items": rows_from_language_fuel_items,
        "music_comments": rows_from_music_comments,
        "hit_comments": rows_from_hit_comments,
    }
    selected = loaders if args.source == "all" else {args.source: loaders[args.source]}

    total = 0
    inserted = 0
    with conn:
        for name, loader in selected.items():
            source_total = 0
            source_inserted = 0
            for item in loader(conn):
                source_total += 1
                if insert_comment(conn, item):
                    source_inserted += 1
            total += source_total
            inserted += source_inserted
            print(f"{name}: scanned {source_total}, inserted {source_inserted}")
    conn.close()
    print(f"language_fuel_comments backfill complete: scanned {total}, inserted {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
