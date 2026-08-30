from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.external_adapters.anysearch_executor import RESEARCH_EXECUTION_VERSION
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StaleResultError, StateTransitionError
from scripts.web.read_only_server import create_action_server
from tests.test_core_unified_status import _seed_domain


def _research_plan_output() -> dict[str, Any]:
    return {
        "research_objective": "建立测试主题的事实研究依据",
        "research_scope": {"included": ["测试主题"], "excluded": ["无关主题"]},
        "research_sequence": ["先确认事实，再形成研究结论"],
        "research_questions": ["测试主题的关键事实是什么"],
        "source_plan": ["只使用已保留的测试来源"],
        "required_outputs": ["事实结论和未决问题"],
    }


def _content_output(node: str) -> dict[str, Any]:
    if node == "deep_research":
        document: dict[str, Any] = {
            "subject": "测试主题",
            "timeline": ["测试时间线"],
            "career_stages": ["测试阶段"],
            "representative_works": ["测试作品"],
            "turning_points": ["测试转折"],
            "historical_context": ["测试背景"],
            "public_memory": ["测试记忆"],
            "current_status": ["测试现状"],
            "source_map": ["material_01"],
        }
    elif node == "content_plan":
        document = {"outline": "开头、事实、结论", "key_points": ["只使用已确认材料"]}
    else:
        document = {"title": "测试内容", "script_text": "这是一段测试内容。"}
        if node == "review":
            document.update({"decision": "accepted_for_next_core_rule", "issues": []})
    return {
        "node": node,
        "document": document,
        "source_boundaries": ["只使用本次测试提供的材料"],
        "unresolved": ["没有未决问题"],
    }


def _nested_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_keys(child)


class _TestAnySearchExecutor:
    def __init__(self, *, core: Stage0ContentProductionCore) -> None:
        self.core = core

    def run(
        self,
        *,
        task_id: str,
        topic: dict[str, Any],
        approved_plan: dict[str, Any],
        user_requirements: str,
    ) -> dict[str, Any]:
        del topic, approved_plan, user_requirements
        return self.core.record_research_material(
            task_id=task_id,
            source_ref="https://example.org/test-research",
            title="测试研究来源",
            evidence_role="fact_evidence",
            material={
                "provider": "test-fixture",
                "research_execution": RESEARCH_EXECUTION_VERSION,
                "research_plan_fingerprint": "test-fixture",
                "research_task_id": "research_task_01",
                "research_task_label": "测试研究任务",
                "research_task_objective": "建立测试主题的事实研究依据",
                "research_task_question": "测试主题的关键事实是什么",
                "research_sequence_context": "先确认事实，再形成研究结论",
                "research_source_guidance": "只使用已保留的测试来源",
                "extracted_content": "测试来源中的保留内容",
                "retrieved_at": "2026-08-30T00:00:00+00:00",
            },
            collected_at="2026-08-30T00:00:00+00:00",
        )


class FormalProductionModeChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "formal-production.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        _seed_domain(self.core, "music_entertainment", cold_status="running")
        self.core.conn.commit()
        self.business = CreationAssistantFormalBusinessCore(core=self.core)

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    def _create_research_task(self) -> tuple[str, dict[str, Any]]:
        result = self.business.execute_formal_research(
            action="create_plan",
            payload={
                "domain_label": "music_entertainment",
                "account_ref": "douyin:owned-music_entertainment",
                "core_question": "测试主题为什么值得研究",
                "scope_or_requirement": "只验证测试主题的事实依据",
                "original_instruction": "请围绕测试主题形成一条生产内容",
                "actor": "test-user",
                "user_requirements": "围绕测试主题制定研究计划",
                "idempotency_key": "mode-chain:create-plan",
            },
            external_executor=None,
        )
        return str(result["task_id"]), result

    @staticmethod
    def _external_executor(received: list[dict[str, Any]]):
        counter = 0

        def execute(task: dict[str, Any]) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            received.append(task)
            node = str(task["business_context"]["node"])
            submission: dict[str, Any] = {
                "execution_id": f"test-execution-{counter}",
                "executor_id": "test-executor",
                "model_ref": f"executor-current-model-{counter}",
                "submitted_at": "2026-08-30T00:00:00+00:00",
                "output": _research_plan_output() if node == "research_plan" else _content_output(node),
            }
            if node == "content_plan":
                submission["experience_usage"] = {
                    "adopted_experience_ids": [],
                    "not_adopted_experience_ids": [],
                    "rationale": "本测试没有提供正式经验",
                }
            return submission

        return execute

    def _approve_current(
        self,
        task_id: str,
        reason: str,
        executor,
        key: str,
    ) -> dict[str, Any]:
        task = self.core.get_task(task_id)
        return self.business.approve_formal_production_node(
            task_id=task_id,
            version_id=str(task["current_version_id"]),
            actor="test-user",
            reason=reason,
            user_requirements=reason,
            idempotency_key=key,
            external_executor=executor,
        )

    @patch("scripts.core.production.stage1c_content_pipeline.AnySearchExecutor", _TestAnySearchExecutor)
    def test_manual_guard_mode_runs_one_task_to_final_confirmation(self) -> None:
        task_id, created = self._create_research_task()
        self.assertEqual(created["research_plan_status"], "requires_external_intelligence")
        self.assertEqual(self.core.get_task(task_id)["current_status"], "processing")

        submitted = self.business.submit_formal_external_result(
            task_id=task_id,
            node_version_id=str(created["research_plan_version_id"]),
            execution_id="test-research-plan-execution",
            executor_id="test-executor",
            model_ref="executor-current-model",
            submitted_at="2026-08-30T00:00:00+00:00",
            output=_research_plan_output(),
            actor="test-executor",
            idempotency_key="mode-chain:submit-research-plan",
        )
        self.assertEqual(submitted["continuation"]["current_node"], "research_plan")
        self.assertEqual(self.core.get_task(task_id)["current_status"], "awaiting_human_review")

        received: list[dict[str, Any]] = []
        executor = self._external_executor(received)
        expected_gates = ("research_plan", "deep_research", "content_plan", "formal_draft", "review")
        for index, node in enumerate(expected_gates):
            task = self.core.get_task(task_id)
            self.assertEqual(task["current_node"], node)
            self.assertEqual(task["current_status"], "awaiting_human_review")
            result = self._approve_current(task_id, f"确认{node}测试结果", executor, f"mode-chain:approve-{index}")
            self.assertEqual(result["decision"]["task_id"], task_id)

        final = self.core.get_task(task_id)
        self.assertEqual(final["current_node"], "user_final_confirmation")
        self.assertEqual(final["current_status"], "approved")
        self.assertEqual(
            [str(task["business_context"]["node"]) for task in received],
            ["deep_research", "content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"],
        )
        self.assertTrue(all(task["business_context"]["task_id"] == task_id for task in received))
        forbidden = {"model", "model_name", "provider", "provider_name", "model_route"}
        for task in received:
            control_plane = {
                "task_type": task.get("task_type"),
                "skill": task.get("skill"),
                "constraints": task.get("constraints"),
                "business_context": task.get("business_context"),
            }
            self.assertFalse(forbidden.intersection(_nested_keys(control_plane)))

    @patch("scripts.core.production.stage1c_content_pipeline.AnySearchExecutor", _TestAnySearchExecutor)
    def test_mature_automatic_mode_only_releases_deep_research_and_draft(self) -> None:
        task_id, created = self._create_research_task()
        self.business.submit_formal_external_result(
            task_id=task_id,
            node_version_id=str(created["research_plan_version_id"]),
            execution_id="mature-research-plan-execution",
            executor_id="test-executor",
            model_ref="executor-current-model",
            submitted_at="2026-08-30T00:00:00+00:00",
            output=_research_plan_output(),
            actor="test-executor",
            idempotency_key="mature-chain:submit-research-plan",
        )
        received: list[dict[str, Any]] = []
        executor = self._external_executor(received)
        with patch("scripts.core.production.stage0_content_core.get_content_workflow_mode", return_value="mature_automatic"), patch(
            "scripts.core.production.stage1c_content_pipeline.get_content_workflow_mode",
            return_value="mature_automatic",
        ):
            approved = self._approve_current(task_id, "确认研究计划", executor, "mature-chain:approve-plan")
            self.assertEqual(approved["continuation"]["current_node"], "content_plan")
            self.assertEqual(self.core.get_task(task_id)["current_status"], "awaiting_human_review")
            self._approve_current(task_id, "确认内容计划", executor, "mature-chain:approve-content-plan")
            self.assertEqual(self.core.get_task(task_id)["current_node"], "review")
            self.assertEqual(self.core.get_task(task_id)["current_status"], "awaiting_human_review")
            final = self._approve_current(task_id, "确认审核结果", executor, "mature-chain:approve-review")

        self.assertEqual(final["continuation"]["current_node"], "user_final_confirmation")
        self.assertEqual(self.core.get_task(task_id)["current_status"], "approved")
        self.assertEqual(
            [str(task["business_context"]["node"]) for task in received],
            ["deep_research", "content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"],
        )

    def test_stale_version_cannot_be_used_to_advance_the_same_task(self) -> None:
        task_id, created = self._create_research_task()
        self.business.submit_formal_external_result(
            task_id=task_id,
            node_version_id=str(created["research_plan_version_id"]),
            execution_id="stale-test-execution",
            executor_id="test-executor",
            model_ref="executor-current-model",
            submitted_at="2026-08-30T00:00:00+00:00",
            output=_research_plan_output(),
            actor="test-executor",
            idempotency_key="stale-test:submit",
        )
        with self.assertRaises(StaleResultError):
            self.business.approve_formal_production_node(
                task_id=task_id,
                version_id=str(created["research_plan_version_id"]),
                actor="test-user",
                reason="使用旧版本必须被拒绝",
                idempotency_key="stale-test:approve-old-version",
            )

    def test_task_from_released_activation_cannot_be_advanced(self) -> None:
        task_id, created = self._create_research_task()
        self.business.submit_formal_external_result(
            task_id=task_id,
            node_version_id=str(created["research_plan_version_id"]),
            execution_id="released-activation-execution",
            executor_id="test-executor",
            model_ref="executor-current-model",
            submitted_at="2026-08-30T00:00:00+00:00",
            output=_research_plan_output(),
            actor="test-executor",
            idempotency_key="released-activation:submit",
        )
        self.core.reset_domain(domain_label="music_entertainment", actor="test-user")
        with self.assertRaisesRegex(StateTransitionError, "historical or non-current"):
            self.business.approve_formal_production_node(
                task_id=task_id,
                actor="test-user",
                reason="已释放激活不应继续",
                idempotency_key="released-activation:approve",
            )

    def test_web_reads_and_operates_the_waiting_gate_through_core(self) -> None:
        task_id, created = self._create_research_task()
        self.business.submit_formal_external_result(
            task_id=task_id,
            node_version_id=str(created["research_plan_version_id"]),
            execution_id="web-test-execution",
            executor_id="test-executor",
            model_ref="executor-current-model",
            submitted_at="2026-08-30T00:00:00+00:00",
            output=_research_plan_output(),
            actor="test-executor",
            idempotency_key="web-test:submit",
        )
        received: list[dict[str, Any]] = []
        executor = self._external_executor(received)
        with patch("scripts.core.production.stage1c_content_pipeline.AnySearchExecutor", _TestAnySearchExecutor):
            self._approve_current(task_id, "确认研究计划", executor, "web-test:approve-plan")
            self._approve_current(task_id, "确认实际研究结果", executor, "web-test:approve-research")
        self.assertEqual(self.core.get_task(task_id)["current_node"], "content_plan")
        self.assertEqual(self.core.get_task(task_id)["current_status"], "awaiting_human_review")
        server = create_action_server(
            host="127.0.0.1",
            port=0,
            data_identity="test",
            database_path=self.database,
            actor="test-web-user",
            carrier_binding_id="test-web-carrier",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urlopen(base + "/api/content-tasks") as response:
                tasks = json.loads(response.read().decode("utf-8"))
            self.assertTrue(tasks["ok"])
            self.assertEqual([item["task_id"] for item in tasks["tasks"]], [task_id])

            request = Request(
                base + "/api/action",
                data=json.dumps(
                    {"action": "approve_content_node", "task_id": task_id, "reason": "确认研究计划"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["core_result"]["decision"]["current_node"], "formal_draft")
            self.assertEqual(self.core.get_task(task_id)["current_node"], "formal_draft")
            self.assertEqual(self.core.get_task(task_id)["current_status"], "processing")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
