from __future__ import annotations

import argparse
import json
from pathlib import Path


PLATFORMS = {
    "netease_music": ("https://music.163.com/", ("music.163.com",)),
    "douban": ("https://www.douban.com/", ("douban.com",)),
}


def main() -> int:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description="Prepare one private persistent login profile for music audience collection")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--profile-dir", required=True)
    args = parser.parse_args()
    profile = Path(args.profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    url, cookie_domains = PLATFORMS[args.platform]
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        input("Complete login in the opened browser, confirm the home page is visible, then press Enter here: ")
        cookies = context.cookies()
        matched = [item for item in cookies if any(domain in str(item.get("domain") or "") for domain in cookie_domains)]
        context.close()
    print(json.dumps({
        "platform": args.platform, "profile_dir": str(profile),
        "profile_state_written": any(item.is_file() for item in profile.rglob("*")),
        "matching_cookie_count": len(matched), "manual_login_required": len(matched) == 0,
    }, ensure_ascii=False))
    return 0 if matched else 2


if __name__ == "__main__":
    raise SystemExit(main())
