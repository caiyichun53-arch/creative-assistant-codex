from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, UUIDv7Generator
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer


class FakeClock:
    def __init__(self, start_ms: int):
        self.current_ms = start_ms

    def now_ms(self) -> int:
        return self.current_ms

    def advance(self, ms: int) -> None:
        self.current_ms += ms


class DeterministicBits:
    def __init__(self):
        self.value = 0

    def __call__(self, bit_count: int) -> int:
        self.value += 1
        return self.value & ((1 << bit_count) - 1)


def make_scheduler() -> tuple[Goal03Scheduler, FakeClock]:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    store = PersistenceStore.in_memory(id_factory=generator.new)
    scheduler = Goal03Scheduler(store, id_factory=generator.new, now_ms=clock.now_ms)
    return scheduler, clock


def expect_failure(label: str, fn, *error_types: type[BaseException]) -> None:
    try:
        fn()
    except error_types:
        print(f"PASS {label}")
        return
    raise AssertionError(f"expected failure: {label}")


def test_enqueue_idempotency_and_conflict() -> None:
    scheduler, _clock = make_scheduler()
    first = scheduler.enqueue_job(
        job_kind="materializer.dispatch",
        payload={"target": "topic"},
        idempotency_key="enqueue-topic",
    )
    replay = scheduler.enqueue_job(
        job_kind="materializer.dispatch",
        payload={"target": "topic"},
        idempotency_key="enqueue-topic",
    )
    assert first.job_id == replay.job_id
    assert first.receipt_id == replay.receipt_id
    assert replay.replayed
    count = scheduler.conn.execute("SELECT count(*) FROM scheduler_job").fetchone()[0]
    assert count == 1
    expect_failure(
        "idempotency conflict rejected",
        lambda: scheduler.enqueue_job(
            job_kind="materializer.dispatch",
            payload={"target": "claim"},
            idempotency_key="enqueue-topic",
        ),
        IdempotencyConflict,
    )
    print("PASS scheduler enqueue idempotency")


def test_outbox_claim_heartbeat_and_complete() -> None:
    scheduler, clock = make_scheduler()
    outbox_id = scheduler.store.enqueue_outbox(
        topic="core.state.changed",
        payload={"object_kind": "topic"},
        correlation_id="018f0000-0000-7000-8000-000000000301",
        causation_id="018f0000-0000-7000-8000-000000000302",
    )
    enqueued = scheduler.enqueue_from_outbox(outbox_id, idempotency_key="outbox-topic")
    job = scheduler.get_job(enqueued.job_id)
    assert job["source_outbox_id"] == outbox_id
    claim = scheduler.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claim.job_id == enqueued.job_id
    assert claim.attempt_no == 1
    assert claim.payload["outbox_id"] == outbox_id
    original_lease = claim.lease_expires_at
    clock.advance(5_000)
    new_lease = scheduler.heartbeat(attempt_id=claim.attempt_id, worker_id="worker-a", lease_seconds=60)
    assert new_lease > original_lease
    scheduler.complete(attempt_id=claim.attempt_id, worker_id="worker-a", result={"delivered": True})
    job = scheduler.get_job(enqueued.job_id)
    assert job["status"] == "succeeded"
    outbox = scheduler.conn.execute("SELECT status, attempts FROM outbox_message WHERE outbox_id=?", (outbox_id,)).fetchone()
    assert outbox["status"] == "sent"
    assert outbox["attempts"] == 1
    joined = scheduler.conn.execute(
        """
        SELECT count(*)
          FROM scheduler_job j
          JOIN audit_event a ON a.correlation_id = j.correlation_id
         WHERE j.job_id=? AND a.object_kind='scheduler_job'
        """,
        (enqueued.job_id,),
    ).fetchone()[0]
    assert joined >= 3
    expect_failure(
        "terminal scheduler_job immutable",
        lambda: scheduler.conn.execute(
            "UPDATE scheduler_job SET status='queued' WHERE job_id=?",
            (enqueued.job_id,),
        ),
        sqlite3.DatabaseError,
    )
    print("PASS outbox claim heartbeat complete and terminal gate")


def test_goal02_outbox_schedules_goal03_job() -> None:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    core = CoreMaterializer.in_memory(id_factory=generator.new)
    core.grant_permission("core_tester", "create_state")
    created = core.execute(
        CoreCommandEnvelope(
            command_type="create_state",
            actor="core_tester",
            object_kind="topic",
            idempotency_key="goal02-topic-for-scheduler",
            payload={},
        )
    )
    assert created.status == "succeeded"
    outbox = core.conn.execute(
        "SELECT outbox_id, correlation_id FROM outbox_message WHERE topic='core.state.changed'"
    ).fetchone()
    assert outbox is not None
    scheduler = Goal03Scheduler(core.store, id_factory=generator.new, now_ms=clock.now_ms)
    enqueued = scheduler.enqueue_from_outbox(outbox["outbox_id"], idempotency_key="goal02-outbox-job")
    job = scheduler.get_job(enqueued.job_id)
    assert job["source_outbox_id"] == outbox["outbox_id"]
    assert job["correlation_id"] == outbox["correlation_id"]
    claim = scheduler.claim_next(worker_id="worker-goal02")
    assert claim.payload["topic"] == "core.state.changed"
    assert claim.payload["payload"]["object_kind"] == "topic"
    print("PASS GOAL-02 outbox schedules GOAL-03 job")


def test_retry_and_dead_letter() -> None:
    scheduler, _clock = make_scheduler()
    result = scheduler.enqueue_job(
        job_kind="portable.skill",
        payload={"skill": "example"},
        idempotency_key="retry-job",
        max_attempts=2,
    )
    first = scheduler.claim_next(worker_id="worker-a")
    next_status = scheduler.fail(
        attempt_id=first.attempt_id,
        worker_id="worker-a",
        error={"code": "temporary"},
        retry=True,
    )
    assert next_status == "queued"
    second = scheduler.claim_next(worker_id="worker-b")
    assert second.job_id == result.job_id
    assert second.attempt_no == 2
    next_status = scheduler.fail(
        attempt_id=second.attempt_id,
        worker_id="worker-b",
        error={"code": "permanent"},
        retry=True,
    )
    assert next_status == "dead"
    job = scheduler.get_job(result.job_id)
    assert job["status"] == "dead"
    attempts = scheduler.conn.execute(
        "SELECT count(*) FROM scheduler_job_attempt WHERE job_id=? AND status='failed'",
        (result.job_id,),
    ).fetchone()[0]
    assert attempts == 2
    print("PASS retry and dead-letter gate")


def test_expired_lease_recovery() -> None:
    scheduler, clock = make_scheduler()
    result = scheduler.enqueue_job(
        job_kind="portable.skill",
        payload={"skill": "slow"},
        idempotency_key="recover-job",
        max_attempts=2,
    )
    claim = scheduler.claim_next(worker_id="worker-a", lease_seconds=1)
    clock.advance(2_000)
    recovered = scheduler.recover_expired_leases()
    assert recovered == 1
    job = scheduler.get_job(result.job_id)
    assert job["status"] == "queued"
    failed_attempt = scheduler.conn.execute(
        "SELECT status, error_json FROM scheduler_job_attempt WHERE attempt_id=?",
        (claim.attempt_id,),
    ).fetchone()
    assert failed_attempt["status"] == "failed"
    assert "lease_expired" in failed_attempt["error_json"]
    next_claim = scheduler.claim_next(worker_id="worker-b")
    assert next_claim.attempt_no == 2
    print("PASS expired lease recovery")


def test_cancel_prevents_claim() -> None:
    scheduler, _clock = make_scheduler()
    result = scheduler.enqueue_job(
        job_kind="portable.skill",
        payload={"skill": "cancel"},
        idempotency_key="cancel-job",
    )
    scheduler.cancel_job(job_id=result.job_id, actor="core_tester", reason="not needed")
    job = scheduler.get_job(result.job_id)
    assert job["status"] == "cancelled"
    expect_failure(
        "cancelled job cannot be claimed",
        lambda: scheduler.claim_next(worker_id="worker-a"),
        NoClaimableJob,
    )
    print("PASS cancel gate")


def test_enqueue_transaction_rollback() -> None:
    scheduler, _clock = make_scheduler()
    expect_failure(
        "scheduler transaction rolls back injected fault",
        lambda: scheduler.enqueue_job(
            job_kind="portable.skill",
            payload={"inject": "fault"},
            idempotency_key="fault-job",
            inject_fault_after_job=True,
        ),
        RuntimeError,
    )
    jobs = scheduler.conn.execute("SELECT count(*) FROM scheduler_job").fetchone()[0]
    receipts = scheduler.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope='goal03.scheduler.enqueue'"
    ).fetchone()[0]
    assert jobs == 0
    assert receipts == 0
    print("PASS scheduler enqueue transaction rollback")


def main() -> int:
    test_enqueue_idempotency_and_conflict()
    test_outbox_claim_heartbeat_and_complete()
    test_goal02_outbox_schedules_goal03_job()
    test_retry_and_dead_letter()
    test_expired_lease_recovery()
    test_cancel_prevents_claim()
    test_enqueue_transaction_rollback()
    print("GOAL-03 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
