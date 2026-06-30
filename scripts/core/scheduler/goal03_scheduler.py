from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from scripts.core.persistence.goal01_store import (
    IdempotencyConflict,
    PersistenceStore,
    canonical_json,
    content_hash,
    uuid7,
)


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "persistence" / "goal03_schema.sqlite.sql"

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "dead"}
TERMINAL_ATTEMPT_STATUSES = {"succeeded", "failed", "cancelled"}


class SchedulerError(RuntimeError):
    pass


class NoClaimableJob(SchedulerError):
    pass


class InvalidWorkerLease(SchedulerError):
    pass


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    receipt_id: str
    status: str
    replayed: bool = False


@dataclass(frozen=True)
class ClaimResult:
    job_id: str
    attempt_id: str
    attempt_no: int
    lease_expires_at: str
    payload: dict[str, Any]


class Goal03Scheduler:
    def __init__(
        self,
        store: PersistenceStore,
        *,
        id_factory: Callable[[], str] = uuid7,
        now_ms: Callable[[], int] | None = None,
    ):
        self.store = store
        self.conn = store.conn
        self.id_factory = id_factory
        self.now_ms = now_ms or (lambda: 0)
        self.install_schema()

    @classmethod
    def in_memory(
        cls,
        *,
        id_factory: Callable[[], str] = uuid7,
        now_ms: Callable[[], int] | None = None,
    ) -> "Goal03Scheduler":
        store = PersistenceStore.in_memory(id_factory=id_factory)
        return cls(store, id_factory=id_factory, now_ms=now_ms)

    def install_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def enqueue_job(
        self,
        *,
        job_kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        priority: int = 0,
        run_after_ms: int | None = None,
        max_attempts: int = 3,
        source_outbox_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        inject_fault_after_job: bool = False,
    ) -> EnqueueResult:
        request_payload = {
            "job_kind": job_kind,
            "payload": payload,
            "priority": priority,
            "run_after_ms": run_after_ms,
            "max_attempts": max_attempts,
            "source_outbox_id": source_outbox_id,
        }
        existing = self._existing_enqueue(idempotency_key, request_payload)
        if existing is not None:
            return existing
        if max_attempts < 1:
            raise SchedulerError("max_attempts must be positive")
        with self.conn:
            job_id = self.id_factory()
            corr = correlation_id or job_id
            receipt_id = self.store.record_command(
                command_scope="goal03.scheduler.enqueue",
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                result_payload={"job_id": job_id, "status": "queued"},
                correlation_id=corr,
                causation_id=causation_id,
                status="succeeded",
            )
            self.conn.execute(
                """
                INSERT INTO scheduler_job(
                    job_id, job_kind, status, priority, run_after, max_attempts,
                    payload_json, source_outbox_id, correlation_id, causation_id,
                    created_by_receipt_id
                )
                VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_kind,
                    priority,
                    self._format_ms(run_after_ms if run_after_ms is not None else self.now_ms()),
                    max_attempts,
                    canonical_json(payload),
                    source_outbox_id,
                    corr,
                    causation_id,
                    receipt_id,
                ),
            )
            if inject_fault_after_job:
                raise RuntimeError(f"injected scheduler fault after job {job_id}")
            self._audit(
                event_type="scheduler_job_queued",
                actor="scheduler",
                job_id=job_id,
                payload={"job_kind": job_kind, "source_outbox_id": source_outbox_id},
                correlation_id=corr,
                causation_id=causation_id,
            )
        return EnqueueResult(job_id=job_id, receipt_id=receipt_id, status="queued")

    def enqueue_from_outbox(
        self,
        outbox_id: str,
        *,
        idempotency_key: str,
        priority: int = 0,
        run_after_ms: int | None = None,
        max_attempts: int = 3,
    ) -> EnqueueResult:
        row = self.conn.execute(
            """
            SELECT outbox_id, topic, payload_json, correlation_id, causation_id, status
              FROM outbox_message
             WHERE outbox_id=?
            """,
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise SchedulerError(f"missing outbox message: {outbox_id}")
        if row["status"] != "pending":
            raise SchedulerError(f"outbox message is not pending: {outbox_id}")
        return self.enqueue_job(
            job_kind="outbox.dispatch",
            payload={
                "outbox_id": row["outbox_id"],
                "topic": row["topic"],
                "payload": json.loads(row["payload_json"]),
            },
            idempotency_key=idempotency_key,
            priority=priority,
            run_after_ms=run_after_ms,
            max_attempts=max_attempts,
            source_outbox_id=row["outbox_id"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
        )

    def claim_next(self, *, worker_id: str, lease_seconds: int = 60) -> ClaimResult:
        now = self._now_text()
        lease_expires_at = self._format_ms(self.now_ms() + lease_seconds * 1000)
        with self.conn:
            row = self.conn.execute(
                """
                SELECT *
                  FROM scheduler_job
                 WHERE status='queued'
                   AND run_after <= ?
                   AND attempt_count < max_attempts
                 ORDER BY priority DESC, run_after ASC, created_at ASC
                 LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                raise NoClaimableJob("no claimable scheduler job")
            attempt_id = self.id_factory()
            attempt_no = int(row["attempt_count"]) + 1
            self.conn.execute(
                """
                UPDATE scheduler_job
                   SET status='leased',
                       attempt_count=?,
                       current_attempt_id=?,
                       lease_owner=?,
                       lease_expires_at=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE job_id=? AND status='queued'
                """,
                (attempt_no, attempt_id, worker_id, lease_expires_at, row["job_id"]),
            )
            self.conn.execute(
                """
                INSERT INTO scheduler_job_attempt(
                    attempt_id, job_id, attempt_no, worker_id, status, started_at,
                    heartbeat_at, lease_expires_at, correlation_id, causation_id
                )
                VALUES(?, ?, ?, ?, 'leased', ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    row["job_id"],
                    attempt_no,
                    worker_id,
                    now,
                    now,
                    lease_expires_at,
                    row["correlation_id"],
                    row["causation_id"],
                ),
            )
            self._audit(
                event_type="scheduler_job_leased",
                actor=worker_id,
                job_id=row["job_id"],
                payload={"attempt_id": attempt_id, "attempt_no": attempt_no},
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
            )
        return ClaimResult(
            job_id=row["job_id"],
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            lease_expires_at=lease_expires_at,
            payload=json.loads(row["payload_json"]),
        )

    def heartbeat(self, *, attempt_id: str, worker_id: str, lease_seconds: int = 60) -> str:
        now = self._now_text()
        lease_expires_at = self._format_ms(self.now_ms() + lease_seconds * 1000)
        with self.conn:
            attempt = self._leased_attempt(attempt_id, worker_id)
            self.conn.execute(
                """
                UPDATE scheduler_job_attempt
                   SET heartbeat_at=?,
                       lease_expires_at=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE attempt_id=?
                """,
                (now, lease_expires_at, attempt_id),
            )
            self.conn.execute(
                """
                UPDATE scheduler_job
                   SET lease_expires_at=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE job_id=? AND current_attempt_id=? AND status='leased'
                """,
                (lease_expires_at, attempt["job_id"], attempt_id),
            )
            self._audit(
                event_type="scheduler_job_heartbeat",
                actor=worker_id,
                job_id=attempt["job_id"],
                payload={"attempt_id": attempt_id, "lease_expires_at": lease_expires_at},
                correlation_id=attempt["correlation_id"],
                causation_id=attempt["causation_id"],
            )
        return lease_expires_at

    def complete(self, *, attempt_id: str, worker_id: str, result: dict[str, Any] | None = None) -> None:
        now = self._now_text()
        with self.conn:
            attempt = self._leased_attempt(attempt_id, worker_id)
            job = self._job(attempt["job_id"])
            self.conn.execute(
                """
                UPDATE scheduler_job_attempt
                   SET status='succeeded',
                       finished_at=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE attempt_id=?
                """,
                (now, attempt_id),
            )
            self.conn.execute(
                """
                UPDATE scheduler_job
                   SET status='succeeded',
                       lease_owner=NULL,
                       lease_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE job_id=? AND current_attempt_id=? AND status='leased'
                """,
                (attempt["job_id"], attempt_id),
            )
            if job["source_outbox_id"]:
                self.conn.execute(
                    """
                    UPDATE outbox_message
                       SET status='sent',
                           attempts=attempts+1,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE outbox_id=? AND status='pending'
                    """,
                    (job["source_outbox_id"],),
                )
            self._audit(
                event_type="scheduler_job_succeeded",
                actor=worker_id,
                job_id=attempt["job_id"],
                payload={"attempt_id": attempt_id, "result": result or {}},
                correlation_id=attempt["correlation_id"],
                causation_id=attempt["causation_id"],
            )

    def fail(
        self,
        *,
        attempt_id: str,
        worker_id: str,
        error: dict[str, Any],
        retry: bool = True,
        backoff_seconds: int = 0,
    ) -> str:
        now_ms = self.now_ms()
        now = self._format_ms(now_ms)
        with self.conn:
            attempt = self._leased_attempt(attempt_id, worker_id)
            job = self._job(attempt["job_id"])
            can_retry = retry and int(job["attempt_count"]) < int(job["max_attempts"])
            next_status = "queued" if can_retry else "dead"
            run_after = self._format_ms(now_ms + backoff_seconds * 1000)
            self.conn.execute(
                """
                UPDATE scheduler_job_attempt
                   SET status='failed',
                       finished_at=?,
                       error_json=?,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE attempt_id=?
                """,
                (now, canonical_json(error), attempt_id),
            )
            self.conn.execute(
                """
                UPDATE scheduler_job
                   SET status=?,
                       run_after=?,
                       lease_owner=NULL,
                       lease_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE job_id=? AND current_attempt_id=? AND status='leased'
                """,
                (next_status, run_after, attempt["job_id"], attempt_id),
            )
            if next_status == "dead" and job["source_outbox_id"]:
                self.conn.execute(
                    """
                    UPDATE outbox_message
                       SET status='failed',
                           attempts=attempts+1,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE outbox_id=? AND status='pending'
                    """,
                    (job["source_outbox_id"],),
                )
            self._audit(
                event_type="scheduler_job_failed" if next_status == "dead" else "scheduler_job_retry_scheduled",
                actor=worker_id,
                job_id=attempt["job_id"],
                payload={"attempt_id": attempt_id, "error": error, "next_status": next_status},
                correlation_id=attempt["correlation_id"],
                causation_id=attempt["causation_id"],
            )
        return next_status

    def cancel_job(self, *, job_id: str, actor: str, reason: str) -> None:
        with self.conn:
            job = self._job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                return
            if job["current_attempt_id"]:
                self.conn.execute(
                    """
                    UPDATE scheduler_job_attempt
                       SET status='cancelled',
                           finished_at=?,
                           error_json=?,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE attempt_id=? AND status='leased'
                    """,
                    (self._now_text(), canonical_json({"reason": reason}), job["current_attempt_id"]),
                )
            self.conn.execute(
                """
                UPDATE scheduler_job
                   SET status='cancelled',
                       lease_owner=NULL,
                       lease_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE job_id=? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'dead')
                """,
                (job_id,),
            )
            if job["source_outbox_id"]:
                self.conn.execute(
                    """
                    UPDATE outbox_message
                       SET status='cancelled',
                           updated_at=CURRENT_TIMESTAMP
                     WHERE outbox_id=? AND status='pending'
                    """,
                    (job["source_outbox_id"],),
                )
            self._audit(
                event_type="scheduler_job_cancelled",
                actor=actor,
                job_id=job_id,
                payload={"reason": reason},
                correlation_id=job["correlation_id"],
                causation_id=job["causation_id"],
            )

    def recover_expired_leases(self, *, actor: str = "scheduler.recovery") -> int:
        now = self._now_text()
        rows = self.conn.execute(
            """
            SELECT *
              FROM scheduler_job
             WHERE status='leased'
               AND lease_expires_at <= ?
             ORDER BY lease_expires_at ASC
            """,
            (now,),
        ).fetchall()
        recovered = 0
        with self.conn:
            for job in rows:
                next_status = "queued" if int(job["attempt_count"]) < int(job["max_attempts"]) else "dead"
                if job["current_attempt_id"]:
                    self.conn.execute(
                        """
                        UPDATE scheduler_job_attempt
                           SET status='failed',
                               finished_at=?,
                               error_json=?,
                               updated_at=CURRENT_TIMESTAMP
                         WHERE attempt_id=? AND status='leased'
                        """,
                        (
                            now,
                            canonical_json({"reason": "lease_expired"}),
                            job["current_attempt_id"],
                        ),
                    )
                self.conn.execute(
                    """
                    UPDATE scheduler_job
                       SET status=?,
                           lease_owner=NULL,
                           lease_expires_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE job_id=? AND status='leased'
                    """,
                    (next_status, job["job_id"]),
                )
                if next_status == "dead" and job["source_outbox_id"]:
                    self.conn.execute(
                        """
                        UPDATE outbox_message
                           SET status='failed',
                               attempts=attempts+1,
                               updated_at=CURRENT_TIMESTAMP
                         WHERE outbox_id=? AND status='pending'
                        """,
                        (job["source_outbox_id"],),
                    )
                self._audit(
                    event_type="scheduler_job_lease_recovered",
                    actor=actor,
                    job_id=job["job_id"],
                    payload={"next_status": next_status, "attempt_id": job["current_attempt_id"]},
                    correlation_id=job["correlation_id"],
                    causation_id=job["causation_id"],
                )
                recovered += 1
        return recovered

    def get_job(self, job_id: str) -> sqlite3.Row:
        return self._job(job_id)

    def _existing_enqueue(self, idempotency_key: str, request_payload: dict[str, Any]) -> EnqueueResult | None:
        request_hash = content_hash(request_payload)
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json, status
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal03.scheduler.enqueue", idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key reused with different request")
        result = json.loads(row["result_json"])
        return EnqueueResult(
            job_id=result["job_id"],
            receipt_id=row["receipt_id"],
            status=result["status"],
            replayed=True,
        )

    def _leased_attempt(self, attempt_id: str, worker_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            """
            SELECT *
              FROM scheduler_job_attempt
             WHERE attempt_id=? AND worker_id=? AND status='leased'
            """,
            (attempt_id, worker_id),
        ).fetchone()
        if row is None:
            raise InvalidWorkerLease(f"invalid active lease for attempt {attempt_id}")
        return row

    def _job(self, job_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM scheduler_job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise SchedulerError(f"missing scheduler job: {job_id}")
        return row

    def _audit(
        self,
        *,
        event_type: str,
        actor: str,
        job_id: str,
        payload: dict[str, Any],
        correlation_id: str,
        causation_id: str | None,
    ) -> str:
        return self.store.record_audit(
            event_type=event_type,
            actor=actor,
            object_kind="scheduler_job",
            object_id=job_id,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    def _now_text(self) -> str:
        return self._format_ms(self.now_ms())

    @staticmethod
    def _format_ms(ms: int) -> str:
        seconds = ms // 1000
        millis = ms % 1000
        return f"{seconds:012d}.{millis:03d}"
