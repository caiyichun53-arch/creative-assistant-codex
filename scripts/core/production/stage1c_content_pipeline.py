"""The controlled post-plan content chain.

Each model-backed step consumes only the prior approved artifact and retained
research references.  It never fetches facts, publishes, or skips a human
decision.  The final review produces the exact title and script consumed by the
controlled audio-production chain.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.runtime.liveness import budget_for
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.external_adapters.anysearch_executor import (
    AnySearchExecutionError,
    AnySearchExecutor,
    RESEARCH_EXECUTION_VERSION,
)
from scripts.core.production.stage0_content_core import (
    ARTIFACT_NODES,
    CoreModelRunMaterializer,
    InputAssembly,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.business_data.domain_labels import get_content_workflow_mode
from scripts.core.production.stage1a_research_plan import _configured_environment_value
from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired
from scripts.core.production.stage1d_audio_production import AudioProductionExecutor
from scripts.core.production.experience_candidate_proposal import (
    ExperienceCandidateProposalService,
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


_RESEARCH_EXCLUDED_HOSTS = frozenset({
    "baike.baidu.com",
    "wikipedia.org",
    "zh.wikipedia.org",
    "gugutm.com",
    "xgccm.com",
})
_RESEARCH_PUBLIC_DISCUSSION_HOSTS = frozenset({"m.weibo.cn", "weibo.com"})


def _screen_research_materials(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a bounded, role-correct handoff without mutating the source ledger."""
    task_materials = [
        item for item in materials
        if (item.get("material") or {}).get("research_execution") == RESEARCH_EXECUTION_VERSION
    ]
    # Once the task-oriented collector has produced material, the publisher
    # must read that execution only. Legacy one-batch material remains in the
    # ledger for provenance but cannot silently mix into the new run.
    if task_materials:
        materials = task_materials
    candidates: list[dict[str, Any]] = []
    for item in materials:
        source_ref = str(item.get("source_ref") or "").strip()
        title = str(item.get("title") or "").strip()
        parsed = urlsplit(source_ref)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        title_lower = title.casefold()
        if (
            host in _RESEARCH_EXCLUDED_HOSTS
            or "下载" in title
            or "无损音乐源" in title
            or "320kbps" in title_lower
            or (host == "sohu.com" and "/a/" in parsed.path)
            or (host == "163.com" and "/dy/" in parsed.path)
        ):
            continue
        screened = dict(item)
        original_material = dict(item.get("material") or {})
        # Keep the complete material in Core. The model handoff carries a
        # compact per-source pack so every task and every retained source can
        # participate without overflowing the provider context.
        material = {
            "provider": original_material.get("provider"),
            "research_execution": original_material.get("research_execution"),
            "research_task_id": original_material.get("research_task_id"),
            "research_task_label": original_material.get("research_task_label"),
            "research_task_objective": original_material.get("research_task_objective"),
            "research_task_question": original_material.get("research_task_question"),
            "research_sequence_context": original_material.get("research_sequence_context"),
            "research_source_guidance": original_material.get("research_source_guidance"),
            "retrieved_at": original_material.get("retrieved_at"),
        }
        extracted = str(original_material.get("extracted_content") or "")
        search_result = str(original_material.get("search_result") or "")
        material["extracted_content"] = extracted[:600] + ("\n[正文已截取]" if len(extracted) > 600 else "")
        material["search_result"] = search_result[:250] + ("\n[摘要已截取]" if len(search_result) > 250 else "")
        screened["material"] = material
        screened["evidence_role"] = (
            "audience_perception" if host in _RESEARCH_PUBLIC_DISCUSSION_HOSTS else "fact_evidence"
        )
        candidates.append(screened)

    # Preserve coverage across GPT Researcher tasks instead of filling the
    # handoff with the first task's largest sources.
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        task_key = str((item.get("material") or {}).get("research_task_id") or "unassigned")
        groups.setdefault(task_key, []).append(item)
    accepted: list[dict[str, Any]] = []
    total_chars = 0
    max_group_size = max((len(group) for group in groups.values()), default=0)
    # Two representative sources per task are enough for the publisher to
    # establish coverage and source mapping.  The complete source ledger stays
    # in Core; the smaller handoff prevents the model's visible JSON from being
    # cut off when a search task returns many long pages.
    max_sources_per_task = 2
    for offset in range(min(max_group_size, max_sources_per_task)):
        for group in groups.values():
            if offset >= len(group):
                continue
            item = group[offset]
            item_chars = len(json.dumps(item, ensure_ascii=False))
            if accepted and total_chars + item_chars > 40000:
                continue
            accepted.append(item)
            total_chars += item_chars
    for index, item in enumerate(accepted, start=1):
        item["source_key"] = f"material_{index:02d}"
    return accepted


def _research_execution_trace(
    materials: list[dict[str, Any]],
    *,
    approved_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    for item in materials:
        material = dict(item.get("material") or {})
        if material.get("research_execution") != RESEARCH_EXECUTION_VERSION:
            continue
        task_id = str(material.get("research_task_id") or "").strip()
        if not task_id:
            continue
        task = tasks.setdefault(task_id, {
            "research_task_id": task_id,
            "task_label": str(material.get("research_task_label") or ""),
            "objective": str(material.get("research_task_objective") or ""),
            "question": str(material.get("research_task_question") or ""),
            "source_count": 0,
            "sources": [],
        })
        task["source_count"] += 1
        task["sources"].append({
            "source_ref": str(item.get("source_ref") or ""),
            "title": str(item.get("title") or ""),
        })
    plan = approved_plan or {}
    questions = [str(value).strip() for value in (plan.get("research_questions") or []) if str(value).strip()]
    sequence = [str(value).strip() for value in (plan.get("research_sequence") or []) if str(value).strip()]
    source_plan = [str(value).strip() for value in (plan.get("source_plan") or []) if str(value).strip()]
    for index, question in enumerate(questions, start=1):
        task_id = f"research_task_{index:02d}"
        tasks.setdefault(task_id, {
            "research_task_id": task_id,
            "task_label": f"研究任务{index}",
            "objective": question,
            "question": question,
            "sequence_context": sequence[index - 1] if index <= len(sequence) else "",
            "source_guidance": source_plan[index - 1] if index <= len(source_plan) else "",
            "source_count": 0,
            "sources": [],
        })
    ordered_tasks = [tasks[key] for key in sorted(tasks)]
    return {
        "framework": "GPT Researcher",
        "execution_version": RESEARCH_EXECUTION_VERSION,
        "task_count": len(ordered_tasks),
        "tasks": ordered_tasks,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _derive_research_subject(topic_payload: dict[str, Any]) -> str:
    explicit = str(
        topic_payload.get("subject")
        or topic_payload.get("research_subject")
        or ""
    ).strip()
    if explicit:
        return explicit
    title = str(topic_payload.get("title") or "").strip()
    for suffix in ("人物传记", "人物研究", "人物生平", "传记研究", "专题研究", "传记", "研究"):
        if title.endswith(suffix) and title[:-len(suffix)].strip():
            return title[:-len(suffix)].strip()
    return title


def _validate_document(
    node: str,
    payload: Any,
    *,
    allowed_materials: list[dict[str, Any]] | None = None,
    expected_subject: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"node", "document", "source_boundaries", "unresolved"}:
        raise ContentPipelineValidationError("post-plan model output must contain exactly node, document, source_boundaries and unresolved")
    if payload["node"] != node:
        raise ContentPipelineValidationError("post-plan model output node does not match the active step")
    if not isinstance(payload["document"], dict) or not payload["document"]:
        raise ContentPipelineValidationError("post-plan model output needs a non-empty structured document")
    if node == "deep_research":
        expected_document = {
            "subject", "timeline", "career_stages", "representative_works",
            "turning_points", "historical_context", "public_memory", "current_status", "source_map",
        }
        if set(payload["document"]) != expected_document:
            raise ContentPipelineValidationError("deep research document must contain the fixed research sections")
        subject = payload["document"].get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise ContentPipelineValidationError("deep research subject must be a non-empty string")
        if expected_subject and subject.strip() != expected_subject.strip():
            raise ContentPipelineValidationError("deep research subject does not match the formal research subject")
        for key in sorted(expected_document - {"subject"}):
            value = payload["document"].get(key)
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                raise ContentPipelineValidationError(
                    f"deep research document {key} must be a non-empty string list"
                )
        if allowed_materials is not None:
            # Providers sometimes renumber the frozen source keys as
            # source_01/source_02 while preserving the same material order.
            # Canonicalize only that positional transport alias; no new source
            # can be introduced by this conversion.
            source_map = []
            normalized_unresolved = list(payload.get("unresolved") or [])
            for citation in payload["document"]["source_map"]:
                normalized_citation = citation
                for index, item in enumerate(allowed_materials, start=1):
                    canonical_key = str(item.get("source_key") or "").strip()
                    if canonical_key:
                        normalized_citation = normalized_citation.replace(
                            f"source_{index:02d}", canonical_key
                        )
                source_map.append(normalized_citation)
            payload["document"]["source_map"] = source_map
            allowed_citations = {
                value
                for item in allowed_materials
                for value in (
                    str(item.get("source_key") or "").strip(),
                    str(item.get("source_ref") or "").strip(),
                    str(item.get("title") or "").strip(),
                )
                if value
            }
            valid_source_map: list[str] = []
            for citation in payload["document"]["source_map"]:
                if not any(allowed in citation for allowed in allowed_citations):
                    if any(marker in citation for marker in (
                        "未确认", "未提供", "无对应来源", "没有对应来源", "材料中未找到",
                        "研究任务", "任务目标", "研究方案", "研究问题",
                    )):
                        normalized_unresolved.append(citation)
                        continue
                    raise ContentPipelineValidationError(
                        "deep research source_map cites a source outside the retained material set"
                    )
                valid_source_map.append(citation)
            payload["document"]["source_map"] = valid_source_map
            payload["unresolved"] = normalized_unresolved
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
    if any(
        definition is None
        or definition.provider_ref != "active_provider"
        or definition.fallback != "none"
        for definition in definitions
    ):
        raise ModelRouterError("every post-plan route must be explicitly configured with fallback none")
    limits = budget_for("model")
    routes = {}
    for contract in contracts:
        route_parameters = {"stream": False}
        if contract.formal_skill_id == "content_deep_research":
            route_parameters["max_tokens"] = 12000
        route = router.resolve_bound_route(
            contract.route_id, route_name=contract.route_name, parameters=route_parameters
        )
        routes[route.route_name] = route
    provider_refs = {route.provider_ref for route in routes.values()}
    if len(provider_refs) != 1:
        raise ModelRouterError("post-plan routes must use one active model service until multi-provider materialization is implemented")
    provider = router.resolve_bound_provider(next(iter(routes.values())))
    adapter = build_configured_model_provider(
        provider,
        next(iter(routes.values())),
        model_limits=limits,
    )
    return ModelGateway(routes=routes, providers={adapter.provider_name: adapter}, materializer=CoreModelRunMaterializer(core))


class Stage1CContentPipelineService:
    """Research dossier -> plan -> draft -> review, with Core-enforced human gates."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        gateway: ModelGateway | None = None,
        external_executor: Any | None = None,
    ):
        self.core = core
        # Keep the old constructor argument as compatibility only.  All
        # post-plan intelligent work now crosses the external-task boundary.
        del gateway
        self.gateway = None
        self.external_executor = external_executor

    def generate(
        self,
        *,
        task_id: str,
        actor: str,
        user_requirements: str,
        research_refs: tuple[dict[str, Any], ...] = (),
        considered_experience: tuple[dict[str, Any], ...] = (),
        idempotency_key: str,
    ) -> dict[str, Any]:
        task = self.core.get_task(task_id)
        node = str(task["current_node"])
        if node not in POST_PLAN_NODES or task["current_status"] != "not_started":
            raise StateTransitionError("post-plan generation requires the current approved next production step")
        if not actor.strip() or not user_requirements.strip():
            raise StateTransitionError("post-plan generation requires an explicit actor and requirements")
        if node == "deep_research" and research_refs:
            raise StateTransitionError("deep research reads retained Core research materials; do not pass transient references")
        if node == "deep_research":
            retained = self.core.list_research_materials(task_id=task_id)
            research_refs = tuple(_screen_research_materials(retained))
            task_materials = [
                item for item in retained
                if (item.get("material") or {}).get("research_execution") == RESEARCH_EXECUTION_VERSION
            ]
            if task_materials and not research_refs:
                raise StateTransitionError("deep research has no eligible retained materials after source screening")
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
            prompt_version=f"stage1c.{node}.prompt.v3.1" if node == "deep_research" else f"stage1c.{node}.prompt.v1",
            skill_version=f"stage1c.{node}.skill.v3.1" if node == "deep_research" else f"stage1c.{node}.skill.v1",
            model_config_version="model_routes.v1",
        )
        assembly_result = self.core.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        request_result = self.core.create_node_request(
            task_id=task_id, node=node, input_assembly_id=assembly_result["assembly_id"], actor=actor,
            idempotency_key=f"{idempotency_key}:request",
        )
        if node == "deep_research":
            try:
                AnySearchExecutor(core=self.core).run(
                    task_id=task_id,
                    topic=self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"],
                    approved_plan=self.core.get_artifact_payload(str(upstream_version_id))["payload"],
                    user_requirements=user_requirements,
                )
            except AnySearchExecutionError as exc:
                self.core.fail_current_node_from_model(
                    task_id=task_id,
                    node_version_id=request_result["node_version_id"],
                    model_run_id=None,
                    failure_stage="external_research",
                    reason=str(exc),
                    raw_model_output=None,
                )
                raise
        return self._complete_processing_version(
            task_id=task_id,
            node_version_id=request_result["node_version_id"],
            actor=actor,
            idempotency_key=idempotency_key,
        )
    def _deep_research_external_input(
        self, *, task_id: str, node_version_id: str
    ) -> dict[str, Any]:
        task = self.core.get_task(task_id)
        version = self.core.get_node_version(node_version_id)
        if task["current_node"] != "deep_research" or task["current_status"] != "processing" or task["current_version_id"] != node_version_id:
            raise StateTransitionError("deep research external task requires the current processing version")
        assembly_payload = self.core.get_input_assembly_payload(str(version["input_assembly_id"]))
        topic_payload = self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"]
        explicit_subject = str(topic_payload.get("subject") or topic_payload.get("research_subject") or "").strip()
        research_subject = _derive_research_subject(topic_payload)
        if not research_subject:
            raise StateTransitionError("deep research requires a formal research subject")
        retained_materials = _screen_research_materials(self.core.list_research_materials(task_id=task_id))
        if not retained_materials:
            raise StateTransitionError("deep research requires retained AnySearch materials; a model may not invent or fetch facts")
        assembly_payload.update({
            "research_topic_title": str(topic_payload.get("title") or "").strip(),
            "research_subject": research_subject,
            "expected_research_subject": explicit_subject,
            "research_refs": retained_materials,
            "research_execution": _research_execution_trace(
                retained_materials,
                approved_plan=self.core.get_artifact_payload(str(version["upstream_version_id"]))["payload"],
            ),
        })
        return {
            "correlation_id": task_id,
            "input_assembly": assembly_payload,
            "schema_version": "content_deep_research.input.v1",
        }

    def prepare_deep_research_external_task(self, *, task_id: str, node_version_id: str) -> dict[str, Any]:
        input_payload = self._deep_research_external_input(task_id=task_id, node_version_id=node_version_id)
        task_payload, _ = prepare_external_skill_task(
            FormalSkillContract.from_runtime_skill("content_deep_research"),
            input_payload,
            constraints={
                "use_only_core_retained_materials": True,
                "do_not_search": True,
                "cannot_change_business_state": True,
                "do_not_approve_or_skip_human_review": True,
                "preserve_source_boundaries": True,
            },
            business_context={
                "task_id": task_id,
                "node": "deep_research",
                "node_version_id": node_version_id,
                "data_identity": self.core.data_identity,
            },
        )
        return task_payload

    def submit_deep_research_external_result(
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
        input_payload = self._deep_research_external_input(task_id=task_id, node_version_id=node_version_id)
        model_run_id = self.core.record_external_node_execution(
            task_id=task_id, node_version_id=node_version_id,
            execution_id=execution_id, executor_id=executor_id, model_ref=model_ref,
            submitted_at=submitted_at, output_payload=output,
        )
        try:
            validated = validate_external_skill_output(
                FormalSkillContract.from_runtime_skill("content_deep_research"),
                input_payload,
                output,
            )
            retained_materials = list(input_payload["input_assembly"].get("research_refs") or [])
            result = _validate_document(
                "deep_research", validated,
                allowed_materials=retained_materials,
                expected_subject=str(input_payload["input_assembly"].get("expected_research_subject") or "").strip() or None,
            )
            result = dict(result)
            result["research_execution"] = input_payload["input_assembly"].get("research_execution") or _research_execution_trace(retained_materials)
        except (FormalSkillValidationError, ContentPipelineValidationError) as exc:
            self.core.record_model_validation_failure(
                task_id=task_id, node_version_id=node_version_id,
                model_run_id=model_run_id, reason=str(exc), raw_model_output=None,
            )
            raise FormalSkillValidationError(str(exc), model_run_envelope_version_id=model_run_id) from exc
        return self.core.complete_node_from_external_result(
            task_id=task_id, node_version_id=node_version_id, model_run_id=model_run_id,
            output_ref=_canonical(result), validation_status="passed", actor=actor,
            expected_task_revision=int(task["task_revision"]),
            idempotency_key=idempotency_key, artifact_payload=result,
        )

    def _content_external_input(
        self, *, task_id: str, node_version_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        task = self.core.get_task(task_id)
        version = self.core.get_node_version(node_version_id)
        node = str(version["node"])
        if (
            node not in {"content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"}
            or task["current_node"] != node
            or task["current_status"] != "processing"
            or task["current_version_id"] != node_version_id
            or version["task_id"] != task_id
        ):
            raise StateTransitionError("content external task requires the current processing version")
        assembly_payload = self.core.get_input_assembly_payload(str(version["input_assembly_id"]))
        input_payload = {
            "correlation_id": task_id,
            "input_assembly": assembly_payload,
            "schema_version": f"{CONTENT_SKILL_BY_NODE[node]}.input.v1",
        }
        return task, version, input_payload

    def prepare_content_external_task(
        self, *, task_id: str, node_version_id: str
    ) -> dict[str, Any]:
        task, version, input_payload = self._content_external_input(
            task_id=task_id, node_version_id=node_version_id
        )
        node = str(version["node"])
        if node == "content_plan":
            constraints = {
                "use_only_core_approved_materials": True,
                "match_current_topic": True,
                "report_experience_adoption_in_structured_submission_metadata": True,
                "cannot_change_business_state": True,
                "do_not_approve_or_skip_human_review": True,
            }
        elif node == "review":
            constraints = {
                "review_only_current_content_version": True,
                "do_not_modify_or_replace_content": True,
                "do_not_approve_or_publish": True,
                "cannot_change_business_state": True,
            }
        else:
            constraints = {
                "use_only_current_approved_planning_input": True,
                "content_text_is_a_result_field": True,
                "cannot_change_business_state": True,
                "do_not_skip_review": True,
            }
        task_payload, _ = prepare_external_skill_task(
            FormalSkillContract.from_runtime_skill(CONTENT_SKILL_BY_NODE[node]),
            input_payload,
            constraints=constraints,
            business_context={
                "task_id": task_id,
                "node": node,
                "node_version_id": node_version_id,
                "upstream_version_id": str(version["upstream_version_id"]),
                "input_assembly_id": str(version["input_assembly_id"]),
                "content_identity": str(task["topic_version_id"]),
                "data_identity": self.core.data_identity,
            },
        )
        return task_payload

    def submit_content_external_result(
        self,
        *,
        task_id: str,
        node_version_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output: dict[str, Any],
        experience_usage: Mapping[str, Any] | None = None,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        task, version, input_payload = self._content_external_input(
            task_id=task_id, node_version_id=node_version_id
        )
        node = str(version["node"])
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
                FormalSkillContract.from_runtime_skill(CONTENT_SKILL_BY_NODE[node]),
                input_payload,
                output,
            )
            document = _validate_document(node, validated)
        except (FormalSkillValidationError, ContentPipelineValidationError) as exc:
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
            output_ref=_canonical(document),
            validation_status="passed",
            actor=actor,
            expected_task_revision=int(task["task_revision"]),
            idempotency_key=idempotency_key,
            artifact_payload=document,
            experience_usage=(experience_usage if node == "content_plan" else None),
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
        if node == "deep_research":
            topic_payload = self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"]
            research_topic_title = str(topic_payload.get("title") or "").strip()
            explicit_research_subject = str(
                topic_payload.get("subject")
                or topic_payload.get("research_subject")
                or ""
            ).strip()
            research_subject = _derive_research_subject(topic_payload)
            if not research_subject:
                raise StateTransitionError("deep research requires a formal research subject")
            assembly_payload["research_topic_title"] = research_topic_title
            assembly_payload["research_subject"] = research_subject
            assembly_payload["expected_research_subject"] = explicit_research_subject
            retained_materials = _screen_research_materials(
                self.core.list_research_materials(task_id=task_id)
            )
            if not retained_materials:
                raise StateTransitionError(
                    "deep research requires retained AnySearch materials; a model may not invent or fetch facts"
                )
            # AnySearch runs after the input assembly is frozen so the source
            # ledger is the only mutable handoff into the model step.
            assembly_payload["research_refs"] = retained_materials
            approved_plan = self.core.get_artifact_payload(str(version["upstream_version_id"]))["payload"]
            assembly_payload["research_execution"] = _research_execution_trace(
                retained_materials,
                approved_plan=approved_plan,
            )
            external_task = self.prepare_deep_research_external_task(
                task_id=task_id, node_version_id=node_version_id
            )
            if self.external_executor is None:
                raise ExternalIntelligenceRequired(external_task)
            submission = self.external_executor(external_task)
            return self.submit_deep_research_external_result(
                task_id=task_id,
                node_version_id=node_version_id,
                execution_id=str(submission.get("execution_id") or ""),
                executor_id=str(submission.get("executor_id") or ""),
                model_ref=str(submission.get("model_ref") or "") or None,
                submitted_at=str(submission.get("submitted_at") or "") or None,
                output=submission.get("output"),
                actor=actor,
                idempotency_key=f"{idempotency_key}:external-complete",
            )
        external_task = self.prepare_content_external_task(
            task_id=task_id, node_version_id=node_version_id
        )
        if self.external_executor is None:
            return {
                "task_id": task_id,
                "node_version_id": node_version_id,
                "status": "requires_external_intelligence",
                "task": external_task,
            }
        submission = self.external_executor(external_task)
        return self.submit_content_external_result(
            task_id=task_id,
            node_version_id=node_version_id,
            execution_id=str(submission.get("execution_id") or ""),
            executor_id=str(submission.get("executor_id") or ""),
            model_ref=str(submission.get("model_ref") or "") or None,
            submitted_at=str(submission.get("submitted_at") or "") or None,
            output=submission.get("output"),
            experience_usage=submission.get("experience_usage") if node == "content_plan" else None,
            actor=actor,
            idempotency_key=f"{idempotency_key}:external-complete",
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
                    gateway=None,
                    external_executor=self.external_executor,
                ).prepare_for_content_plan(task_id=task_id, actor=actor)
                if candidate is not None:
                    if candidate.get("status") == "requires_external_intelligence":
                        return {
                            "task_id": task_id,
                            "status": "requires_external_intelligence",
                            "external_task": candidate.get("task"),
                            "experience_candidate": candidate,
                            "generated": generated,
                        }
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
            if output.get("status") == "requires_external_intelligence":
                return {
                    "task_id": task_id,
                    "status": "requires_external_intelligence",
                    "external_task": output.get("task"),
                    "generated": generated,
                }
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
        """Advance one formal content task; failed steps need an explicit user retry."""
        task = self.core.get_task(task_id)
        if task["current_status"] == "processing":
            node = str(task["current_node"])
            version_id = str(task["current_version_id"] or "")
            if node not in POST_PLAN_NODES or not version_id:
                raise StateTransitionError(
                    "the processing content step cannot be recovered because its current version is invalid"
                )
            if node == "deep_research":
                # Returning a research result creates a replacement version in
                # processing state.  It must collect a fresh execution batch
                # before the publisher is resumed; otherwise the returned
                # version would silently reuse the previous handoff.
                current_version = self.core.get_node_version(version_id)
                topic_payload = self.core.get_artifact_payload(
                    str(task["topic_version_id"])
                )["payload"]
                approved_plan = self.core.get_artifact_payload(
                    str(current_version["upstream_version_id"])
                )["payload"]
                try:
                    AnySearchExecutor(core=self.core).run(
                        task_id=task_id,
                        topic=topic_payload,
                        approved_plan=approved_plan,
                        user_requirements=user_requirements,
                    )
                except AnySearchExecutionError as exc:
                    self.core.fail_current_node_from_model(
                        task_id=task_id,
                        node_version_id=version_id,
                        model_run_id=None,
                        failure_stage="external_research",
                        reason=str(exc),
                        raw_model_output=None,
                    )
                    raise
            # This is an explicit user recovery of the current version.  It is
            # deliberately not an automatic retry and keeps the frozen input,
            # upstream version, and current node unchanged.
            resumed = self._complete_processing_version(
                task_id=task_id,
                node_version_id=version_id,
                actor=actor,
                idempotency_key=f"{idempotency_key}:{node}:resume",
            )
            current = self.core.get_task(task_id)
            topic_payload = self.core.get_artifact_payload(str(current["topic_version_id"]))["payload"]
            domain_label = str(topic_payload.get("domain_label") or topic_payload.get("domain") or "").strip()
            if node in HUMAN_REVIEW_NODES or (
                node in {"deep_research", "formal_draft"}
                and get_content_workflow_mode(domain_label) == "manual_guard"
            ):
                return {
                    "task_id": task_id,
                    "status": "awaiting_human_review",
                    "current_node": node,
                    "generated": [resumed],
                    "recovered": True,
                }
            if node not in AUTOMATIC_REFINEMENT_NODES:
                raise StateTransitionError("content task reached an unsupported recovered step")
            self.core.approve_current_node(
                task_id=task_id,
                version_id=str(current["current_version_id"]),
                actor="content_pipeline",
                actor_kind="system",
                reason="approved recovered automatic refinement step in the formal content contract",
                idempotency_key=f"{idempotency_key}:{node}:recovery-approval",
            )
            return self.advance_to_next_human_gate(
                task_id=task_id,
                actor=actor,
                user_requirements=user_requirements,
                idempotency_key=f"{idempotency_key}:after-recovery",
            )
        if task["current_status"] == "failed":
            self.core.requeue_failed_node_for_manual_retry(
                task_id=task_id,
                actor=actor,
                reason=user_requirements,
                idempotency_key=f"{idempotency_key}:manual-retry",
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
