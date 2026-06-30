"""豆瓣采集通用件。

边界:
- 必须指定目标,不做无目标推荐流采集。
- 采集、过滤、入库、写文件和 Obsidian 都是确定性逻辑。
- 输出进入 language_fuel_items,仍是 raw_material,不直接进入范例库。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml
from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"
sys.path.insert(0, str(ROOT / "scripts"))

from language_fuel.obsidian import safe_name, write_language_fuel_note  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CHANNELS = {
    "movie": {
        "host": "movie.douban.com",
        "search_cat": "1002",
        "domain": "film",
        "domain_name": "影视",
        "subject_word": "影片/剧集",
    },
    "book": {
        "host": "book.douban.com",
        "search_cat": "1001",
        "domain": "book",
        "domain_name": "读书",
        "subject_word": "书籍",
    },
    "music": {
        "host": "music.douban.com",
        "search_cat": "1003",
        "domain": "music",
        "domain_name": "音乐",
        "subject_word": "音乐条目",
    },
}

PRIVACY_RE = re.compile(
    r"((?:1[3-9]\d{9})|(?:[\w.+-]+@[\w.-]+\.\w+)|(?:微信|vx|VX|qq|QQ)[:：]?\s*[A-Za-z0-9_-]{5,})"
)


def load_settings() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))


def douban_config() -> dict[str, Any]:
    return load_settings().get("language_fuel", {}).get("platforms", {}).get("douban", {})


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def strip_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", "", text).lower().replace("，", ",")


def parse_int(text: Any) -> int | None:
    if text is None:
        return None
    m = re.search(r"\d+", str(text).replace(",", ""))
    return int(m.group(0)) if m else None


def redact_privacy(text: str) -> tuple[str, str]:
    redacted = PRIVACY_RE.sub("[已脱敏]", text)
    return redacted, "redacted" if redacted != text else "clean"


def quality_status(content: str, source_kind: str, content_status: str) -> str:
    length = len(content)
    if content_status != "full":
        return "partial"
    if source_kind == "long_review" and length < 300:
        return "too_short"
    if source_kind in {"group_topic", "group_reply"} and length < 20:
        return "too_short"
    if source_kind == "short_comment" and length < 4:
        return "too_short"
    return "usable"


def item_id(channel: str, source_kind: str, raw_id: str) -> str:
    return f"{channel}:{source_kind}:{raw_id}"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_json(url: str, params: dict[str, str] | None = None, referer: str = "https://www.douban.com/") -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_text(url: str, params: dict[str, str] | None = None, referer: str = "https://www.douban.com/") -> str:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def search_subjects_fallback(channel: str, query: str) -> list[dict[str, Any]]:
    channel_cfg = CHANNELS[channel]
    body = fetch_text(
        "https://www.douban.com/search",
        {"cat": channel_cfg["search_cat"], "q": query},
        referer="https://www.douban.com/",
    )
    out: list[dict[str, Any]] = []
    host_pattern = re.escape(channel_cfg["host"]).replace(r"\.", r"(?:\.|%2E)")
    pattern = (
        r'<div class="result">[\s\S]*?url=https%3A%2F%2F'
        + host_pattern
        + r'%2Fsubject%2F(\d+)%2F[\s\S]*?<h3>[\s\S]*?<a [^>]+>([\s\S]*?)</a>'
        + r'[\s\S]*?<span class="subject-cast">([\s\S]*?)</span>'
    )
    for m in re.finditer(pattern, body):
        subject_id = m.group(1)
        out.append(
            {
                "id": subject_id,
                "title": strip_tags(m.group(2)),
                "url": f"https://{channel_cfg['host']}/subject/{subject_id}/",
                "sub_title": strip_tags(m.group(3)),
                "year": "",
                "type": channel,
            }
        )
    return out


def search_subjects(channel: str, query: str) -> list[dict[str, Any]]:
    channel_cfg = CHANNELS[channel]
    url = f"https://{channel_cfg['host']}/j/subject_suggest"
    try:
        data = fetch_json(url, {"q": query}, referer=f"https://{channel_cfg['host']}/")
        results = [item for item in data if item.get("id")]
    except Exception:
        results = []
    return results or search_subjects_fallback(channel, query)


def choose_subject(channel: str, query: str, subjects: list[dict[str, Any]]) -> dict[str, Any]:
    if not subjects:
        raise RuntimeError(f"没有搜索到豆瓣{CHANNELS[channel]['subject_word']}: {query}")
    normalized_query = normalize_title(query)
    exact = [
        item for item in subjects
        if normalize_title(item.get("title", "")) == normalized_query
        or normalize_title(item.get("sub_title", "")) == normalized_query
    ]
    if len(exact) == 1:
        return exact[0]
    if len(subjects) == 1:
        return subjects[0]
    lines = ["搜索结果不唯一,请改用 --subject-id 指定其中一个:"]
    for idx, item in enumerate(subjects[:10], 1):
        lines.append(
            f"{idx}. id={item.get('id')} | {item.get('title')} | {item.get('year', '')} | {item.get('sub_title', '')}"
        )
    raise RuntimeError("\n".join(lines))


@contextmanager
def browser_page(headless: bool = True) -> Iterator[Page]:
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(user_agent=UA, locale="zh-CN")
            yield page
        finally:
            browser.close()


def base_item(
    *,
    channel: str,
    source_kind: str,
    raw_id: str,
    domain: str,
    title: str,
    subtitle: str,
    url: str,
    source_url: str,
    author: str,
    publish_time: str,
    like_count: int | None,
    comment_count: int | None,
    content: str,
    content_status: str,
    raw_json: dict[str, Any],
) -> dict[str, Any]:
    content, privacy_status = redact_privacy(content)
    return {
        "platform": "douban",
        "channel": channel,
        "platform_item_id": item_id(channel, source_kind, raw_id),
        "source_kind": source_kind,
        "domain": domain,
        "title": title,
        "subtitle": subtitle,
        "url": url,
        "source_url": source_url,
        "author": author,
        "publish_time": publish_time,
        "like_count": like_count,
        "comment_count": comment_count,
        "content": content,
        "content_status": content_status,
        "word_count": len(content),
        "content_hash": content_hash(content),
        "quality_status": quality_status(content, source_kind, content_status),
        "privacy_status": privacy_status,
        "item_type": source_kind,
        "raw_json": raw_json,
    }


def store_items(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    platform: str,
    source_kind: str,
    domain: str,
    source_query: str,
    requested_limit: int,
    obsidian_note: str | None,
) -> int:
    changed = 0
    with conn:
        for item in items:
            cur = conn.execute(
                """INSERT INTO language_fuel_items
                   (platform, channel, platform_item_id, source_kind, domain, title, subtitle,
                    url, source_url, author, publish_time, like_count, comment_count,
                    content, content_status, word_count, content_hash, quality_status,
                    privacy_status, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform, platform_item_id) DO UPDATE SET
                     channel=excluded.channel,
                     source_kind=excluded.source_kind,
                     domain=excluded.domain,
                     title=excluded.title,
                     subtitle=excluded.subtitle,
                     url=excluded.url,
                     source_url=excluded.source_url,
                     author=excluded.author,
                     publish_time=excluded.publish_time,
                     like_count=excluded.like_count,
                     comment_count=excluded.comment_count,
                     content=excluded.content,
                     content_status=excluded.content_status,
                     word_count=excluded.word_count,
                     content_hash=excluded.content_hash,
                     quality_status=excluded.quality_status,
                     privacy_status=excluded.privacy_status,
                     raw_json=excluded.raw_json,
                     collected_at=datetime('now','localtime')""",
                (
                    item["platform"],
                    item["channel"],
                    item["platform_item_id"],
                    item["source_kind"],
                    item["domain"],
                    item["title"],
                    item["subtitle"],
                    item["url"],
                    item["source_url"],
                    item["author"],
                    item["publish_time"],
                    item["like_count"],
                    item["comment_count"],
                    item["content"],
                    item["content_status"],
                    item["word_count"],
                    item["content_hash"],
                    item["quality_status"],
                    item["privacy_status"],
                    json.dumps(item["raw_json"], ensure_ascii=False),
                ),
            )
            changed += cur.rowcount
        conn.execute(
            """INSERT INTO language_fuel_batches
               (platform, source_kind, domain, source_query, requested_limit, fetched_count,
                kept_count, obsidian_note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (platform, source_kind, domain, source_query, requested_limit, len(items), len(items), obsidian_note),
        )
    return changed


def write_data_file(items: list[dict[str, Any]], out_root: Path, source_name: str, source_kind: str) -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    path = out_root / f"douban_{safe_name(source_name, 'source')}_{source_kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "platform": "douban",
        "source_kind": source_kind,
        "source": source_name,
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_obsidian(items: list[dict[str, Any]], source_name: str, title: str, source_kind: str, domain_name: str) -> Path:
    settings = load_settings()
    language_fuel = settings.get("language_fuel", {})
    vault_root = ROOT / settings.get("paths", {}).get("vault", "vault")
    return write_language_fuel_note(
        vault_root=vault_root,
        vault_dir=language_fuel.get("vault_dir", "语感燃料"),
        platform="douban",
        platform_name="豆瓣",
        domain=items[0]["domain"] if items else "unknown",
        domain_name=domain_name,
        source=source_name,
        source_kind=source_kind,
        item_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
        title=title,
        subtitle="指定目标采集",
        url=items[0].get("source_url") if items else "",
        items=items,
        total_items=len(items),
        sample_heading="原料样本",
        extra_frontmatter={
            "channel": items[0].get("channel") if items else None,
            "content_status": items[0].get("content_status") if items else None,
        },
        extra_sections=[
            "## 采集说明",
            "- 本笔记来自豆瓣指定目标采集,不是无目标热门榜采集。",
            "- 仍是原料层,后续需要提炼/晋升才进入范例库。",
        ],
    )
