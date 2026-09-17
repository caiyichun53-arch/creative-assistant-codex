"""Minimal stdio MCP server for the shared external task boundary.

The server is deliberately an adapter: it exposes fixed Core operations and
does not expose SQL, a task queue, a second lifecycle, or model selection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.core.core_entry import build_status
from scripts.core.formal_business_entrypoints import (
    CreationAssistantFormalBusinessCore,
)
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    validate_external_skill_output,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.domain_boundary_lifecycle import (
    prepare_cold_start_domain_boundary_external_task,
)
from scripts.core.production.experience_candidate_proposal import (
    ExperienceCandidateProposalService,
)
from scripts.core.production.stage1_competitor_registration import (
    prepare_test_only_competitor_breakdown_batch,
    submit_competitor_breakdown_external_result,
)
from scripts.agent_platform.hermes_competitor_breakdown_test_entry import (
    read_formal_competitor_breakdown_material,
)
from scripts.core.production.stage1a_research_plan import Stage1AResearchPlanService
from scripts.core.production.stage1_daily_operations import (
    prepare_daily_competitor_breakdown_external_task,
    submit_daily_competitor_breakdown_external_result,
)
from scripts.core.production.stage1b_daily_discovery import (
    Stage1BDailyDiscoveryService,
)
from scripts.core.production.stage1c_content_pipeline import (
    Stage1CContentPipelineService,
)


SERVER_NAME = "creation-assistant"
SERVER_VERSION = "stage2b-1"
MCP_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_EXTERNAL_TASK_TYPES = (
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
)


class CreationAssistantMcpError(RuntimeError):
    """An expected MCP operation error returned as a tool error."""


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CreationAssistantMcpError(f"{name} is required")
    return value.strip()


def _required_object(arguments: dict[str, Any], name: str) -> dict[str, Any]:
    value = arguments.get(name)
    if not isinstance(value, dict):
        raise CreationAssistantMcpError(f"{name} must be an object")
    return value


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CreationAssistantMcpApplication:
    """Map the small MCP surface to the existing Core/service boundary."""

    def __init__(self, core: Stage0ContentProductionCore) -> None:
        self.core = core
        self.business = CreationAssistantFormalBusinessCore(core=core)
        self.discovery = Stage1BDailyDiscoveryService(core=core, gateway=None)
        self.research = Stage1AResearchPlanService(core=core, gateway=None)
        self.content = Stage1CContentPipelineService(core=core, gateway=None)
        self.experience = ExperienceCandidateProposalService(core=core)

    def close(self) -> None:
        self.core.close()

    def status(self) -> dict[str, Any]:
        """Return the unified read-only Core status plus static MCP metadata."""
        status = build_status(
            data_identity=self.core.data_identity,
            database_path=self.core.db_path,
        )
        status.update({
            "mcp_state": "none",
            "mcp_database": None,
            "database_path": str(self.core.db_path),
            "external_task_boundary": {
                "task_types": list(SUPPORTED_EXTERNAL_TASK_TYPES),
                "model_selection": "external_client",
                "provider_selection": "external_client",
                "formal_skill_source": "Creation Assistant runtime skill",
            },
        })
        return status

    def competitor_breakdown_validation(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"operation", "platform", "platform_item_id", "model_result"}
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise CreationAssistantMcpError(
                f"competitor breakdown validation received unsupported arguments: {', '.join(unexpected)}"
            )
        operation = _required_text(arguments, "operation").casefold()
        if operation not in {"prepare", "validate"}:
            raise CreationAssistantMcpError(
                "competitor breakdown validation operation must be prepare or validate"
            )
        platform = _required_text(arguments, "platform")
        platform_item_id = _required_text(arguments, "platform_item_id")
        model_result: dict[str, Any] | None = None
        if operation == "validate":
            model_result = _required_object(arguments, "model_result")
            if model_result.get("schema_version") != "competitor_breakdown.output.raw.v5":
                raise CreationAssistantMcpError(
                    "competitor breakdown validation requires the current v5 result"
                )
        elif "model_result" in arguments:
            raise CreationAssistantMcpError(
                "prepare operation does not accept model_result"
            )
        try:
            material = read_formal_competitor_breakdown_material(
                platform=platform,
                platform_item_id=platform_item_id,
            )
            batch = prepare_test_only_competitor_breakdown_batch(
                test_id="competitor-breakdown-validation",
                materials=[material],
            )
            outcome = batch["outcomes"][0]
            task = dict(outcome["task"])
            validation_input = dict(outcome["validation_input"])
        except Exception as exc:
            raise CreationAssistantMcpError(str(exc)) from exc

        task.pop("task_identity", None)
        task.pop("business_context", None)
        if operation == "prepare":
            return {
                "status": "ready",
                "task": task,
                "formal_business_data_written": False,
            }
        assert model_result is not None
        validated = validate_external_skill_output(
            FormalSkillContract.from_runtime_skill("competitor_breakdown"),
            validation_input,
            model_result,
        )
        return {
            "status": "valid",
            "task_type": "competitor_breakdown",
            "validated_output": validated,
            "formal_business_data_written": False,
        }

    @staticmethod
    def _task_request(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        task_type = arguments.get("task_type")
        task_identity = arguments.get("task_identity")
        # Keep the original source-to-topic calls readable for existing
        # clients while normalizing them into the same generic protocol.
        if task_type is None and all(
            isinstance(arguments.get(key), str) and arguments.get(key).strip()
            for key in ("run_id", "source_version_id", "assembly_id")
        ):
            task_type = "source_to_topic"
            task_identity = {
                "run_id": arguments["run_id"],
                "source_version_id": arguments["source_version_id"],
                "assembly_id": arguments["assembly_id"],
            }
        if not isinstance(task_type, str) or not task_type.strip():
            raise CreationAssistantMcpError("task_type is required")
        task_type = task_type.strip()
        if task_type not in SUPPORTED_EXTERNAL_TASK_TYPES:
            raise CreationAssistantMcpError(
                f"task_type is not exposed by the standard external boundary: {task_type}"
            )
        if not isinstance(task_identity, dict):
            raise CreationAssistantMcpError("task_identity must be an object")
        return task_type, dict(task_identity)

    @staticmethod
    def _identity_text(identity: dict[str, Any], name: str) -> str:
        value = identity.get(name)
        if not isinstance(value, str) or not value.strip():
            raise CreationAssistantMcpError(
                f"task_identity.{name} is required for the requested task type"
            )
        return value.strip()

    @staticmethod
    def _attach_identity(
        task: dict[str, Any], *, task_type: str, task_identity: dict[str, Any]
    ) -> dict[str, Any]:
        if str(task.get("task_type") or "") != task_type:
            raise CreationAssistantMcpError(
                "Core returned a task type different from the requested task type"
            )
        result = dict(task)
        result["task_identity"] = dict(task_identity)
        return result

    def get_external_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_type, identity = self._task_request(arguments)
        if task_type == "source_to_topic":
            task = self.discovery.prepare_source_to_topic_external_task(
                run_id=self._identity_text(identity, "run_id"),
                source_version_id=self._identity_text(identity, "source_version_id"),
                assembly_id=self._identity_text(identity, "assembly_id"),
            )
        elif task_type == "candidate_priority":
            task = self.core.prepare_candidate_priority_external_task(
                run_id=self._identity_text(identity, "run_id"),
                domain_label=self._identity_text(identity, "domain_label"),
            )
        elif task_type == "competitor_breakdown":
            if "hit_id" in identity:
                if "registration_id" in identity or "source_id" in identity:
                    raise CreationAssistantMcpError(
                        "competitor breakdown identity cannot mix hit and registration identities"
                    )
                task = prepare_daily_competitor_breakdown_external_task(
                    self.core,
                    hit_id=self._identity_text(identity, "hit_id"),
                )
            else:
                if "registration_id" not in identity or "source_id" not in identity:
                    raise CreationAssistantMcpError(
                        "competitor breakdown identity needs a hit or registration/source identity"
                    )
                task = prepare_competitor_breakdown_external_task(
                    self.core,
                    registration_id=self._identity_text(identity, "registration_id"),
                    source_id=self._identity_text(identity, "source_id"),
                )
        elif task_type == "research_plan":
            task = self.research.prepare_research_plan_external_task(
                task_id=self._identity_text(identity, "task_id"),
                node_version_id=self._identity_text(identity, "node_version_id"),
            )
        elif task_type == "content_deep_research":
            task = self.content.prepare_deep_research_external_task(
                task_id=self._identity_text(identity, "task_id"),
                node_version_id=self._identity_text(identity, "node_version_id"),
            )
        elif task_type in {
            "content_plan_generation",
            "formal_draft_generate",
            "copy_optimization",
            "de_ai_revision",
            "final_content_review",
        }:
            task = self.content.prepare_content_external_task(
                task_id=self._identity_text(identity, "task_id"),
                node_version_id=self._identity_text(identity, "node_version_id"),
            )
        elif task_type == "experience_candidate_propose":
            task = self.experience.prepare_external_task(
                experience_candidate_id=self._identity_text(
                    identity, "experience_candidate_id"
                ),
            )
        elif task_type == "domain_boundary_proposal":
            task = prepare_cold_start_domain_boundary_external_task(
                self.core,
                cold_start_id=self._identity_text(identity, "cold_start_id"),
                boundary_candidate_id=self._identity_text(
                    identity, "boundary_candidate_id"
                ),
            )
        else:  # pragma: no cover - guarded by _task_request
            raise CreationAssistantMcpError(f"unsupported task type: {task_type}")
        task = self._attach_identity(
            task, task_type=task_type, task_identity=identity
        )
        return {"status": "ready", "task": task}

    def submit_external_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_type, identity = self._task_request(arguments)
        execution_id = _required_text(arguments, "execution_id")
        executor_id = _required_text(arguments, "executor_id")
        output = _required_object(arguments, "output")
        model_ref = str(arguments.get("model_ref") or "").strip() or None
        submitted_at = str(arguments.get("submitted_at") or "").strip() or None
        if task_type == "candidate_priority":
            result = self.core.submit_candidate_priority_external_result(
                run_id=self._identity_text(identity, "run_id"),
                domain_label=self._identity_text(identity, "domain_label"),
                output=output, execution_id=execution_id, executor_id=executor_id, model_ref=model_ref,
            )
            return {"status": "accepted", "task_type": task_type, "result": result,
                    "continuation": self.core.continue_candidate_priority_run(run_id=self._identity_text(identity, "run_id")),
                    "business_state_changed_by": "Creation Assistant Core"}
        if task_type == "source_to_topic":
            receipt, output_payload = self.discovery.submit_source_to_topic_external_result(
                run_id=self._identity_text(identity, "run_id"),
                source_version_id=self._identity_text(identity, "source_version_id"),
                assembly_id=self._identity_text(identity, "assembly_id"),
                execution_id=execution_id,
                executor_id=executor_id,
                model_ref=model_ref,
                submitted_at=submitted_at,
                output=output,
            )
            return {
                "status": "accepted",
                "task_type": task_type,
                "model_run_id": receipt.model_run_id,
                "validated_output": output_payload,
                "continuation": self.core.continue_candidate_priority_run(run_id=self._identity_text(identity, "run_id")),
                "business_state_changed_by": "Creation Assistant Core",
            }
        if task_type == "competitor_breakdown":
            if "hit_id" in identity:
                if "registration_id" in identity or "source_id" in identity:
                    raise CreationAssistantMcpError(
                        "competitor breakdown identity cannot mix hit and registration identities"
                    )
                result = submit_daily_competitor_breakdown_external_result(
                    self.core,
                    hit_id=self._identity_text(identity, "hit_id"),
                    execution_id=execution_id,
                    executor_id=executor_id,
                    model_ref=model_ref,
                    submitted_at=submitted_at,
                    output=output,
                )
            else:
                if "registration_id" not in identity or "source_id" not in identity:
                    raise CreationAssistantMcpError(
                        "competitor breakdown identity needs a hit or registration/source identity"
                    )
                result = submit_competitor_breakdown_external_result(
                    self.core,
                    registration_id=self._identity_text(identity, "registration_id"),
                    source_id=self._identity_text(identity, "source_id"),
                    execution_id=execution_id,
                    executor_id=executor_id,
                    model_ref=model_ref,
                    submitted_at=submitted_at,
                    output=output,
                )
            return {
                "status": "accepted",
                "task_type": task_type,
                "result": result,
                "business_state_changed_by": "Creation Assistant Core",
            }
        if task_type == "domain_boundary_proposal":
            result = self.core.submit_cold_start_domain_boundary_external_result(
                cold_start_id=self._identity_text(identity, "cold_start_id"),
                boundary_candidate_id=self._identity_text(
                    identity, "boundary_candidate_id"
                ),
                execution_id=execution_id,
                executor_id=executor_id,
                model_ref=model_ref,
                submitted_at=submitted_at,
                output=output,
            )
            return {
                "status": "accepted",
                "task_type": task_type,
                "result": result,
                "business_state_changed_by": "Creation Assistant Core",
            }
        if task_type == "experience_candidate_propose":
            task = self.get_external_task(
                {"task_type": task_type, "task_identity": identity}
            )["task"]
            result = self.experience.submit_experience_candidate_external_result(
                task=task,
                execution_id=execution_id,
                executor_id=executor_id,
                model_ref=model_ref,
                submitted_at=submitted_at,
                output=output,
            )
            return {
                "status": "accepted",
                "task_type": task_type,
                "result": result,
                "business_state_changed_by": "Creation Assistant Core",
            }
        task = self.get_external_task(
            {"task_type": task_type, "task_identity": identity}
        )["task"]
        user_requirements = str(
            arguments.get("user_requirements")
            or "continue the current formal production task"
        ).strip()
        if not user_requirements:
            raise CreationAssistantMcpError("user_requirements cannot be empty")
        usage = arguments.get("experience_usage")
        validation_usage = arguments.get("validation_usage")
        if usage is not None and not isinstance(usage, dict):
            raise CreationAssistantMcpError("experience_usage must be an object")
        if validation_usage is not None and not isinstance(validation_usage, dict):
            raise CreationAssistantMcpError("validation_usage must be an object")
        identity_key = _json_text(identity)
        result = self.business.submit_formal_external_result(
            task_id=self._identity_text(identity, "task_id"),
            node_version_id=self._identity_text(identity, "node_version_id"),
            execution_id=execution_id,
            executor_id=executor_id,
            model_ref=model_ref,
            submitted_at=submitted_at,
            output=output,
            actor=executor_id,
            idempotency_key=str(
                arguments.get("idempotency_key")
                or f"mcp-external:{task_type}:{identity_key}:{execution_id}"
            ),
            experience_usage=usage,
            validation_usage=validation_usage,
            user_requirements=user_requirements,
        )
        return {
            "status": "accepted",
            "task_type": task_type,
            "result": result,
            "business_state_changed_by": "Creation Assistant Core",
        }

    def get_external_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "task_type" in arguments or "task_identity" in arguments:
            task_type, identity = self._task_request(arguments)
            if task_type == "candidate_priority":
                return self.core.get_candidate_priority_report(
                    run_id=self._identity_text(identity, "run_id"),
                    domain_label=self._identity_text(identity, "domain_label"),
                )
            if task_type != "source_to_topic":
                raise CreationAssistantMcpError(
                    "persisted external-result lookup is not defined for this task type"
                )
            arguments = {
                "run_id": self._identity_text(identity, "run_id"),
                "source_version_id": self._identity_text(identity, "source_version_id"),
                "assembly_id": self._identity_text(identity, "assembly_id"),
            }
        return self.core.get_discovery_external_execution_result(
            run_id=_required_text(arguments, "run_id"),
            source_version_id=_required_text(arguments, "source_version_id"),
            assembly_id=_required_text(arguments, "assembly_id"),
        )

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments or {}
        if name == "creation_assistant_status":
            return self.status()
        if name == "creation_assistant_hotspot_report":
            return self.business.hotspot_report(arguments)
        if name == "creation_assistant_competitor_breakdown_validation":
            return self.competitor_breakdown_validation(arguments)
        if name == "creation_assistant_get_external_task":
            return self.get_external_task(arguments)
        if name == "creation_assistant_submit_external_result":
            return self.submit_external_result(arguments)
        if name == "creation_assistant_get_external_result":
            return self.get_external_result(arguments)
        raise CreationAssistantMcpError(f"unknown tool: {name}")


def _identifier_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "enum": list(SUPPORTED_EXTERNAL_TASK_TYPES),
            },
            "task_identity": {"type": "object"},
        },
        "required": ["task_type", "task_identity"],
        "additionalProperties": False,
    }


def _legacy_source_identifier_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "source_version_id": {"type": "string"},
            "assembly_id": {"type": "string"},
        },
        "required": ["run_id", "source_version_id", "assembly_id"],
        "additionalProperties": False,
    }


def _competitor_breakdown_validation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["prepare", "validate"]},
            "platform": {"type": "string"},
            "platform_item_id": {"type": "string"},
            "model_result": {"type": "object"},
        },
        "required": ["operation", "platform", "platform_item_id"],
        "additionalProperties": False,
    }


def tool_definitions() -> list[dict[str, Any]]:
    identifiers = _identifier_schema()
    return [
        {
            "name": "creation_assistant_status",
            "description": "Read basic facts from the connected Creation Assistant Core.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "creation_assistant_competitor_breakdown_validation",
            "description": "Prepare or validate one competitor breakdown by platform item identity without persisting business data.",
            "inputSchema": _competitor_breakdown_validation_schema(),
        },
        {
            "name": "creation_assistant_hotspot_report",
            "description": "综合热点榜：collect 获取当前数据和热点筛选 Skill；prepare 重放原快照；当前 Agent 判断全部事件后用 render 提交 snapshot、output 及用户要求的 top_n。返回偏好排除与排名靠后两份独立清单。无模型调用、候选生成或正式业务写入。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": ["collect", "prepare", "render"]},
                    "snapshot": {"type": "object"}, "output": {"type": "object"},
                    "top_n": {"type": "integer", "minimum": 1},
                },
                "required": ["operation"], "additionalProperties": False,
            },
        },
        {
            "name": "creation_assistant_get_external_task",
            "description": "Get one Core-prepared external task of the requested type, including its formal Skill, material, constraints, and output schema.",
            "inputSchema": identifiers,
        },
        {
            "name": "creation_assistant_submit_external_result",
            "description": "Submit one structured external result for Core validation and continuation of the same business task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **identifiers["properties"],
                    "execution_id": {"type": "string"},
                    "executor_id": {"type": "string"},
                    "model_ref": {"type": "string"},
                    "submitted_at": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "user_requirements": {"type": "string"},
                    "experience_usage": {"type": "object"},
                    "validation_usage": {"type": "object"},
                    "output": {"type": "object"},
                },
                "required": [
                    "task_type", "task_identity",
                    "execution_id", "executor_id", "output",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "creation_assistant_get_external_result",
            "description": "Read the persisted Core processing result for one external execution.",
            "inputSchema": _legacy_source_identifier_schema(),
        },
    ]


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_message(app: CreationAssistantMcpApplication, message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message without writing anything to stdout except JSON-RPC."""
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str):
        return _error(request_id, -32600, "invalid JSON-RPC request")
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        protocol_version = requested if isinstance(requested, str) else MCP_PROTOCOL_VERSION
        return _success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _success(request_id, {})
    if method == "tools/list":
        return _success(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        try:
            result = app.call_tool(params["name"], params.get("arguments"))
        except Exception as exc:
            return _success(
                request_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text({"error": str(exc)})}],
                },
            )
        return _success(
            request_id,
            {
                "isError": False,
                "content": [{"type": "text", "text": _json_text(result)}],
                "structuredContent": result,
            },
        )
    return _error(request_id, -32601, f"method not found: {method}")


def serve_stdio(app: CreationAssistantMcpApplication) -> int:
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = handle_message(app, message)
        except Exception as exc:
            response = _error(None, -32700, str(exc))
        if response is not None:
            sys.stdout.write(_json_text(response) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Creation Assistant MCP server")
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--data-identity", required=True, choices=("test", "production"))
    args = parser.parse_args(argv)
    core = Stage0ContentProductionCore.open(args.db_path, data_identity=args.data_identity)
    app = CreationAssistantMcpApplication(core)
    try:
        return serve_stdio(app)
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
