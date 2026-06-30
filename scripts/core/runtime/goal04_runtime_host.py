from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob


RuntimeHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


class RuntimeHostError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeStepResult:
    status: str
    job_id: str | None = None
    attempt_id: str | None = None
    job_kind: str | None = None
    reason: str | None = None


class RuntimeHost:
    def __init__(self, scheduler: Goal03Scheduler, *, worker_id: str):
        self.scheduler = scheduler
        self.worker_id = worker_id
        self._handlers: dict[str, RuntimeHandler] = {}

    def register_handler(self, job_kind: str, handler: RuntimeHandler) -> None:
        if not job_kind:
            raise RuntimeHostError("job_kind is required")
        self._handlers[job_kind] = handler

    def run_once(self, *, lease_seconds: int = 60, retry_unknown: bool = False) -> RuntimeStepResult:
        try:
            claim = self.scheduler.claim_next(worker_id=self.worker_id, lease_seconds=lease_seconds)
        except NoClaimableJob:
            return RuntimeStepResult(status="idle")

        job = self.scheduler.get_job(claim.job_id)
        job_kind = str(job["job_kind"])
        handler = self._handlers.get(job_kind)
        if handler is None:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "unknown_job_kind", "job_kind": job_kind},
                retry=retry_unknown,
            )
            return RuntimeStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                job_kind=job_kind,
                reason="unknown_job_kind",
            )

        try:
            result = handler(claim.payload) or {}
        except Exception as exc:  # noqa: BLE001 - handler boundary records error details.
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "handler_error", "message": str(exc), "job_kind": job_kind},
                retry=True,
            )
            return RuntimeStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                job_kind=job_kind,
                reason="handler_error",
            )

        self.scheduler.complete(
            attempt_id=claim.attempt_id,
            worker_id=self.worker_id,
            result=result,
        )
        return RuntimeStepResult(
            status="succeeded",
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            job_kind=job_kind,
        )
