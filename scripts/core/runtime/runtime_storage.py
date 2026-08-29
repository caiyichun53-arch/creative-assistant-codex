"""Resolve and verify the one active location for formal runtime data."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = PROJECT_ROOT / "config" / "runtime_storage.json"


class RuntimeStorageError(RuntimeError):
    pass


RuntimeIdentity = Literal["production", "test"]


def _config() -> dict[str, Any]:
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeStorageError("runtime storage configuration must be one object")
    return value


def _formal_runtime_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("formal_runtime_root") or "").strip()
    if not raw:
        raise RuntimeStorageError("formal runtime root is missing")
    root = Path(os.path.expandvars(raw)).resolve()
    try:
        root.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return root
    raise RuntimeStorageError("formal runtime root must stay outside the build root")


def _test_runtime_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("test_runtime_root") or "").strip()
    if not raw:
        raise RuntimeStorageError("test runtime root is missing")
    root = Path(os.path.expandvars(raw)).resolve()
    try:
        root.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return root
    raise RuntimeStorageError("test runtime root must stay outside the build root")


def _assert_runtime_roots_are_disjoint(
    formal_root: Path,
    test_root: Path,
) -> None:
    for left, right in ((formal_root, test_root), (test_root, formal_root)):
        try:
            right.relative_to(left)
        except ValueError:
            continue
        raise RuntimeStorageError("formal and test runtime roots must be disjoint")


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


def active_runtime_root(*, data_identity: str) -> Path:
    """Resolve an explicitly selected active runtime identity."""
    return runtime_root_for_identity(data_identity)


def runtime_root_for_identity(data_identity: str) -> Path:
    """Resolve one explicit runtime identity; never infer TEST from a path."""
    identity = str(data_identity or "").strip().lower()
    if identity not in {"production", "test"}:
        raise RuntimeStorageError(
            "runtime identity must be explicitly 'production' or 'test'"
        )
    config = _config()
    formal_root = _formal_runtime_root(config)
    test_root = _test_runtime_root(config)
    _assert_runtime_roots_are_disjoint(formal_root, test_root)
    if identity == "production":
        if str(config.get("migration_status") or "") != "completed":
            raise RuntimeStorageError(
                "formal runtime root is not active until migration is completed"
            )
        return formal_root
    return test_root


def formal_database_path() -> Path:
    config = _config()
    return runtime_root_for_identity("production") / "formal" / str(
        config["formal_database_filename"]
    )


def database_path_for_identity(data_identity: str) -> Path:
    identity = str(data_identity or "").strip().lower()
    config = _config()
    filename_key = (
        "formal_database_filename"
        if identity == "production"
        else "test_database_filename"
    )
    filename = str(config.get(filename_key) or "").strip()
    if not filename or Path(filename).name != filename:
        raise RuntimeStorageError(
            f"{identity or 'unspecified'} database filename is missing or invalid"
        )
    subdirectory = "formal" if identity == "production" else "test"
    return runtime_root_for_identity(identity) / subdirectory / filename


def runtime_path(*parts: str, data_identity: str | None = None) -> Path:
    return runtime_root_for_identity(data_identity).joinpath(*parts)


def require_runtime_path(
    path: Path | str,
    *,
    purpose: str,
    data_identity: str,
) -> Path:
    """Require an artifact to stay under its explicitly selected runtime root."""
    root = runtime_root_for_identity(data_identity).resolve()
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


def runtime_storage_overview(*, data_identity: str) -> dict[str, Any]:
    """Return a small, human-facing description without exposing raw internals as a workbench."""
    config = _config()
    identity = str(data_identity or "").strip().lower()
    root = runtime_root_for_identity(identity)
    database = database_path_for_identity(identity)
    formal_root = runtime_root_for_identity("production")
    formal_database = database_path_for_identity("production")
    test_root = runtime_root_for_identity("test")
    test_database = database_path_for_identity("test")
    receipt = (
        runtime_identity_receipt()
        if identity == "production" and migration_status() == "completed"
        else None
    )
    return {
        "status": migration_status(),
        "data_identity": identity,
        "runtime_root": str(root),
        "database": str(database),
        "formal_runtime_root": str(formal_root),
        "formal_database": str(formal_database),
        "test_runtime_root": str(test_root),
        "test_database": str(test_database),
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
