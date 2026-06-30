from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, blob_hash, content_hash, uuid7


def expect_sqlite_failure(label: str, fn) -> None:
    try:
        fn()
    except sqlite3.IntegrityError:
        print(f"PASS {label}")
        return
    except sqlite3.DatabaseError:
        print(f"PASS {label}")
        return
    raise AssertionError(f"expected sqlite failure: {label}")


def test_uuid7_shape_and_order() -> None:
    ids = [uuid7() for _ in range(5)]
    assert all(len(x) == 36 and x[14] == "7" for x in ids)
    assert ids == sorted(ids)
    print("PASS uuid7 shape and monotonic order")


def test_version_immutability_and_cross_root_pointer() -> None:
    store = PersistenceStore.in_memory()
    root_a = store.create_root("script")
    root_b = store.create_root("script")
    version_a = store.append_version(
        root_a,
        {"text": "a"},
        blob_payload=b"raw-a",
        business_payload={"business": "a"},
    )
    version_b = store.append_version(root_b, {"text": "b"})
    store.set_current_version(root_a, version_a)
    version_row = store.conn.execute(
        """
        SELECT blob_hash, content_hash, business_hash
          FROM trace_version
         WHERE version_id=?
        """,
        (version_a,),
    ).fetchone()
    assert version_row["blob_hash"] == blob_hash(b"raw-a")
    assert version_row["content_hash"] == content_hash({"text": "a"})
    assert version_row["business_hash"] == content_hash({"business": "a"})
    print("PASS blob/content/business hashes recorded")

    expect_sqlite_failure(
        "version cannot be overwritten",
        lambda: store.conn.execute(
            "UPDATE trace_version SET payload_json=? WHERE version_id=?",
            ('{"text":"changed"}', version_a),
        ),
    )
    expect_sqlite_failure(
        "version cannot be deleted",
        lambda: store.conn.execute("DELETE FROM trace_version WHERE version_id=?", (version_a,)),
    )
    expect_sqlite_failure(
        "cross-root current pointer rejected",
        lambda: store.set_current_version(root_a, version_b),
    )


def test_references_and_binding_manifest() -> None:
    store = PersistenceStore.in_memory()
    source_root = store.create_root("script")
    target_root = store.create_root("evidence")
    source_version = store.append_version(source_root, {"text": "source"})
    target_version = store.append_version(target_root, {"text": "target"})
    reference_id = store.record_object_reference(
        source_version_id=source_version,
        relation_role="cites",
        target_object_kind="evidence",
        target_stable_id=target_root,
        target_version_id=target_version,
        target_content_hash=content_hash({"text": "target"}),
        locator={"local_ref": "ref_1"},
    )
    manifest_id = store.record_binding_manifest(
        source_version_id=source_version,
        local_ref="ref_1",
        object_ref={"reference_id": reference_id},
        before_hash=content_hash({"local_ref": "ref_1"}),
        after_hash=content_hash({"reference_id": reference_id}),
    )
    expect_sqlite_failure(
        "object reference cannot be overwritten",
        lambda: store.conn.execute(
            "UPDATE object_reference SET relation_role=? WHERE reference_id=?",
            ("changed", reference_id),
        ),
    )
    expect_sqlite_failure(
        "binding manifest cannot be deleted",
        lambda: store.conn.execute("DELETE FROM binding_manifest WHERE manifest_id=?", (manifest_id,)),
    )
    print("PASS object reference and binding manifest recorded")


def test_command_idempotency() -> None:
    store = PersistenceStore.in_memory()
    first = store.record_command(
        command_scope="goal01.verify",
        idempotency_key="same-key",
        request_payload={"action": "create", "value": 1},
        result_payload={"ok": True},
        correlation_id=uuid7(),
    )
    second = store.record_command(
        command_scope="goal01.verify",
        idempotency_key="same-key",
        request_payload={"action": "create", "value": 1},
        result_payload={"ok": True},
        correlation_id=uuid7(),
    )
    assert first == second
    try:
        store.record_command(
            command_scope="goal01.verify",
            idempotency_key="same-key",
            request_payload={"action": "create", "value": 2},
            result_payload={"ok": True},
            correlation_id=uuid7(),
        )
    except IdempotencyConflict:
        print("PASS idempotency conflict rejected")
    else:
        raise AssertionError("different request reused idempotency key")
    expect_sqlite_failure(
        "command receipt cannot be overwritten",
        lambda: store.conn.execute(
            "UPDATE command_receipt SET status=? WHERE receipt_id=?",
            ("failed", first),
        ),
    )
    print("PASS idempotency same request returns receipt")


def test_preference_candidate_cannot_be_current() -> None:
    store = PersistenceStore.in_memory()
    profile_a = store.create_preference_profile("global", None)
    profile_b = store.create_preference_profile("domain", uuid7())
    candidate = store.append_preference_revision(
        profile_a,
        status="candidate",
        preference_payload={"voice": "plain spoken"},
        evidence_refs=[{"kind": "human_edit", "id": "sample-1"}],
        origin="human_edit",
    )
    published = store.append_preference_revision(
        profile_a,
        status="published",
        preference_payload={"voice": "plain spoken"},
        evidence_refs=[{"kind": "explicit_instruction", "id": "sample-2"}],
        origin="explicit_instruction",
    )
    foreign_published = store.append_preference_revision(
        profile_b,
        status="published",
        preference_payload={"voice": "different"},
        evidence_refs=[],
        origin="explicit_instruction",
    )

    expect_sqlite_failure(
        "candidate preference cannot be current",
        lambda: store.set_current_preference(profile_a, candidate),
    )
    expect_sqlite_failure(
        "duplicate global preference profile rejected",
        lambda: store.create_preference_profile("global", None),
    )
    expect_sqlite_failure(
        "cross-profile preference pointer rejected",
        lambda: store.set_current_preference(profile_a, foreign_published),
    )
    store.set_current_preference(profile_a, published)
    row = store.conn.execute(
        "SELECT current_revision_id FROM content_preference_profile WHERE profile_id=?",
        (profile_a,),
    ).fetchone()
    assert row["current_revision_id"] == published
    print("PASS published preference can be current")


def test_audit_and_outbox_correlation() -> None:
    store = PersistenceStore.in_memory()
    root = store.create_root("script")
    version = store.append_version(root, {"text": "hello"})
    correlation_id = uuid7()
    audit_id = store.record_audit(
        event_type="version_created",
        actor="goal01_verify",
        object_kind="script",
        object_id=root,
        version_id=version,
        payload={"version": version},
        correlation_id=correlation_id,
    )
    outbox_id = store.enqueue_outbox(
        topic="goal01.verify",
        payload={"version": version},
        correlation_id=correlation_id,
        causation_id=audit_id,
    )
    row = store.conn.execute(
        """
        SELECT a.correlation_id AS audit_corr, o.correlation_id AS outbox_corr, o.causation_id
          FROM audit_event a, outbox_message o
         WHERE a.audit_id=? AND o.outbox_id=?
        """,
        (audit_id, outbox_id),
    ).fetchone()
    assert row["audit_corr"] == correlation_id
    assert row["outbox_corr"] == correlation_id
    assert row["causation_id"] == audit_id
    print("PASS audit/outbox correlation")


def main() -> int:
    test_uuid7_shape_and_order()
    test_version_immutability_and_cross_root_pointer()
    test_references_and_binding_manifest()
    test_command_idempotency()
    test_preference_candidate_cannot_be_current()
    test_audit_and_outbox_correlation()
    print("GOAL-01 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
