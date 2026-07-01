from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.experience.goal09_experiments import (  # noqa: E402
    ExperienceEvidence,
    ExperienceRevisionProposalOutput,
    ExperienceRevisionProposalPublishCommand,
    ExperienceStateInput,
    ExperimentMaterializer,
    ExperimentResultCommand,
    PPlusMetricInput,
    ProposalTrigger,
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


def make_proposal_trigger() -> ProposalTrigger:
    return ProposalTrigger(
        trigger_kind="repeated_formal_failures",
        allowed_proposal_types=("revise", "split", "deprecate", "no_proposal"),
        evidence_ids=("failure-1", "failure-2", "failure-3"),
        reason="fixture repeated formal failures",
    )


def make_proposal_output(**overrides: object) -> ExperienceRevisionProposalOutput:
    payload = {
        "proposal_type": "revise",
        "trigger_kind": "repeated_formal_failures",
        "proposed_experiences": (
            {
                "mechanism": "revised fixture mechanism",
                "usage_action": "use only under narrower condition",
                "applicable_conditions": ["condition-a"],
                "failure_conditions": ["failure-a"],
            },
        ),
        "evidence_mapping": {"revision_basis": ["failure-1", "failure-2"]},
        "rationale": "fixture proposal rationale",
    }
    payload.update(overrides)
    return ExperienceRevisionProposalOutput(**payload)


def make_proposal_publish_command(
    store: PersistenceStore,
    *,
    idempotency_key: str = "goal09-proposal-publish",
    output: ExperienceRevisionProposalOutput | None = None,
) -> tuple[ExperienceRevisionProposalPublishCommand, VersionRef]:
    base_ref = create_version_ref(
        store,
        "tactic_version",
        "base_tactic_version",
        {"mechanism": "base fixture mechanism", "usage_action": "base action"},
    )
    evidence_refs = tuple(
        create_version_ref(
            store,
            "goal09_experiment_result",
            "proposal_trigger_evidence",
            {"evidence_id": evidence_id, "signal": "not_supported"},
        )
        for evidence_id in ("failure-1", "failure-2", "failure-3")
    )
    skill_run_ref = create_version_ref(
        store,
        "model_run_envelope",
        "proposed_by_skill_run",
        {"skill_name": "experience_revision_propose", "status": "succeeded"},
    )
    return (
        ExperienceRevisionProposalPublishCommand(
            actor="experience-curator",
            idempotency_key=idempotency_key,
            output=output or make_proposal_output(),
            trigger=make_proposal_trigger(),
            base_version_refs=(base_ref,),
            evidence_refs=evidence_refs,
            skill_run_ref=skill_run_ref,
        ),
        base_ref,
    )


def test_experience_revision_proposal_output_gate_and_publication_path() -> None:
    store = make_store()
    command, _base_ref = make_proposal_publish_command(store)
    materializer = ExperimentMaterializer(store)
    result = materializer.publish_experience_revision_proposal(command)
    replay = materializer.publish_experience_revision_proposal(command)

    assert result.version_id
    assert result.proposal_hash == replay.proposal_hash
    assert replay.replayed
    payload = payload_for(store, result.version_id)
    assert payload["status"] == "published"
    assert payload["output"]["proposal_type"] == "revise"
    assert payload["trigger"]["trigger_kind"] == "repeated_formal_failures"
    refs = store.conn.execute(
        "SELECT relation_role FROM object_reference WHERE source_version_id=?",
        (result.version_id,),
    ).fetchall()
    assert {row["relation_role"] for row in refs} == {
        "base_tactic_version",
        "proposal_trigger_evidence",
        "proposed_by_skill_run",
    }
    assert store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope='goal09.experience_revision_proposal.publish'"
    ).fetchone()[0] == 1
    print("PASS GOAL-09 proposal output gate publishes one immutable proposal with refs")


def test_experience_revision_proposal_rejects_host_fields_and_no_proposal_publication() -> None:
    store = make_store()
    leaked_output = make_proposal_output(
        proposed_experiences=(
            {
                "mechanism": "bad",
                "usage_action": "bad",
                "applicable_conditions": ["bad"],
                "failure_conditions": ["bad"],
                "tactic_id": "host-tactic-id",
            },
        )
    )
    leaked_command, _base_ref = make_proposal_publish_command(
        store,
        idempotency_key="goal09-proposal-leak",
        output=leaked_output,
    )
    no_proposal_command, _base_ref2 = make_proposal_publish_command(
        store,
        idempotency_key="goal09-no-proposal",
        output=make_proposal_output(proposal_type="no_proposal"),
    )

    for command, expected in (
        (leaked_command, "forbidden host field"),
        (no_proposal_command, "cannot be published"),
    ):
        try:
            ExperimentMaterializer(store).publish_experience_revision_proposal(command)
        except Exception as exc:  # noqa: BLE001 - exact boundary exception text is asserted.
            assert expected in str(exc)
        else:
            raise AssertionError("expected proposal gate rejection")
    print("PASS GOAL-09 proposal output gate rejects host fields and no_proposal publication")


def test_experience_revision_proposal_rejects_stale_base_and_duplicate_hash() -> None:
    store = make_store()
    materializer = ExperimentMaterializer(store)
    first_command, _base_ref = make_proposal_publish_command(store, idempotency_key="goal09-proposal-first")
    first = materializer.publish_experience_revision_proposal(first_command)
    duplicate_command, _duplicate_base_ref = make_proposal_publish_command(
        store,
        idempotency_key="goal09-proposal-duplicate-hash",
        output=first_command.output,
    )
    try:
        materializer.publish_experience_revision_proposal(duplicate_command)
    except Exception as exc:  # noqa: BLE001 - exact boundary exception text is asserted.
        assert "proposal_hash was already published" in str(exc)
    else:
        raise AssertionError("expected duplicate proposal hash rejection")

    stale_store = make_store()
    stale_command, stale_base_ref = make_proposal_publish_command(
        stale_store,
        idempotency_key="goal09-proposal-stale-base",
    )
    stale_store.append_version(
        stale_base_ref.target_stable_id,
        {"mechanism": "new current", "usage_action": "new action"},
        projection_version="goal09.fixture_tactic_version.v1",
    )
    new_current = stale_store.conn.execute(
        "SELECT version_id FROM trace_version WHERE root_id=? ORDER BY version_no DESC LIMIT 1",
        (stale_base_ref.target_stable_id,),
    ).fetchone()["version_id"]
    stale_store.set_current_version(stale_base_ref.target_stable_id, new_current)
    try:
        ExperimentMaterializer(stale_store).publish_experience_revision_proposal(stale_command)
    except Exception as exc:  # noqa: BLE001 - exact boundary exception text is asserted.
        assert "base version is no longer current" in str(exc)
    else:
        raise AssertionError("expected stale base rejection")
    assert first.version_id
    print("PASS GOAL-09 proposal publication rejects duplicate hash and stale base")


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
    test_experience_revision_proposal_output_gate_and_publication_path()
    test_experience_revision_proposal_rejects_host_fields_and_no_proposal_publication()
    test_experience_revision_proposal_rejects_stale_base_and_duplicate_hash()
    print("GOAL-09 experiment metric verification passed")


if __name__ == "__main__":
    main()
