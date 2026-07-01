from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import UUIDv7Generator
from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost, RuntimeHostError
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler


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


def make_runtime() -> tuple[Goal03Scheduler, RuntimeHost]:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=clock.now_ms)
    host = RuntimeHost(scheduler, worker_id="runtime-worker")
    return scheduler, host


def test_idle_when_no_job() -> None:
    _scheduler, host = make_runtime()
    result = host.run_once()
    assert result.status == "idle"
    print("PASS runtime host idle")


def test_dispatch_success_completes_job() -> None:
    scheduler, host = make_runtime()
    host.register_handler(
        "local.echo",
        lambda payload: {"echo": payload["value"]},
        contract=RuntimeHandlerContract(
            job_kind="local.echo",
            required_payload_keys=("value",),
            required_result_keys=("echo",),
        ),
    )
    enqueued = scheduler.enqueue_job(
        job_kind="local.echo",
        payload={"value": "ok"},
        idempotency_key="runtime-echo",
    )
    result = host.run_once()
    assert result.status == "succeeded"
    assert result.job_id == enqueued.job_id
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "succeeded"
    print("PASS runtime host dispatch success")


def test_contract_mismatch_rejected_at_registration() -> None:
    _scheduler, host = make_runtime()
    try:
        host.register_handler(
            "local.echo",
            lambda payload: payload,
            contract=RuntimeHandlerContract(job_kind="local.other"),
        )
    except RuntimeHostError:
        print("PASS runtime host rejects mismatched contract")
        return
    raise AssertionError("expected runtime host contract mismatch rejection")


def test_contract_input_error_dead_letters_without_retry() -> None:
    scheduler, host = make_runtime()
    host.register_handler(
        "local.needs-input",
        lambda payload: {"ok": payload["required"]},
        contract=RuntimeHandlerContract(
            job_kind="local.needs-input",
            required_payload_keys=("required",),
        ),
    )
    enqueued = scheduler.enqueue_job(
        job_kind="local.needs-input",
        payload={"other": "missing"},
        idempotency_key="runtime-contract-input",
        max_attempts=2,
    )
    result = host.run_once()
    assert result.status == "failed"
    assert result.reason == "contract_input_error"
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "dead"
    assert job["attempt_count"] == 1
    print("PASS runtime host contract input error dead-letters")


def test_contract_output_error_requeues() -> None:
    scheduler, host = make_runtime()
    host.register_handler(
        "local.needs-output",
        lambda _payload: {"other": "missing"},
        contract=RuntimeHandlerContract(
            job_kind="local.needs-output",
            required_result_keys=("required",),
        ),
    )
    enqueued = scheduler.enqueue_job(
        job_kind="local.needs-output",
        payload={"value": "ok"},
        idempotency_key="runtime-contract-output",
        max_attempts=2,
    )
    result = host.run_once()
    assert result.status == "failed"
    assert result.reason == "contract_output_error"
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "queued"
    assert job["attempt_count"] == 1
    print("PASS runtime host contract output error requeues")


def test_handler_failure_requeues_job() -> None:
    scheduler, host = make_runtime()

    def fail_handler(_payload):
        raise RuntimeError("boom")

    host.register_handler("local.fail", fail_handler)
    enqueued = scheduler.enqueue_job(
        job_kind="local.fail",
        payload={"value": "bad"},
        idempotency_key="runtime-fail",
        max_attempts=2,
    )
    result = host.run_once()
    assert result.status == "failed"
    assert result.reason == "handler_error"
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "queued"
    assert job["attempt_count"] == 1
    print("PASS runtime host handler failure requeues")


def test_unknown_handler_dead_letters_without_duplicate_side_effect() -> None:
    scheduler, host = make_runtime()
    enqueued = scheduler.enqueue_job(
        job_kind="local.missing",
        payload={"value": "missing"},
        idempotency_key="runtime-missing",
        max_attempts=1,
    )
    result = host.run_once()
    assert result.status == "failed"
    assert result.reason == "unknown_job_kind"
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "dead"
    attempts = scheduler.conn.execute(
        "SELECT count(*) FROM scheduler_job_attempt WHERE job_id=?",
        (enqueued.job_id,),
    ).fetchone()[0]
    assert attempts == 1
    print("PASS runtime host unknown handler dead-letters once")


def main() -> int:
    test_idle_when_no_job()
    test_dispatch_success_completes_job()
    test_contract_mismatch_rejected_at_registration()
    test_contract_input_error_dead_letters_without_retry()
    test_contract_output_error_requeues()
    test_handler_failure_requeues_job()
    test_unknown_handler_dead_letters_without_duplicate_side_effect()
    print("GOAL-04 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
