from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.core.state.goal02_core import STATE_TABLES
from scripts.validation.clean_room_empty_db import table_counts

GROUPED_STATUS_TABLES: dict[str, str] = {
    "scheduler_job": "status",
    "outbox_message": "status",
    "command_receipt": "status",
    "runtime_probe_skill_run": "status",
}


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _grouped_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = conn.execute(
        f'SELECT "{column}" AS bucket, COUNT(*) AS n FROM "{table}" GROUP BY "{column}"'
    ).fetchall()
    return {str(row["bucket"]): int(row["n"]) for row in rows}


def summary(conn: sqlite3.Connection) -> dict:
    table_rows = table_counts(conn)
    by_status = {
        table: _grouped_counts(conn, table, column)
        for table, column in GROUPED_STATUS_TABLES.items()
    }
    by_state = {
        object_kind: _grouped_counts(conn, table_name, "state")
        for object_kind, (table_name, _id_column) in STATE_TABLES.items()
    }
    return {
        "table_rows": table_rows,
        "scheduler_job_by_status": by_status["scheduler_job"],
        "outbox_message_by_status": by_status["outbox_message"],
        "command_receipt_by_status": by_status["command_receipt"],
        "skill_run_by_status": by_status["runtime_probe_skill_run"],
        "state_by_object_kind": by_state,
    }


def list_scheduler_jobs(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100
) -> list[dict]:
    if status:
        rows = conn.execute(
            """
            SELECT job_id, job_kind, status, priority, run_after, attempt_count,
                   max_attempts, lease_owner, lease_expires_at, correlation_id,
                   created_at, updated_at
              FROM scheduler_job
             WHERE status = ?
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT job_id, job_kind, status, priority, run_after, attempt_count,
                   max_attempts, lease_owner, lease_expires_at, correlation_id,
                   created_at, updated_at
              FROM scheduler_job
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_job_attempts(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT attempt_id, job_id, attempt_no, worker_id, status, started_at,
               heartbeat_at, lease_expires_at, finished_at, error_json,
               correlation_id, created_at, updated_at
          FROM scheduler_job_attempt
         WHERE job_id = ?
         ORDER BY attempt_no DESC
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_state_rows(
    conn: sqlite3.Connection,
    object_kind: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if object_kind not in STATE_TABLES:
        raise ValueError(f"unknown object_kind: {object_kind}")
    table_name, id_column = STATE_TABLES[object_kind]
    if status:
        rows = conn.execute(
            f"""
            SELECT "{id_column}" AS object_id, state, state_rank, basis_version_id,
                   row_revision, updated_by_command_id, created_at, updated_at
              FROM "{table_name}"
             WHERE state = ?
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT "{id_column}" AS object_id, state, state_rank, basis_version_id,
                   row_revision, updated_by_command_id, created_at, updated_at
              FROM "{table_name}"
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_audit_events(
    conn: sqlite3.Connection, *, limit: int = 100, object_kind: str | None = None
) -> list[dict]:
    if object_kind:
        rows = conn.execute(
            """
            SELECT audit_id, event_type, actor, object_kind, object_id, version_id,
                   correlation_id, causation_id, created_at
              FROM audit_event
             WHERE object_kind = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (object_kind, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT audit_id, event_type, actor, object_kind, object_id, version_id,
                   correlation_id, causation_id, created_at
              FROM audit_event
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_outbox_messages(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100
) -> list[dict]:
    if status:
        rows = conn.execute(
            """
            SELECT outbox_id, topic, status, attempts, correlation_id,
                   causation_id, created_at, updated_at
              FROM outbox_message
             WHERE status = ?
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT outbox_id, topic, status, attempts, correlation_id,
                   causation_id, created_at, updated_at
              FROM outbox_message
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_command_receipts(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100
) -> list[dict]:
    if status:
        rows = conn.execute(
            """
            SELECT receipt_id, command_scope, idempotency_key, status,
                   correlation_id, causation_id, created_at
              FROM command_receipt
             WHERE status = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT receipt_id, command_scope, idempotency_key, status,
                   correlation_id, causation_id, created_at
              FROM command_receipt
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_skill_runs(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100
) -> list[dict]:
    if status:
        rows = conn.execute(
            """
            SELECT skill_run_id, job_id, attempt_id, status, skill_name,
                   skill_version, binding_name, binding_version, model_route,
                   model_port, error_json, created_at
              FROM runtime_probe_skill_run
             WHERE status = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT skill_run_id, job_id, attempt_id, status, skill_name,
                   skill_version, binding_name, binding_version, model_route,
                   model_port, error_json, created_at
              FROM runtime_probe_skill_run
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
