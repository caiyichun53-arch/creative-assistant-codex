"""网易云音乐短评论采集:歌曲/评论入库 + 按作品落 JSON/Obsidian 语感燃料。

用途:
  1. 语感燃料:后续再由 LLM 从已筛好的评论中提炼写作经验。
  2. 音乐领域创作备料:围绕一首歌沉淀听众故事、情绪、年代感表达。

边界:
  - 采集/过滤/入库/写 Obsidian 原料笔记是通用能力。
  - 网易云这条实现是音乐领域专用入口,不是通用领域功能。

固定路线:走网易云网页接口,不做浏览器页面自动化,不接 LLM。

用法:
  python scripts/music/collect_netease.py --song-id 186016 --limit 80 --top 50
  python scripts/music/collect_netease.py --url "https://music.163.com/#/song?id=186016"
  python scripts/music/collect_netease.py --query "晴天 周杰伦" --song 晴天 --artist 周杰伦
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")
from language_fuel.obsidian import safe_name, write_language_fuel_note  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
SONG_ID_RE = re.compile(r"(?:song\?id=|/song/)(\d+)|^\s*(\d+)\s*$")
EMOJI_ONLY = re.compile(r"^[\W\d\s\U0001F000-\U0001FAFF]+$")
AD_PAT = re.compile(
    r"(微信|加\s*[vV]|v\s*x|薇信|徽信|威信|加我|私信|私我|代理|招商|"
    r"兼职|日入|月入|引流|涨粉|互粉|互关|回关|刷单|商务合作|接广)",
    re.I,
)


def load_settings() -> dict[str, Any]:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    source_cfg = (
        settings.get("language_fuel", {}).get("platforms", {}).get("music", {}).get("netease")
        or settings.get("music_sources", {}).get("netease", {})
    )
    source_cfg["vault_dir"] = (
        settings.get("language_fuel", {}).get("vault_dir")
        or "语感燃料"
    )
    source_cfg["vault_root"] = settings.get("paths", {}).get("vault", "vault")
    source_cfg["default_write_obsidian"] = settings.get("language_fuel", {}).get("default_write_obsidian", True)
    return source_cfg


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def request_json(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {e.reason}") from e
    return json.loads(body.decode("utf-8", errors="replace"))


def extract_song_id(text: str | None) -> str | None:
    if not text:
        return None
    m = SONG_ID_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def search_songs(query: str, limit: int = 10) -> list[dict[str, Any]]:
    data = request_json(
        "https://music.163.com/api/search/get/web",
        {
            "csrf_token": "",
            "hlpretag": "",
            "hlposttag": "",
            "s": query,
            "type": 1,
            "offset": 0,
            "total": "true",
            "limit": limit,
        },
    )
    return data.get("result", {}).get("songs") or []


def get_song_detail(song_id: str) -> dict[str, Any]:
    data = request_json("https://music.163.com/api/song/detail", {"ids": f"[{song_id}]"})
    songs = data.get("songs") or []
    if not songs:
        raise RuntimeError(f"找不到歌曲详情: {song_id}")
    return songs[0]


def artist_names(track: dict[str, Any]) -> list[str]:
    artists = track.get("artists") or track.get("ar") or []
    return [a.get("name", "") for a in artists if a.get("name")]


def album_name(track: dict[str, Any]) -> str | None:
    album = track.get("album") or track.get("al") or {}
    return album.get("name")


def norm_name(text: str) -> str:
    return re.sub(r"[\s·・]+", "", text or "").strip().lower()


def pick_search_result(
    songs: list[dict[str, Any]],
    *,
    expected_song: str | None = None,
    expected_artist: str | None = None,
) -> dict[str, Any]:
    if not songs:
        raise RuntimeError("搜索没有返回歌曲")

    def score(song: dict[str, Any]) -> tuple[int, int]:
        name = song.get("name") or ""
        artists = artist_names(song)
        artist_norms = {norm_name(a) for a in artists}
        s = 0
        if expected_song and name == expected_song:
            s += 80
        elif expected_song and expected_song in name:
            s += 40
        if expected_artist and norm_name(expected_artist) in artist_norms:
            s += 60
        return (s, int(song.get("score") or 0))

    ranked = sorted(songs, key=score, reverse=True)
    best = ranked[0]
    if expected_artist and norm_name(expected_artist) not in {norm_name(a) for a in artist_names(best)}:
        choices = "\n".join(
            f"  {i + 1}. id={s.get('id')} {s.get('name')} / {','.join(artist_names(s))} / {album_name(s) or ''}"
            for i, s in enumerate(ranked[:5])
        )
        raise RuntimeError("没有找到歌手匹配的结果,请改用 --song-id。候选:\n" + choices)
    return best


def normalize_track(track: dict[str, Any]) -> dict[str, Any]:
    song_id = str(track.get("id"))
    return {
        "platform": "netease",
        "platform_track_id": song_id,
        "name": track.get("name") or "",
        "artist_names": artist_names(track),
        "album_name": album_name(track),
        "duration_ms": track.get("duration"),
        "url": f"https://music.163.com/#/song?id={song_id}",
        "raw_json": track,
    }


def comment_time(raw: dict[str, Any]) -> str | None:
    ts = raw.get("time")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def normalize_comment(raw: dict[str, Any], comment_type: str) -> dict[str, Any]:
    user = raw.get("user") or {}
    return {
        "platform": "netease",
        "platform_comment_id": str(raw.get("commentId") or raw.get("comment_id") or ""),
        "content": (raw.get("content") or "").strip(),
        "like_count": int(raw.get("likedCount") or 0),
        "reply_count": int(raw.get("replyCount") or 0),
        "user_id": str(user.get("userId") or ""),
        "user_nickname": user.get("nickname") or "",
        "comment_time": comment_time(raw),
        "comment_type": comment_type,
        "raw_json": raw,
    }


def useful_comment(c: dict[str, Any], min_len: int = 2) -> bool:
    text = c["content"].strip()
    if len(text) < min_len:
        return False
    if EMOJI_ONLY.match(text):
        return False
    if AD_PAT.search(text):
        return False
    return True


def collect_comments(song_id: str, limit: int, page_size: int, interval_sec: float) -> tuple[list[dict], int | None]:
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    total: int | None = None
    offset = 0
    hot_done = False

    while len([c for c in comments if c["comment_type"] == "normal"]) < limit:
        data = request_json(
            f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}",
            {"limit": page_size, "offset": offset},
        )
        if data.get("code") != 200:
            raise RuntimeError(f"评论接口返回异常: {data.get('code')}")
        total = data.get("total", total)

        if not hot_done:
            for raw in data.get("hotComments") or []:
                c = normalize_comment(raw, "hot")
                if c["platform_comment_id"] and c["platform_comment_id"] not in seen:
                    seen.add(c["platform_comment_id"])
                    comments.append(c)
            hot_done = True

        page_comments = data.get("comments") or []
        for raw in page_comments:
            c = normalize_comment(raw, "normal")
            if c["platform_comment_id"] and c["platform_comment_id"] not in seen:
                seen.add(c["platform_comment_id"])
                comments.append(c)
        if not data.get("more") or not page_comments:
            break
        offset += page_size
        if offset >= limit:
            break
        time.sleep(interval_sec)
    return comments, total


def filter_comments(comments: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    ranked = sorted(comments, key=lambda c: (c["comment_type"] == "hot", c["like_count"]), reverse=True)
    out: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for c in ranked:
        if not useful_comment(c):
            continue
        key = re.sub(r"\s+", "", c["content"])[:40]
        if key in seen_text:
            continue
        seen_text.add(key)
        out.append(c)
        if len(out) >= top:
            break
    return out


def store_track(conn: sqlite3.Connection, track: dict[str, Any]) -> int:
    with conn:
        conn.execute(
            """INSERT INTO music_tracks
               (platform, platform_track_id, name, artist_names, album_name, duration_ms, url, raw_json)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(platform, platform_track_id) DO UPDATE SET
                 name=excluded.name,
                 artist_names=excluded.artist_names,
                 album_name=excluded.album_name,
                 duration_ms=excluded.duration_ms,
                 url=excluded.url,
                 raw_json=excluded.raw_json""",
            (
                track["platform"],
                track["platform_track_id"],
                track["name"],
                json.dumps(track["artist_names"], ensure_ascii=False),
                track["album_name"],
                track["duration_ms"],
                track["url"],
                json.dumps(track["raw_json"], ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT id FROM music_tracks WHERE platform=? AND platform_track_id=?",
            (track["platform"], track["platform_track_id"]),
        ).fetchone()
    return int(row["id"])


def store_comments(
    conn: sqlite3.Connection,
    track_id: int,
    comments: list[dict[str, Any]],
    *,
    source_query: str | None,
    requested_limit: int,
    fetched_count: int,
    total_comments: int | None,
) -> int:
    inserted = 0
    with conn:
        for c in comments:
            cur = conn.execute(
                """INSERT OR IGNORE INTO music_comments
                   (track_id, platform, platform_comment_id, content, like_count, reply_count,
                    user_id, user_nickname, comment_time, comment_type, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    track_id,
                    c["platform"],
                    c["platform_comment_id"],
                    c["content"],
                    c["like_count"],
                    c["reply_count"],
                    c["user_id"],
                    c["user_nickname"],
                    c["comment_time"],
                    c["comment_type"],
                    json.dumps(c["raw_json"], ensure_ascii=False),
                ),
            )
            inserted += cur.rowcount
        conn.execute(
            """INSERT INTO music_comment_batches
               (track_id, platform, source_query, requested_limit, fetched_count, kept_count, total_comments)
               VALUES(?,?,?,?,?,?,?)""",
            (track_id, "netease", source_query, requested_limit, fetched_count, len(comments), total_comments),
        )
        conn.execute(
            "UPDATE music_tracks SET last_collected_at=datetime('now','localtime') WHERE id=?",
            (track_id,),
        )
    return inserted


def write_materials(track: dict[str, Any], comments: list[dict[str, Any]], total_comments: int | None, out_root: Path) -> Path:
    artists = "、".join(track["artist_names"]) or "未知歌手"
    folder = out_root / safe_name(f"{artists} - {track['name']}_{track['platform_track_id']}", track["platform_track_id"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "track.json").write_text(json.dumps(track, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = {
        "primary_usage": "writing_experience_fuel",
        "secondary_usage": "music_domain_research_material",
        "track": {
            "platform": track["platform"],
            "platform_track_id": track["platform_track_id"],
            "name": track["name"],
            "artist_names": track["artist_names"],
            "album_name": track["album_name"],
            "url": track["url"],
        },
        "total_comments": total_comments,
        "kept_count": len(comments),
        "comments": comments,
    }
    (folder / "comments.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


def write_obsidian_note(
    track: dict[str, Any],
    comments: list[dict[str, Any]],
    total_comments: int | None,
    *,
    vault_root: Path,
    vault_dir: str,
) -> Path:
    artists = "、".join(track["artist_names"]) or "未知歌手"
    return write_language_fuel_note(
        vault_root=vault_root,
        vault_dir=vault_dir,
        platform="netease",
        platform_name="网易云音乐",
        domain="music",
        domain_name="音乐",
        source="netease_music_comments",
        source_kind="short_comment",
        item_id=track["platform_track_id"],
        title=f"{artists} - {track['name']}",
        subtitle=track["album_name"],
        url=track["url"],
        items=comments,
        total_items=total_comments,
        sample_heading="短评论样本",
        extra_frontmatter={
            "secondary_usage": "music_domain_research_material",
            "platform_track_id": track["platform_track_id"],
            "song": track["name"],
            "artist_names": track["artist_names"],
            "album_name": track["album_name"],
        },
        extra_sections=[
            "## 音乐资料属性",
            f"- 歌曲: {track['name']}",
            f"- 歌手: {artists}",
            f"- 专辑: {track['album_name'] or ''}",
            "- 附加用途: 音乐内容资料搜集",
        ],
    )


def resolve_track(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    song_id = args.song_id or extract_song_id(args.url)
    if song_id:
        return normalize_track(get_song_detail(song_id)), None
    if not args.query:
        raise RuntimeError("请提供 --song-id / --url / --query 之一")
    songs = search_songs(args.query, limit=args.search_limit)
    picked = pick_search_result(songs, expected_song=args.song, expected_artist=args.artist)
    return normalize_track(get_song_detail(str(picked["id"]))), args.query


def main() -> int:
    settings = load_settings()
    p = argparse.ArgumentParser()
    p.add_argument("--song-id", help="网易云歌曲 ID,最稳")
    p.add_argument("--url", help="网易云歌曲链接,会解析 song?id=")
    p.add_argument("--query", help="搜索关键词,例如: 晴天 周杰伦")
    p.add_argument("--song", help="搜索时用于校验的歌名")
    p.add_argument("--artist", help="搜索时用于校验的歌手名")
    p.add_argument("--search-limit", type=int, default=10)
    p.add_argument("--limit", type=int, default=int(settings.get("default_comment_limit", 100)),
                   help="普通评论抓取上限;热评会额外抓取并合并去重")
    p.add_argument("--top", type=int, default=80, help="过滤去重后最多保留/入库多少条")
    p.add_argument("--page-size", type=int, default=int(settings.get("page_size", 20)))
    p.add_argument("--out-dir", default=settings.get("out_dir", "data/music/netease"))
    p.add_argument("--interval-sec", type=float, default=float(settings.get("request_interval_sec", 0.8)))
    p.add_argument("--no-obsidian", action="store_true", help="只入库和写 data 材料,不写 Obsidian 语感燃料原料笔记")
    args = p.parse_args()

    track, source_query = resolve_track(args)
    comments, total_comments = collect_comments(
        track["platform_track_id"], limit=args.limit, page_size=args.page_size, interval_sec=args.interval_sec
    )
    kept = filter_comments(comments, top=args.top)
    conn = connect()
    try:
        track_id = store_track(conn, track)
        inserted = store_comments(
            conn,
            track_id,
            kept,
            source_query=source_query,
            requested_limit=args.limit,
            fetched_count=len(comments),
            total_comments=total_comments,
        )
    finally:
        conn.close()
    out_dir = write_materials(track, kept, total_comments, ROOT / args.out_dir)
    note_path = None
    if settings.get("default_write_obsidian", True) and not args.no_obsidian:
        note_path = write_obsidian_note(
            track,
            kept,
            total_comments,
            vault_root=ROOT / settings.get("vault_root", "vault"),
            vault_dir=settings.get("vault_dir", "语感燃料"),
        )

    artists = "、".join(track["artist_names"]) or "未知歌手"
    print(f"网易云采集完成: {artists} - {track['name']} ({track['platform_track_id']})")
    print(f"评论总量: {total_comments} | 本次抓回: {len(comments)} | 过滤留存: {len(kept)} | 新入库: {inserted}")
    print(f"材料目录: {out_dir}")
    if note_path:
        print(f"Obsidian原料笔记: {note_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        raise SystemExit(1)
