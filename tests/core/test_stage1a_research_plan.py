from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelProviderResult, ModelRequest, ModelRoute, ModelUsage
from scripts.core.production.stage0_content_core import (
    CoreModelRunMaterializer,
    DataIdentityError,
    FORMAL_DB_PATH,
    InputAssembly,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1a_research_plan import (
    ResearchPlanValidationError,
    Stage1AResearchPlanService,
)


VALID_PLAN = {
    "core_question": "Why does this everyday pattern persist?",
    "provisional_viewpoint": "The pattern may be driven by an incentive mismatch.",
    "research_scope": {"included": ["public rules", "primary sources"], "excluded": ["video-platform analysis"]},
    "research_questions": ["Which rule or mechanism is decisive?"],
    "candidate_claims": [{"claim": "The rule changes behavior.", "status": "to_verify"}],
    "evidence_requirements": ["A primary source for each blocking claim."],
    "blocking_claims": ["The proposed mechanism must be evidenced before writing."],
    "required_materials": ["Official rules or original documents."],
    "prohibited_materials": ["Video-platform content and comments as factual evidence."],
    "candidate_content_routes": ["Start from the audience's concrete confusion."],
    "stop_conditions": ["Stop when the blocking claim is verified or disproved."],
    "budget_boundary": {"max_sources": 6, "max_time_minutes": 45},
    "risks_uncertainties": ["Available sources may not establish causality."],
}


class FakeResearchPlanProvider:
    provider_name = "test-mimo"

    def __init__(self, *, output: object = VALID_PLAN):
        self.output = output
        self.calls = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.calls += 1
        text = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return ModelProviderResult(
            output_text=text,
            usage=ModelUsage(prompt_tokens=11, completion_tokens=13, total_tokens=24),
            cost={"status": "test"},
            metadata={"test_identity": True, "retry_count": 0},
        )


class Stage1AResearchPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "stage1a.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        self.provider = FakeResearchPlanProvider()
        route = ModelRoute(
            route_name="stage0.research_plan",
            provider_name=self.provider.provider_name,
            model_name="test-mimo-v1",
            config_version="test.v1",
            config_hash="test-config",
            route_id="business_analysis",
            provider_ref="test_mimo",
        )
        self.gateway = ModelGateway(
            routes={route.route_name: route},
            providers={self.provider.provider_name: self.provider},
            materializer=CoreModelRunMaterializer(self.core),
        )
        self.service = Stage1AResearchPlanService(core=self.core, gateway=self.gateway)

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    def _submitted_topic(self) -> dict[str, str]:
        return self.service.submit_formal_topic(
            topic_payload={
                "title": "Everyday pattern",
                "core_question": "Why does the pattern persist?",
                "domain": "fan_kepu_social_life",
                "source_refs": [{"kind": "user_direction", "reference": "user supplied direction"}],
            },
            actor="user-1",
            reason="I want to consider this topic",
            idempotency_key="topic-1",
        )

    def _confirmed_topic(self) -> dict[str, str]:
        topic = self._submitted_topic()
        self.service.confirm_formal_topic(
            task_id=topic["task_id"],
            topic_version_id=topic["topic_version_id"],
            actor="user-1",
            reason="confirmed topic",
            idempotency_key="topic-confirm-1",
        )
        return topic

    def _plan(self) -> tuple[dict[str, str], dict[str, str]]:
        topic = self._confirmed_topic()
        plan = self.service.generate_research_plan(
            task_id=topic["task_id"],
            user_requirements="Focus on rule boundaries and uncertainty.",
            actor="stage1a-worker",
            idempotency_key="plan-1",
        )
        return topic, plan

    def test_unconfirmed_topic_cannot_generate_research_plan(self) -> None:
        topic = self._submitted_topic()
        with self.assertRaisesRegex(StateTransitionError, "research_plan"):
            self.service.generate_research_plan(
                task_id=topic["task_id"], user_requirements="x", actor="worker", idempotency_key="plan-1"
            )
        self.assertEqual(self.provider.calls, 0)

    def test_topic_versions_and_payloads_are_immutable_and_idempotent(self) -> None:
        first = self._submitted_topic()
        replay = self._submitted_topic()
        self.assertEqual(first, replay)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_content_task").fetchone()[0], 1)
        with self.assertRaises(sqlite3.DatabaseError):
            self.core.conn.execute("UPDATE stage0_content_node_version SET status='approved' WHERE version_id=?", (first["topic_version_id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.core.conn.execute("UPDATE stage1a_artifact_payload SET payload_json='{}' WHERE version_id=?", (first["topic_version_id"],))

    def test_research_plan_references_exact_confirmed_topic_and_awaits_human_review(self) -> None:
        topic, plan = self._plan()
        version = self.core._version(plan["node_version_id"])
        task = self.core.get_task(topic["task_id"])
        self.assertEqual(version["upstream_version_id"], topic["topic_version_id"])
        self.assertEqual(task["current_status"], "awaiting_human_review")
        self.assertEqual(task["current_node"], "research_plan")
        self.assertIsNotNone(self.service.view_artifact(version_id=plan["node_version_id"])["payload"])
        approved = self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_content_decision WHERE version_id=? AND decision='approved'",
            (plan["node_version_id"],),
        ).fetchone()[0]
        self.assertEqual(approved, 0)

    def test_same_generation_key_is_idempotent_without_a_second_model_request(self) -> None:
        topic = self._confirmed_topic()
        first = self.service.generate_research_plan(
            task_id=topic["task_id"], user_requirements="Focus on rule boundaries.", actor="worker", idempotency_key="plan-idempotent"
        )
        replay = self.service.generate_research_plan(
            task_id=topic["task_id"], user_requirements="Focus on rule boundaries.", actor="worker", idempotency_key="plan-idempotent"
        )
        self.assertEqual(first, replay)
        self.assertEqual(self.provider.calls, 1)

    def test_invalid_model_output_never_writes_a_formal_research_plan(self) -> None:
        self.provider.output = {"core_question": "incomplete"}
        topic = self._confirmed_topic()
        with self.assertRaises(ResearchPlanValidationError):
            self.service.generate_research_plan(
                task_id=topic["task_id"], user_requirements="x", actor="worker", idempotency_key="plan-invalid"
            )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage1a_artifact_payload WHERE artifact_kind='research_plan'").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.core.conn.execute("SELECT validation_status FROM stage0_model_run").fetchone()[0],
            "failed",
        )

    def test_non_json_model_output_never_writes_a_formal_research_plan(self) -> None:
        self.provider.output = "not-json"
        topic = self._confirmed_topic()
        with self.assertRaisesRegex(ResearchPlanValidationError, "valid JSON"):
            self.service.generate_research_plan(
                task_id=topic["task_id"], user_requirements="x", actor="worker", idempotency_key="plan-non-json"
            )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage1a_artifact_payload WHERE artifact_kind='research_plan'").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.core.conn.execute("SELECT validation_status FROM stage0_model_run").fetchone()[0],
            "failed",
        )

    def test_return_preserves_old_plan_creates_new_version_and_rejects_stale_review(self) -> None:
        topic, first = self._plan()
        replacement = self.service.return_research_plan(
            task_id=topic["task_id"],
            research_plan_version_id=first["node_version_id"],
            modification_requirements="Narrow the scope and state the counterarguments.",
            actor="user-1",
            idempotency_key="plan-return-1",
        )
        with self.assertRaisesRegex(StateTransitionError, "not .*awaiting_human_review|not research_plan"):
            self.service.approve_research_plan(
                task_id=topic["task_id"], research_plan_version_id=first["node_version_id"], actor="user-1", reason="late", idempotency_key="late"
            )
        request_version = self.core._version(replacement["node_version_id"])
        self.assertEqual(request_version["parent_version_id"], first["node_version_id"])
        with self.assertRaisesRegex(StateTransitionError, "input is frozen"):
            self.service.generate_research_plan(
                task_id=topic["task_id"], user_requirements="silently replace the returned input", actor="worker", idempotency_key="plan-return-conflict"
            )
        second = self.service.generate_research_plan(
            task_id=topic["task_id"], user_requirements=None, actor="worker", idempotency_key="plan-return-run"
        )
        self.assertNotEqual(first["node_version_id"], second["node_version_id"])
        self.assertEqual(self.core.get_artifact_payload(first["node_version_id"])["payload"], VALID_PLAN)

    def test_unapproved_plan_cannot_start_deep_research(self) -> None:
        topic, plan = self._plan()
        assembly = InputAssembly(
            task_id=topic["task_id"], node="deep_research", upstream_version_id=plan["node_version_id"],
            user_requirements="must not start", material_refs=(), research_refs=(), content_plan_ref=None,
            considered_experience=(), adopted_experience=(), rejected_experience=(), omitted_materials=(),
            prompt_version="test", skill_version="test", model_config_version="test",
        )
        with self.assertRaises(StateTransitionError):
            self.core.create_input_assembly(assembly, idempotency_key="deep-assembly")

    def test_test_identity_can_never_open_the_formal_database(self) -> None:
        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="test")

    def test_cancel_is_explicit_and_does_not_create_a_plan(self) -> None:
        topic = self._submitted_topic()
        cancelled = self.service.cancel_task(
            task_id=topic["task_id"], actor="user-1", reason="do not proceed", idempotency_key="cancel-1"
        )
        self.assertEqual(cancelled["task_id"], topic["task_id"])
        self.assertEqual(self.core.get_task(topic["task_id"])["current_status"], "cancelled")
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage1a_artifact_payload WHERE artifact_kind='research_plan'").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
