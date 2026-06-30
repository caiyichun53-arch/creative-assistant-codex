"""扩展 language_fuel_items,支撑豆瓣多频道采集。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(language_fuel_items)")}
    columns = {
        "channel": "TEXT",
        "source_url": "TEXT",
        "word_count": "INTEGER",
        "content_hash": "TEXT",
        "quality_status": "TEXT NOT NULL DEFAULT 'raw'",
        "privacy_status": "TEXT NOT NULL DEFAULT 'clean'",
    }
    with conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE language_fuel_items ADD COLUMN {name} {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lfuel_channel "
            "ON language_fuel_items(platform, channel, source_kind, collected_at)"
        )
    conn.close()
    print("迁移完成: language_fuel_items 豆瓣多频道字段")


if __name__ == "__main__":
    main()
