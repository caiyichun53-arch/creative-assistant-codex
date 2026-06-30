"""MediaCrawler 统一入口 -> 语感燃料通用表。

只用于 MediaCrawler 已支持的平台:小红书/抖音/快手/B站/微博/贴吧/知乎。
豆瓣、网易云这类专用来源不走这里。

默认跑 MediaCrawler search/detail,读取其 JSONL 输出,标准化入:
  - language_fuel_items
  - language_fuel_batches

用法:
  python scripts/language_fuel/collect_mediacrawler.py --platform zhihu --keywords 周杰伦 --limit 20 --no-obsidian
  python scripts/language_fuel/collect_mediacrawler.py --platform douyin --specified-id 123 --type detail --no-obsidian
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"
MC_DIR = ROOT / "vendor" / "MediaCrawler"
DEFAULT_RAW_DIR = ROOT / "data" / "raw" / "mediacrawler_language_fuel"
LOCK_FILE = MC_DIR / ".crawl.lock"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from language_fuel.obsidian import write_language_fuel_note  # noqa: E402


PLATFORM_TO_MC = {
    "xhs": "xhs",
    "douyin": "dy",
    "dy": "dy",
    "kuaishou": "ks",
    "ks": "ks",
    "bilibili": "bili",
    "bili": "bili",
    "weibo": "wb",
    "wb": "wb",
    "tieba": "tieba",
    "zhihu": "zhihu",
}

MC_TO_CANONICAL = {
    "xhs": "xhs",
    "dy": "douyin",
    "ks": "kuaishou",
    "bili": "bilibili",
    "wb": "weibo",
    "tieba": "tieba",
    "zhihu": "zhihu",
}

PLATFORM_NAMES = {
    "xhs": "小红书",
    "douyin": "抖音",
    "kuaishou": "快手",
    "bilibili": "B站",
    "weibo": "微博",
    "tieba": "贴吧",
    "zhihu": "知乎",
}

ID_KEYS = (
    "note_id",
    "aweme_id",
    "video_id",
    "bvid",
    "aid",
    "mblogid",
    "note_id",
    "content_id",
    "comment_id",
)
TITLE_KEYS = ("title", "note_title", "display_title", "question_title", "desc")
CONTENT_KEYS = (
    "content",
    "desc",
    "note_desc",
    "text",
    "comment_text",
    "answer_content",
    "article_content",
)
URL_KEYS = ("url", "note_url", "aweme_url", "video_url", "mblog_url", "content_url", "source_url")
AUTHOR_KEYS = ("nickname", "user_name", "author", "creator_name", "user_nickname")
TIME_KEYS = ("publish_time", "create_time", "created_time", "time", "add_ts")


def load_settings() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    candidates = []
    userprofile = os.getenv("USERPROFILE")
    localappdata = os.getenv("LOCALAPPDATA")
    uv_exe = os.getenv("UV_EXE")
    if uv_exe:
        candidates.append(Path(uv_exe))
    if userprofile:
        candidates.append(Path(userprofile) / ".local" / "bin" / "uv.exe")
    if localappdata:
        candidates.append(Path(localappdata) / "Programs" / "uv" / "uv.exe")
    for path in candidates:
        if path.exists():
            return str(path)
    raise RuntimeError("找不到 uv.exe。请安装 uv,或把 uv.exe 加入 PATH。")


def find_mediacrawler_python() -> str | None:
    candidates = []
    env_python = os.getenv("MEDIACRAWLER_PYTHON")
    if env_python:
        candidates.append(Path(env_python))
    candidates.extend([
        ROOT / "tools" / "python311" / "python.exe",
        Path(sys.executable),
    ])
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def acquire_lock() -> None:
    if LOCK_FILE.exists():
        pid = LOCK_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        raise RuntimeError(f"MediaCrawler 已有任务在跑(pid={pid}),锁文件:{LOCK_FILE}")
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def strip_tags(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return strip_tags(value)
    return ""


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_comment_item(item: dict[str, Any]) -> bool:
    return bool(item.get("comment_id") or item.get("parent_comment_id") or item.get("sub_comment_id"))


def normalize_item(platform: str, source_kind: str, item: dict[str, Any]) -> dict[str, Any] | None:
    item_id = first_value(item, ID_KEYS)
    content = first_value(item, CONTENT_KEYS)
    if not item_id or not content:
        return None
    row_source_kind = "short_comment" if is_comment_item(item) else source_kind
    return {
        "platform": platform,
        "platform_item_id": item_id,
        "source_kind": row_source_kind,
        "domain": "general",
        "title": first_value(item, TITLE_KEYS),
        "subtitle": "",
        "url": first_value(item, URL_KEYS),
        "author": first_value(item, AUTHOR_KEYS),
        "publish_time": first_value(item, TIME_KEYS),
        "like_count": to_int(item.get("liked_count") or item.get("like_count") or item.get("digg_count")),
        "comment_count": to_int(item.get("comment_count") or item.get("comments_count")),
        "content": content,
        "content_status": "raw",
        "item_type": row_source_kind,
        "raw_json": item,
    }


def jsonl_files(raw_dir: Path) -> set[Path]:
    if not raw_dir.exists():
        return set()
    return set(raw_dir.glob("**/*.jsonl"))


def run_mediacrawler(
    mc_platform: str,
    crawler_type: str,
    keywords: str | None,
    specified_id: str | None,
    limit: int,
    raw_dir: Path,
    headless: bool,
    get_comment: bool,
) -> set[Path]:
    before = jsonl_files(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    uv_cache_dir = ROOT / "data" / "uv-cache"
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(uv_cache_dir)
    env.setdefault("UV_PROJECT_ENVIRONMENT", str(MC_DIR / ".venv312"))
    mc_python = find_mediacrawler_python()
    cmd = [
        find_uv(),
        "run",
    ]
    if mc_python:
        cmd.extend(["--python", mc_python])
    cmd.extend([
        "main.py",
        "--platform",
        mc_platform,
        "--lt",
        "qrcode",
        "--type",
        crawler_type,
        "--save_data_option",
        "jsonl",
        "--save_data_path",
        str(raw_dir),
        "--get_comment",
        "yes" if get_comment else "no",
        "--get_sub_comment",
        "no",
        "--headless",
        "yes" if headless else "no",
        "--crawler_max_notes_count",
        str(limit),
    ])
    if crawler_type == "search":
        if not keywords:
            raise RuntimeError("search 模式必须传 --keywords")
        cmd.extend(["--keywords", keywords])
    elif crawler_type == "detail":
        if not specified_id:
            raise RuntimeError("detail 模式必须传 --specified-id")
        cmd.extend(["--specified_id", specified_id])
    else:
        raise RuntimeError("语感燃料入口当前只允许 search/detail")

    acquire_lock()
    try:
        subprocess.run(cmd, cwd=MC_DIR, check=True, creationflags=NO_WINDOW, env=env)
    finally:
        release_lock()
    return jsonl_files(raw_dir) - before


def read_items(files: set[Path], platform: str, source_kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(files):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = normalize_item(platform, source_kind, json.loads(line))
            if item:
                rows.append(item)
    return rows


def store_items(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    platform: str,
    source_kind: str,
    source_query: str,
    limit: int,
    obsidian_note: str | None,
) -> int:
    changed = 0
    with conn:
        for item in items:
            cur = conn.execute(
                """INSERT INTO language_fuel_items
                   (platform, platform_item_id, source_kind, domain, title, subtitle, url,
                    author, publish_time, like_count, comment_count, content, content_status, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform, platform_item_id) DO UPDATE SET
                     source_kind=excluded.source_kind,
                     title=excluded.title,
                     subtitle=excluded.subtitle,
                     url=excluded.url,
                     author=excluded.author,
                     publish_time=excluded.publish_time,
                     like_count=excluded.like_count,
                     comment_count=excluded.comment_count,
                     content=excluded.content,
                     content_status=excluded.content_status,
                     raw_json=excluded.raw_json,
                     collected_at=datetime('now','localtime')""",
                (
                    item["platform"],
                    item["platform_item_id"],
                    item["source_kind"],
                    item["domain"],
                    item["title"],
                    item["subtitle"],
                    item["url"],
                    item["author"],
                    item["publish_time"],
                    item["like_count"],
                    item["comment_count"],
                    item["content"],
                    item["content_status"],
                    json.dumps(item["raw_json"], ensure_ascii=False),
                ),
            )
            changed += cur.rowcount
        conn.execute(
            """INSERT INTO language_fuel_batches
               (platform, source_kind, domain, source_query, requested_limit, fetched_count,
                kept_count, obsidian_note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (platform, source_kind, "general", source_query, limit, len(items), len(items), obsidian_note),
        )
    return changed


def write_obsidian(items: list[dict[str, Any]], settings: dict[str, Any], platform: str, source: str) -> Path:
    language_fuel = settings.get("language_fuel", {})
    vault_root = ROOT / settings.get("paths", {}).get("vault", "vault")
    return write_language_fuel_note(
        vault_root=vault_root,
        vault_dir=language_fuel.get("vault_dir", "语感燃料"),
        platform=platform,
        platform_name=PLATFORM_NAMES.get(platform, platform),
        domain="general",
        domain_name="通用",
        source=source,
        source_kind=items[0]["source_kind"] if items else "post",
        item_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
        title=f"{PLATFORM_NAMES.get(platform, platform)} MediaCrawler 语感燃料",
        subtitle=source,
        url="",
        items=items,
        total_items=len(items),
        sample_heading="原料样本",
        extra_frontmatter={"adapter": "mediacrawler"},
    )


def main() -> int:
    settings = load_settings()
    adapter_cfg = settings.get("language_fuel", {}).get("adapters", {}).get("mediacrawler", {})
    configured = set(adapter_cfg.get("platforms", []))
    source_kind_by_platform = adapter_cfg.get("source_kind_by_platform", {})

    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORM_TO_MC))
    parser.add_argument("--type", choices=["search", "detail"], default="search")
    parser.add_argument("--keywords")
    parser.add_argument("--specified-id")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--with-comments", action="store_true")
    parser.add_argument("--show", action="store_true", help="显示浏览器,用于首次扫码/重登")
    parser.add_argument("--no-run", action="store_true", help="只入库 raw-dir 中已有的新 JSONL 不调用 MediaCrawler")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--no-obsidian", action="store_true")
    args = parser.parse_args()

    mc_platform = PLATFORM_TO_MC[args.platform]
    platform = MC_TO_CANONICAL[mc_platform]
    if configured and platform not in configured and args.platform not in configured:
        raise RuntimeError(f"{platform} 不在 language_fuel.adapters.mediacrawler.platforms 配置里")

    source_kind = source_kind_by_platform.get(platform) or source_kind_by_platform.get(args.platform) or "post"
    raw_dir = Path(args.raw_dir)
    files = jsonl_files(raw_dir) if args.no_run else run_mediacrawler(
        mc_platform=mc_platform,
        crawler_type=args.type,
        keywords=args.keywords,
        specified_id=args.specified_id,
        limit=args.limit,
        raw_dir=raw_dir,
        headless=not args.show,
        get_comment=args.with_comments,
    )
    items = read_items(files, platform, source_kind)
    if not items:
        raise RuntimeError("MediaCrawler 没有产出可入库的语感燃料 JSONL")

    note_path: Path | None = None
    if settings.get("language_fuel", {}).get("default_write_obsidian", True) and not args.no_obsidian:
        source = args.keywords or args.specified_id or args.type
        note_path = write_obsidian(items, settings, platform, source)

    conn = connect()
    try:
        changed = store_items(
            conn,
            items,
            platform=platform,
            source_kind=source_kind,
            source_query=args.keywords or args.specified_id or "",
            limit=args.limit,
            obsidian_note=str(note_path) if note_path else None,
        )
    finally:
        conn.close()

    print(f"MediaCrawler 语感燃料入库完成: {platform} 解析 {len(items)} 条 | 入库/更新 {changed} 条")
    if note_path:
        print(f"Obsidian原料笔记: {note_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        raise SystemExit(1)
