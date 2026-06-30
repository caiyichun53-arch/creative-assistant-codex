"""每日选题流(定时·快·全确定性·不碰逆向转写)。三条独立轨之①。

流程:
  1. 翻昨页:status='pushed' 且推送日 < 今天 → 进候选池(candidate)
  2. 候选池维护:
     - 衰减:current_heat = initial_heat × 0.5^(年龄天/半衰期) + 回温加成
     - 回温:近1天新晋爆款的标签 ∩ 候选标签 → 加成+标🔥(标签词表启用前自动跳过)
     - 淘汰:current_heat < expire_heat → expired 沉底
  3. 爆款库新货(没生成过选题的 hit)→ 生成选题(标题去话题标签;初始热度=相对超额)
  4. 分组推飞书卡片:新晋爆款选题(按超额排) / 候选池 Top N(按热度排)——各排各的,不统一排
选中/打回由飞书回复触发(阶段4监听);本脚本只推不收。

用法: python scripts/topics/daily_topics.py [--dry-run]
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collect"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "feishu"))
sys.stdout.reconfigure(encoding="utf-8")
from common import SETTINGS, connect  # noqa: E402
import push  # noqa: E402

CFG = SETTINGS["candidate_pool"]
TOP_N_CANDIDATES = 8
TOP_N_NEW = 10        # 新晋爆款选题单卡上限(其余仍入库,明日翻进候选池;防飞书卡片超限)
CLUSTER_BOOST = 0.3   # 同题簇强度加成:heat = 代表超额 ×(1 + 0.3×(簇大小-1)),多人验证=更稳


def clean_title(title: str | None) -> str:
    """视频标题 → 选题表述:去 #话题标签、修空白。"""
    t = re.sub(r"#\S+", "", title or "").strip(" #@~ 　")
    return re.sub(r"\s+", " ", t) or "(无标题)"


def rollover(conn) -> int:
    """昨日推送未选中 → 候选池。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cur = conn.execute(
        "UPDATE topics SET status='candidate' WHERE status='pushed' AND date(pushed_at) < ?",
        (today,))
    return cur.rowcount


def maintain_candidates(conn) -> tuple[int, int]:
    """衰减重算 + 回温 + 淘汰。返回(回温数, 淘汰数)。"""
    # 回温:近1天新晋爆款 tags ∩ 候选 tags(词表打标启用前两边都空,自动不命中)
    import json as _json
    fresh_tags = set()
    for (tags,) in conn.execute(
            "SELECT tags FROM hits WHERE promoted_at >= datetime('now','localtime','-1 day')"):
        fresh_tags.update(_json.loads(tags) if tags else [])

    reheated = 0
    for row in conn.execute("SELECT id, initial_heat, tags, created_at, reheat_count "
                            "FROM topics WHERE status='candidate'").fetchall():
        age_days = max((datetime.now()
                        - datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")).days, 0)
        heat = (row["initial_heat"] or 1.0) * 0.5 ** (age_days / CFG["decay_half_life_days"])
        topic_tags = set(_json.loads(row["tags"]) if row["tags"] else [])
        if fresh_tags & topic_tags:
            heat += (row["initial_heat"] or 1.0) * CFG["reheat_boost"]
            conn.execute(
                "UPDATE topics SET reheat_count=reheat_count+1, "
                "last_reheat_at=datetime('now','localtime') WHERE id=?", (row["id"],))
            reheated += 1
        conn.execute("UPDATE topics SET current_heat=? WHERE id=?", (round(heat, 3), row["id"]))

    cur = conn.execute(
        "UPDATE topics SET status='expired' WHERE status='candidate' AND current_heat < ?",
        (CFG["expire_heat"],))
    return reheated, cur.rowcount


def spawn_from_hits(conn) -> list:
    """爆款库新货 → 选题。同题簇(hits.cluster_id)出一个选题:代表作=超额最高,
    挂多源(其余成员靠 cluster_id 关联,逆向时整簇拆)、强度按簇大小加成、标签合并。
    返回 [(topic_id, 代表hit行, 簇大小)],按加成后强度降序。"""
    import json as _json
    # 已被选题覆盖的簇(任一成员当过 source_hit)→ 去重,避免重复出题
    done = set()
    for (shid,) in conn.execute("SELECT source_hit_id FROM topics WHERE source_hit_id IS NOT NULL"):
        r = conn.execute("SELECT COALESCE(cluster_id, id) AS ck FROM hits WHERE id=?", (shid,)).fetchone()
        if r:
            done.add(r["ck"])
    rows = conn.execute(
        """SELECT h.*, c.name AS comp_name, c.domain AS domain,
                  COALESCE(h.cluster_id, h.id) AS ck
           FROM hits h JOIN competitor_accounts c ON c.id = h.competitor_id
           ORDER BY h.excess_ratio DESC""").fetchall()
    clusters: dict = {}
    for h in rows:
        clusters.setdefault(h["ck"], []).append(h)

    new_topics = []
    for ck, members in clusters.items():
        if ck in done:
            continue
        rep = max(members, key=lambda m: m["excess_ratio"] or 0)
        size = len(members)
        heat = round((rep["excess_ratio"] or 1.0) * (1 + CLUSTER_BOOST * (size - 1)), 3)
        tagset = []                                    # 合并簇内标签
        for m in members:
            for t in (_json.loads(m["tags"]) if m["tags"] else []):
                if t not in tagset:
                    tagset.append(t)
        tags = _json.dumps(tagset, ensure_ascii=False) if tagset else rep["tags"]
        cur = conn.execute(
            """INSERT INTO topics(title, domain, source_type, source_hit_id, tags,
                                  initial_heat, current_heat, status, pushed_at)
               VALUES(?,?, 'hit', ?, ?, ?, ?, 'pushed', datetime('now','localtime'))""",
            (clean_title(rep["title"]), rep["domain"], rep["id"], tags, heat, heat))
        new_topics.append((cur.lastrowid, rep, size))
    new_topics.sort(key=lambda x: (x[1]["excess_ratio"] or 0) * (1 + CLUSTER_BOOST * (x[2] - 1)),
                    reverse=True)
    return new_topics


def build_card(conn, new_topics: list) -> tuple[str, list[str], str]:
    today = datetime.now().strftime("%Y-%m-%d")
    blocks = []

    if new_topics:
        shown = new_topics[:TOP_N_NEW]          # 已按加成后强度降序,取前 N
        lines = [f"**🔥 新晋爆款选题**(按强度排,共 {len(new_topics)} 条取前 {len(shown)})"]
        for tid, h, size in shown:
            sc = f" · 转评比{h['share_comment_ratio']}" if h["share_comment_ratio"] else ""
            multi = f" · 🔥{size}源同题" if size > 1 else ""
            lines.append(f"**[{tid}]** {clean_title(h['title'])}{multi}\n"
                         f"　↳ 超额 {h['excess_ratio']}x · {h['like_count']}赞{sc}"
                         f" · {h['comp_name']} · [原片]({h['url']})")
        if len(new_topics) > len(shown):
            lines.append(f"　…另有 {len(new_topics) - len(shown)} 条已入库,明日进候选池。")
        blocks.append("\n".join(lines))
    else:
        blocks.append("**🔥 新晋爆款选题**\n今日无新晋。")

    cands = conn.execute(
        """SELECT id, title, current_heat, reheat_count, last_reheat_at FROM topics
           WHERE status='candidate' ORDER BY current_heat DESC LIMIT ?""",
        (TOP_N_CANDIDATES,)).fetchall()
    if cands:
        lines = ["**📦 候选池**(按当前热度排,🔥=回温)"]
        for c in cands:
            hot = " 🔥" if (c["last_reheat_at"] or "") >= datetime.now().strftime("%Y-%m-%d") else ""
            lines.append(f"**[{c['id']}]** {c['title']} · 热度 {c['current_heat']}{hot}")
        blocks.append("\n".join(lines))

    return (f"每日选题 · {today}", blocks, "回复:选 <编号> 开工 / 弃 <编号> 沉底")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="只算不推")
    a = p.parse_args()
    conn = connect()
    with conn:
        n_roll = rollover(conn)
        n_reheat, n_expire = maintain_candidates(conn)
        new_topics = spawn_from_hits(conn)
    header, blocks, note = build_card(conn, new_topics)
    print(f"翻页进候选 {n_roll} | 回温 {n_reheat} | 淘汰 {n_expire} | 新晋选题 {len(new_topics)}")
    if a.dry_run:
        print("\n".join(blocks))
    else:
        push.send_card(header, blocks, note)
        print("已推飞书")
    conn.close()


if __name__ == "__main__":
    main()
