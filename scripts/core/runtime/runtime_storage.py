"""Resolve and verify the one active location for formal runtime data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "config" / "runtime_storage.json"


class RuntimeStorageError(RuntimeError):
    pass


def _config() -> dict[str, Any]:
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeStorageError("runtime storage configuration must be one object")
    return value


def _formal_runtime_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("formal_runtime_root") or "").strip()
    if not raw:
        raise RuntimeStorageError("formal runtime root is missing")
    root = Path(raw).resolve()
    try:
        root.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return root
    raise RuntimeStorageError("formal runtime root must stay outside the build root")


def _legacy_data_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("legacy_build_root_data") or "").strip()
    candidate = (PROJECT_ROOT / raw).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeStorageError("legacy data root must be inside the build root") from exc
    return candidate


def migration_status() -> str:
    return str(_config().get("migration_status") or "")


def active_runtime_root() -> Path:
    config = _config()
    status = str(config.get("migration_status") or "")
    if status == "completed":
        return _formal_runtime_root(config)
    if status == "preparing":
        return _legacy_data_root(config)
    raise RuntimeStorageError("runtime storage migration status is invalid")


def formal_database_path() -> Path:
    config = _config()
    return active_runtime_root() / "formal" / str(config["formal_database_filename"])


def runtime_path(*parts: str) -> Path:
    return active_runtime_root().joinpath(*parts)


def require_runtime_path(path: Path | str, *, purpose: str) -> Path:
    """Require an externally stored artifact to stay under the active runtime root."""
    root = active_runtime_root().resolve()
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeStorageError(f"{purpose} must stay under the active runtime root") from exc
    return candidate


def runtime_identity_receipt_path() -> Path:
    config = _config()
    return _formal_runtime_root(config) / str(config["runtime_identity_receipt"])


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def runtime_identity_receipt() -> dict[str, Any]:
    """Verify the active formal storage identity.

    The digest written in the receipt is evidence that the database was copied
    intact at migration time.  It must not be compared with the live database:
    formal operations legitimately change that file after the cutover.  The
    live guard therefore verifies the immutable storage identity (contract,
    root and database path), while keeping the migration digest for audit.
    """
    config = _config()
    if str(config.get("migration_status")) != "completed":
        raise RuntimeStorageError("formal runtime root is not active until migration is completed")
    root = _formal_runtime_root(config)
    database = formal_database_path()
    receipt_path = runtime_identity_receipt_path()
    if not root.is_dir() or not database.is_file() or not receipt_path.is_file():
        raise RuntimeStorageError("formal runtime root or identity receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise RuntimeStorageError("runtime identity receipt is invalid")
    expected = {
        "contract_version": config.get("contract_version"),
        "formal_runtime_root": str(root),
        "formal_database": str(database),
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches:
        raise RuntimeStorageError("runtime identity receipt does not match the active formal data")
    if not isinstance(receipt.get("formal_database_sha256"), str) or not receipt["formal_database_sha256"]:
        raise RuntimeStorageError("runtime identity receipt is missing its migration verification digest")
    return receipt


def runtime_storage_overview() -> dict[str, Any]:
    """Return a small, human-facing description without exposing raw internals as a workbench."""
    config = _config()
    root = active_runtime_root()
    database = formal_database_path()
    receipt = runtime_identity_receipt() if migration_status() == "completed" else None
    return {
        "status": migration_status(),
        "formal_runtime_root": str(root),
        "formal_database": str(database),
        "identity_verified": receipt is not None,
        "description": "This location stores formal runtime records, source archives, and recovery state. It is not the human-readable knowledge library.",
        "change_rule": "A new location is copied, verified, and switched only after explicit confirmation. The previous location is retained and is never overwritten or automatically deleted.",
    }


def assert_formal_runtime_storage(*, database_path: Path | str) -> dict[str, Any]:
    config = _config()
    configured = formal_database_path().resolve()
    actual = Path(database_path).resolve()
    if actual != configured:
        raise RuntimeStorageError("formal operation is bound to a non-active data location")
    if str(config.get("migration_status")) == "completed":
        return runtime_identity_receipt()
    return {
        "migration_status": "preparing",
        "formal_database": str(configured),
        "legacy_root_in_use": True
    }
