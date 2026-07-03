from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import FORMAL_SKILL_JOB_KIND, sample_content_plan_input
from scripts.core.workflow.goal_phase5_business_workflow import (
    BusinessWorkflowError,
    ExperienceSelector,
    ExperienceUsageValidator,
    ExperienceVersion,
    FORMAL_BUSINESS_WORKFLOW_SKILLS,
    InputAssembly,
    build_formal_business_workflow_steps,
)
from scripts.core.persistence.goal01_store import UUIDv7Generator
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


if __name__ == "__main__":
    unittest.main()
