"""One-way copy-and-verify helper for moving active runtime data out of the build tree."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.core.runtime.runtime_storage import (
    CONFIG_PATH,
    RuntimeStorageError,
    _config,
    _formal_runtime_root,
    _legacy_data_root,
    file_digest,
    runtime_identity_receipt,
)


LIVE_RELATIVE_PATHS = (
    "formal/production_activation.sqlite3",
    "formal/competitor_registration",
    "formal/backups",
    "agent_platform",
    "local",
)


def _bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _validate_relocation_target(raw_target: str | Path) -> Path:
    target = Path(raw_target).expanduser().resolve()
    try:
        target.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeStorageError("new formal runtime root must stay outside the build root")
    if target.exists():
        raise RuntimeStorageError("new formal runtime root already exists; refusing to merge or overwrite it")
    return target


def _plan(*, source_root: Path, target_root: Path, status: str, mode: str) -> dict[str, Any]:
    entries = []
    for relative in LIVE_RELATIVE_PATHS:
        source = source_root / relative
        entries.append({
            "relative_path": relative,
            "exists": source.exists(),
            "bytes": _bytes(source) if source.exists() else 0,
        })
    return {
        "migration_status": status,
        "mode": mode,
        "source_root": str(source_root),
        "target_root": str(target_root),
        "entries": entries,
        "excluded_external_cache": "formal/competitor_registration_runtime",
        "reason": "the excluded cache is not referenced by the formal database and must not become a second formal data store",
    }


def plan() -> dict[str, Any]:
    config = _config()
    return _plan(
        source_root=_legacy_data_root(config),
        target_root=_formal_runtime_root(config),
        status=str(config.get("migration_status") or ""),
        mode="initial_migration",
    )


def plan_relocation(target: str | Path) -> dict[str, Any]:
    """Check a proposed new storage location without copying or changing configuration."""
    if str(_config().get("migration_status") or "") != "completed":
        raise RuntimeStorageError("formal runtime relocation requires one completed active runtime root")
    source_root = _formal_runtime_root(_config())
    target_root = _validate_relocation_target(target)
    if source_root == target_root:
        raise RuntimeStorageError("new formal runtime root must differ from the active root")
    return _plan(source_root=source_root, target_root=target_root, status="completed", mode="relocation")


def _copy(source: Path, target: Path) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return
    shutil.copytree(source, target, dirs_exist_ok=False)


def _replace_legacy_references(database: Path, *, old_root: Path, new_root: Path) -> int:
    old_text, new_text = str(old_root.resolve()), str(new_root.resolve())
    changed = 0
    with sqlite3.connect(database) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            for _, name, kind, *_ in columns:
                if "CHAR" not in str(kind).upper() and "TEXT" not in str(kind).upper() and kind:
                    continue
                rows = connection.execute(
                    f'SELECT rowid, "{name}" FROM "{table}" WHERE "{name}" LIKE ?',
                    (f"%{old_text}%",),
                ).fetchall()
                for rowid, value in rows:
                    updated = str(value).replace(old_text, new_text)
                    connection.execute(
                        f'UPDATE "{table}" SET "{name}"=? WHERE rowid=?',
                        (updated, rowid),
                    )
                    changed += 1
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeStorageError(f"copied formal database failed integrity check: {integrity}")
    return changed


def copy_and_verify() -> dict[str, Any]:
    config = _config()
    if config.get("migration_status") != "preparing":
        raise RuntimeStorageError("runtime storage copy requires preparing status")
    source_root, target_root = _legacy_data_root(config), _formal_runtime_root(config)
    if target_root.exists():
        raise RuntimeStorageError("target runtime root already exists; refusing to merge or overwrite it")
    target_root.mkdir(parents=True, exist_ok=False)
    try:
        for relative in LIVE_RELATIVE_PATHS:
            source = source_root / relative
            if source.exists():
                _copy(source, target_root / relative)
        database = target_root / "formal" / str(config["formal_database_filename"])
        changed_references = _replace_legacy_references(
            database,
            old_root=source_root,
            new_root=target_root,
        )
        receipt = {
            "contract_version": config["contract_version"],
            "formal_runtime_root": str(target_root.resolve()),
            "formal_database": str(database.resolve()),
            "formal_database_sha256": file_digest(database),
            "rewritten_legacy_path_values": changed_references,
            "copy_plan": plan(),
        }
        receipt_path = target_root / str(config["runtime_identity_receipt"])
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return receipt
    except Exception:
        # The target remains for inspection; never delete an incomplete copy automatically.
        raise


def relocate_active_runtime(target: str | Path) -> dict[str, Any]:
    """Copy the one active formal store to a new empty location and switch only after verification."""
    config = _config()
    if config.get("migration_status") != "completed":
        raise RuntimeStorageError("formal runtime relocation requires an active completed runtime root")
    source_root = _formal_runtime_root(config)
    target_root = _validate_relocation_target(target)
    if source_root == target_root:
        raise RuntimeStorageError("new formal runtime root must differ from the active root")
    source_receipt = runtime_identity_receipt()
    relocation_plan = _plan(
        source_root=source_root,
        target_root=target_root,
        status="completed",
        mode="relocation",
    )
    target_root.mkdir(parents=True, exist_ok=False)
    try:
        for relative in LIVE_RELATIVE_PATHS:
            source = source_root / relative
            if source.exists():
                _copy(source, target_root / relative)
        database = target_root / "formal" / str(config["formal_database_filename"])
        changed_references = _replace_legacy_references(
            database,
            old_root=source_root,
            new_root=target_root,
        )
        receipt = {
            "contract_version": config["contract_version"],
            "formal_runtime_root": str(target_root.resolve()),
            "formal_database": str(database.resolve()),
            "formal_database_sha256": file_digest(database),
            "rewritten_active_path_values": changed_references,
            "relocated_from": str(source_root),
            "source_identity_receipt": source_receipt,
            "copy_plan": relocation_plan,
        }
        receipt_path = target_root / str(config["runtime_identity_receipt"])
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        verified = json.loads(receipt_path.read_text(encoding="utf-8"))
        if verified.get("formal_database_sha256") != file_digest(database):
            raise RuntimeStorageError("new formal runtime root failed identity verification")
        updated = dict(config)
        updated["formal_runtime_root"] = str(target_root.resolve())
        updated["last_relocation"] = {
            "from": str(source_root),
            "to": str(target_root),
            "identity_receipt": str(receipt_path),
        }
        CONFIG_PATH.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt
    except Exception:
        # Leave an incomplete target available for inspection. Never merge, overwrite, or delete it automatically.
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy-and-verify", action="store_true")
    args = parser.parse_args()
    result = copy_and_verify() if args.copy_and_verify else plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
