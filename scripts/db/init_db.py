"""初始化/升级 SQLite 数据库:执行 schema.sql(全部 IF NOT EXISTS,可重复跑)。

用法: python scripts/db/init_db.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        print(f"OK {DB_PATH}")
        print("tables/views:", ", ".join(tables))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
