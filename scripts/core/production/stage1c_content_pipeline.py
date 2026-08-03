"""The controlled post-plan content chain.

Each model-backed step consumes only the prior approved artifact and retained
research references.  It never fetches facts, publishes, or skips a human
decision.  The final review produces the exact title and script consumed by the
controlled audio-production chain.
"""

from __future__ import annotations

import json
from typing import Any

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalBusinessSkillAdapter,
    FormalSkillContract,
    FormalSkillValidationError,
)
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.runtime.liveness import budget_for
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import (
    ARTIFACT_NODES,
    CoreModelRunMaterializer,
    InputAssembly,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.business_data.domain_labels import get_content_workflow_mode
from scripts.core.production.stage1a_research_plan import _configured_environment_value
from scripts.core.production.stage1d_audio_production import AudioProductionExecutor
from scripts.core.production.experience_candidate_proposal import (
    ExperienceCandidateProposalService,
    build_production_experience_candidate_gateway,
)


POST_PLAN_NODES = frozenset({
    "deep_research", "content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review",
})
CONTENT_SKILL_BY_NODE = {
    "deep_research": "content_deep_research",
    "content_plan": "content_plan_generation",
    "formal_draft": "formal_draft_generate",
    "copy_optimization": "copy_optimization",
    "de_ai_revision": "de_ai_revision",
    "review": "final_content_review",
}
HUMAN_REVIEW_NODES = frozenset(
    {"content_plan", "review"}
)
AUTOMATIC_REFINEMENT_NODES = frozenset(
    {"deep_research", "formal_draft", "copy_optimization", "de_ai_revision"}
)


class ContentPipelineValidationError(StateTransitionError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_document(node: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"node", "document", "source_boundaries", "unresolved"}:
        raise ContentPipelineValidationError("post-plan model output must contain exactly node, document, source_boundaries and unresolved")
    if payload["node"] != node:
        raise ContentPipelineValidationError("post-plan model output node does not match the active step")
    if not isinstance(payload["document"], dict) or not payload["document"]:
        raise ContentPipelineValidationError("post-plan model output needs a non-empty structured document")
    if node == "review":
        title = payload["document"].get("title")
        script_text = payload["document"].get("script_text")
        if not isinstance(title, str) or not title.strip() or not isinstance(script_text, str) or not script_text.strip():
            raise ContentPipelineValidationError("final review document must contain non-empty title and script_text")
    for key in ("source_boundaries", "unresolved"):
        if not isinstance(payload[key], list) or not all(isinstance(item, str) and item.strip() for item in payload[key]):
            raise ContentPipelineValidationError(f"post-plan model output {key} must be a string list")
    return payload


def build_production_content_pipeline_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    """Build only the explicit business and writing routes; no fallback or silent provider switch is permitted."""
    if core.data_identity != "production":
        raise StateTransitionError("production content-pipeline gateway requires production data identity")
    router = ModelRouter.from_file()
    contracts = [
        FormalSkillContract.from_runtime_skill(CONTENT_SKILL_BY_NODE[node])
        for node in sorted(POST_PLAN_NODES)
    ]
    route_ids = {contract.route_id for contract in contracts}
    definitions = [router.routes.get(route_id) for route_id in route_ids]
    if any(definition is None or definition.fallback != "none" for definition in definitions):
        raise ModelRouterError("every post-plan route must be explicitly configured with fallback none")
    provider_refs = {definition.provider_ref for definition in definitions if definition is not None}
    if len(provider_refs) != 1:
        raise ModelRouterError("post-plan routes must use one explicit configured provider until multi-provider materialization is implemented")
    provider = router.providers[next(iter(provider_refs))]
    limits = budget_for("model")
    routes = {}
    for contract in contracts:
        route = router.resolve_bound_route(
            contract.route_id, route_name=contract.route_name, parameters={"stream": False}
        )
        routes[route.route_name] = route
    adapter = build_configured_model_provider(
        provider,
        next(iter(routes.values())),
        model_limits=limits,
    )
    return ModelGateway(routes=routes, providers={adapter.provider_name: adapter}, materializer=CoreModelRunMaterializer(core))


class Stage1CContentPipelineService:
    """Research dossier -> plan -> draft -> review, with Core-enforced human gates."""

    def __init__(self, *, core: Stage0ContentProductionCore, gateway: ModelGateway):
        self.core = core
        self.gateway = gateway

    def generate(
        self,
        *,
        task_id: str,
        actor: str,
        user_requirements: str,
        research_refs: tuple[dict[str, Any], ...] = (),
        considered_experience: tuple[dict[str, Any], ...] = (),
        idempotency_key: str,
    ) -> dict[str, str]:
        task = self.core.get_task(task_id)
        node = str(task["current_node"])
        if node not in POST_PLAN_NODES or task["current_status"] != "not_started":
            raise StateTransitionError("post-plan generation requires the current approved next production step")
        if not actor.strip() or not user_requirements.strip():
            raise StateTransitionError("post-plan generation requires an explicit actor and requirements")
        if node == "deep_research" and research_refs:
            raise StateTransitionError("deep research reads retained Core research materials; do not pass transient references")
        if node == "deep_research":
            research_refs = tuple(self.core.list_research_materials(task_id=task_id))
        if node == "deep_research" and not research_refs:
            raise StateTransitionError("deep research requires retained external research references; a model may not invent or fetch facts")
        if node == "content_plan":
            topic = self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"]
            domain_label = str(topic.get("domain_label") or "").strip()
            if domain_label:
                approved_research = self.core.get_artifact_payload(str(task["current_version_id"]))["payload"]
                context_text = _canonical({
                    "topic": topic,
                    "user_requirements": user_requirements,
                    "approved_research": approved_research,
                })
                retained = tuple(self.core.list_active_experiences(
                    domain_label=domain_label, context_text=context_text, limit=3,
                ))
                retained_by_id = {str(item["experience_id"]): item for item in retained}
                requested_ids = [str(item.get("experience_id") or "") for item in considered_experience]
                considered_experience = tuple(
                    retained_by_id[experience_id]
                    for experience_id in requested_ids
                    if experience_id in retained_by_id
                ) + tuple(
                    item for item in retained if str(item["experience_id"]) not in requested_ids
                )
        upstream_version_id = str(task["current_version_id"] or "")
        upstream = self.core.get_artifact_payload(upstream_version_id)
        assembly = InputAssembly(
            task_id=task_id,
            node=node,
            upstream_version_id=upstream_version_id,
            user_requirements=user_requirements,
            material_refs=(
                {"kind": "approved_upstream_artifact", "version_id": upstream_version_id, "artifact_kind": upstream["artifact_kind"]},
            ),
            research_refs=research_refs,
            content_plan_ref={"version_id": upstream_version_id} if node in {"formal_draft", "copy_optimization", "de_ai_revision", "review"} else None,
            considered_experience=considered_experience,
            adopted_experience=(),
            rejected_experience=(),
            omitted_materials=(),
            prompt_version=f"stage1c.{node}.prompt.v1",
            skill_version=f"stage1c.{node}.skill.v1",
            model_config_version="model_routes.v1",
        )
        assembly_result = self.core.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        request_result = self.core.create_node_request(
            task_id=task_id, node=node, input_assembly_id=assembly_result["assembly_id"], actor=actor,
            idempotency_key=f"{idempotency_key}:request",
        )
        return self._complete_processing_version(
            task_id=task_id,
            node_version_id=request_result["node_version_id"],
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def _complete_processing_version(
        self,
        *,
        task_id: str,
        node_version_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        task = self.core.get_task(task_id)
        version = self.core.get_node_version(node_version_id)
        node = str(version["node"])
        if (
            node not in POST_PLAN_NODES
            or task["current_node"] != node
            or task["current_status"] != "processing"
            or task["current_version_id"] != node_version_id
        ):
            raise StateTransitionError(
                "content generation requires the current processing version"
            )
        assembly_payload = self.core.get_input_assembly_payload(
            str(version["input_assembly_id"])
        )
        skill_id = CONTENT_SKILL_BY_NODE[node]
        try:
            skill_result = FormalBusinessSkillAdapter(
                contract=FormalSkillContract.from_runtime_skill(skill_id),
                gateway=self.gateway,
            ).run(
                {
                    "correlation_id": task_id,
                    "input_assembly": assembly_payload,
                    "schema_version": f"{skill_id}.input.v1",
                },
                request_metadata=self.core.prepare_atomic_skill_binding(
                    task_id=task_id, node_version_id=node_version_id
                ),
            )
        except FormalSkillValidationError as exc:
            self.core.fail_current_node_from_model(
                task_id=task_id, node_version_id=node_version_id,
                model_run_id=exc.model_run_envelope_version_id,
                failure_stage=("model_output_validation" if exc.model_run_envelope_version_id else "model_execution"),
                reason=str(exc), raw_model_output=exc.raw_model_output,
            )
            raise
        except ModelGatewayError as exc:
            self.core.fail_current_node_from_model(
                task_id=task_id, node_version_id=node_version_id,
                model_run_id=exc.model_run_envelope_version_id,
                failure_stage="model_execution", reason=str(exc), raw_model_output=None,
            )
            raise
        try:
            output = _validate_document(node, skill_result.output_payload)
        except ContentPipelineValidationError as exc:
            self.core.record_model_validation_failure(
                task_id=task_id, node_version_id=node_version_id,
                model_run_id=skill_result.model_run_envelope_version_id, reason=str(exc), raw_model_output=skill_result.raw_model_output,
            )
            raise ContentPipelineValidationError(str(exc)) from exc
        return self.core.complete_node_from_model(
            task_id=task_id, node_version_id=node_version_id,
            model_run_id=skill_result.model_run_envelope_version_id, output_ref=_canonical(output), validation_status="passed",
            actor=actor, expected_task_revision=int(task["task_revision"]),
            idempotency_key=f"{idempotency_key}:complete", artifact_payload=output,
        )

    def advance_to_next_human_gate(
        self,
        *,
        task_id: str,
        actor: str,
        user_requirements: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Generate the next formal output and auto-run only non-human refinement steps."""
        generated: list[dict[str, str]] = []
        while True:
            task = self.core.get_task(task_id)
            node = str(task["current_node"])
            status = str(task["current_status"])
            if node == "user_final_confirmation" and status == "approved":
                return {
                    "task_id": task_id,
                    "status": "final_content_approved",
                    "generated": generated,
                }
            if status == "awaiting_human_review":
                return {
                    "task_id": task_id,
                    "status": "awaiting_human_review",
                    "current_node": node,
                    "generated": generated,
                }
            if status != "not_started" or node not in POST_PLAN_NODES:
                raise StateTransitionError(
                    "content task is not ready to advance from its current state"
                )
            if node == "content_plan":
                candidate = ExperienceCandidateProposalService(
                    core=self.core,
                    gateway=build_production_experience_candidate_gateway(self.core),
                ).prepare_for_content_plan(task_id=task_id, actor=actor)
                if candidate is not None:
                    return {
                        "task_id": task_id,
                        "status": "awaiting_experience_confirmation",
                        "experience_candidate": candidate,
                        "generated": generated,
                    }
            output = self.generate(
                task_id=task_id,
                actor=actor,
                user_requirements=user_requirements,
                idempotency_key=f"{idempotency_key}:{node}",
            )
            generated.append(output)
            topic_payload = self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"]
            domain_label = str(topic_payload.get("domain_label") or topic_payload.get("domain") or "").strip()
            if node in HUMAN_REVIEW_NODES or (
                node in {"deep_research", "formal_draft"}
                and get_content_workflow_mode(domain_label) == "manual_guard"
            ):
                return {
                    "task_id": task_id,
                    "status": "awaiting_human_review",
                    "current_node": node,
                    "generated": generated,
                }
            if node not in AUTOMATIC_REFINEMENT_NODES:
                raise StateTransitionError(
                    "content task reached an unsupported automatic step"
                )
            current = self.core.get_task(task_id)
            self.core.approve_current_node(
                task_id=task_id,
                version_id=str(current["current_version_id"]),
                actor="content_pipeline",
                actor_kind="system",
                reason="approved automatic refinement step in the formal content contract",
                idempotency_key=f"{idempotency_key}:{node}:automatic-approval",
            )

    def advance_formal_content(
        self,
        *,
        task_id: str,
        actor: str,
        user_requirements: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Advance one formal content task without re-running an in-flight or failed model step."""
        task = self.core.get_task(task_id)
        if task["current_status"] in {"processing", "failed"}:
            raise StateTransitionError(
                "a formal content model step is already running or has failed; it will not be retried"
            )
        return self.advance_to_next_human_gate(
            task_id=task_id,
            actor=actor,
            user_requirements=user_requirements,
            idempotency_key=idempotency_key,
        )

    def approve(self, *, task_id: str, version_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        return self.core.approve_current_node(
            task_id=task_id, version_id=version_id, actor=actor, actor_kind="user", reason=reason,
            idempotency_key=idempotency_key,
        )

    def return_for_revision(
        self, *, task_id: str, version_id: str, actor: str, requirements: str, idempotency_key: str
    ) -> dict[str, str]:
        if not requirements.strip():
            raise StateTransitionError("a returned production step requires explicit revision requirements")
        task = self.core.get_task(task_id)
        previous = self.core.get_node_version(version_id)
        if task["current_version_id"] != version_id or task["current_status"] != "awaiting_human_review":
            raise StateTransitionError("only the current awaiting production output may be returned")
        previous_assembly = self.core.get_input_assembly_payload(previous["input_assembly_id"])
        replacement = InputAssembly(
            task_id=task_id, node=previous["node"], upstream_version_id=previous["upstream_version_id"],
            user_requirements=requirements, material_refs=tuple(previous_assembly["material_refs"]),
            research_refs=tuple(previous_assembly["research_refs"]), content_plan_ref=previous_assembly["content_plan_ref"],
            considered_experience=tuple(previous_assembly["considered_experience"]), adopted_experience=(),
            rejected_experience=(), omitted_materials=(), prompt_version=previous_assembly["prompt_version"],
            skill_version=previous_assembly["skill_version"], model_config_version=previous_assembly["model_config_version"],
        )
        assembly = self.core.create_input_assembly(replacement, idempotency_key=f"{idempotency_key}:assembly")
        return self.core.return_current_node(
            task_id=task_id, version_id=version_id, input_assembly_id=assembly["assembly_id"], actor=actor,
            reason=requirements, idempotency_key=f"{idempotency_key}:return",
        )
