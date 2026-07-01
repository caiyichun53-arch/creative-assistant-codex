from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.correction.goal10_corrections import (  # noqa: E402
    GOAL10_PROPAGATION_JOB_KIND,
    CorrectionMaterializer,
    CorrectionPropagationCommand,
    CorrectionRegistrationCommand,
    CorrectionResumeCommand,
)
from scripts.core.persistence.goal01_store import (  # noqa: E402
    IdempotencyConflict,
    PersistenceStore,
    UUIDv7Generator,
    content_hash,
)
from scripts.core.production.goal08_production_chain import VersionRef  # noqa: E402
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_780_100_000_000

    def now_ms(self) -> int:
        self.value += 1
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms


def make_store() -> PersistenceStore:
    clock = FakeClock()
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=lambda bits: 101)
    return PersistenceStore.in_memory(id_factory=generator.new)


def payload_for(store: PersistenceStore, version_id: str) -> dict[str, object]:
    row = store.conn.execute("SELECT payload_json FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
    return json.loads(row["payload_json"])


def create_version(
    store: PersistenceStore,
    object_kind: str,
    payload: dict[str, object],
    *,
    projection_version: str | None = None,
    business_payload: dict[str, object] | None = None,
) -> VersionRef:
    projection = projection_version or f"goal10.fixture_{object_kind}.v1"
    root = store.create_root(object_kind)
    version = store.append_version(root, payload, projection_version=projection, business_payload=business_payload)
    store.set_current_version(root, version)
    return VersionRef(
        relation_role="fixture_target",
        target_object_kind=object_kind,
        target_stable_id=root,
        target_version_id=version,
        target_content_hash=content_hash(payload, projection),
        locator={"fixture": object_kind},
    )


def record_ref(
    store: PersistenceStore,
    source_ref: VersionRef,
    target_ref: VersionRef,
    relation_role: str,
) -> str:
    return store.record_object_reference(
        source_version_id=str(source_ref.target_version_id),
        relation_role=relation_role,
        target_object_kind=target_ref.target_object_kind,
        target_stable_id=target_ref.target_stable_id,
        target_version_id=target_ref.target_version_id,
        target_content_hash=target_ref.target_content_hash,
        locator={"fixture_relation": relation_role},
    )


def make_command(store: PersistenceStore, target_ref: VersionRef, *, idempotency_key: str = "goal10-correct-1"):
    evidence_ref = create_version(
        store,
        "source_evidence_snapshot",
        {"url": "https://example.invalid/source", "observed_text": "author correction"},
    )
    return CorrectionRegistrationCommand(
        actor="correction-worker",
        idempotency_key=idempotency_key,
        target_ref=target_ref,
        corrected_payload={"claim": "corrected fact", "confidence": "verified"},
        change_scope="source_fact_value",
        reason="fixture source correction",
        evidence_refs=(evidence_ref,),
        safety_limits={"max_actions": 20, "max_depth": 4},
    )


def test_correction_record_is_immutable_and_preserves_original_history() -> None:
    store = make_store()
    target_ref = create_version(
        store,
        "research_claim",
        {"claim": "old wrong fact", "confidence": "reported"},
        business_payload={"claim": "old wrong fact"},
    )
    original_payload = payload_for(store, str(target_ref.target_version_id))
    result = CorrectionMaterializer(store).register_correction(make_command(store, target_ref))

    correction_payload = payload_for(store, result.version_id)
    assert correction_payload["target_ref"]["target_version_id"] == target_ref.target_version_id
    assert correction_payload["old_content_hash"] == target_ref.target_content_hash
    assert correction_payload["corrected_payload_hash"] != target_ref.target_content_hash
    assert payload_for(store, str(target_ref.target_version_id)) == original_payload
    current = store.conn.execute(
        "SELECT current_version_id FROM trace_root WHERE root_id=?",
        (target_ref.target_stable_id,),
    ).fetchone()["current_version_id"]
    assert current == target_ref.target_version_id
    try:
        store.conn.execute(
            "UPDATE trace_version SET payload_json='{}' WHERE version_id=?",
            (result.version_id,),
        )
    except sqlite3.IntegrityError as exc:
        assert "trace_version is immutable" in str(exc)
    else:
        raise AssertionError("expected immutable correction record version")
    print("PASS GOAL-10 correction record is immutable and original history remains readable")


def test_identical_correction_replays_without_duplicate_side_effects() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old"})
    consumer_ref = create_version(store, "production_script", {"text": "script saw old claim"})
    record_ref(store, consumer_ref, target_ref, "input_assembly_included_ref")
    materializer = CorrectionMaterializer(store)
    command = make_command(store, target_ref, idempotency_key="goal10-replay")
    first = materializer.register_correction(command)
    replay = materializer.register_correction(command)

    assert replay.replayed
    assert replay.root_id == first.root_id
    assert replay.version_id == first.version_id
    assert store.conn.execute(
        "SELECT count(*) FROM trace_root WHERE object_kind='goal10_correction_record'"
    ).fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT count(*) FROM trace_root WHERE object_kind='goal10_correction_impact'"
    ).fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT count(*) FROM outbox_message WHERE topic='goal10.correction.registered'"
    ).fetchone()[0] == 1

    changed = CorrectionRegistrationCommand(
        actor=command.actor,
        idempotency_key=command.idempotency_key,
        target_ref=command.target_ref,
        corrected_payload={"claim": "different correction"},
        change_scope=command.change_scope,
        reason=command.reason,
        evidence_refs=command.evidence_refs,
    )
    try:
        materializer.register_correction(changed)
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("expected changed correction idempotency payload rejection")
    print("PASS GOAL-10 identical correction replays without duplicate side effects")


def test_direct_dependency_index_uses_explicit_refs_only() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old direct fact"})
    direct_ref = create_version(store, "content_plan", {"outline": "explicitly includes old direct fact"})
    semantic_only_ref = create_version(store, "content_plan", {"outline": "mentions old direct fact without ref"})
    binding_ref = create_version(store, "model_run_envelope", {"prompt": "local refs include claim"})
    approved_ref = create_version(store, "production_approved_draft", {"text": "approved old direct fact"})
    publication_ref = create_version(store, "production_publication_capture", {"text": "published old direct fact"})

    record_ref(store, direct_ref, target_ref, "evidence_ref")
    record_ref(store, approved_ref, target_ref, "input_assembly_included_ref")
    record_ref(store, publication_ref, target_ref, "publication_history_ref")
    store.record_binding_manifest(
        source_version_id=str(binding_ref.target_version_id),
        local_ref="claim_a",
        object_ref={
            "relation_role": "binding_local_ref",
            "target_object_kind": target_ref.target_object_kind,
            "target_stable_id": target_ref.target_stable_id,
            "target_version_id": target_ref.target_version_id,
            "target_content_hash": target_ref.target_content_hash,
            "locator": {"local_ref": "claim_a"},
        },
        before_hash=str(target_ref.target_content_hash),
        after_hash=str(target_ref.target_content_hash),
    )

    result = CorrectionMaterializer(store).register_correction(make_command(store, target_ref))
    impact_payloads = [payload_for(store, impact.version_id) for impact in result.impacts]
    impact_targets = {payload["target_version_id"] for payload in impact_payloads}

    assert direct_ref.target_version_id in impact_targets
    assert binding_ref.target_version_id in impact_targets
    assert approved_ref.target_version_id in impact_targets
    assert publication_ref.target_version_id in impact_targets
    assert semantic_only_ref.target_version_id not in impact_targets
    assert len(result.dependency_index_version_ids) == 4
    assert len(result.impacts) == 4
    actions = {payload["target_version_id"]: payload["action_kind"] for payload in impact_payloads}
    assert actions[direct_ref.target_version_id] == "deterministic_recompute"
    assert actions[binding_ref.target_version_id] == "semantic_rerun"
    assert actions[approved_ref.target_version_id] == "require_user_reconfirmation"
    assert actions[publication_ref.target_version_id] == "historical_annotation"
    print("PASS GOAL-10 direct dependency index uses explicit refs only")


def test_impact_basis_key_is_stable_for_same_correction_target_and_action() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old stable fact"})
    consumer_ref = create_version(store, "content_plan", {"outline": "uses old stable fact"})
    record_ref(store, consumer_ref, target_ref, "evidence_ref")
    result = CorrectionMaterializer(store).register_correction(make_command(store, target_ref))

    assert len(result.impacts) == 1
    impact = result.impacts[0]
    payload = payload_for(store, impact.version_id)
    assert payload["expected_basis_hash"] == impact.expected_basis_hash
    assert payload["processing_status"] == "planned"
    assert payload["action_kind"] in {
        "no_action",
        "deterministic_recompute",
        "semantic_rerun",
        "replace_current_candidate",
        "require_user_reconfirmation",
        "historical_annotation",
        "human_attention_required",
    }
    print("PASS GOAL-10 impact basis key is stable and action kind is bounded")


def test_propagation_converges_when_business_hash_and_refs_are_unchanged() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old"})
    consumer_ref = create_version(
        store,
        "content_plan",
        {"outline": "uses source by reference only"},
        business_payload={"outline": "uses source by reference only"},
    )
    record_ref(store, consumer_ref, target_ref, "evidence_ref")
    materializer = CorrectionMaterializer(store)
    registration = materializer.register_correction(make_command(store, target_ref, idempotency_key="goal10-converge"))
    impact = registration.impacts[0]

    first = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="propagation-worker",
            idempotency_key="goal10-process-converge",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=impact.version_id,
        )
    )
    replay = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="propagation-worker",
            idempotency_key="goal10-process-converge",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=impact.version_id,
        )
    )

    assert first.processing_status == "completed"
    assert first.stop_reason == "business_equivalent_refs_unchanged"
    assert first.result["replacement_version_id"] is None
    assert replay.replayed
    assert replay.impact_version_id == first.impact_version_id
    assert store.conn.execute(
        "SELECT count(*) FROM trace_version WHERE root_id=?",
        (consumer_ref.target_stable_id,),
    ).fetchone()[0] == 1
    assert store.conn.execute(
        "SELECT count(*) FROM outbox_message WHERE topic='goal10.correction.impact.completed'"
    ).fetchone()[0] == 1
    print("PASS GOAL-10 propagation converges without duplicate side effects")


def test_propagation_creates_replacement_version_when_business_hash_changes() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old"})
    consumer_ref = create_version(
        store,
        "content_plan",
        {"claim": "old", "outline": "uses corrected fact in body"},
        business_payload={"claim": "old", "outline": "uses corrected fact in body"},
    )
    record_ref(store, consumer_ref, target_ref, "evidence_ref")
    materializer = CorrectionMaterializer(store)
    registration = materializer.register_correction(make_command(store, target_ref, idempotency_key="goal10-replace"))
    impact = registration.impacts[0]

    result = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="propagation-worker",
            idempotency_key="goal10-process-replace",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=impact.version_id,
        )
    )

    assert result.processing_status == "completed"
    assert result.stop_reason == "replacement_version_created"
    replacement_version_id = result.result["replacement_version_id"]
    assert replacement_version_id
    replacement_payload = payload_for(store, replacement_version_id)
    assert replacement_payload["claim"] == "corrected fact"
    current = store.conn.execute(
        "SELECT current_version_id FROM trace_root WHERE root_id=?",
        (consumer_ref.target_stable_id,),
    ).fetchone()["current_version_id"]
    assert current == replacement_version_id
    assert store.conn.execute(
        """
        SELECT count(*)
          FROM object_reference
         WHERE source_version_id=?
           AND relation_role='corrected_by_goal10_correction'
           AND target_version_id=?
        """,
        (replacement_version_id, registration.version_id),
    ).fetchone()[0] == 1
    print("PASS GOAL-10 propagation creates replacement versions through materializer")


def test_blocked_and_resume_recovery_have_audit_correlation_and_causation() -> None:
    store = make_store()
    target_ref = create_version(store, "research_claim", {"claim": "old"})
    approved_ref = create_version(store, "production_approved_draft", {"claim": "old"})
    record_ref(store, approved_ref, target_ref, "input_assembly_included_ref")
    materializer = CorrectionMaterializer(store)
    registration = materializer.register_correction(make_command(store, target_ref, idempotency_key="goal10-block"))
    impact = registration.impacts[0]

    blocked = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="propagation-worker",
            idempotency_key="goal10-process-block",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=impact.version_id,
        )
    )
    assert blocked.processing_status == "blocked"
    assert blocked.stop_reason == "require_user_reconfirmation"
    blocked_audit = store.conn.execute(
        """
        SELECT correlation_id, causation_id
          FROM audit_event
         WHERE event_type='goal10.correction.impact.blocked'
           AND object_id=?
        """,
        (impact.root_id,),
    ).fetchone()
    assert blocked_audit["correlation_id"] == registration.version_id
    assert blocked_audit["causation_id"] == impact.version_id

    resumed = materializer.resume_blocked_impact(
        CorrectionResumeCommand(
            actor="human-reviewer",
            idempotency_key="goal10-resume-block",
            impact_root_id=impact.root_id,
            human_action="reconfirmed corrected draft",
            correlation_id=registration.version_id,
            causation_id=blocked.impact_version_id,
        )
    )
    assert resumed.processing_status == "planned"
    resume_audit = store.conn.execute(
        """
        SELECT correlation_id, causation_id
          FROM audit_event
         WHERE event_type='goal10.correction.impact.resumed'
           AND object_id=?
        """,
        (impact.root_id,),
    ).fetchone()
    assert resume_audit["correlation_id"] == registration.version_id
    assert resume_audit["causation_id"] == blocked.impact_version_id

    completed = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="propagation-worker",
            idempotency_key="goal10-process-resumed",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=resumed.impact_version_id,
        )
    )
    assert completed.processing_status == "completed"
    assert completed.stop_reason == "human_action_recorded"
    print("PASS GOAL-10 blocked and resume recovery keep audit correlation causation")


def test_job_retry_recovers_processing_impact_without_duplicate_replacement() -> None:
    clock = FakeClock()
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=lambda bits: 202)
    store = PersistenceStore.in_memory(id_factory=generator.new)
    scheduler = Goal03Scheduler(store, id_factory=generator.new, now_ms=clock.now_ms)
    target_ref = create_version(store, "research_claim", {"claim": "old"})
    consumer_ref = create_version(
        store,
        "content_plan",
        {"claim": "old", "outline": "uses corrected fact"},
        business_payload={"claim": "old", "outline": "uses corrected fact"},
    )
    record_ref(store, consumer_ref, target_ref, "evidence_ref")
    materializer = CorrectionMaterializer(store)
    registration = materializer.register_correction(make_command(store, target_ref, idempotency_key="goal10-job"))
    impact = registration.impacts[0]

    enqueued = materializer.enqueue_impact_jobs(scheduler, registration.version_id, max_attempts=2)
    replayed_enqueue = materializer.enqueue_impact_jobs(scheduler, registration.version_id, max_attempts=2)
    assert len(enqueued) == 1
    assert replayed_enqueue[0].replayed
    job = scheduler.get_job(enqueued[0].job_id)
    assert job["job_kind"] == GOAL10_PROPAGATION_JOB_KIND
    first_claim = scheduler.claim_next(worker_id="goal10-worker", lease_seconds=1)
    try:
        materializer.process_impact(
            CorrectionPropagationCommand(
                actor="goal10-worker",
                idempotency_key=f"goal10.process.{impact.root_id}.{impact.expected_basis_hash}",
                impact_root_id=impact.root_id,
                expected_basis_hash=impact.expected_basis_hash,
                correlation_id=registration.version_id,
                causation_id=registration.version_id,
                inject_fault_after_processing=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - fixture checks scheduler recovery path.
        assert "injected fault" in str(exc)
    else:
        raise AssertionError("expected injected impact processing fault")
    next_status = scheduler.fail(
        attempt_id=first_claim.attempt_id,
        worker_id="goal10-worker",
        error={"code": "injected_processing_fault"},
        retry=True,
    )
    assert next_status == "queued"
    _processing_version, processing_payload = materializer._current_impact(impact.root_id)
    assert processing_payload["processing_status"] == "processing"

    second_claim = scheduler.claim_next(worker_id="goal10-worker", lease_seconds=1)
    recovered = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="goal10-worker",
            idempotency_key=f"goal10.process.{impact.root_id}.{impact.expected_basis_hash}",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=registration.version_id,
        )
    )
    scheduler.complete(
        attempt_id=second_claim.attempt_id,
        worker_id="goal10-worker",
        result={"impact_root_id": impact.root_id, "processing_status": recovered.processing_status},
    )
    assert recovered.processing_status == "completed"
    assert recovered.stop_reason == "replacement_version_created"
    assert scheduler.get_job(enqueued[0].job_id)["status"] == "succeeded"
    assert store.conn.execute(
        """
        SELECT count(*)
          FROM trace_version tv
          JOIN object_reference r ON r.source_version_id=tv.version_id
         WHERE tv.root_id=?
           AND tv.based_on_version_id=?
           AND r.relation_role='corrected_by_goal10_correction'
           AND r.target_version_id=?
        """,
        (consumer_ref.target_stable_id, consumer_ref.target_version_id, registration.version_id),
    ).fetchone()[0] == 1
    replay = materializer.process_impact(
        CorrectionPropagationCommand(
            actor="goal10-worker",
            idempotency_key=f"goal10.process.{impact.root_id}.{impact.expected_basis_hash}",
            impact_root_id=impact.root_id,
            expected_basis_hash=impact.expected_basis_hash,
            correlation_id=registration.version_id,
            causation_id=registration.version_id,
        )
    )
    assert replay.replayed
    assert replay.result["replacement_version_id"] == recovered.result["replacement_version_id"]
    print("PASS GOAL-10 job retry resumes processing impact without duplicate replacement")


def main() -> None:
    test_correction_record_is_immutable_and_preserves_original_history()
    test_identical_correction_replays_without_duplicate_side_effects()
    test_direct_dependency_index_uses_explicit_refs_only()
    test_impact_basis_key_is_stable_for_same_correction_target_and_action()
    test_propagation_converges_when_business_hash_and_refs_are_unchanged()
    test_propagation_creates_replacement_version_when_business_hash_changes()
    test_blocked_and_resume_recovery_have_audit_correlation_and_causation()
    test_job_retry_recovers_processing_impact_without_duplicate_replacement()
    print("GOAL-10 correction contract verification passed")


if __name__ == "__main__":
    main()
