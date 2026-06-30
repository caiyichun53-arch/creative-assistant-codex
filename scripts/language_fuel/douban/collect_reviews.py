"""豆瓣书影音长评全文采集。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from language_fuel.douban.common import (  # noqa: E402
    CHANNELS,
    base_item,
    browser_page,
    choose_subject,
    connect,
    douban_config,
    fetch_json,
    parse_int,
    search_subjects,
    store_items,
    strip_tags,
    write_data_file,
    write_obsidian,
)


def extract_review_ids(text: str | None) -> list[str]:
    if not text:
        return []
    return re.findall(r"(?:review/)?(\d{5,})", text)


def review_full_url(channel: str, review_id: str) -> str:
    return f"https://{CHANNELS[channel]['host']}/j/review/{review_id}/full"


def extract_full_text(review_html: str) -> str:
    m = re.search(r'<div class="review-content clearfix"[\s\S]*?>([\s\S]*?)</div>\s*</div>', review_html)
    return strip_tags(m.group(1) if m else review_html)


def fetch_full_review(channel: str, review_id: str, referer: str) -> str:
    data = fetch_json(review_full_url(channel, review_id), referer=referer)
    body = data.get("body") if isinstance(data, dict) else ""
    text = extract_full_text(body or "")
    if not text:
        raise RuntimeError(f"长评全文为空: {channel}/{review_id}")
    return text


def collect_review_cards(channel: str, subject_id: str, subject_title: str, limit: int, sort: str, show: bool) -> list[dict[str, Any]]:
    host = CHANNELS[channel]["host"]
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    with browser_page(headless=not show) as page:
        while len(cards) < limit:
            url = f"https://{host}/subject/{subject_id}/reviews?sort={sort}&start={start}"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            page_cards = page.evaluate(
                """() => Array.from(document.querySelectorAll('.main.review-item')).map(node => {
                    const titleLink = node.querySelector('h2 a');
                    const action = node.querySelector('.action');
                    const reply = action ? action.querySelector('a.reply') : null;
                    const up = action ? action.querySelector('a.up') : null;
                    const author = node.querySelector('header .name');
                    const meta = node.querySelector('header .main-meta');
                    return {
                        review_id: node.id || (titleLink && (titleLink.href.match(/review\\/(\\d+)/) || [])[1]) || '',
                        title: titleLink ? titleLink.textContent.trim() : '',
                        url: titleLink ? titleLink.href : '',
                        author: author ? author.textContent.trim() : '',
                        publish_time: meta ? meta.textContent.trim() : '',
                        like_count_text: up ? up.textContent.trim() : '',
                        comment_count_text: reply ? reply.textContent.trim() : ''
                    };
                })"""
            )
            if not page_cards:
                break
            for card in page_cards:
                review_id = str(card.get("review_id") or "")
                if not review_id or review_id in seen:
                    continue
                seen.add(review_id)
                card["subject_id"] = subject_id
                card["subject_title"] = subject_title
                card["source_url"] = url
                cards.append(card)
                if len(cards) >= limit:
                    break
            start += 20
    return cards


def build_items(channel: str, cards: list[dict[str, Any]], subject_title: str, source_query: str) -> list[dict[str, Any]]:
    channel_cfg = CHANNELS[channel]
    items: list[dict[str, Any]] = []
    for card in cards:
        review_id = str(card["review_id"])
        url = card.get("url") or f"https://{channel_cfg['host']}/review/{review_id}/"
        content = fetch_full_review(channel, review_id, url)
        items.append(
            base_item(
                channel=channel,
                source_kind="long_review",
                raw_id=review_id,
                domain=channel_cfg["domain"],
                title=card.get("title", ""),
                subtitle=subject_title,
                url=url,
                source_url=card.get("source_url") or url,
                author=card.get("author", ""),
                publish_time=card.get("publish_time", ""),
                like_count=parse_int(card.get("like_count_text")),
                comment_count=parse_int(card.get("comment_count_text")),
                content=content,
                content_status="full",
                raw_json={
                    "review_id": review_id,
                    "subject_id": card.get("subject_id"),
                    "subject_title": subject_title,
                    "source_query": source_query,
                    "full_text_status": "ok",
                },
            )
        )
    return items


def items_from_review_ids(channel: str, review_ids: list[str]) -> list[dict[str, Any]]:
    cards = [
        {
            "review_id": review_id,
            "title": f"豆瓣长评 {review_id}",
            "url": f"https://{CHANNELS[channel]['host']}/review/{review_id}/",
            "author": "",
            "publish_time": "",
            "like_count_text": "",
            "comment_count_text": "",
            "subject_id": "",
            "source_url": f"https://{CHANNELS[channel]['host']}/review/{review_id}/",
        }
        for review_id in review_ids
    ]
    return build_items(channel, cards, "未指定条目", ",".join(review_ids))


def main() -> int:
    cfg = douban_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="movie")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", help="搜索书影音条目,结果唯一或精确匹配时自动采集")
    source.add_argument("--subject-id", help="豆瓣 subject id")
    source.add_argument("--review-id", action="append", help="豆瓣长评 ID,可重复传")
    source.add_argument("--review-url", action="append", help="豆瓣长评 URL,可重复传")
    parser.add_argument("--list-subjects", action="store_true", help="只列出搜索候选,不采集")
    parser.add_argument("--limit", type=int, default=int(cfg.get("default_limit", 10)))
    parser.add_argument("--sort", choices=["hotest", "time"], default="hotest")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--no-obsidian", action="store_true")
    args = parser.parse_args()

    review_ids = extract_review_ids(" ".join(args.review_id or []) + " " + " ".join(args.review_url or []))
    if review_ids:
        source_name = f"{args.channel}_{','.join(review_ids)}"
        items = items_from_review_ids(args.channel, review_ids)
    else:
        subject_title = args.subject_id or ""
        subject_id = args.subject_id
        if args.query:
            subjects = search_subjects(args.channel, args.query)
            if args.list_subjects:
                for idx, item in enumerate(subjects[:10], 1):
                    print(f"{idx}. id={item.get('id')} | {item.get('title')} | {item.get('year', '')} | {item.get('sub_title', '')}")
                return 0
            subject = choose_subject(args.channel, args.query, subjects)
            subject_id = str(subject["id"])
            subject_title = subject.get("title") or subject_id
        if not subject_id:
            raise RuntimeError("缺少 subject id")
        source_name = f"{args.channel}_{subject_title}_{subject_id}"
        cards = collect_review_cards(args.channel, subject_id, subject_title, args.limit, args.sort, args.show)
        if not cards:
            raise RuntimeError(f"没有采到长评列表: {args.channel}/{subject_id}")
        items = build_items(args.channel, cards, subject_title, args.query or subject_id)

    out_dir = ROOT / cfg.get("out_dir", "data/language_fuel/douban") / args.channel
    data_path = write_data_file(items, out_dir, source_name, "long_review")
    note_path = None
    if cfg.get("default_write_obsidian", True) and not args.no_obsidian:
        note_path = write_obsidian(
            items,
            source_name,
            f"豆瓣{CHANNELS[args.channel]['domain_name']}长评全文 - {source_name}",
            "long_review",
            CHANNELS[args.channel]["domain_name"],
        )
    conn = connect()
    try:
        changed = store_items(
            conn,
            items,
            platform="douban",
            source_kind="long_review",
            domain=CHANNELS[args.channel]["domain"],
            source_query=source_name,
            requested_limit=args.limit,
            obsidian_note=str(note_path) if note_path else None,
        )
    finally:
        conn.close()
    print(f"豆瓣长评全文采集完成: {source_name} | {len(items)} 条 | 入库/更新 {changed} 条")
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
