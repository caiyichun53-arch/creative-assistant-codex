from __future__ import annotations

import unittest
from typing import Any

from scripts.core.production.stage0_content_core import InputAssembly
from scripts.core.production.stage1c_content_pipeline import (
    Stage1CContentPipelineService,
)
from scripts.core.model_gateway.formal_skill_adapter import FormalSkillValidationError


_NEXT_NODE = {
    "content_plan": "formal_draft",
    "formal_draft": "copy_optimization",
    "copy_optimization": "de_ai_revision",
    "de_ai_revision": "review",
    "review": "user_final_confirmation",
}


class _PipelineCore:
    data_identity = "test"

    def __init__(self) -> None:
        self.task = {
            "task_id": "content-task-1",
            "topic_version_id": "topic-1",
            "current_node": "content_plan",
            "current_version_id": "research-1",
            "current_status": "not_started",
            "task_revision": 0,
        }
        self.versions = {
            "research-1": {
                "version_id": "research-1",
                "task_id": "content-task-1",
                "node": "research_plan",
                "upstream_version_id": "topic-1",
                "input_assembly_id": "research-assembly-1",
                "task_revision": 0,
            }
        }
        self.artifacts = {
            "topic-1": {
                "artifact_kind": "formal_topic",
                "payload": {
                    "title": "隔离测试选题",
                    "core_question": "为什么这个选题值得做？",
                    "domain_label": "music_entertainment",
                },
            },
            "research-1": {
                "artifact_kind": "research_plan",
                "payload": {
                    "research_objective": "为测试内容准备研究依据",
                    "research_scope": {"included": ["测试材料"], "excluded": ["无关材料"]},
                    "research_questions": ["需要确认什么？"],
                    "source_plan": ["只使用已批准材料"],
                    "required_outputs": ["研究结论"],
                    "research_sequence": ["先确认材料，再形成结论"],
                },
            }
        }
        self.assemblies: dict[str, dict[str, Any]] = {}
        self.external_runs: list[dict[str, Any]] = []
        self.validation_failures: list[dict[str, Any]] = []
        self._sequence = 0

    def get_task(self, task_id: str) -> dict[str, Any]:
        assert task_id == self.task["task_id"]
        return dict(self.task)

    def get_node_version(self, version_id: str) -> dict[str, Any]:
        return dict(self.versions[version_id])

    def get_input_assembly_payload(self, assembly_id: str) -> dict[str, Any]:
        if assembly_id == "research-assembly-1":
            return {
                "task_id": "content-task-1",
                "node": "research_plan",
                "user_requirements": "完成一条隔离测试内容链",
                "material_refs": [{"kind": "formal_topic", "version_id": "topic-1"}],
                "prompt_version": "research.test.v1",
                "skill_version": "research.test.v1",
            }
        return dict(self.assemblies[assembly_id])

    def get_artifact_payload(self, version_id: str) -> dict[str, Any]:
        return dict(self.artifacts[version_id])

    def list_active_experiences(self, *, domain_label: str, context_text: str, limit: int) -> list[dict[str, Any]]:
        assert domain_label == "music_entertainment"
        assert context_text
        assert limit == 3
        return []

    def create_input_assembly(self, assembly: InputAssembly, *, idempotency_key: str) -> dict[str, str]:
        self._sequence += 1
        assembly_id = f"assembly-{self._sequence}"
        self.assemblies[assembly_id] = assembly.payload()
        return {"assembly_id": assembly_id, "input_integrity_hash": f"hash-{self._sequence}"}

    def create_node_request(
        self, *, task_id: str, node: str, input_assembly_id: str, actor: str, idempotency_key: str
    ) -> dict[str, str]:
        assert task_id == self.task["task_id"]
        assert node == self.task["current_node"]
        self._sequence += 1
        version_id = f"{node}-request-{self._sequence}"
        revision = int(self.task["task_revision"]) + 1
        self.versions[version_id] = {
            "version_id": version_id,
            "task_id": task_id,
            "node": node,
            "upstream_version_id": str(self.task["current_version_id"]),
            "input_assembly_id": input_assembly_id,
            "task_revision": revision,
        }
        self.task.update({
            "current_version_id": version_id,
            "current_status": "processing",
            "task_revision": revision,
        })
        return {"node_version_id": version_id, "task_revision": str(revision)}

    def record_external_node_execution(self, **kwargs: Any) -> str:
        self.external_runs.append(dict(kwargs))
        return f"external-run-{len(self.external_runs)}"

    def record_model_validation_failure(self, **kwargs: Any) -> None:
        self.validation_failures.append(dict(kwargs))

    def complete_node_from_external_result(self, **kwargs: Any) -> dict[str, str]:
        node_version_id = str(kwargs["node_version_id"])
        version = self.versions[node_version_id]
        self._sequence += 1
        output_version_id = f"{version['node']}-output-{self._sequence}"
        revision = int(self.task["task_revision"]) + 1
        self.versions[output_version_id] = {
            **version,
            "version_id": output_version_id,
            "task_revision": revision,
        }
        self.artifacts[output_version_id] = {
            "artifact_kind": version["node"],
            "payload": dict(kwargs["artifact_payload"]),
        }
        self.task.update({
            "current_version_id": output_version_id,
            "current_status": "awaiting_human_review",
            "task_revision": revision,
        })
        return {"node_version_id": output_version_id, "task_revision": str(revision)}

    def approve_current_node(self, **kwargs: Any) -> dict[str, str]:
        version = self.versions[str(kwargs["version_id"])]
        next_node = _NEXT_NODE[version["node"]]
        revision = int(self.task["task_revision"]) + 1
        if next_node == "user_final_confirmation":
            self.task.update({"current_node": next_node, "current_status": "approved", "task_revision": revision})
        else:
            self.task.update({"current_node": next_node, "current_status": "not_started", "task_revision": revision})
        return {"task_id": self.task["task_id"], "current_node": next_node, "task_revision": str(revision)}


def _keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def _result_for(node: str) -> dict[str, Any]:
    if node == "content_plan":
        document = {"outline": "开头—冲突—解释—结尾", "key_points": ["只依据已确认材料"]}
    else:
        document = {"title": "隔离测试稿件", "script_text": "这是一段隔离测试正文。"}
    if node == "review":
        document["decision"] = "accepted_for_next_core_rule"
        document["issues"] = []
    return {
        "node": node,
        "document": document,
        "source_boundaries": ["仅使用本次任务提供的材料"],
        "unresolved": ["没有未解决问题"],
    }


class Stage2B2SecondRoundContentExternalIntelligenceTests(unittest.TestCase):
    def test_full_content_chain_uses_one_core_line_and_external_results(self) -> None:
        core = _PipelineCore()
        received: list[dict[str, Any]] = []
        counter = 0

        def executor(task: dict[str, Any]) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            received.append(task)
            node = str(task["business_context"]["node"])
            submission = {
                "execution_id": f"execution-{counter}",
                "executor_id": "isolated-executor-a" if counter % 2 else "isolated-executor-b",
                "model_ref": f"isolated-model-{counter}",
                "submitted_at": "2026-08-29T00:00:00+00:00",
                "output": _result_for(node),
            }
            if node == "content_plan":
                submission["experience_usage"] = {
                    "adopted_experience_ids": [],
                    "not_adopted_experience_ids": [],
                    "rationale": "本次测试输入没有提供正式经验。",
                }
            return submission

        service = Stage1CContentPipelineService(core=core, external_executor=executor)
        outputs: list[dict[str, Any]] = []
        for position in range(5):
            result = service.generate(
                task_id="content-task-1",
                actor="test-user",
                user_requirements="完成隔离测试内容链",
                idempotency_key=f"content-chain-{position}",
            )
            outputs.append(result)
            service.approve(
                task_id="content-task-1",
                version_id=str(result["node_version_id"]),
                actor="test-user",
                reason="隔离测试用户确认",
                idempotency_key=f"content-chain-{position}:approve",
            )

        self.assertEqual([task["task_type"] for task in received], [
            "content_plan_generation",
            "formal_draft_generate",
            "copy_optimization",
            "de_ai_revision",
            "final_content_review",
        ])
        self.assertEqual(len(core.external_runs), 5)
        self.assertEqual(core.task["current_node"], "user_final_confirmation")
        self.assertEqual(core.task["current_status"], "approved")
        self.assertEqual({task["business_context"]["task_id"] for task in received}, {"content-task-1"})
        for task in received:
            self.assertNotIn("model", set(_keys(task)))
            self.assertNotIn("provider", set(_keys(task)))
            self.assertEqual(task["skill"]["source_reference"].split("/")[0], "runtime_skills")
        self.assertEqual(
            [run["executor_id"] for run in core.external_runs],
            ["isolated-executor-a", "isolated-executor-b", "isolated-executor-a", "isolated-executor-b", "isolated-executor-a"],
        )

    def test_without_external_executor_waits_and_does_not_use_old_gateway(self) -> None:
        class ExplodingGateway:
            def run(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("old model gateway must not be called")

        core = _PipelineCore()
        service = Stage1CContentPipelineService(core=core, gateway=ExplodingGateway())
        result = service.generate(
            task_id="content-task-1",
            actor="test-user",
            user_requirements="等待外部执行",
            idempotency_key="content-waiting",
        )
        self.assertEqual(result["status"], "requires_external_intelligence")
        self.assertEqual(result["task"]["task_type"], "content_plan_generation")
        self.assertEqual(core.task["current_status"], "processing")
        self.assertEqual(core.external_runs, [])

    def test_invalid_structured_result_is_rejected_by_core_boundary(self) -> None:
        core = _PipelineCore()

        def invalid_executor(task: dict[str, Any]) -> dict[str, Any]:
            return {
                "execution_id": "execution-invalid",
                "executor_id": "isolated-executor",
                "model_ref": "isolated-model",
                "output": {
                    "node": task["business_context"]["node"],
                    "document": {},
                    "source_boundaries": ["provided"],
                    "unresolved": ["none"],
                },
            }

        service = Stage1CContentPipelineService(core=core, external_executor=invalid_executor)
        with self.assertRaises(FormalSkillValidationError):
            service.generate(
                task_id="content-task-1",
                actor="test-user",
                user_requirements="提交非法内容",
                idempotency_key="content-invalid",
            )
        self.assertEqual(len(core.validation_failures), 1)
        self.assertEqual(core.task["current_status"], "processing")


if __name__ == "__main__":
    unittest.main()
