from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
DEFAULT_DB = ROOT / "data" / "formal" / "clean_room_v0_6_2.sqlite3"
ALLOWED_DB_ROOTS = (
    ROOT / "data" / "formal",
    ROOT / "validation_evidence",
    ROOT / "tests" / "fixtures",
)


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}


def configured_db_path(settings: dict[str, Any] | None = None) -> Path:
    settings = settings or load_settings()
    rel = (
        settings.get("storage", {}).get("local_validation_db")
        or settings.get("paths", {}).get("db")
        or DEFAULT_DB.relative_to(ROOT).as_posix()
    )
    path = Path(str(rel))
    if not path.is_absolute():
        path = ROOT / path
    return path


def sqlite_schema_chain(settings: dict[str, Any] | None = None) -> list[Path]:
    settings = settings or load_settings()
    configured = settings.get("storage", {}).get("sqlite_schema_chain") or [
        "scripts/core/persistence/goal01_schema.sqlite.sql",
        "scripts/core/persistence/goal02_schema.sqlite.sql",
        "scripts/core/persistence/goal03_schema.sqlite.sql",
    ]
    paths = []
    for item in configured:
        path = Path(str(item))
        if not path.is_absolute():
            path = ROOT / path
        paths.append(path)
    return paths


def ensure_safe_db_path(db_path: Path) -> Path:
    resolved = db_path.resolve()
    allowed = [root.resolve() for root in ALLOWED_DB_ROOTS]
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise ValueError(f"database path is outside allowed clean-room roots: {resolved}")
    if resolved.name == "creation.db" or resolved.as_posix().endswith("/data/creation.db"):
        raise ValueError("clean-room database must not be data/creation.db")
    return resolved


def install_schema(db_path: Path, *, destroy_existing: bool = False) -> dict[str, Any]:
    resolved = ensure_safe_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if destroy_existing and resolved.exists():
        resolved.unlink()
    conn = sqlite3.connect(resolved)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for schema_path in sqlite_schema_chain():
            if not schema_path.exists():
                raise FileNotFoundError(f"missing schema file: {schema_path}")
            conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()
        counts = table_counts(conn)
        non_empty = {name: count for name, count in counts.items() if count != 0}
        if non_empty:
            raise RuntimeError(f"formal clean-room DB is not empty: {non_empty}")
        return {
            "database": resolved.as_posix(),
            "schema_chain": [path.relative_to(ROOT).as_posix() for path in sqlite_schema_chain()],
            "table_count": len(counts),
            "table_rows": counts,
        }
    finally:
        conn.close()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type='table'
           AND name NOT LIKE 'sqlite_%'
         ORDER BY name
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for (table_name,) in rows:
        counts[str(table_name)] = int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
    return counts


def health_check(db_path: Path) -> dict[str, Any]:
    resolved = ensure_safe_db_path(db_path)
    if not resolved.exists():
        raise FileNotFoundError(f"formal clean-room DB missing: {resolved}")
    conn = sqlite3.connect(resolved)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        counts = table_counts(conn)
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        non_empty = {name: count for name, count in counts.items() if count != 0}
        if foreign_key_errors:
            raise RuntimeError(f"foreign key check failed: {foreign_key_errors}")
        if non_empty:
            raise RuntimeError(f"formal clean-room DB is not empty: {non_empty}")
        return {
            "database": resolved.as_posix(),
            "table_count": len(counts),
            "table_rows": counts,
            "foreign_key_check": "passed",
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Create or verify the GOAL-DATA-RESET-01 empty formal validation DB.")
    parser.add_argument("--db", help="SQLite validation DB path. Defaults to config storage.local_validation_db.")
    parser.add_argument("--init", action="store_true", help="Install the formal schema chain.")
    parser.add_argument("--destroy-existing", action="store_true", help="Delete the target validation DB before init.")
    parser.add_argument("--health", action="store_true", help="Verify the DB exists, schema loads and all formal tables are empty.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of YAML.")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else configured_db_path()
    if not args.init and not args.health:
        args.health = True
    result = install_schema(db_path, destroy_existing=args.destroy_existing) if args.init else health_check(db_path)
    if args.health and args.init:
        result["health"] = health_check(db_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
