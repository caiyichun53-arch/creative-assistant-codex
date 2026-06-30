"""迁移005:新增语感燃料通用原料表。

豆瓣长评、知乎回答/文章、MediaCrawler 支持平台等统一进 language_fuel_*。
网易云仍保留 music_* 专表,因为它兼具音乐资料属性。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'language_fuel_%'"
            )
        }
        print("迁移完成: " + ", ".join(sorted(tables)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
