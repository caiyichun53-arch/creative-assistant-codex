from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import (
    IdempotencyConflict,
    PersistenceStore,
    UUIDv7Generator,
    content_hash,
)


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


def expect_failure(label: str, fn, *error_types: type[BaseException]) -> None:
    try:
        fn()
    except error_types:
        print(f"PASS {label}")
        return
    raise AssertionError(f"expected failure: {label}")


def make_store() -> tuple[PersistenceStore, FakeClock]:
    clock = FakeClock(1_725_000_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    return PersistenceStore.in_memory(id_factory=generator.new), clock


def apply_fixture(store: PersistenceStore) -> dict[str, str]:
    root = store.create_root("script")
    evidence_root = store.create_root("evidence")
    version = store.append_version(
        root,
        {"title": "fixture", "body": "first"},
        blob_payload=b"fixture-blob",
        business_payload={"normalized": "fixture"},
    )
    evidence_version = store.append_version(evidence_root, {"quote": "source"})
    reference = store.record_object_reference(
        source_version_id=version,
        relation_role="uses_source",
        target_object_kind="evidence",
        target_stable_id=evidence_root,
        target_version_id=evidence_version,
        target_content_hash=content_hash({"quote": "source"}),
        locator={"local_ref": "source_1", "path": "fixture://source"},
    )
    manifest = store.record_binding_manifest(
        source_version_id=version,
        local_ref="source_1",
        object_ref={"reference_id": reference},
        before_hash=content_hash({"local_ref": "source_1"}),
        after_hash=content_hash({"reference_id": reference}),
    )
    store.set_current_version(root, version, expected_row_revision=0)
    return {
        "root": root,
        "version": version,
        "evidence_root": evidence_root,
        "evidence_version": evidence_version,
        "reference": reference,
        "manifest": manifest,
    }


def test_fake_clock_uuid7_replay_order() -> None:
    clock = FakeClock(1_725_000_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    same_tick_ids = [generator.new() for _ in range(5)]
    clock.advance(1)
    next_tick = generator.new()
    assert same_tick_ids == sorted(same_tick_ids)
    assert same_tick_ids[-1] < next_tick
    generator.last_ms = clock.now_ms()
    generator.last_rand_a = 0xFFE
    overflow_edge = generator.new()
    overflow_next = generator.new()
    assert overflow_edge < overflow_next
    print("PASS FakeClock UUIDv7 monotonic replay")


def test_fixture_replay_and_idempotency() -> None:
    store, _clock = make_store()
    fixture = apply_fixture(store)
    correlation_id = fixture["version"]
    first = store.record_command(
        command_scope="goal01.replay",
        idempotency_key="fixture-command",
        request_payload={"fixture": fixture["root"], "action": "publish"},
        result_payload={"version": fixture["version"]},
        correlation_id=correlation_id,
    )
    second = store.record_command(
        command_scope="goal01.replay",
        idempotency_key="fixture-command",
        request_payload={"fixture": fixture["root"], "action": "publish"},
        result_payload={"version": fixture["version"]},
        correlation_id=correlation_id,
    )
    assert first == second
    expect_failure(
        "replay rejects changed request under same idempotency key",
        lambda: store.record_command(
            command_scope="goal01.replay",
            idempotency_key="fixture-command",
            request_payload={"fixture": fixture["root"], "action": "different"},
            result_payload={"version": fixture["version"]},
            correlation_id=correlation_id,
        ),
        IdempotencyConflict,
    )
    print("PASS fixture replay returns original receipt")


def test_fault_injection_gates() -> None:
    store, _clock = make_store()
    fixture = apply_fixture(store)
    next_version = store.append_version(fixture["root"], {"title": "fixture", "body": "second"})

    expect_failure(
        "stale root revision rejected",
        lambda: store.set_current_version(fixture["root"], next_version, expected_row_revision=0),
        RuntimeError,
    )
    expect_failure(
        "reference mutation fault rejected",
        lambda: store.conn.execute(
            "UPDATE object_reference SET relation_role=? WHERE reference_id=?",
            ("changed", fixture["reference"]),
        ),
        sqlite3.DatabaseError,
    )
    expect_failure(
        "invalid receipt status rejected",
        lambda: store.record_command(
            command_scope="goal01.fault",
            idempotency_key="bad-status",
            request_payload={"bad": True},
            result_payload={"ok": False},
            correlation_id=fixture["version"],
            status="unknown",
        ),
        sqlite3.DatabaseError,
    )
    print("PASS fault injection gates")


def main() -> int:
    test_fake_clock_uuid7_replay_order()
    test_fixture_replay_and_idempotency()
    test_fault_injection_gates()
    print("GOAL-01 replay verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
