"""迁移003:hits 加 cluster_id(同题聚类用)。

同一选题多个作者做的爆款(洗稿/二创)归一簇:选题侧一簇出一个选题(代表作=超额最高),
其余挂同 cluster_id;强度按簇大小加成;逆向时整簇一起拆。聚类由
scripts/analyze/cluster_hits.py(本地 embedding,确定性,无 LLM)算。可重复跑。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(ROOT / "data" / "creation.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(hits)")]
with conn:
    if "cluster_id" not in cols:
        conn.execute("ALTER TABLE hits ADD COLUMN cluster_id INTEGER")  # NULL=未聚类/独一簇
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hits_cluster ON hits(cluster_id)")
        print("迁移完成: hits.cluster_id 就位")
    else:
        print("hits.cluster_id 已存在,跳过")
conn.close()
