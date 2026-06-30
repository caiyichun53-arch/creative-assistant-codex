"""豆瓣小组帖子/回复采集。

小组内容边界更敏感,默认要求配置白名单:
  config/settings.yaml -> language_fuel.platforms.douban.channels.group.group_whitelist

临时人工测试可加 --allow-unlisted-group。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from language_fuel.douban.common import (  # noqa: E402
    base_item,
    browser_page,
    connect,
    douban_config,
    parse_int,
    store_items,
    write_data_file,
    write_obsidian,
)

GROUP_DOMAIN = "community"
GROUP_DOMAIN_NAME = "小组"


def normalize_topic_url(url: str) -> str:
    m = re.search(r"https?://www\.douban\.com/group/topic/\d+/?", url)
    if not m:
        raise RuntimeError(f"不是合法豆瓣小组 topic URL: {url}")
    return m.group(0).rstrip("/") + "/"


def group_id_from_url(url: str) -> str | None:
    m = re.search(r"https?://www\.douban\.com/group/([^/?#]+)/?", url)
    if not m:
        return None
    value = m.group(1)
    return None if value == "topic" else value


def topic_id_from_url(url: str) -> str:
    m = re.search(r"/group/topic/(\d+)/", url)
    if not m:
        raise RuntimeError(f"无法解析 topic id: {url}")
    return m.group(1)


def assert_group_allowed(group_id: str | None, allow_unlisted: bool) -> None:
    cfg = douban_config().get("channels", {}).get("group", {})
    whitelist = {str(x) for x in cfg.get("group_whitelist", [])}
    require = bool(cfg.get("require_whitelist", True))
    if require and group_id and group_id not in whitelist and not allow_unlisted:
        raise RuntimeError(f"小组 {group_id} 不在白名单。确认要采集时加配置白名单,或本次手动加 --allow-unlisted-group。")


def list_topic_urls(group_id: str, limit: int, keyword: str | None, show: bool, allow_unlisted: bool) -> list[str]:
    assert_group_allowed(group_id, allow_unlisted)
    urls: list[str] = []
    seen: set[str] = set()
    start = 0
    with browser_page(headless=not show) as page:
        while len(urls) < limit:
            url = f"https://www.douban.com/group/{group_id}/discussion?start={start}"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000)
            rows = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/group/topic/"]')).map(a => ({
                    href: a.href,
                    text: a.textContent.trim()
                }))"""
            )
            if not rows:
                break
            added = 0
            for row in rows:
                href = normalize_topic_url(row["href"])
                if href in seen:
                    continue
                if keyword and keyword not in row.get("text", ""):
                    continue
                seen.add(href)
                urls.append(href)
                added += 1
                if len(urls) >= limit:
                    break
            if added == 0 and start > 0:
                break
            start += 25
    return urls


def collect_topic(url: str, include_replies: bool, show: bool) -> list[dict[str, Any]]:
    topic_url = normalize_topic_url(url)
    topic_id = topic_id_from_url(topic_url)
    with browser_page(headless=not show) as page:
        page.goto(topic_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        data = page.evaluate(
            """() => {
                const pickText = (selectors) => {
                    for (const selector of selectors) {
                        const node = document.querySelector(selector);
                        if (node && node.textContent.trim()) return node.textContent.trim();
                    }
                    return '';
                };
                const title = pickText(['h1', '.topic-doc h1']);
                const content = pickText(['.topic-content', '.rich-content', '.article', '.topic-doc']);
                const author = pickText(['.topic-doc .from a', '.topic-doc h3 a', '.user-face a', '.topic-content-container a']);
                const time = pickText(['.topic-doc .color-green', '.create-time', '.pubtime']);
                const groupLink = document.querySelector('a[href*="/group/"]:not([href*="/topic/"])');
                const groupHref = groupLink ? groupLink.href : '';
                const replies = Array.from(document.querySelectorAll('.reply-doc, .comment-item, li.clearfix')).map((node, idx) => {
                    const authorNode = node.querySelector('h4 a, .reply-doc .bg-img-green, a');
                    const timeNode = node.querySelector('.pubtime, .color-green, .reply-time');
                    const contentNode = node.querySelector('.reply-content, p, .content');
                    const votesNode = node.querySelector('.votes, .vote-count');
                    return {
                        local_index: idx,
                        author: authorNode ? authorNode.textContent.trim() : '',
                        publish_time: timeNode ? timeNode.textContent.trim() : '',
                        content: contentNode ? contentNode.textContent.trim() : '',
                        like_count_text: votesNode ? votesNode.textContent.trim() : ''
                    };
                }).filter(x => x.content);
                return {title, content, author, time, groupHref, replies, pageText: document.body.innerText.slice(0, 500)};
            }"""
        )
    group_id = group_id_from_url(data.get("groupHref", ""))
    items: list[dict[str, Any]] = []
    title = data.get("title") or f"豆瓣小组帖子 {topic_id}"
    content = data.get("content") or ""
    if content:
        items.append(
            base_item(
                channel="group",
                source_kind="group_topic",
                raw_id=topic_id,
                domain=GROUP_DOMAIN,
                title=title,
                subtitle=group_id or "",
                url=topic_url,
                source_url=topic_url,
                author=data.get("author", ""),
                publish_time=data.get("time", ""),
                like_count=None,
                comment_count=len(data.get("replies") or []),
                content=content,
                content_status="full",
                raw_json={"topic_id": topic_id, "group_id": group_id, "source_url": topic_url},
            )
        )
    if include_replies:
        for reply in data.get("replies") or []:
            raw_id = f"{topic_id}_{reply.get('local_index')}"
            items.append(
                base_item(
                    channel="group",
                    source_kind="group_reply",
                    raw_id=raw_id,
                    domain=GROUP_DOMAIN,
                    title=f"{title} 回复",
                    subtitle=title,
                    url=topic_url,
                    source_url=topic_url,
                    author=reply.get("author", ""),
                    publish_time=reply.get("publish_time", ""),
                    like_count=parse_int(reply.get("like_count_text")),
                    comment_count=None,
                    content=reply.get("content", ""),
                    content_status="full",
                    raw_json={"topic_id": topic_id, "group_id": group_id, "reply_index": reply.get("local_index")},
                )
            )
    return items


def main() -> int:
    cfg = douban_config()
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--topic-url", action="append", help="豆瓣小组帖子 URL,可重复传")
    source.add_argument("--group-id", help="豆瓣小组 id/slug,抓该小组讨论列表")
    parser.add_argument("--keyword", help="抓 group-id 时按标题包含关键词过滤")
    parser.add_argument("--limit", type=int, default=int(cfg.get("default_limit", 10)))
    parser.add_argument("--include-replies", action="store_true", default=True)
    parser.add_argument("--no-replies", dest="include_replies", action="store_false")
    parser.add_argument("--allow-unlisted-group", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--no-obsidian", action="store_true")
    args = parser.parse_args()

    topic_urls = [normalize_topic_url(u) for u in args.topic_url or []]
    if args.group_id:
        assert_group_allowed(args.group_id, args.allow_unlisted_group)
        topic_urls = list_topic_urls(args.group_id, args.limit, args.keyword, args.show, args.allow_unlisted_group)
    if not topic_urls:
        raise RuntimeError("没有可采集的小组帖子 URL")

    items: list[dict[str, Any]] = []
    for url in topic_urls[: args.limit]:
        topic_items = collect_topic(url, args.include_replies, args.show)
        group_id = None
        if topic_items:
            group_id = (topic_items[0].get("raw_json") or {}).get("group_id")
        assert_group_allowed(group_id, args.allow_unlisted_group)
        items.extend(topic_items)

    if not items:
        raise RuntimeError("小组帖子没有解析出正文/回复")
    source_name = args.group_id or ",".join(topic_id_from_url(u) for u in topic_urls)
    out_dir = ROOT / cfg.get("out_dir", "data/language_fuel/douban") / "group"
    data_path = write_data_file(items, out_dir, source_name, "group_topic")
    note_path = None
    if cfg.get("default_write_obsidian", True) and not args.no_obsidian:
        note_path = write_obsidian(items, source_name, f"豆瓣小组语感燃料 - {source_name}", "group_topic", GROUP_DOMAIN_NAME)
    conn = connect()
    try:
        changed = store_items(
            conn,
            items,
            platform="douban",
            source_kind="group_topic",
            domain=GROUP_DOMAIN,
            source_query=source_name,
            requested_limit=args.limit,
            obsidian_note=str(note_path) if note_path else None,
        )
    finally:
        conn.close()
    print(f"豆瓣小组采集完成: {source_name} | {len(items)} 条 | 入库/更新 {changed} 条")
    print(f"材料文件: {data_path}")
    if note_path:
        print(f"Obsidian原料笔记: {note_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        raise SystemExit(1)
