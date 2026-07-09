from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import IdempotencyConflict, UUIDv7Generator
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer


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


def make_core() -> CoreMaterializer:
    clock = FakeClock(1_725_100_000_000)
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits())
    core = CoreMaterializer.in_memory(id_factory=generator.new)
    core.grant_permission("core_tester", "create_state")
    core.grant_permission("core_tester", "transition_state")
    return core


def expect_failure(label: str, fn, *error_types: type[BaseException]) -> None:
    try:
        fn()
    except error_types:
        print(f"PASS {label}")
        return
    raise AssertionError(f"expected failure: {label}")


def create_state(core: CoreMaterializer, kind: str, key: str):
    return core.execute(
        CoreCommandEnvelope(
            command_type="create_state",
            actor="core_tester",
            object_kind=kind,
            idempotency_key=key,
            payload={},
        )
    )


def transition_state(
    core: CoreMaterializer,
    kind: str,
    object_id: str,
    basis_version_id: str,
    to_state: str,
    key: str,
    *,
    confirmed: bool = False,
    extra_payload: dict | None = None,
):
    payload = {"to_state": to_state}
    if extra_payload:
        payload.update(extra_payload)
    return core.execute(
        CoreCommandEnvelope(
            command_type="transition_state",
            actor="core_tester",
            object_kind=kind,
            object_id=object_id,
            expected_basis_version_id=basis_version_id,
            idempotency_key=key,
            confirmed=confirmed,
            payload=payload,
        )
    )


def test_create_all_goal02_states() -> None:
    core = make_core()
    expected = {
        "production_task": "draft",
        "topic": "candidate",
        "claim": "proposed",
        "experiment": "planned",
        "tactic": "candidate",
    }
    for kind, state in expected.items():
        result = create_state(core, kind, f"create-{kind}")
        assert result.status == "succeeded"
        assert result.object_id is not None
        row = core.get_state(kind, result.object_id)
        assert row["state"] == state
        assert row["basis_version_id"] == result.basis_version_id
    print("PASS production/topic/claim/experiment/tactic states created")


def test_permission_and_idempotency() -> None:
    core = make_core()
    denied = core.execute(
        CoreCommandEnvelope(
            command_type="create_state",
            actor="viewer",
            object_kind="topic",
            idempotency_key="viewer-create-topic",
            payload={},
        )
    )
    assert denied.status == "rejected"
    assert denied.reason == "permission_denied"
    count = core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0]
    assert count == 0

    first = create_state(core, "topic", "same-create-topic")
    replay = create_state(core, "topic", "same-create-topic")
    assert first.receipt_id == replay.receipt_id
    assert replay.replayed
    count = core.conn.execute("SELECT count(*) FROM topic_state").fetchone()[0]
    assert count == 1

    expect_failure(
        "idempotency conflict rejected",
        lambda: core.execute(
            CoreCommandEnvelope(
                command_type="create_state",
                actor="core_tester",
                object_kind="topic",
                idempotency_key="same-create-topic",
                payload={"state": "selected"},
            )
        ),
        IdempotencyConflict,
    )
    print("PASS permission and idempotency gates")


def test_state_one_way_and_confirmation_gate() -> None:
    core = make_core()
    topic = create_state(core, "topic", "topic-create")
    selected = transition_state(
        core,
        "topic",
        topic.object_id or "",
        topic.basis_version_id or "",
        "selected",
        "topic-selected",
    )
    assert selected.status == "succeeded"
    row = core.get_state("topic", topic.object_id or "")
    assert row["state"] == "selected"

    expect_failure(
        "state cannot move backward at DB gate",
        lambda: core.conn.execute(
            "UPDATE topic_state SET state=?, state_rank=? WHERE topic_id=?",
            ("candidate", 0, topic.object_id),
        ),
        sqlite3.DatabaseError,
    )

    researching = transition_state(
        core,
        "topic",
        topic.object_id or "",
        selected.basis_version_id or "",
        "researching",
        "topic-researching",
    )
    planned = transition_state(
        core,
        "topic",
        topic.object_id or "",
        researching.basis_version_id or "",
        "planned",
        "topic-planned",
    )
    rejected = transition_state(
        core,
        "topic",
        topic.object_id or "",
        planned.basis_version_id or "",
        "approved",
        "topic-approved-unconfirmed",
    )
    assert rejected.status == "rejected"
    assert rejected.reason == "confirmation_required"
    row = core.get_state("topic", topic.object_id or "")
    assert row["state"] == "planned"

    approved = transition_state(
        core,
        "topic",
        topic.object_id or "",
        planned.basis_version_id or "",
        "approved",
        "topic-approved-confirmed",
        confirmed=True,
    )
    assert approved.status == "succeeded"
    print("PASS one-way state and confirmation gate")


def test_stale_basis_cannot_activate() -> None:
    core = make_core()
    tactic = create_state(core, "tactic", "tactic-create")
    other = create_state(core, "topic", "topic-stale-basis-source")
    stale = transition_state(
        core,
        "tactic",
        tactic.object_id or "",
        other.basis_version_id or "",
        "active",
        "tactic-active-stale",
        confirmed=True,
    )
    assert stale.status == "rejected"
    assert stale.reason == "stale_basis"
    row = core.get_state("tactic", tactic.object_id or "")
    assert row["state"] == "candidate"

    unconfirmed = transition_state(
        core,
        "tactic",
        tactic.object_id or "",
        tactic.basis_version_id or "",
        "active",
        "tactic-active-unconfirmed",
    )
    assert unconfirmed.status == "rejected"
    assert unconfirmed.reason == "confirmation_required"

    active = transition_state(
        core,
        "tactic",
        tactic.object_id or "",
        tactic.basis_version_id or "",
        "active",
        "tactic-active-confirmed",
        confirmed=True,
    )
    assert active.status == "succeeded"
    print("PASS stale basis cannot activate")


def test_materializer_transaction_rollback() -> None:
    core = make_core()
    experiment = create_state(core, "experiment", "experiment-create")
    before_versions = core.conn.execute(
        "SELECT count(*) FROM trace_version WHERE root_id=?",
        (experiment.object_id,),
    ).fetchone()[0]
    before_outbox = core.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0]

    expect_failure(
        "materializer transaction rolls back injected fault",
        lambda: transition_state(
            core,
            "experiment",
            experiment.object_id or "",
            experiment.basis_version_id or "",
            "active",
            "experiment-active-fault",
            confirmed=True,
            extra_payload={"inject_fault_after_version": True},
        ),
        RuntimeError,
    )

    row = core.get_state("experiment", experiment.object_id or "")
    assert row["state"] == "planned"
    after_versions = core.conn.execute(
        "SELECT count(*) FROM trace_version WHERE root_id=?",
        (experiment.object_id,),
    ).fetchone()[0]
    after_outbox = core.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0]
    assert before_versions == after_versions
    assert before_outbox == after_outbox
    print("PASS materializer transaction rollback")


def test_success_correlation_and_command_envelope_immutability() -> None:
    core = make_core()
    claim = create_state(core, "claim", "claim-create")
    checking = transition_state(
        core,
        "claim",
        claim.object_id or "",
        claim.basis_version_id or "",
        "checking",
        "claim-checking",
    )
    assert checking.command_id is not None
    joined = core.conn.execute(
        """
        SELECT c.correlation_id AS command_corr,
               a.correlation_id AS audit_corr,
               o.correlation_id AS outbox_corr
          FROM core_command_envelope c
          JOIN audit_event a ON a.correlation_id = c.correlation_id
          JOIN outbox_message o ON o.correlation_id = c.correlation_id
                               AND o.causation_id = a.audit_id
         WHERE c.command_id=?
        """,
        (checking.command_id,),
    ).fetchone()
    assert joined["command_corr"] == joined["audit_corr"] == joined["outbox_corr"]
    expect_failure(
        "core command envelope immutable",
        lambda: core.conn.execute(
            "UPDATE core_command_envelope SET status=? WHERE command_id=?",
            ("failed", checking.command_id),
        ),
        sqlite3.DatabaseError,
    )
    print("PASS command envelope correlation and immutability")


def main() -> int:
    test_create_all_goal02_states()
    test_permission_and_idempotency()
    test_state_one_way_and_confirmation_gate()
    test_stale_basis_cannot_activate()
    test_materializer_transaction_rollback()
    test_success_correlation_and_command_envelope_immutability()
    print("GOAL-02 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
