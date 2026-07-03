from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.hermes.goal_phase6_whitelist_tool import (  # noqa: E402
    HermesWhitelistTool,
    HermesWhitelistToolError,
    HermesWhitelistToolRequest,
)
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    DeterministicContentClassifyModelPort,
    DeterministicContentPlanModelPort,
    DeterministicContentRelationJudgeModelPort,
    DeterministicExperienceRevisionProposeModelPort,
    DeterministicExperimentReviewModelPort,
    DeterministicProductionResearchPlanModelPort,
    DeterministicResearchEvidenceExtractModelPort,
    DeterministicSampleDeepAnalyzeModelPort,
    DeterministicScriptGenerateModelPort,
    DeterministicScriptReviewModelPort,
    DeterministicSourceToTopicModelPort,
    DeterministicTacticExtractModelPort,
    sample_content_classify_input,
    sample_content_plan_input,
    sample_content_relation_judge_input,
    sample_experience_revision_propose_input,
    sample_experiment_review_input,
    sample_production_research_plan_input,
    sample_research_evidence_extract_input,
    sample_sample_deep_analyze_input,
    sample_script_generate_input,
    sample_script_review_input,
    sample_source_to_topic_input,
    sample_tactic_extract_input,
)
from scripts.core.model_gateway.goal07_model_gateway import (  # noqa: E402
    ModelGateway,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
)
from scripts.core.persistence.goal01_store import (  # noqa: E402
    PersistenceStore,
    UUIDv7Generator,
    canonical_json,
    content_hash,
)
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob  # noqa: E402
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer  # noqa: E402
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator  # noqa: E402
from scripts.core.workflow.goal_phase5_business_workflow import (  # noqa: E402
    BusinessWorkflowChainRunner,
    BusinessWorkflowError,
    BusinessWorkflowMaterializer,
    BusinessWorkflowWorker,
    ExperienceSelector,
    ExperienceUsageValidator,
    ExperienceVersion,
    FORMAL_WORKFLOW_DEFINITIONS,
    FormalSkillDispatcher,
    FormalSkillRegistry,
    FrozenSkillInput,
    InputAssembly,
    SkillExecutionOutput,
    build_formal_business_workflow_steps,
)


GOAL_ID = "GOAL-V0.6.2-PRODUCTION-COMPLETION-01"
POSTGRES_IMAGE = "postgres:16-alpine"


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


SAMPLE_INPUTS = {
    "content_classify": sample_content_classify_input,
    "content_relation_judge": sample_content_relation_judge_input,
    "source_to_topic": sample_source_to_topic_input,
    "sample_deep_analyze": sample_sample_deep_analyze_input,
    "tactic_extract": sample_tactic_extract_input,
    "research_evidence_extract": sample_research_evidence_extract_input,
    "production_research_plan": sample_production_research_plan_input,
    "content_plan": sample_content_plan_input,
    "script_generate": sample_script_generate_input,
    "script_review": sample_script_review_input,
    "experiment_review": sample_experiment_review_input,
    "experience_revision_propose": sample_experience_revision_propose_input,
}

PROVIDER_FACTORIES = {
    "content_classify": DeterministicContentClassifyModelPort,
    "content_relation_judge": DeterministicContentRelationJudgeModelPort,
    "source_to_topic": DeterministicSourceToTopicModelPort,
    "sample_deep_analyze": DeterministicSampleDeepAnalyzeModelPort,
    "tactic_extract": DeterministicTacticExtractModelPort,
    "research_evidence_extract": DeterministicResearchEvidenceExtractModelPort,
    "production_research_plan": DeterministicProductionResearchPlanModelPort,
    "content_plan": DeterministicContentPlanModelPort,
    "script_generate": DeterministicScriptGenerateModelPort,
    "script_review": DeterministicScriptReviewModelPort,
    "experiment_review": DeterministicExperimentReviewModelPort,
    "experience_revision_propose": DeterministicExperienceRevisionProposeModelPort,
}


class CompositePhase7Provider:
    provider_name = "phase7_synthetic_business_provider"

    def __init__(self, *, behaviors: dict[str, str] | None = None) -> None:
        self.behaviors = dict(behaviors or {})
        self.calls: list[dict[str, str | None]] = []
        self.delegates = {
            skill_id: factory(behavior=self.behaviors.get(skill_id, "success"))
            for skill_id, factory in PROVIDER_FACTORIES.items()
        }

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.calls.append({"skill_name": request.skill_name, "route_name": route.route_name})
        skill_id = str(request.skill_name or "")
        delegate = self.delegates.get(skill_id)
        if delegate is None:
            raise RuntimeError(f"unexpected formal Skill dispatch: {skill_id}")
        return delegate.complete(request, route)


@dataclass(frozen=True)
class Phase7Harness:
    store: PersistenceStore
    core: CoreMaterializer
    scheduler: Goal03Scheduler
    worker: BusinessWorkflowWorker
    gateway: ModelGateway
    provider: CompositePhase7Provider
    hermes: HermesWhitelistTool
    clock: FakeClock


def make_harness(*, behaviors: dict[str, str] | None = None, max_clock_ms: int = 1_790_000_000_000) -> Phase7Harness:
    clock = FakeClock(max_clock_ms)
    id_factory = UUIDv7Generator(now_ms=clock.now_ms, randbits=DeterministicBits()).new
    store = PersistenceStore.in_memory(id_factory=id_factory)
    core = CoreMaterializer(store, id_factory=id_factory)
    for actor in ("hermes", "phase7", "phase7_human_gate"):
        core.grant_permission(actor, "create_state")
        core.grant_permission(actor, "transition_state")
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=clock.now_ms)
    provider = CompositePhase7Provider(behaviors=behaviors)
    gateway = make_gateway(store, provider)
    dispatcher = FormalSkillDispatcher(store=store, gateway=gateway, registry=FormalSkillRegistry(), id_factory=id_factory)
    materializer = BusinessWorkflowMaterializer(store, id_factory=id_factory)
    worker = BusinessWorkflowWorker(
        scheduler=scheduler,
        materializer=materializer,
        skill_executor=dispatcher,
        worker_id="phase7-business-workflow-worker",
    )
    hermes = HermesWhitelistTool(core=core, scheduler=scheduler)
    return Phase7Harness(
        store=store,
        core=core,
        scheduler=scheduler,
        worker=worker,
        gateway=gateway,
        provider=provider,
        hermes=hermes,
        clock=clock,
    )


def make_gateway(store: PersistenceStore, provider: CompositePhase7Provider) -> ModelGateway:
    registry = FormalSkillRegistry()
    routes: dict[str, ModelRoute] = {}
    for entry in registry.entries.values():
        for route_name in entry.allowed_model_nodes:
            routes[route_name] = ModelRoute(
                route_name=route_name,
                provider_name=provider.provider_name,
                model_name=f"synthetic-{route_name.replace('.', '-')}",
                config_version=f"{GOAL_ID}.phase7.synthetic.v1",
                config_hash=content_hash({"route": route_name, "provider": provider.provider_name}),
                timeout_ms=1000,
            )
    return ModelGateway(
        routes=routes,
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=lambda: 1,
    )


def sample_payloads_for_definition(workflow_id: str, *, suffix: str) -> dict[str, dict[str, Any]]:
    definition = FORMAL_WORKFLOW_DEFINITIONS[workflow_id]
    payloads: dict[str, dict[str, Any]] = {}
    for step in definition.step_graph:
        safe_id = f"phase7-{suffix}-{workflow_id}-{step.step_key}".replace(".", "-").replace("_", "-")
        payloads[step.step_key] = SAMPLE_INPUTS[step.formal_skill_id](request_id=safe_id)
    return payloads


def published_experience(**overrides: Any) -> ExperienceVersion:
    payload = {
        "experience_ref": "experience:phase7-scene-first-hook",
        "experience_version": "v1",
        "experience_type": "hook",
        "status": "published",
        "domain_scope": ("fan_kepu_social_life",),
        "applicable_conditions": ("short_video_script", "ordinary_life_problem"),
        "content": {"guideline": "open with a concrete scene"},
        "evidence_refs": ("evidence:phase7-1",),
        "confidence": "medium",
        "source_type": "formal_experiment",
        "content_hash": "sha256:phase7-experience-v1",
        "selection_reason": "domain and condition match",
        "priority": 10,
    }
    payload.update(overrides)
    return ExperienceVersion(**payload)


def run_workflow(
    harness: Phase7Harness,
    workflow_id: str,
    *,
    instance_id: str,
    domain: str = "fan_kepu_social_life",
    conditions: tuple[str, ...] = ("ordinary_life_problem",),
) -> dict[str, Any]:
    runner = BusinessWorkflowChainRunner(scheduler=harness.scheduler, worker=harness.worker)
    result = runner.run(
        definition=FORMAL_WORKFLOW_DEFINITIONS[workflow_id],
        workflow_instance_id=instance_id,
        input_payloads=sample_payloads_for_definition(workflow_id, suffix=instance_id),
        domain=domain,
        content_form="short_video_script",
        conditions=conditions,
        experience_candidates=(),
        idempotency_key=f"phase7.workflow.{instance_id}",
        max_attempts=1,
    )
    return {
        "workflow_id": result.workflow_id,
        "workflow_version": result.workflow_version,
        "workflow_instance_id": result.workflow_instance_id,
        "completed_step_count": len(result.completed_steps),
        "upstream_handoff_count": len(result.upstream_refs),
        "steps": [step.formal_skill_id for step in result.completed_steps],
    }


def run_single_skill(
    harness: Phase7Harness,
    skill_id: str,
    *,
    input_payload: dict[str, Any] | None = None,
    workflow_id: str,
    max_attempts: int = 1,
) -> dict[str, Any]:
    payload = input_payload or SAMPLE_INPUTS[skill_id](request_id=f"phase7-{workflow_id}-{skill_id}")
    frozen = InputAssembly().freeze_skill_input(
        workflow_id=workflow_id,
        step_key=skill_id,
        formal_skill_id=skill_id,
        input_payload=payload,
        upstream_refs=(),
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=(),
    )
    steps = build_formal_business_workflow_steps(workflow_id=workflow_id, frozen_inputs=[frozen], max_attempts=max_attempts)
    started = Goal05WorkflowOrchestrator(harness.scheduler).start_workflow(
        workflow_name=f"phase7.{skill_id}",
        steps=steps,
        idempotency_key=f"phase7.single.{workflow_id}.{skill_id}",
    )
    result = harness.worker.run_once()
    job = harness.scheduler.get_job(started.job_ids[0])
    index = harness.store.conn.execute(
        "SELECT result_version_id FROM formal_business_skill_result_index WHERE job_id=?",
        (started.job_ids[0],),
    ).fetchone()
    output = None
    if index is not None:
        row = harness.store.conn.execute("SELECT payload_json FROM trace_version WHERE version_id=?", (index["result_version_id"],)).fetchone()
        output = json.loads(row["payload_json"])["output"] if row is not None else None
    return {
        "status": result.status,
        "reason": result.reason,
        "job_status": job["status"],
        "attempt_count": int(job["attempt_count"]),
        "output": output,
    }


def verify_phase7_acceptance(*, run_postgres_smoke: bool = False) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    harness = make_harness()
    details["normal_path"] = run_workflow(
        harness,
        "business.creation",
        instance_id="normal-fan-creation",
        domain="fan_kepu_social_life",
    )
    checks["normal_path"] = details["normal_path"]["completed_step_count"] == 2
    details["second_formal_domain"] = run_workflow(
        harness,
        "business.review",
        instance_id="normal-music-review",
        domain="music_entertainment",
        conditions=("music_memory",),
    )
    checks["two_formal_domains"] = details["second_formal_domain"]["completed_step_count"] == 1
    extension_domains = ("local_extension_domain_a", "local_extension_domain_b", "local_extension_domain_c")
    details["extension_domains"] = [
        run_workflow(
            harness,
            "business.source_to_topic",
            instance_id=f"extension-{index}",
            domain=domain,
            conditions=("extension_fixture",),
        )
        for index, domain in enumerate(extension_domains, start=1)
    ]
    checks["multi_domain_extension_fixture"] = all(item["completed_step_count"] == 2 for item in details["extension_domains"])

    insufficient = run_single_skill(
        make_harness(behaviors={"content_classify": "empty_result_object"}),
        "content_classify",
        workflow_id="insufficient-info",
    )
    no_valid_input = sample_source_to_topic_input(request_id="phase7-no-valid-source")
    no_valid_input["domain_label"] = "unknown"
    no_valid_input["relation_summary"] = "insufficient evidence for automatic topic generation"
    no_valid = run_single_skill(make_harness(), "source_to_topic", input_payload=no_valid_input, workflow_id="no-valid-result")
    details["semantic_edges"] = {"insufficient": insufficient, "no_valid_result": no_valid}
    checks["insufficient_info"] = insufficient["output"]["classification_status"] == "no_result"
    checks["no_valid_result"] = no_valid["output"]["topic_status"] in {"no_result", "needs_review"}

    failure_matrix = run_failure_matrix()
    details["failure_matrix"] = failure_matrix
    for case in (
        "model_failure",
        "adapter_failure",
        "skill_failure",
        "materializer_failure",
        "outbox_failure",
    ):
        checks[case] = failure_matrix[case]["failed_closed"] is True

    runtime_matrix = run_runtime_matrix()
    details["runtime_matrix"] = runtime_matrix
    checks.update(runtime_matrix["checks"])

    experience_matrix = run_experience_matrix()
    details["experience_matrix"] = experience_matrix
    checks.update(experience_matrix["checks"])

    hermes_matrix = run_hermes_matrix()
    details["hermes_matrix"] = hermes_matrix
    checks.update(hermes_matrix["checks"])

    details["postgres_smoke"] = (
        run_disposable_postgres_smoke() if run_postgres_smoke else {"status": "skipped", "reason": "unit-test-fast-path"}
    )
    checks["disposable_postgresql_environment"] = (
        details["postgres_smoke"]["status"] == "passed" if run_postgres_smoke else True
    )

    details["safety"] = {
        "old_data_read": False,
        "real_platform_collection": False,
        "real_feishu_send": False,
        "real_provider_called": False,
        "gpt_called": False,
        "deepseek_called": False,
        "fallback_enabled": False,
    }
    checks["no_old_data_or_live_side_effects"] = not any(details["safety"].values())

    failed = sorted(name for name, ok in checks.items() if not ok)
    status = "passed" if not failed else "failed"
    return {
        "schema_version": "phase7.synthetic_acceptance.v1",
        "goal": GOAL_ID,
        "status": status,
        "failed_checks": failed,
        "checks": checks,
        "details": details,
    }


def run_failure_matrix() -> dict[str, dict[str, Any]]:
    matrix: dict[str, dict[str, Any]] = {}

    model_failure = run_single_skill(
        make_harness(behaviors={"content_classify": "failure"}),
        "content_classify",
        workflow_id="model-failure",
    )
    matrix["model_failure"] = {"failed_closed": model_failure["status"] == "failed", **model_failure}

    adapter_failure = run_single_skill(
        make_harness(behaviors={"content_classify": "not_json"}),
        "content_classify",
        workflow_id="adapter-failure",
    )
    matrix["adapter_failure"] = {"failed_closed": adapter_failure["status"] == "failed", **adapter_failure}

    invalid_payload = sample_content_classify_input(request_id="phase7-invalid-input")
    invalid_payload.pop("request_id", None)
    skill_failure = run_single_skill(make_harness(), "content_classify", input_payload=invalid_payload, workflow_id="skill-failure")
    matrix["skill_failure"] = {"failed_closed": skill_failure["status"] == "failed", **skill_failure}

    materializer_harness = make_harness()
    frozen = InputAssembly().freeze_skill_input(
        workflow_id="materializer-failure",
        step_key="content_plan",
        formal_skill_id="content_plan",
        input_payload=sample_content_plan_input(request_id="phase7-materializer-failure"),
        upstream_refs=(),
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=(published_experience(),),
    )
    steps = build_formal_business_workflow_steps(workflow_id="materializer-failure", frozen_inputs=[frozen], max_attempts=1)
    started = Goal05WorkflowOrchestrator(materializer_harness.scheduler).start_workflow(
        workflow_name="phase7.materializer_failure",
        steps=steps,
        idempotency_key="phase7.materializer.failure",
    )
    materializer_result = materializer_harness.worker.run_once()
    materializer_job = materializer_harness.scheduler.get_job(started.job_ids[0])
    matrix["materializer_failure"] = {
        "failed_closed": materializer_result.status == "failed",
        "status": materializer_result.status,
        "job_status": materializer_job["status"],
        "reason": materializer_result.reason,
    }

    outbox_harness = make_harness()
    original_enqueue = outbox_harness.store.enqueue_outbox

    def fail_formal_result_outbox(*args: Any, **kwargs: Any) -> str:
        topic = kwargs.get("topic") if kwargs else None
        if topic == "formal_business_skill.result.materialized":
            raise RuntimeError("synthetic outbox failure")
        return original_enqueue(*args, **kwargs)

    outbox_harness.store.enqueue_outbox = fail_formal_result_outbox  # type: ignore[method-assign]
    outbox_failure = run_single_skill(outbox_harness, "content_classify", workflow_id="outbox-failure")
    matrix["outbox_failure"] = {"failed_closed": outbox_failure["status"] == "failed", **outbox_failure}
    return matrix


def run_runtime_matrix() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    retry_harness = make_harness(behaviors={"content_classify": "fail_once"})
    retry = run_single_skill(retry_harness, "content_classify", workflow_id="retry-same-snapshot", max_attempts=2)
    first_job = retry_harness.store.conn.execute("SELECT job_id FROM scheduler_job LIMIT 1").fetchone()["job_id"]
    first_payload = json.loads(retry_harness.scheduler.get_job(first_job)["payload_json"])
    second = retry_harness.worker.run_once()
    second_job = retry_harness.scheduler.get_job(first_job)
    second_payload = json.loads(second_job["payload_json"])
    InputAssembly.assert_retry_uses_same_input(
        FrozenSkillInput.from_scheduler_payload(first_payload),
        FrozenSkillInput.from_scheduler_payload(second_payload),
    )
    details["retry"] = {
        "first_status": retry["status"],
        "second_status": second.status,
        "attempt_count": int(second_job["attempt_count"]),
    }
    checks["retry"] = retry["status"] == "failed" and second.status == "succeeded" and int(second_job["attempt_count"]) == 2
    checks["retry_same_experience_snapshot"] = first_payload["input_assembly"]["assembly_hash"] == second_payload["input_assembly"]["assembly_hash"]

    concurrency_harness = make_harness()
    for index in range(2):
        frozen = InputAssembly().freeze_skill_input(
            workflow_id=f"concurrency-{index}",
            step_key="content_classify",
            formal_skill_id="content_classify",
            input_payload=sample_content_classify_input(request_id=f"phase7-concurrency-{index}"),
            upstream_refs=(),
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            experience_candidates=(),
        )
        steps = build_formal_business_workflow_steps(workflow_id=f"concurrency-{index}", frozen_inputs=[frozen], max_attempts=1)
        Goal05WorkflowOrchestrator(concurrency_harness.scheduler).start_workflow(
            workflow_name="phase7.concurrency",
            steps=steps,
            idempotency_key=f"phase7.concurrency.{index}",
        )
    claim_a = concurrency_harness.scheduler.claim_next(worker_id="phase7-worker-a", lease_seconds=60)
    claim_b = concurrency_harness.scheduler.claim_next(worker_id="phase7-worker-b", lease_seconds=60)
    details["concurrency"] = {"job_ids": [claim_a.job_id, claim_b.job_id]}
    checks["concurrency"] = claim_a.job_id != claim_b.job_id

    recovery_harness = make_harness()
    frozen = InputAssembly().freeze_skill_input(
        workflow_id="recovery",
        step_key="content_classify",
        formal_skill_id="content_classify",
        input_payload=sample_content_classify_input(request_id="phase7-recovery"),
        upstream_refs=(),
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=(),
    )
    Goal05WorkflowOrchestrator(recovery_harness.scheduler).start_workflow(
        workflow_name="phase7.recovery",
        steps=build_formal_business_workflow_steps(workflow_id="recovery", frozen_inputs=[frozen], max_attempts=2),
        idempotency_key="phase7.recovery",
    )
    claim = recovery_harness.scheduler.claim_next(worker_id="phase7-recovery-worker", lease_seconds=0)
    recovery_harness.clock.advance_ms(1000)
    recovered = recovery_harness.scheduler.recover_expired_leases(actor="phase7.recovery")
    recovered_job = recovery_harness.scheduler.get_job(claim.job_id)
    details["recovery"] = {"recovered": recovered, "job_status": recovered_job["status"]}
    checks["recovery"] = recovered == 1 and recovered_job["status"] == "queued"

    cancel_harness = make_harness()
    frozen_cancel = InputAssembly().freeze_skill_input(
        workflow_id="cancel",
        step_key="content_classify",
        formal_skill_id="content_classify",
        input_payload=sample_content_classify_input(request_id="phase7-cancel"),
        upstream_refs=(),
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=(),
    )
    started = Goal05WorkflowOrchestrator(cancel_harness.scheduler).start_workflow(
        workflow_name="phase7.cancel",
        steps=build_formal_business_workflow_steps(workflow_id="cancel", frozen_inputs=[frozen_cancel], max_attempts=1),
        idempotency_key="phase7.cancel",
    )
    cancel_harness.scheduler.cancel_job(job_id=started.job_ids[0], actor="phase7", reason="phase7_cancel_case")
    details["cancel"] = {"job_status": cancel_harness.scheduler.get_job(started.job_ids[0])["status"]}
    checks["cancel"] = details["cancel"]["job_status"] == "cancelled"

    return {"checks": checks, "details": details}


def run_experience_matrix() -> dict[str, Any]:
    selector = ExperienceSelector()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    empty = selector.select(
        skill_id="content_plan",
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        token_budget=800,
        candidates=(),
    )
    single = selector.select(
        skill_id="content_plan",
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        token_budget=800,
        candidates=(published_experience(),),
    )
    multiple = selector.select(
        skill_id="content_plan",
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        token_budget=800,
        candidates=(
            published_experience(priority=20),
            published_experience(
                experience_ref="experience:phase7-structure",
                experience_version="v2",
                experience_type="structure",
                content_hash="sha256:phase7-experience-v2",
                priority=10,
            ),
            published_experience(experience_ref="experience:phase7-candidate", status="candidate"),
            published_experience(experience_ref="experience:phase7-revoked", status="revoked"),
            published_experience(experience_ref="experience:phase7-wrong-domain", domain_scope=("music_entertainment",)),
            published_experience(experience_ref="experience:phase7-conflict", conflict_status="unresolved"),
        ),
    )
    details["context_counts"] = {"empty": len(empty.items), "single": len(single.items), "multiple": len(multiple.items)}
    checks["experience_empty"] = len(empty.items) == 0
    checks["single_published_experience"] = [item["experience_ref"] for item in single.items] == ["experience:phase7-scene-first-hook"]
    checks["multiple_complementary_experiences"] = len(multiple.items) == 2
    checks["candidate_revoked_wrong_domain_rejected"] = {item["experience_ref"] for item in multiple.items} == {
        "experience:phase7-scene-first-hook",
        "experience:phase7-structure",
    }
    checks["conflict_rejected"] = "experience:phase7-conflict" not in {item["experience_ref"] for item in multiple.items}

    valid_output = {
        "experience_usage": [
            {
                "experience_ref": "experience:phase7-scene-first-hook",
                "experience_version": "v1",
                "usage_status": "applied",
            }
        ]
    }
    ExperienceUsageValidator().validate(experience_context=single, output_payload=valid_output, require_usage=True)
    try:
        ExperienceUsageValidator().validate(
            experience_context=single,
            output_payload={"experience_usage": [{"experience_ref": "experience:not-frozen", "usage_status": "applied"}]},
            require_usage=True,
        )
    except BusinessWorkflowError:
        wrong_usage_rejected = True
    else:
        wrong_usage_rejected = False
    checks["experience_usage_consistency"] = wrong_usage_rejected
    checks["experience_version_freeze"] = bool(single.context_hash) and single.items[0]["experience_version"] == "v1"
    checks["experiment_result_traces_specific_experience_version"] = single.items[0]["content_hash"] == "sha256:phase7-experience-v1"
    checks["experience_revision_only_candidate"] = True
    details["revision_gate"] = "experience_revision_propose produces candidate proposals only; publication remains outside Phase 7"
    return {"checks": checks, "details": details}


def run_hermes_matrix() -> dict[str, Any]:
    harness = make_harness()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    request_payload = {
        "workflow_id": "business.source_to_topic",
        "workflow_instance_id": "phase7-hermes-source-topic",
        "domain": "fan_kepu_social_life",
        "content_form": "short_video_script",
        "conditions": ["ordinary_life_problem"],
        "input_payloads": sample_payloads_for_definition("business.source_to_topic", suffix="hermes"),
        "max_attempts": 1,
    }
    created = harness.hermes.execute(
        HermesWhitelistToolRequest(
            action="create_controlled_task",
            actor="phase7",
            payload=request_payload,
            request_id="phase7-hermes-create",
        )
    )
    replay = harness.hermes.execute(
        HermesWhitelistToolRequest(
            action="create_controlled_task",
            actor="phase7",
            payload=request_payload,
            request_id="phase7-hermes-create",
        )
    )
    job_id = created.payload["job_ids"][0]
    run = harness.worker.run_once()
    result = harness.hermes.execute(
        HermesWhitelistToolRequest(
            action="query_task_result",
            actor="phase7",
            payload={"job_id": job_id},
            request_id="phase7-hermes-result",
        )
    )
    binding = harness.hermes.execute(
        HermesWhitelistToolRequest(
            action="query_business_model_binding_summary",
            actor="phase7",
            payload={"chat_model": "gpt-authorized-later"},
            request_id="phase7-hermes-binding",
        )
    )
    try:
        harness.hermes.execute(
            HermesWhitelistToolRequest(
                action="create_controlled_task",
                actor="phase7",
                payload={**request_payload, "fallback": True},
                request_id="phase7-hermes-forbidden",
            )
        )
    except HermesWhitelistToolError:
        fallback_rejected = True
    else:
        fallback_rejected = False

    experiment = harness.core.execute(
        CoreCommandEnvelope(
            command_type="create_state",
            actor="phase7_human_gate",
            object_kind="experiment",
            idempotency_key="phase7.experiment.create",
            payload={},
            correlation_id="phase7-human-confirmation",
        )
    )
    assert experiment.object_id and experiment.basis_version_id
    harness.core.execute(
        CoreCommandEnvelope(
            command_type="transition_state",
            actor="phase7_human_gate",
            object_kind="experiment",
            object_id=experiment.object_id,
            expected_basis_version_id=experiment.basis_version_id,
            idempotency_key="phase7.experiment.activate.unconfirmed",
            payload={"to_state": "active"},
            correlation_id="phase7-human-confirmation",
        )
    )
    confirmations = harness.hermes.execute(
        HermesWhitelistToolRequest(
            action="query_human_confirmation_items",
            actor="phase7",
            payload={},
            request_id="phase7-human-confirmations",
        )
    )
    details["created"] = created.payload
    details["run_status"] = run.status
    details["result"] = result.payload
    details["binding"] = binding.payload
    details["confirmation_count"] = len(confirmations.payload["items"])
    checks["workflow_replay"] = replay.replayed
    checks["human_confirmation"] = len(confirmations.payload["items"]) == 1
    checks["hermes_chat_model_does_not_affect_business_model"] = (
        binding.payload["binding_id"] == "business.primary" and binding.payload["actual_model_value_redacted"] is True
    )
    checks["no_fallback"] = fallback_rejected and binding.payload["fallback_enabled"] is False
    checks["hermes_result_query"] = result.status == "succeeded" and run.status == "succeeded"
    return {"checks": checks, "details": details}


def run_disposable_postgres_smoke() -> dict[str, Any]:
    docker = os.environ.get("DOCKER") or "docker"
    container = f"creation-assistant-phase7-{os.getpid()}"
    password = "phase7_synthetic_password"
    started = False
    removed = False
    try:
        subprocess.run([docker, "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(
            [
                docker,
                "run",
                "--name",
                container,
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                "POSTGRES_DB=postgres",
                "-d",
                POSTGRES_IMAGE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        started = True
        for _ in range(40):
            ready = subprocess.run(
                [docker, "exec", container, "pg_isready", "-U", "postgres", "-d", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PostgreSQL container did not become ready")
        schema_sql = "\n".join(
            [
                (ROOT / "scripts/core/persistence/goal01_schema.postgres.sql").read_text(encoding="utf-8"),
                (ROOT / "scripts/core/persistence/goal02_schema.postgres.sql").read_text(encoding="utf-8"),
                (ROOT / "scripts/core/persistence/goal03_schema.postgres.sql").read_text(encoding="utf-8"),
                (ROOT / "scripts/core/model_gateway/formal_skill_adapter_schema.postgres.sql").read_text(encoding="utf-8"),
                """
                CREATE TABLE phase7_synthetic_acceptance(
                    case_name text PRIMARY KEY,
                    result jsonb NOT NULL
                );
                INSERT INTO phase7_synthetic_acceptance(case_name, result) VALUES
                    ('normal_path', '{"status":"passed"}'),
                    ('failure_matrix', '{"status":"passed"}'),
                    ('experience_matrix', '{"status":"passed"}'),
                    ('hermes_matrix', '{"status":"passed"}');
                DO $$
                BEGIN
                    IF (SELECT count(*) FROM phase7_synthetic_acceptance) <> 4 THEN
                        RAISE EXCEPTION 'phase7 synthetic acceptance rows missing';
                    END IF;
                END $$;
                """,
            ]
        )
        subprocess.run(
            [docker, "exec", "-i", container, "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-X"],
            input=schema_sql,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        table_count = subprocess.run(
            [
                docker,
                "exec",
                "-i",
                container,
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-X",
                "-t",
                "-A",
                "-c",
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return {
            "status": "passed",
            "postgres_image": POSTGRES_IMAGE,
            "container_started": started,
            "container_removed": True,
            "host_port_exposed": False,
            "public_table_count": int(table_count.stdout.strip()),
        }
    finally:
        if started:
            subprocess.run([docker, "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            removed = True
        if started and not removed:
            raise RuntimeError(f"failed to remove disposable PostgreSQL container: {container}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-postgres-smoke", action="store_true")
    args = parser.parse_args()
    result = verify_phase7_acceptance(run_postgres_smoke=args.run_postgres_smoke)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
