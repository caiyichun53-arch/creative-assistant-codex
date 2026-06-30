"""Migration 008: add shared comment corpus and content atom tables."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SCHEMA_PATH = ROOT / "scripts" / "db" / "schema.sql"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('language_fuel_comments', 'content_atoms')"
            )
        }
        missing = {"language_fuel_comments", "content_atoms"} - tables
        if missing:
            raise RuntimeError(f"missing tables: {', '.join(sorted(missing))}")
        print("migration complete: language_fuel_comments, content_atoms")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
