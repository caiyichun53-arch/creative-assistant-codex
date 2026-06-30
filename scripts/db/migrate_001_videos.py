"""迁移001:observation_pool 表 → competitor_videos 表 + observation_pool 视图。

背景:观察池语义修正——只放"新发布待复查"的视频;存量视频是基线材料(archived)。
可重复跑(已迁移则跳过)。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(ROOT / "data" / "creation.db")
conn.execute("PRAGMA foreign_keys = OFF")

is_table = conn.execute(
    "SELECT type FROM sqlite_master WHERE name='observation_pool'").fetchone()
if is_table and is_table[0] == "table":
    with conn:
        conn.executescript("""
            ALTER TABLE observation_pool RENAME TO competitor_videos;
            DROP INDEX IF EXISTS idx_obs_status;
            CREATE INDEX IF NOT EXISTS idx_vid_status
                ON competitor_videos(status, last_checked_at);
            CREATE VIEW observation_pool AS
                SELECT * FROM competitor_videos WHERE status = 'watching';
        """)
    print("迁移完成: competitor_videos 表 + observation_pool 视图")
else:
    print("已是新结构,跳过")
conn.close()
