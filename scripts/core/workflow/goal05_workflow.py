from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from scripts.core.persistence.goal01_store import content_hash
from scripts.core.scheduler.goal03_scheduler import EnqueueResult, Goal03Scheduler


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowStepSpec:
    step_key: str
    job_kind: str
    payload: dict[str, Any]
    priority: int = 0
    run_after_ms: int | None = None
    max_attempts: int = 3


@dataclass(frozen=True)
class WorkflowStartResult:
    workflow_id: str
    workflow_name: str
    job_ids: tuple[str, ...]
    replayed: bool


class Goal05WorkflowOrchestrator:
    def __init__(self, scheduler: Goal03Scheduler):
        self.scheduler = scheduler

    def start_workflow(
        self,
        *,
        workflow_name: str,
        steps: Iterable[WorkflowStepSpec],
        idempotency_key: str,
    ) -> WorkflowStartResult:
        step_list = tuple(steps)
        self._validate_workflow(workflow_name=workflow_name, steps=step_list, idempotency_key=idempotency_key)
        workflow_id = content_hash(
            {
                "goal": "GOAL-05",
                "workflow_name": workflow_name,
                "idempotency_key": idempotency_key,
                "step_keys": [step.step_key for step in step_list],
            }
        )
        enqueued: list[EnqueueResult] = []
        for index, step in enumerate(step_list):
            payload = dict(step.payload)
            payload["_workflow"] = {
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "step_key": step.step_key,
                "step_index": index,
                "step_count": len(step_list),
            }
            enqueued.append(
                self.scheduler.enqueue_job(
                    job_kind=step.job_kind,
                    payload=payload,
                    idempotency_key=f"{idempotency_key}:{step.step_key}",
                    priority=step.priority,
                    run_after_ms=step.run_after_ms,
                    max_attempts=step.max_attempts,
                    correlation_id=workflow_id,
                )
            )
        return WorkflowStartResult(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            job_ids=tuple(result.job_id for result in enqueued),
            replayed=all(result.replayed for result in enqueued),
        )

    @staticmethod
    def _validate_workflow(
        *,
        workflow_name: str,
        steps: tuple[WorkflowStepSpec, ...],
        idempotency_key: str,
    ) -> None:
        if not workflow_name:
            raise WorkflowError("workflow_name is required")
        if not idempotency_key:
            raise WorkflowError("idempotency_key is required")
        if not steps:
            raise WorkflowError("workflow must contain at least one step")
        seen: set[str] = set()
        for step in steps:
            if not step.step_key:
                raise WorkflowError("workflow step_key is required")
            if step.step_key in seen:
                raise WorkflowError("workflow step_key must be unique")
            seen.add(step.step_key)
            if not step.job_kind:
                raise WorkflowError("workflow step job_kind is required")
            if step.max_attempts < 1:
                raise WorkflowError("workflow step max_attempts must be positive")
