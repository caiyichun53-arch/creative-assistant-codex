"""TEST-only full standard-chain verification.

This test deliberately uses a disposable business environment.  Static domain
rules are copied from the repository; business results are created only by
Core and by the normal external-task submission boundaries.
"""

from __future__ import annotations

import json
import hashlib
import threading
import wave
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch
from urllib.request import Request, urlopen

from scripts.core.external_adapters.local_voxcpm2_executor import VoxCPM2SynthesisResult
from scripts.core.business_data.domain_labels import get_content_type_registry
from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.experience_candidate_proposal import ExperienceCandidateProposalService
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.cold_start_orchestrator import ColdStartExecutionOrchestrator
from scripts.core.production.high_signal_policy import (
    build_high_signal_artifact,
    build_historical_collection_artifact,
)
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
    CompetitorRegistrationService,
)
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    ExternalIntelligenceRequired,
)
from scripts.core.production.stage1d_audio_production import AudioProductionService
from scripts.web.read_only_server import create_action_server
from tests._fresh_test_environment import FreshTestEnvironment
from tests.test_formal_production_mode_chain import (
    _TestAnySearchExecutor,
    _content_output,
    _research_plan_output,
)


ACTOR = "TEST user"
DOMAIN = "music_entertainment"


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict[str, Any]:
    with urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


def _submit_formal_external_result(
    *,
    business: CreationAssistantFormalBusinessCore,
    core: Stage0ContentProductionCore,
    task_id: str,
    output: dict[str, Any],
    execution_id: str,
    experience_usage: Mapping[str, Any] | None = None,
    validation_usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task = core.get_task(task_id)
    assert task["current_status"] == "processing"
    return business.submit_formal_external_result(
        task_id=task_id,
        node_version_id=str(task["current_version_id"]),
        execution_id=execution_id,
        executor_id="test-fake-executor",
        model_ref="test-fake-model",
        submitted_at="2026-08-31T00:00:00+08:00",
        output=output,
        actor="test-fake-executor",
        idempotency_key=f"test-full-e2e:{execution_id}",
        experience_usage=experience_usage,
        validation_usage=validation_usage,
    )


def _formal_external_task_from_continuation(
    continuation: Mapping[str, Any],
) -> dict[str, Any]:
    assert continuation.get("status") == "requires_external_intelligence", continuation
    task = continuation.get("external_task")
    assert isinstance(task, dict), continuation
    return task


def _formal_fake_submission(
    task: Mapping[str, Any],
    *,
    experience_candidate_id: str | None,
) -> tuple[
    dict[str, Any],
    Mapping[str, Any] | None,
    Mapping[str, Any] | None,
]:
    business_context = task.get("business_context")
    assert isinstance(business_context, Mapping), task
    node = str(business_context.get("node") or "").strip()
    assert node in {
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
    }, task
    if node == "research_plan":
        output = _research_plan_output()
    else:
        output = _content_output(node)
    experience_usage = None
    validation_usage = None
    if node == "content_plan":
        input_payload = task.get("input")
        assert isinstance(input_payload, Mapping), task
        input_assembly = input_payload.get("input_assembly")
        assert isinstance(input_assembly, Mapping), task

        considered_experience = input_assembly.get("considered_experience") or []
        assert isinstance(considered_experience, list), task
        considered_experience_ids: list[str] = []
        for item in considered_experience:
            assert isinstance(item, Mapping), task
            experience_id = str(item.get("experience_id") or "").strip()
            assert experience_id, task
            considered_experience_ids.append(experience_id)
        experience_usage = {
            "adopted_experience_ids": [],
            "not_adopted_experience_ids": considered_experience_ids,
            "rationale": "TEST按Core本次内容计划提供的经验集合记录本次未采用决定",
        }

        considered_validation = input_assembly.get("considered_validation_candidates") or []
        assert isinstance(considered_validation, list), task
        considered_validation_ids: list[str] = []
        for item in considered_validation:
            assert isinstance(item, Mapping), task
            candidate_id = str(item.get("experience_candidate_id") or "").strip()
            assert candidate_id, task
            considered_validation_ids.append(candidate_id)
        if considered_validation_ids:
            adopted_validation_ids = (
                [experience_candidate_id]
                if experience_candidate_id in considered_validation_ids
                else []
            )
            validation_usage = {
                "adopted_candidate_ids": adopted_validation_ids,
                "not_adopted_candidate_ids": [
                    candidate_id
                    for candidate_id in considered_validation_ids
                    if candidate_id not in adopted_validation_ids
                ],
                "rationale": "TEST按Core本次内容计划提供的验证候选集合记录验证使用决定",
            }
    return output, experience_usage, validation_usage


def _run_formal_external_task_loop(
    *,
    business: CreationAssistantFormalBusinessCore,
    core: Stage0ContentProductionCore,
    task_id: str,
    initial_external_task: Mapping[str, Any],
    actor: str,
    experience_candidate_id: str | None,
) -> dict[str, Any]:
    """Consume Core-issued tasks and confirm only the gates Core exposes."""

    external_task = dict(initial_external_task)
    execution_number = 0
    approval_number = 0
    while True:
        current = core.get_task(task_id)
        assert current["current_status"] == "processing", current
        business_context = external_task.get("business_context")
        assert isinstance(business_context, Mapping), external_task
        assert business_context.get("task_id") == task_id, external_task
        assert business_context.get("node_version_id") == current["current_version_id"], external_task
        output, experience_usage, validation_usage = _formal_fake_submission(
            external_task,
            experience_candidate_id=experience_candidate_id,
        )
        execution_number += 1
        submitted = _submit_formal_external_result(
            business=business,
            core=core,
            task_id=task_id,
            output=output,
            execution_id=f"test-formal-external-{execution_number}",
            experience_usage=experience_usage,
            validation_usage=validation_usage,
        )
        continuation = submitted["continuation"]
        assert isinstance(continuation, Mapping), submitted
        current = core.get_task(task_id)
        if current["current_status"] == "awaiting_human_review":
            approval_number += 1
            try:
                approved = business.approve_formal_production_node(
                    task_id=task_id,
                    actor=actor,
                    reason=f"TEST确认Core当前人工Gate-{approval_number}",
                    idempotency_key=f"test-full-e2e:approve-formal-gate-{approval_number}",
                )
            except ExternalIntelligenceRequired as exc:
                next_external_task = dict(exc.task)
                after_approval = core.get_task(task_id)
                assert after_approval["current_status"] == "processing", after_approval
                assert next_external_task["business_context"]["task_id"] == task_id
                assert next_external_task["business_context"]["node_version_id"] == after_approval["current_version_id"]
                external_task = next_external_task
                continue
            continuation = approved["continuation"]
            assert isinstance(continuation, Mapping), approved
            current = core.get_task(task_id)
            if (
                current["current_node"] == "user_final_confirmation"
                and current["current_status"] == "approved"
            ):
                assert continuation.get("status") == "awaiting_final_confirmation", approved
                return current
        if (
            current["current_node"] == "user_final_confirmation"
            and current["current_status"] == "approved"
        ):
            assert continuation.get("status") == "awaiting_final_confirmation", continuation
            return current
        external_task = _formal_external_task_from_continuation(continuation)


class _ColdStartExternalFixture:
    """Deterministic collection fixture plus the real breakdown boundary."""

    def __init__(self, *, core: Stage0ContentProductionCore, runtime_root: Path) -> None:
        self.core = core
        self.runtime_root = runtime_root
        self.received_breakdown_tasks: list[dict[str, Any]] = []
        self._execution_number = 0
        self.breakdown_executor = object.__new__(ConfiguredCompetitorRegistrationExecutor)
        self.breakdown_executor.core = core
        self.breakdown_executor.gateway = None
        self.breakdown_executor.external_executor = self._execute_breakdown_task
        self.breakdown_executor.progress_callback = None
        self.breakdown_executor.on_material_change = None

    def _execute_breakdown_task(self, task: dict[str, Any]) -> dict[str, Any]:
        self.received_breakdown_tasks.append(dict(task))
        self._execution_number += 1
        source_id = str(task["business_context"]["source_id"])
        return {
            "execution_id": f"test-breakdown-execution-{self._execution_number}",
            "executor_id": "test-fake-executor",
            "model_ref": "test-fake-model",
            "submitted_at": "2026-08-31T00:00:00+08:00",
            "output": {
                "source_id": source_id,
                "source_content_type": "人物经历故事",
                "analysis_text": (
                    "WHAT\n测试材料的核心对象和命题。\n"
                    "HOW\n测试材料中的推进动作及其关系。\n"
                    "SO WHAT\n无有效复用参考。"
                ),
                "boundary_observation": "材料展示了具体人物及其变化或影响",
                "schema_version": "competitor_breakdown.output.raw.v5",
            },
        }

    def execute(self, *, step_name: str, registration: dict[str, Any], completed_artifacts: tuple[dict[str, Any], ...]):
        registration_id = str(registration["registration_id"])
        if step_name == "historical_material":
            items = [
                {
                    "source_id": f"{registration_id}-video-{index}",
                    "platform": "douyin",
                    "url": f"https://www.douyin.com/video/{registration_id}-{index}",
                    "title": f"#音乐领域{index % 2} 第{index}条内容",
                    "author": registration_id,
                    "published_at": 1788000000 - (8 * 86400) - index * 3600,
                    "duration_seconds": 30,
                    "metrics": {
                        "like_count": 10,
                        "comment_count": 2,
                        "share_count": 0,
                        "collect_count": 0,
                    },
                }
                for index in range(20)
            ]
            return (
                build_historical_collection_artifact(
                    platform="douyin",
                    account_source_ref=str(registration["external_account_ref"]),
                    items=items,
                    raw_archive_ref=f"isolated://{registration_id}/history",
                    command_hash="test-history-command",
                    output_hash="test-history-output",
                    evaluated_at=1788000000,
                ),
            )
        if step_name == "high_signal_identification":
            history = next(
                item["artifact_refs"][0]
                for item in completed_artifacts
                if item["step_name"] == "historical_material"
            )
            return (build_high_signal_artifact(history["items"], evaluated_at=history["evaluated_at"]),)
        if step_name == "transcripts_and_comments":
            high_signal = next(
                item["artifact_refs"][0]
                for item in completed_artifacts
                if item["step_name"] == "high_signal_identification"
            )
            selected = [
                item for item in high_signal.get("selected_items", [])
                if isinstance(item, dict) and str(item.get("source_id") or "").strip()
            ]
            for item in selected:
                source_id = str(item["source_id"])
                transcript_path = self.runtime_root / "cold-start" / registration_id / f"{source_id}.txt"
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                transcript_path.write_text("测试保留的口播材料", encoding="utf-8")
                self.core.record_competitor_registration_item(
                    registration_id=registration_id,
                    step_name="transcripts_and_comments",
                    item_ref=source_id,
                    status="completed",
                    artifact={
                        "artifact_kind": "transcript_and_comments",
                        "source_id": source_id,
                        "transcript_ref": str(transcript_path),
                        "transcript": "测试保留的口播材料",
                        "metrics": dict(item.get("metrics") or {}),
                        "comments": [],
                    },
                    error=None,
                )
            return ({"artifact_kind": "isolated_prepared_materials", "selected_count": len(selected)},)
        if step_name == "breakdown":
            materials = [
                item for item in self.core.list_competitor_registration_items(
                    registration_id=registration_id,
                    step_name="transcripts_and_comments",
                )
                if item["status"] == "completed" and isinstance(item.get("artifact"), dict)
            ]
            for item in materials:
                self.breakdown_executor.process_prepared_breakdown(
                    registration=registration,
                    material=dict(item["artifact"]),
                )
            return ({"artifact_kind": "isolated_breakdown", "selected_count": len(materials)},)
        raise AssertionError(f"unexpected cold-start step: {step_name}")


def _cold_start_payload() -> dict[str, Any]:
    return {
        "domain_mode": "reuse",
        "existing_domain_label": DOMAIN,
        "domain_name": "音乐内容测试领域",
        "platform": "douyin",
        "owned_account": {
            "display_name": "TEST自营账号",
            "external_account_ref": "douyin:test-owned-account",
        },
        "competitor_accounts": [
            {
                "display_name": f"TEST对标账号{index}",
                "external_account_ref": f"douyin:test-competitor-{index}",
            }
            for index in range(20)
        ],
    }


def _review_cold_start_results(
    *,
    core: Stage0ContentProductionCore,
    config_dir: Path,
    cold_start_id: str,
) -> None:
    core.propose_human_decision_carrier(
        carrier_binding_id="test-cold-start-carrier",
        carrier_kind="isolated_test",
        entry_ref="full-standard-e2e",
        context_strategy="same_test_session",
        actor=ACTOR,
    )
    core.validate_human_decision_carrier(
        carrier_binding_id="test-cold-start-carrier",
        validation_evidence={
            "inbound_round_trip": True,
            "outbound_round_trip": True,
            "same_context_verified": True,
            "decision_identity_verified": True,
            "evidence_ref": "full-standard-e2e-carrier",
        },
        actor=ACTOR,
        actor_kind="user",
    )
    adapter = ColdStartHumanDecisionAdapter(core=core, config_dir=config_dir)
    tags = adapter.get_tag_library_for_review(cold_start_id=cold_start_id)
    assert isinstance(tags, dict)
    tag_decisions = [
        {
            "tag_id": str(item["tag_id"]),
            "decision": "accepted",
            "edited_tag": str(item.get("tag") or item.get("name") or "测试标签"),
        }
        for item in tags.get("candidates", [])
        if isinstance(item, dict) and item.get("tag_id")
    ]
    adapter.review_tag_library(
        command=FormalHumanDecisionCommand(
            command_id="test-cold-start-review-tags",
            carrier_binding_id="test-cold-start-carrier",
            session_ref="test-cold-start-session",
            action="review_competitor_tag_library",
            target_ref=f"cold_start:{cold_start_id}",
            payload={
                "cold_start_id": cold_start_id,
                "decisions": tag_decisions,
                "reason": "TEST确认当前运行生成的标签",
            },
            actor=ACTOR,
        )
    )

    content_types = adapter.get_content_type_candidates_for_review(cold_start_id=cold_start_id)
    assert isinstance(content_types, dict)
    content_decisions = [
        {
            "candidate_id": str(item["candidate_id"]),
            "decision": "accepted",
            "edited_name": str(item.get("name") or "测试内容类型"),
            "edited_definition": str(item.get("definition") or "基于本次材料确认的测试内容类型"),
        }
        for item in content_types.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id")
    ]
    assert content_decisions
    adapter.review_content_types(
        command=FormalHumanDecisionCommand(
            command_id="test-cold-start-review-content-types",
            carrier_binding_id="test-cold-start-carrier",
            session_ref="test-cold-start-session",
            action="review_cold_start_content_types",
            target_ref=f"cold_start:{cold_start_id}",
            payload={
                "cold_start_id": cold_start_id,
                "decisions": content_decisions,
                "reason": "TEST确认当前运行生成的内容类型",
            },
            actor=ACTOR,
        )
    )

    boundary = adapter.get_domain_boundary_candidates_for_review(cold_start_id=cold_start_id)
    assert isinstance(boundary, dict)
    boundary_proposal = boundary.get("proposal") if isinstance(boundary.get("proposal"), dict) else boundary
    boundary_decisions = [
        {"boundary_id": str(item["boundary_id"]), "decision": "accepted"}
        for item in (
            list(boundary_proposal.get("in_boundary_principles", []))
            + list(boundary_proposal.get("out_boundary_principles", []))
        )
        if isinstance(item, dict) and item.get("boundary_id")
    ]
    adapter.review_domain_boundary(
        command=FormalHumanDecisionCommand(
            command_id="test-cold-start-review-boundary",
            carrier_binding_id="test-cold-start-carrier",
            session_ref="test-cold-start-session",
            action="review_cold_start_domain_boundary",
            target_ref=f"cold_start:{cold_start_id}",
            payload={
                "cold_start_id": cold_start_id,
                "decisions": boundary_decisions,
                "reason": "TEST确认当前运行生成的生产边界",
                "unknown_topic_rule": boundary_proposal.get("unknown_topic_rule") or {
                    "rule": "无法判断时交给用户确认",
                    "uncertain_action": "等待人工确认",
                },
            },
            actor=ACTOR,
        )
    )


def _source_to_topic_output(source_ref: str) -> dict[str, Any]:
    angle = {
        "found": True,
        "direction": "解释一个具体问题",
        "reason": "输入材料支持这个方向",
    }
    return {
        "topic_status": "generated",
        "candidate_topic": "一个可以继续核验的具体音乐问题",
        "topic_angle": "从具体机制解释现象",
        "core_question": "这个音乐现象为什么会发生",
        "audience_relation": "帮助目标观众理解一个具体问题",
        "content_increment": "补充机制和核验路径",
        "supporting_evidence": [source_ref],
        "source_constraints": ["来源只用于发现线索"],
        "no_result_reason": "none",
        "confidence": "medium",
        "angle_discovery": {
            "problem_angle": angle,
            "audience_relevance_angle": angle,
            "content_increment_angle": angle,
            "tension_angle": angle,
            "distinct_angle": angle,
            "producible_angle": angle,
            "durable_value_angle": angle,
        },
        "candidate_selection": {
            "selected_direction": "解释一个具体问题",
            "why_selected": "材料足以支持下一步核验",
            "rejected_directions": [],
        },
        "risks": [],
        "material_gaps": ["正式研究仍需独立核验"],
        "user_review_required": True,
        "user_review_reasons": ["候选仍需人工选择"],
        "execution_review": {
            "used_only_supplied_material": True,
            "did_not_search_by_itself": True,
            "did_not_invent_facts": True,
            "respected_domain_boundary": True,
            "respected_risk_boundary": True,
            "did_not_force_candidate": True,
            "no_score_rank_weight": True,
        },
        "experience_usage": {
            "used_experience_ids": [],
            "unused_experience_ids": [],
            "rationale": "没有适用的经验卡",
        },
        "topic_shape": {
            "core_subject": "一个具体音乐现象",
            "scope_boundary": "只讨论当前来源支持的范围",
            "one_piece_line": "解释这个现象的机制",
        },
        "delivery_contract": {"user_gets": "一份有核验边界的候选选题"},
        "schema_version": "source_to_topic.output.v2",
    }


def _daily_external_executor(received: list[dict[str, Any]]):
    counter = 0

    def execute(task: dict[str, Any]) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        received.append(dict(task))
        input_payload = task.get("input") if isinstance(task.get("input"), dict) else {}
        evidence_items = input_payload.get("source_evidence_refs")
        if not isinstance(evidence_items, list) or not evidence_items:
            raise AssertionError("daily external task did not provide source evidence items")
        source_ref = str(evidence_items[0]).strip()
        if not source_ref:
            raise AssertionError("daily external task provided an empty source evidence item")
        return {
            "execution_id": f"test-daily-execution-{counter}",
            "executor_id": "test-fake-executor",
            "model_ref": "test-fake-model",
            "submitted_at": "2026-08-31T00:00:00+08:00",
            "output": _source_to_topic_output(source_ref),
        }

    return execute


def _experience_candidate_output(task: Mapping[str, Any]) -> dict[str, Any]:
    input_payload = task.get("input") if isinstance(task.get("input"), Mapping) else {}
    sources = input_payload.get("frozen_breakdowns")
    if not isinstance(sources, list) or len(sources) < 3:
        raise AssertionError("experience candidate task did not provide three frozen breakdowns")
    source_ids = [
        str(item.get("source_id") or "").strip()
        for item in sources
        if isinstance(item, Mapping) and str(item.get("source_id") or "").strip()
    ]
    if len(source_ids) < 3:
        raise AssertionError("experience candidate task provided empty source identities")
    return {
        "decision": "proposal",
        "candidate": {
            "summary": "先找出音乐问题中的关键冲突，再用材料解释原因",
            "experience_layer": "section_method",
            "use_positions": ["body"],
            "trigger_signals": ["音乐问题需要解释原因"],
            "applicable_when": ["讨论具体音乐问题"],
            "method": ["先指出具体冲突", "再用材料解释原因"],
            "boundary": ["只适用于材料支持的具体问题"],
            "not_applicable_when": ["材料无法支持具体事实"],
        },
        "source_ids": source_ids,
    }


class _TestAudioExecutor:
    def synthesize(self, request: Any) -> VoxCPM2SynthesisResult:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = request.output_dir / "test-output.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 8000)
        metadata_path = request.output_dir / "test-output.json"
        metadata_path.write_text(
            json.dumps(
                {"segment_strategy": request.segment_strategy, "segment_count": 1, "silence_ms": request.silence_ms},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return VoxCPM2SynthesisResult(
            audio_path=audio_path,
            metadata_path=metadata_path,
            audio_sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            metadata={
                "segment_strategy": request.segment_strategy,
                "segment_count": 1,
                "silence_ms": request.silence_ms,
            },
        )


class _TestAudioTranscriber:
    def __init__(self, *, transcript: str) -> None:
        self.transcript = transcript

    def transcribe(self, *, media_ref: str, media_path: str, max_duration_seconds: int) -> Any:
        del media_ref, max_duration_seconds
        transcript_path = Path(media_path).with_suffix(".txt")
        transcript_path.write_text(self.transcript, encoding="utf-8")
        return type(
            "TestTranscription",
            (),
            {
                "payload": {
                    "transcript_ref": str(transcript_path),
                    "transcript_hash": hashlib.sha256(self.transcript.encode("utf-8")).hexdigest(),
                    "asr_model_ref": "test-asr-model",
                    "vad_model_ref": "test-vad-model",
                }
            },
        )()


class _TestStandardDailySourceAcquirer:
    """TEST source boundary for the standard daily source set.

    The standard daily rules still require the tag-search source path to be
    present.  This fixture supplies an empty successful TEST result, so the
    candidate is produced only from the saved direction recorded in this
    fresh TEST database.  It never calls an external service or creates a
    source fact outside the normal acquisition boundary.
    """

    def search_tags(
        self,
        *,
        discovery_run_id: str,
        domain: str,
        now: Any,
        deadline_monotonic: float | None = None,
        allowed_tag_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        del discovery_run_id, domain, now, deadline_monotonic, allowed_tag_ids
        return {"status": "completed", "failed": 0, "items": []}


def test_full_standard_e2e_starts_from_fresh_test_business_state() -> None:
    with FreshTestEnvironment() as environment:
        assert environment.core is not None
        core = environment.core
        environment.assert_empty_formal_registries()
        fixture = _ColdStartExternalFixture(core=core, runtime_root=environment.runtime_root)
        registration_service = CompetitorRegistrationService(core=core, executor=fixture)
        onboarding = ColdStartOnboardingService(
            core=core,
            config_dir=environment.config_dir,
        )
        preview = onboarding.preview(_cold_start_payload())
        assert preview["ready_to_confirm"] is True
        result = onboarding.confirm(_cold_start_payload(), transport_actor=ACTOR)
        cold_start_id = str(result["cold_start_id"])
        assert result["status"] == "running"

        execution = ColdStartExecutionOrchestrator(
            core=core,
            registration_service=registration_service,
        ).run(
            cold_start_id=cold_start_id,
            actor=ACTOR,
            idempotency_key="test-full-e2e-cold-start",
        )
        assert execution["status"] in {"tag_input_ready", "waiting_human", "completed"}
        assert len(core.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration WHERE cold_start_id=?",
            (cold_start_id,),
        ).fetchall()) == 20
        _review_cold_start_results(
            core=core,
            config_dir=environment.config_dir,
            cold_start_id=cold_start_id,
        )
        assert core.cold_start_content_types_are_frozen(cold_start_id=cold_start_id)
        assert get_content_type_registry(DOMAIN)["status"] == "FROZEN"

        experience_service = ExperienceCandidateProposalService(core=core)
        experience_run = core.start_pre_topic_experience_run(
            domain_label=DOMAIN,
            actor=ACTOR,
        )
        experience_preparation = experience_service.prepare_before_topic(
            domain_label=DOMAIN,
            actor=ACTOR,
            experience_candidate_run_id=str(experience_run["experience_candidate_run_id"]),
        )
        experience_candidate_id: str | None = None
        if experience_preparation is not None:
            assert experience_preparation["status"] == "requires_external_intelligence"
            experience_task = experience_preparation["task"]
            experience_result = experience_service.submit_experience_candidate_external_result(
                task=experience_task,
                execution_id="test-experience-candidate-execution",
                executor_id="test-fake-executor",
                model_ref="test-fake-model",
                submitted_at="2026-08-31T00:00:00+08:00",
                output=_experience_candidate_output(experience_task),
            )
            assert experience_result["status"] == "awaiting_human_decision"
            experience_candidate_id = str(experience_result["experience_candidate_id"])
            experience_decision = core.decide_experience_candidate(
                experience_candidate_id=experience_candidate_id,
                decision="accepted",
                actor=ACTOR,
                actor_kind="user",
                reason="TEST确认这条可供后续生产验证的经验候选",
            )
            assert experience_decision["status"] == "validation_ready"
        else:
            experience_completion = core.complete_pre_topic_experience_run(
                run_id=str(experience_run["experience_candidate_run_id"])
            )
            assert experience_completion["run_status"] in {
                "completed",
                "completed_with_gaps",
            }
            assert core.list_pre_topic_experience_candidates(
                domain_label=DOMAIN,
                run_id=str(experience_run["experience_candidate_run_id"]),
            ) == []

        business = CreationAssistantFormalBusinessCore(core=core)
        core.register_saved_user_direction_source(
            direction_id="test-daily-direction",
            domain_label=DOMAIN,
            core_question="这个音乐现象为什么会发生",
            submitted_by=ACTOR,
        )
        daily_request = business.request_daily(
            domain_label=DOMAIN,
            business_date="2026-08-31",
            actor=ACTOR,
        )
        daily_run_id = str(daily_request["daily_run"]["daily_run_id"])
        daily_tasks: list[dict[str, Any]] = []
        daily_result = business.execute_daily_discovery(
            discovery_date="2026-08-31",
            actor=ACTOR,
            idempotency_key="test-full-e2e-daily",
            execution_mode="production_daily",
            daily_run_id=daily_run_id,
            domains=(DOMAIN,),
            source_types=DAILY_REPORT_SOURCE_TYPES,
            source_acquirer=_TestStandardDailySourceAcquirer(),
            external_executor=_daily_external_executor(daily_tasks),
        )
        assert daily_result["status"] == "completed", daily_result
        assert daily_result["source_types"] == list(DAILY_REPORT_SOURCE_TYPES)
        daily_snapshot = business.view_daily_discovery_snapshot(
            run_id=str(daily_result["run_id"]),
            domains=(DOMAIN,),
        )
        candidates = daily_snapshot[DOMAIN]
        assert candidates
        daily_source_identities = {
            (
                str(task["source_identity"]["source_version_id"]),
                str(task["source_identity"]["source_type"]),
                str(task["source_identity"]["source_object_id"]),
                str(task["source_identity"]["source_object_version"]),
            )
            for task in daily_tasks
        }
        assert daily_source_identities
        for item in candidates:
            source_reference = item["candidate"]["source_reference"]
            assert item["execution_mode"] == "production_daily"
            assert item["lifecycle_status"] == "completed"
            assert item["formal_candidate_pool"] is True
            assert item["user_decision"] is None
            assert item["candidate"]["domain"] == DOMAIN
            assert source_reference["source_type"] in DAILY_REPORT_SOURCE_TYPES
            assert (
                str(source_reference["source_version_id"]),
                str(source_reference["source_type"]),
                str(source_reference["source_object_id"]),
                str(source_reference["source_object_version"]),
            ) in daily_source_identities
        candidate = next(
            item
            for item in candidates
            if item["status"] == "awaiting_user_decision"
            and not item["user_decision"]
        )

        web_server = create_action_server(
            host="127.0.0.1",
            port=0,
            data_identity="test",
            database_path=environment.database,
            actor=ACTOR,
            carrier_binding_id="test-web-carrier",
            config_dir=environment.config_dir,
        )
        web_thread = threading.Thread(target=web_server.serve_forever, daemon=True)
        web_thread.start()
        try:
            web_candidates = _get_json(
                f"http://127.0.0.1:{web_server.server_address[1]}/api/daily-candidates"
            )
            assert web_candidates["ok"] is True, web_candidates
            web_group = next(
                group
                for group in web_candidates["domains"]
                if group["domain_label"] == DOMAIN
            )
            assert [
                item["candidate_version_id"] for item in web_group["candidates"]
            ] == [item["candidate_version_id"] for item in candidates]
            web_candidate = next(
                item
                for item in web_group["candidates"]
                if item["status"] == "awaiting_user_decision"
                and not item["user_decision"]
            )
            assert web_candidate["candidate_version_id"] == candidate["candidate_version_id"]
            candidate_version_id = str(web_candidate["candidate_version_id"])
            selected = _post_json(
                f"http://127.0.0.1:{web_server.server_address[1]}/api/action",
                {
                    "action": "select_daily_candidate",
                    "domain_label": DOMAIN,
                    "candidate_version_id": candidate_version_id,
                    "reason": "TEST通过Web选择当前候选",
                },
            )
        finally:
            web_server.shutdown()
            web_server.server_close()
            web_thread.join()
        assert selected["ok"] is True, selected
        assert selected["core_result"]["research_plan_status"] == "requires_external_intelligence"

        task_id = str(selected["core_result"]["task_id"])
        assert core.get_task(task_id)["current_node"] == "research_plan"
        initial_external_task = selected["core_result"].get("external_task")
        assert isinstance(initial_external_task, dict), selected
        with patch(
            "scripts.core.production.stage1c_content_pipeline.AnySearchExecutor",
            _TestAnySearchExecutor,
        ):
            final_task = _run_formal_external_task_loop(
                business=business,
                core=core,
                task_id=task_id,
                initial_external_task=initial_external_task,
                actor=ACTOR,
                experience_candidate_id=experience_candidate_id,
            )
        validation_usage_records = core.list_content_validation_usage(task_id=task_id)
        if experience_candidate_id is not None:
            assert any(
                item["experience_candidate_id"] == experience_candidate_id
                for item in validation_usage_records
            )
        else:
            assert validation_usage_records == []
        assert final_task["current_status"] == "approved"
        final_confirmation = business.approve_final_content(
            task_id=task_id,
            version_id=str(final_task["current_version_id"]),
            actor=ACTOR,
            reason="TEST确认最终稿",
            idempotency_key="test-full-e2e:confirm-final-content",
        )
        assert final_confirmation["status"] == "confirmed"

        settings = {
            "style_prompt": "",
            "emotion_preset": "neutral",
            "emotion_strength": 0.0,
            "segment_strategy": "auto",
            "max_chars": 120,
            "silence_ms": 260,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        }
        voice_profile = core.submit_voice_profile(
            voice_profile_id="test-full-e2e-voice",
            profile_label="TEST声音",
            reference_audio_ref=str(environment.runtime_root / "voice-reference.wav"),
            emotion_reference_audio_ref=None,
            mode="basic",
            prompt_text="",
            emotion_prompt_text="",
            settings=settings,
            actor=ACTOR,
            actor_kind="user",
            idempotency_key="test-full-e2e:submit-voice-profile",
        )
        assert voice_profile["status"] == "awaiting_human_confirmation"
        assert core.confirm_voice_profile(
            voice_profile_id="test-full-e2e-voice",
            actor=ACTOR,
            actor_kind="user",
            idempotency_key="test-full-e2e:confirm-voice-profile",
        )["status"] == "active"
        review_payload = core.get_artifact_payload(str(final_task["current_version_id"]))["payload"]
        script_text = str(review_payload["document"]["script_text"])
        audio_service = AudioProductionService(
            core=core,
            executor=_TestAudioExecutor(),
            transcriber=_TestAudioTranscriber(transcript=script_text),
            output_root=environment.runtime_root / "audio",
        )
        audio_result = audio_service.produce(
            task_id=task_id,
            approved_content_version_id=str(final_task["current_version_id"]),
            voice_profile_id="test-full-e2e-voice",
            actor=ACTOR,
            idempotency_key="test-full-e2e:produce-audio",
        )
        assert audio_result["status"] == "awaiting_human_review"
        audio_review = audio_service.review(
            audio_production_id=str(audio_result["audio_production_id"]),
            decision="approved",
            issue_scope="none",
            reason="TEST确认音频",
            actor=ACTOR,
        )
        assert audio_review["status"] == "approved"

        publication = business.register_publication_for_task(
            task_id=task_id,
            platform="test-platform",
            external_video_url="https://example.test/full-standard-e2e",
            published_at="2026-08-31T10:00:00+08:00",
            actual_content_status="same_as_approved",
            actual_content_note="",
            actor=ACTOR,
            idempotency_key="test-full-e2e:register-publication",
        )
        publication_id = str(publication["publication_id"])
        assert publication["status"] == "registered"
        observation_ids: dict[str, str] = {}
        for index in range(8):
            point_code = f"P{index}"
            source_ref = f"test://full-standard-e2e/{point_code}"
            observed_at = f"2026-08-31T10:00:0{index}+08:00"
            observation = business.record_publication_observation(
                publication_id=publication_id,
                point_code=point_code,
                observation_status="recorded",
                metrics={"observed_value": index + 1},
                missing_reason="",
                source_ref=source_ref,
                observed_at=observed_at,
                actor=ACTOR,
                idempotency_key=f"test-full-e2e:observe-{point_code}",
            )
            assert set(observation) == {"observation_id", "idempotent"}
            assert str(observation["observation_id"]).strip()
            assert observation["idempotent"] is False
            observation_ids[point_code] = str(observation["observation_id"])

            repeated = business.record_publication_observation(
                publication_id=publication_id,
                point_code=point_code,
                observation_status="recorded",
                metrics={"observed_value": index + 1},
                missing_reason="",
                source_ref=source_ref,
                observed_at=observed_at,
                actor=ACTOR,
                idempotency_key=f"test-full-e2e:observe-{point_code}:same-facts",
            )
            assert repeated == {
                "observation_id": observation_ids[point_code],
                "idempotent": True,
            }

        publication_views = business.list_publications()
        assert len(publication_views) == 1
        publication_view = publication_views[0]
        publication_record = publication_view["publication"]
        assert publication_record["publication_id"] == publication_id
        assert publication_record["task_id"] == task_id
        assert publication_record["data_identity"] == core.data_identity
        assert publication_record["status"] == "registered"
        observations = publication_view["observations"]
        assert len(observations) == 8
        observations_by_point = {item["point_code"]: item for item in observations}
        assert set(observations_by_point) == {f"P{index}" for index in range(8)}
        for index in range(8):
            point_code = f"P{index}"
            observation = observations_by_point[point_code]
            assert observation["observation_id"] == observation_ids[point_code]
            assert observation["publication_id"] == publication_id
            assert observation["point_code"] == point_code
            assert observation["observation_status"] == "recorded"
            assert observation["metrics_json"] == {"observed_value": index + 1}
            assert observation["source_ref"] == f"test://full-standard-e2e/{point_code}"
            assert observation["observed_at"] == f"2026-08-31T10:00:0{index}+08:00"
            assert observation["data_identity"] == core.data_identity
        feedback_candidate = None
        if experience_candidate_id is not None:
            feedback_candidate = {
                "validation_results": [
                    {
                        "experience_candidate_id": experience_candidate_id,
                        "result": "supports",
                        "performance_summary": "TEST观察结果支持这条经验候选",
                        "review_basis": "完整P0-P7观察和当前正式任务使用记录",
                    }
                ]
            }
        p7 = business.prepare_p7_review(
            publication_id=publication_id,
            selection_assessment="TEST记录候选选择结果",
            narrative_assessment="TEST记录叙事结果",
            material_assessment="TEST记录材料结果",
            external_conditions_assessment="TEST记录外部条件",
            feedback_candidate=feedback_candidate,
            actor=ACTOR,
            idempotency_key="test-full-e2e:prepare-p7",
        )
        assert p7["status"] == "awaiting_user_confirmation"
        confirmed_p7 = business.decide_p7_review(
            review_id=str(p7["review_id"]),
            decision="confirmed",
            actor=ACTOR,
            reason="TEST确认P7复盘",
            idempotency_key="test-full-e2e:confirm-p7",
        )
        assert confirmed_p7["status"] == "confirmed"
        confirmed_publication_views = business.list_publications()
        assert len(confirmed_publication_views) == 1
        confirmed_publication_view = confirmed_publication_views[0]
        assert confirmed_publication_view["publication"]["publication_id"] == publication_id
        assert confirmed_publication_view["publication"]["task_id"] == task_id
        assert len(confirmed_publication_view["observations"]) == 8
        assert len(confirmed_publication_view["reviews"]) == 1
        p7_record = confirmed_publication_view["reviews"][0]
        assert p7_record["publication_id"] == publication_id
        assert p7_record["status"] == "confirmed"
        assert p7_record["data_identity"] == core.data_identity
        if experience_candidate_id is not None:
            promoted_experience = core.promote_experience_candidate(
                experience_candidate_id=experience_candidate_id,
                actor=ACTOR,
                actor_kind="user",
                reason="TEST根据已确认P7支持证据正式纳入经验",
            )
            assert promoted_experience["status"] == "promoted"
