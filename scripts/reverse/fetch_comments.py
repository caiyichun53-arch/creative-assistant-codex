"""评论抓取(逆向备料):对爆款跑 MediaCrawler detail + 评论,过 comment_filter 去噪后
入 hit_comments 表 + 写分类 json(data/reverse/领域/账号/hit/comments.json)。

MC 与文案转写共用浏览器(自带互斥锁),必须等 MC 空闲再跑。
用法: python scripts/reverse/fetch_comments.py [--hit ID] [--top 50]
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")
from common import (MC_DIR, MC_JSONL_DIR, RAW_ARCHIVE, acquire_lock, connect,  # noqa: E402
                    kill_leftover_browser, release_lock, safe_title)
from comment_filter import clean_comments, TOP_COMMENTS  # noqa: E402


def crawl_comments(aweme_ids: list[str]) -> dict[str, list[dict]]:
    """detail 模式 + get_comment yes → {aweme_id: [评论行,...]}。复用 MC 锁+归档。"""
    before = set(MC_JSONL_DIR.glob("*.jsonl")) if MC_JSONL_DIR.exists() else set()
    acquire_lock()
    try:
        kill_leftover_browser()
        cmd = ["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode",
               "--type", "detail", "--specified_id", ",".join(aweme_ids),
               "--save_data_option", "jsonl", "--get_comment", "yes",
               "--get_sub_comment", "no", "--headless", "yes"]
        subprocess.run(cmd, cwd=MC_DIR, check=True)
    finally:
        release_lock()
    after = set(MC_JSONL_DIR.glob("*.jsonl")) if MC_JSONL_DIR.exists() else set()
    new = sorted(after - before)
    by_aweme: dict[str, list[dict]] = {}
    for f in new:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("comment_id"):                 # 评论行(视频行无 comment_id)
                by_aweme.setdefault(str(item.get("aweme_id")), []).append(item)
    if new:                                            # 归档,别让增量 ingest 误收
        RAW_ARCHIVE.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in new:
            shutil.move(str(f), RAW_ARCHIVE / f"cmt_{stamp}_{f.name}")
    return by_aweme


def reverse_dir(conn, hit) -> Path:
    """data/reverse/{领域}/{账号}/{hit}_{标题}/  (备料分类目录,给 web 按领域>账号查)"""
    row = conn.execute(
        "SELECT ca.domain, ca.name FROM hits h JOIN competitor_accounts ca "
        "ON ca.id=h.competitor_id WHERE h.id=?", (hit["id"],)).fetchone()
    d = (ROOT / "data" / "reverse" / (row["domain"] or "未分类")
         / (row["name"] or "未知") / f'{hit["id"]}_{safe_title(hit["title"], hit["id"])}')
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_hit_comments(conn, hit, raw_comments: list[dict], top: int = TOP_COMMENTS) -> int:
    """归一化 → clean_comments 过滤去重 → 入 hit_comments(OR IGNORE 幂等)+ 写 comments.json。

    备料的"存评论"单元,转写脚本(备料合并)与本脚本(单独补抓)共用。返回留存条数。
    """
    norm = [{"content": c.get("content"),
             "like_count": int(c.get("like_count") or 0),
             "comment_id": c.get("comment_id"),
             "sub_comment_count": int(c.get("sub_comment_count") or 0),
             "ip_location": c.get("ip_location"),
             "comment_time": c.get("create_time"),
             "parent_comment_id": c.get("parent_comment_id")}
            for c in raw_comments]
    kept = clean_comments(norm, top_n=top)
    with conn:
        for c in kept:
            conn.execute(
                """INSERT OR IGNORE INTO hit_comments
                   (hit_id, comment_id, content, like_count, sub_comment_count,
                    ip_location, comment_time, parent_comment_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (hit["id"], c["comment_id"], c["content"], c["like_count"],
                 c["sub_comment_count"], c["ip_location"], c["comment_time"],
                 c["parent_comment_id"]))
    (reverse_dir(conn, hit) / "comments.json").write_text(
        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(kept)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hit", type=int, help="只抓指定爆款;缺省=所有还没评论的爆款")
    p.add_argument("--top", type=int, default=TOP_COMMENTS, help="每条爆款过滤后保留的评论数")
    a = p.parse_args()
    conn = connect()
    if a.hit:
        hits = conn.execute("SELECT * FROM hits WHERE id=?", (a.hit,)).fetchall()
    else:
        hits = conn.execute(
            "SELECT * FROM hits WHERE id NOT IN "
            "(SELECT DISTINCT hit_id FROM hit_comments)").fetchall()
    if not hits:
        print("没有待抓评论的爆款")
        return
    id2hit = {str(h["platform_item_id"]): h for h in hits}
    print(f"抓评论: {len(hits)} 个爆款")
    raw = crawl_comments(list(id2hit.keys()))

    total = 0
    for aid, comments in raw.items():
        hit = id2hit.get(aid)
        if not hit:
            continue
        kept = store_hit_comments(conn, hit, comments, top=a.top)
        total += kept
        print(f"  hit {hit['id']}: {len(comments)} 原始 → {kept} 留存")
    print(f"评论入库完成: 共 {total} 条")


if __name__ == "__main__":
    main()
