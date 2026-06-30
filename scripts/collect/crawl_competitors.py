"""增量采集对标账号视频 → competitor_videos(按发布时间分流状态)。

流程(全确定性):
  1. 从 competitor_accounts 取 active 账号
  2. 调 MediaCrawler creator 模式(扫码登录,登录态缓存;CDP 无头)
  3. 解析其 jsonl 输出,upsert 进 competitor_videos:
     - 新视频:发布 ≤ observe_days(7天)→ status=watching(观察池,等7天定生死)
              发布 > observe_days(存量,已定型)→ status=archived(基线材料)
     - 已有视频:只刷新计数(状态不动,升降由 judge_hits.py 管)
     creator 信息回填账号池(昵称/粉丝/简介)
  4. 处理过的 jsonl 归档到 data/raw/mediacrawler/
采集后接着跑 scripts/analyze/judge_hits.py 算基线+判爆款。

用法:
  python scripts/collect/crawl_competitors.py            # 全部 active 账号
  python scripts/collect/crawl_competitors.py --ids 1,2  # 指定账号
  python scripts/collect/crawl_competitors.py --ingest-only  # 只入库已有 jsonl(不爬)
  python scripts/collect/crawl_competitors.py --show     # 弹窗(首次扫码/重登)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from common import (MC_DIR, MC_JSONL_DIR, RAW_ARCHIVE, SETTINGS, acquire_lock,  # noqa: E402
                    connect, hidden_startupinfo, kill_leftover_browser, release_lock,
                    strip_ephemeral_urls, to_int)

OBSERVE_DAYS = SETTINGS["hit_detection"]["observe_days"]


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
        try:
            if path.exists():
                return str(path)
        except OSError:
            continue
    raise RuntimeError("找不到 uv.exe。请安装 uv,或把 uv.exe 加入 PATH。")


def crawl(sec_uids: list[str], headless: bool, max_notes: int) -> None:
    """固定技术路线:调 MediaCrawler,不给任何"重新决定怎么做"的余地。"""
    cmd = [
        find_uv(), "run", "main.py",
        "--platform", "dy",
        "--lt", "qrcode",
        "--type", "creator",
        "--creator_id", ",".join(sec_uids),
        "--save_data_option", "jsonl",
        "--get_comment", "no",
        "--get_sub_comment", "no",
        "--headless", "yes" if headless else "no",
        "--crawler_max_notes_count", str(max_notes),
    ]
    print(f"运行 MediaCrawler: {len(sec_uids)} 个账号,每账号最多 {max_notes} 条 …")
    # 定时任务以 pythonw(无控制台)为父进程,启动 uv(控制台程序)会被 Windows
    # 新开一个控制台窗口并抢焦点;CREATE_NO_WINDOW 抑制该窗口。与无头/登录态无关。
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.run(cmd, cwd=MC_DIR, check=True, creationflags=flags,
                   startupinfo=hidden_startupinfo())


def ingest() -> tuple[int, int]:
    """jsonl → SQLite。幂等:重复行只刷新计数。返回(新视频数, 刷新数)。

    两类记录:视频行(有 aweme_id,含 sec_uid+user_id) / creator 行(只有 user_id)。
    先过视频行建 user_id→账号 映射,再用映射回填 creator 行的粉丝/简介。
    """
    conn = connect()
    sec2id = {}
    first_crawl = {}  # 首采(从未采集过)=注册存量快照,全部归档,不进观察池
    for r in conn.execute("SELECT id, platform_uid, last_crawled_at "
                          "FROM competitor_accounts WHERE platform='douyin'"):
        sec2id[r["platform_uid"]] = r["id"]
        first_crawl[r["id"]] = r["last_crawled_at"] is None
    new_cnt = upd_cnt = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    files = sorted(MC_JSONL_DIR.glob("*.jsonl")) if MC_JSONL_DIR.exists() else []

    records = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append((json.loads(line), line))

    uid2comp: dict[str, int] = {}  # user_id(uid) → competitor id
    with conn:
        # 第一遍:视频行
        for item, line in records:
            aweme_id = item.get("aweme_id")
            if not aweme_id:
                continue
            comp_id = sec2id.get(item.get("sec_uid"))
            if comp_id is None:
                continue  # 未注册账号的数据,跳过
            if item.get("user_id"):
                uid2comp[str(item["user_id"])] = comp_id
            if item.get("nickname"):
                conn.execute("UPDATE competitor_accounts SET name=? WHERE id=?",
                             (item["nickname"], comp_id))
            pub = item.get("create_time")
            pub_dt = datetime.fromtimestamp(int(pub)) if pub else None
            pub_iso = pub_dt.strftime("%Y-%m-%d %H:%M:%S") if pub_dt else None
            # 状态分流:观察=「日常增量里看着新发布的」。首采全是存量快照(哪怕年轻,
            # 曲线缺前几帧)→archived;此后采到的新视频且发布≤窗口→watching。
            # 年轻存量不吃亏:计数照样每日刷新,judge 每轮扫 archived 照样能晋升。
            is_fresh = pub_dt and (datetime.now() - pub_dt) <= timedelta(days=OBSERVE_DAYS)
            status = "watching" if (is_fresh and not first_crawl[comp_id]) else "archived"
            conn.execute(
                """INSERT INTO competitor_videos(
                     competitor_id, platform_item_id, title, url, publish_time,
                     like_count, comment_count, share_count, collect_count, raw_json, status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(competitor_id, platform_item_id) DO UPDATE SET
                     like_count = excluded.like_count,
                     comment_count = excluded.comment_count,
                     share_count = excluded.share_count,
                     collect_count = excluded.collect_count,
                     last_checked_at = ?,
                     check_count = check_count + 1""",  # 已有行:只刷计数,状态升降由 judge_hits 管
                (comp_id, aweme_id, item.get("title") or item.get("desc"),
                 item.get("aweme_url"), pub_iso,
                 to_int(item.get("liked_count")), to_int(item.get("comment_count")),
                 to_int(item.get("share_count")), to_int(item.get("collected_count")),
                 json.dumps(strip_ephemeral_urls(item), ensure_ascii=False), status, now),
            )
            # rowcount 对 upsert 都是 1;改查 check_count 区分新增/刷新
            row = conn.execute(
                "SELECT id, status, check_count FROM competitor_videos "
                "WHERE competitor_id=? AND platform_item_id=?",
                (comp_id, aweme_id)).fetchone()
            if row["check_count"] == 0:
                new_cnt += 1
            else:
                upd_cnt += 1
            # 观察期内的视频记复查快照(生长曲线原料;出观察期即停,行数封顶)
            if row["status"] == "watching" and pub_dt:
                age_h = round((datetime.now() - pub_dt).total_seconds() / 3600, 1)
                conn.execute(
                    """INSERT INTO video_checks(video_id, age_hours, like_count,
                                                comment_count, share_count, collect_count)
                       VALUES(?,?,?,?,?,?)""",
                    (row["id"], age_h,
                     to_int(item.get("liked_count")), to_int(item.get("comment_count")),
                     to_int(item.get("share_count")), to_int(item.get("collected_count"))))

        # 第二遍:creator 行(粉丝数/简介)。
        # 注意:creator 行的 user_id 实际是 sec_uid(MediaCrawler 字段名误导),两种都试。
        for item, _ in records:
            if item.get("aweme_id") or not item.get("user_id"):
                continue
            key = str(item["user_id"])
            comp_id = sec2id.get(key) or uid2comp.get(key)
            if comp_id is None:
                continue
            conn.execute(
                """UPDATE competitor_accounts SET
                     name = COALESCE(?, name),
                     follower_count = COALESCE(?, follower_count),
                     signature = COALESCE(?, signature)
                   WHERE id = ?""",
                (item.get("nickname"), to_int(item.get("fans")),
                 item.get("desc"), comp_id),
            )

    # 归档已处理文件
    if files:
        RAW_ARCHIVE.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in files:
            shutil.move(str(f), RAW_ARCHIVE / f"{stamp}_{f.name}")
    conn.close()
    return new_cnt, upd_cnt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ids", default=None, help="逗号分隔的 competitor id;缺省=全部 active")
    p.add_argument("--mode", choices=["daily", "full"], default="daily",
                   help="daily=增量(最新约20条,覆盖新发布+观察池复查,默认) / "
                        "full=全量复刷(注册时 & 每周刷存量计数喂基线)")
    p.add_argument("--ingest-only", action="store_true")
    p.add_argument("--show", action="store_true",
                   help="弹出浏览器窗口(仅首次扫码/登录失效时用;默认无头,不抢焦点)")
    a = p.parse_args()

    if not a.ingest_only:
        conn = connect()
        if a.ids:
            ids = [int(x) for x in a.ids.split(",")]
            q = (f"SELECT platform_uid FROM competitor_accounts "
                 f"WHERE id IN ({','.join('?' * len(ids))}) AND status='active'")
            rows = conn.execute(q, ids).fetchall()
        else:
            rows = conn.execute(
                "SELECT platform_uid FROM competitor_accounts WHERE status='active'").fetchall()
        conn.close()
        if not rows:
            sys.exit("账号池里没有 active 的对标账号,先用 register_competitor.py 注册")
        max_notes = SETTINGS["crawler"]["daily_max_notes"] if a.mode == "daily" else 100000
        acquire_lock()
        try:
            kill_leftover_browser()
            crawl([r["platform_uid"] for r in rows], headless=not a.show, max_notes=max_notes)
        finally:
            release_lock()

    new_cnt, upd_cnt = ingest()
    print(f"入库完成: 观察池新增 {new_cnt} 条视频,刷新 {upd_cnt} 条计数")

    if not a.ingest_only:
        conn = connect()
        with conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if a.ids:
                ids = [int(x) for x in a.ids.split(",")]
                conn.execute(
                    f"UPDATE competitor_accounts SET last_crawled_at=? "
                    f"WHERE id IN ({','.join('?' * len(ids))})", (now, *ids))
            else:
                conn.execute(
                    "UPDATE competitor_accounts SET last_crawled_at=? WHERE status='active'", (now,))
        conn.close()


if __name__ == "__main__":
    main()
