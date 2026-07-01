from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob


RuntimeHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


class RuntimeHostError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeHandlerContract:
    job_kind: str
    required_payload_keys: tuple[str, ...] = ()
    required_result_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeAdapter:
    adapter_name: str
    job_kind: str
    handler: RuntimeHandler
    contract: RuntimeHandlerContract
    uses_external_io: bool = False


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
        self._handlers: dict[str, tuple[RuntimeHandler, RuntimeHandlerContract]] = {}

    def register_handler(
        self,
        job_kind: str,
        handler: RuntimeHandler,
        *,
        contract: RuntimeHandlerContract | None = None,
    ) -> None:
        if not job_kind:
            raise RuntimeHostError("job_kind is required")
        effective_contract = contract or RuntimeHandlerContract(job_kind=job_kind)
        if effective_contract.job_kind != job_kind:
            raise RuntimeHostError("handler contract job_kind mismatch")
        self._handlers[job_kind] = (handler, effective_contract)

    def register_adapter(self, adapter: RuntimeAdapter) -> None:
        if not adapter.adapter_name:
            raise RuntimeHostError("adapter_name is required")
        if adapter.uses_external_io:
            raise RuntimeHostError("external adapter I/O is outside GOAL-04 current checkpoint")
        self.register_handler(
            adapter.job_kind,
            adapter.handler,
            contract=adapter.contract,
        )

    def run_once(self, *, lease_seconds: int = 60, retry_unknown: bool = False) -> RuntimeStepResult:
        try:
            claim = self.scheduler.claim_next(worker_id=self.worker_id, lease_seconds=lease_seconds)
        except NoClaimableJob:
            return RuntimeStepResult(status="idle")

        job = self.scheduler.get_job(claim.job_id)
        job_kind = str(job["job_kind"])
        registered = self._handlers.get(job_kind)
        if registered is None:
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
        handler, contract = registered
        missing_payload_keys = self._missing_keys(claim.payload, contract.required_payload_keys)
        if missing_payload_keys:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={
                    "code": "contract_input_error",
                    "job_kind": job_kind,
                    "missing_keys": missing_payload_keys,
                },
                retry=False,
            )
            return RuntimeStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                job_kind=job_kind,
                reason="contract_input_error",
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
        missing_result_keys = self._missing_keys(result, contract.required_result_keys)
        if missing_result_keys:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={
                    "code": "contract_output_error",
                    "job_kind": job_kind,
                    "missing_keys": missing_result_keys,
                },
                retry=True,
            )
            return RuntimeStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                job_kind=job_kind,
                reason="contract_output_error",
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

    def run_batch(
        self,
        *,
        max_jobs: int,
        lease_seconds: int = 60,
        retry_unknown: bool = False,
    ) -> list[RuntimeStepResult]:
        if max_jobs < 1:
            raise RuntimeHostError("max_jobs must be positive")
        results: list[RuntimeStepResult] = []
        for _ in range(max_jobs):
            result = self.run_once(lease_seconds=lease_seconds, retry_unknown=retry_unknown)
            if result.status == "idle":
                break
            results.append(result)
        return results

    @staticmethod
    def _missing_keys(payload: dict[str, Any], required_keys: tuple[str, ...]) -> list[str]:
        return [key for key in required_keys if key not in payload]
