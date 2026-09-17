from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.core.business_data.domain_labels import require_frozen_content_type_registry
from scripts.core.model_gateway.formal_skill_adapter import FormalSkillValidationError
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage0_content_core import StateTransitionError
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService
from scripts.mcp.creation_assistant_mcp_server import (
    CreationAssistantMcpApplication,
    CreationAssistantMcpError,
)
from tests.test_stage2a_external_intelligence_boundary import _valid_source_to_topic_output


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_MODULE = "scripts.mcp.creation_assistant_mcp_server"


class _StdioMcpClient:
    def __init__(self, db_path: Path) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                SERVER_MODULE,
                "--db-path",
                str(db_path),
                "--data-identity",
                "test",
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            stderr = self.process.stderr.read() if self.process.stderr is not None else ""
            raise AssertionError(f"MCP server exited without a response: {stderr}")
        return json.loads(response)

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            # EOF lets the MCP server close Core. On Windows, killing the
            # virtualenv launcher can leave its Python child holding SQLite.
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


class CreationAssistantMcpStage2B1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "stage2b1.sqlite3"
        core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        try:
            self.identifiers = self._prepare_external_source(core, "mcp")
            self.codex_identifiers = self._prepare_external_source(
                core, "mcp-codex", run_id=self.identifiers["run_id"]
            )
        finally:
            core.close()
        self.client = _StdioMcpClient(self.db_path)
        self.client.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "isolated-test", "version": "1"}},
        )
        self.client.notify("notifications/initialized")

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    @staticmethod
    def _prepare_external_source(
        core: Stage0ContentProductionCore,
        suffix: str,
        run_id: str | None = None,
        material_payload: dict | None = None,
    ) -> dict[str, str]:
        run = (
            {"run_id": run_id}
            if run_id is not None
            else core.create_discovery_run(
                discovery_date="2026-08-28",
                actor="isolated-mcp-test",
                execution_mode="test_isolated",
                domains=("music_entertainment",),
                idempotency_key=f"stage2b1-run:{suffix}",
            )
        )
        direction = core.register_saved_user_direction_source(
            direction_id=f"direction-{suffix}",
            domain_label="music_entertainment",
            core_question="一个足够长的来源问题",
            submitted_by="isolated-mcp-test",
        )
        source_version = core.record_discovery_source(
            run_id=run["run_id"],
            domain_label="music_entertainment",
            source_type="saved_user_direction",
            source_object_id=direction["direction_id"],
            source_object_version=direction["source_object_version"],
            source_time=direction["saved_at"],
            expires_at=None,
            payload={
                "title": "一个足够长的来源问题",
                "core_question": "一个足够长的来源问题",
                "url": "",
                "account_name": "用户保存方向",
                **(material_payload or {}),
                "formal_source": {
                    "table": "stage1_saved_user_direction_source",
                    "object_id": direction["direction_id"],
                    "object_version": direction["source_object_version"],
                    "raw_metadata_hash": direction["source_object_version"],
                },
            },
            idempotency_key=f"stage2b1-source:{suffix}",
        )
        core.record_discovery_filter(
            source_version_id=source_version["source_version_id"],
            outcome="eligible",
            reason_code="eligible",
            detail={},
            idempotency_key=f"stage2b1-filter:{suffix}",
        )
        source_payload = {
            "source_type": "saved_user_direction",
            "source_object_id": direction["direction_id"],
            "source_object_version": direction["source_object_version"],
            "source_time": direction["saved_at"],
            "payload": {
                "title": "一个足够长的来源问题",
                "core_question": "一个足够长的来源问题",
                "url": "",
            },
            "source_version_id": source_version["source_version_id"],
        }
        assembly_payload = Stage1BDailyDiscoveryService._assembly_payload(
            run_id=run["run_id"],
            domain_label="music_entertainment",
            source=source_payload,
            source_version_id=source_version["source_version_id"],
            experience_cards=[],
        )
        assembly = core.create_discovery_input_assembly(
            run_id=run["run_id"],
            source_version_id=source_version["source_version_id"],
            payload=assembly_payload,
            prompt_version="source_to_topic.prompt.v3",
            skill_version="source_to_topic.skill.v2.0.0",
            idempotency_key=f"stage2b1-assembly:{suffix}",
        )
        return {
            "run_id": run["run_id"],
            "source_version_id": source_version["source_version_id"],
            "assembly_id": assembly["assembly_id"],
        }

    def _call_tool(self, name: str, arguments: dict) -> dict:
        response = self.client.request("tools/call", {"name": name, "arguments": arguments})
        self.assertNotIn("error", response)
        result = response["result"]
        if result["isError"]:
            self.fail(result["content"][0]["text"])
        return result["structuredContent"]

    @staticmethod
    def _generic_source_identity(identifiers: dict[str, str]) -> dict:
        return {
            "task_type": "source_to_topic",
            "task_identity": dict(identifiers),
        }

    def test_generic_task_identity_protocol_uses_the_existing_source_boundary(self) -> None:
        task_response = self._call_tool(
            "creation_assistant_get_external_task",
            self._generic_source_identity(self.identifiers),
        )
        task = task_response["task"]
        self.assertEqual(task["task_type"], "source_to_topic")
        self.assertEqual(task["task_identity"], self.identifiers)
        submitted = self._call_tool(
            "creation_assistant_submit_external_result",
            {
                **self._generic_source_identity(self.identifiers),
                "execution_id": "mcp-generic-execution",
                "executor_id": "generic-test-executor",
                "model_ref": "executor-current-model",
                "output": _valid_source_to_topic_output(
                    task["input"]["source_evidence_refs"][0]
                ),
            },
        )
        self.assertEqual(submitted["status"], "accepted")
        self.assertEqual(submitted["task_type"], "source_to_topic")

    def test_competitor_breakdown_validation_uses_natural_identity_without_persistence(self) -> None:
        registry = require_frozen_content_type_registry("music_entertainment")
        material = {
            "source_id": "source-test",
            "title": "测试内容",
            "transcript": "这是验证当前内容分析边界的测试原文。",
            "metrics": {"views": 7},
            "comments": [],
            "domain_label": "music_entertainment",
            "domain_context": {
                "content_type_lifecycle": "classify",
                "content_type_registry": registry,
                "observed_content_types": [],
            },
        }
        output_payload = {
            "source_id": "source-test",
            "source_content_type": "作品背景说明",
            "matched_source_content_type": registry["types"][0]["canonical_id"],
            "analysis_text": (
                "内容围绕材料中的对象和命题展开，先交付必要背景，再说明实际推进动作及其关系；"
                "有限参考只保留当前材料能够支持的做法。"
            ),
            "schema_version": "competitor_breakdown.output.raw.v5",
        }
        core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        app = CreationAssistantMcpApplication(core)
        try:
            with patch(
                "scripts.mcp.creation_assistant_mcp_server.read_formal_competitor_breakdown_material",
                return_value=material,
            ) as read_material:
                result = app.call_tool(
                    "creation_assistant_competitor_breakdown_validation",
                    {
                        "operation": "prepare",
                        "platform": "douyin",
                        "platform_item_id": "source-test",
                    },
                )
                self.assertEqual(result["status"], "ready")
                self.assertFalse(result["formal_business_data_written"])
                self.assertNotIn("task_identity", result["task"])
                self.assertNotIn("business_context", result["task"])
                read_material.assert_called_once_with(
                    platform="douyin",
                    platform_item_id="source-test",
                )
                self.assertEqual(result["task"]["input"]["source_id"], "source-test")
                self.assertEqual(
                    result["task"]["skill"]["source_reference"],
                    "runtime_skills/competitor_breakdown",
                )

            before_changes = core.conn.total_changes
            with patch(
                "scripts.mcp.creation_assistant_mcp_server.read_formal_competitor_breakdown_material",
                return_value=material,
            ) as read_material:
                result = app.call_tool(
                    "creation_assistant_competitor_breakdown_validation",
                    {
                        "operation": "validate",
                        "platform": "douyin",
                        "platform_item_id": "source-test",
                        "model_result": output_payload,
                    },
                )
                self.assertEqual(result["status"], "valid")
                self.assertFalse(result["formal_business_data_written"])
                self.assertEqual(result["validated_output"], output_payload)
                read_material.assert_called_once_with(
                    platform="douyin",
                    platform_item_id="source-test",
                )

                for invalid_output in (
                    {
                        **output_payload,
                        "analysis_text": "",
                    },
                    {
                        **output_payload,
                        "matched_source_content_type": "not_in_current_registry",
                    },
                    {**output_payload, "question_expansions": []},
                ):
                    with self.assertRaises(FormalSkillValidationError):
                        app.call_tool(
                            "creation_assistant_competitor_breakdown_validation",
                            {
                                "operation": "validate",
                                "platform": "douyin",
                                "platform_item_id": "source-test",
                                "model_result": invalid_output,
                            },
                        )
                self.assertEqual(core.conn.total_changes, before_changes)

            for invalid_arguments in (
                {
                    "operation": "prepare",
                    "platform": "douyin",
                    "platform_item_id": "source-test",
                    "registration_id": "internal-only",
                },
                {"operation": "prepare", "platform": "douyin"},
                {
                    "operation": "validate",
                    "platform": "douyin",
                    "platform_item_id": "source-test",
                },
            ):
                with self.assertRaises(CreationAssistantMcpError):
                    app.call_tool(
                        "creation_assistant_competitor_breakdown_validation",
                        invalid_arguments,
                    )

            with patch(
                "scripts.mcp.creation_assistant_mcp_server.read_formal_competitor_breakdown_material",
                side_effect=StateTransitionError("没有找到具备完整口播文案的正式竞品材料"),
            ):
                with self.assertRaisesRegex(
                    CreationAssistantMcpError,
                    "没有找到具备完整口播文案",
                ):
                    app.call_tool(
                        "creation_assistant_competitor_breakdown_validation",
                        {
                            "operation": "prepare",
                            "platform": "douyin",
                            "platform_item_id": "missing-source",
                        },
                    )

            with patch(
                "scripts.mcp.creation_assistant_mcp_server.read_formal_competitor_breakdown_material",
                side_effect=StateTransitionError("平台作品对应多个正式竞品材料上下文，无法安全选择"),
            ):
                with self.assertRaisesRegex(
                    CreationAssistantMcpError,
                    "多个正式竞品材料",
                ):
                    app.call_tool(
                        "creation_assistant_competitor_breakdown_validation",
                        {
                            "operation": "prepare",
                            "platform": "douyin",
                            "platform_item_id": "ambiguous-source",
                        },
                    )
        finally:
            app.close()

    def test_real_stdio_mcp_flow_uses_one_core_skill_and_structured_submission(self) -> None:
        tools = self.client.request("tools/list")
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {
                "creation_assistant_status",
                "creation_assistant_hotspot_report",
                "creation_assistant_competitor_breakdown_validation",
                "creation_assistant_get_external_task",
                "creation_assistant_submit_external_result",
                "creation_assistant_get_external_result",
            },
        )
        validation_tool = next(
            tool
            for tool in tools["result"]["tools"]
            if tool["name"] == "creation_assistant_competitor_breakdown_validation"
        )
        self.assertEqual(
            set(validation_tool["inputSchema"]["properties"]),
            {"operation", "platform", "platform_item_id", "model_result"},
        )
        self.assertEqual(
            validation_tool["inputSchema"]["required"],
            ["operation", "platform", "platform_item_id"],
        )

        status = self._call_tool("creation_assistant_status", {})
        self.assertEqual(status["business_state_owner"], "Creation Assistant Core")
        self.assertEqual(status["mcp_state"], "none")
        self.assertEqual(status["mcp_database"], None)
        self.assertEqual(
            status["external_task_boundary"]["task_types"],
            [
                "candidate_priority",
                "source_to_topic",
                "competitor_breakdown",
                "research_plan",
                "content_deep_research",
                "content_plan_generation",
                "formal_draft_generate",
                "copy_optimization",
                "de_ai_revision",
                "final_content_review",
                "experience_candidate_propose",
                "domain_boundary_proposal",
            ],
        )

        task_response = self._call_tool("creation_assistant_get_external_task", self.identifiers)
        task = task_response["task"]
        self.assertEqual(task["task_type"], "source_to_topic")
        self.assertEqual(task["skill"]["source_reference"], "runtime_skills/source_to_topic")
        self.assertTrue(task["constraints"]["cannot_change_business_state"])
        self.assertEqual(task["output_requirements"]["submission"], "structured_fields")
        forbidden = {"model", "model_name", "provider", "provider_name", "model_route"}
        self.assertFalse(forbidden.intersection(task))

        source_ref = task["input"]["source_evidence_refs"][0]
        submitted = self._call_tool(
            "creation_assistant_submit_external_result",
            {
                **self.identifiers,
                "execution_id": "mcp-hermes-isolated-execution",
                "executor_id": "Hermes",
                "model_ref": "hermes-isolated-test-model",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "output": _valid_source_to_topic_output(source_ref),
            },
        )
        self.assertEqual(submitted["status"], "accepted")
        self.assertEqual(submitted["business_state_changed_by"], "Creation Assistant Core")
        self.assertTrue(submitted["validated_output"]["execution_review"]["respected_domain_boundary"])

        persisted = self._call_tool("creation_assistant_get_external_result", self.identifiers)
        self.assertEqual(persisted["result"]["executor_id"], "Hermes")
        self.assertEqual(persisted["result"]["via_model_gateway"], False)
        self.assertEqual(persisted["candidate_count"], 1)
        self.assertEqual(persisted["result"]["validation_status"], "passed")

    def test_invalid_structured_submission_is_rejected_by_core(self) -> None:
        task = self._call_tool("creation_assistant_get_external_task", self.identifiers)["task"]
        response = self.client.request(
            "tools/call",
            {
                "name": "creation_assistant_submit_external_result",
                "arguments": {
                    **self.identifiers,
                    "execution_id": "mcp-invalid-execution",
                    "executor_id": "Codex",
                    "model_ref": "codex-isolated-test-model",
                    "output": {"topic_status": "generated"},
                },
            },
        )
        self.assertFalse(response["result"]["isError"] is False)
        self.assertIn("error", json.loads(response["result"]["content"][0]["text"]))
        persisted = self._call_tool("creation_assistant_get_external_result", self.identifiers)
        self.assertEqual(persisted["result"]["validation_status"], "failed")
        self.assertEqual(persisted["candidate_count"], 0)
        self.assertEqual(task["task_type"], "source_to_topic")

    def test_hermes_and_codex_use_the_same_core_business_run(self) -> None:
        first_task = self._call_tool(
            "creation_assistant_get_external_task", self.identifiers
        )["task"]
        first = self._call_tool(
            "creation_assistant_submit_external_result",
            {
                **self.identifiers,
                "execution_id": "mcp-hermes-execution",
                "executor_id": "Hermes",
                "model_ref": "hermes-current-isolated-model",
                "output": _valid_source_to_topic_output(
                    first_task["input"]["source_evidence_refs"][0]
                ),
            },
        )
        second_task = self._call_tool(
            "creation_assistant_get_external_task", self.codex_identifiers
        )["task"]
        second = self._call_tool(
            "creation_assistant_submit_external_result",
            {
                **self.codex_identifiers,
                "execution_id": "mcp-codex-execution",
                "executor_id": "Codex",
                "model_ref": "codex-current-isolated-model",
                "output": _valid_source_to_topic_output(
                    second_task["input"]["source_evidence_refs"][0]
                ),
            },
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "accepted")
        first_result = self._call_tool(
            "creation_assistant_get_external_result", self.identifiers
        )
        second_result = self._call_tool(
            "creation_assistant_get_external_result", self.codex_identifiers
        )
        self.assertEqual(first_result["run_id"], second_result["run_id"])
        self.assertEqual(first_result["result"]["executor_id"], "Hermes")
        self.assertEqual(second_result["result"]["executor_id"], "Codex")
        self.assertEqual(first_result["result"]["via_model_gateway"], False)
        self.assertEqual(second_result["result"]["via_model_gateway"], False)


if __name__ == "__main__":
    unittest.main()
