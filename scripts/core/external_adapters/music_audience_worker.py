from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any


def _number(text: str) -> int:
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group()) if match else 0


def _blocked(*, page_url: str, title: str, body_text: str) -> str | None:
    """Detect an actual blocking page, not a harmless login button in normal content."""
    folded_url = page_url.casefold()
    folded_title = title.casefold()
    folded_body = body_text.casefold()
    if any(marker in folded_body for marker in ("验证码", "安全验证", "captcha")):
        return "verification_required"
    if any(marker in folded_body for marker in ("访问异常", "请求异常", "访问受限", "操作频繁")):
        return "access_restricted"
    login_url = any(marker in folded_url for marker in ("/login", "accounts.douban.com", "music.163.com/login"))
    login_title = folded_title.strip() in {"登录", "用户登录", "login", "sign in"}
    login_wall = any(marker in folded_body for marker in ("登录后才能继续", "请先登录后查看", "扫码登录以继续"))
    return "login_required" if login_url or login_title or login_wall else None


def _netease(context: Any, works: list[dict[str, Any]], limits: dict[str, int], budget: list[int]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page = context.new_page()
    retained: list[dict[str, Any]] = []
    try:
        for work in works:
            url = str(work.get("netease_url") or "")
            if not url:
                continue
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            frame = page.frame(name="contentFrame") or page.frame(name="g_iframe") or page.main_frame
            body = frame.locator("body").inner_text(timeout=10000)
            reason = _blocked(page_url=page.url, title=page.title(), body_text=body)
            if reason:
                return {"status": reason, "retry": "manual_login_only"}, retained
            rows = frame.locator(".cmmts .itm, .m-cmmt .itm")
            viewed = min(rows.count(), limits["netease_comments_per_song"], max(0, limits["total_viewed"] - budget[0]))
            budget[0] += viewed
            for index in range(viewed):
                row = rows.nth(index)
                text = row.locator(".cnt").inner_text().strip() if row.locator(".cnt").count() else row.inner_text().strip()
                likes = _number(row.locator(".zan").inner_text()) if row.locator(".zan").count() else 0
                if likes >= 1 and len(text) >= 5:
                    retained.append({
                        "platform": "netease_music", "work_title": work["title"], "subject_kind": "song",
                        "material_kind": "comment", "text": text, "useful_count": likes,
                        "source_url": url, "viewed_position": index,
                    })
            if budget[0] >= limits["total_viewed"]:
                break
        return {"status": "completed", "retry": "none"}, retained
    finally:
        page.close()


def _douban(context: Any, works: list[dict[str, Any]], limits: dict[str, int], budget: list[int]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    page = context.new_page()
    retained: list[dict[str, Any]] = []
    try:
        for work in works:
            url = str(work.get("douban_song_url") or work.get("douban_album_url") or "")
            if not url:
                continue
            subject_kind = "song" if work.get("douban_song_url") else "album"
            if subject_kind == "album" and not bool(work.get("album_material_approved")):
                continue
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            body = page.locator("body").inner_text(timeout=10000)
            reason = _blocked(page_url=page.url, title=page.title(), body_text=body)
            if reason:
                return {"status": reason, "retry": "manual_login_only"}, retained
            groups = (
                ("long_review", page.locator(".review-item, .main.review-item"), limits["douban_long_reviews_per_subject"]),
                ("short_review", page.locator(".comment-item"), limits["douban_short_reviews_per_subject"]),
            )
            for material_kind, rows, limit in groups:
                viewed = min(rows.count(), limit, max(0, limits["total_viewed"] - budget[0]))
                budget[0] += viewed
                for index in range(viewed):
                    row = rows.nth(index)
                    text_node = row.locator(".review-short, .review-content, .short")
                    text = text_node.first.inner_text().strip() if text_node.count() else row.inner_text().strip()
                    vote_node = row.locator(".votes, .useful_count")
                    useful = _number(vote_node.first.inner_text()) if vote_node.count() else 0
                    if useful >= 1 and len(text) >= 5:
                        retained.append({
                            "platform": "douban", "work_title": work["title"], "subject_kind": subject_kind,
                            "material_kind": material_kind, "text": text, "useful_count": useful,
                            "source_url": url, "viewed_position": index,
                        })
                if budget[0] >= limits["total_viewed"]:
                    break
            if budget[0] >= limits["total_viewed"]:
                break
        return {"status": "completed", "retry": "none"}, retained
    finally:
        page.close()


def main() -> int:
    from playwright.sync_api import sync_playwright

    request_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    materials: list[dict[str, Any]] = []
    platforms: dict[str, Any] = {}
    budget = [0]
    with sync_playwright() as playwright:
        for name, profile, collector in (
            ("netease_music", request["netease_profile_dir"], _netease),
            ("douban", request["douban_profile_dir"], _douban),
        ):
            context = playwright.chromium.launch_persistent_context(profile, headless=True)
            try:
                status, retained = collector(context, request["works"], request["limits"], budget)
                platforms[name] = status
                materials.extend(retained)
            finally:
                context.close()
    output_path.write_text(json.dumps({
        "person_name": request["person_name"], "platforms": platforms, "materials": materials,
        "viewed_count": budget[0], "view_limit": request["limits"]["total_viewed"],
    }, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
