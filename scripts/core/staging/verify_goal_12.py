from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.correction.goal10_corrections import (  # noqa: E402
    GOAL10_PROPAGATION_JOB_KIND,
    CorrectionMaterializer,
    CorrectionPropagationCommand,
    CorrectionRegistrationCommand,
    CorrectionReportCommand,
)
from scripts.core.experience.goal09_experiments import (  # noqa: E402
    ExperimentMaterializer,
    ExperimentResultCommand,
    ExperienceEvidence,
    ExperienceRevisionProposalOutput,
    ExperienceRevisionProposalPublishCommand,
    ExperienceStateInput,
    InferredPreferenceCandidateCommand,
    PPlusMetricInput,
    ProposalTrigger,
    recompute_experience_state,
)
from scripts.core.hermes.goal11_host_binding import (  # noqa: E402
    GOAL11_RESPONSE_SEND_SCOPE,
    GOAL11_RESPONSE_TOPIC,
    FeishuBindingEvent,
    FeishuResponseDispatcher,
    FeishuThinBinding,
    HermesCoreBridge,
)
from scripts.core.host.production_host import PRODUCTION_HOST_ACTOR  # noqa: E402
from scripts.core.model_gateway.goal07_model_gateway import (  # noqa: E402
    ModelGateway,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.goal07_skill_runner import (  # noqa: E402
    HostBindingSpec,
    PortableSkillRunner,
    PortableSkillSpec,
)
from scripts.core.persistence.goal01_store import (  # noqa: E402
    PersistenceStore,
    UUIDv7Generator,
    content_hash,
)
from scripts.core.production.goal08_production_chain import (  # noqa: E402
    PreferenceCandidateCommand,
    ProductionArtifactCommand,
    ProductionVersionChainMaterializer,
    VersionRef,
)
from scripts.core.research.goal06_formal_research import (  # noqa: E402
    ExtractedEvidence,
    FetchedDocument,
    FormalResearchMaterializer,
    FormalResearchService,
    ResearchQuery,
    SearchResult,
    make_formal_research_runtime_handler,
    start_topic_first_research_workflow,
)
from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost  # noqa: E402
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler  # noqa: E402
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer  # noqa: E402
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator  # noqa: E402


class FakeClock:
    def __init__(self, start_ms: int = 1_790_000_000_000) -> None:
        self.value = start_ms

    def now_ms(self) -> int:
        self.value += 1
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value


class DeterministicBits:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, bit_count: int) -> int:
        self.value += 1
        return self.value & ((1 << bit_count) - 1)


class FakeSearchProvider:
    provider_name = "goal12-fake-search"

    def search(self, query: ResearchQuery) -> list[SearchResult]:
        return [
            SearchResult(
                result_id=f"{query.topic_id}-source-1",
                title="Staging source one",
                url="https://example.invalid/staging-source-one",
                provider=self.provider_name,
                snippet=f"Evidence for {query.query}",
            ),
            SearchResult(
                result_id=f"{query.topic_id}-source-2",
                title="Staging source two",
                url="https://example.invalid/staging-source-two",
                provider=self.provider_name,
                snippet=f"Second source for {query.query}",
            ),
        ]


class FakeFetcher:
    fetcher_name = "goal12-fake-fetcher"

    def fetch(self, result: SearchResult) -> FetchedDocument:
        return FetchedDocument(
            result_id=result.result_id,
            url=result.url,
            title=result.title,
            text=f"{result.title}: {result.snippet}. This is isolated staging fixture evidence.",
            fetched_at="2026-07-01T00:00:00Z",
            fetcher=self.fetcher_name,
        )


class FakeExtractor:
    extractor_name = "goal12-fake-extractor"

    def extract(self, document: FetchedDocument) -> list[ExtractedEvidence]:
        return [
            ExtractedEvidence(
                evidence_id=f"{document.result_id}-evidence",
                result_id=document.result_id,
                claim=f"Claim from {document.title}",
                quote=document.text,
                locator={"url": document.url, "title": document.title},
                extractor=self.extractor_name,
            )
        ]


class FakeModelProvider:
    provider_name = "goal12-fake-model"

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=f"staging script for {request.input_payload['topic_id']} with {request.input_payload['claim']}",
            usage=ModelUsage(prompt_tokens=17, completion_tokens=11, total_tokens=28),
            cost={"currency": "USD", "amount": "0"},
            provider_request_id="goal12-fake-provider-request",
            metadata={"staging": True, "external_io": False},
        )


@dataclass(frozen=True)
class StagingArtifacts:
    topic_id: str
    topic_version_id: str
    research_artifact_version_id: str
    evidence_ref: VersionRef
    model_run_root_id: str
    model_run_version_id: str
    script_root_id: str
    script_version_id: str
    approved_draft_root_id: str
    approved_draft_version_id: str
    publication_root_id: str
    publication_version_id: str
    manual_edit_root_id: str
    manual_edit_version_id: str
    experiment_version_id: str
    proposal_version_id: str
    inferred_preference_revision_id: str
    correction_version_id: str
    correction_report_version_id: str


class Goal12StagingHarness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        generator = UUIDv7Generator(now_ms=self.clock.now_ms, randbits=DeterministicBits())
        self.core = CoreMaterializer.in_memory(id_factory=generator.new)
        self.store = self.core.store
        self.scheduler = Goal03Scheduler(self.store, id_factory=generator.new, now_ms=self.clock.now_ms)
        self.core.grant_permission(PRODUCTION_HOST_ACTOR, "create_state")
        self.core.grant_permission(PRODUCTION_HOST_ACTOR, "transition_state")

    def make_runtime(self, response_dispatcher: FeishuResponseDispatcher | None = None) -> RuntimeHost:
        runtime = RuntimeHost(self.scheduler, worker_id="goal12-worker")
        runtime.register_handler(
            "outbox.dispatch",
            (response_dispatcher or FeishuResponseDispatcher(self.store)).handler(),
            contract=RuntimeHandlerContract(
                job_kind="outbox.dispatch",
                required_payload_keys=("outbox_id", "topic", "payload"),
                required_result_keys=("outbox_id", "reply_channel_id", "receipt_id"),
            ),
        )
        research_service = FormalResearchService(
            provider=FakeSearchProvider(),
            fetcher=FakeFetcher(),
            extractor=FakeExtractor(),
            materializer=FormalResearchMaterializer(self.store),
        )
        runtime.register_handler(
            "research.formal.topic_first",
            make_formal_research_runtime_handler(research_service),
            contract=RuntimeHandlerContract(
                job_kind="research.formal.topic_first",
                required_payload_keys=("topic_id", "query"),
                required_result_keys=("plan_version_id", "artifact_version_id", "source_count", "evidence_count"),
            ),
        )
        runtime.register_handler(
            GOAL10_PROPAGATION_JOB_KIND,
            CorrectionMaterializer(self.store).propagation_job_handler(actor="goal12-correction-worker"),
            contract=RuntimeHandlerContract(
                job_kind=GOAL10_PROPAGATION_JOB_KIND,
                required_payload_keys=("impact_root_id", "expected_basis_hash"),
                required_result_keys=("impact_root_id", "processing_status"),
            ),
        )
        return runtime


def payload_for(store: PersistenceStore, version_id: str) -> dict[str, Any]:
    row = store.conn.execute("SELECT payload_json FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
    if row is None:
        raise AssertionError(f"missing version: {version_id}")
    return json.loads(row["payload_json"])


def root_for(store: PersistenceStore, version_id: str) -> str:
    return str(
        store.conn.execute("SELECT root_id FROM trace_version WHERE version_id=?", (version_id,)).fetchone()["root_id"]
    )


def trace_count(store: PersistenceStore, object_kind: str) -> int:
    return int(
        store.conn.execute("SELECT count(*) FROM trace_root WHERE object_kind=?", (object_kind,)).fetchone()[0]
    )


def latest_version_ref(
    store: PersistenceStore,
    *,
    version_id: str,
    relation_role: str,
    target_object_kind: str | None = None,
) -> VersionRef:
    root_id = root_for(store, version_id)
    payload = payload_for(store, version_id)
    projection = store.conn.execute("SELECT projection_version FROM trace_version WHERE version_id=?", (version_id,)).fetchone()[
        "projection_version"
    ]
    return VersionRef(
        relation_role=relation_role,
        target_object_kind=target_object_kind or str(payload.get("artifact_kind", "trace_version")),
        target_stable_id=root_id,
        target_version_id=version_id,
        target_content_hash=content_hash(payload, projection),
        locator={"version_id": version_id},
    )


def run_end_to_end_staging() -> tuple[Goal12StagingHarness, StagingArtifacts]:
    harness = Goal12StagingHarness()
    bridge = HermesCoreBridge(harness.core)
    binding = FeishuThinBinding()
    create_result = bridge.dispatch(
        binding.to_hermes_message(
            FeishuBindingEvent(
                event_id="goal12-topic-create",
                chat_id="goal12-chat",
                sender_id="goal12-user",
                command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
            )
        )
    )
    assert create_result.status == "succeeded"
    assert create_result.object_id is not None
    assert create_result.basis_version_id is not None
    transition_result = bridge.dispatch(
        binding.to_hermes_message(
            FeishuBindingEvent(
                event_id="goal12-topic-select",
                chat_id="goal12-chat",
                sender_id="goal12-user",
                command={
                    "command_type": "transition_state",
                    "object_kind": "topic",
                    "object_id": create_result.object_id,
                    "expected_basis_version_id": create_result.basis_version_id,
                    "payload": {"to_state": "selected"},
                },
            )
        )
    )
    assert transition_result.status == "succeeded"

    bridge.enqueue_response_jobs(harness.scheduler)
    response_runtime = harness.make_runtime()
    response_results = response_runtime.run_batch(max_jobs=8)
    assert response_results and all(result.status == "succeeded" for result in response_results)

    research = start_topic_first_research_workflow(
        orchestrator=Goal05WorkflowOrchestrator(harness.scheduler),
        topic_id=create_result.object_id,
        query="staging topic evidence",
        idempotency_key="goal12-research-workflow",
    )
    assert len(research.job_ids) == 1
    runtime = harness.make_runtime()
    research_run = runtime.run_once()
    assert research_run.status == "succeeded"
    research_artifact = harness.store.conn.execute(
        """
        SELECT tv.version_id, tv.payload_json
          FROM trace_version tv
          JOIN trace_root tr ON tr.root_id=tv.root_id
         WHERE tr.object_kind='research_artifact'
         ORDER BY tv.version_no DESC, tv.version_id DESC
         LIMIT 1
        """
    ).fetchone()
    assert research_artifact is not None
    research_payload = json.loads(research_artifact["payload_json"])
    evidence_version_id = research_payload["evidence_version_ids"][0]
    evidence_root_id = root_for(harness.store, evidence_version_id)
    evidence_ref = VersionRef(
        relation_role="uses_evidence",
        target_object_kind="research_evidence",
        target_stable_id=evidence_root_id,
        target_version_id=evidence_version_id,
        locator={"topic_id": create_result.object_id},
    )

    model_run_root, model_run_version = run_script_skill(harness.store, create_result.object_id)
    production = ProductionVersionChainMaterializer(harness.store)
    plan = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="content_plan",
            topic_id=create_result.object_id,
            actor="goal12-planner",
            idempotency_key="goal12-content-plan",
            content_payload={"claim": "Claim from Staging source one", "outline": "A deterministic staging plan"},
            evidence_refs=(evidence_ref,),
        )
    )
    script = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="script",
            topic_id=create_result.object_id,
            actor="goal12-writer",
            idempotency_key="goal12-script-v1",
            content_payload={"claim": "Claim from Staging source one", "text": "staging script draft"},
            evidence_refs=(
                VersionRef(
                    relation_role="input_assembly_included_ref",
                    target_object_kind="research_evidence",
                    target_stable_id=evidence_root_id,
                    target_version_id=evidence_version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
            model_run_root_id=model_run_root,
            model_run_envelope_version_id=model_run_version,
        )
    )
    review = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="review",
            topic_id=create_result.object_id,
            actor="goal12-reviewer",
            idempotency_key="goal12-review-v1",
            content_payload={"decision": "pass"},
            evidence_refs=(
                VersionRef(
                    relation_role="reviews_script_version",
                    target_object_kind="production_script",
                    target_stable_id=script.root_id,
                    target_version_id=script.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )
    approval = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="approval",
            topic_id=create_result.object_id,
            actor="goal12-editor",
            idempotency_key="goal12-approval-v1",
            content_payload={"approved": True, "review_version_id": review.version_id},
            evidence_refs=(
                VersionRef(
                    relation_role="approves_script_version",
                    target_object_kind="production_script",
                    target_stable_id=script.root_id,
                    target_version_id=script.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )
    approved_draft = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="approved_draft",
            topic_id=create_result.object_id,
            actor="goal12-editor",
            idempotency_key="goal12-approved-draft-v1",
            content_payload={"text": "approved staging script"},
            evidence_refs=(
                VersionRef(
                    relation_role="approved_by",
                    target_object_kind="production_approval",
                    target_stable_id=approval.root_id,
                    target_version_id=approval.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )
    publication = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="publication_capture",
            topic_id=create_result.object_id,
            actor="goal12-publisher",
            idempotency_key="goal12-publication-capture-v1",
            content_payload={"platform": "fixture", "text": "published staging script"},
            evidence_refs=(
                VersionRef(
                    relation_role="published_from_approved_draft",
                    target_object_kind="production_approved_draft",
                    target_stable_id=approved_draft.root_id,
                    target_version_id=approved_draft.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )
    manual_edit = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="manual_edit",
            topic_id=create_result.object_id,
            actor="goal12-human-editor",
            idempotency_key="goal12-manual-edit-v1",
            content_payload={"from": "approved staging script", "to": "published staging script"},
            evidence_refs=(
                VersionRef(
                    relation_role="edits_approved_draft",
                    target_object_kind="production_approved_draft",
                    target_stable_id=approved_draft.root_id,
                    target_version_id=approved_draft.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )
    profile_id = harness.store.create_preference_profile("account", "goal12-account")
    preference = production.record_preference_candidate(
        PreferenceCandidateCommand(
            profile_id=profile_id,
            actor="goal12-editor",
            idempotency_key="goal12-preference-candidate",
            preference_payload={"voice": "candidate only from manual edit"},
            origin="manual_edit",
            evidence_refs=(
                VersionRef(
                    relation_role="derived_from_manual_edit",
                    target_object_kind="production_manual_edit",
                    target_stable_id=manual_edit.root_id,
                    target_version_id=manual_edit.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
        )
    )

    experiment_materializer = ExperimentMaterializer(harness.store)
    experiment = experiment_materializer.record_experiment_result(
        ExperimentResultCommand(
            account_id="goal12-account",
            topic_id=create_result.object_id,
            actor="goal12-experiment-worker",
            idempotency_key="goal12-experiment-result",
            experiment_kind="formal",
            primary_hypothesis_ref=VersionRef(
                relation_role="primary_hypothesis",
                target_object_kind="production_content_plan",
                target_stable_id=plan.root_id,
                target_version_id=plan.version_id,
                locator={"topic_id": create_result.object_id},
            ),
            publication_capture_ref=VersionRef(
                relation_role="measured_publication_capture",
                target_object_kind="production_publication_capture",
                target_stable_id=publication.root_id,
                target_version_id=publication.version_id,
                locator={"topic_id": create_result.object_id},
            ),
            metric=PPlusMetricInput(metric_name="views", baseline_value=100, observed_value=151, support_ratio="1.2"),
            primary_hypothesis_frozen=True,
            actual_use_status="used",
        )
    )
    state = recompute_experience_state(
        ExperienceStateInput(
            tactic_key="goal12-tactic",
            current_recommendation_status="active",
            evidence=(
                ExperienceEvidence(
                    evidence_id="goal12-pplus-supported",
                    evidence_kind="formal_p_result",
                    independence_key=create_result.object_id,
                    metric_signal=experiment.metric_signal.signal,
                    primary_used=True,
                    core_question_hash="goal12-question",
                ),
            ),
        )
    )
    assert state.recommendation_status == "active"

    gate_refs = tuple(
        create_fixture_ref(harness.store, "goal12_validation_gate", f"passed_{gate}_gate", {"gate": gate, "passed": True})
        for gate in ("regression", "ablation", "compatibility", "provenance")
    )
    proposal = experiment_materializer.publish_experience_revision_proposal(
        ExperienceRevisionProposalPublishCommand(
            actor="goal12-curator",
            idempotency_key="goal12-experience-proposal",
            output=ExperienceRevisionProposalOutput(
                proposal_type="revise",
                trigger_kind="human_requested_revision",
                proposed_experiences=(
                    {
                        "mechanism": "staging mechanism",
                        "usage_action": "use when evidence supports the topic",
                        "applicable_conditions": ["fixture only"],
                        "failure_conditions": ["missing evidence"],
                    },
                ),
                evidence_mapping={"manual": ["goal12-human-request"]},
                rationale="staging proposal with all gates passed",
            ),
            trigger=ProposalTrigger(
                trigger_kind="human_requested_revision",
                allowed_proposal_types=("revise", "split", "merge", "deprecate", "restore", "no_proposal"),
                evidence_ids=("goal12-human-request",),
                reason="fixture request",
            ),
            base_version_refs=(
                VersionRef(
                    relation_role="base_tactic_version",
                    target_object_kind="production_content_plan",
                    target_stable_id=plan.root_id,
                    target_version_id=plan.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
            evidence_refs=(
                VersionRef(
                    relation_role="proposal_trigger_evidence",
                    target_object_kind="goal09_experiment_result",
                    target_stable_id=experiment.root_id,
                    target_version_id=experiment.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
            validation_gate_refs=gate_refs,
            validation_gate_results={"regression": True, "ablation": True, "compatibility": True, "provenance": True},
            skill_run_ref=VersionRef(
                relation_role="proposed_by_skill_run",
                target_object_kind="model_run_envelope",
                target_stable_id=model_run_root,
                target_version_id=model_run_version,
                locator={"topic_id": create_result.object_id},
            ),
        )
    )
    inferred = experiment_materializer.record_inferred_preference_candidate(
        InferredPreferenceCandidateCommand(
            profile_id=profile_id,
            actor="goal12-preference-curator",
            idempotency_key="goal12-inferred-preference-candidate",
            preference_payload={"style_signal": "candidate only"},
            evidence_refs=(
                VersionRef(
                    relation_role="inferred_from_publication_capture",
                    target_object_kind="production_publication_capture",
                    target_stable_id=publication.root_id,
                    target_version_id=publication.version_id,
                    locator={"topic_id": create_result.object_id},
                ),
            ),
            inference_basis="fixture repeated evidence",
        )
    )
    correction = CorrectionMaterializer(harness.store).register_correction(
        CorrectionRegistrationCommand(
            actor="goal12-correction-worker",
            idempotency_key="goal12-correction-register",
            target_ref=VersionRef(
                relation_role="corrects_research_evidence",
                target_object_kind="research_evidence",
                target_stable_id=evidence_root_id,
                target_version_id=evidence_version_id,
                locator={"topic_id": create_result.object_id},
            ),
            corrected_payload={"claim": "corrected staging claim"},
            change_scope="source_fact_value",
            reason="staging correction propagation",
        )
    )
    correction_materializer = CorrectionMaterializer(harness.store)
    correction_materializer.enqueue_impact_jobs(harness.scheduler, correction.version_id, max_attempts=3)
    correction_runtime = harness.make_runtime()
    propagation_results = correction_runtime.run_batch(max_jobs=20)
    assert propagation_results
    report = correction_materializer.create_report(
        CorrectionReportCommand(
            actor="goal12-report-worker",
            idempotency_key="goal12-correction-report",
            correction_version_id=correction.version_id,
            correlation_id=correction.version_id,
            causation_id=correction.version_id,
        )
    )

    artifacts = StagingArtifacts(
        topic_id=create_result.object_id,
        topic_version_id=transition_result.basis_version_id or create_result.basis_version_id,
        research_artifact_version_id=str(research_artifact["version_id"]),
        evidence_ref=evidence_ref,
        model_run_root_id=model_run_root,
        model_run_version_id=model_run_version,
        script_root_id=script.root_id,
        script_version_id=script.version_id,
        approved_draft_root_id=approved_draft.root_id,
        approved_draft_version_id=approved_draft.version_id,
        publication_root_id=publication.root_id,
        publication_version_id=publication.version_id,
        manual_edit_root_id=manual_edit.root_id,
        manual_edit_version_id=manual_edit.version_id,
        experiment_version_id=experiment.version_id,
        proposal_version_id=proposal.version_id,
        inferred_preference_revision_id=inferred.revision_id,
        correction_version_id=correction.version_id,
        correction_report_version_id=report.version_id,
    )
    print("PASS GOAL-12 end-to-end staging path materialized")
    return harness, artifacts


def run_script_skill(store: PersistenceStore, topic_id: str) -> tuple[str, str]:
    route = ModelRoute(
        route_name="goal12_script",
        provider_name="goal12-fake-model",
        model_name="goal12-fake-model-v1",
        config_version="goal12.routes.v1",
        config_hash=content_hash({"route": "goal12_script"}, "goal12.route.v1"),
    )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={"goal12-fake-model": FakeModelProvider()},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=FakeClock().now_ms,
    )
    skill = PortableSkillSpec(
        skill_name="goal12-script-writer",
        skill_version="0.1.0",
        route_name="goal12_script",
        prompt_template="Write a fixture script for {topic_id}: {claim}",
        required_input_keys=("topic_id", "claim"),
        output_contract={"type": "plain_text"},
    )
    binding = HostBindingSpec(
        binding_name="goal12-production-binding",
        binding_version="0.1.0",
        input_map={"topic_id": "topic_id", "claim": "claim"},
    )
    input_payload = binding.bind({"topic_id": topic_id, "claim": "Claim from Staging source one"})
    result = PortableSkillRunner(gateway).run(
        skill=skill,
        input_payload=input_payload,
        binding=binding,
        correlation_id=topic_id,
    )
    return root_for(store, result.model_run.envelope_version_id), result.model_run.envelope_version_id


def create_fixture_ref(store: PersistenceStore, object_kind: str, relation_role: str, payload: dict[str, Any]) -> VersionRef:
    root = store.create_root(object_kind)
    version = store.append_version(root, payload, projection_version=f"goal12.{object_kind}.v1")
    store.set_current_version(root, version)
    return VersionRef(
        relation_role=relation_role,
        target_object_kind=object_kind,
        target_stable_id=root,
        target_version_id=version,
        target_content_hash=content_hash(payload, f"goal12.{object_kind}.v1"),
        locator={"fixture": object_kind},
    )


def test_traceability_replay_and_faults(harness: Goal12StagingHarness, artifacts: StagingArtifacts) -> None:
    store = harness.store
    for version_id in (
        artifacts.research_artifact_version_id,
        artifacts.script_version_id,
        artifacts.approved_draft_version_id,
        artifacts.publication_version_id,
        artifacts.experiment_version_id,
        artifacts.proposal_version_id,
        artifacts.correction_report_version_id,
    ):
        root_id = root_for(store, version_id)
        current = store.conn.execute("SELECT current_version_id FROM trace_root WHERE root_id=?", (root_id,)).fetchone()[
            "current_version_id"
        ]
        assert current is not None
        assert store.conn.execute("SELECT count(*) FROM trace_version WHERE version_id=?", (version_id,)).fetchone()[0] == 1

    production = ProductionVersionChainMaterializer(store)
    replay = production.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="publication_capture",
            topic_id=artifacts.topic_id,
            actor="goal12-publisher",
            idempotency_key="goal12-publication-capture-v1",
            content_payload={"platform": "fixture", "text": "published staging script"},
            evidence_refs=(
                VersionRef(
                    relation_role="published_from_approved_draft",
                    target_object_kind="production_approved_draft",
                    target_stable_id=artifacts.approved_draft_root_id,
                    target_version_id=artifacts.approved_draft_version_id,
                    locator={"topic_id": artifacts.topic_id},
                ),
            ),
        )
    )
    assert replay.replayed
    assert replay.version_id == artifacts.publication_version_id

    before = {
        "roots": trace_count(store, "production_script"),
        "receipts": store.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0],
        "outbox": store.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0],
    }
    stale = harness.core.execute(
        CoreCommandEnvelope(
            command_type="transition_state",
            actor=PRODUCTION_HOST_ACTOR,
            object_kind="topic",
            object_id=artifacts.topic_id,
            expected_basis_version_id=artifacts.research_artifact_version_id,
            idempotency_key="goal12-stale-transition",
            payload={"to_state": "planned"},
        )
    )
    assert stale.status == "rejected"
    rejected = store.conn.execute(
        "SELECT status, result_json FROM command_receipt WHERE idempotency_key='goal12-stale-transition'"
    ).fetchone()
    assert rejected is not None and rejected["status"] == "rejected"

    store.conn.execute(
        """
        CREATE TEMP TRIGGER fail_goal12_script_receipt
        BEFORE INSERT ON command_receipt
        WHEN NEW.command_scope='goal08.production_chain'
         AND NEW.idempotency_key='goal12-script-fault'
        BEGIN
            SELECT RAISE(ABORT, 'goal12 injected script receipt failure');
        END
        """
    )
    try:
        production.materialize_artifact(
            ProductionArtifactCommand(
                artifact_kind="script",
                topic_id=artifacts.topic_id,
                actor="goal12-writer",
                idempotency_key="goal12-script-fault",
                content_payload={"claim": "fault", "text": "must rollback"},
                evidence_refs=(artifacts.evidence_ref,),
            )
        )
    except sqlite3.IntegrityError as exc:
        assert "goal12 injected" in str(exc)
    else:
        raise AssertionError("expected injected GOAL-12 script fault")
    finally:
        store.conn.execute("DROP TRIGGER fail_goal12_script_receipt")
    after = {
        "roots": trace_count(store, "production_script"),
        "receipts": store.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0],
        "outbox": store.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0],
    }
    assert after["roots"] == before["roots"]
    assert after["receipts"] == before["receipts"] + 1  # stale transition rejection is durable evidence.
    assert after["outbox"] == before["outbox"]
    print("PASS GOAL-12 traceability, replay, stale-basis and rollback fault gates")


def test_backup_restore_and_clean_room(harness: Goal12StagingHarness, artifacts: StagingArtifacts) -> None:
    with tempfile.TemporaryDirectory(prefix="goal12_staging_") as tmp:
        tmp_path = Path(tmp)
        backup_path = tmp_path / "goal12_backup.sqlite"
        restored_path = tmp_path / "goal12_restored.sqlite"
        validation_root = tmp_path / "validation_assets"
        candidate_root = tmp_path / "candidate_workspace"
        formal_skill_root = tmp_path / "formal_skills"
        validation_root.mkdir()
        candidate_root.mkdir()
        formal_skill_root.mkdir()
        (validation_root / "report.json").write_text('{"mode":"validation"}', encoding="utf-8")
        (candidate_root / "candidate_skill.json").write_text('{"skill":"candidate"}', encoding="utf-8")
        (formal_skill_root / "goal12_script.json").write_text(
            json.dumps({"skill_name": "goal12-script-writer", "allowlisted": True}),
            encoding="utf-8",
        )

        backup_conn = sqlite3.connect(str(backup_path))
        harness.store.conn.backup(backup_conn)
        backup_conn.close()
        shutil.copy2(backup_path, restored_path)
        restored = sqlite3.connect(str(restored_path))
        restored.row_factory = sqlite3.Row
        restored_count = restored.execute(
            "SELECT count(*) FROM trace_version WHERE version_id IN (?, ?, ?, ?)",
            (
                artifacts.script_version_id,
                artifacts.publication_version_id,
                artifacts.experiment_version_id,
                artifacts.correction_report_version_id,
            ),
        ).fetchone()[0]
        assert restored_count == 4
        restored.close()

        shutil.rmtree(validation_root)
        shutil.rmtree(candidate_root)
        loaded = load_formal_skill_allowlist(formal_skill_root, allowlist={"goal12-script-writer"})
        assert loaded == {"goal12-script-writer"}
        production_probe = Goal12StagingHarness()
        production_probe.core.grant_permission(PRODUCTION_HOST_ACTOR, "create_state")
        probe_result = HermesCoreBridge(production_probe.core).dispatch(
            FeishuThinBinding().to_hermes_message(
                FeishuBindingEvent(
                    event_id="goal12-clean-room-startup",
                    chat_id="goal12-clean-room",
                    sender_id="goal12-user",
                    command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
                )
            )
        )
        assert probe_result.status == "succeeded"
        assert not validation_root.exists()
        assert not candidate_root.exists()
    print("PASS GOAL-12 backup/restore and clean-room deletion gates")


def load_formal_skill_allowlist(root: Path, *, allowlist: set[str]) -> set[str]:
    loaded: set[str] = set()
    for path in root.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        skill_name = str(payload.get("skill_name") or "")
        if skill_name in allowlist and payload.get("allowlisted") is True:
            loaded.add(skill_name)
    return loaded


def test_interruption_resume_and_continuous_run() -> None:
    harness = Goal12StagingHarness()
    target = create_fixture_ref(
        harness.store,
        "research_claim",
        "corrects_research_claim",
        {"claim": "old"},
    )
    consumer = create_fixture_ref(
        harness.store,
        "content_plan",
        "consumer_plan",
        {"claim": "old", "outline": "uses corrected claim"},
    )
    harness.store.record_object_reference(
        source_version_id=str(consumer.target_version_id),
        relation_role="evidence_ref",
        target_object_kind=target.target_object_kind,
        target_stable_id=target.target_stable_id,
        target_version_id=target.target_version_id,
        target_content_hash=target.target_content_hash,
        locator={"fixture": "resume"},
    )
    materializer = CorrectionMaterializer(harness.store)
    registration = materializer.register_correction(
        CorrectionRegistrationCommand(
            actor="goal12-resume-worker",
            idempotency_key="goal12-resume-correction",
            target_ref=target,
            corrected_payload={"claim": "corrected"},
            change_scope="source_fact_value",
            reason="forced interruption resume",
        )
    )
    impact = registration.impacts[0]
    enqueued = materializer.enqueue_impact_jobs(harness.scheduler, registration.version_id, max_attempts=3)
    first_claim = harness.scheduler.claim_next(worker_id="interrupted-worker", lease_seconds=1)
    assert first_claim.job_id == enqueued[0].job_id
    harness.clock.advance_ms(2_000)
    recovered = harness.scheduler.recover_expired_leases(actor="goal12-resume-recovery")
    assert recovered == 1
    runtime = harness.make_runtime()
    result = runtime.run_once(lease_seconds=1)
    assert result.status == "succeeded"
    report = materializer.create_report(
        CorrectionReportCommand(
            actor="goal12-report-worker",
            idempotency_key="goal12-resume-report",
            correction_version_id=registration.version_id,
            correlation_id=registration.version_id,
            causation_id=impact.version_id,
        )
    )
    assert report.version_id
    for idx in range(5):
        harness.scheduler.enqueue_job(
            job_kind="outbox.dispatch",
            payload={
                "outbox_id": f"goal12-continuous-{idx}",
                "topic": GOAL11_RESPONSE_TOPIC,
                "payload": {
                    "reply_channel_id": "goal12-continuous-chat",
                    "source_event_id": f"goal12-continuous-{idx}",
                    "correlation_id": f"goal12-continuous-{idx}",
                },
            },
            idempotency_key=f"goal12-continuous-job-{idx}",
            max_attempts=2,
        )
    continuous = harness.make_runtime().run_batch(max_jobs=10)
    assert len([item for item in continuous if item.status == "succeeded"]) == 5
    assert harness.store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope=?",
        (GOAL11_RESPONSE_SEND_SCOPE,),
    ).fetchone()[0] == 5
    print("PASS GOAL-12 forced interruption/resume and continuous-run fixture")


def test_boundary_review() -> None:
    staging_file = Path(__file__)
    text = staging_file.read_text(encoding="utf-8")
    network_token = "requests" + "."
    process_token = "sub" + "process"
    env_token = "os" + "." + "environ"
    create_table_token = "CREATE" + " TABLE"
    candidate_loader_token = "target_object_kind=" + '"' + "candidate_skill" + '"'
    assert network_token not in text
    assert process_token not in text
    assert env_token not in text
    assert "candidate_skill" not in text or candidate_loader_token not in text
    assert create_table_token not in text
    print("PASS GOAL-12 two-review boundary check: no external I/O, no new business table, no candidate loader")


def main() -> int:
    harness, artifacts = run_end_to_end_staging()
    test_traceability_replay_and_faults(harness, artifacts)
    test_backup_restore_and_clean_room(harness, artifacts)
    test_interruption_resume_and_continuous_run()
    test_boundary_review()
    print("GOAL-12 verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
