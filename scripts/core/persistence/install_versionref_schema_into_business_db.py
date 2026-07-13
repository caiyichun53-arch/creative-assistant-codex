"""Installs the goal01/02/03 VersionRef/experience schema chain into the real
business database (data/formal/production_activation.sqlite3), additively --
alongside the existing hits/topic_candidates/etc. tables, not replacing them.

Why this exists: BR-EXPERIENCE-001 ("经验共享库") requires experience to have a
real formal-artifact truth source. That machinery (VersionRef, trace_root/
trace_version, tactic_state) was designed and built (scripts/core/production/
goal08_production_chain.py, scripts/core/experience/goal09_experiments.py) but
bound to a schema that only ever ran against an empty, disposable clean-room
database (data/formal/clean_room_v0_6_2.sqlite3, see scripts/validation/
clean_room_empty_db.py) -- it never touched real data. This script closes that
gap by installing the same schema chain into the real business database,
confirmed to have zero table-name collisions with the existing business schema.

This is NOT scripts/validation/clean_room_empty_db.py's install_schema(): that
function requires every table to be empty afterward (it's for a disposable
validation DB). This one is for an existing, populated real database and
enforces the opposite invariant -- every pre-existing table's row content must
be byte-for-byte unchanged after install. It also refuses to silently accept a
same-named table with a different column structure (CREATE TABLE IF NOT EXISTS
would otherwise no-op on it and this function would look like it "succeeded"
against a broken schema).

Usage:
    python -m scripts.core.persistence.install_versionref_schema_into_business_db --db data/formal/production_activation.sqlite3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_baseline_citations  # noqa: E402

SCHEMA_CHAIN = (
    ROOT / "scripts" / "core" / "persistence" / "goal01_schema.sqlite.sql",
    ROOT / "scripts" / "core" / "persistence" / "goal02_schema.sqlite.sql",
    ROOT / "scripts" / "core" / "persistence" / "goal03_schema.sqlite.sql",
)

class SchemaCollisionError(RuntimeError):
    """Raised when a table the schema chain is about to create already exists
    in the target database with a different column structure than the schema
    file defines -- CREATE TABLE IF NOT EXISTS would silently no-op on it,
    which would otherwise look like a successful install of a broken schema."""


class BusinessDataMutatedError(RuntimeError):
    """Raised when any pre-existing business table's row content changed
    across the install -- this script must be purely additive."""


def _safe_db_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    allowed = (ROOT / "data" / "formal").resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError(f"VersionRef schema install must target a db under data/formal: {resolved}")
    if resolved.name == "creation.db":
        raise ValueError("refusing to write old data/creation.db")
    return resolved


def _table_columns_from_schema(sql_text: str) -> dict[str, list[str]]:
    """Executes the schema text into a scratch in-memory SQLite connection and
    reads back each resulting table's real column list via PRAGMA table_info
    -- this is what SQLite itself thinks the table looks like, not a hand-
    rolled parse of the CREATE TABLE text (which is exactly the kind of thing
    that goes subtly wrong on CHECK(...)/UNIQUE(...) clauses containing
    commas)."""
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(sql_text)
        table_names = [
            row[0]
            for row in scratch.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {
            name: [col[1] for col in scratch.execute(f'PRAGMA table_info("{name}")').fetchall()]
            for name in table_names
        }
    finally:
        scratch.close()


def _existing_table_columns(conn: sqlite3.Connection, table_name: str) -> list[str] | None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchall()
    if not rows:
        return None
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]


def check_no_schema_collision(conn: sqlite3.Connection, schema_paths: tuple[Path, ...] = SCHEMA_CHAIN) -> None:
    """Raises SchemaCollisionError if any table the schema chain would create
    already exists in conn with a different column set. Must be called BEFORE
    executing the schema chain -- CREATE TABLE IF NOT EXISTS gives no signal
    on its own if a same-named, wrongly-shaped table is already present."""
    for schema_path in schema_paths:
        expected_tables = _table_columns_from_schema(schema_path.read_text(encoding="utf-8"))
        for table_name, expected_columns in expected_tables.items():
            existing_columns = _existing_table_columns(conn, table_name)
            if existing_columns is not None and existing_columns != expected_columns:
                raise SchemaCollisionError(
                    f"table {table_name!r} already exists with columns {existing_columns} "
                    f"but the schema chain defines it as {expected_columns} -- refusing to "
                    "install (CREATE TABLE IF NOT EXISTS would silently no-op on this)"
                )


def _existing_business_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
         WHERE type='table' AND name NOT LIKE 'sqlite_%'
         ORDER BY name
        """
    ).fetchall()
    return [row[0] for row in rows]


def snapshot_business_tables(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Row count + a canonical content hash per pre-existing table, so a
    caller can prove byte-for-byte equality before vs after a schema install."""
    import hashlib

    snapshot: dict[str, dict[str, Any]] = {}
    for table_name in _existing_business_table_names(conn):
        rows = conn.execute(f'SELECT * FROM "{table_name}" ORDER BY rowid').fetchall()
        payload = json.dumps([list(row) for row in rows], ensure_ascii=False, sort_keys=True, default=str)
        snapshot[table_name] = {
            "row_count": len(rows),
            "content_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
    return snapshot


def diff_snapshots(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Returns only the tables that changed between two snapshots. A table
    present in `before` must still be present in `after` with an identical
    row_count/content_hash, or it is reported here as a violation."""
    changed: dict[str, dict[str, Any]] = {}
    for table_name, before_state in before.items():
        after_state = after.get(table_name)
        if after_state != before_state:
            changed[table_name] = {"before": before_state, "after": after_state}
    return changed


def backup_path_for(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(f"{db_path.stem}_pre_versionref_schema_{timestamp}{db_path.suffix}")


def install(db_path: Path, *, skip_backup: bool = False) -> dict[str, Any]:
    require_baseline_citations(["7", "9", "18"])
    resolved = _safe_db_path(db_path)
    if not resolved.exists():
        raise FileNotFoundError(f"target business database does not exist: {resolved}")

    backup: Path | None = None
    if not skip_backup:
        backup = backup_path_for(resolved)
        shutil.copy2(resolved, backup)

    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        before = snapshot_business_tables(conn)
        check_no_schema_collision(conn)
        for schema_path in SCHEMA_CHAIN:
            if not schema_path.exists():
                raise FileNotFoundError(f"missing schema file: {schema_path}")
            conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()
        after = snapshot_business_tables(conn)
        mutated = diff_snapshots(before, after)
        if mutated:
            raise BusinessDataMutatedError(
                f"pre-existing business table(s) changed during schema install: {mutated}"
            )
        new_tables = sorted(set(after) - set(before))
        return {
            "database": resolved.as_posix(),
            "backup": backup.as_posix() if backup else None,
            "pre_existing_tables_unchanged": len(before),
            "new_tables_installed": new_tables,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="path to the real business database, under data/formal")
    parser.add_argument("--skip-backup", action="store_true", help="do not copy a pre-install backup (testing only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = install(Path(args.db), skip_backup=args.skip_backup)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"database: {result['database']}")
        print(f"backup: {result['backup']}")
        print(f"pre-existing tables unchanged: {result['pre_existing_tables_unchanged']}")
        print(f"new tables installed: {', '.join(result['new_tables_installed'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
