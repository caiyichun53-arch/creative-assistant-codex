"""迁移002:加 video_checks 快照表(观察期内视频的每日复查记录,生长曲线原料)。

设计:基线指标统一为"出观察期时的点赞数"(由 daily 采集免费产生,基线自保鲜,
取消每周全量复刷);快照表攒生长曲线,将来做早期预警/强度细化,现在只捕获不建模。
可重复跑。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(ROOT / "data" / "creation.db")
with conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS video_checks (
            id            INTEGER PRIMARY KEY,
            video_id      INTEGER NOT NULL REFERENCES competitor_videos(id),
            checked_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            age_hours     REAL,              -- 距发布小时数(生长曲线的x轴)
            like_count    INTEGER,
            comment_count INTEGER,
            share_count   INTEGER,
            collect_count INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_checks_video ON video_checks(video_id, checked_at);
    """)
print("迁移完成: video_checks 表就位")
conn.close()
