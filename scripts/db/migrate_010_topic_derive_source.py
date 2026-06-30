"""迁移010:topics 加 derive_source(衍生来源类别)+ derive_detail(来源原文/评论依据)。

衍生选题(source_type='spinoff')要能追溯"从哪儿延展来的"——是评论真追问长出来的
(拆解·可裂变选题),还是研究里的信息缺口/争议。评论延展信号最强,值得重点关注。
spinoff.py 建衍生选题时写入这两列;prepare_topic 组装 brief 时显示。可重复跑。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(ROOT / "data" / "creation.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(topics)")]
with conn:
    if "derive_source" not in cols:
        conn.execute("ALTER TABLE topics ADD COLUMN derive_source TEXT")  # 如 拆解·可裂变选题 / 研究·争议
        print("迁移完成: topics.derive_source 就位")
    else:
        print("topics.derive_source 已存在,跳过")
    if "derive_detail" not in cols:
        conn.execute("ALTER TABLE topics ADD COLUMN derive_detail TEXT")  # 来源原文(可裂变项含依据评论)
        print("迁移完成: topics.derive_detail 就位")
    else:
        print("topics.derive_detail 已存在,跳过")
conn.close()
