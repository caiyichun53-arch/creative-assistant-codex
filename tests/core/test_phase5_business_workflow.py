from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    FORMAL_SKILL_JOB_KIND,
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
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRequest, ModelRoute
from scripts.core.model_gateway.goal07_model_gateway import ModelProviderResult
from scripts.core.model_gateway.goal07_model_gateway import ModelRunMaterializer
from scripts.core.workflow.goal_phase5_business_workflow import (
    BusinessWorkflowError,
    BusinessWorkflowMaterializer,
    BusinessWorkflowWorker,
    ExperienceSelector,
    ExperienceUsageValidator,
    ExperienceVersion,
    FORMAL_BUSINESS_WORKFLOW_SKILLS,
    FormalSkillDispatcher,
    FormalSkillRegistry,
    InputAssembly,
    SkillExecutionOutput,
    build_formal_business_workflow_steps,
)
from scripts.core.persistence.goal01_store import PersistenceStore, UUIDv7Generator, content_hash
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator


def published_experience(**overrides) -> ExperienceVersion:
    base = {
        "experience_ref": "experience:scene_first_hook",
        "experience_version": "v1",
        "experience_type": "hook",
        "status": "published",
        "domain_scope": ("fan_kepu_social_life",),
        "applicable_conditions": ("short_video_script", "ordinary_life_problem"),
        "content": {"guideline": "open with a concrete scene"},
        "evidence_refs": ("evidence:1",),
        "confidence": "medium",
        "source_type": "formal_experiment",
        "content_hash": "sha256:experience-v1",
        "selection_reason": "domain and condition match",
        "priority": 10,
    }
    base.update(overrides)
    return ExperienceVersion(**base)


def frozen_content_plan_input():
    return InputAssembly().freeze_skill_input(
        workflow_id="workflow-1",
        step_key="content_plan",
        formal_skill_id="content_plan",
        input_payload=sample_content_plan_input(request_id="phase5-materialized"),
        upstream_refs=[
            {
                "formal_skill_id": "production_research_plan",
                "result_version_id": "version-plan-1",
                "output_hash": "sha256:plan",
            }
        ],
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=[published_experience()],
    )


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


class CompositePhase5Provider:
    provider_name = "phase5_formal_dispatch_test_port"

    def __init__(self):
        self.calls: list[tuple[str | None, str]] = []
        self.delegates = {
            "content_classify": DeterministicContentClassifyModelPort(),
            "content_relation_judge": DeterministicContentRelationJudgeModelPort(),
            "source_to_topic": DeterministicSourceToTopicModelPort(),
            "sample_deep_analyze": DeterministicSampleDeepAnalyzeModelPort(),
            "tactic_extract": DeterministicTacticExtractModelPort(),
            "research_evidence_extract": DeterministicResearchEvidenceExtractModelPort(),
            "production_research_plan": DeterministicProductionResearchPlanModelPort(),
            "content_plan": DeterministicContentPlanModelPort(),
            "script_generate": DeterministicScriptGenerateModelPort(),
            "script_review": DeterministicScriptReviewModelPort(),
            "experiment_review": DeterministicExperimentReviewModelPort(),
            "experience_revision_propose": DeterministicExperienceRevisionProposeModelPort(),
        }

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.calls.append((request.skill_name, route.route_name))
        delegate = self.delegates.get(str(request.skill_name))
        if delegate is None:
            raise RuntimeError(f"unexpected formal Skill dispatch: {request.skill_name}")
        return delegate.complete(request, route)


def make_dispatch_gateway(store: PersistenceStore, *, omit_route: str | None = None) -> tuple[ModelGateway, CompositePhase5Provider]:
    provider = CompositePhase5Provider()
    registry = FormalSkillRegistry()
    routes = {}
    for entry in registry.entries.values():
        for route_name in entry.allowed_model_nodes:
            if route_name == omit_route:
                continue
            routes[route_name] = ModelRoute(
                route_name=route_name,
                provider_name=provider.provider_name,
                model_name=f"deterministic-{route_name.replace('.', '-')}",
                config_version="GOAL-V0.6.2-PRODUCTION-COMPLETION-01.phase5.dispatch.test.v1",
                config_hash=content_hash({"route": route_name, "provider": provider.provider_name}),
                timeout_ms=1000,
            )
    return (
        ModelGateway(
            routes=routes,
            providers={provider.provider_name: provider},
            materializer=ModelRunMaterializer(store),
        ),
        provider,
    )


def frozen_sample_input(skill_id: str, *, workflow_id: str = "workflow-dispatch", version: str | None = None):
    payload = SAMPLE_INPUTS[skill_id](request_id=f"phase5-{skill_id}")
    return InputAssembly().freeze_skill_input(
        workflow_id=workflow_id,
        step_key=skill_id,
        formal_skill_id=skill_id,
        formal_skill_version=version,
        input_payload=payload,
        upstream_refs=[],
        domain="fan_kepu_social_life",
        content_form="short_video_script",
        conditions=("ordinary_life_problem",),
        experience_candidates=[],
    )


class Phase5BusinessWorkflowTests(unittest.TestCase):
    def test_formal_business_workflow_lists_all_twelve_skills(self) -> None:
        self.assertEqual(len(FORMAL_BUSINESS_WORKFLOW_SKILLS), 12)
        self.assertEqual(FORMAL_BUSINESS_WORKFLOW_SKILLS[0], "content_classify")
        self.assertEqual(FORMAL_BUSINESS_WORKFLOW_SKILLS[-1], "experience_revision_propose")
        self.assertEqual(len(set(FORMAL_BUSINESS_WORKFLOW_SKILLS)), 12)

    def test_experience_selector_only_freezes_published_matching_non_conflicting_versions(self) -> None:
        selector = ExperienceSelector()
        context = selector.select(
            skill_id="content_plan",
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            token_budget=800,
            candidates=[
                published_experience(),
                published_experience(experience_ref="experience:candidate", status="candidate"),
                published_experience(experience_ref="experience:wrong-domain", domain_scope=("music_entertainment",)),
                published_experience(experience_ref="experience:conflict", conflict_status="unresolved"),
                published_experience(experience_ref="experience:revoked", status="revoked"),
            ],
        )

        self.assertEqual([item["experience_ref"] for item in context.items], ["experience:scene_first_hook"])
        self.assertEqual(context.schema_version, "experience_context.v1")
        self.assertTrue(context.context_hash)

    def test_input_assembly_freezes_public_input_upstream_refs_and_experience_context(self) -> None:
        assembler = InputAssembly()
        payload = sample_content_plan_input(request_id="phase5-content-plan")
        frozen = assembler.freeze_skill_input(
            workflow_id="workflow-1",
            step_key="content_plan",
            formal_skill_id="content_plan",
            input_payload=payload,
            upstream_refs=[
                {
                    "formal_skill_id": "production_research_plan",
                    "result_version_id": "version-plan-1",
                    "output_hash": "sha256:plan",
                }
            ],
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            experience_candidates=[published_experience()],
        )
        retry = assembler.freeze_skill_input(
            workflow_id="workflow-1",
            step_key="content_plan",
            formal_skill_id="content_plan",
            input_payload=payload,
            upstream_refs=[
                {
                    "formal_skill_id": "production_research_plan",
                    "result_version_id": "version-plan-1",
                    "output_hash": "sha256:plan",
                }
            ],
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            experience_candidates=[published_experience()],
        )

        self.assertEqual(frozen.input_hash, retry.input_hash)
        self.assertEqual(frozen.assembly_hash, retry.assembly_hash)
        InputAssembly.assert_retry_uses_same_input(frozen, retry)
        scheduler_payload = frozen.scheduler_payload()
        self.assertEqual(scheduler_payload["formal_skill_id"], "content_plan")
        self.assertEqual(scheduler_payload["input"], payload)
        self.assertEqual(
            scheduler_payload["input_assembly"]["experience_context"]["items"][0]["experience_ref"],
            "experience:scene_first_hook",
        )

    def test_input_assembly_rejects_private_or_legacy_inputs(self) -> None:
        assembler = InputAssembly()
        payload = sample_content_plan_input(request_id="phase5-bad-input")
        payload["database_connection"] = "data/creation.db"
        with self.assertRaises(BusinessWorkflowError):
            assembler.freeze_skill_input(
                workflow_id="workflow-1",
                step_key="content_plan",
                formal_skill_id="content_plan",
                input_payload=payload,
                upstream_refs=[
                    {
                        "formal_skill_id": "production_research_plan",
                        "result_version_id": "version-plan-1",
                        "output_hash": "sha256:plan",
                    }
                ],
                domain="fan_kepu_social_life",
                content_form="short_video_script",
                conditions=("ordinary_life_problem",),
                experience_candidates=[published_experience()],
            )

    def test_experience_usage_must_reference_frozen_context_only(self) -> None:
        context = ExperienceSelector().select(
            skill_id="script_generate",
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            token_budget=800,
            candidates=[published_experience()],
        )
        validator = ExperienceUsageValidator()
        validator.validate(
            experience_context=context,
            output_payload={
                "experience_usage": [
                    {
                        "experience_ref": "experience:scene_first_hook",
                        "experience_version": "v1",
                        "usage_status": "applied",
                        "influence_scope": "opening",
                        "usage_summary": "used the scene-first opening principle",
                    }
                ]
            },
            require_usage=True,
        )
        with self.assertRaises(BusinessWorkflowError):
            validator.validate(
                experience_context=context,
                output_payload={
                    "experience_usage": [
                        {
                            "experience_ref": "experience:not-in-context",
                            "usage_status": "applied",
                        }
                    ]
                },
                require_usage=True,
            )
        with self.assertRaises(BusinessWorkflowError):
            validator.validate(experience_context=context, output_payload={}, require_usage=True)

    def test_build_workflow_steps_uses_formal_skill_job_kind_and_frozen_payload(self) -> None:
        assembler = InputAssembly()
        frozen = assembler.freeze_skill_input(
            workflow_id="workflow-1",
            step_key="content_plan",
            formal_skill_id="content_plan",
            input_payload=sample_content_plan_input(request_id="phase5-step"),
            upstream_refs=[
                {
                    "formal_skill_id": "production_research_plan",
                    "result_version_id": "version-plan-1",
                    "output_hash": "sha256:plan",
                }
            ],
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            experience_candidates=[published_experience()],
        )

        steps = build_formal_business_workflow_steps(workflow_id="workflow-1", frozen_inputs=[frozen])

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].job_kind, FORMAL_SKILL_JOB_KIND)
        self.assertEqual(steps[0].payload["formal_skill_id"], "content_plan")
        self.assertEqual(steps[0].payload["input_assembly"]["assembly_hash"], frozen.assembly_hash)

    def test_frozen_workflow_steps_enqueue_idempotently_through_goal05_orchestrator(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        assembler = InputAssembly()
        frozen = assembler.freeze_skill_input(
            workflow_id="workflow-1",
            step_key="content_plan",
            formal_skill_id="content_plan",
            input_payload=sample_content_plan_input(request_id="phase5-enqueue"),
            upstream_refs=[
                {
                    "formal_skill_id": "production_research_plan",
                    "result_version_id": "version-plan-1",
                    "output_hash": "sha256:plan",
                }
            ],
            domain="fan_kepu_social_life",
            content_form="short_video_script",
            conditions=("ordinary_life_problem",),
            experience_candidates=[published_experience()],
        )
        steps = build_formal_business_workflow_steps(workflow_id="workflow-1", frozen_inputs=[frozen])
        orchestrator = Goal05WorkflowOrchestrator(scheduler)

        first = orchestrator.start_workflow(
            workflow_name="business.formal.content_creation",
            steps=steps,
            idempotency_key="phase5-workflow-1",
        )
        replay = orchestrator.start_workflow(
            workflow_name="business.formal.content_creation",
            steps=steps,
            idempotency_key="phase5-workflow-1",
        )

        self.assertEqual(first.job_ids, replay.job_ids)
        self.assertTrue(replay.replayed)
        row = scheduler.conn.execute("SELECT job_kind, payload_json FROM scheduler_job").fetchone()
        self.assertEqual(row["job_kind"], FORMAL_SKILL_JOB_KIND)
        self.assertIn(frozen.assembly_hash, row["payload_json"])

    def test_materializer_persists_input_assembly_with_refs_audit_outbox_and_idempotency(self) -> None:
        store = PersistenceStore.in_memory()
        self.addCleanup(store.conn.close)
        materializer = BusinessWorkflowMaterializer(store)
        frozen = frozen_content_plan_input()

        first = materializer.record_input_assembly(
            frozen,
            actor="phase5-test",
            idempotency_key="assembly-1",
        )
        replay = materializer.record_input_assembly(
            frozen,
            actor="phase5-test",
            idempotency_key="assembly-1",
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.version_id, replay.version_id)
        roots = store.conn.execute(
            "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_input_assembly'"
        ).fetchone()[0]
        refs = store.conn.execute("SELECT relation_role FROM object_reference").fetchall()
        outbox = store.conn.execute(
            "SELECT topic FROM outbox_message WHERE topic='phase5.input_assembly.recorded'"
        ).fetchall()
        self.assertEqual(roots, 1)
        self.assertIn("assembled_from_upstream_skill_result", {row["relation_role"] for row in refs})
        self.assertIn("freezes_experience_context", {row["relation_role"] for row in refs})
        self.assertEqual(len(outbox), 1)

    def test_materializer_persists_experience_usage_and_rejects_unfrozen_usage_without_write(self) -> None:
        store = PersistenceStore.in_memory()
        self.addCleanup(store.conn.close)
        materializer = BusinessWorkflowMaterializer(store)
        frozen = frozen_content_plan_input()
        good = {
            "experience_usage": [
                {
                    "experience_ref": "experience:scene_first_hook",
                    "experience_version": "v1",
                    "usage_status": "applied",
                    "influence_scope": "opening",
                    "usage_summary": "used the scene-first opening principle",
                }
            ]
        }

        result = materializer.record_experience_usage(
            frozen=frozen,
            output_payload=good,
            result_version_id="result-version-1",
            actor="phase5-test",
            idempotency_key="usage-1",
            require_usage=True,
        )
        replay = materializer.record_experience_usage(
            frozen=frozen,
            output_payload=good,
            result_version_id="result-version-1",
            actor="phase5-test",
            idempotency_key="usage-1",
            require_usage=True,
        )

        self.assertFalse(result.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(result.version_id, replay.version_id)
        self.assertEqual(
            store.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_experience_usage'"
            ).fetchone()[0],
            1,
        )
        with self.assertRaises(BusinessWorkflowError):
            materializer.record_experience_usage(
                frozen=frozen,
                output_payload={"experience_usage": [{"experience_ref": "experience:not-frozen", "usage_status": "applied"}]},
                result_version_id="result-version-2",
                actor="phase5-test",
                idempotency_key="usage-bad",
                require_usage=True,
            )
        self.assertEqual(
            store.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_experience_usage'"
            ).fetchone()[0],
            1,
        )

    def test_business_workflow_worker_records_assembly_usage_and_completes_job(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        frozen = frozen_content_plan_input()
        steps = build_formal_business_workflow_steps(workflow_id="workflow-1", frozen_inputs=[frozen])
        Goal05WorkflowOrchestrator(scheduler).start_workflow(
            workflow_name="business.formal.content_creation",
            steps=steps,
            idempotency_key="phase5-worker-success",
        )

        def runner(received, *, job_id: str, attempt_id: str):
            self.assertEqual(received.assembly_hash, frozen.assembly_hash)
            return SkillExecutionOutput(
                result_version_id="result-version-1",
                output_payload={
                    "experience_usage": [
                        {
                            "experience_ref": "experience:scene_first_hook",
                            "experience_version": "v1",
                            "usage_status": "applied",
                            "influence_scope": "opening",
                            "usage_summary": "used the scene-first opening principle",
                        }
                    ]
                },
            )

        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(scheduler.store),
            skill_executor=runner,
            worker_id="phase5-worker",
        )

        step = worker.run_once()

        self.assertEqual(step.status, "succeeded")
        self.assertEqual(step.step_key, "content_plan")
        self.assertEqual(
            scheduler.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_input_assembly'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            scheduler.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_experience_usage'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(scheduler.conn.execute("SELECT status FROM scheduler_job").fetchone()["status"], "succeeded")

    def test_business_workflow_worker_fails_closed_when_required_usage_is_missing(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        frozen = frozen_content_plan_input()
        steps = build_formal_business_workflow_steps(workflow_id="workflow-1", frozen_inputs=[frozen], max_attempts=1)
        Goal05WorkflowOrchestrator(scheduler).start_workflow(
            workflow_name="business.formal.content_creation",
            steps=steps,
            idempotency_key="phase5-worker-missing-usage",
        )

        def runner(_received, *, job_id: str, attempt_id: str):
            return SkillExecutionOutput(result_version_id="result-version-1", output_payload={})

        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(scheduler.store),
            skill_executor=runner,
            worker_id="phase5-worker",
        )

        step = worker.run_once()

        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertEqual(scheduler.conn.execute("SELECT status FROM scheduler_job").fetchone()["status"], "dead")
        self.assertEqual(
            scheduler.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_experience_usage'"
            ).fetchone()[0],
            0,
        )

    def test_formal_skill_dispatcher_runs_all_twelve_skills_through_adapter_and_gateway(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        gateway, provider = make_dispatch_gateway(scheduler.store)
        dispatcher = FormalSkillDispatcher(store=scheduler.store, gateway=gateway, id_factory=generator.new)
        frozen_inputs = [frozen_sample_input(skill_id) for skill_id in FORMAL_BUSINESS_WORKFLOW_SKILLS]
        steps = build_formal_business_workflow_steps(workflow_id="workflow-dispatch", frozen_inputs=frozen_inputs, max_attempts=1)
        Goal05WorkflowOrchestrator(scheduler).start_workflow(
            workflow_name="business.formal.dispatcher_matrix",
            steps=steps,
            idempotency_key="phase5-dispatch-all-skills",
        )
        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(scheduler.store),
            skill_executor=dispatcher,
            worker_id="phase5-dispatch-worker",
        )

        results = [worker.run_once() for _ in FORMAL_BUSINESS_WORKFLOW_SKILLS]

        self.assertEqual({result.status for result in results}, {"succeeded"})
        self.assertEqual(
            scheduler.conn.execute("SELECT count(*) FROM formal_business_skill_result_index").fetchone()[0],
            12,
        )
        self.assertEqual(
            scheduler.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_input_assembly'"
            ).fetchone()[0],
            12,
        )
        self.assertEqual(
            scheduler.conn.execute(
                "SELECT count(*) FROM trace_root WHERE object_kind='business_workflow_experience_usage'"
            ).fetchone()[0],
            12,
        )
        self.assertEqual(
            set(row[0] for row in scheduler.conn.execute("SELECT DISTINCT formal_skill_id FROM formal_business_skill_result_index")),
            set(FORMAL_BUSINESS_WORKFLOW_SKILLS),
        )
        self.assertGreaterEqual(len(provider.calls), 12)
        self.assertIn(("content_plan", "business.creation_hook"), provider.calls)
        self.assertIn(("script_review", "business.ai_flavor_judge"), provider.calls)

    def test_formal_skill_dispatcher_fails_closed_on_version_mismatch(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        gateway, _provider = make_dispatch_gateway(scheduler.store)
        dispatcher = FormalSkillDispatcher(store=scheduler.store, gateway=gateway, id_factory=generator.new)
        frozen = frozen_sample_input("content_classify", workflow_id="workflow-version", version="9.9.9")
        steps = build_formal_business_workflow_steps(workflow_id="workflow-version", frozen_inputs=[frozen], max_attempts=1)
        Goal05WorkflowOrchestrator(scheduler).start_workflow(
            workflow_name="business.formal.dispatcher_version_guard",
            steps=steps,
            idempotency_key="phase5-dispatch-version-mismatch",
        )
        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(scheduler.store),
            skill_executor=dispatcher,
            worker_id="phase5-dispatch-worker",
        )

        step = worker.run_once()

        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertEqual(scheduler.conn.execute("SELECT status FROM scheduler_job").fetchone()["status"], "dead")
        self.assertEqual(
            scheduler.conn.execute("SELECT count(*) FROM formal_business_skill_result_index").fetchone()[0],
            0,
        )

    def test_formal_skill_dispatcher_fails_closed_when_approved_route_is_missing(self) -> None:
        generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
        scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
        self.addCleanup(scheduler.store.conn.close)
        gateway, _provider = make_dispatch_gateway(scheduler.store, omit_route="business.creation_outline")
        dispatcher = FormalSkillDispatcher(store=scheduler.store, gateway=gateway, id_factory=generator.new)
        frozen = frozen_sample_input("content_plan", workflow_id="workflow-missing-route")
        steps = build_formal_business_workflow_steps(workflow_id="workflow-missing-route", frozen_inputs=[frozen], max_attempts=1)
        Goal05WorkflowOrchestrator(scheduler).start_workflow(
            workflow_name="business.formal.dispatcher_route_guard",
            steps=steps,
            idempotency_key="phase5-dispatch-missing-route",
        )
        worker = BusinessWorkflowWorker(
            scheduler=scheduler,
            materializer=BusinessWorkflowMaterializer(scheduler.store),
            skill_executor=dispatcher,
            worker_id="phase5-dispatch-worker",
        )

        step = worker.run_once()

        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertEqual(scheduler.conn.execute("SELECT status FROM scheduler_job").fetchone()["status"], "dead")
        self.assertEqual(
            scheduler.conn.execute("SELECT count(*) FROM formal_business_skill_result_index").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
