"""Stage 1A formal-topic and research-plan orchestration.

This module is deliberately limited to the first two production objects.  It
does not search, fetch materials, execute deep research, or create a draft.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.production.stage0_content_core import (
    EXTERNAL_INTELLIGENCE_EXECUTION_VERSION,
    InputAssembly,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1b_daily_discovery import (
    ExternalIntelligenceReceipt,
    ExternalIntelligenceRequired,
)


RESEARCH_PLAN_PROMPT_VERSION = "stage1a.research_plan.prompt.v9"
RESEARCH_PLAN_SKILL_VERSION = "stage1a.research_plan.skill.v1.9"
RESEARCH_PLAN_MODEL_CONFIG_VERSION = EXTERNAL_INTELLIGENCE_EXECUTION_VERSION
RESEARCH_PLAN_REQUIRED_FIELDS = (
    "research_objective",
    "research_scope",
    "research_sequence",
    "research_questions",
    "source_plan",
    "required_outputs",
)


class ResearchPlanValidationError(StateTransitionError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResearchPlanValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _normalize_string_array(value: Any, *, key: str) -> list[str]:
    """Keep the persisted artifact flat when a model uses labeled sub-objects."""
    if not isinstance(value, list) or not value:
        raise ResearchPlanValidationError(f"research plan {key} must be a non-empty list")
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
            continue
        if isinstance(item, dict) and item:
            parts: list[str] = []
            for label, detail in item.items():
                if isinstance(detail, list):
                    detail_text = "、".join(str(entry).strip() for entry in detail if str(entry).strip())
                elif isinstance(detail, dict):
                    detail_text = "；".join(
                        f"{nested_label}：{nested_detail}"
                        for nested_label, nested_detail in detail.items()
                    )
                else:
                    detail_text = str(detail).strip()
                if detail_text:
                    parts.append(f"{label}：{detail_text}")
            if parts:
                normalized.append("；".join(parts))
                continue
        raise ResearchPlanValidationError(f"research plan {key} must contain non-empty strings")
    return normalized


def validate_formal_topic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ResearchPlanValidationError("formal topic must be an object")
    normalized = {
        "title": _required_text(payload, "title"),
        "core_question": _required_text(payload, "core_question"),
        "domain": _required_text(payload, "domain"),
        "source_refs": list(payload.get("source_refs") or []),
    }
    if not all(isinstance(item, (str, dict)) for item in normalized["source_refs"]):
        raise ResearchPlanValidationError("source_refs may contain only string or object references")
    return normalized


def validate_research_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ResearchPlanValidationError("research plan output must be an object")
    missing = [key for key in RESEARCH_PLAN_REQUIRED_FIELDS if key not in payload]
    if missing:
        raise ResearchPlanValidationError(f"research plan missing required fields: {missing}")
    normalized = dict(payload)
    normalized["research_objective"] = _required_text(payload, "research_objective")
    for key in (
        "research_sequence",
        "research_questions",
        "source_plan",
        "required_outputs",
    ):
        normalized[key] = _normalize_string_array(payload[key], key=key)
    scope = payload["research_scope"]
    if not isinstance(scope, dict) or not isinstance(scope.get("included"), list) or not isinstance(scope.get("excluded"), list):
        raise ResearchPlanValidationError("research_scope must contain included and excluded lists")
    return normalized


class Stage1AResearchPlanService:
    """Only the controlled formal-topic -> research-plan-awaiting-review path."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        gateway: Any | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ):
        if gateway is not None:
            raise StateTransitionError(
                "formal research-plan execution must use an external executor; direct model gateways are not supported"
            )
        self.core = core
        self.gateway = None
        self.external_executor = external_executor

    def submit_formal_topic(
        self, *, topic_payload: dict[str, Any], actor: str, reason: str, idempotency_key: str
    ) -> dict[str, Any]:
        return self.core.submit_formal_topic(
            topic_payload=validate_formal_topic_payload(topic_payload),
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def confirm_formal_topic(
        self, *, task_id: str, topic_version_id: str, actor: str, reason: str, idempotency_key: str
    ) -> dict[str, str]:
        return self.core.confirm_formal_topic(
            task_id=task_id,
            topic_version_id=topic_version_id,
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def create_direct_formal_topic_and_generate_plan(
        self,
        *,
        domain_label: str,
        account_ref: str,
        core_question: str,
        scope_or_requirement: str,
        original_instruction: Any,
        actor: str,
        existing_manual_source_ids: tuple[str, ...] = (),
        known_materials: list[dict[str, Any]] | None = None,
        material_gaps: list[str] | None = None,
        timeliness: dict[str, Any] | None = None,
        risks: list[str] | None = None,
        related_topic_refs: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        """One user instruction confirms the topic, then immediately produces a plan for user review."""
        direct = self.core.create_direct_formal_topic(
            domain_label=domain_label, account_ref=account_ref, core_question=core_question,
            scope_or_requirement=scope_or_requirement, original_instruction=original_instruction,
            actor=actor, actor_kind="user", existing_manual_source_ids=existing_manual_source_ids,
            known_materials=known_materials, material_gaps=material_gaps, timeliness=timeliness,
            risks=risks, related_topic_refs=related_topic_refs, idempotency_key=f"{idempotency_key}:topic",
        )
        plan = self.generate_research_plan(
            task_id=direct["task_id"],
            user_requirements=json.dumps({"scope_or_requirement": scope_or_requirement, "material_gaps": material_gaps or [], "risks": risks or []}, ensure_ascii=False),
            actor=actor,
            idempotency_key=f"{idempotency_key}:research-plan",
        )
        return {
            **direct,
            "research_plan_version_id": plan["node_version_id"],
            "research_plan_status": str(plan.get("status") or "awaiting_human_review"),
            **({"external_task": plan["task"]} if isinstance(plan.get("task"), dict) else {}),
        }

    def view_artifact(self, *, version_id: str) -> dict[str, Any]:
        return self.core.get_artifact_payload(version_id)

    def generate_research_plan(
        self,
        *,
        task_id: str,
        user_requirements: str | None,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        service_request = {"task_id": task_id, "user_requirements": user_requirements, "actor": actor}
        replay = self.core.find_command_replay("stage1a_generate_research_plan", idempotency_key, service_request)
        if replay:
            return replay
        task = self.core.get_task(task_id)
        if task["current_node"] != "research_plan":
            raise StateTransitionError("only the research_plan node is implemented in Stage 1A")
        if task["current_status"] == "not_started":
            if not isinstance(user_requirements, str) or not user_requirements.strip():
                raise StateTransitionError("an initial research-plan request requires non-empty user requirements")
            request_version_id = self._start_research_plan(task, user_requirements=user_requirements, actor=actor, idempotency_key=idempotency_key)
        elif task["current_status"] == "processing":
            request_version_id = str(task["current_version_id"])
            frozen_requirements = str(
                self.core.get_input_assembly_payload(self.core.get_node_version(request_version_id)["input_assembly_id"])[
                    "user_requirements"
                ]
            )
            if user_requirements is not None and user_requirements != frozen_requirements:
                raise StateTransitionError("research-plan input is frozen; return the plan to submit new requirements")
        else:
            raise StateTransitionError("research plan is not ready for a model execution")
        request_version = self.core.get_node_version(request_version_id)
        input_assembly = self.core.get_input_assembly_payload(
            request_version["input_assembly_id"]
        )
        if self.gateway is None:
            external_task = self.prepare_research_plan_external_task(
                task_id=task_id, node_version_id=request_version_id
            )
            if self.external_executor is None:
                return {
                    "task_id": task_id,
                    "node_version_id": request_version_id,
                    "status": "requires_external_intelligence",
                    "task": external_task,
                }
            submission = self.external_executor(external_task)
            return self.submit_research_plan_external_result(
                task_id=task_id,
                node_version_id=request_version_id,
                execution_id=str(submission.get("execution_id") or ""),
                executor_id=str(submission.get("executor_id") or ""),
                model_ref=str(submission.get("model_ref") or "") or None,
                submitted_at=str(submission.get("submitted_at") or "") or None,
                output=submission.get("output"),
                actor=actor,
                idempotency_key=f"{idempotency_key}:external-complete",
            )
        raise StateTransitionError(
            "research-plan model execution must be submitted by an external executor"
        )

    def prepare_research_plan_external_task(
        self, *, task_id: str, node_version_id: str | None = None
    ) -> dict[str, Any]:
        task = self.core.get_task(task_id)
        version_id = node_version_id or str(task.get("current_version_id") or "")
        version = self.core.get_node_version(version_id)
        if (
            task["current_node"] != "research_plan"
            or task["current_status"] != "processing"
            or version["task_id"] != task_id
            or version["node"] != "research_plan"
            or task["current_version_id"] != version_id
        ):
            raise StateTransitionError("research plan external task requires the current processing version")
        input_assembly = self.core.get_input_assembly_payload(str(version["input_assembly_id"]))
        task_payload, _ = prepare_external_skill_task(
            FormalSkillContract.from_runtime_skill("research_plan"),
            {
                "correlation_id": task_id,
                "input_assembly": input_assembly,
                "schema_version": "research_plan.input.v1",
            },
            constraints={
                "use_only_supplied_material": True,
                "do_not_search": True,
                "cannot_change_business_state": True,
                "do_not_approve_or_skip_human_review": True,
            },
            business_context={
                "task_id": task_id,
                "node": "research_plan",
                "node_version_id": version_id,
                "input_assembly_id": str(version["input_assembly_id"]),
                "data_identity": self.core.data_identity,
            },
        )
        return task_payload

    def submit_research_plan_external_result(
        self,
        *,
        task_id: str,
        node_version_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output: dict[str, Any],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        task = self.core.get_task(task_id)
        version = self.core.get_node_version(node_version_id)
        input_assembly = self.core.get_input_assembly_payload(str(version["input_assembly_id"]))
        contract = FormalSkillContract.from_runtime_skill("research_plan")
        _, prepared = prepare_external_skill_task(
            contract,
            {"correlation_id": task_id, "input_assembly": input_assembly, "schema_version": "research_plan.input.v1"},
            constraints={},
        )
        model_run_id = self.core.record_external_node_execution(
            task_id=task_id,
            node_version_id=node_version_id,
            execution_id=execution_id,
            executor_id=executor_id,
            model_ref=model_ref,
            submitted_at=submitted_at,
            output_payload=output,
        )
        try:
            validated = validate_external_skill_output(
                contract,
                {"correlation_id": task_id, "input_assembly": input_assembly, "schema_version": "research_plan.input.v1"},
                output,
                prepared=prepared,
            )
            plan = validate_research_plan_payload(validated)
        except (FormalSkillValidationError, ResearchPlanValidationError) as exc:
            self.core.record_model_validation_failure(
                task_id=task_id,
                node_version_id=node_version_id,
                model_run_id=model_run_id,
                reason=str(exc),
                raw_model_output=None,
            )
            raise FormalSkillValidationError(
                str(exc), model_run_envelope_version_id=model_run_id
            ) from exc
        return self.core.complete_node_from_external_result(
            task_id=task_id,
            node_version_id=node_version_id,
            model_run_id=model_run_id,
            output_ref=_payload_hash(plan),
            validation_status="passed",
            actor=actor,
            expected_task_revision=int(task["task_revision"]),
            idempotency_key=idempotency_key,
            artifact_payload=plan,
        )

    def return_research_plan(
        self,
        *,
        task_id: str,
        research_plan_version_id: str,
        modification_requirements: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        task = self.core.get_task(task_id)
        if task["current_node"] != "research_plan" or task["current_status"] != "awaiting_human_review":
            raise StateTransitionError("only an awaiting research plan may be returned")
        previous = self.core.get_node_version(research_plan_version_id)
        topic = self.core.get_artifact_payload(previous["upstream_version_id"])
        assembly = self._research_plan_assembly(
            task_id=task_id,
            topic_version_id=previous["upstream_version_id"],
            topic_payload=topic["payload"],
            user_requirements=modification_requirements,
        )
        assembly_result = self.core.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        return self.core.return_current_node(
            task_id=task_id,
            version_id=research_plan_version_id,
            input_assembly_id=assembly_result["assembly_id"],
            actor=actor,
            reason=modification_requirements,
            idempotency_key=f"{idempotency_key}:return",
        )

    def approve_research_plan(
        self, *, task_id: str, research_plan_version_id: str, actor: str, reason: str, idempotency_key: str
    ) -> dict[str, str]:
        return self.core.approve_current_node(
            task_id=task_id,
            version_id=research_plan_version_id,
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def cancel_task(self, *, task_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        return self.core.cancel_current_task(task_id=task_id, actor=actor, reason=reason, idempotency_key=idempotency_key)

    def _start_research_plan(self, task: dict[str, Any], *, user_requirements: str, actor: str, idempotency_key: str) -> str:
        topic = self.core.get_artifact_payload(str(task["topic_version_id"]))
        assembly = self._research_plan_assembly(
            task_id=str(task["task_id"]),
            topic_version_id=str(task["topic_version_id"]),
            topic_payload=topic["payload"],
            user_requirements=user_requirements,
        )
        assembly_result = self.core.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        request_result = self.core.create_node_request(
            task_id=str(task["task_id"]),
            node="research_plan",
            input_assembly_id=assembly_result["assembly_id"],
            actor=actor,
            idempotency_key=f"{idempotency_key}:request",
        )
        return request_result["node_version_id"]

    @staticmethod
    def _research_plan_assembly(
        *, task_id: str,
        topic_version_id: str,
        topic_payload: dict[str, Any],
        user_requirements: str,
    ) -> InputAssembly:
        material_refs: list[dict[str, Any]] = [
            {
                "kind": "formal_topic",
                "version_id": topic_version_id,
                "title": topic_payload["title"],
                "core_question": topic_payload["core_question"],
                "domain": topic_payload["domain"],
                "current_date": datetime.now(timezone.utc).date().isoformat(),
            }
        ]
        for reference in topic_payload.get("source_refs", []):
            material_refs.append(reference if isinstance(reference, dict) else {"kind": "topic_source_clue", "reference": reference})
        return InputAssembly(
            task_id=task_id,
            node="research_plan",
            upstream_version_id=topic_version_id,
            user_requirements=user_requirements,
            material_refs=tuple(material_refs),
            research_refs=(),
            content_plan_ref=None,
            considered_experience=(),
            adopted_experience=(),
            rejected_experience=(),
            omitted_materials=(),
            prompt_version=RESEARCH_PLAN_PROMPT_VERSION,
            skill_version=RESEARCH_PLAN_SKILL_VERSION,
            model_config_version=RESEARCH_PLAN_MODEL_CONFIG_VERSION,
        )
