from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.production.stage1a_research_plan import (
    Stage1AResearchPlanService,
)
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
)
from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService
from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired


def _competitor_input(source_id: str = "source-1") -> dict:
    return {
        "correlation_id": f"test:{source_id}",
        "source_id": source_id,
        "transcript": "一段用于隔离验证的完整口播材料。",
        "metrics": {"like_count": 10, "comment_count": 2, "share_count": 1, "collect_count": 0},
        "comments": [],
        "domain_label": "music_entertainment",
        "domain_context": {},
        "schema_version": "competitor_breakdown.input.v1",
    }


def _competitor_output(source_id: str = "source-1") -> dict:
    return {
        "source_id": source_id,
        "source_content_type": "person/story",
        "analysis_text": (
            "WHAT\n这是一份只基于所给口播材料的核心对象和命题。\n"
            "HOW\n仅说明所给材料中的实际推进。\n"
            "SO WHAT\n无有效复用参考。"
        ),
        "question_expansions": [],
        "schema_version": "competitor_breakdown.output.raw.v4",
    }


class _ResearchCore:
    data_identity = "test"

    def get_task(self, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "current_node": "research_plan",
            "current_status": "processing",
            "current_version_id": "version-1",
        }

    def get_node_version(self, version_id: str) -> dict:
        return {
            "version_id": version_id,
            "task_id": "task-1",
            "node": "research_plan",
            "input_assembly_id": "assembly-1",
        }

    def get_input_assembly_payload(self, assembly_id: str) -> dict:
        return {
            "task_id": "task-1",
            "node": "research_plan",
            "user_requirements": "围绕当前主题制定研究计划",
            "material_refs": [{"kind": "formal_topic", "version_id": "topic-1"}],
            "prompt_version": "research.prompt.test",
            "skill_version": "research.skill.test",
        }


class _CompetitorCore:
    data_identity = "test"

    def observed_breakdown_content_types(self, *, domain_label: str) -> list[str]:
        return []

    def record_external_competitor_execution(self, **kwargs) -> str:
        return "external-competitor-run"


class Stage2B2FirstRoundExternalIntelligenceTests(unittest.TestCase):
    def test_research_task_is_core_owned_and_has_no_model_choice(self) -> None:
        task = Stage1AResearchPlanService(core=_ResearchCore()).prepare_research_plan_external_task(
            task_id="task-1", node_version_id="version-1"
        )
        self.assertEqual(task["task_type"], "research_plan")
        self.assertEqual(task["skill"]["source_reference"], "runtime_skills/research_plan")
        self.assertIn("input", task)
        self.assertIn("constraints", task)

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertFalse({"model", "model_name", "provider", "provider_name", "model_route"}.intersection(keys(task)))

    def test_competitor_breakdown_uses_external_executor_and_strict_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_id = "cold-source-1"
            transcript = Path(directory) / "source.txt"
            transcript.write_text("隔离测试口播材料", encoding="utf-8")
            captured: dict = {}

            def executor(task):
                captured.update(task)
                return {
                    "execution_id": "execution-1",
                    "executor_id": "hermes-test-double",
                    "model_ref": "test-model",
                    "submitted_at": "2026-08-29T00:00:00+00:00",
                    "output": _competitor_output(source_id),
                }

            worker = ConfiguredCompetitorRegistrationExecutor(
                core=_CompetitorCore(),
                collector=None,
                transcriber=None,
                media_materializer=None,
                external_executor=executor,
            )
            artifact = worker._generate_prepared_breakdown_artifact(
                registration={
                    "registration_id": "registration-1",
                    "status": "processing",
                    "domain_label": "music_entertainment",
                    "cold_start_id": "cold-start-1",
                },
                material={
                    "source_id": source_id,
                    "transcript_ref": str(transcript),
                    "metrics": {"like_count": 1},
                    "comments": [],
                },
                attempt_kind="initial",
            )
        self.assertEqual(artifact["deep_breakdown"]["source_id"], source_id)
        self.assertEqual(captured["task_type"], "competitor_breakdown")
        self.assertEqual(captured["business_context"]["origin"], "cold_start_intelligent_judgment")
        self.assertEqual(captured["output_requirements"]["submission"], "structured_fields")

    def test_competitor_without_executor_reports_waiting_without_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_id = "cold-source-2"
            transcript = Path(directory) / "source.txt"
            transcript.write_text("隔离测试口播材料", encoding="utf-8")
            worker = ConfiguredCompetitorRegistrationExecutor(
                core=_CompetitorCore(),
                collector=None,
                transcriber=None,
                media_materializer=None,
            )
            with self.assertRaises(ExternalIntelligenceRequired) as raised:
                worker._generate_prepared_breakdown_artifact(
                    registration={
                        "registration_id": "registration-2",
                        "status": "processing",
                        "domain_label": "music_entertainment",
                        "cold_start_id": "cold-start-2",
                    },
                    material={
                        "source_id": source_id,
                        "transcript_ref": str(transcript),
                        "metrics": {"like_count": 1},
                        "comments": [],
                    },
                    attempt_kind="initial",
                )
        self.assertEqual(raised.exception.task["business_context"]["origin"], "cold_start_intelligent_judgment")

    def test_invalid_structured_result_is_rejected(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        with self.assertRaises(FormalSkillValidationError):
            validate_external_skill_output(contract, _competitor_input(), {"source_id": "source-1"})

    def test_experience_proposal_and_daily_breakdown_share_the_same_boundary(self) -> None:
        experience = FormalSkillContract.from_runtime_skill("experience_candidate_propose")
        experience_input = {
            "correlation_id": "experience-1",
            "domain_label": "music_entertainment",
            "content_plan_context": {"stage": "test"},
            "frozen_breakdowns": [],
            "schema_version": "experience_candidate_propose.input.v1",
        }
        task, _ = prepare_external_skill_task(
            experience,
            experience_input,
            constraints={"proposal_is_not_a_formal_rule": True},
            business_context={"origin": "experience_proposal"},
        )
        self.assertEqual(task["skill"]["formal_skill_id"], "experience_candidate_propose")
        self.assertNotIn("model", str(task).lower())

        daily_source = inspect.getsource(ProductionDailyOperationsService._prepare_and_break_down_daily_hits)
        self.assertNotIn("build_production_daily_hit_gateway", daily_source)
        self.assertNotIn("_execution_model_binding", daily_source)


if __name__ == "__main__":
    unittest.main()
