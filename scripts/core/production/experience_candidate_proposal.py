"""One bounded experience-candidate call before a content plan, never automatic publication."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.goal07_skill_runner import SkillContractError
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalBusinessSkillAdapter,
    FormalSkillContract,
    FormalSkillValidationError,
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import CoreExperienceCandidateModelRunMaterializer, Stage0ContentProductionCore, StateTransitionError
from scripts.core.production.stage1a_research_plan import _configured_environment_value
from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired
from scripts.core.runtime.liveness import budget_for


class ExperienceCandidateValidationError(StateTransitionError):
    pass


EXPERIENCE_LAYERS = frozenset({"structure", "section_method", "local_detail"})
EXPERIENCE_POSITIONS = frozenset({"whole_content", "opening", "body", "transition", "ending", "sentence"})


def build_production_experience_candidate_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    if core.data_identity != "production":
        raise StateTransitionError("experience candidate gateway requires production data")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("experience candidate requires an explicit business-analysis route with fallback none")
    limits = budget_for("model")
    route = router.resolve_bound_route("business_analysis", route_name="stage0.experience_candidate_propose", parameters={"stream": False})
    provider = router.resolve_bound_provider(route)
    adapter = build_configured_model_provider(provider, route, model_limits=limits)
    return ModelGateway(
        routes={route.route_name: route}, providers={adapter.provider_name: adapter},
        materializer=CoreExperienceCandidateModelRunMaterializer(core),
    )


def _existing_experience_summaries(
    core: Stage0ContentProductionCore,
    *,
    domain_label: str,
    experience_candidate_run_id: str | None = None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for experience in core.list_active_experiences(domain_label=domain_label):
        summary = str(experience.get("summary") or "").strip()
        if summary and summary not in seen:
            summaries.append({
                "status": "accepted",
                "summary": summary,
                "experience_layer": str(experience.get("experience_layer") or "section_method"),
                "use_positions": list(experience.get("use_positions") or []),
            })
            seen.add(summary)
    for candidate in core.list_pre_topic_experience_candidates(
        domain_label=domain_label, run_id=experience_candidate_run_id
    ):
        proposal = candidate.get("proposal") if isinstance(candidate.get("proposal"), dict) else {}
        body = proposal.get("candidate") if isinstance(proposal.get("candidate"), dict) else {}
        summary = str(body.get("summary") or "").strip()
        if summary and summary not in seen:
            summaries.append({
                "status": str(candidate.get("status") or "candidate"),
                "summary": summary,
                "experience_layer": str(body.get("experience_layer") or "section_method"),
                "use_positions": list(body.get("use_positions") or []),
            })
            seen.add(summary)
    return summaries


def _source_account_refs(sources: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item.get("source_id") or "").strip(): str(item.get("account_ref") or "").strip()
        for item in sources
        if str(item.get("source_id") or "").strip()
    }


def _validate_output(
    value: Any,
    *,
    allowed_source_ids: set[str],
    source_account_refs: dict[str, str],
    source_breakdowns: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"decision", "candidate", "source_ids"}:
        raise ExperienceCandidateValidationError("experience candidate output has an invalid structure")
    if value["decision"] == "no_proposal":
        if value["candidate"] is not None or value["source_ids"] != []:
            raise ExperienceCandidateValidationError("no-proposal output must not contain a candidate or source IDs")
        return value
    if value["decision"] != "proposal":
        raise ExperienceCandidateValidationError("experience candidate output has an invalid decision")
    sources = value["source_ids"]
    if not isinstance(sources, list) or len(sources) < 3 or len(set(sources)) != len(sources) or not set(sources).issubset(allowed_source_ids):
        raise ExperienceCandidateValidationError("experience candidate must cite at least three supplied source IDs")
    cited_accounts = {
        source_account_refs.get(str(source_id), "") for source_id in sources
        if source_account_refs.get(str(source_id), "")
    }
    if len(cited_accounts) < 2:
        raise ExperienceCandidateValidationError(
            "experience candidate must cite evidence from at least two accounts"
        )
    candidate = value["candidate"]
    if (
        isinstance(candidate, dict)
        and isinstance(candidate.get("summary"), list)
        and len(candidate["summary"]) == 1
        and isinstance(candidate["summary"][0], str)
    ):
        candidate = {**candidate, "summary": candidate["summary"][0]}
        value = {**value, "candidate": candidate}
    required_candidate_keys = {
        "summary", "experience_layer", "use_positions", "trigger_signals",
        "applicable_when", "method", "boundary", "not_applicable_when",
    }
    if not isinstance(candidate, dict) or set(candidate) != required_candidate_keys:
        raise ExperienceCandidateValidationError(
            "experience candidate needs summary, layer, placement, trigger, method and boundary fields"
        )
    if not isinstance(candidate["summary"], str) or not candidate["summary"].strip():
        raise ExperienceCandidateValidationError("experience candidate summary is empty")
    if candidate["experience_layer"] not in EXPERIENCE_LAYERS:
        raise ExperienceCandidateValidationError("experience candidate layer is invalid")
    positions = candidate["use_positions"]
    if (
        not isinstance(positions, list)
        or not positions
        or not all(isinstance(item, str) and item in EXPERIENCE_POSITIONS for item in positions)
        or len(set(positions)) != len(positions)
    ):
        raise ExperienceCandidateValidationError("experience candidate placements are invalid")
    for key in ("trigger_signals", "applicable_when", "method", "boundary", "not_applicable_when"):
        if not isinstance(candidate[key], list) or not candidate[key] or not all(isinstance(item, str) and item.strip() for item in candidate[key]):
            raise ExperienceCandidateValidationError(f"experience candidate {key} must be a non-empty string list")
    if candidate["experience_layer"] == "structure" and positions != ["whole_content"]:
        raise ExperienceCandidateValidationError("structure experience must target the whole content")
    if candidate["experience_layer"] == "structure":
        by_source_id = {
            str(item.get("source_id") or ""): item
            for item in source_breakdowns
            if isinstance(item, dict)
        }
        cited_progressions = 0
        for source_id in sources:
            breakdown = by_source_id.get(str(source_id)) or {}
            progression = breakdown.get("spoken_progression")
            if isinstance(progression, list) and progression:
                cited_progressions += 1
        if cited_progressions < 2:
            raise ExperienceCandidateValidationError(
                "structure experience needs real progression evidence from at least two cited breakdowns"
            )
    if candidate["experience_layer"] == "section_method" and not set(positions).intersection(
        {"opening", "body", "transition", "ending"}
    ):
        raise ExperienceCandidateValidationError("section experience must name a spoken section")
    if candidate["experience_layer"] == "local_detail":
        if positions != ["sentence"]:
            raise ExperienceCandidateValidationError(
                "local detail experience must be limited to the sentence placement"
            )
        if len(candidate["method"]) > 2:
            raise ExperienceCandidateValidationError(
                "local detail experience must be expressible in at most two adjacent sentence actions"
            )
        local_scope_markers = (
            "整条", "全文", "主体", "段落", "转场", "结尾", "每个条目", "逐段", "多个案例",
        )
        if any(marker in candidate["summary"] for marker in local_scope_markers):
            raise ExperienceCandidateValidationError(
                "local detail experience must not describe a section or whole-content scope"
            )

    text = " ".join([
        candidate["summary"], *candidate["trigger_signals"], *candidate["applicable_when"],
        *candidate["method"], *candidate["boundary"], *candidate["not_applicable_when"],
    ])
    list_markers = (
        "盘点", "清单", "榜单", "名次", "编号", "报出", "歌曲名称", "歌名", "歌词片段", "排序",
    )
    list_only_phrases = (
        "先报出编号或名次和歌曲名称",
        "名次和歌曲名称",
        "名次或对象名称，再插入一小段歌词",
        "按这个口径推进到最终排序",
    )
    substantive_markers = (
        "冲突", "反差", "原因", "语境", "创作", "处境", "关系", "对照", "问题", "意义", "判断",
        "评价", "场景", "转折", "真实指向", "内核", "机制",
    )
    if any(phrase in text for phrase in list_only_phrases) or (
        sum(marker in text for marker in list_markers) >= 2
        and not any(marker in text for marker in substantive_markers)
    ):
        raise ExperienceCandidateValidationError(
            "listicle formatting alone is not a reusable experience"
        )
    banned = (
        "已经验证", "已验证", "一定有效", "普遍有效", "有效", "成功", "成熟套路",
        "普遍适用", "通用方法", "大家通常", "爆款证明", "高播放证明",
    )
    banned = tuple(token for token in banned if token not in ("鏈夋晥", "鎴愬姛"))
    text = " ".join([
        candidate["summary"], candidate["experience_layer"], *candidate["use_positions"],
        *candidate["trigger_signals"], *candidate["applicable_when"], *candidate["method"],
        *candidate["boundary"], *candidate["not_applicable_when"],
    ])
    if any(token in text for token in banned):
        raise ExperienceCandidateValidationError("experience candidate overstates effectiveness or commonness")
    for clause in re.split(r"[，,。；;：:！？!?\n]", text):
        if not any(token in clause for token in ("有效", "成功")):
            continue
        if any(marker in clause for marker in ("不", "不能", "不可", "不要", "不得", "未", "没有", "避免", "禁止")):
            continue
        raise ExperienceCandidateValidationError("experience candidate overstates effectiveness or commonness")
    return value


class ExperienceCandidateProposalService:
    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        gateway: ModelGateway | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ):
        self.core = core
        # Keep the old constructor argument as transport compatibility only.
        # Experience proposals are always completed by an external executor;
        # this service must never retain a model gateway for that path.
        del gateway
        self.gateway = None
        self.external_executor = external_executor

    def _external_candidate_task(
        self,
        *,
        candidate_id: str,
        input_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        task, _ = prepare_external_skill_task(
            FormalSkillContract.from_runtime_skill("experience_candidate_propose"),
            input_payload,
            constraints={
                "use_only_supplied_material": True,
                "preserve_source_identity": True,
                "cannot_change_business_state": True,
                "proposal_is_not_a_formal_rule": True,
                "do_not_apply_without_user_decision": True,
            },
            business_context={
                "experience_candidate_id": candidate_id,
                "data_identity": self.core.data_identity,
                **context,
            },
        )
        return task

    def _submit_external_candidate(
        self,
        *,
        candidate_id: str,
        selected: dict[str, Any],
        input_payload: dict[str, Any],
        submission: Mapping[str, Any],
    ) -> dict[str, Any]:
        output = submission.get("output")
        model_run_id = self.core.record_external_experience_candidate_execution(
            experience_candidate_id=candidate_id,
            execution_id=str(submission.get("execution_id") or ""),
            executor_id=str(submission.get("executor_id") or ""),
            model_ref=str(submission.get("model_ref") or "") or None,
            submitted_at=str(submission.get("submitted_at") or "") or None,
            input_payload=input_payload,
            output_payload=output,
        )
        try:
            validated = validate_external_skill_output(
                FormalSkillContract.from_runtime_skill("experience_candidate_propose"),
                input_payload,
                output,
            )
            proposal = _validate_output(
                validated,
                allowed_source_ids={item["source_id"] for item in selected["sources"]},
                source_account_refs=_source_account_refs(selected["sources"]),
                source_breakdowns=selected["sources"],
            )
        except (FormalSkillValidationError, ExperienceCandidateValidationError) as exc:
            self.core.conn.execute(
                "UPDATE stage0_experience_candidate_model_run SET envelope_json=? WHERE experience_candidate_model_run_id=?",
                (json.dumps({"validation_error": str(exc), "execution_id": str(submission.get("execution_id") or "")}, ensure_ascii=False, sort_keys=True), model_run_id),
            )
            raise FormalSkillValidationError(str(exc), model_run_envelope_version_id=model_run_id) from exc
        return self.core.complete_experience_candidate(
            experience_candidate_id=candidate_id,
            proposal=proposal,
            model_run_id=model_run_id,
        )

    def submit_experience_candidate_external_result(
        self,
        *,
        task: Mapping[str, Any],
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit a task-package result through the Core candidate boundary."""
        context = task.get("business_context") if isinstance(task.get("business_context"), Mapping) else {}
        candidate_id = str(context.get("experience_candidate_id") or "")
        task_input = task.get("input") if isinstance(task.get("input"), Mapping) else {}
        sources = task_input.get("frozen_breakdowns")
        if not candidate_id or not isinstance(sources, list):
            raise StateTransitionError("external experience task is missing its Core identity")
        input_payload = {
            "correlation_id": candidate_id,
            "domain_label": task_input.get("domain_label"),
            "content_plan_context": task_input.get("content_plan_context"),
            "frozen_breakdowns": sources,
            "schema_version": "experience_candidate_propose.input.v1",
        }
        candidate = self.core._experience_candidate(candidate_id)
        completed = self._submit_external_candidate(
            candidate_id=candidate_id,
            selected={
                "domain_label": str(candidate["domain_label"]),
                "sources": list(sources),
                "content_type": "external",
            },
            input_payload=input_payload,
            submission={
                "execution_id": execution_id,
                "executor_id": executor_id,
                "model_ref": model_ref,
                "submitted_at": submitted_at,
                "output": output,
            },
        )
        return completed

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
        task = self.core.get_task(task_id)
        upstream = self.core.get_artifact_payload(str(task["current_version_id"]))
        input_payload = {
            "correlation_id": candidate_id,
            "domain_label": selected["domain_label"],
            "content_plan_context": {
                "topic": self.core.get_artifact_payload(str(task["topic_version_id"]))["payload"],
                "approved_research": upstream["payload"],
                "existing_experience_summaries": _existing_experience_summaries(
                    self.core, domain_label=selected["domain_label"]
                ),
            },
            "frozen_breakdowns": selected["sources"],
            "schema_version": "experience_candidate_propose.input.v1",
        }
        external_task = self._external_candidate_task(
            candidate_id=candidate_id,
            input_payload=input_payload,
            context={"task_id": task_id, "origin": "content_plan_experience_proposal"},
        )
        if self.external_executor is None:
            return {
                "experience_candidate_id": candidate_id,
                "status": "requires_external_intelligence",
                "source_ids": [item["source_id"] for item in selected["sources"]],
                "source_count": len(selected["sources"]),
                "content_type": selected["content_type"],
                "task": external_task,
            }
        submission = self.external_executor(external_task)
        completed = self._submit_external_candidate(
            candidate_id=candidate_id,
            selected=selected,
            input_payload=input_payload,
            submission=submission,
        )
        return self.core.list_task_experience_candidates(task_id=task_id)[-1] if completed["status"] == "awaiting_human_decision" else completed

    def prepare_before_topic(
        self,
        *,
        domain_label: str,
        actor: str,
        experience_candidate_run_id: str | None = None,
        new_run: bool = False,
    ) -> dict[str, Any] | None:
        """Propose one experience from frozen breakdowns before any topic exists."""

        if experience_candidate_run_id is None or new_run:
            run = self.core.start_pre_topic_experience_run(
                domain_label=domain_label, actor=actor
            )
            experience_candidate_run_id = str(run["experience_candidate_run_id"])
        else:
            self.core.get_pre_topic_experience_run(run_id=experience_candidate_run_id)
        existing = self.core.list_pre_topic_experience_candidates(
            domain_label=domain_label, run_id=experience_candidate_run_id
        )
        if existing:
            current = existing[-1]
            if current["status"] == "awaiting_human_decision":
                return current
            if current["status"] == "preparing":
                return None
        selected = self.core.select_pre_topic_experience_candidate_sources(
            domain_label=domain_label,
            experience_candidate_run_id=experience_candidate_run_id,
        )
        if selected is None:
            return None
        result = self._prepare_selected_before_topic(
            selected=selected,
            actor=actor,
            experience_candidate_run_id=experience_candidate_run_id,
        )
        if result["status"] == "requires_external_intelligence":
            return result
        if result["status"] != "awaiting_human_decision":
            return None
        return next(
            (
                item
                for item in self.core.list_pre_topic_experience_candidates(
                    domain_label=domain_label, run_id=experience_candidate_run_id
                )
                if item["experience_candidate_id"] == result["experience_candidate_id"]
            ),
            None,
        )

    def _prepare_selected_before_topic(
        self,
        *,
        selected: dict[str, Any],
        actor: str,
        experience_candidate_run_id: str | None = None,
    ) -> dict[str, Any]:
        opened = self.core.open_experience_candidate(
            task_id=None,
            domain_label=selected["domain_label"],
            frozen_sources=selected["sources"],
            actor=actor,
            experience_candidate_run_id=(
                experience_candidate_run_id
                or selected.get("experience_candidate_run_id")
            ),
        )
        candidate_id = opened["experience_candidate_id"]
        input_payload = {
            "correlation_id": candidate_id,
            "domain_label": selected["domain_label"],
            "content_plan_context": {
                "stage": "pre_topic_experience_review",
                "domain_label": selected["domain_label"],
                "purpose": "浠庡凡鍐荤粨鎷嗚В涓彁鍑轰竴鏉″緟鐢ㄦ埛纭鐨勫彲澶嶇敤瑙傚療锛涘綋鍓嶅皻鏈缓绔嬫寮忛€夐銆?",
                "existing_experience_summaries": _existing_experience_summaries(
                    self.core,
                    domain_label=selected["domain_label"],
                    experience_candidate_run_id=(experience_candidate_run_id or selected.get("experience_candidate_run_id")),
                ),
            },
            "frozen_breakdowns": selected["sources"],
            "schema_version": "experience_candidate_propose.input.v1",
        }
        external_task = self._external_candidate_task(
            candidate_id=candidate_id,
            input_payload=input_payload,
            context={
                "origin": "pre_topic_experience_proposal",
                "experience_candidate_run_id": experience_candidate_run_id,
            },
        )
        if self.external_executor is None:
            return {
                "experience_candidate_id": candidate_id,
                "status": "requires_external_intelligence",
                "source_ids": [item["source_id"] for item in selected["sources"]],
                "source_count": len(selected["sources"]),
                "content_type": selected["content_type"],
                "task": external_task,
            }
        submission = self.external_executor(external_task)
        completed = self._submit_external_candidate(
            candidate_id=candidate_id,
            selected=selected,
            input_payload=input_payload,
            submission=submission,
        )
        return {
            "experience_candidate_id": candidate_id,
            "status": completed["status"],
            "source_ids": [item["source_id"] for item in selected["sources"]],
            "source_count": len(selected["sources"]),
            "content_type": selected["content_type"],
        }

    def prepare_before_topic_batch(
        self,
        *,
        domain_label: str,
        actor: str,
        experience_candidate_run_id: str | None = None,
        new_run: bool = False,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Process eligible pre-topic source batches, optionally in resumable chunks."""

        if max_batches is not None and max_batches <= 0:
            raise ValueError("max_batches must be positive when supplied")

        if experience_candidate_run_id is None or new_run:
            run = self.core.start_pre_topic_experience_run(
                domain_label=domain_label, actor=actor
            )
            experience_candidate_run_id = str(run["experience_candidate_run_id"])
        else:
            self.core.get_pre_topic_experience_run(run_id=experience_candidate_run_id)
        batch_results: list[dict[str, Any]] = []
        failed_source_ids: set[str] = set()
        consecutive_failures = 0
        batch_limit = max_batches if max_batches is not None else 1000
        exhausted = False
        while len(batch_results) < batch_limit:
            selected = self.core.select_pre_topic_experience_candidate_sources(
                domain_label=domain_label,
                additional_covered_source_ids=failed_source_ids,
                experience_candidate_run_id=experience_candidate_run_id,
            )
            if selected is None:
                exhausted = True
                break
            result = self._prepare_selected_before_topic(
                selected=selected,
                actor=actor,
                experience_candidate_run_id=experience_candidate_run_id,
            )
            batch_results.append(result)
            if result["status"] == "requires_external_intelligence":
                return {
                    "status": "requires_external_intelligence",
                    "domain_label": domain_label,
                    "experience_candidate_run_id": experience_candidate_run_id,
                    "experience_candidate_id": result["experience_candidate_id"],
                    "external_task": result.get("task"),
                    "source_count": result["source_count"],
                    "resumable_chunk": True,
                }
            if result["status"] == "failed":
                failed_source_ids.update(str(item) for item in result["source_ids"])
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    break
            else:
                consecutive_failures = 0

        run_summary = (
            self.core.complete_pre_topic_experience_run(run_id=experience_candidate_run_id)
            if max_batches is None or exhausted
            else self.core.summarize_pre_topic_experience_run(run_id=experience_candidate_run_id)
        )
        current_candidates = self.core.list_pre_topic_experience_candidates(
            domain_label=domain_label, run_id=experience_candidate_run_id
        )
        awaiting = [
            str(item["experience_candidate_id"])
            for item in current_candidates
            if item["status"] == "awaiting_human_decision"
        ]
        no_proposal = sum(item["status"] == "no_proposal" for item in current_candidates)
        failed = [
            str(item["experience_candidate_id"])
            for item in current_candidates
            if item["status"] == "failed"
        ]
        stopped_after_repeated_failures = consecutive_failures >= 3
        return {
            "status": (
                "stopped_after_repeated_failures"
                if stopped_after_repeated_failures
                else str(run_summary["run_status"])
            ),
            "domain_label": domain_label,
            "experience_candidate_run_id": experience_candidate_run_id,
            "batch_count": len(batch_results),
            "source_count": sum(int(item["source_count"]) for item in batch_results),
            "candidate_count": len(awaiting),
            "no_proposal_count": no_proposal,
            "failed_batch_count": len(failed),
            "candidate_ids": awaiting,
            "failed_candidate_ids": failed,
            "automatic_retry": False,
            "active_source_count": run_summary["active_source_count"],
            "processed_source_count": run_summary["processed_source_count"],
            "failed_source_count": run_summary["failed_source_count"],
            "unprocessed_source_count": run_summary["unprocessed_source_count"],
            "unprocessed_source_ids": run_summary["unprocessed_source_ids"],
            "resumable_chunk": max_batches is not None and not exhausted,
        }
