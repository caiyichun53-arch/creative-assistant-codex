from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import yaml

from scripts.core.model_gateway.business_route_registry import REGISTRY_PATH
from scripts.core.host.production_host import PRODUCTION_HOST_ACTOR
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator
from scripts.core.workflow.goal_phase5_business_workflow import (
    FORMAL_WORKFLOW_DEFINITIONS,
    InputAssembly,
    build_formal_business_workflow_steps,
)


class ProductionHostWhitelistToolError(RuntimeError):
    pass


WHITELISTED_ACTIONS = frozenset(
    {
        "create_controlled_task",
        "query_task_status",
        "query_task_result",
        "query_failure_reason",
        "cancel_task",
        "query_human_confirmation_items",
        "query_business_model_binding_summary",
    }
)

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "shell",
        "shell_command",
        "command",
        "db",
        "database",
        "sql",
        "raw_sql",
        "direct_skill",
        "skill_call",
        "model_switch",
        "fallback",
        "provider_fallback",
        "feishu_send_direct",
    }
)


@dataclass(frozen=True)
class ProductionHostWhitelistToolRequest:
    action: str
    actor: str
    payload: dict[str, Any]
    request_id: str
    confirmed: bool = False
    host: str = "hermes"


@dataclass(frozen=True)
class ProductionHostWhitelistToolResult:
    action: str
    status: str
    payload: dict[str, Any]
    replayed: bool = False


class ProductionHostWhitelistTool:
    def __init__(
        self,
        *,
        core: CoreMaterializer,
        scheduler: Goal03Scheduler,
        input_assembly: InputAssembly | None = None,
        core_actor: str = PRODUCTION_HOST_ACTOR,
    ):
        self.core = core
        self.scheduler = scheduler
        self.input_assembly = input_assembly or InputAssembly()
        self.orchestrator = Goal05WorkflowOrchestrator(scheduler)
        self.core_actor = core_actor

    def execute(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        self._validate_request(request)
        if request.action == "create_controlled_task":
            return self._create_controlled_task(request)
        if request.action == "query_task_status":
            return self._query_task_status(request)
        if request.action == "query_task_result":
            return self._query_task_result(request)
        if request.action == "query_failure_reason":
            return self._query_failure_reason(request)
        if request.action == "cancel_task":
            return self._cancel_task(request)
        if request.action == "query_human_confirmation_items":
            return self._query_human_confirmation_items(request)
        if request.action == "query_business_model_binding_summary":
            return self._query_business_model_binding_summary(request)
        raise ProductionHostWhitelistToolError(f"unsupported action: {request.action}")

    def _create_controlled_task(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        workflow_id = _required_text(request.payload, "workflow_id")
        workflow_instance_id = _required_text(request.payload, "workflow_instance_id")
        definition = FORMAL_WORKFLOW_DEFINITIONS.get(workflow_id)
        if definition is None:
            raise ProductionHostWhitelistToolError(f"unknown formal workflow: {workflow_id}")
        input_payloads = request.payload.get("input_payloads") or {}
        if not isinstance(input_payloads, dict):
            raise ProductionHostWhitelistToolError("input_payloads must be an object")
        domain = _required_text(request.payload, "domain")
        content_form = _required_text(request.payload, "content_form")
        conditions = tuple(str(item) for item in request.payload.get("conditions") or ())
        first_step = definition.step_graph[0]
        first_input = input_payloads.get(first_step.step_key)
        if not isinstance(first_input, dict):
            raise ProductionHostWhitelistToolError(f"missing input for first workflow step: {first_step.step_key}")

        create = self.core.execute(
            CoreCommandEnvelope(
                command_type="create_state",
                actor=self.core_actor,
                object_kind="production_task",
                idempotency_key=f"phase6.task.create.{request.request_id}",
                payload={
                    "workflow_id": workflow_id,
                    "workflow_version": definition.workflow_version,
                    "workflow_instance_id": workflow_instance_id,
                },
                correlation_id=workflow_instance_id,
            )
        )
        if create.status != "succeeded" or not create.object_id or not create.basis_version_id:
            return ProductionHostWhitelistToolResult(
                action=request.action,
                status=create.status,
                payload={"reason": create.reason},
                replayed=create.replayed,
            )
        queued = self.core.execute(
            CoreCommandEnvelope(
                command_type="transition_state",
                actor=self.core_actor,
                object_kind="production_task",
                object_id=create.object_id,
                expected_basis_version_id=create.basis_version_id,
                idempotency_key=f"phase6.task.queue.{request.request_id}",
                payload={"to_state": "queued"},
                correlation_id=workflow_instance_id,
                causation_id=create.command_id,
            )
        )
        if queued.status != "succeeded":
            return ProductionHostWhitelistToolResult(
                action=request.action,
                status=queued.status,
                payload={"task_id": create.object_id, "reason": queued.reason},
                replayed=queued.replayed,
            )

        frozen = self.input_assembly.freeze_skill_input(
            workflow_id=workflow_instance_id,
            step_key=first_step.step_key,
            formal_skill_id=first_step.formal_skill_id,
            input_payload=first_input,
            upstream_refs=(),
            domain=domain,
            content_form=content_form,
            conditions=conditions,
            experience_candidates=(),
        )
        steps = build_formal_business_workflow_steps(
            workflow_id=workflow_instance_id,
            frozen_inputs=[frozen],
            max_attempts=int(request.payload.get("max_attempts") or 1),
        )
        started = self.orchestrator.start_workflow(
            workflow_name=definition.workflow_id,
            steps=steps,
            idempotency_key=f"phase6.task.workflow.{request.request_id}",
        )
        return ProductionHostWhitelistToolResult(
            action=request.action,
            status="succeeded",
            payload={
                "task_id": create.object_id,
                "task_state": "queued",
                "basis_version_id": queued.basis_version_id,
                "workflow_id": workflow_id,
                "workflow_version": definition.workflow_version,
                "workflow_instance_id": workflow_instance_id,
                "first_step_key": first_step.step_key,
                "job_ids": list(started.job_ids),
            },
            replayed=create.replayed and queued.replayed and started.replayed,
        )

    def _query_task_status(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        payload: dict[str, Any] = {}
        task_id = request.payload.get("task_id")
        job_id = request.payload.get("job_id")
        if task_id:
            row = self.core.get_state("production_task", str(task_id))
            payload["task"] = {
                "task_id": str(task_id),
                "state": row["state"],
                "basis_version_id": row["basis_version_id"],
                "row_revision": row["row_revision"],
            }
        if job_id:
            row = self.scheduler.get_job(str(job_id))
            payload["job"] = {
                "job_id": row["job_id"],
                "job_kind": row["job_kind"],
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "max_attempts": row["max_attempts"],
            }
        if not payload:
            raise ProductionHostWhitelistToolError("task_id or job_id is required")
        return ProductionHostWhitelistToolResult(action=request.action, status="succeeded", payload=payload)

    def _query_task_result(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        job_id = _required_text(request.payload, "job_id")
        row = self.core.conn.execute(
            """
            SELECT formal_skill_id, result_version_id, output_hash, schema_version
              FROM formal_business_skill_result_index
             WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return ProductionHostWhitelistToolResult(action=request.action, status="not_ready", payload={"job_id": job_id})
        return ProductionHostWhitelistToolResult(
            action=request.action,
            status="succeeded",
            payload={
                "job_id": job_id,
                "formal_skill_id": row["formal_skill_id"],
                "result_version_id": row["result_version_id"],
                "output_hash": row["output_hash"],
                "schema_version": row["schema_version"],
            },
        )

    def _query_failure_reason(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        job_id = _required_text(request.payload, "job_id")
        row = self.core.conn.execute(
            """
            SELECT error_json
              FROM scheduler_job_attempt
             WHERE job_id=? AND status='failed'
             ORDER BY attempt_no DESC
             LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return ProductionHostWhitelistToolResult(action=request.action, status="not_failed", payload={"job_id": job_id})
        return ProductionHostWhitelistToolResult(
            action=request.action,
            status="succeeded",
            payload={"job_id": job_id, "error": json.loads(row["error_json"])},
        )

    def _cancel_task(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        cancelled: dict[str, Any] = {}
        job_id = request.payload.get("job_id")
        task_id = request.payload.get("task_id")
        if job_id:
            self.scheduler.cancel_job(job_id=str(job_id), actor="production_host_whitelist_tool", reason="cancelled_by_whitelist_tool")
            cancelled["job_id"] = str(job_id)
        if task_id:
            row = self.core.get_state("production_task", str(task_id))
            result = self.core.execute(
                CoreCommandEnvelope(
                    command_type="transition_state",
                    actor=self.core_actor,
                    object_kind="production_task",
                    object_id=str(task_id),
                    expected_basis_version_id=row["basis_version_id"],
                    idempotency_key=f"phase6.task.cancel.{request.request_id}.{task_id}",
                    payload={"to_state": "cancelled"},
                    correlation_id=str(task_id),
                )
            )
            cancelled["task_id"] = str(task_id)
            cancelled["task_status"] = result.status
            cancelled["reason"] = result.reason
        if not cancelled:
            raise ProductionHostWhitelistToolError("task_id or job_id is required")
        return ProductionHostWhitelistToolResult(action=request.action, status="succeeded", payload=cancelled)

    def _query_human_confirmation_items(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        rows = self.core.conn.execute(
            """
            SELECT c.command_id, c.object_kind, c.object_id, c.payload_json, r.result_json
              FROM core_command_envelope c
              JOIN command_receipt r ON r.receipt_id=c.command_receipt_id
             WHERE c.status='rejected' AND r.result_json LIKE '%confirmation_required%'
             ORDER BY c.created_at, c.command_id
            """
        ).fetchall()
        return ProductionHostWhitelistToolResult(
            action=request.action,
            status="succeeded",
            payload={
                "items": [
                    {
                        "command_id": row["command_id"],
                        "object_kind": row["object_kind"],
                        "object_id": row["object_id"],
                        "payload": json.loads(row["payload_json"]),
                        "reason": (json.loads(row["result_json"]) or {}).get("reason"),
                    }
                    for row in rows
                ]
            },
        )

    def _query_business_model_binding_summary(self, request: ProductionHostWhitelistToolRequest) -> ProductionHostWhitelistToolResult:
        registry = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
        defaults = registry.get("provider_policy_defaults") or {}
        fallback = defaults.get("fallback_policy") or {}
        return ProductionHostWhitelistToolResult(
            action=request.action,
            status="succeeded",
            payload={
                "binding_id": "business.primary",
                "active_binding_count": 1,
                "provider_alias": defaults.get("provider_name"),
                "live_model_port": defaults.get("live_model_port"),
                "model_ref_source": defaults.get("model_ref_env"),
                "tools_enabled": bool(defaults.get("tools_enabled")),
                "memory_enabled": bool(defaults.get("memory_enabled")),
                "feishu_messaging_enabled": bool(defaults.get("feishu_messaging_enabled")),
                "fallback_enabled": any(bool(fallback.get(key)) for key in ("dry_run_fallback", "fake_port_fallback", "cli_fallback")),
                "on_failure": fallback.get("on_failure"),
                "actual_model_value_redacted": True,
            },
        )

    @staticmethod
    def _validate_request(request: ProductionHostWhitelistToolRequest) -> None:
        if request.action not in WHITELISTED_ACTIONS:
            raise ProductionHostWhitelistToolError(f"action is not whitelisted: {request.action}")
        if not request.actor or request.actor in {request.host, PRODUCTION_HOST_ACTOR}:
            raise ProductionHostWhitelistToolError("external actor is required")
        if not request.request_id:
            raise ProductionHostWhitelistToolError("request_id is required")
        _reject_forbidden_keys(request.payload)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProductionHostWhitelistToolError(f"{key} is required")
    return value


def _reject_forbidden_keys(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_PAYLOAD_KEYS:
                raise ProductionHostWhitelistToolError(f"forbidden payload key at {path}.{key}")
            _reject_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")


HermesWhitelistToolError = ProductionHostWhitelistToolError
HermesWhitelistToolRequest = ProductionHostWhitelistToolRequest
HermesWhitelistToolResult = ProductionHostWhitelistToolResult
HermesWhitelistTool = ProductionHostWhitelistTool
