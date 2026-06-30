"""爆款同题聚类(确定性·本地 embedding·无 LLM)。

同一选题多个作者做的(洗稿/二创)→ 归一簇,写回 hits.cluster_id(=簇内代表作id,
代表=超额最高;独一份留 NULL)。选题侧据此一簇出一个选题、挂多源;逆向整簇拆。

模型:BAAI/bge-small-zh-v1.5(本地,模型库 reverse_engine.models_root 下)。
聚类:标题(去#标签)向量化 → 余弦≥阈值连边 → 连通分量。纯 numpy,无 sklearn。

同时算逆向排队分 reverse_priority(步骤1=强度 log1p(超额);代表作+独一份才给分,
洗稿非代表留 NULL → 逆向队列排除)。新爆款入库后应重跑(重聚类+刷新 priority)。

⚠️ 依赖 embedding 库(modelscope/sentence_transformers),装在 asr venv,**必须用 asr venv 的 python 跑**:
  tools/asr/.venv/Scripts/python.exe scripts/analyze/cluster_hits.py            # 跑并写回(阈值默认0.82)
  tools/asr/.venv/Scripts/python.exe scripts/analyze/cluster_hits.py --dry-run   # 只看分簇不写库
  tools/asr/.venv/Scripts/python.exe scripts/analyze/cluster_hits.py --threshold 0.85
"""
import argparse
import math
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
DB = ROOT / "data" / "creation.db"
MODELS_ROOT = Path(SETTINGS["reverse_engine"]["models_root"])
MS_CACHE = MODELS_ROOT / "modelscope_cache"            # 走 ModelScope(国内可靠,HF镜像连不上)
MODEL_ID = "AI-ModelScope/bge-small-zh-v1.5"
TAG_RE = re.compile(r"#\S+")
EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")


def clean(title: str) -> str:
    return EMOJI.sub("", TAG_RE.sub("", title or "")).strip(" #@~　") or "(无标题)"


def cluster(sim: np.ndarray, th: float) -> list[int]:
    """余弦≥th 连边 → 连通分量(并查集)。返回每行的 root 下标。"""
    n = len(sim)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= th:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    return [find(i) for i in range(n)]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=float, default=0.82)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, title, excess_ratio FROM hits ORDER BY id").fetchall()
    if not rows:
        print("没有爆款")
        return
    titles = [clean(r["title"]) for r in rows]

    print(f"加载 {MODEL_ID} …({len(rows)} 条爆款,阈值 {a.threshold})")
    from modelscope import snapshot_download
    from sentence_transformers import SentenceTransformer
    local = snapshot_download(MODEL_ID, cache_dir=str(MS_CACHE))   # 已缓存即秒回,不联网
    model = SentenceTransformer(local)
    emb = model.encode(titles, normalize_embeddings=True, show_progress_bar=False)
    sim = np.asarray(emb) @ np.asarray(emb).T

    roots = cluster(sim, a.threshold)
    # 按 root 分组
    groups: dict[int, list[int]] = {}
    for idx, r in enumerate(roots):
        groups.setdefault(r, []).append(idx)

    # 每簇代表 = 超额最高;cluster_id = 代表的 hit id;独一份留 NULL
    assign: dict[int, int | None] = {}
    multi = 0
    for members in groups.values():
        if len(members) == 1:
            assign[rows[members[0]]["id"]] = None
            continue
        multi += 1
        rep = max(members, key=lambda m: rows[m]["excess_ratio"] or 0)
        rep_id = rows[rep]["id"]
        for m in members:
            assign[rows[m]["id"]] = rep_id

    # 打印多成员簇(同题组)
    print(f"\n=== 同题簇 {multi} 个(共 {len(rows)} 爆款 → {len(groups)} 簇)===")
    for members in sorted(groups.values(), key=len, reverse=True):
        if len(members) == 1:
            continue
        rep = max(members, key=lambda m: rows[m]["excess_ratio"] or 0)
        print(f"\n[簇·代表 hit {rows[rep]['id']}] {len(members)} 条同题:")
        for m in sorted(members, key=lambda m: rows[m]["excess_ratio"] or 0, reverse=True):
            mark = "★" if m == rep else " "
            print(f"  {mark} hit {rows[m]['id']:>3} 超额{rows[m]['excess_ratio']}x  {clean(rows[m]['title'])[:40]}")

    # reverse_priority = 逆向排队分。步骤1:只算"强度"=log1p(超额);"新意"软分留步骤2(届时 ×新意)。
    # 代表作 + 独一份 才给分;洗稼/二创非代表留 NULL → 逆向队列直接排除(同簇只拆代表)。
    id2excess = {r["id"]: (r["excess_ratio"] or 0) for r in rows}
    priority = {hid: (round(math.log1p(id2excess[hid]), 4) if (cid is None or cid == hid) else None)
                for hid, cid in assign.items()}

    if a.dry_run:
        print("\n(--dry-run 未写库)")
    else:
        with conn:
            for hid, cid in assign.items():
                conn.execute("UPDATE hits SET cluster_id=?, reverse_priority=? WHERE id=?",
                             (cid, priority[hid], hid))
        skipped = sum(1 for v in priority.values() if v is None)
        print(f"\n已写回 cluster_id + reverse_priority:{multi} 簇归并,洗稿非代表 {skipped} 条 priority=NULL(逆向不拆)。")
    conn.close()


if __name__ == "__main__":
    main()
