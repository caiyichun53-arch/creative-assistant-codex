"""Minimal independent Creation Assistant Core entry.

This entry deliberately exposes only the Core startup/status boundary needed
for migration phase 1A.  It does not import agent-platform, transport,
knowledge, or model execution modules, and it never creates or changes
business state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.core.runtime.runtime_storage import (
    RuntimeStorageError,
    active_runtime_root,
    file_digest,
    formal_database_path,
    migration_status,
    runtime_identity_receipt,
)


PROJECT_NAME = "Creation Assistant"
FORMAL_DATA_IDENTITY = "production"


class CoreStartupError(RuntimeError):
    """Raised when the independent Core boundary cannot be established."""


def _open_read_only_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database using SQLite's read-only URI mode."""

    resolved = database_path.resolve()
    if not resolved.is_file():
        raise CoreStartupError(f"formal database does not exist: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _group_counts(connection: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT {column} AS value, COUNT(*) AS count FROM {table} GROUP BY {column} ORDER BY {column}"
    ).fetchall()
    return {str(row["value"]): int(row["count"]) for row in rows}


def _count_where(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    value: str,
) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",
        (value,),
    ).fetchone()
    return int(row["count"])


@dataclass
class CoreReadOnlySession:
    """A read-only view of the active formal Core storage."""

    connection: sqlite3.Connection
    runtime_root: Path
    database_path: Path
    identity_receipt: dict[str, Any]

    @classmethod
    def open_formal(cls) -> "CoreReadOnlySession":
        """Resolve the configured formal runtime and open its database read-only."""

        try:
            if migration_status() != "completed":
                raise CoreStartupError("formal runtime storage migration is not completed")
            runtime_root = active_runtime_root()
            database_path = formal_database_path().resolve()
            receipt = runtime_identity_receipt()
        except RuntimeStorageError as exc:
            raise CoreStartupError(str(exc)) from exc

        expected_database = (runtime_root / "formal" / database_path.name).resolve()
        if database_path != expected_database:
            raise CoreStartupError("formal database is not under the configured formal runtime")
        if receipt.get("formal_database") != str(database_path):
            raise CoreStartupError("runtime identity receipt does not match the formal database path")

        try:
            connection = _open_read_only_database(database_path)
        except (OSError, sqlite3.Error) as exc:
            raise CoreStartupError(f"formal database could not be opened read-only: {exc}") from exc
        return cls(
            connection=connection,
            runtime_root=runtime_root.resolve(),
            database_path=database_path,
            identity_receipt=receipt,
        )

    def close(self) -> None:
        self.connection.close()

    def basic_status(self) -> dict[str, Any]:
        """Read a small truthful status without starting any business action."""

        table_count = int(
            self.connection.execute(
                "SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'table'"
            ).fetchone()["count"]
        )
        daily_runs = _group_counts(self.connection, "stage0_daily_run", "lifecycle")
        cold_starts = _group_counts(self.connection, "stage0_cold_start", "status")
        discovery_runs = _group_counts(self.connection, "stage1b_discovery_run", "status")
        content_tasks = _group_counts(self.connection, "stage0_content_task", "current_status")
        unfinished = {
            "stage1b_discovery_processing": _count_where(
                self.connection,
                "stage1b_discovery_run",
                "status",
                "processing",
            )
            if "processing" in discovery_runs
            else 0,
            "content_node_processing": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage0_content_node_version "
                    "WHERE status = 'processing'"
                ).fetchone()["count"]
            ),
            "experience_candidate_run_running": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage0_experience_candidate_run "
                    "WHERE status = 'running'"
                ).fetchone()["count"]
            ),
            "candidate_awaiting_user_decision": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage1b_candidate_version "
                    "WHERE status = 'awaiting_user_decision'"
                ).fetchone()["count"]
            ),
        }
        unfinished_present = any(value > 0 for value in unfinished.values())

        migration_digest = str(self.identity_receipt.get("formal_database_sha256") or "")
        live_digest = file_digest(self.database_path) if self.database_path.is_file() else ""
        return {
            "project": PROJECT_NAME,
            "core_entry": "scripts.core.core_entry",
            "runtime_root": str(self.runtime_root),
            "formal_database": str(self.database_path),
            "database_identity": FORMAL_DATA_IDENTITY,
            "database_open": True,
            "access_mode": "read_only",
            "runtime_identity_verified": True,
            "database_path_matches_runtime_identity": True,
            "historical_migration_digest_matches_live_database": live_digest == migration_digest,
            "schema": {"table_count": table_count},
            "business_runs": {
                "daily_runs": {"total": sum(daily_runs.values()), "by_lifecycle": daily_runs},
                "cold_starts": {"total": sum(cold_starts.values()), "by_status": cold_starts},
                "discovery_runs": {
                    "total": sum(discovery_runs.values()),
                    "by_status": discovery_runs,
                },
                "content_tasks": {
                    "total": sum(content_tasks.values()),
                    "by_status": content_tasks,
                },
            },
            "unfinished_business_detected": unfinished_present,
            "unfinished_status_counts": unfinished,
            "automatic_business_execution": False,
            "model_required_for_startup": False,
            "external_agent_required_for_model_tasks": True,
            "hermes_started": False,
            "hermes_profile_read": False,
            "hermes_plugin_read": False,
            "model_called": False,
            "core_capabilities_without_external_agent": [
                "resolve_formal_runtime",
                "verify_runtime_identity",
                "open_formal_database_read_only",
                "read_basic_business_status",
            ],
            "later_external_agent_capabilities": [
                "model_backed_skill_execution",
                "model_result_submission",
            ],
            "historical_migration_digest_note": (
                "The migration digest is a historical copy-verification record, "
                "not a live write lock."
            ),
        }


def build_status() -> dict[str, Any]:
    session = CoreReadOnlySession.open_formal()
    try:
        return session.basic_status()
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Creation Assistant Core status")
    parser.parse_args(argv)
    try:
        print(json.dumps(build_status(), ensure_ascii=False, indent=2, sort_keys=True))
    except (CoreStartupError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "project": PROJECT_NAME,
                    "database_open": False,
                    "access_mode": "read_only",
                    "automatic_business_execution": False,
                    "model_called": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
