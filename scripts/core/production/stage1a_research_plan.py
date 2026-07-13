"""Stage 1A formal-topic and research-plan orchestration.

This module is deliberately limited to the first two production objects.  It
does not search, fetch materials, execute deep research, or create a draft.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError, ModelRequest, ModelRoute
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig
from scripts.core.model_gateway.model_router import DEFAULT_MODEL_ENV_PATH, ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import (
    CoreModelRunMaterializer,
    InputAssembly,
    Stage0ContentProductionCore,
    StateTransitionError,
)


RESEARCH_PLAN_PROMPT_VERSION = "stage1a.research_plan.prompt.v1"
RESEARCH_PLAN_SKILL_VERSION = "stage1a.research_plan.skill.v1"
RESEARCH_PLAN_MODEL_CONFIG_VERSION = "model_routes.v1"
RESEARCH_PLAN_REQUIRED_FIELDS = (
    "core_question",
    "provisional_viewpoint",
    "research_scope",
    "research_questions",
    "candidate_claims",
    "evidence_requirements",
    "blocking_claims",
    "required_materials",
    "prohibited_materials",
    "candidate_content_routes",
    "stop_conditions",
    "budget_boundary",
    "risks_uncertainties",
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
    for key in ("core_question", "provisional_viewpoint"):
        normalized[key] = _required_text(payload, key)
    for key in (
        "research_questions",
        "candidate_claims",
        "evidence_requirements",
        "blocking_claims",
        "required_materials",
        "prohibited_materials",
        "candidate_content_routes",
        "stop_conditions",
        "risks_uncertainties",
    ):
        value = payload[key]
        if not isinstance(value, list) or not value:
            raise ResearchPlanValidationError(f"research plan {key} must be a non-empty list")
        if key == "candidate_claims":
            if not all(isinstance(item, (str, dict)) for item in value):
                raise ResearchPlanValidationError("candidate_claims must contain strings or structured claim objects")
        elif not all(isinstance(item, str) and item.strip() for item in value):
            raise ResearchPlanValidationError(f"research plan {key} must contain non-empty strings")
    scope = payload["research_scope"]
    if not isinstance(scope, dict) or not isinstance(scope.get("included"), list) or not isinstance(scope.get("excluded"), list):
        raise ResearchPlanValidationError("research_scope must contain included and excluded lists")
    budget = payload["budget_boundary"]
    if not isinstance(budget, dict):
        raise ResearchPlanValidationError("budget_boundary must be an object")
    for key in ("max_sources", "max_time_minutes"):
        value = budget.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ResearchPlanValidationError(f"budget_boundary.{key} must be a positive integer")
    return normalized


def research_plan_prompt(input_payload: dict[str, Any]) -> str:
    return (
        "You are preparing a research plan only. Do not search, fetch, download, transcribe, or analyze any "
        "video-platform material. Videos and comments may be noted only as topic-origin clues, never as factual evidence. "
        "Do not write a content plan, draft, review, or final answer. Return only a JSON object with these exact fields: "
        + ", ".join(RESEARCH_PLAN_REQUIRED_FIELDS)
        + ". research_scope must contain included and excluded arrays. budget_boundary must contain positive integer "
        "max_sources and max_time_minutes. Make blocking claims, evidence needs, material boundaries, risks, and uncertainty explicit. "
        "Use only the supplied formal-topic and user-requirement context.\n\n"
        f"Controlled input: {_canonical(input_payload)}"
    )


def _dotenv_value(reference: str, path: Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{reference}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _configured_environment_value(reference: str, *, env_path: Path = DEFAULT_MODEL_ENV_PATH) -> str:
    process_value = str(os.environ.get(reference) or "").strip()
    dotenv_value = _dotenv_value(reference, env_path)
    if process_value and dotenv_value and process_value != dotenv_value:
        raise ModelRouterError(f"conflicting values for configured environment reference: {reference}")
    value = process_value or dotenv_value
    if not value:
        raise ModelRouterError(f"configured environment reference is unresolved: {reference}")
    return value


def build_production_research_plan_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    """Build the one permitted formal Mimo path; it never calls a provider directly."""
    if core.data_identity != "production":
        raise StateTransitionError("production research-plan gateway requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("research-plan route must be explicitly bound with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None or provider.provider_type != "mimo":
        raise ModelRouterError("research-plan route must use the configured Mimo provider")
    route = router.resolve_bound_route("business_analysis", route_name="stage0.research_plan")
    if route.provider_name != "hermes":
        raise ModelRouterError("research-plan route has an unexpected provider adapter")
    auth_ref = str(provider.settings.get("auth_ref") or "")
    endpoint_ref = str(provider.settings.get("endpoint_ref") or "")
    if not auth_ref or not endpoint_ref:
        raise ModelRouterError("configured Mimo provider lacks auth_ref or endpoint_ref")
    api_key = _configured_environment_value(auth_ref)
    base_url = _configured_environment_value(endpoint_ref)
    if not base_url.startswith(("https://", "http://")):
        raise ModelRouterError("configured Mimo endpoint must be an HTTP(S) URL")
    adapter = HermesModelProviderAdapter(
        HermesModelProviderConfig(api_key=api_key, base_url=base_url, model=route.model_name, timeout_seconds=90, max_retries=0)
    )
    return ModelGateway(
        routes={route.route_name: route},
        providers={adapter.provider_name: adapter},
        materializer=CoreModelRunMaterializer(core),
    )


class Stage1AResearchPlanService:
    """Only the controlled formal-topic -> research-plan-awaiting-review path."""

    def __init__(self, *, core: Stage0ContentProductionCore, gateway: ModelGateway):
        self.core = core
        self.gateway = gateway

    def submit_formal_topic(
        self, *, topic_payload: dict[str, Any], actor: str, reason: str, idempotency_key: str
    ) -> dict[str, str]:
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
        request = self.core.prepare_model_request(
            task_id=task_id,
            node_version_id=request_version_id,
            prompt=research_plan_prompt(self.core.get_input_assembly_payload(request_version["input_assembly_id"])),
        )
        try:
            model_result = self.gateway.complete(request)
        except ModelGatewayError:
            raise
        try:
            raw_plan = json.loads(model_result.output_text)
            plan = validate_research_plan_payload(raw_plan)
        except (json.JSONDecodeError, ResearchPlanValidationError) as exc:
            message = "research plan model output must be valid JSON" if isinstance(exc, json.JSONDecodeError) else str(exc)
            self.core.record_model_validation_failure(
                task_id=task_id,
                node_version_id=request_version_id,
                model_run_id=model_result.envelope_version_id,
                reason=message,
            )
            raise ResearchPlanValidationError(message) from exc
        result = self.core.complete_node_from_model(
            task_id=task_id,
            node_version_id=request_version_id,
            model_run_id=model_result.envelope_version_id,
            output_ref=_payload_hash(plan),
            validation_status="passed",
            actor=actor,
            expected_task_revision=int(request_version["task_revision"]),
            idempotency_key=f"{idempotency_key}:complete",
            artifact_payload=plan,
        )
        self.core.record_completed_command(
            command="stage1a_generate_research_plan",
            idempotency_key=idempotency_key,
            request=service_request,
            task_id=task_id,
            event="stage1a_research_plan_generated",
            result=result,
        )
        return result

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
