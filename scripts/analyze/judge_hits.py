"""爆款判定(全确定性,无 LLM):基线自算 + 过阈值升爆款库 + 观察到期归档。

每个 active 对标账号:
  1. 算基线:取"已定型"视频(发布 > observe_days,数据稳定)在滚动窗口内的
     点赞中位数 + P90,样本不足 min_samples 则扩窗到全部存量;再不足就跳过判定。
     基线快照存 baselines 表(留历史,可回看漂移)。
  2. 判定:扫该账号所有未晋升视频(watching + archived),
     like_count ≥ 中位数 × excess_threshold 且(可配)≥ P90 → 升爆款库 hits(浅入库)。
     已定型的存量爆款一次到位;观察中的新视频随复查数据增长随时可触发。
  3. 到期:watching 且发布超过 observe_days → archived(凉了,转为基线材料)。

用法: python scripts/analyze/judge_hits.py [--ids 1,2]
设计依据:AGENTS.md 两类池 + target-architecture 爆款判定(相对基线,数据自算非拍脑袋)。
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collect"))
sys.stdout.reconfigure(encoding="utf-8")
from common import SETTINGS, connect  # noqa: E402

CFG = SETTINGS["hit_detection"]


def p90(values: list[int]) -> float:
    return statistics.quantiles(sorted(values), n=10)[8] if len(values) >= 2 else float(values[0])


def get_sample(conn, comp_id: int) -> tuple[list, int]:
    """基线样本 = 已定型视频(发布 > observe_days),优先近 baseline_window_days 天;
    近窗口不足 baseline_min_samples 条 → 往前补到**最近 min_samples 条**(补足基础样本,
    **不是抓全部历史**)。爆款判定就在这批样本里找(基线与判定同一口径)。
    返回 (样本视频行 list, window_days 标记;0=触发了补足)。"""
    settled_before = (datetime.now() - timedelta(days=CFG["observe_days"])).strftime("%Y-%m-%d %H:%M:%S")
    window_start = (datetime.now() - timedelta(days=CFG["baseline_window_days"])).strftime("%Y-%m-%d %H:%M:%S")
    base_q = ("SELECT * FROM competitor_videos "
              "WHERE competitor_id=? AND like_count IS NOT NULL AND publish_time < ?")
    sample = conn.execute(base_q + " AND publish_time >= ? ORDER BY publish_time DESC",
                          (comp_id, settled_before, window_start)).fetchall()
    window_days = CFG["baseline_window_days"]
    if len(sample) < CFG["baseline_min_samples"]:          # 近窗口不足 → 补到最近 min_samples 条(非全量)
        sample = conn.execute(base_q + " ORDER BY publish_time DESC LIMIT ?",
                              (comp_id, settled_before, CFG["baseline_min_samples"])).fetchall()
        window_days = 0  # 0 = 触发补足样本(非纯 window 天口径)
    return sample, window_days


def compute_baseline(conn, comp_id: int):
    """在样本上算门槛基线;样本 < 2 无法算统计 → (None, [])。返回 (基线dict, 样本)。"""
    sample, window_days = get_sample(conn, comp_id)
    if len(sample) < 2:
        return None, []
    likes = [v["like_count"] for v in sample]
    bl = {"median": statistics.median(likes), "p90": p90(likes),
          "n": len(sample), "window_days": window_days}
    conn.execute(
        """INSERT INTO baselines(competitor_id, metric, window_days, sample_count,
                                 median_value, p90_value)
           VALUES(?, 'like_count', ?, ?, ?, ?)""",
        (comp_id, bl["window_days"], bl["n"], bl["median"], bl["p90"]))
    return bl, sample


def judge(conn, comp_id: int, bl: dict, sample: list) -> int:
    """爆款只在基线样本里找(基线与判定同一批视频,口径天生一致):
    样本里 like ≥ max(中位×N, P90) → hits 浅入库。返回新晋升数。"""
    threshold = bl["median"] * CFG["excess_threshold"]
    if CFG["p90_required"]:
        threshold = max(threshold, bl["p90"])
    promoted = 0
    for v in sample:
        if v["like_count"] < threshold:
            continue
        excess = round(v["like_count"] / bl["median"], 2) if bl["median"] else None
        sc_ratio = (round(v["share_count"] / v["comment_count"], 2)
                    if v["share_count"] and v["comment_count"] else None)
        conn.execute(
            """INSERT INTO hits(observation_id, competitor_id, platform_item_id, title, url,
                                publish_time, duration_sec, like_count, comment_count,
                                share_count, collect_count, excess_ratio, share_comment_ratio)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(competitor_id, platform_item_id) DO UPDATE SET
                 like_count = excluded.like_count,
                 comment_count = excluded.comment_count,
                 share_count = excluded.share_count,
                 collect_count = excluded.collect_count,
                 excess_ratio = excluded.excess_ratio,
                 share_comment_ratio = excluded.share_comment_ratio""",
            (v["id"], comp_id, v["platform_item_id"], v["title"], v["url"],
             v["publish_time"], v["duration_sec"], v["like_count"], v["comment_count"],
             v["share_count"], v["collect_count"], excess, sc_ratio))
        conn.execute("UPDATE competitor_videos SET status='promoted' WHERE id=?", (v["id"],))
        promoted += 1
    return promoted


def expire_watching(conn, comp_id: int) -> int:
    """观察到期(发布超过窗口)且没爆 → archived,转为基线材料。"""
    cutoff = (datetime.now() - timedelta(days=CFG["observe_days"])).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """UPDATE competitor_videos SET status='archived'
           WHERE competitor_id=? AND status='watching' AND publish_time < ?""",
        (comp_id, cutoff))
    return cur.rowcount


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ids", default=None, help="逗号分隔的 competitor id;缺省=全部 active")
    a = p.parse_args()
    conn = connect()
    if a.ids:
        ids = [int(x) for x in a.ids.split(",")]
        comps = conn.execute(
            f"SELECT id, name FROM competitor_accounts WHERE id IN ({','.join('?' * len(ids))})",
            ids).fetchall()
    else:
        comps = conn.execute(
            "SELECT id, name FROM competitor_accounts WHERE status='active'").fetchall()

    summary = []
    with conn:
        for c in comps:
            bl, sample = compute_baseline(conn, c["id"])
            if bl is None:
                summary.append(f"[{c['name']}] 样本不足(<2),跳过判定")
                continue
            n_hit = judge(conn, c["id"], bl, sample)
            n_exp = expire_watching(conn, c["id"])
            summary.append(
                f"[{c['name']}] 基线: 中位数 {bl['median']:.0f} / P90 {bl['p90']:.0f}"
                f" (样本{bl['n']},{'近'+str(bl['window_days'])+'天' if bl['window_days'] else '补足'}) | "
                f"新晋爆款 {n_hit} | 观察到期归档 {n_exp}")
    print("\n".join(summary))
    total = conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
    watching = conn.execute("SELECT COUNT(*) FROM observation_pool").fetchone()[0]
    print(f"\n爆款库共 {total} 条 | 观察池当前 {watching} 条")
    conn.close()


if __name__ == "__main__":
    main()
