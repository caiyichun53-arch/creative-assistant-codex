from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import UUIDv7Generator
from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator, WorkflowError, WorkflowStepSpec


class FakeClock:
    def __init__(self, start_ms: int):
        self.current_ms = start_ms

    def now_ms(self) -> int:
        return self.current_ms


class DeterministicBits:
    def __init__(self):
        self.value = 0

    def __call__(self, bit_count: int) -> int:
        self.value += 1
        return self.value & ((1 << bit_count) - 1)


def make_workflow() -> tuple[Goal03Scheduler, RuntimeHost, Goal05WorkflowOrchestrator]:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=clock.now_ms)
    host = RuntimeHost(scheduler, worker_id="workflow-runtime-worker")
    orchestrator = Goal05WorkflowOrchestrator(scheduler)
    return scheduler, host, orchestrator


def test_workflow_enqueue_and_idempotency() -> None:
    scheduler, _host, orchestrator = make_workflow()
    steps = (
        WorkflowStepSpec(step_key="prepare", job_kind="workflow.local.prepare", payload={"topic_id": "topic-1"}),
        WorkflowStepSpec(step_key="draft", job_kind="workflow.local.draft", payload={"topic_id": "topic-1"}),
    )
    first = orchestrator.start_workflow(
        workflow_name="creation.local",
        steps=steps,
        idempotency_key="workflow-topic-1",
    )
    replay = orchestrator.start_workflow(
        workflow_name="creation.local",
        steps=steps,
        idempotency_key="workflow-topic-1",
    )
    assert first.workflow_id == replay.workflow_id
    assert first.job_ids == replay.job_ids
    assert replay.replayed
    count = scheduler.conn.execute("SELECT count(*) FROM scheduler_job").fetchone()[0]
    assert count == 2
    row = scheduler.conn.execute("SELECT payload_json FROM scheduler_job WHERE job_kind='workflow.local.prepare'").fetchone()
    assert first.workflow_id in row["payload_json"]
    print("PASS workflow enqueue idempotency")


def test_workflow_dispatches_through_runtime_host() -> None:
    scheduler, host, orchestrator = make_workflow()
    handled: list[str] = []

    def handle_step(payload):
        handled.append(payload["_workflow"]["step_key"])
        return {"step_key": payload["_workflow"]["step_key"]}

    host.register_handler(
        "workflow.local.step",
        handle_step,
        contract=RuntimeHandlerContract(
            job_kind="workflow.local.step",
            required_payload_keys=("_workflow",),
            required_result_keys=("step_key",),
        ),
    )
    started = orchestrator.start_workflow(
        workflow_name="creation.local",
        steps=(
            WorkflowStepSpec(step_key="align", job_kind="workflow.local.step", payload={"topic_id": "topic-2"}),
            WorkflowStepSpec(step_key="outline", job_kind="workflow.local.step", payload={"topic_id": "topic-2"}),
        ),
        idempotency_key="workflow-topic-2",
    )
    results = host.run_batch(max_jobs=5)
    assert len(results) == 2
    assert all(result.status == "succeeded" for result in results)
    assert handled == ["align", "outline"]
    succeeded = scheduler.conn.execute("SELECT count(*) FROM scheduler_job WHERE status='succeeded'").fetchone()[0]
    assert succeeded == len(started.job_ids)
    print("PASS workflow dispatches through runtime host")


def test_invalid_workflows_rejected() -> None:
    _scheduler, _host, orchestrator = make_workflow()
    cases = (
        lambda: orchestrator.start_workflow(workflow_name="", steps=(), idempotency_key="bad"),
        lambda: orchestrator.start_workflow(workflow_name="bad", steps=(), idempotency_key="bad"),
        lambda: orchestrator.start_workflow(
            workflow_name="bad",
            steps=(WorkflowStepSpec(step_key="dup", job_kind="workflow.local.step", payload={}), WorkflowStepSpec(step_key="dup", job_kind="workflow.local.step", payload={})),
            idempotency_key="bad",
        ),
        lambda: orchestrator.start_workflow(
            workflow_name="bad",
            steps=(WorkflowStepSpec(step_key="step", job_kind="", payload={}),),
            idempotency_key="bad",
        ),
        lambda: orchestrator.start_workflow(
            workflow_name="bad",
            steps=(WorkflowStepSpec(step_key="step", job_kind="workflow.local.step", payload={}, max_attempts=0),),
            idempotency_key="bad",
        ),
    )
    for case in cases:
        try:
            case()
        except WorkflowError:
            continue
        raise AssertionError("expected invalid workflow rejection")
    print("PASS invalid workflows rejected")


def main() -> int:
    test_workflow_enqueue_and_idempotency()
    test_workflow_dispatches_through_runtime_host()
    test_invalid_workflows_rejected()
    print("GOAL-05 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
