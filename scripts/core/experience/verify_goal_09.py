from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.experience.goal09_experiments import (  # noqa: E402
    ExperienceEvidence,
    ExperienceStateInput,
    ExperimentMaterializer,
    ExperimentResultCommand,
    PPlusMetricInput,
    recompute_experience_state,
)
from scripts.core.persistence.goal01_store import (  # noqa: E402
    IdempotencyConflict,
    PersistenceStore,
    UUIDv7Generator,
    content_hash,
)
from scripts.core.production.goal08_production_chain import (  # noqa: E402
    PreferenceCandidateCommand,
    ProductionVersionChainMaterializer,
    VersionRef,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_780_000_000_000

    def now_ms(self) -> int:
        self.value += 1
        return self.value


def make_store() -> PersistenceStore:
    clock = FakeClock()
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=lambda bits: 99)
    return PersistenceStore.in_memory(id_factory=generator.new)


def payload_for(store: PersistenceStore, version_id: str) -> dict[str, object]:
    row = store.conn.execute("SELECT payload_json FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
    return json.loads(row["payload_json"])


def create_version_ref(
    store: PersistenceStore,
    object_kind: str,
    relation_role: str,
    payload: dict[str, object],
) -> VersionRef:
    root = store.create_root(object_kind)
    version = store.append_version(root, payload, projection_version=f"goal09.fixture_{object_kind}.v1")
    store.set_current_version(root, version)
    return VersionRef(
        relation_role=relation_role,
        target_object_kind=object_kind,
        target_stable_id=root,
        target_version_id=version,
        target_content_hash=content_hash(payload, f"goal09.fixture_{object_kind}.v1"),
        locator={"fixture": object_kind},
    )


def make_experiment_command(
    store: PersistenceStore,
    *,
    idempotency_key: str = "goal09-exp-1",
    experiment_kind: str = "formal",
    frozen: bool = True,
    actual_use_status: str = "used",
    major_confounder: bool = False,
    publication_relation: str = "same_as_approved",
    attribution_conflict: bool = False,
    baseline_value: int | None = 100,
    observed_value: int | None = 150,
) -> ExperimentResultCommand:
    tactic_ref = create_version_ref(
        store,
        "tactic_version",
        "primary_hypothesis",
        {"mechanism": "fixture mechanism", "usage_action": "fixture action"},
    )
    publication_ref = create_version_ref(
        store,
        "production_publication_capture",
        "measured_publication_capture",
        {"text": "published fixture script"},
    )
    return ExperimentResultCommand(
        account_id="account-9",
        topic_id="topic-9",
        actor="experiment-worker",
        idempotency_key=idempotency_key,
        experiment_kind=experiment_kind,
        primary_hypothesis_ref=tactic_ref,
        publication_capture_ref=publication_ref,
        metric=PPlusMetricInput(
            metric_name="views",
            baseline_value=baseline_value,
            observed_value=observed_value,
            support_ratio="1.2",
        ),
        primary_hypothesis_frozen=frozen,
        actual_use_status=actual_use_status,
        major_confounder=major_confounder,
        publication_relation=publication_relation,
        attribution_conflict=attribution_conflict,
    )


def test_formal_primary_used_p_plus_materializes_supported_signal() -> None:
    store = make_store()
    materializer = ExperimentMaterializer(store)
    result = materializer.record_experiment_result(make_experiment_command(store))

    payload = payload_for(store, result.version_id)
    assert payload["metric_signal"]["signal"] == "supported"
    assert payload["metric_signal"]["eligible"] is True
    assert payload["metric_signal"]["ratio"] == "1.5"
    assert payload["experiment_review_boundary"]["required"] is False
    refs = store.conn.execute(
        "SELECT relation_role, target_object_kind FROM object_reference WHERE source_version_id=?",
        (result.version_id,),
    ).fetchall()
    assert {(row["relation_role"], row["target_object_kind"]) for row in refs} == {
        ("primary_hypothesis", "tactic_version"),
        ("measured_publication_capture", "production_publication_capture"),
    }
    assert store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope='goal09.experiment_result'"
    ).fetchone()[0] == 1
    print("PASS GOAL-09 formal primary-used P+ materializes supported metric signal")


def test_ineligible_when_primary_not_used_even_with_high_p_plus() -> None:
    store = make_store()
    profile_id = store.create_preference_profile("domain", "domain-9")
    production = ProductionVersionChainMaterializer(store)
    edit_ref = create_version_ref(
        store,
        "production_manual_edit",
        "derived_from_manual_edit",
        {"from": "draft", "to": "human edited"},
    )
    preference = production.record_preference_candidate(
        PreferenceCandidateCommand(
            profile_id=profile_id,
            actor="editor",
            idempotency_key="goal09-pref-candidate",
            preference_payload={"voice": "fixture candidate only"},
            evidence_refs=(edit_ref,),
            origin="manual_edit",
        )
    )
    result = ExperimentMaterializer(store).record_experiment_result(
        make_experiment_command(
            store,
            idempotency_key="goal09-exp-not-used",
            actual_use_status="not_used",
            baseline_value=100,
            observed_value=10000,
        )
    )

    assert result.metric_signal.signal == "ineligible"
    assert result.metric_signal.eligible is False
    assert "not actually used" in result.metric_signal.reason
    current = store.conn.execute(
        "SELECT current_revision_id FROM content_preference_profile WHERE profile_id=?",
        (profile_id,),
    ).fetchone()["current_revision_id"]
    assert current is None
    pref_row = store.conn.execute(
        "SELECT status FROM content_preference_revision WHERE revision_id=?",
        (preference.revision_id,),
    ).fetchone()
    assert pref_row["status"] == "candidate"
    print("PASS GOAL-09 P+ success cannot override unused primary hypothesis or publish preference")


def test_metric_signal_replay_and_conflict_gate() -> None:
    store = make_store()
    materializer = ExperimentMaterializer(store)
    command = make_experiment_command(store, idempotency_key="goal09-exp-replay")
    first = materializer.record_experiment_result(command)
    replay = materializer.record_experiment_result(command)

    assert replay.replayed
    assert replay.version_id == first.version_id
    assert store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE idempotency_key='goal09-exp-replay'"
    ).fetchone()[0] == 1

    conflict = make_experiment_command(
        store,
        idempotency_key="goal09-exp-replay",
        observed_value=99,
    )
    try:
        materializer.record_experiment_result(conflict)
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("expected changed idempotency payload rejection")
    print("PASS GOAL-09 replay returns original result and changed payload conflicts")


def test_inconclusive_deterministic_inputs_do_not_call_review_or_model() -> None:
    store = make_store()
    result = ExperimentMaterializer(store).record_experiment_result(
        make_experiment_command(
            store,
            idempotency_key="goal09-exp-baseline-zero",
            baseline_value=0,
            observed_value=500,
        )
    )

    assert result.metric_signal.signal == "inconclusive"
    assert result.metric_signal.eligible is True
    assert "baseline" in result.metric_signal.reason
    assert result.review_boundary.required is False
    assert result.review_boundary.blocked_by_deterministic_invalidity is True
    assert store.conn.execute(
        "SELECT count(*) FROM trace_root WHERE object_kind='model_run_envelope'"
    ).fetchone()[0] == 0
    print("PASS GOAL-09 deterministic invalid P+ input stays inconclusive without model/review")


def test_experiment_review_boundary_requires_review_only_for_ambiguous_attribution() -> None:
    store = make_store()
    result = ExperimentMaterializer(store).record_experiment_result(
        make_experiment_command(
            store,
            idempotency_key="goal09-exp-review-needed",
            actual_use_status="unknown",
            publication_relation="modified_text_provided",
            attribution_conflict=True,
            baseline_value=100,
            observed_value=300,
        )
    )

    payload = payload_for(store, result.version_id)
    boundary = payload["experiment_review_boundary"]
    assert result.metric_signal.signal == "inconclusive"
    assert result.review_boundary.required is True
    assert boundary["required"] is True
    assert set(boundary["reasons"]) == {
        "publication_relation:modified_text_provided",
        "actual_use_status:unknown",
        "attribution_conflict",
    }
    assert store.conn.execute(
        "SELECT count(*) FROM trace_root WHERE object_kind='model_run_envelope'"
    ).fetchone()[0] == 0
    print("PASS GOAL-09 experiment_review boundary triggers only for ambiguous attribution")


def test_major_confounder_requires_review_without_overriding_metric_signal() -> None:
    store = make_store()
    result = ExperimentMaterializer(store).record_experiment_result(
        make_experiment_command(
            store,
            idempotency_key="goal09-exp-major-confounder",
            major_confounder=True,
            baseline_value=100,
            observed_value=400,
        )
    )

    assert result.metric_signal.signal == "inconclusive"
    assert result.metric_signal.reason == "major confounder recorded"
    assert result.review_boundary.required is True
    assert result.review_boundary.reasons == ("major_confounder",)
    print("PASS GOAL-09 experiment_review boundary does not override deterministic metric signal")


def test_cr002_recompute_maturity_status_and_repeated_failure_trigger() -> None:
    result = recompute_experience_state(
        ExperienceStateInput(
            tactic_key="tactic-9",
            current_recommendation_status="active",
            evidence=(
                ExperienceEvidence(
                    evidence_id="support-1",
                    evidence_kind="formal_p_result",
                    independence_key="topic-a",
                    metric_signal="supported",
                    primary_used=True,
                    core_question_hash="q-a",
                ),
                ExperienceEvidence(
                    evidence_id="support-2",
                    evidence_kind="formal_p_result",
                    independence_key="topic-b",
                    metric_signal="supported",
                    primary_used=True,
                    core_question_hash="q-b",
                ),
                ExperienceEvidence(
                    evidence_id="support-3",
                    evidence_kind="formal_p_result",
                    independence_key="topic-c",
                    metric_signal="supported",
                    primary_used=True,
                    core_question_hash="q-c",
                ),
                ExperienceEvidence(
                    evidence_id="failure-1",
                    evidence_kind="formal_p_result",
                    independence_key="topic-d",
                    metric_signal="not_supported",
                    primary_used=True,
                    core_question_hash="q-d",
                ),
                ExperienceEvidence(
                    evidence_id="failure-2",
                    evidence_kind="formal_p_result",
                    independence_key="topic-e",
                    metric_signal="not_supported",
                    primary_used=True,
                    core_question_hash="q-e",
                ),
                ExperienceEvidence(
                    evidence_id="failure-3",
                    evidence_kind="formal_p_result",
                    independence_key="topic-f",
                    metric_signal="not_supported",
                    primary_used=True,
                    core_question_hash="q-f",
                ),
            ),
        )
    )

    assert result.maturity_level == "L5"
    assert result.recommendation_status == "paused"
    assert result.formal_supports == 3
    assert result.formal_failures == 3
    assert len(result.proposal_triggers) == 1
    trigger = result.proposal_triggers[0]
    assert trigger.trigger_kind == "repeated_formal_failures"
    assert trigger.allowed_proposal_types == ("revise", "split", "deprecate", "no_proposal")
    print("PASS GOAL-09 CR-002 recomputes maturity/status and repeated failure trigger")


def test_cr002_external_structural_and_human_triggers_do_not_publish_proposals() -> None:
    result = recompute_experience_state(
        ExperienceStateInput(
            tactic_key="tactic-10",
            current_recommendation_status="active",
            evidence=(
                ExperienceEvidence(
                    evidence_id="external-1",
                    evidence_kind="external_counterexample",
                    independence_key="source-a",
                ),
                ExperienceEvidence(
                    evidence_id="external-2",
                    evidence_kind="external_counterexample",
                    independence_key="source-b",
                ),
                ExperienceEvidence(
                    evidence_id="external-duplicate",
                    evidence_kind="external_counterexample",
                    independence_key="source-b",
                ),
                ExperienceEvidence(
                    evidence_id="structural-1",
                    evidence_kind="structural_revision_signal",
                    independence_key="signal-a",
                ),
                ExperienceEvidence(
                    evidence_id="human-1",
                    evidence_kind="human_revision_request",
                    independence_key="request-a",
                    requested_action="restore",
                ),
            ),
        )
    )

    assert result.recommendation_status == "watch"
    assert result.independent_external_counterexamples == 2
    triggers = {trigger.trigger_kind: trigger for trigger in result.proposal_triggers}
    assert set(triggers) == {
        "independent_external_counterexamples",
        "structural_revision_signal",
        "human_requested_revision",
    }
    assert triggers["independent_external_counterexamples"].evidence_ids == ("external-1", "external-2")
    assert "restore" in triggers["human_requested_revision"].allowed_proposal_types
    assert all("published" not in trigger.as_payload() for trigger in result.proposal_triggers)
    print("PASS GOAL-09 CR-002 detects proposal triggers without publishing proposals")


def test_cr002_manual_lock_and_deprecated_are_not_silently_overridden() -> None:
    locked = recompute_experience_state(
        ExperienceStateInput(
            tactic_key="tactic-11",
            current_recommendation_status="active",
            manual_lock=True,
            evidence=(
                ExperienceEvidence(
                    evidence_id="failure-locked",
                    evidence_kind="formal_p_result",
                    independence_key="topic-locked",
                    metric_signal="not_supported",
                    primary_used=True,
                    core_question_hash="q-locked",
                ),
            ),
        )
    )
    deprecated = recompute_experience_state(
        ExperienceStateInput(
            tactic_key="tactic-12",
            current_recommendation_status="deprecated",
            evidence=(
                ExperienceEvidence(
                    evidence_id="support-late",
                    evidence_kind="formal_p_result",
                    independence_key="topic-late",
                    metric_signal="supported",
                    primary_used=True,
                    core_question_hash="q-late",
                ),
            ),
        )
    )

    assert locked.recommendation_status == "active"
    assert "manual_lock" in locked.audit_reasons[0]
    assert deprecated.recommendation_status == "deprecated"
    assert "restore proposal" in deprecated.audit_reasons[0]
    print("PASS GOAL-09 CR-002 respects manual lock and deprecated proposal boundary")


def main() -> None:
    test_formal_primary_used_p_plus_materializes_supported_signal()
    test_ineligible_when_primary_not_used_even_with_high_p_plus()
    test_metric_signal_replay_and_conflict_gate()
    test_inconclusive_deterministic_inputs_do_not_call_review_or_model()
    test_experiment_review_boundary_requires_review_only_for_ambiguous_attribution()
    test_major_confounder_requires_review_without_overriding_metric_signal()
    test_cr002_recompute_maturity_status_and_repeated_failure_trigger()
    test_cr002_external_structural_and_human_triggers_do_not_publish_proposals()
    test_cr002_manual_lock_and_deprecated_are_not_silently_overridden()
    print("GOAL-09 experiment metric verification passed")


if __name__ == "__main__":
    main()
