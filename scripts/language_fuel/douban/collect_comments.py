"""豆瓣书影音短评采集。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

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
    parse_int,
    search_subjects,
    store_items,
    write_data_file,
    write_obsidian,
)


def comments_url(channel: str, subject_id: str, start: int) -> str:
    host = CHANNELS[channel]["host"]
    if channel == "movie":
        return f"https://{host}/subject/{subject_id}/comments?start={start}&limit=20&status=P&sort=new_score"
    if channel == "book":
        return f"https://{host}/subject/{subject_id}/comments/?status=P&sort=score&percent_type=&start={start}&limit=20"
    return f"https://{host}/subject/{subject_id}/comments/?start={start}"


def collect_comment_cards(channel: str, subject_id: str, subject_title: str, limit: int, show: bool) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    with browser_page(headless=not show) as page:
        while len(cards) < limit:
            url = comments_url(channel, subject_id, start)
            page_cards = []
            for attempt in range(2):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.wait_for_selector(".comment-item, .comment", timeout=8000)
                    page_cards = page.evaluate(
                        """() => {
                    const nodes = document.querySelectorAll('.comment-item').length
                      ? document.querySelectorAll('.comment-item')
                      : document.querySelectorAll('.comment');
                    return Array.from(nodes).map((node, idx) => {
                    const author = node.querySelector('.comment-info a, .comment-info span a, h3 a');
                    const time = node.querySelector('.comment-time, .comment-info .comment-time');
                    const votes = node.querySelector('.votes, .vote-count');
                    const short = node.querySelector('.short, .comment-content, p');
                    const rating = node.querySelector('[class*="allstar"], .rating');
                    return {
                        comment_id: node.getAttribute('data-cid') || node.id || '',
                        local_index: idx,
                        author: author ? author.textContent.trim() : '',
                        publish_time: time ? (time.getAttribute('title') || time.textContent.trim()) : '',
                        like_count_text: votes ? votes.textContent.trim() : '',
                        rating: rating ? (rating.getAttribute('title') || rating.className || '') : '',
                        content: short ? short.textContent.trim() : '',
                        source_url: location.href
                    };
                });
                }"""
                    )
                    break
                except (PlaywrightTimeoutError, PlaywrightError):
                    if attempt == 1:
                        page_cards = []
                    else:
                        page.wait_for_timeout(1500)
            if not page_cards:
                break
            for card in page_cards:
                raw_id = str(card.get("comment_id") or f"{subject_id}_{start}_{card.get('local_index')}")
                if raw_id in seen:
                    continue
                seen.add(raw_id)
                card["raw_id"] = raw_id
                card["subject_id"] = subject_id
                card["subject_title"] = subject_title
                cards.append(card)
                if len(cards) >= limit:
                    break
            start += 20
    return cards


def build_items(channel: str, cards: list[dict[str, Any]], subject_title: str, source_query: str) -> list[dict[str, Any]]:
    channel_cfg = CHANNELS[channel]
    items = []
    for card in cards:
        content = card.get("content", "")
        if not content:
            continue
        raw_id = str(card["raw_id"])
        source_url = card.get("source_url") or f"https://{channel_cfg['host']}/subject/{card.get('subject_id')}/comments/"
        items.append(
            base_item(
                channel=channel,
                source_kind="short_comment",
                raw_id=raw_id,
                domain=channel_cfg["domain"],
                title=f"{subject_title} 短评",
                subtitle=subject_title,
                url=source_url,
                source_url=source_url,
                author=card.get("author", ""),
                publish_time=card.get("publish_time", ""),
                like_count=parse_int(card.get("like_count_text")),
                comment_count=None,
                content=content,
                content_status="full",
                raw_json={
                    "subject_id": card.get("subject_id"),
                    "subject_title": subject_title,
                    "source_query": source_query,
                    "rating": card.get("rating", ""),
                },
            )
        )
    return items


def main() -> int:
    cfg = douban_config()
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="movie")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument("--subject-id")
    parser.add_argument("--list-subjects", action="store_true")
    parser.add_argument("--limit", type=int, default=int(cfg.get("default_limit", 10)))
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--no-obsidian", action="store_true")
    args = parser.parse_args()

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

    source_name = f"{args.channel}_{subject_title}_{subject_id}_comments"
    cards = collect_comment_cards(args.channel, subject_id, subject_title, args.limit, args.show)
    items = build_items(args.channel, cards, subject_title, args.query or subject_id)
    if not items:
        raise RuntimeError(f"没有采到短评: {args.channel}/{subject_id}")

    out_dir = ROOT / cfg.get("out_dir", "data/language_fuel/douban") / args.channel
    data_path = write_data_file(items, out_dir, source_name, "short_comment")
    note_path = None
    if cfg.get("default_write_obsidian", True) and not args.no_obsidian:
        note_path = write_obsidian(
            items,
            source_name,
            f"豆瓣{CHANNELS[args.channel]['domain_name']}短评 - {source_name}",
            "short_comment",
            CHANNELS[args.channel]["domain_name"],
        )
    conn = connect()
    try:
        changed = store_items(
            conn,
            items,
            platform="douban",
            source_kind="short_comment",
            domain=CHANNELS[args.channel]["domain"],
            source_query=source_name,
            requested_limit=args.limit,
            obsidian_note=str(note_path) if note_path else None,
        )
    finally:
        conn.close()
    print(f"豆瓣短评采集完成: {source_name} | {len(items)} 条 | 入库/更新 {changed} 条")
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
