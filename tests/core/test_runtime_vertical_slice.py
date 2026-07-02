from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.persistence.goal01_store import IdempotencyConflict, UUIDv7Generator
from scripts.core.runtime.goal_runtime_vertical_slice import (
    RuntimeProbeContract,
    RuntimeProbeValidationError,
    make_runtime_probe_harness,
)
from scripts.core.scheduler.goal03_scheduler import InvalidWorkerLease, NoClaimableJob
from scripts.validation.clean_room_empty_db import health_check
from scripts.validation.production_startup_smoke import run_smoke


ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, start_ms: int = 1_725_100_000_000):
        self.current_ms = start_ms

    def now_ms(self) -> int:
        return self.current_ms

    def advance(self, ms: int) -> None:
        self.current_ms += ms


class DeterministicBits:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, bit_count: int) -> int:
        self.value += 1
        return self.value & ((1 << bit_count) - 1)


def make_harness():
    clock = FakeClock()
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    harness = make_runtime_probe_harness(id_factory=generator.new, now_ms=clock.now_ms, monotonic_ms=clock.now_ms)
    return harness, clock


def valid_payload(**overrides):
    payload = {
        "request_id": "probe-1",
        "correlation_id": "018f0000-0000-7000-8000-000000000101",
        "text": "  Hello   Runtime  ",
        "requested_operation": "normalize",
        "metadata": {"case_id": "success"},
        "idempotency_key": "probe-key-1",
    }
    payload.update(overrides)
    return payload


class RuntimeVerticalSliceTests(unittest.TestCase):
    def make_harness(self):
        harness, clock = make_harness()
        self.addCleanup(harness.store.conn.close)
        return harness, clock

    def test_schema_accepts_valid_and_rejects_missing_or_extra_fields(self) -> None:
        request = RuntimeProbeContract.validate_request_dict(valid_payload())
        self.assertEqual(request.request_id, "probe-1")
        with self.assertRaises(RuntimeProbeValidationError):
            RuntimeProbeContract.validate_request_dict({"request_id": "only"})
        with self.assertRaises(RuntimeProbeValidationError):
            RuntimeProbeContract.validate_request_dict(valid_payload(extra="nope"))
        with self.assertRaises(RuntimeProbeValidationError):
            RuntimeProbeContract.validate_request_dict(valid_payload(metadata={"secret": "nope"}))

    def test_core_api_creates_job_and_duplicate_submit_replays(self) -> None:
        harness, _clock = self.make_harness()
        self.assertEqual(harness.api.health()["status"], "ok")
        self.assertEqual(harness.api.readiness()["status"], "ready")
        created = harness.api.create_runtime_probe_job(valid_payload())
        replay = harness.api.create_runtime_probe_job(valid_payload())
        self.assertEqual(created.job_id, replay.job_id)
        self.assertTrue(replay.replayed)
        with self.assertRaises(IdempotencyConflict):
            harness.api.create_runtime_probe_job(valid_payload(text="changed"))

    def test_end_to_end_success_materializes_result_and_outbox(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(valid_payload())
        step = harness.worker.run_once()
        self.assertEqual(step.status, "succeeded")
        job = harness.api.get_job(created.job_id)
        self.assertEqual(job["status"], "succeeded")
        result = harness.api.get_result(created.job_id)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["output"]["normalized_text"], "hello runtime")
        self.assertEqual(result["schema_version"], "runtime_probe.output.v1")
        outbox = harness.api.list_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["status"], "pending")
        self.assertEqual(outbox[0]["payload"]["job_id"], created.job_id)

    def test_same_job_cannot_be_completed_by_two_workers(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(valid_payload())
        claim = harness.scheduler.claim_next(worker_id="worker-a")
        with self.assertRaises(NoClaimableJob):
            harness.scheduler.claim_next(worker_id="worker-b")
        harness.scheduler.complete(attempt_id=claim.attempt_id, worker_id="worker-a", result={"ok": True})
        with self.assertRaises(InvalidWorkerLease):
            harness.scheduler.complete(attempt_id=claim.attempt_id, worker_id="worker-b", result={"ok": True})
        self.assertEqual(harness.api.get_job(created.job_id)["status"], "succeeded")

    def test_model_port_failure_retries_without_formal_result(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(
            valid_payload(metadata={"case_id": "failure", "provider_behavior": "failure"})
        )
        first = harness.worker.run_once()
        self.assertEqual(first.status, "failed")
        self.assertEqual(first.reason, "queued")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.get_job(created.job_id)["status"], "queued")
        run_count = harness.scheduler.conn.execute(
            "SELECT count(*) FROM runtime_probe_skill_run WHERE job_id=? AND status='failed'",
            (created.job_id,),
        ).fetchone()[0]
        self.assertEqual(run_count, 1)

    def test_output_schema_error_retries_without_result(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(
            valid_payload(metadata={"case_id": "bad-output", "provider_behavior": "invalid_structure"})
        )
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])

    def test_empty_model_output_fails_without_result(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(
            valid_payload(metadata={"case_id": "empty", "provider_behavior": "empty"})
        )
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertIsNone(harness.api.get_result(created.job_id))

    def test_model_port_timeout_fails_without_result(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(
            valid_payload(metadata={"case_id": "timeout", "provider_behavior": "timeout"})
        )
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertIsNone(harness.api.get_result(created.job_id))

    def test_retry_then_success_does_not_duplicate_result_or_outbox(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(
            valid_payload(metadata={"case_id": "retry", "provider_behavior": "failure"})
        )
        self.assertEqual(harness.worker.run_once().status, "failed")
        row = harness.scheduler.conn.execute(
            "SELECT payload_json FROM scheduler_job WHERE job_id=?",
            (created.job_id,),
        ).fetchone()
        payload = row["payload_json"].replace('"provider_behavior":"failure"', '"provider_behavior":"success"')
        harness.scheduler.conn.execute("UPDATE scheduler_job SET payload_json=? WHERE job_id=?", (payload, created.job_id))
        step = harness.worker.run_once()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(harness.api.get_result(created.job_id))
        self.assertEqual(len(harness.api.list_outbox()), 1)

    def test_lease_expiry_recovery_allows_worker_retry(self) -> None:
        harness, clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(valid_payload())
        claim = harness.scheduler.claim_next(worker_id="worker-a", lease_seconds=1)
        clock.advance(2_000)
        recovered = harness.scheduler.recover_expired_leases()
        self.assertEqual(recovered, 1)
        self.assertEqual(harness.api.get_job(created.job_id)["status"], "queued")
        step = harness.worker.run_once()
        self.assertEqual(step.status, "succeeded")
        failed_attempt = harness.scheduler.conn.execute(
            "SELECT status FROM scheduler_job_attempt WHERE attempt_id=?",
            (claim.attempt_id,),
        ).fetchone()
        self.assertEqual(failed_attempt["status"], "failed")

    def test_success_result_index_is_immutable(self) -> None:
        harness, _clock = self.make_harness()
        created = harness.api.create_runtime_probe_job(valid_payload())
        harness.worker.run_once()
        with self.assertRaises(sqlite3.DatabaseError):
            harness.scheduler.conn.execute(
                "UPDATE runtime_probe_result_index SET output_hash='changed' WHERE job_id=?",
                (created.job_id,),
            )

    def test_clean_room_production_paths_remain_empty_and_fixture_rejected(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            smoke = run_smoke(init_if_missing=True, require_legacy_absent=True)
        self.assertEqual(smoke["legacy_paths_present"], [])
        health = health_check(ROOT / "data" / "formal" / "clean_room_v0_6_2.sqlite3")
        self.assertTrue(all(count == 0 for count in health["table_rows"].values()))
        with patch.dict("os.environ", {"CREATION_ASSISTANT_ALLOW_FIXTURES": "1"}, clear=True):
            with self.assertRaises(RuntimeError):
                run_smoke()


if __name__ == "__main__":
    unittest.main()
