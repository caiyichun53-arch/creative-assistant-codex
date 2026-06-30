"""Migration 007: add classified language-fuel insight storage.

Raw collection is deterministic and lives in source tables. This table stores
the LLM-produced, categorized writing experience that can be retrieved by the
creation flow.
"""
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
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='language_fuel_insights'"
        ).fetchone()
        if not exists:
            raise RuntimeError("language_fuel_insights was not created")
        print("migration complete: language_fuel_insights")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
