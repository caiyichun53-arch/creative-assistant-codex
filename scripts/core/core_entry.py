"""Read-only, unified Creation Assistant Core status boundary."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.core.runtime.runtime_storage import (
    RuntimeStorageError,
    database_path_for_identity,
    formal_database_path,
    migration_status,
    runtime_identity_receipt,
    runtime_root_for_identity,
)


PROJECT_NAME = "Creation Assistant"
FORMAL_DATA_IDENTITY = "production"
STATUS_SCHEMA_VERSION = "creation_assistant_core_status_v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CoreStartupError(RuntimeError):
    """Raised when the independent Core boundary cannot be established."""


def _open_read_only_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database using SQLite's read-only URI mode."""

    resolved = database_path.resolve()
    if not resolved.is_file():
        raise CoreStartupError(f"database does not exist: {resolved}")
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


def _read_json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CoreStartupError(f"configuration is not an object: {path}")
    return value


def _configuration() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return (
            _read_json_file(PROJECT_ROOT / "config" / "schedule_registry.json"),
            _read_json_file(PROJECT_ROOT / "config" / "external_executor.json"),
        )
    except (OSError, ValueError, TypeError) as exc:
        raise CoreStartupError(f"status configuration could not be read: {exc}") from exc


def _identity_name(data_identity: str) -> str:
    return "FORMAL" if data_identity == "production" else "TEST"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


@dataclass
class CoreReadOnlySession:
    """A read-only view of one explicitly selected Core storage identity."""

    connection: sqlite3.Connection
    runtime_root: Path
    database_path: Path
    identity_receipt: dict[str, Any]
    data_identity: str = FORMAL_DATA_IDENTITY

    @classmethod
    def open(
        cls,
        data_identity: str = FORMAL_DATA_IDENTITY,
        database_path: Path | str | None = None,
    ) -> "CoreReadOnlySession":
        """Resolve one explicit identity and open only its existing database read-only."""

        identity = str(data_identity or "").strip().lower()
        if identity not in {"production", "test"}:
            raise CoreStartupError("runtime identity must be explicitly 'production' or 'test'")

        try:
            if migration_status() != "completed":
                raise CoreStartupError("formal runtime storage migration is not completed")
            runtime_root = runtime_root_for_identity(identity).resolve()
            configured_formal = formal_database_path().resolve()
            selected_database = (
                Path(database_path).resolve()
                if database_path is not None
                else database_path_for_identity(identity).resolve()
            )
            receipt = runtime_identity_receipt() if identity == "production" else {
                "data_identity": "test",
            }
        except RuntimeStorageError as exc:
            raise CoreStartupError(str(exc)) from exc

        if identity == "production":
            expected_database = (runtime_root / "formal" / configured_formal.name).resolve()
            if selected_database != configured_formal or selected_database != expected_database:
                raise CoreStartupError("formal identity may only use the configured formal database")
            if receipt.get("formal_database") != str(selected_database):
                raise CoreStartupError("runtime identity receipt does not match the formal database path")
        else:
            if selected_database == configured_formal:
                raise CoreStartupError("TEST identity must never open the formal production database")
            for forbidden_root in (runtime_root_for_identity("production").resolve(), PROJECT_ROOT.resolve()):
                try:
                    selected_database.relative_to(forbidden_root)
                except ValueError:
                    continue
                raise CoreStartupError("TEST identity must not open formal runtime or build-root data")

        try:
            connection = _open_read_only_database(selected_database)
        except (OSError, sqlite3.Error) as exc:
            raise CoreStartupError(f"database could not be opened read-only: {exc}") from exc
        return cls(
            connection=connection,
            runtime_root=runtime_root,
            database_path=selected_database,
            identity_receipt=receipt,
            data_identity=identity,
        )

    @classmethod
    def open_formal(cls) -> "CoreReadOnlySession":
        """Keep the original formal-only entry point for callers and tests."""

        return cls.open(FORMAL_DATA_IDENTITY)

    def close(self) -> None:
        self.connection.close()

    def _table_exists(self, table: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _domain_catalog(self) -> dict[str, dict[str, Any]]:
        domains: dict[str, dict[str, Any]] = {}
        if not self._table_exists("stage0_cold_start_configuration"):
            return domains
        rows = self.connection.execute(
            "SELECT domain_label, domain_name, platform, configuration_id "
            "FROM stage0_cold_start_configuration "
            "WHERE data_identity=? AND status != 'cancelled' "
            "ORDER BY confirmed_at, configuration_id",
            (self.data_identity,),
        ).fetchall()
        for row in rows:
            label = str(row["domain_label"])
            domains[label] = {
                "domain_identity": label,
                "name": str(row["domain_name"]),
                "platform": str(row["platform"]),
                "configuration_id": str(row["configuration_id"]),
            }

        rows = self.connection.execute(
            "SELECT domain_label FROM stage0_content_account "
            "WHERE data_identity=? AND status='active' ORDER BY domain_label",
            (self.data_identity,),
        ).fetchall()
        for row in rows:
            label = str(row["domain_label"])
            domains.setdefault(
                label,
                {"domain_identity": label, "name": label, "platform": None, "configuration_id": None},
            )

        rows = self.connection.execute(
            "SELECT domain_label FROM stage0_domain_activation "
            "WHERE data_identity=? ORDER BY domain_label",
            (self.data_identity,),
        ).fetchall()
        for row in rows:
            label = str(row["domain_label"])
            domains.setdefault(
                label,
                {"domain_identity": label, "name": label, "platform": None, "configuration_id": None},
            )
        return domains

    def _current_activation(self, domain_label: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT activation_id, domain_label, cold_start_id, configuration_id, created_at "
            "FROM stage0_domain_activation "
            "WHERE domain_label=? AND data_identity=? AND is_current=1 "
            "ORDER BY created_at DESC, activation_id DESC LIMIT 1",
            (domain_label, self.data_identity),
        ).fetchone()

    def _waiting_for(self, cold_start_id: str) -> list[dict[str, str]]:
        waiting: list[dict[str, str]] = []
        checks = (
            (
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_review' "
                "ORDER BY registration_id",
                "competitor_registration_review",
                "registration_id",
            ),
            (
                "SELECT tag_library_id FROM stage0_cold_start_tag_library "
                "WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_review' "
                "ORDER BY tag_library_id",
                "tag_library_review",
                "tag_library_id",
            ),
            (
                "SELECT content_type_candidate_id FROM stage0_cold_start_content_type_candidate "
                "WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_decision' "
                "ORDER BY created_at DESC, content_type_candidate_id DESC",
                "content_type_decision",
                "content_type_candidate_id",
            ),
            (
                "SELECT boundary_candidate_id FROM stage0_cold_start_domain_boundary_candidate "
                "WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_decision' "
                "ORDER BY created_at DESC, boundary_candidate_id DESC",
                "domain_boundary_decision",
                "boundary_candidate_id",
            ),
        )
        for query, kind, identifier_column in checks:
            for row in self.connection.execute(query, (cold_start_id, self.data_identity)).fetchall():
                waiting.append({"kind": kind, "id": str(row[identifier_column])})
        return waiting

    def _current_daily(self, domain_label: str, cold_start_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT daily_run_id, domain_label, business_date, lifecycle, created_at, "
            "started_at, finished_at, cold_start_id FROM stage0_daily_run "
            "WHERE domain_label=? AND cold_start_id=? AND data_identity=? "
            "ORDER BY business_date DESC, COALESCE(finished_at, started_at, created_at) DESC, "
            "daily_run_id DESC LIMIT 1",
            (domain_label, cold_start_id, self.data_identity),
        ).fetchone()

    def _history(self, domain_label: str, current_cold_start_id: str | None, current_daily_id: str | None) -> dict[str, Any]:
        cold_rows = self.connection.execute(
            "SELECT cold_start_id, status, created_at, completed_at FROM stage0_cold_start "
            "WHERE domain_label=? AND data_identity=? ORDER BY created_at, cold_start_id",
            (domain_label, self.data_identity),
        ).fetchall()
        daily_rows = self.connection.execute(
            "SELECT daily_run_id, business_date, lifecycle, created_at, started_at, finished_at, cold_start_id "
            "FROM stage0_daily_run WHERE domain_label=? AND data_identity=? "
            "ORDER BY business_date, created_at, daily_run_id",
            (domain_label, self.data_identity),
        ).fetchall()
        historical_cold = [
            {
                "cold_start_id": str(row["cold_start_id"]),
                "status": str(row["status"]),
                "created_at": _text(row["created_at"]),
                "completed_at": _text(row["completed_at"]),
            }
            for row in cold_rows
            if str(row["cold_start_id"]) != str(current_cold_start_id or "")
        ]
        historical_daily = [
            {
                "daily_run_id": str(row["daily_run_id"]),
                "business_date": str(row["business_date"]),
                "lifecycle": str(row["lifecycle"]),
                "created_at": _text(row["created_at"]),
                "started_at": _text(row["started_at"]),
                "finished_at": _text(row["finished_at"]),
                "cold_start_id": _text(row["cold_start_id"]),
            }
            for row in daily_rows
            if str(row["daily_run_id"]) != str(current_daily_id or "")
        ]
        current_run_ids = {
            str(run["run_id"])
            for run in self._in_progress_business_runs(domain_label)
        }
        historical_unfinished = [
            {
                **run,
                "classification": "historical/non-current",
                "has_current_business_control": False,
                "will_resume": False,
                "will_continue": False,
                "blocks_new_cold_start": False,
            }
            for run in self._unfinished_business_runs(domain_label)
            if str(run["run_id"]) not in current_run_ids
        ]
        return {
            "cold_starts": {
                "total": len(historical_cold),
                "by_status": self._counts(historical_cold, "status"),
                "records": historical_cold,
            },
            "daily_runs": {
                "total": len(historical_daily),
                "by_lifecycle": self._counts(historical_daily, "lifecycle"),
                "records": historical_daily,
            },
            "unfinished_business_runs": {
                "total": len(historical_unfinished),
                "records": historical_unfinished,
            },
        }

    @staticmethod
    def _counts(records: list[dict[str, Any]], field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            value = str(record[field])
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    def _unfinished_business_runs(self, domain_label: str) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        discovery_rows = self.connection.execute(
            "SELECT run.run_id, run.status, x.execution_mode, x.lifecycle_status, x.daily_run_id, "
            "d.lifecycle AS daily_lifecycle, d.cold_start_id AS daily_cold_start_id "
            "FROM stage1b_discovery_run run "
            "JOIN stage1b_run_domain_scope scope ON scope.run_id=run.run_id "
            "AND scope.data_identity=run.data_identity "
            "LEFT JOIN stage1b_run_execution_context x ON x.run_id=run.run_id "
            "AND x.data_identity=run.data_identity "
            "LEFT JOIN stage0_daily_run d ON d.daily_run_id=x.daily_run_id "
            "AND d.data_identity=run.data_identity "
            "WHERE scope.domain_label=? AND run.data_identity=? AND run.status='processing' "
            "ORDER BY run.created_at, run.run_id",
            (domain_label, self.data_identity),
        ).fetchall()
        runs.extend(
            {
                "kind": "discovery",
                "run_id": str(row["run_id"]),
                "status": str(row["status"]),
                "execution_mode": _text(row["execution_mode"]),
                "lifecycle_status": _text(row["lifecycle_status"]),
                "daily_run_id": _text(row["daily_run_id"]),
                "daily_lifecycle": _text(row["daily_lifecycle"]),
                "daily_cold_start_id": _text(row["daily_cold_start_id"]),
            }
            for row in discovery_rows
        )
        candidate_rows = self.connection.execute(
            "SELECT experience_candidate_run_id, status FROM stage0_experience_candidate_run "
            "WHERE domain_label=? AND data_identity=? AND status='running' "
            "ORDER BY created_at, experience_candidate_run_id",
            (domain_label, self.data_identity),
        ).fetchall()
        runs.extend(
            {
                "kind": "experience_candidate",
                "run_id": str(row["experience_candidate_run_id"]),
                "status": str(row["status"]),
                "execution_mode": None,
                "lifecycle_status": None,
                "daily_run_id": None,
                "daily_lifecycle": None,
                "daily_cold_start_id": None,
            }
            for row in candidate_rows
        )
        return runs

    def _in_progress_business_runs(self, domain_label: str) -> list[dict[str, Any]]:
        """Return only unfinished runs that are bound to the current activation."""

        activation = self._current_activation(domain_label)
        if activation is None:
            return []
        current_cold_start_id = str(activation["cold_start_id"])
        current: list[dict[str, Any]] = []
        for run in self._unfinished_business_runs(domain_label):
            if run["kind"] == "experience_candidate":
                is_current = True
            else:
                is_current = (
                    run["daily_run_id"] is not None
                    and run["daily_cold_start_id"] == current_cold_start_id
                )
            if is_current:
                current.append(
                    {
                        **run,
                        "classification": "current",
                        "has_current_business_control": True,
                    }
                )
        return current

    def _domain_status(self, catalog: dict[str, Any]) -> dict[str, Any]:
        domain_label = str(catalog["domain_identity"])
        activation = self._current_activation(domain_label)
        activation_payload: dict[str, Any] = {
            "exists": activation is not None,
            "activation_id": str(activation["activation_id"]) if activation else None,
            "cold_start_id": str(activation["cold_start_id"]) if activation else None,
        }
        current_cold: dict[str, Any] = {
            "exists": False,
            "cold_start_id": None,
            "lifecycle": None,
            "status": "\u672acold-start",
        }
        waiting_for: list[dict[str, str]] = []
        current_daily_payload: dict[str, Any] = {
            "exists": False,
            "daily_run_id": None,
            "business_date": None,
            "lifecycle": None,
        }
        current_cold_start_id: str | None = None
        current_daily_id: str | None = None
        if activation is not None:
            current_cold_start_id = str(activation["cold_start_id"])
            cold = self.connection.execute(
                "SELECT cold_start_id, status, created_at, completed_at FROM stage0_cold_start "
                "WHERE cold_start_id=? AND data_identity=?",
                (current_cold_start_id, self.data_identity),
            ).fetchone()
            if cold is not None:
                status = str(cold["status"])
                current_cold = {
                    "exists": True,
                    "cold_start_id": current_cold_start_id,
                    "lifecycle": status,
                    "status": status,
                }
                if status == "waiting_human":
                    waiting_for = self._waiting_for(current_cold_start_id)
                    if not waiting_for:
                        waiting_for = [{"kind": "cold_start_human_confirmation", "id": current_cold_start_id}]
            daily = self._current_daily(domain_label, current_cold_start_id)
            if daily is not None:
                current_daily_id = str(daily["daily_run_id"])
                current_daily_payload = {
                    "exists": True,
                    "daily_run_id": current_daily_id,
                    "business_date": str(daily["business_date"]),
                    "lifecycle": str(daily["lifecycle"]),
                }

        waiting = bool(current_cold["exists"] and current_cold["status"] == "waiting_human")
        history = self._history(domain_label, current_cold_start_id, current_daily_id)
        return {
            **catalog,
            "current_activation": activation_payload,
            "current_cold_start": current_cold,
            "waiting_human": waiting,
            "waiting_for": waiting_for,
            "current_daily": current_daily_payload,
            "in_progress_business_runs": self._in_progress_business_runs(domain_label),
            "can_start_new_cold_start": activation is None,
            "history": history,
        }

    def _system_status(self, schedule: dict[str, Any], executor: dict[str, Any]) -> dict[str, Any]:
        schedule_entry = ((schedule.get("schedules") or {}).get("daily") or {})
        default_executor = str(executor.get("default_executor") or "")
        executors = executor.get("executors") or {}
        executor_entry = executors.get(default_executor) if isinstance(executors, dict) else None
        executor_entry = executor_entry if isinstance(executor_entry, dict) else {}
        launch = executor_entry.get("launch") or {}
        command = str(launch.get("command") or "")
        args = launch.get("args")
        launch_valid = bool(default_executor and isinstance(executor_entry, dict) and command and isinstance(args, list))
        return {
            "runtime_identity": _identity_name(self.data_identity),
            "data_identity": self.data_identity,
            "migration_protection": bool(schedule.get("migration_protection")),
            "project_environment": {
                "status": "normal",
                "database_readable": True,
                "configuration_readable": True,
                "model_called": False,
                "executor_started": False,
            },
            "database": {
                "readable": True,
                "access_mode": "read_only",
                "path": str(self.database_path),
                "identity_verified": True,
            },
            "daily_schedule": {
                "enabled": bool(schedule_entry.get("enabled")),
                "time": str(schedule_entry.get("time") or ""),
                "timezone": str(schedule_entry.get("timezone") or ""),
            },
            "default_executor": default_executor,
            "executor": {
                "default": default_executor,
                "configured": bool(executor_entry),
                "launch_configuration_valid": launch_valid,
                "command": command,
                "command_available": bool(shutil.which(command)) if command else False,
                "start_probe": "not_run",
                "started": False,
            },
        }

    def unified_status(self) -> dict[str, Any]:
        """Return the one authoritative current/history status without any writes."""

        schedule, executor = _configuration()
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
                self.connection, "stage1b_discovery_run", "status", "processing"
            ),
            "content_node_processing": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage0_content_node_version WHERE status = 'processing'"
                ).fetchone()["count"]
            ),
            "experience_candidate_run_running": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage0_experience_candidate_run WHERE status = 'running'"
                ).fetchone()["count"]
            ),
            "candidate_awaiting_user_decision": int(
                self.connection.execute(
                    "SELECT COUNT(*) AS count FROM stage1b_candidate_version WHERE status = 'awaiting_user_decision'"
                ).fetchone()["count"]
            ),
        }
        migration_digest = str(self.identity_receipt.get("formal_database_sha256") or "")
        domain_statuses = [
            self._domain_status(catalog)
            for catalog in self._domain_catalog().values()
        ]
        domain_statuses.sort(key=lambda item: str(item["domain_identity"]))
        in_progress = [
            run
            for domain in domain_statuses
            for run in domain["in_progress_business_runs"]
        ]
        historical_unfinished = [
            record
            for domain in domain_statuses
            for record in domain["history"]["unfinished_business_runs"]["records"]
        ]
        status: dict[str, Any] = {
            "status_schema_version": STATUS_SCHEMA_VERSION,
            "project": PROJECT_NAME,
            "core_entry": "scripts.core.core_entry",
            "business_state_owner": "Creation Assistant Core",
            "runtime_identity": _identity_name(self.data_identity),
            "data_identity": self.data_identity,
            "runtime_root": str(self.runtime_root),
            "formal_database": str(self.database_path) if self.data_identity == "production" else None,
            "database_identity": self.data_identity,
            "database_open": True,
            "access_mode": "read_only",
            "runtime_identity_verified": True,
            "database_path_matches_runtime_identity": True,
            "historical_migration_digest": migration_digest,
            "historical_migration_digest_recorded": bool(migration_digest),
            "historical_migration_digest_note": (
                "The migration digest is a historical copy-verification baseline, "
                "not a live database digest and not a live write lock."
            ),
            "system": self._system_status(schedule, executor),
            "schema": {"table_count": table_count},
            "domains": domain_statuses,
            "history": {
                "current_isolated_from_history": True,
                "domains_with_history": sum(bool(domain["history"]["cold_starts"]["total"] or domain["history"]["daily_runs"]["total"]) for domain in domain_statuses),
                "unfinished_business_runs_total": len(historical_unfinished),
                "unfinished_business_runs": historical_unfinished,
            },
            "business_runs": {
                "daily_runs": {"total": sum(daily_runs.values()), "by_lifecycle": daily_runs},
                "cold_starts": {"total": sum(cold_starts.values()), "by_status": cold_starts},
                "discovery_runs": {"total": sum(discovery_runs.values()), "by_status": discovery_runs},
                "content_tasks": {"total": sum(content_tasks.values()), "by_status": content_tasks},
                "in_progress": in_progress,
            },
            "unfinished_business_detected": any(value > 0 for value in unfinished.values()),
            "unfinished_status_counts": unfinished,
            "automatic_business_execution": False,
            "model_required_for_startup": False,
            "external_agent_required_for_model_tasks": True,
            "hermes_started": False,
            "hermes_profile_read": False,
            "hermes_plugin_read": False,
            "model_called": False,
            "core_capabilities_without_external_agent": [
                "resolve_runtime_identity",
                "verify_runtime_identity",
                "open_database_read_only",
                "read_unified_current_and_history_status",
            ],
            "later_external_agent_capabilities": [
                "model_backed_skill_execution",
                "model_result_submission",
            ],
        }
        status["human_summary"] = self._human_summary(status)
        return status

    def _human_summary(self, status: dict[str, Any]) -> str:
        system = status["system"]
        lines = [
            f"{PROJECT_NAME} \u5f53\u524d\u8fd0\u884c\u8eab\u4efd\uff1a{system['runtime_identity']}\u3002",
            f"\u8fc1\u79fb\u4fdd\u62a4\uff1a{'\u5f00\u542f' if system['migration_protection'] else '\u672a\u5f00\u542f'}\uff1b\u6570\u636e\u5e93\uff1a\u53ef\u8bfb\u3001\u53ea\u8bfb\u3002",
            f"daily\u8ba1\u5212\uff1a{'\u5f00\u542f' if system['daily_schedule']['enabled'] else '\u5173\u95ed'}\uff0c\u65f6\u95f4 {system['daily_schedule']['time']}\u3002",
        ]
        for domain in status["domains"]:
            cold = domain["current_cold_start"]["status"]
            daily = domain["current_daily"]["lifecycle"] or "无"
            waiting = "\uff0c\u7b49\u5f85\u4eba\u5de5\u786e\u8ba4" if domain["waiting_human"] else ""
            lines.append(f"\u9886\u57df {domain['domain_identity']}\uff1a\u5f53\u524dcold-start={cold}\uff0c\u5f53\u524ddaily={daily}{waiting}\u3002")
        historical_unfinished = int(status["history"]["unfinished_business_runs_total"])
        if historical_unfinished:
            lines.append(
                f"\u53e6\u6709 {historical_unfinished} \u6761 historical/non-current \u672a\u5b8c\u6210\u8fd0\u884c\uff0c"
                f"\u4e0d\u5177\u6709\u5f53\u524d\u4e1a\u52a1\u63a7\u5236\u6743\u3002"
            )
        return "\n".join(lines)

    def basic_status(self) -> dict[str, Any]:
        """Backward-compatible name for the unified status result."""

        return self.unified_status()


def build_status(
    *,
    data_identity: str = FORMAL_DATA_IDENTITY,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    session = (
        CoreReadOnlySession.open_formal()
        if database_path is None and data_identity == FORMAL_DATA_IDENTITY
        else CoreReadOnlySession.open(data_identity=data_identity, database_path=database_path)
    )
    try:
        return session.unified_status()
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Creation Assistant Core status")
    parser.add_argument("--data-identity", choices=("test", "production"), default="production")
    parser.add_argument("--database-path", type=Path)
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                build_status(data_identity=args.data_identity, database_path=args.database_path),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
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
