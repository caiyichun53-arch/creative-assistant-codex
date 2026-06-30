"""语感燃料平台可用性探针。

目的:在正式适配平台前,先证明平台当前能拿到目标原料类型。

探针只做确定性检查,不入库、不写 Obsidian、不调用 LLM:
  python scripts/language_fuel/probe_platforms.py
  python scripts/language_fuel/probe_platforms.py --platform netease
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class ProbeResult:
    platform: str
    source_kind: str
    ok: bool
    status: str
    detail: str
    sample: str = ""


def fetch(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
          timeout: int = 20) -> tuple[int, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body


def strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def probe_netease() -> ProbeResult:
    song_id = "186016"
    status, body = fetch(
        f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}",
        params={"limit": 5, "offset": 0},
        headers={"Referer": "https://music.163.com/"},
    )
    if status != 200:
        return ProbeResult("netease", "short_comment", False, f"HTTP {status}", body[:120])
    data = json.loads(body)
    comments = data.get("hotComments") or data.get("comments") or []
    if not comments:
        return ProbeResult("netease", "short_comment", False, "no_comments", "接口通但没有评论")
    sample = comments[0].get("content", "")[:120]
    return ProbeResult("netease", "short_comment", True, "ok", f"comments={len(comments)} total={data.get('total')}", sample)


def probe_douban() -> ProbeResult:
    status, body = fetch(
        "https://movie.douban.com/j/subject_suggest",
        params={"q": "罗马，不设防的城市"},
        headers={"Referer": "https://movie.douban.com/"},
    )
    if status != 200:
        return ProbeResult("douban", "long_review", False, f"search_HTTP_{status}", strip_tags(body)[:160])
    subjects = json.loads(body)
    subject = next((item for item in subjects if str(item.get("id")) == "1296669"), None)
    if not subject:
        return ProbeResult("douban", "long_review", False, "search_no_subject", body[:160])

    review_id = "17659921"
    status, body = fetch(
        f"https://movie.douban.com/j/review/{review_id}/full",
        headers={"Referer": f"https://movie.douban.com/review/{review_id}/"},
    )
    if status != 200:
        return ProbeResult("douban", "long_review", False, f"full_HTTP_{status}", strip_tags(body)[:160])
    data = json.loads(body)
    sample = strip_tags(data.get("body", ""))[:160]
    if not sample:
        return ProbeResult("douban", "long_review", False, "full_empty", body[:160])
    return ProbeResult("douban", "long_review", True, "ok", "search=passed full_review=passed", sample)


PROBES = {
    "netease": probe_netease,
    "douban": probe_douban,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(PROBES), help="只测一个平台")
    args = parser.parse_args()
    names = [args.platform] if args.platform else list(PROBES)
    any_failed = False
    for name in names:
        try:
            result = PROBES[name]()
        except Exception as e:
            result = ProbeResult(name, "unknown", False, "exception", str(e))
        any_failed = any_failed or not result.ok
        mark = "OK" if result.ok else "FAIL"
        print(f"[{mark}] {result.platform} ({result.source_kind}) {result.status}: {result.detail}")
        if result.sample:
            print(f"  sample: {result.sample}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
