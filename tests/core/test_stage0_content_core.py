from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelUsage,
)
from scripts.core.production.stage0_content_core import (
    CoreModelRunMaterializer,
    DataIdentityError,
    InputAssembly,
    ModelBindingUnavailableError,
    ModelGatewayRequiredError,
    Stage0ContentProductionCore,
    StaleResultError,
    StateTransitionError,
)


class TestProvider:
    provider_name = "test_mimo"

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        return ModelProviderResult("{}", ModelUsage(3, 2, 5), {"status": "test"}, "test-request")


class Stage0ContentCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "stage0_test.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.core.close)

    def _task(self) -> dict[str, str]:
        return self.core.create_task(
            topic_payload={"topic": "test topic"}, actor="user-1", actor_kind="user", reason="confirmed", idempotency_key="task-1"
        )

    def _assembly(self, task_id: str, node: str, upstream_version_id: str, key: str) -> dict[str, str]:
        return self.core.create_input_assembly(
            InputAssembly(
                task_id=task_id,
                node=node,
                upstream_version_id=upstream_version_id,
                user_requirements="test-only requirement",
                material_refs=({"version_id": upstream_version_id, "reason": "required"},),
                research_refs=(),
                content_plan_ref=None,
                considered_experience=({"experience_id": "candidate", "reason": "considered"},),
                adopted_experience=(),
                rejected_experience=({"experience_id": "candidate", "reason": "not applicable"},),
                omitted_materials=({"material_id": "omitted", "reason": "budget"},),
                prompt_version="stage0.prompt.v1",
                skill_version="stage0.skill.v1",
                model_config_version="model_routes.v1",
            ),
            idempotency_key=key,
        )

    def _gateway_result(self, task_id: str, node: str, version_id: str, assembly_id: str, revision: int) -> str:
        route = ModelRoute(
            route_name=f"stage0.{node}", provider_name="test_mimo", model_name="test-mimo", config_version="test.v1", config_hash="test"
        )
        gateway = ModelGateway(
            routes={route.route_name: route}, providers={"test_mimo": TestProvider()}, materializer=CoreModelRunMaterializer(self.core)
        )
        result = gateway.complete(
            ModelRequest(
                route_name=route.route_name,
                prompt="test prompt",
                input_payload={"test": True},
                correlation_id=task_id,
                metadata={
                    "stage0_core": {
                        "task_id": task_id,
                        "node": node,
                        "node_version_id": version_id,
                        "input_assembly_id": assembly_id,
                        "task_revision": revision,
                        "prompt_version": "stage0.prompt.v1",
                        "skill_version": "stage0.skill.v1",
                        "data_identity": "test",
                    }
                },
            )
        )
        return result.envelope_version_id

    def _complete_research_plan(self) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        task = self._task()
        assembly = self._assembly(task["task_id"], "research_plan", task["topic_version_id"], "assembly-1")
        request = self.core.create_node_request(
            task_id=task["task_id"], node="research_plan", input_assembly_id=assembly["assembly_id"], actor="worker", idempotency_key="request-1"
        )
        run_id = self._gateway_result(task["task_id"], "research_plan", request["node_version_id"], assembly["assembly_id"], int(request["task_revision"]))
        output = self.core.complete_node_from_model(
            task_id=task["task_id"], node_version_id=request["node_version_id"], model_run_id=run_id, output_ref="test-output-ref",
            validation_status="passed", actor="core", expected_task_revision=int(request["task_revision"]), idempotency_key="output-1"
        )
        return task, assembly, output

    def test_non_production_identity_cannot_open_formal_database(self) -> None:
        from scripts.core.production.stage0_content_core import FORMAL_DB_PATH

        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="fixture")

    def test_upstream_must_be_confirmed_and_adjacent(self) -> None:
        task = self._task()
        with self.assertRaises(StateTransitionError):
            self._assembly(task["task_id"], "content_plan", task["topic_version_id"], "bad-assembly")
        assembly = self._assembly(task["task_id"], "research_plan", task["topic_version_id"], "assembly-1")
        self.core.create_node_request(task_id=task["task_id"], node="research_plan", input_assembly_id=assembly["assembly_id"], actor="worker", idempotency_key="request-1")
        with self.assertRaises(StateTransitionError):
            self._assembly(task["task_id"], "deep_research", task["topic_version_id"], "assembly-2")

    def test_model_output_stops_for_human_approval_and_moves_one_node(self) -> None:
        task, assembly, output = self._complete_research_plan()
        row = self.core.conn.execute("SELECT current_node, current_status FROM stage0_content_task WHERE task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual((row["current_node"], row["current_status"]), ("research_plan", "awaiting_human_review"))
        approved = self.core.approve_current_node(
            task_id=task["task_id"], version_id=output["node_version_id"], actor="reviewer", actor_kind="human", reason="approved", idempotency_key="approval-1"
        )
        self.assertEqual(approved["current_node"], "deep_research")
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_content_node_version WHERE task_id=?", (task["task_id"],)).fetchone()[0], 3)
        payload = self.core.conn.execute("SELECT payload_json FROM stage0_input_assembly WHERE assembly_id=?", (assembly["assembly_id"],)).fetchone()[0]
        self.assertIn("omitted_materials", payload)
        self.assertIn("rejected_experience", payload)

    def test_return_creates_new_version_history_is_immutable_and_old_result_is_stale(self) -> None:
        task, _, output = self._complete_research_plan()
        assembly = self._assembly(task["task_id"], "research_plan", task["topic_version_id"], "assembly-return")
        replacement = self.core.return_current_node(
            task_id=task["task_id"], version_id=output["node_version_id"], input_assembly_id=assembly["assembly_id"], actor="user-1", reason="revise", idempotency_key="return-1"
        )
        parent = self.core.conn.execute("SELECT parent_version_id, status FROM stage0_content_node_version WHERE version_id=?", (replacement["node_version_id"],)).fetchone()
        self.assertEqual(parent["parent_version_id"], output["node_version_id"])
        self.assertEqual(parent["status"], "processing")
        with self.assertRaises(sqlite3.DatabaseError):
            self.core.conn.execute("UPDATE stage0_content_node_version SET status='approved' WHERE version_id=?", (output["node_version_id"],))
        with self.assertRaises(StaleResultError):
            self.core.complete_node_from_model(
                task_id=task["task_id"], node_version_id=output["node_version_id"], model_run_id="missing", output_ref="late",
                validation_status="passed", actor="core", expected_task_revision=0, idempotency_key="late-1"
            )

    def test_idempotency_and_gateway_only_persistence(self) -> None:
        first, replay = self._task(), self._task()
        self.assertEqual(first, replay)
        assembly = self._assembly(first["task_id"], "research_plan", first["topic_version_id"], "assembly-1")
        request = self.core.create_node_request(task_id=first["task_id"], node="research_plan", input_assembly_id=assembly["assembly_id"], actor="worker", idempotency_key="request-1")
        with self.assertRaises(ModelGatewayRequiredError):
            self.core.complete_node_from_model(
                task_id=first["task_id"], node_version_id=request["node_version_id"], model_run_id="not-a-gateway-run", output_ref="output",
                validation_status="passed", actor="core", expected_task_revision=int(request["task_revision"]), idempotency_key="output-1"
            )
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_model_run").fetchone()[0], 0)

    def test_unresolved_model_reference_fails_closed_before_provider_call(self) -> None:
        task = self._task()
        assembly = self._assembly(task["task_id"], "research_plan", task["topic_version_id"], "assembly-1")
        request = self.core.create_node_request(task_id=task["task_id"], node="research_plan", input_assembly_id=assembly["assembly_id"], actor="worker", idempotency_key="request-1")
        with patch("scripts.core.production.stage0_content_core.ModelRouter.resolve_bound_route", side_effect=RuntimeError("unresolved")):
            with self.assertRaises(ModelBindingUnavailableError):
                self.core.prepare_model_request(task_id=task["task_id"], node_version_id=request["node_version_id"], prompt="must not run")

    def test_final_confirmation_is_user_only_and_never_automatic(self) -> None:
        task = self._task()
        upstream = task["topic_version_id"]
        for index, node in enumerate(("research_plan", "deep_research", "content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"), start=1):
            assembly = self._assembly(task["task_id"], node, upstream, f"assembly-{index}")
            request = self.core.create_node_request(task_id=task["task_id"], node=node, input_assembly_id=assembly["assembly_id"], actor="worker", idempotency_key=f"request-{index}")
            run_id = self._gateway_result(task["task_id"], node, request["node_version_id"], assembly["assembly_id"], int(request["task_revision"]))
            output = self.core.complete_node_from_model(
                task_id=task["task_id"], node_version_id=request["node_version_id"], model_run_id=run_id, output_ref=f"output-{index}",
                validation_status="passed", actor="core", expected_task_revision=int(request["task_revision"]), idempotency_key=f"output-{index}"
            )
            if node == "review":
                with self.assertRaises(StateTransitionError):
                    self.core.approve_current_node(task_id=task["task_id"], version_id=output["node_version_id"], actor="worker", actor_kind="human", reason="not user", idempotency_key="review-human")
                self.core.approve_current_node(task_id=task["task_id"], version_id=output["node_version_id"], actor="user-1", actor_kind="user", reason="final confirmation", idempotency_key="review-user")
            else:
                self.core.approve_current_node(task_id=task["task_id"], version_id=output["node_version_id"], actor="reviewer", actor_kind="human", reason="approved", idempotency_key=f"approval-{index}")
            upstream = output["node_version_id"]
        row = self.core.conn.execute("SELECT current_node, current_status FROM stage0_content_task WHERE task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual((row["current_node"], row["current_status"]), ("user_final_confirmation", "approved"))


if __name__ == "__main__":
    unittest.main()
