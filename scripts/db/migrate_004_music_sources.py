"""迁移004:新增音乐资料源表(网易云评论先落地)。

音乐评论不是抖音爆款逆向材料,单独存 music_tracks / music_comments,供音乐领域
创作备料和后续网友语感提炼读取。可重复跑。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'music_%'"
            )
        }
        print("迁移完成: " + ", ".join(sorted(tables)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
