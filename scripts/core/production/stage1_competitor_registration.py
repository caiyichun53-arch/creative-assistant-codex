"""Controlled automatic execution for a confirmed competitor-account registration.

The worker may call only an injected, configured collection or analysis executor.
It records every completed step through Core, automatically activates the account
after all required artifacts exist, and resumes only from the next unfinished step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.core.external_adapters import (
    AsrAdapter,
    LocalCompetitorMediaMaterializer,
    LocalMediaCrawlerExecutor,
    LocalSenseVoiceExecutor,
    MediaCrawlerCollectorAdapter,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRunEnvelope
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalBusinessSkillAdapter,
    FormalSkillContract,
    FormalSkillValidationError,
    validate_competitor_breakdown_structural_output_semantics,
)
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.runtime.liveness import budget_for
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import (
    COMPETITOR_REGISTRATION_STEPS,
    CoreCompetitorRegistrationModelRunMaterializer,
    CoreDailyHitModelRunMaterializer,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.runtime.runtime_storage import require_runtime_path, runtime_path
from scripts.core.production.high_signal_policy import (
    FIRST_REGISTRATION_MAX_ITEMS,
    HISTORICAL_METRICS,
    MIN_RELIABLE_HISTORY_ITEMS,
    build_historical_collection_artifact,
    build_high_signal_artifact,
)
from scripts.core.production.stage1a_research_plan import _configured_environment_value
from scripts.core.external_adapters.runtime_config import external_runtime_value


COMPETITOR_ANALYSIS_ROUTE = "stage0.competitor_registration_analysis"
CONTENT_SUBJECT_TYPES = frozenset({
    "person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear",
})
EXPRESSION_FORMS = frozenset({
    "story", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear",
})
METRIC_NAMES = HISTORICAL_METRICS
_SOURCE_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")


class CompetitorBreakdownFailed(StateTransitionError):
    """One atomic breakdown failed after its complete failure record was saved."""

    def __init__(self, message: str, *, failure_record: dict[str, Any]) -> None:
        super().__init__(message)
        self.failure_record = dict(failure_record)


class TestOnlyCompetitorBreakdownMaterializer:
    """Keep test envelopes in the returned receipt, never in formal business storage."""

    def __init__(self) -> None:
        self.envelopes: list[ModelRunEnvelope] = []

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        self.envelopes.append(envelope)
        return f"test_envelope_{len(self.envelopes):02d}"


def _test_correction_eligible(exc: Exception) -> bool:
    """Only a complete, rejected test answer may receive one correction request."""
    if not isinstance(exc, FormalSkillValidationError):
        return False
    if not isinstance(exc.raw_model_output, str) or not exc.raw_model_output.strip():
        return False
    receipt = exc.model_completion_receipt
    return isinstance(receipt, dict) and str(receipt.get("finish_reason") or "") == "stop"
_TAG_EDGE_PUNCTUATION = "，,。.！!?？:：;；、|/\\()（）[]【】<>《》“”'\"`~·…"
_COMMON_COLLECTOR_POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "business_guardrails" / "competitor_registration.json"


def _configured_comment_limit() -> int:
    try:
        payload = json.loads(_COMMON_COLLECTOR_POLICY_PATH.read_text(encoding="utf-8"))
        value = int((payload.get("common_collector_policy") or {}).get("comments_top_n"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StateTransitionError("公共采集规则缺少有效的评论采集数量") from exc
    if not 1 <= value <= 500:
        raise StateTransitionError("公共采集规则中的评论采集数量必须在 1 到 500 之间")
    return value


COMMENT_TOP_N = _configured_comment_limit()


class CompetitorRegistrationStepExecutor(Protocol):
    def execute(
        self,
        *,
        step_name: str,
        registration: dict[str, Any],
        completed_artifacts: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], ...]:
        ...


def _step_artifacts(completed: tuple[dict[str, Any], ...], step_name: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for record in completed:
        if record.get("step_name") == step_name:
            values = record.get("artifact_refs")
            if isinstance(values, (list, tuple)):
                artifacts.extend(item for item in values if isinstance(item, dict))
    return artifacts


def _parse_account_source(value: str) -> tuple[str, str]:
    text = value.strip()
    for platform in ("douyin", "xiaohongshu", "bilibili", "kuaishou", "weibo", "tieba", "zhihu"):
        prefix = f"{platform}:"
        if text.startswith(prefix):
            return platform, text[len(prefix):]
    host_platforms = {
        "douyin.com": "douyin", "xiaohongshu.com": "xiaohongshu", "bilibili.com": "bilibili",
        "kuaishou.com": "kuaishou", "weibo.com": "weibo", "tieba.baidu.com": "tieba", "zhihu.com": "zhihu",
    }
    for host, platform in host_platforms.items():
        if host in text:
            return platform, text
    raise StateTransitionError("competitor account source must include a supported platform identity")


def _sanitize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": str(item.get("source_id") or ""),
        "platform": str(item.get("platform") or ""),
        "url": str(item.get("url") or ""),
        "title": str(item.get("title") or ""),
        "author": str(item.get("author") or ""),
        "published_at": item.get("published_at"),
        "duration_seconds": int(item.get("duration_seconds") or 0),
        "metrics": {name: int((item.get("metrics") or {}).get(name) or 0) for name in METRIC_NAMES},
    }


def _filter_comments(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    retained: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        text = str(value.get("text") or value.get("content") or "").strip()
        likes = int(value.get("like_count") or 0)
        parent = value.get("parent_comment_id") or value.get("reply_to_reply_id")
        if likes < 1 or len(text) < 5 or parent:
            continue
        comment_id = str(value.get("comment_id") or value.get("id") or "").strip() or hashlib.sha256(text.encode("utf-8")).hexdigest()
        if comment_id in seen:
            continue
        seen.add(comment_id)
        retained.append({"comment_id": comment_id, "text": text, "like_count": likes, "sample_rank": rank})
        if len(retained) >= COMMENT_TOP_N:
            break
    return retained


def _comments_for_source(values: Any, source_id: str) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    retained: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        parent_id = str(
            value.get("aweme_id")
            or value.get("note_id")
            or value.get("video_id")
            or value.get("source_id")
            or ""
        ).strip()
        if parent_id == source_id:
            retained.append(value)
    return retained


def _decode_model_json_object(output_text: str) -> dict[str, Any]:
    text = str(output_text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(value, dict):
            return value
        last_error = StateTransitionError("competitor registration model output must be one JSON object")
    raise StateTransitionError(f"model returned no usable JSON object: {last_error or 'empty output'}")


def build_production_competitor_registration_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    if core.data_identity != "production":
        raise StateTransitionError("production competitor registration requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("competitor registration requires business_analysis with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None:
        raise ModelRouterError("competitor registration requires a configured model provider")
    limits = budget_for("model")
    route = router.resolve_bound_route(
        "business_analysis",
        route_name=COMPETITOR_ANALYSIS_ROUTE,
        parameters={"stream": False},
    )
    adapter = build_configured_model_provider(provider, route, model_limits=limits)
    return ModelGateway(
        routes={route.route_name: route}, providers={adapter.provider_name: adapter},
        materializer=CoreCompetitorRegistrationModelRunMaterializer(core),
    )


def build_production_daily_hit_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    if core.data_identity != "production":
        raise StateTransitionError("production daily-hit breakdown requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("daily-hit breakdown requires business_analysis with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None:
        raise ModelRouterError("daily-hit breakdown requires a configured model provider")
    limits = budget_for("model")
    route = router.resolve_bound_route("business_analysis", route_name=COMPETITOR_ANALYSIS_ROUTE, parameters={"stream": False})
    adapter = build_configured_model_provider(provider, route, model_limits=limits)
    return ModelGateway(
        routes={route.route_name: route},
        providers={adapter.provider_name: adapter},
        materializer=CoreDailyHitModelRunMaterializer(core),
    )


def run_test_only_competitor_breakdown_batch(
    *, test_id: str, materials: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run a fixed, non-writing comparison batch through the formal Skill.

    This is deliberately separate from formal registration: it uses the same
    route, Skill, validation, and one-call boundary, but keeps every envelope
    and raw answer only in the returned test receipt.  It never receives Core
    and therefore cannot write formal business records.
    """
    if not isinstance(test_id, str) or not test_id.strip():
        raise StateTransitionError("test-only competitor breakdown needs a test identifier")
    if not isinstance(materials, list) or not materials:
        raise StateTransitionError("test-only competitor breakdown needs selected materials")

    normalized_materials: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for position, material in enumerate(materials, start=1):
        if not isinstance(material, dict):
            raise StateTransitionError("test-only competitor breakdown material is invalid")
        source_id = str(material.get("source_id") or "").strip()
        transcript = str(material.get("transcript") or "").strip()
        metrics = material.get("metrics")
        comments = material.get("comments")
        if not source_id or not transcript or not isinstance(metrics, dict) or not isinstance(comments, list):
            raise StateTransitionError("test-only competitor breakdown material is incomplete")
        if source_id in source_ids:
            raise StateTransitionError("test-only competitor breakdown sources must be unique")
        source_ids.add(source_id)
        normalized_materials.append({
            "position": position,
            "source_id": source_id,
            "title": str(material.get("title") or ""),
            "hit_id": str(material.get("hit_id") or ""),
            "transcript": transcript,
            "metrics": dict(metrics),
            "comments": list(comments),
        })

    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none":
        raise ModelRouterError("test-only competitor breakdown requires business_analysis with fallback none")
    provider_definition = router.providers.get(definition.provider_ref)
    if provider_definition is None:
        raise ModelRouterError("test-only competitor breakdown requires a configured model provider")
    limits = budget_for("model")
    route = router.resolve_bound_route(
        "business_analysis", route_name=COMPETITOR_ANALYSIS_ROUTE, parameters={"stream": False},
    )
    adapter = build_configured_model_provider(provider_definition, route, model_limits=limits)
    test_materializer = TestOnlyCompetitorBreakdownMaterializer()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={adapter.provider_name: adapter},
        materializer=test_materializer,
    )
    skill = FormalBusinessSkillAdapter(
        contract=FormalSkillContract.from_runtime_skill("competitor_breakdown_structural_v13"), gateway=gateway,
    )

    outcomes: list[dict[str, Any]] = []
    for material in normalized_materials:
        payload = {
            "correlation_id": f"{test_id}:{material['position']}:{material['source_id']}",
            "source_id": material["source_id"],
            "transcript": material["transcript"],
            "metrics": material["metrics"],
            "comments": material["comments"],
            "schema_version": "competitor_breakdown.input.v7",
        }
        outcome = {
            "position": material["position"],
            "hit_id": material["hit_id"],
            "source_id": material["source_id"],
            "title": material["title"],
        }
        try:
            result = skill.run(
                payload,
                quality_comparison=True,
                request_metadata={
                    "test_only": True,
                    "test_name": test_id,
                    "formal_business_data_written": False,
                    "automatic_retry": False,
                },
            )
            outcome.update({
                "status": "completed",
                "output": result.output_payload,
                "raw_model_output": result.raw_model_output,
                "model_run_envelope_version_id": result.model_run_envelope_version_id,
                "test_correction": {"attempted": False},
            })
        except Exception as exc:  # Test batches retain each failure and continue to the next material.
            if _test_correction_eligible(exc):
                initial_failure = {
                    "reason": str(exc),
                    "raw_model_output": exc.raw_model_output,
                    "model_run_envelope_version_id": exc.model_run_envelope_version_id,
                    "model_completion_receipt": exc.model_completion_receipt,
                }
                try:
                    correction = skill.run_test_correction(
                        payload,
                        rejected_model_output=exc.raw_model_output,
                        validation_errors=[str(exc)],
                        request_metadata={
                            "test_only": True,
                            "test_name": test_id,
                            "formal_business_data_written": False,
                            "test_single_correction": True,
                        },
                    )
                except Exception as correction_exc:
                    outcome.update({
                        "status": "failed",
                        "reason": str(correction_exc),
                        "failure_type": type(correction_exc).__name__,
                        "raw_model_output": getattr(correction_exc, "raw_model_output", None),
                        "model_run_envelope_version_id": getattr(correction_exc, "model_run_envelope_version_id", None),
                        "model_completion_receipt": getattr(correction_exc, "model_completion_receipt", None),
                        "test_correction": {
                            "attempted": True,
                            "status": "failed",
                            "initial_failure": initial_failure,
                        },
                    })
                else:
                    outcome.update({
                        "status": "completed",
                        "output": correction.output_payload,
                        "raw_model_output": correction.raw_model_output,
                        "model_run_envelope_version_id": correction.model_run_envelope_version_id,
                        "test_correction": {
                            "attempted": True,
                            "status": "accepted",
                            "initial_failure": initial_failure,
                        },
                    })
            else:
                outcome.update({
                    "status": "failed",
                    "reason": str(exc),
                    "failure_type": type(exc).__name__,
                    "raw_model_output": getattr(exc, "raw_model_output", None),
                    "model_run_envelope_version_id": getattr(exc, "model_run_envelope_version_id", None),
                    "model_completion_receipt": getattr(exc, "model_completion_receipt", None),
                    "test_correction": {"attempted": False},
                })
        outcomes.append(outcome)

    return {
        "kind": "competitor_breakdown_structural_v13_quality_comparison",
        "test_id": test_id,
        "formal_business_data_written": False,
        "automatic_retry": False,
        "test_single_correction_enabled": True,
        "model_delivery": "non_streaming_json_object",
        "route_timeout_ms": route.timeout_ms,
        "outcomes": outcomes,
        "model_envelopes": [envelope.as_payload() for envelope in test_materializer.envelopes],
    }


class ConfiguredCompetitorRegistrationExecutor:
    """Real configured collector -> ASR/comments -> breakdown -> tag-candidate connector."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        collector: MediaCrawlerCollectorAdapter,
        transcriber: AsrAdapter,
        media_materializer: LocalCompetitorMediaMaterializer,
        gateway: ModelGateway,
        max_historical_items: int = FIRST_REGISTRATION_MAX_ITEMS,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        if max_historical_items != FIRST_REGISTRATION_MAX_ITEMS:
            raise StateTransitionError(
                "first competitor registration must collect exactly 50 historical items"
            )
        self.core = core
        self.collector = collector
        self.transcriber = transcriber
        self.media_materializer = media_materializer
        self.gateway = gateway
        self.max_historical_items = max_historical_items
        self.progress_callback = progress_callback

    def _report_progress(self, phase: str, detail: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(phase, detail)

    def execute(
        self, *, step_name: str, registration: dict[str, Any], completed_artifacts: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        handlers = {
            "historical_material": self._historical_material,
            "high_signal_identification": self._high_signal_identification,
            "transcripts_and_comments": self._transcripts_and_comments,
            "breakdown": self._breakdown,
            "tag_candidates": self._tag_candidates,
        }
        handler = handlers.get(step_name)
        if handler is None:
            raise StateTransitionError("competitor registration requested an unsupported step")
        return handler(registration, completed_artifacts)

    def _historical_material(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        del completed
        self._report_progress("historical_collection", "正在采集对标账号历史内容")
        platform, source_ref = _parse_account_source(str(registration["external_account_ref"]))
        evaluated_at = int(time.time())
        result = self.collector.collect_video_snapshot(
            platform=platform, source_url=source_ref, max_items=self.max_historical_items, with_comments=False
        )
        items = [_sanitize_item(item) for item in result.payload["items"]]
        if not items:
            raise StateTransitionError(
                "competitor historical collection returned no retained items; registration cannot advance"
            )
        initial = build_historical_collection_artifact(
            platform=platform,
            account_source_ref=str(registration["external_account_ref"]),
            items=items,
            raw_archive_ref=result.raw_archive_ref,
            command_hash=result.command_hash,
            output_hash=result.output_hash,
            evaluated_at=evaluated_at,
        )
        if int(initial["recent_mature_item_count"]) < MIN_RELIABLE_HISTORY_ITEMS:
            self._report_progress(
                "historical_collection",
                "最近90天成熟样本不足20条，正在继续读取更早历史并只补足成熟样本",
            )
            expanded = self.collector.collect_video_snapshot(
                platform=platform,
                source_url=source_ref,
                max_items=500,
                with_comments=False,
            )
            merged = {str(item["source_id"]): item for item in items}
            for raw_item in expanded.payload["items"]:
                sanitized = _sanitize_item(raw_item)
                merged[str(sanitized["source_id"])] = sanitized
            items = list(merged.values())
            result = expanded
        return (build_historical_collection_artifact(
            platform=platform,
            account_source_ref=str(registration["external_account_ref"]),
            items=items,
            raw_archive_ref=result.raw_archive_ref,
            command_hash=result.command_hash,
            output_hash=result.output_hash,
            evaluated_at=evaluated_at,
        ),)

    def _high_signal_identification(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        del registration
        history = _step_artifacts(completed, "historical_material")
        if len(history) != 1 or not isinstance(history[0].get("items"), list):
            raise StateTransitionError("high-signal identification requires one complete historical collection")
        items = history[0]["items"]
        self._report_progress("baseline_calculation", f"正在用 {len(items)} 条历史内容计算账号基线")
        self._report_progress("hit_filtering", "正在按账号基线筛选历史爆款")
        return (build_high_signal_artifact(items),)

    def _transcripts_and_comments(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        high_signal = _step_artifacts(completed, "high_signal_identification")
        if len(high_signal) != 1:
            raise StateTransitionError("transcription requires completed high-signal identification")
        selected = high_signal[0].get("selected_items")
        if not isinstance(selected, list):
            raise StateTransitionError("high-signal selection artifact is invalid")
        return self.prepare_selected_materials(
            registration=registration,
            selected_items=selected,
        )

    def prepare_selected_materials(
        self,
        *,
        registration: dict[str, Any],
        selected_items: list[dict[str, Any]],
        material_replenishment: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        """Prepare only the explicitly supplied high-signal sources.

        This is intentionally separate from registration progression so an
        authorized material-repair batch cannot accidentally enter model
        breakdown work.
        """
        selected = list(selected_items)
        self._report_progress("hit_material_preparation", f"正在为 {len(selected)} 条爆款准备详情、评论、音频和转写")
        registration_id = str(registration["registration_id"])
        existing = {
            item["item_ref"]: item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        }

        checkpoints = {
            value["item_ref"]: value
            for value in self.core.list_replenished_competitor_material_collection_checkpoints(
                registration_id=registration_id
            )
        } if material_replenishment else {}
        failures: list[str] = []
        collection_problems: list[str] = []
        delivery_retry_queue: list[dict[str, Any]] = []
        completed_count = len(existing)
        total_count = len(selected)
        for position, item in enumerate(selected, start=1):
            source_id = str(item.get("source_id") or "")
            if source_id in existing:
                continue
            self._report_progress(
                "hit_material_preparation",
                f"爆款备料已完成 {completed_count}/{total_count}，正在处理第 {position} 条",
            )
            checkpoint = checkpoints.get(source_id)
            if checkpoint is None:
                try:
                    detail = self.collector.collect_video_snapshots(
                        platform=str(item["platform"]), source_urls=(str(item["url"]),),
                        with_comments=True, max_comments_per_item=60,
                    )
                    returned = {
                        str(value.get("source_id") or ""): value
                        for value in detail.payload["items"] if isinstance(value, dict)
                    }
                    detail_item = returned.get(source_id)
                    if detail_item is None:
                        raise StateTransitionError("detail collection returned no video for this source")
                    checkpoint = {
                        "item_ref": source_id,
                        "detail": detail_item,
                        "comments": _comments_for_source(detail.payload.get("comments"), source_id),
                        "collection_ref": str(detail.raw_archive_ref or "").strip(),
                    }
                    if material_replenishment:
                        self.core.record_replenished_competitor_material_collection_checkpoint(
                            registration_id=registration_id, item_ref=source_id,
                            detail=checkpoint["detail"], comments=checkpoint["comments"],
                            collection_ref=checkpoint["collection_ref"],
                        )
                        checkpoints[source_id] = checkpoint
                except Exception as exc:
                    if material_replenishment:
                        collection_problems.append(source_id)
                        continue
                    failures.append(source_id)
                    self.core.record_competitor_registration_item(
                        registration_id=registration_id, step_name="transcripts_and_comments", item_ref=source_id,
                        status="failed", artifact=None, error={"reason": str(exc)},
                    )
                    if "account blocked" in str(exc).lower():
                        raise StateTransitionError("account blocked during competitor material preparation") from exc
                    continue
            try:
                artifact = self._materialize_collected_detail(
                    item=item, detail_item=checkpoint["detail"], comments=checkpoint["comments"],
                    collection_ref=checkpoint["collection_ref"],
                )
                if material_replenishment:
                    self.core.record_replenished_competitor_material_item(
                        registration_id=registration_id, item_ref=source_id,
                        status="completed", artifact=artifact, error=None,
                    )
                else:
                    self.core.record_competitor_registration_item(
                        registration_id=registration_id, step_name="transcripts_and_comments", item_ref=source_id,
                        status="completed", artifact=artifact, error=None,
                    )
                completed_count += 1
                try:
                    self.process_prepared_breakdown(registration=registration, material=artifact)
                except CompetitorBreakdownFailed as exc:
                    if self._is_delivery_interruption(exc.failure_record):
                        delivery_retry_queue.append(artifact)
                        self._report_progress(
                            "hit_breakdown",
                            "本条服务端中断，先继续准备本次任务的其他材料；结束后自动原样补跑一次",
                        )
                    else:
                        self._report_progress(
                            "hit_breakdown",
                            "本条拆解未通过核查，已留档；继续处理本次任务其余材料",
                        )
            except CompetitorBreakdownFailed:
                raise
            except Exception as exc:
                failures.append(source_id)
                if material_replenishment:
                    self.core.record_replenished_competitor_material_item(
                        registration_id=registration_id, item_ref=source_id,
                        status="failed", artifact=None, error={"reason": str(exc)},
                    )
                else:
                    self.core.record_competitor_registration_item(
                        registration_id=registration_id, step_name="transcripts_and_comments", item_ref=source_id,
                        status="failed", artifact=None, error={"reason": str(exc)},
                    )
                if "account blocked" in str(exc).lower():
                    raise StateTransitionError("account blocked during competitor material preparation") from exc
        if collection_problems:
            raise StateTransitionError(
                f"detail collection did not finish for {len(collection_problems)} recovery item(s); they remain excluded and were not retried automatically"
            )
        if failures:
            raise StateTransitionError(f"competitor material preparation failed for {len(failures)} item(s); failures remain recorded and are not automatically retried")
        self._retry_delivery_interruptions_after_task(
            registration=registration,
            materials=delivery_retry_queue,
        )
        artifacts = [
            item["artifact"] for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        ]
        return tuple(artifacts or [{"artifact_kind": "transcripts_and_comments", "selected_count": 0, "items": []}])

    def _materialize_collected_detail(
        self,
        *,
        item: dict[str, Any],
        detail_item: dict[str, Any],
        comments: list[dict[str, Any]],
        collection_ref: str,
    ) -> dict[str, Any]:
        source_id = str(item.get("source_id") or "")
        media_urls: list[str] = []
        for value in (detail_item.get("music_download_url"), detail_item.get("video_download_url")):
            media_url = str(value or "").strip()
            if media_url and media_url not in media_urls:
                media_urls.append(media_url)
        if not media_urls:
            raise StateTransitionError("collected detail has no downloadable audio or video")
        materialized = None
        material_errors: list[str] = []
        for media_url in media_urls:
            try:
                materialized = self.media_materializer.materialize(media_url=media_url, media_ref=source_id)
                break
            except Exception as exc:
                material_errors.append(str(exc))
        if materialized is None:
            raise StateTransitionError(
                "all retained audio/video download choices failed: " + " | ".join(material_errors)
            )
        transcript = self.transcriber.transcribe(
            media_ref=source_id, media_path=str(materialized.wav_path), max_duration_seconds=600
        )
        return {
            "artifact_kind": "transcript_and_comments", "source_id": source_id,
            "source_url": item["url"], "metrics": item["metrics"],
            "transcript_ref": transcript.payload["transcript_ref"],
            "transcript_hash": transcript.payload["transcript_hash"],
            "asr_model_ref": transcript.payload["asr_model_ref"],
            "vad_model_ref": transcript.payload["vad_model_ref"],
            "source_media_hash": materialized.source_sha256,
            "comments": _filter_comments(comments),
            "comment_collection_ref": collection_ref,
        }

    def prepare_collected_materials(
        self,
        *,
        registration: dict[str, Any],
        selected_items: list[dict[str, Any]],
        details_by_source: dict[str, dict[str, Any]],
        comments_by_source: dict[str, list[dict[str, Any]]],
        collection_ref_by_source: dict[str, str],
    ) -> tuple[dict[str, Any], ...]:
        """Finish material preparation from already-collected detail snapshots.

        The caller supplies only source ids from the original selected-hit set.
        This path never calls the platform collector, so an interrupted batch can
        continue with its real collected results instead of collecting them again.
        """
        registration_id = str(registration["registration_id"])
        existing = {
            item["item_ref"] for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        }
        pending = [
            item for item in selected_items
            if str(item.get("source_id") or "") not in existing
        ]
        failures: list[str] = []
        missing_details: list[str] = []
        delivery_retry_queue: list[dict[str, Any]] = []
        completed_count = len(existing)
        for position, item in enumerate(pending, start=1):
            source_id = str(item.get("source_id") or "")
            detail_item = details_by_source.get(source_id)
            if detail_item is None:
                missing_details.append(source_id)
                continue
            comments = comments_by_source.get(source_id, [])
            collection_ref = collection_ref_by_source.get(source_id, "")
            self.core.record_replenished_competitor_material_collection_checkpoint(
                registration_id=registration_id, item_ref=source_id,
                detail=detail_item, comments=comments, collection_ref=collection_ref,
            )
            self._report_progress(
                "hit_material_preparation",
                f"preparing material from saved detail {completed_count}/{len(pending)}; processing item {position}",
            )
            try:
                artifact = self._materialize_collected_detail(
                    item=item, detail_item=detail_item, comments=comments, collection_ref=collection_ref,
                )
                self.core.record_replenished_competitor_material_item(
                    registration_id=registration_id, item_ref=source_id,
                    status="completed", artifact=artifact, error=None,
                )
                completed_count += 1
                try:
                    self.process_prepared_breakdown(registration=registration, material=artifact)
                except CompetitorBreakdownFailed as exc:
                    if self._is_delivery_interruption(exc.failure_record):
                        delivery_retry_queue.append(artifact)
                        self._report_progress(
                            "hit_breakdown",
                            "本条服务端中断，先继续准备本次任务的其他材料；结束后自动原样补跑一次",
                        )
                    else:
                        self._report_progress(
                            "hit_breakdown",
                            "本条拆解未通过核查，已留档；继续处理本次任务其余材料",
                        )
            except CompetitorBreakdownFailed:
                raise
            except Exception as exc:
                failures.append(source_id)
                self.core.record_replenished_competitor_material_item(
                    registration_id=registration_id, item_ref=source_id,
                    status="failed", artifact=None, error={"reason": str(exc)},
                )
        if missing_details:
            raise StateTransitionError(
                f"saved detail is still missing for {len(missing_details)} recovery item(s); they remain excluded"
            )
        if failures:
            raise StateTransitionError(
                f"collected-detail material preparation failed for {len(failures)} item(s); failures remain recorded and are not automatically retried"
            )
        self._retry_delivery_interruptions_after_task(
            registration=registration,
            materials=delivery_retry_queue,
        )
        return tuple(
            item["artifact"] for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        )

    def _model_json(
        self,
        *,
        registration: dict[str, Any],
        step_name: str,
        input_payload: dict[str, Any],
        attempt_kind: str,
    ) -> tuple[dict[str, Any], str, str]:
        if step_name != "breakdown":
            raise StateTransitionError(
                "the competitor-registration model is reserved for individual hit breakdowns"
            )
        payload = {
            "correlation_id": f"{registration['registration_id']}:{step_name}",
            "source_id": str(input_payload["source_id"]),
            "transcript": str(input_payload["transcript"]),
            "metrics": dict(input_payload["metrics"]),
            "comments": list(input_payload["comments"]),
            "schema_version": "competitor_breakdown.input.v7",
        }
        result = FormalBusinessSkillAdapter(
            contract=FormalSkillContract.from_runtime_skill("competitor_breakdown_structural_v13"),
            gateway=self.gateway,
        ).run(
            payload,
            request_metadata={
                "automatic_retry": attempt_kind == "post_batch_delivery_retry",
                "breakdown_attempt_kind": attempt_kind,
                "competitor_registration_core": {
                    "registration_id": registration["registration_id"],
                    "step_name": step_name,
                    "source_id": str(input_payload["source_id"]),
                    "data_identity": self.core.data_identity,
                },
            },
        )
        return result.output_payload, result.model_run_envelope_version_id, result.raw_model_output

    def _breakdown_failure_record(self, exc: Exception) -> dict[str, Any]:
        raw_output = exc.raw_model_output if isinstance(exc, FormalSkillValidationError) else None
        record = {
            "reason": str(exc),
            "failure_stage": "model_output_validation" if isinstance(exc, FormalSkillValidationError) else "model_execution",
            "failure_type": type(exc).__name__,
            "raw_model_output": raw_output,
            "raw_model_output_status": "available" if raw_output is not None else "not_available",
            "automatic_retry": False,
        }
        if isinstance(exc, FormalSkillValidationError):
            record["model_run_envelope_version_id"] = exc.model_run_envelope_version_id
            record["model_completion_receipt"] = exc.model_completion_receipt
        if self._is_delivery_interruption(record):
            record["automatic_retry"] = True
            record["retry_disposition"] = "one_post_batch_delivery_retry_pending"
        return record

    @staticmethod
    def _is_delivery_interruption(error: dict[str, Any]) -> bool:
        """Only a provider-declared incomplete delivery may receive the one retry."""
        receipt = error.get("model_completion_receipt")
        return (
            isinstance(receipt, dict)
            and str(receipt.get("finish_reason") or "") == "abort"
            and str(error.get("failure_stage") or "") == "model_output_validation"
            and "model output is not JSON" in str(error.get("reason") or "")
        )

    def _retry_delivery_interruptions_after_task(
        self,
        *,
        registration: dict[str, Any],
        materials: list[dict[str, Any]],
    ) -> None:
        """After one concentrated task finishes, retry only provider-aborted deliveries once."""
        for position, material in enumerate(materials, start=1):
            source_id = str(material.get("source_id") or "")
            self._report_progress(
                "hit_breakdown",
                f"本次任务的常规材料已处理完，正在自动补跑第 {position}/{len(materials)} 条服务端中断材料",
            )
            try:
                self.process_prepared_breakdown(
                    registration=registration,
                    material=material,
                    attempt_kind="post_batch_delivery_retry",
                )
            except CompetitorBreakdownFailed as exc:
                if self._is_delivery_interruption(exc.failure_record):
                    self._report_progress(
                        "hit_breakdown",
                        f"第 {position} 条补拆仍被服务端中断，已留档；继续处理其余补拆材料",
                    )
                else:
                    self._report_progress(
                        "hit_breakdown",
                        f"第 {position} 条补拆未通过核查，已留档；继续处理其余补拆材料",
                    )

    def _record_atomic_breakdown_failure(
        self,
        *,
        registration: dict[str, Any],
        source_id: str,
        error: dict[str, Any],
    ) -> None:
        if registration["status"] == "processing":
            self.core.record_competitor_registration_item(
                registration_id=str(registration["registration_id"]), step_name="breakdown", item_ref=source_id,
                status="failed", artifact=None, error=error,
            )
        else:
            self.core.record_independent_competitor_breakdown(
                registration_id=str(registration["registration_id"]), item_ref=source_id,
                status="failed", artifact=None, error=error,
            )

    def process_prepared_breakdown(
        self,
        *,
        registration: dict[str, Any],
        material: dict[str, Any],
        attempt_kind: str = "initial",
    ) -> dict[str, Any]:
        """Run one source-bound breakdown and leave exactly one explicit outcome."""
        registration_id = str(registration["registration_id"])
        source_id = str(material.get("source_id") or "")
        if not source_id:
            raise StateTransitionError("prepared competitor material has no source identity")
        self._report_progress(
            "hit_breakdown",
            f"已提交第 {source_id} 条拆解，等待模型最终答复；当前接口不提供模型端实时进度",
        )
        try:
            transcript_text = Path(str(material["transcript_ref"])).read_text(encoding="utf-8")
            payload = {
                "source_id": source_id,
                "transcript": transcript_text,
                "metrics": material["metrics"],
                "comments": material["comments"],
            }
            value, model_run_id, raw_model_output = self._model_json(
                registration=registration, step_name="breakdown", input_payload=payload, attempt_kind=attempt_kind,
            )
            validate_competitor_breakdown_structural_output_semantics(
                {"source_id": source_id, "transcript": transcript_text, "comments": material["comments"]}, value,
            )
            artifact = {
                "artifact_kind": "deep_breakdown",
                "source_id": source_id,
                "model_run_id": model_run_id,
                "raw_model_output": raw_model_output,
                "deep_breakdown": value,
            }
            self.core.record_competitor_breakdown_attempt(
                registration_id=registration_id,
                source_id=source_id,
                attempt_kind=attempt_kind,
                outcome="completed",
                raw_model_output=raw_model_output,
                raw_model_output_status="available",
                model_run_id=model_run_id,
            )
            if registration["status"] == "processing":
                self.core.record_competitor_registration_item(
                    registration_id=registration_id, step_name="breakdown", item_ref=source_id,
                    status="completed", artifact=artifact, error=None,
                )
            else:
                self.core.record_independent_competitor_breakdown(
                    registration_id=registration_id, item_ref=source_id,
                    status="completed", artifact=artifact, error=None,
                    automatic_delivery_retry=attempt_kind == "post_batch_delivery_retry",
                )
            return artifact
        except Exception as exc:
            error = self._breakdown_failure_record(exc)
            delivery_interrupted = self._is_delivery_interruption(error)
            self.core.record_competitor_breakdown_attempt(
                registration_id=registration_id,
                source_id=source_id,
                attempt_kind=attempt_kind,
                outcome="delivery_interrupted" if delivery_interrupted else "failed",
                reason=str(error.get("reason") or ""),
                raw_model_output=error.get("raw_model_output"),
                raw_model_output_status=str(error.get("raw_model_output_status") or "not_available"),
                model_run_id=error.get("model_run_envelope_version_id"),
            )
            self._record_atomic_breakdown_failure(
                registration=registration, source_id=source_id, error=error,
            )
            raise CompetitorBreakdownFailed(
                f"atomic competitor breakdown failed for {source_id}", failure_record=error,
            ) from exc

    def _breakdown(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        materials = _step_artifacts(completed, "transcripts_and_comments")
        materials = [item for item in materials if item.get("artifact_kind") == "transcript_and_comments"]
        self._report_progress("hit_breakdown", f"正在逐条拆解 {len(materials)} 条已备料爆款")
        registration_id = str(registration["registration_id"])
        recorded = {
            item["item_ref"]: item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            )
        }
        existing = {source_id: item for source_id, item in recorded.items() if item["status"] == "completed"}
        delivery_retry_queue: list[dict[str, Any]] = []
        completed_count = len(existing)
        total_count = len(materials)
        for position, material in enumerate(materials, start=1):
            source_id = str(material.get("source_id") or "")
            if source_id in existing:
                continue
            if source_id in recorded and recorded[source_id]["status"] in {"failed", "excluded"}:
                self._report_progress(
                    "hit_breakdown",
                    f"第 {position} 条已有未完成留档，继续处理本次任务其余材料",
                )
                continue
            self._report_progress(
                "hit_breakdown",
                f"爆款拆解已完成 {completed_count}/{total_count}，正在处理第 {position} 条",
            )
            try:
                self.process_prepared_breakdown(registration=registration, material=material)
                completed_count += 1
            except CompetitorBreakdownFailed as exc:
                if self._is_delivery_interruption(exc.failure_record):
                    delivery_retry_queue.append(material)
                    self._report_progress(
                        "hit_breakdown",
                        f"第 {position} 条服务端中断，先继续本批；结束后会自动原样补跑一次",
                    )
                    continue
                self._report_progress(
                    "hit_breakdown",
                    f"第 {position} 条未通过核查，已留档；继续处理本次任务其余材料",
                )
                continue
            except Exception as exc:
                error = self._breakdown_failure_record(exc)
                self._record_atomic_breakdown_failure(
                    registration=registration, source_id=source_id, error=error,
                )
                self._report_progress(
                    "hit_breakdown",
                    f"第 {position} 条执行异常，已留档；继续处理本次任务其余材料",
                )
                continue
        for retry_position, material in enumerate(delivery_retry_queue, start=1):
            source_id = str(material.get("source_id") or "")
            self._report_progress(
                "hit_breakdown",
                f"本批常规拆解结束，正在自动补跑第 {retry_position}/{len(delivery_retry_queue)} 条服务端中断材料",
            )
            try:
                self.process_prepared_breakdown(
                    registration=registration,
                    material=material,
                    attempt_kind="post_batch_delivery_retry",
                )
                completed_count += 1
            except CompetitorBreakdownFailed as exc:
                if self._is_delivery_interruption(exc.failure_record):
                    self._report_progress(
                        "hit_breakdown",
                        f"第 {retry_position} 条补拆仍被服务端中断，已留档；继续处理其余补拆材料",
                    )
                else:
                    self._report_progress(
                        "hit_breakdown",
                        f"第 {retry_position} 条补拆未通过核查，已留档；继续处理其余补拆材料",
                    )
        failed_records = [
            item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            ) if item["status"] == "failed"
        ]
        if failed_records:
            details = "; ".join(
                f"{item['item_ref']}: {str((item.get('error') or {}).get('reason') or 'unknown failure')}"
                for item in failed_records
            )
            raise StateTransitionError(
                f"competitor deep breakdown completed every first attempt and delivery retry; "
                f"{len(failed_records)} item(s) need final summary handling: {details}"
            )
        artifacts = [
            item["artifact"] for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            ) if item["status"] == "completed"
        ]
        return tuple(artifacts or [{"artifact_kind": "breakdown", "selected_count": 0, "items": []}])

    def _tag_candidates(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        high_signal_records = _step_artifacts(completed, "high_signal_identification")
        selected_items = [
            item
            for record in high_signal_records
            for item in record.get("selected_items", [])
            if isinstance(item, dict) and str(item.get("source_id") or "").strip()
        ]
        self._report_progress("tag_candidate_extraction", "正在读取历史高信号作品自带的话题标签")
        candidates_by_key: dict[str, dict[str, Any]] = {}
        for item in selected_items:
            source_id = str(item["source_id"])
            for raw_tag in _SOURCE_HASHTAG_PATTERN.findall(str(item.get("title") or "")):
                tag = raw_tag.strip().strip(_TAG_EDGE_PUNCTUATION)
                if not tag:
                    continue
                key = tag.casefold()
                candidate = candidates_by_key.setdefault(
                    key,
                    {
                        "tag": tag,
                        "source_ids": [],
                        "reason": "历史高信号作品原文自带话题标签",
                    },
                )
                if source_id not in candidate["source_ids"]:
                    candidate["source_ids"].append(source_id)
        return ({
            "artifact_kind": "tag_candidates",
            "extraction_method": "source_hashtag_deterministic_v1",
            "source_kind": "historical_high_signal_content",
            "model_run_id": None,
            "candidates": sorted(
                candidates_by_key.values(),
                key=lambda item: (-len(item["source_ids"]), str(item["tag"]).casefold()),
            ),
            "human_confirmation_required": False,
            "whole_library_review_required_after_all_registrations": True,
        },)


def build_configured_competitor_registration_executor(
    core: Stage0ContentProductionCore,
    *,
    progress_callback: Callable[[str, str], None] | None = None,
) -> ConfiguredCompetitorRegistrationExecutor:
    """Bind the generic worker to the real local collector, media, ASR and configured model route."""
    archive_root = require_runtime_path(Path(
        os.environ.get("COMPETITOR_REGISTRATION_ARCHIVE_ROOT")
        or runtime_path("formal", "competitor_registration_runtime")
    ), purpose="competitor registration archive")
    def runtime_progress(detail: str) -> None:
        if progress_callback is not None:
            progress_callback("external_runtime", detail or "外部任务仍在运行")

    crawler_executor = LocalMediaCrawlerExecutor(
        archive_root=archive_root / "mediacrawler", progress_callback=runtime_progress
    )
    collector = MediaCrawlerCollectorAdapter(crawler_executor)
    asr_executor = LocalSenseVoiceExecutor(
        python_executable=Path(external_runtime_value("SENSEVOICE_PYTHON")),
        asr_model=external_runtime_value("SENSEVOICE_ASR_MODEL"),
        vad_model=external_runtime_value("SENSEVOICE_VAD_MODEL"),
        archive_root=archive_root / "transcripts",
        worker_path=Path(external_runtime_value("SENSEVOICE_WORKER")),
        progress_callback=runtime_progress,
    )
    return ConfiguredCompetitorRegistrationExecutor(
        core=core, collector=collector, transcriber=AsrAdapter(asr_executor),
        media_materializer=LocalCompetitorMediaMaterializer(
            archive_root=archive_root / "media",
            ffmpeg_executable=Path(external_runtime_value("FFMPEG_PATH")),
        ),
        gateway=build_production_competitor_registration_gateway(core),
        max_historical_items=configured_first_registration_item_limit(),
        progress_callback=progress_callback,
    )


def configured_first_registration_item_limit() -> int:
    value = int(
        os.environ.get("COMPETITOR_FIRST_CRAWL_MAX_ITEMS")
        or str(FIRST_REGISTRATION_MAX_ITEMS)
    )
    if value != FIRST_REGISTRATION_MAX_ITEMS:
        raise StateTransitionError(
            "first competitor registration must collect exactly 50 historical items"
        )
    return value


class CompetitorRegistrationService:
    def __init__(self, *, core: Stage0ContentProductionCore, executor: CompetitorRegistrationStepExecutor):
        self.core = core
        self.executor = executor

    def run_user_authorized_atomic_breakdown(
        self,
        *,
        registration_id: str,
        source_id: str,
    ) -> dict[str, Any]:
        """Run exactly one explicitly authorized retired breakdown candidate.

        The caller is responsible for carrying the user's confirmed command.
        This method never resumes a failed item and never advances to another
        source after the requested source reaches its one final outcome.
        """
        if not registration_id.strip() or not source_id.strip():
            raise StateTransitionError("user-authorized competitor breakdown needs one registration and one source")
        registration = self.core.get_competitor_registration(registration_id=registration_id)
        prepared = next((
            item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            )
            if str(item.get("item_ref") or "") == source_id
        ), None)
        if prepared is None or prepared.get("status") != "completed" or not isinstance(prepared.get("artifact"), dict):
            raise StateTransitionError("the requested source has no completed formal material")
        existing = next((
            item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            )
            if str(item.get("item_ref") or "") == source_id
        ), None)
        if existing is not None:
            status = str(existing.get("status") or "")
            if status == "failed":
                raise StateTransitionError("the requested breakdown already failed and must not retry")
            if status == "completed":
                raise StateTransitionError("the requested breakdown is already completed and will not run again")
            error = existing.get("error")
            retired_by_user = isinstance(error, dict) and (
                error.get("disposition") == "retired_by_user"
                or error.get("reason") == "retired_by_user"
            )
            if status != "excluded" or not retired_by_user:
                raise StateTransitionError("the requested breakdown is not an explicitly retired candidate")
        runner = getattr(self.executor, "process_prepared_breakdown", None)
        if not callable(runner):
            raise StateTransitionError("configured competitor executor does not support atomic breakdown")
        artifact = runner(registration=registration, material=dict(prepared["artifact"]))
        return {
            "source_id": source_id,
            "status": "completed",
            "artifact_kind": str(artifact.get("artifact_kind") or "deep_breakdown"),
        }

    def replace_all_cold_start_breakdowns(
        self,
        *,
        cold_start_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Discard every retired result, then run every retained material once on the current Skill."""
        discarded = self.core.discard_cold_start_breakdown_history(
            cold_start_id=cold_start_id,
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=f"{idempotency_key}:discard",
        )
        materials = self.core.list_cold_start_breakdown_materials(cold_start_id=cold_start_id)
        completed: list[str] = []
        for item in materials:
            registration = self.core.get_competitor_registration(
                registration_id=str(item["registration_id"])
            )
            try:
                self.executor.process_prepared_breakdown(
                    registration=registration,
                    material=dict(item["material"]),
                )
            except Exception as exc:
                raise StateTransitionError(
                    "replacement breakdown stopped at one retained source; the failed item is recorded and no prior result remains"
                ) from exc
            completed.append(str(item["source_id"]))
        return {
            "cold_start_id": cold_start_id,
            "status": "completed",
            "discarded": discarded,
            "replacement_count": len(completed),
        }

    def run_selected_cold_start_breakdowns(
        self,
        *,
        cold_start_id: str,
        source_ids: list[str],
    ) -> dict[str, Any]:
        """Run one explicitly bounded, cross-account review batch without touching other materials."""
        unique_source_ids = list(dict.fromkeys(str(source_id).strip() for source_id in source_ids if str(source_id).strip()))
        if len(unique_source_ids) != 30:
            raise StateTransitionError("the review batch must contain exactly 30 distinct source materials")
        retained = {
            str(item["source_id"]): item
            for item in self.core.list_cold_start_breakdown_materials(cold_start_id=cold_start_id)
        }
        missing = [source_id for source_id in unique_source_ids if source_id not in retained]
        if missing:
            raise StateTransitionError("the review batch includes material outside the retained cold-start source set")
        selected = [retained[source_id] for source_id in unique_source_ids]
        registrations = [str(item["registration_id"]) for item in selected]
        if len(set(registrations)) != 30:
            raise StateTransitionError("the review batch must use one source from each of 30 different accounts")
        already_completed: list[str] = []
        already_failed: list[str] = []
        pending: list[dict[str, Any]] = []
        for item in selected:
            existing = self.core.list_competitor_registration_items(
                registration_id=str(item["registration_id"]), step_name="breakdown"
            )
            current = next(
                (row for row in existing if str(row["item_ref"]) == str(item["source_id"])),
                None,
            )
            if current is None:
                pending.append(item)
            elif str(current["status"]) == "completed":
                already_completed.append(str(item["source_id"]))
            else:
                already_failed.append(str(item["source_id"]))
        completed: list[str] = []
        failed: list[str] = []
        for item in pending:
            registration = self.core.get_competitor_registration(
                registration_id=str(item["registration_id"])
            )
            try:
                self.executor.process_prepared_breakdown(
                    registration=registration,
                    material=dict(item["material"]),
                )
            except Exception as exc:
                failed.append(str(item["source_id"]))
                continue
            completed.append(str(item["source_id"]))
        return {
            "cold_start_id": cold_start_id,
            "status": "completed" if not already_failed and not failed else "completed_with_failures",
            "review_batch_size": len(already_completed) + len(completed),
            "completed_source_ids": already_completed + completed,
            "failed_source_ids": already_failed + failed,
        }

    def run_formal_breakdown_backlog_task(
        self,
        *,
        backlog_task_id: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Finish exactly the source snapshot approved for one formal catch-up task."""
        task = self.core.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        if task["status"] in {"completed", "completed_with_failures"}:
            return self.core.competitor_breakdown_backlog_task_progress(backlog_task_id=backlog_task_id)
        snapshot = task["source_snapshot"]
        if not isinstance(snapshot, list) or not snapshot:
            raise StateTransitionError("formal breakdown backlog task has no frozen material snapshot")
        retained = {
            (str(item["registration_id"]), str(item["source_id"])): item
            for item in self.core.list_cold_start_breakdown_materials(cold_start_id=str(task["cold_start_id"]))
        }
        selected: list[dict[str, Any]] = []
        for item in snapshot:
            if not isinstance(item, dict):
                raise StateTransitionError("formal breakdown backlog task has an invalid frozen material")
            key = (str(item.get("registration_id") or ""), str(item.get("source_id") or ""))
            material = retained.get(key)
            if material is None:
                raise StateTransitionError("a frozen formal breakdown material is no longer available")
            selected.append(material)

        runner = getattr(self.executor, "process_prepared_breakdown", None)
        delivery_check = getattr(self.executor, "_is_delivery_interruption", None)
        if not callable(runner) or not callable(delivery_check):
            raise StateTransitionError("configured competitor executor cannot run a formal backlog task")

        def report(*, phase: str, position: int, retry_position: int | None = None) -> None:
            current = self.core.competitor_breakdown_backlog_task_progress(backlog_task_id=backlog_task_id)
            current = self.core.update_competitor_breakdown_backlog_task(
                backlog_task_id=backlog_task_id,
                status="running",
                summary=current["summary"],
            )
            if progress_callback is not None:
                progress_callback({
                    "phase": phase,
                    "position": position,
                    "retry_position": retry_position,
                    "summary": current["summary"],
                })

        self.core.update_competitor_breakdown_backlog_task(
            backlog_task_id=backlog_task_id,
            status="running",
            summary=self.core.competitor_breakdown_backlog_task_progress(backlog_task_id=backlog_task_id)["summary"],
        )
        delivery_retry_queue: list[dict[str, Any]] = []
        for position, item in enumerate(selected, start=1):
            registration_id, source_id = str(item["registration_id"]), str(item["source_id"])
            existing = next((
                row for row in self.core.list_competitor_registration_items(
                    registration_id=registration_id, step_name="breakdown"
                ) if str(row["item_ref"]) == source_id
            ), None)
            if existing is not None and str(existing["status"]) == "completed":
                report(phase="initial", position=position)
                continue
            if existing is not None and str(existing["status"]) == "failed":
                latest_attempt = self.core.latest_competitor_breakdown_attempt(
                    registration_id=registration_id,
                    source_id=source_id,
                )
                if latest_attempt == {"attempt_kind": "initial", "outcome": "delivery_interrupted"}:
                    delivery_retry_queue.append(item)
                report(phase="initial", position=position)
                continue
            registration = self.core.get_competitor_registration(registration_id=registration_id)
            try:
                runner(registration=registration, material=dict(item["material"]))
            except CompetitorBreakdownFailed as exc:
                if delivery_check(exc.failure_record):
                    delivery_retry_queue.append(item)
            report(phase="initial", position=position)

        for retry_position, item in enumerate(delivery_retry_queue, start=1):
            registration = self.core.get_competitor_registration(registration_id=str(item["registration_id"]))
            try:
                runner(
                    registration=registration,
                    material=dict(item["material"]),
                    attempt_kind="post_batch_delivery_retry",
                )
            except CompetitorBreakdownFailed:
                pass
            report(phase="post_batch_delivery_retry", position=len(selected), retry_position=retry_position)

        final = self.core.competitor_breakdown_backlog_task_progress(backlog_task_id=backlog_task_id)
        terminal_status = "completed" if int(final["summary"]["failed"]) == 0 else "completed_with_failures"
        final = self.core.update_competitor_breakdown_backlog_task(
            backlog_task_id=backlog_task_id,
            status=terminal_status,
            summary=final["summary"],
        )
        if progress_callback is not None:
            progress_callback({"phase": terminal_status, "position": len(selected), "summary": final["summary"]})
        return final

    def run_competitor_registration_step(
        self, *, registration_id: str, actor: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Run exactly one unfinished step so external resources can be scheduled independently."""
        if not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("competitor registration execution requires an actor and idempotency key")
        registration = self.core.get_competitor_registration(registration_id=registration_id)
        if registration["status"] == "awaiting_human_review":
            return self.core.complete_competitor_registration(
                registration_id=registration_id,
                actor=actor,
                idempotency_key=f"{idempotency_key}:complete",
            )
        if registration["status"] != "processing" or registration["current_step"] not in COMPETITOR_REGISTRATION_STEPS:
            raise StateTransitionError("competitor registration is not ready for automatic execution")
        step_name = str(registration["current_step"])
        completed = self.core.list_competitor_registration_steps(registration_id=registration_id)
        artifact_refs = self.executor.execute(
            step_name=step_name,
            registration=registration,
            completed_artifacts=tuple(completed),
        )
        if not artifact_refs or not all(isinstance(item, dict) and item for item in artifact_refs):
            raise StateTransitionError("competitor registration executor returned no formal artifact references")
        artifact_set_digest = hashlib.sha256(
            json.dumps(
                list(artifact_refs),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        return self.core.record_competitor_registration_step(
            registration_id=registration_id,
            step_name=step_name,
            artifact_refs=artifact_refs,
            actor=actor,
            idempotency_key=f"{idempotency_key}:{step_name}:{artifact_set_digest}",
        )

    def run_competitor_registration(
        self, *, registration_id: str, actor: str, idempotency_key: str
    ) -> dict[str, Any]:
        """Run every unfinished registration step and complete registration automatically."""
        if not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("competitor registration execution requires an actor and idempotency key")
        completed: list[dict[str, Any]] = self.core.list_competitor_registration_steps(
            registration_id=registration_id
        )
        while True:
            registration = self.core.get_competitor_registration(registration_id=registration_id)
            if registration["status"] == "awaiting_human_review":
                return self.core.complete_competitor_registration(
                    registration_id=registration_id,
                    actor=actor,
                    idempotency_key=f"{idempotency_key}:complete",
                )
            if registration["status"] != "processing" or registration["current_step"] not in COMPETITOR_REGISTRATION_STEPS:
                raise StateTransitionError("competitor registration is not ready for automatic execution")
            step_name = str(registration["current_step"])
            artifact_refs = self.executor.execute(
                step_name=step_name, registration=registration, completed_artifacts=tuple(completed)
            )
            if not artifact_refs or not all(isinstance(item, dict) and item for item in artifact_refs):
                raise StateTransitionError("competitor registration executor returned no formal artifact references")
            artifact_set_digest = hashlib.sha256(
                json.dumps(
                    list(artifact_refs),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:20]
            transition = self.core.record_competitor_registration_step(
                registration_id=registration_id, step_name=step_name, artifact_refs=artifact_refs,
                actor=actor,
                idempotency_key=f"{idempotency_key}:{step_name}:{artifact_set_digest}",
            )
            completed.append({"step_name": step_name, "artifact_refs": list(artifact_refs), "transition": transition})
