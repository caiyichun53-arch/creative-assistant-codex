"""One bounded experience-candidate call before a content plan, never automatic publication."""

from __future__ import annotations

from typing import Any

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.formal_skill_adapter import FormalBusinessSkillAdapter, FormalSkillContract, FormalSkillValidationError
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import CoreExperienceCandidateModelRunMaterializer, Stage0ContentProductionCore, StateTransitionError
from scripts.core.production.stage1a_research_plan import _configured_environment_value
from scripts.core.runtime.liveness import budget_for


class ExperienceCandidateValidationError(StateTransitionError):
    pass


def build_production_experience_candidate_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    if core.data_identity != "production":
        raise StateTransitionError("experience candidate gateway requires production data")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("experience candidate requires an explicit business-analysis route with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None:
        raise ModelRouterError("experience candidate requires a configured model provider")
    limits = budget_for("model")
    route = router.resolve_bound_route("business_analysis", route_name="stage0.experience_candidate_propose", parameters={"stream": False})
    adapter = build_configured_model_provider(provider, route, model_limits=limits)
    return ModelGateway(
        routes={route.route_name: route}, providers={adapter.provider_name: adapter},
        materializer=CoreExperienceCandidateModelRunMaterializer(core),
    )


def _validate_output(value: Any, *, allowed_source_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"decision", "candidate", "source_ids"}:
        raise ExperienceCandidateValidationError("experience candidate output has an invalid structure")
    if value["decision"] == "no_proposal":
        if value["candidate"] is not None or value["source_ids"] != []:
            raise ExperienceCandidateValidationError("no-proposal output must not contain a candidate or source IDs")
        return value
    if value["decision"] != "proposal":
        raise ExperienceCandidateValidationError("experience candidate output has an invalid decision")
    sources = value["source_ids"]
    if not isinstance(sources, list) or len(sources) < 2 or len(set(sources)) != len(sources) or not set(sources).issubset(allowed_source_ids):
        raise ExperienceCandidateValidationError("experience candidate must cite at least two supplied source IDs")
    candidate = value["candidate"]
    if not isinstance(candidate, dict) or set(candidate) != {"summary", "applicable_when", "method", "boundary"}:
        raise ExperienceCandidateValidationError("experience candidate needs exactly summary, applicable_when, method and boundary")
    if not isinstance(candidate["summary"], str) or not candidate["summary"].strip():
        raise ExperienceCandidateValidationError("experience candidate summary is empty")
    for key in ("applicable_when", "method", "boundary"):
        if not isinstance(candidate[key], list) or not candidate[key] or not all(isinstance(item, str) and item.strip() for item in candidate[key]):
            raise ExperienceCandidateValidationError(f"experience candidate {key} must be a non-empty string list")
    banned = ("已经验证", "已验证", "一定有效", "普遍有效", "大家通常", "爆款证明", "高播放证明")
    text = " ".join([candidate["summary"], *candidate["applicable_when"], *candidate["method"], *candidate["boundary"]])
    if any(token in text for token in banned):
        raise ExperienceCandidateValidationError("experience candidate overstates effectiveness or commonness")
    return value


class ExperienceCandidateProposalService:
    def __init__(self, *, core: Stage0ContentProductionCore, gateway: ModelGateway):
        self.core = core
        self.gateway = gateway

    def prepare_for_content_plan(self, *, task_id: str, actor: str) -> dict[str, Any] | None:
        existing = self.core.list_task_experience_candidates(task_id=task_id)
        if existing:
            current = existing[-1]
            if current["status"] == "awaiting_human_decision":
                return current
            if current["status"] == "failed":
                # This optional suggestion failed, not the content plan itself.
                # Keep its failure record and continue without retrying it.
                return None
            return None
        selected = self.core.select_experience_candidate_sources(task_id=task_id)
        if selected is None:
            return None
        opened = self.core.open_experience_candidate(
            task_id=task_id, domain_label=selected["domain_label"], frozen_sources=selected["sources"], actor=actor,
        )
        candidate_id = opened["experience_candidate_id"]
        model_run_id: str | None = None
        raw_model_output: str | None = None
        try:
            task = self.core.get_task(task_id)
            upstream = self.core.get_artifact_payload(str(task["current_version_id"]))
            result = FormalBusinessSkillAdapter(
                contract=FormalSkillContract.from_runtime_skill("experience_candidate_propose"), gateway=self.gateway,
            ).run(
                {
                    "correlation_id": candidate_id,
                    "domain_label": selected["domain_label"],
                    "content_plan_context": {"topic": self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"], "approved_research": upstream["payload"]},
                    "frozen_breakdowns": selected["sources"],
                    "schema_version": "experience_candidate_propose.input.v1",
                },
                request_metadata={"experience_candidate_core": {"experience_candidate_id": candidate_id, "data_identity": self.core.data_identity}},
            )
            model_run_id = result.model_run_envelope_version_id
            raw_model_output = result.raw_model_output
            output = _validate_output(result.output_payload, allowed_source_ids={item["source_id"] for item in selected["sources"]})
        except (ModelGatewayError, FormalSkillValidationError, ExperienceCandidateValidationError) as exc:
            if isinstance(exc, FormalSkillValidationError):
                model_run_id = exc.model_run_envelope_version_id or model_run_id
                raw_model_output = exc.raw_model_output or raw_model_output
            self.core.fail_experience_candidate(
                experience_candidate_id=candidate_id, reason=str(exc), model_run_id=model_run_id,
                raw_model_output=raw_model_output,
            )
            # A new experience is optional for this plan.  Its failed atomic
            # run remains visible and is never retried automatically, while
            # the independently supported content plan may proceed.
            return None
        completed = self.core.complete_experience_candidate(
            experience_candidate_id=candidate_id, proposal=output, model_run_id=result.model_run_envelope_version_id,
        )
        return self.core.list_task_experience_candidates(task_id=task_id)[-1] if completed["status"] == "awaiting_human_decision" else None
