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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.core.external_adapters import (
    AsrAdapter,
    LocalCompetitorMediaMaterializer,
    LocalMediaCrawlerExecutor,
    LocalSenseVoiceExecutor,
    MediaCrawlerCollectorAdapter,
)
from scripts.core.business_data.domain_labels import (
    get_content_type_registry,
    get_domain_pack,
    require_frozen_content_type_registry,
)
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.production.stage0_content_core import (
    COMPETITOR_REGISTRATION_STEPS,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired
from scripts.core.runtime.runtime_storage import require_runtime_path, runtime_path
from scripts.core.production.high_signal_policy import (
    FIRST_REGISTRATION_MAX_ITEMS,
    HISTORICAL_METRICS,
    HISTORICAL_MATURITY_DAYS,
    MATURE_HISTORY_WINDOW_DAYS,
    MIN_RELIABLE_HISTORY_ITEMS,
    build_historical_collection_artifact,
    build_high_signal_artifact,
)
from scripts.core.external_adapters.runtime_config import external_runtime_value


CONTENT_SUBJECT_TYPES = frozenset({
    "person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear",
})
EXPRESSION_FORMS = frozenset({
    "story", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear",
})
METRIC_NAMES = HISTORICAL_METRICS
_SOURCE_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")
CONTENT_TYPE_LIFECYCLE_MODES = frozenset({"discover", "classify"})


class CompetitorBreakdownFailed(StateTransitionError):
    """One atomic breakdown failed after its complete failure record was saved."""

    def __init__(self, message: str, *, failure_record: dict[str, Any]) -> None:
        super().__init__(message)
        self.failure_record = dict(failure_record)



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


def _breakdown_domain_context(
    domain_label: str,
    *,
    observed_content_types: list[str] | None = None,
    content_type_lifecycle: str = "discover",
    content_type_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pass domain rules plus the domain-owned type registry snapshot.

    The default keeps the old cold-start/test caller in DISCOVER mode.  Real
    daily production callers pass CLASSIFY explicitly and are fail-closed when
    the registry is not frozen.
    """
    label = str(domain_label or "generic").strip() or "generic"
    lifecycle = str(content_type_lifecycle or "discover").strip().casefold()
    if lifecycle not in CONTENT_TYPE_LIFECYCLE_MODES:
        raise StateTransitionError(
            f"unsupported competitor breakdown content type lifecycle: {content_type_lifecycle}"
        )
    try:
        domain_pack = get_domain_pack(label)
    except ValueError:
        domain_pack = {}
    if content_type_registry is None:
        try:
            registry = (
                require_frozen_content_type_registry(label)
                if lifecycle == "classify"
                else get_content_type_registry(label)
            )
        except ValueError:
            if lifecycle == "classify":
                raise StateTransitionError(
                    f"content type classification is disabled until domain registry validation passes: {label}"
                )
            registry = {"status": "NOT_FROZEN", "version": "0", "types": []}
    else:
        registry = dict(content_type_registry)
    if lifecycle == "classify" and str(registry.get("status") or "").strip().casefold() != "frozen":
        raise StateTransitionError(
            f"content type classification is disabled until domain registry is FROZEN: {label}"
        )
    discovery = domain_pack.get("discovery") if isinstance(domain_pack, dict) else {}
    discovery = discovery if isinstance(discovery, dict) else {}
    expansion_policy = (
        domain_pack.get("question_expansion_policy")
        if isinstance(domain_pack, dict)
        else {}
    )
    expansion_policy = expansion_policy if isinstance(expansion_policy, dict) else {}
    return {
        "label": label,
        "description": str(domain_pack.get("description") or "") if isinstance(domain_pack, dict) else "",
        "allowed_scope": str(domain_pack.get("description") or "") if isinstance(domain_pack, dict) else "",
        "excluded_terms": [
            str(item).strip()
            for item in discovery.get("exclude_terms", [])
            if str(item).strip()
        ],
        "risk_block_terms": [
            str(item).strip()
            for item in discovery.get("risk_block_terms", [])
            if str(item).strip()
        ],
        "observed_content_types": [
            str(item).strip() for item in (observed_content_types or []) if str(item).strip()
        ],
        "content_type_lifecycle": lifecycle,
        "content_type_registry": registry,
        "question_expansion_policy": expansion_policy,
    }


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


def _published_at_for_collection(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("published_at") or 0))
    except (TypeError, ValueError):
        return 0


def _filter_comments(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    def has_parent_comment(value: Any) -> bool:
        """Treat platform root markers as no parent, not as a reply."""
        if value is None or value is False:
            return False
        if isinstance(value, (int, float)) and value == 0:
            return False
        return str(value).strip().casefold() not in {"", "0", "none", "null"}

    retained: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        text = str(value.get("text") or value.get("content") or "").strip()
        likes = int(value.get("like_count") or 0)
        parent = value.get("parent_comment_id") or value.get("reply_to_reply_id")
        if likes < 1 or len(text) < 5 or has_parent_comment(parent):
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


def prepare_test_only_competitor_breakdown_batch(
    *,
    test_id: str,
    materials: list[dict[str, Any]],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Prepare test materials as external tasks without executing a model."""
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
            "domain_label": str(material.get("domain_label") or "generic").strip() or "generic",
            "domain_context": (
                dict(material["domain_context"])
                if isinstance(material.get("domain_context"), dict)
                else _breakdown_domain_context(str(material.get("domain_label") or "generic"))
            ),
        })

    outcomes: list[dict[str, Any]] = []
    observed_types_by_domain: dict[str, list[str]] = {}
    for material in normalized_materials:
        domain_label = str(material["domain_label"] or "generic")
        context = material.get("domain_context")
        supplied_types = context.get("observed_content_types") if isinstance(context, dict) else []
        observed_types_by_domain.setdefault(domain_label, [])
        for value in supplied_types if isinstance(supplied_types, list) else []:
            content_type = str(value).strip()
            if content_type and content_type not in observed_types_by_domain[domain_label]:
                observed_types_by_domain[domain_label].append(content_type)

    contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
    for material in normalized_materials:
        if progress_callback is not None:
            progress_callback({
                "source_count": len(normalized_materials),
                "current_position": material["position"],
                "current_source_id": material["source_id"],
                "completed": 0,
                "failed": 0,
                "activity": "external_task_prepared",
            })
        context = material.get("domain_context") or {}
        input_payload = {
            "correlation_id": f"{test_id}:{material['position']}:{material['source_id']}",
            "source_id": material["source_id"],
            "transcript": material["transcript"],
            "metrics": material["metrics"],
            "comments": material["comments"],
            "domain_label": material["domain_label"],
            "domain_context": _breakdown_domain_context(
                material["domain_label"],
                observed_content_types=observed_types_by_domain.get(material["domain_label"], []),
                content_type_lifecycle=str(context.get("content_type_lifecycle") or "discover"),
                content_type_registry=context.get("content_type_registry"),
            ),
            "schema_version": "competitor_breakdown.input.v1",
        }
        task, _ = prepare_external_skill_task(
            contract,
            input_payload,
            constraints={
                "use_only_supplied_material": True,
                "preserve_source_identity": True,
                "cannot_change_business_state": True,
                "do_not_search": True,
                "no_fuzzy_evidence_matching": True,
            },
            business_context={
                "origin": "competitor_breakdown_test",
                "test_id": test_id,
                "source_id": material["source_id"],
                "data_identity": "test",
            },
        )
        task["task_identity"] = {
            "task_type": "competitor_breakdown",
            "test_id": test_id,
            "source_id": material["source_id"],
        }
        outcomes.append({
            "position": material["position"],
            "hit_id": material["hit_id"],
            "source_id": material["source_id"],
            "title": material["title"],
            "status": "requires_external_intelligence",
            "task": task,
        })

    return {
        "kind": "competitor_breakdown_test_batch",
        "test_id": test_id,
        "formal_business_data_written": False,
        "automatic_retry": False,
        "model_delivery": "external_executor_structured_submit",
        "outcomes": outcomes,
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
        gateway: Any | None = None,
        external_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        max_historical_items: int = FIRST_REGISTRATION_MAX_ITEMS,
        progress_callback: Callable[[str, str], None] | None = None,
        on_material_change: Callable[[str], None] | None = None,
    ) -> None:
        if max_historical_items != FIRST_REGISTRATION_MAX_ITEMS:
            raise StateTransitionError(
                "first competitor registration must collect exactly 50 historical items"
            )
        self.core = core
        self.collector = collector
        self.transcriber = transcriber
        self.media_materializer = media_materializer
        if gateway is not None:
            raise StateTransitionError(
                "formal competitor execution must use an external executor; direct model gateways are not supported"
            )
        self.gateway = None
        self.external_executor = external_executor
        self.max_historical_items = max_historical_items
        self.progress_callback = progress_callback
        self.on_material_change = on_material_change

    def _report_progress(self, phase: str, detail: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(phase, detail)

    def _notify_material_change(self, reason: str, source_id: str) -> None:
        if self.on_material_change is None:
            return
        try:
            self.on_material_change(f"{reason}:{source_id}")
        except Exception:
            # Obsidian is a read mirror; a mirror problem must never turn a
            # successfully prepared formal material into a failed material.
            return

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
        window_start = evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
        mature_cutoff = evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
        cutoff = datetime.fromtimestamp(window_start, timezone.utc)
        progress = self.core.get_competitor_historical_page_progress(
            registration_id=str(registration["registration_id"])
        )
        if progress is not None:
            evaluated_at = int(progress.get("evaluated_at") or evaluated_at)
            window_start = evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
            mature_cutoff = evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
            cutoff = datetime.fromtimestamp(window_start, timezone.utc)

        items_by_id: dict[str, dict[str, Any]] = {}
        archive_refs: list[str] = []

        def merge_items(raw_items: list[dict[str, Any]], *, phase: str) -> None:
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                item = _sanitize_item(raw_item)
                source_id = str(item.get("source_id") or "").strip()
                if not source_id:
                    raise StateTransitionError("historical page returned an item without a source identity")
                published_at = _published_at_for_collection(item)
                if phase == "recent_window":
                    if published_at < window_start:
                        continue
                elif phase == "older_backfill":
                    if not (0 < published_at < window_start):
                        continue
                if source_id not in items_by_id and len(items_by_id) >= FIRST_REGISTRATION_MAX_ITEMS:
                    continue
                items_by_id[source_id] = item

        def recent_count() -> int:
            return sum(
                _published_at_for_collection(item) >= window_start
                for item in items_by_id.values()
            )

        def mature_count() -> int:
            return sum(
                0 < _published_at_for_collection(item) <= mature_cutoff
                for item in items_by_id.values()
            )

        def record_progress(
            *,
            phase: str,
            status: str,
            next_cursor: str,
            has_more: bool,
            reached_time_boundary: bool,
            stop_reason: str,
        ) -> None:
            self.core.record_competitor_historical_page_progress(
                registration_id=str(registration["registration_id"]),
                phase=phase,
                status=status,
                evaluated_at=evaluated_at,
                next_cursor=next_cursor,
                has_more=has_more,
                reached_time_boundary=reached_time_boundary,
                stop_reason=stop_reason,
                page_request_count=len(archive_refs),
                raw_archive_refs=archive_refs,
                mature_item_count=mature_count(),
                recent_item_count=recent_count(),
            )

        phase = "recent_window"
        cursor = ""
        has_more = True
        reached_time_boundary = False
        stop_reason = "starting"
        last_command_hash = "paged-history"
        last_output_hash = "paged-history"

        if progress is not None:
            stored_refs = progress.get("raw_archive_refs")
            if not isinstance(stored_refs, list) or not stored_refs:
                raise StateTransitionError("historical page progress lacks successful page archives for resume")
            for raw_ref in stored_refs:
                archive_ref = str(raw_ref or "").strip()
                if archive_ref and archive_ref not in archive_refs:
                    page_items = self.collector.read_video_snapshot_archive(
                        raw_archive_ref=archive_ref,
                        platform=platform,
                    )
                    merge_items(page_items, phase="recent_window")
                    merge_items(page_items, phase="older_backfill")
                    archive_refs.append(archive_ref)
            phase = str(progress.get("phase") or "recent_window")
            cursor = str(progress.get("next_cursor") or "").strip()
            has_more = bool(progress.get("has_more"))
            reached_time_boundary = bool(progress.get("reached_time_boundary"))
            stop_reason = str(progress.get("stop_reason") or "resume")
            stored_status = str(progress.get("status") or "running")
            if stored_status in {"completed", "history_exhausted_insufficient"}:
                insufficient = stored_status == "history_exhausted_insufficient"
                return (
                    self._build_paged_history_artifact(
                        registration=registration,
                        platform=platform,
                        items=list(items_by_id.values()),
                        archive_refs=archive_refs,
                        evaluated_at=evaluated_at,
                        collection_status=(
                            "history_exhausted_insufficient" if insufficient else "target_reached"
                        ),
                        history_exhausted=insufficient,
                        collection_stop_reason=stop_reason,
                        page_request_count=len(archive_refs),
                        command_hash=last_command_hash,
                        output_hash=last_output_hash,
                    ),
                )

        while True:
            if not has_more:
                insufficient = mature_count() < MIN_RELIABLE_HISTORY_ITEMS
                return (
                    self._build_paged_history_artifact(
                        registration=registration,
                        platform=platform,
                        items=list(items_by_id.values()),
                        archive_refs=archive_refs,
                        evaluated_at=evaluated_at,
                        collection_status=(
                            "history_exhausted_insufficient" if insufficient else "target_reached"
                        ),
                        history_exhausted=insufficient,
                        collection_stop_reason=(
                            "history_exhausted" if insufficient else "mature_target_reached"
                        ),
                        page_request_count=len(archive_refs),
                        command_hash=last_command_hash,
                        output_hash=last_output_hash,
                    ),
                )
            if not cursor and archive_refs and phase == "older_backfill":
                raise StateTransitionError("historical paging cannot continue without a cursor")

            self._report_progress(
                "historical_collection",
                "正在按当前时间范围逐页读取历史内容"
                if phase == "recent_window"
                else "最近90天成熟样本不足20条，正在沿当前游标向前补足",
            )
            result = self.collector.collect_video_snapshot_page(
                platform=platform,
                source_url=source_ref,
                continuation_cursor=cursor,
                published_after=(cutoff if phase == "recent_window" else None),
            )
            payload = result.payload if isinstance(result.payload, dict) else {}
            page_items = payload.get("items")
            pagination = payload.get("pagination")
            if not isinstance(page_items, list) or not isinstance(pagination, dict):
                raise StateTransitionError("historical page result lacks items or pagination state")
            archive_ref = str(result.raw_archive_ref or "").strip()
            if not archive_ref:
                raise StateTransitionError("historical page result lacks its raw archive receipt")
            if archive_ref not in archive_refs:
                archive_refs.append(archive_ref)
            merge_items(page_items, phase=phase)
            last_command_hash = str(result.command_hash or "paged-history")
            last_output_hash = str(result.output_hash or "paged-history")

            # Fifty is the collection cap, not merely a later retention cap.
            # Once the existing collected-item set reaches it, finish this
            # account without looking at another cursor or page boundary.
            if len(items_by_id) >= FIRST_REGISTRATION_MAX_ITEMS:
                return (
                    self._build_paged_history_artifact(
                        registration=registration,
                        platform=platform,
                        items=list(items_by_id.values()),
                        archive_refs=archive_refs,
                        evaluated_at=evaluated_at,
                        collection_status="target_reached",
                        history_exhausted=False,
                        collection_stop_reason="maximum_item_count_reached",
                        page_request_count=len(archive_refs),
                        command_hash=last_command_hash,
                        output_hash=last_output_hash,
                    ),
                )

            next_cursor = str(pagination.get("next_cursor") or "").strip()
            page_has_more = bool(pagination.get("has_more"))
            page_reached_boundary = bool(pagination.get("reached_time_boundary"))
            page_stop_reason = str(pagination.get("stop_reason") or "page_complete")
            if page_has_more and next_cursor == cursor:
                raise StateTransitionError("historical paging returned an unchanged cursor while more history remained")
            cursor = next_cursor
            has_more = page_has_more
            reached_time_boundary = reached_time_boundary or page_reached_boundary
            stop_reason = page_stop_reason

            if phase == "recent_window":
                recent_finished = (
                    page_reached_boundary
                    or not page_has_more
                )
                if recent_finished:
                    if mature_count() >= MIN_RELIABLE_HISTORY_ITEMS:
                        return (
                            self._build_paged_history_artifact(
                                registration=registration,
                                platform=platform,
                                items=list(items_by_id.values()),
                                archive_refs=archive_refs,
                                evaluated_at=evaluated_at,
                                collection_status="target_reached",
                                history_exhausted=False,
                                collection_stop_reason=(
                                    "mature_target_reached" if mature_count() >= MIN_RELIABLE_HISTORY_ITEMS
                                    else "recent_window_complete"
                                ),
                                page_request_count=len(archive_refs),
                                command_hash=last_command_hash,
                                output_hash=last_output_hash,
                            ),
                        )
                    if page_has_more and next_cursor:
                        phase = "older_backfill"
                        record_progress(
                            phase=phase,
                            status="running",
                            next_cursor=cursor,
                            has_more=has_more,
                            reached_time_boundary=reached_time_boundary,
                            stop_reason=stop_reason,
                        )
                        continue
                    return (
                        self._build_paged_history_artifact(
                            registration=registration,
                            platform=platform,
                            items=list(items_by_id.values()),
                            archive_refs=archive_refs,
                            evaluated_at=evaluated_at,
                            collection_status="history_exhausted_insufficient",
                            history_exhausted=True,
                            collection_stop_reason="history_exhausted",
                            page_request_count=len(archive_refs),
                            command_hash=last_command_hash,
                            output_hash=last_output_hash,
                        ),
                    )
            elif mature_count() >= MIN_RELIABLE_HISTORY_ITEMS:
                return (
                    self._build_paged_history_artifact(
                        registration=registration,
                        platform=platform,
                        items=list(items_by_id.values()),
                        archive_refs=archive_refs,
                        evaluated_at=evaluated_at,
                        collection_status="target_reached",
                        history_exhausted=False,
                        collection_stop_reason="mature_target_reached",
                        page_request_count=len(archive_refs),
                        command_hash=last_command_hash,
                        output_hash=last_output_hash,
                    ),
                )

            record_progress(
                phase=phase,
                status="running",
                next_cursor=cursor,
                has_more=has_more,
                reached_time_boundary=reached_time_boundary,
                stop_reason=stop_reason,
            )

    def _build_paged_history_artifact(
        self,
        *,
        registration: dict[str, Any],
        platform: str,
        items: list[dict[str, Any]],
        archive_refs: list[str],
        evaluated_at: int,
        collection_status: str,
        history_exhausted: bool,
        collection_stop_reason: str,
        page_request_count: int,
        command_hash: str,
        output_hash: str,
    ) -> dict[str, Any]:
        artifact = build_historical_collection_artifact(
            platform=platform,
            account_source_ref=str(registration["external_account_ref"]),
            items=items,
            raw_archive_ref=archive_refs[-1] if archive_refs else "paged-history",
            command_hash=command_hash,
            output_hash=output_hash,
            evaluated_at=evaluated_at,
            collection_status=collection_status,
            history_exhausted=history_exhausted,
            collection_stop_reason=collection_stop_reason,
            page_request_count=page_request_count,
            raw_archive_refs=archive_refs,
        )
        self.core.record_competitor_historical_page_progress(
            registration_id=str(registration["registration_id"]),
            phase="older_backfill" if collection_status == "history_exhausted_insufficient" else "recent_window",
            status=collection_status if collection_status == "history_exhausted_insufficient" else "completed",
            evaluated_at=evaluated_at,
            next_cursor="",
            has_more=False,
            reached_time_boundary=False,
            stop_reason=collection_stop_reason,
            page_request_count=page_request_count,
            raw_archive_refs=archive_refs,
            mature_item_count=sum(
                0 < _published_at_for_collection(item)
                <= evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
                for item in items
            ),
            recent_item_count=sum(
                _published_at_for_collection(item)
                >= evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
                for item in items
            ),
        )
        return artifact

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
                self._notify_material_change("material_prepared", source_id)
                completed_count += 1
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
                self._notify_material_change("material_preparation_failed", source_id)
                if "account blocked" in str(exc).lower():
                    raise StateTransitionError("account blocked during competitor material preparation") from exc
        if collection_problems:
            raise StateTransitionError(
                f"detail collection did not finish for {len(collection_problems)} recovery item(s); they remain excluded and were not retried automatically"
            )
        if failures:
            raise StateTransitionError(f"competitor material preparation failed for {len(failures)} item(s); failures remain recorded and are not automatically retried")
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
                self._notify_material_change("material_prepared", source_id)
                completed_count += 1
            except Exception as exc:
                failures.append(source_id)
                self.core.record_replenished_competitor_material_item(
                    registration_id=registration_id, item_ref=source_id,
                    status="failed", artifact=None, error={"reason": str(exc)},
                )
                self._notify_material_change("material_preparation_failed", source_id)
        if missing_details:
            raise StateTransitionError(
                f"saved detail is still missing for {len(missing_details)} recovery item(s); they remain excluded"
            )
        if failures:
            raise StateTransitionError(
                f"collected-detail material preparation failed for {len(failures)} item(s); failures remain recorded and are not automatically retried"
            )
        return tuple(
            item["artifact"] for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        )

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

    @staticmethod
    def _validate_core_breakdown_artifact(*, artifact: dict[str, Any], source_id: str) -> None:
        """Validate only the immutable core breakdown contract.

        Optional expansion/question fields are deliberately excluded here.  A
        complete source-bound analysis must have its identity, source content
        type, analysis text and supported output schema; optional enhancement
        generation is recorded separately after the core item is persisted.
        """
        if not isinstance(artifact, dict):
            raise StateTransitionError("competitor breakdown core result must be an object")
        if str(artifact.get("source_id") or "").strip() != source_id:
            raise StateTransitionError("competitor breakdown core result has the wrong source identity")
        if not str(artifact.get("source_content_type") or "").strip():
            raise StateTransitionError("competitor breakdown core result lacks source content type")
        if not str(artifact.get("analysis_text") or "").strip():
            raise StateTransitionError("competitor breakdown core result lacks analysis text")
        if str(artifact.get("schema_version") or "") not in {
            "competitor_breakdown.output.raw.v4",
            "competitor_breakdown.output.raw.v5",
        }:
            raise StateTransitionError("competitor breakdown core result has an unsupported schema version")

    def _generate_prepared_breakdown_artifact(
        self,
        *,
        registration: dict[str, Any],
        material: dict[str, Any],
        attempt_kind: str,
    ) -> dict[str, Any]:
        source_id = str(material.get("source_id") or "")
        transcript_text = Path(str(material["transcript_ref"])).read_text(encoding="utf-8")
        if self.gateway is None:
            content_type_lifecycle = (
                "discover" if str(registration.get("status") or "").strip() == "processing" else "classify"
            )
            input_payload = {
                "correlation_id": f"{registration['registration_id']}:breakdown:{source_id}",
                "source_id": source_id,
                "transcript": transcript_text,
                "metrics": dict(material["metrics"]),
                "comments": list(material["comments"]),
                "domain_label": str(registration.get("domain_label") or "generic"),
                "domain_context": _breakdown_domain_context(
                    str(registration.get("domain_label") or "generic"),
                    observed_content_types=self.core.observed_breakdown_content_types(
                        domain_label=str(registration.get("domain_label") or "generic")
                    ),
                    content_type_lifecycle=content_type_lifecycle,
                ),
                "schema_version": "competitor_breakdown.input.v1",
            }
            task, _ = prepare_external_skill_task(
                FormalSkillContract.from_runtime_skill("competitor_breakdown"),
                input_payload,
                constraints={
                    "use_only_supplied_material": True,
                    "preserve_source_identity": True,
                    "cannot_change_business_state": True,
                    "do_not_search": True,
                    "no_fuzzy_evidence_matching": True,
                },
                business_context={
                    "registration_id": str(registration["registration_id"]),
                    "source_id": source_id,
                    "origin": "cold_start_intelligent_judgment" if registration.get("cold_start_id") else "competitor_breakdown",
                    "data_identity": self.core.data_identity,
                },
            )
            if self.external_executor is None:
                raise ExternalIntelligenceRequired(task)
            submission = self.external_executor(task)
            external_output = submission.get("output") if isinstance(submission, dict) else None
            model_run_id = self.core.record_external_competitor_execution(
                registration_id=str(registration["registration_id"]),
                source_id=source_id,
                execution_id=str(submission.get("execution_id") or "") if isinstance(submission, dict) else "",
                executor_id=str(submission.get("executor_id") or "") if isinstance(submission, dict) else "",
                model_ref=str(submission.get("model_ref") or "") if isinstance(submission, dict) else None,
                submitted_at=str(submission.get("submitted_at") or "") if isinstance(submission, dict) else None,
                input_payload=input_payload,
                output_payload=external_output,
            )
            try:
                artifact = validate_external_skill_output(
                    FormalSkillContract.from_runtime_skill("competitor_breakdown"),
                    input_payload,
                    external_output,
                )
                self._validate_core_breakdown_artifact(artifact=artifact, source_id=source_id)
            except Exception as exc:
                self.core.conn.execute(
                    "UPDATE stage0_competitor_registration_model_run SET error_json=? WHERE registration_model_run_id=?",
                    (json.dumps({"validation_error": str(exc)}, ensure_ascii=False), model_run_id),
                )
                raise
            return {
                "artifact_kind": "deep_breakdown",
                "source_id": source_id,
                "model_run_id": model_run_id,
                "raw_model_output": json.dumps(artifact, ensure_ascii=False, sort_keys=True),
                "deep_breakdown": artifact,
            }
        raise StateTransitionError(
            "formal competitor breakdown has no direct model execution path"
        )

    def prepare_breakdown_replacement_candidate(
        self,
        *,
        registration: dict[str, Any],
        material: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate and validate one replacement without touching its current breakdown."""
        source_id = str(material.get("source_id") or "")
        if not source_id:
            raise StateTransitionError("prepared competitor material has no source identity")
        self._report_progress(
            "hit_breakdown",
            f"正在生成第 {source_id} 条替换拆解；当前旧结果保持可用",
        )
        try:
            return self._generate_prepared_breakdown_artifact(
                registration=registration,
                material=material,
                attempt_kind="initial",
            )
        except ExternalIntelligenceRequired:
            raise
        except Exception as exc:
            error = self._breakdown_failure_record(exc)
            # Replacement candidates do not pass through the normal item
            # writer, so persist the source-bound attempt here before the
            # backlog runner turns it into a compact task summary.  The old
            # breakdown remains untouched; this record is only the diagnostic
            # for the rejected candidate.
            try:
                self.core.record_competitor_breakdown_attempt(
                    registration_id=str(registration["registration_id"]),
                    source_id=source_id,
                    attempt_kind="initial",
                    outcome="failed",
                    reason=str(error.get("reason") or exc),
                    raw_model_output=error.get("raw_model_output"),
                    raw_model_output_status=str(
                        error.get("raw_model_output_status") or "not_available"
                    ),
                    model_run_id=error.get("model_run_envelope_version_id"),
                )
            except Exception as record_exc:
                error["attempt_record_error"] = str(record_exc)
            raise CompetitorBreakdownFailed(
                f"replacement competitor breakdown failed for {source_id}",
                failure_record=error,
            ) from exc

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
            artifact = self._generate_prepared_breakdown_artifact(
                registration=registration,
                material=material,
                attempt_kind=attempt_kind,
            )
            model_run_id = str(artifact["model_run_id"])
            raw_model_output = str(artifact["raw_model_output"])
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
            optional_attachment: dict[str, Any]
            try:
                expansion_result = self.core.register_breakdown_question_expansions(
                    domain_label=str(registration.get("domain_label") or "generic"),
                    breakdown=dict(artifact.get("deep_breakdown") or {}),
                    parent_source_ref={
                        "source_type": "competitor_breakdown",
                        "source_object_id": source_id,
                        "registration_id": registration_id,
                        "source_object_version": str(artifact.get("model_run_id") or ""),
                    },
                    actor="competitor_breakdown",
                    content_type_lifecycle=(
                        "discover"
                        if str(registration.get("status") or "").strip() == "processing"
                        else "classify"
                    ),
                )
                optional_attachment = {
                    "status": "completed",
                    "result": expansion_result,
                }
            except Exception as attachment_exc:
                # An expansion/observation problem is an optional attachment
                # failure.  The already persisted core breakdown remains valid.
                optional_attachment = {
                    "status": "failed",
                    "error_type": type(attachment_exc).__name__,
                    "reason": str(attachment_exc),
                }
            try:
                self.core.record_competitor_breakdown_optional_result(
                    registration_id=registration_id,
                    item_ref=source_id,
                    optional_result=optional_attachment,
                )
            except Exception as optional_record_exc:
                # Never turn a persisted core success into a failed breakdown
                # because the optional attachment receipt could not be updated.
                try:
                    self.core._audit(None, "competitor_breakdown_optional_result_persistence_failed", {
                        "registration_id": registration_id,
                        "item_ref": source_id,
                        "error_type": type(optional_record_exc).__name__,
                        "reason": str(optional_record_exc),
                        "optional_result": optional_attachment,
                    })
                except Exception:
                    pass
            self._notify_material_change("breakdown_completed", source_id)
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
            self._notify_material_change("breakdown_failed", source_id)
            raise CompetitorBreakdownFailed(
                f"atomic competitor breakdown failed for {source_id}", failure_record=error,
            ) from exc

    def _breakdown(
        self, registration: dict[str, Any], completed: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        materials = _step_artifacts(completed, "transcripts_and_comments")
        materials = [item for item in materials if item.get("artifact_kind") == "transcript_and_comments"]
        registration_id = str(registration["registration_id"])
        recorded = {
            item["item_ref"]: item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            )
        }
        existing = {source_id: item for source_id, item in recorded.items() if item["status"] == "completed"}
        excluded_ids = {
            source_id for source_id, item in recorded.items() if item["status"] == "excluded"
        }
        delivery_retry_queue: list[dict[str, Any]] = []
        completed_count = len(existing)
        total_count = len(materials)
        pending_materials = [
            material for material in materials
            if str(material.get("source_id") or "") not in existing
            and str(material.get("source_id") or "") not in excluded_ids
        ]
        pending_count = len(pending_materials)
        initial_completed_count = completed_count
        pending_source_ids = {
            str(material.get("source_id") or "") for material in pending_materials
        }
        if pending_count:
            self._report_progress(
                "hit_breakdown",
                f"总{total_count}条，已完成{completed_count}条，本次处理{pending_count}条",
            )
        for position, material in enumerate(materials, start=1):
            source_id = str(material.get("source_id") or "")
            if source_id in existing:
                continue
            if source_id in recorded and recorded[source_id]["status"] == "excluded":
                continue
            try:
                self.process_prepared_breakdown(registration=registration, material=material)
                completed_count += 1
            except CompetitorBreakdownFailed as exc:
                if self._is_delivery_interruption(exc.failure_record):
                    delivery_retry_queue.append(material)
                    continue
                continue
            except ExternalIntelligenceRequired:
                raise
            except Exception as exc:
                error = self._breakdown_failure_record(exc)
                self._record_atomic_breakdown_failure(
                    registration=registration, source_id=source_id, error=error,
                )
                continue
        for material in delivery_retry_queue:
            try:
                self.process_prepared_breakdown(
                    registration=registration,
                    material=material,
                    attempt_kind="post_batch_delivery_retry",
                )
                completed_count += 1
            except CompetitorBreakdownFailed:
                pass
        failed_records = [
            item for item in self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            ) if item["status"] == "failed"
        ]
        if pending_count:
            current_items = self.core.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            )
            completed_now = sum(item["status"] == "completed" for item in current_items)
            failed_now = sum(
                item["status"] == "failed" and item["item_ref"] in pending_source_ids
                for item in current_items
            )
            newly_completed = max(completed_now - initial_completed_count, 0)
            if failed_now:
                self._report_progress(
                    "hit_breakdown",
                    f"本次{pending_count}条，成功{newly_completed}条，失败{failed_now}条，累计完成{completed_now}/{total_count}",
                )
            else:
                self._report_progress(
                    "hit_breakdown",
                    f"本次完成{newly_completed}条，累计{completed_now}/{total_count}",
                )
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
    stream_breakdowns: bool = False,
    external_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_material_change: Callable[[str], None] | None = None,
) -> ConfiguredCompetitorRegistrationExecutor:
    """Bind collection/material preparation to the external intelligence boundary."""
    del stream_breakdowns
    archive_root = require_runtime_path(Path(
        os.environ.get("COMPETITOR_REGISTRATION_ARCHIVE_ROOT")
        or runtime_path(
            "competitor_registration_runtime", data_identity=core.data_identity
        )
    ), purpose="competitor registration archive", data_identity=core.data_identity)
    def runtime_progress(detail: str) -> None:
        if progress_callback is not None:
            progress_callback("external_runtime", detail or "外部任务仍在运行")

    crawler_executor = LocalMediaCrawlerExecutor(
        archive_root=archive_root / "mediacrawler",
        progress_callback=runtime_progress,
        data_identity=core.data_identity,
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
        gateway=None,
        external_executor=external_executor,
        max_historical_items=configured_first_registration_item_limit(),
        progress_callback=progress_callback,
        on_material_change=on_material_change,
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
            elif str(current["status"]) == "failed":
                # A failed item is resumable work, not a terminal skip.
                pending.append(item)
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
        task_summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
        replacement_mode = str(task_summary.get("mode") or "") == "replace_active_mixed"

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
        if replacement_mode:
            candidate_runner = getattr(self.executor, "prepare_breakdown_replacement_candidate", None)
            if not callable(candidate_runner):
                raise StateTransitionError("configured competitor executor cannot prepare safe replacements")
            for position, item in enumerate(selected, start=1):
                progress = self.core.competitor_breakdown_backlog_task_progress(
                    backlog_task_id=backlog_task_id
                )
                source_id = str(item["source_id"])
                summary = progress["summary"]
                if source_id in set(summary.get("failed_source_ids") or []):
                    report(phase="replacement", position=position)
                    continue
                if int(summary.get("completed") or 0) + int(summary.get("failed") or 0) >= position:
                    report(phase="replacement", position=position)
                    continue
                registration = self.core.get_competitor_registration(
                    registration_id=str(item["registration_id"])
                )
                candidate_artifact: dict[str, Any] | None = None
                try:
                    candidate_artifact = candidate_runner(
                        registration=registration,
                        material=dict(item["material"]),
                    )
                    self.core.replace_completed_mixed_competitor_breakdown(
                        registration_id=str(item["registration_id"]),
                        source_id=source_id,
                        artifact=candidate_artifact,
                    )
                except (CompetitorBreakdownFailed, StateTransitionError) as exc:
                    # A candidate can be valid but still fail at the atomic
                    # swap boundary.  Preserve that outcome against the
                    # source as well, so the model response is not orphaned
                    # from the reason the formal replacement was rejected.
                    if (
                        not isinstance(exc, CompetitorBreakdownFailed)
                        and isinstance(candidate_artifact, dict)
                    ):
                        try:
                            self.core.record_competitor_breakdown_attempt(
                                registration_id=str(registration["registration_id"]),
                                source_id=source_id,
                                attempt_kind="initial",
                                outcome="failed",
                                reason=str(exc),
                                raw_model_output=candidate_artifact.get("raw_model_output"),
                                raw_model_output_status=(
                                    "available"
                                    if candidate_artifact.get("raw_model_output") is not None
                                    else "not_available"
                                ),
                                model_run_id=candidate_artifact.get("model_run_id"),
                            )
                        except Exception:
                            # The task summary still records the swap failure;
                            # do not replace the actionable failure with a
                            # secondary diagnostic-write error.
                            pass
                    current = self.core.competitor_breakdown_backlog_task_progress(
                        backlog_task_id=backlog_task_id
                    )
                    failed_source_ids = set(current["summary"].get("failed_source_ids") or [])
                    failed_source_ids.add(source_id)
                    failure_reasons = dict(current["summary"].get("failure_reasons") or {})
                    failure_details = dict(current["summary"].get("failure_details") or {})
                    failure_record = getattr(exc, "failure_record", {})
                    if not isinstance(failure_record, dict):
                        failure_record = {}
                    if (
                        not isinstance(exc, CompetitorBreakdownFailed)
                        and isinstance(candidate_artifact, dict)
                    ):
                        failure_record = {
                            **failure_record,
                            "failure_stage": "replacement_commit",
                            "failure_type": type(exc).__name__,
                            "model_run_envelope_version_id": candidate_artifact.get("model_run_id"),
                            "raw_model_output_status": (
                                "available"
                                if candidate_artifact.get("raw_model_output") is not None
                                else "not_available"
                            ),
                        }
                    reason = str(failure_record.get("reason") or exc)
                    failure_reasons[source_id] = reason
                    failure_details[source_id] = {
                        "reason": reason,
                        "failure_stage": str(
                            failure_record.get("failure_stage") or "replacement"
                        ),
                        "failure_type": str(
                            failure_record.get("failure_type") or type(exc).__name__
                        ),
                        "model_run_id": failure_record.get("model_run_envelope_version_id"),
                        "raw_model_output_status": str(
                            failure_record.get("raw_model_output_status") or "not_available"
                        ),
                        "automatic_retry": bool(failure_record.get("automatic_retry")),
                    }
                    if failure_record.get("attempt_record_error"):
                        failure_details[source_id]["attempt_record_error"] = str(
                            failure_record["attempt_record_error"]
                        )
                    failed_summary = {
                        **current["summary"],
                        "failed_source_ids": sorted(failed_source_ids),
                        "failure_reasons": failure_reasons,
                        "failure_details": failure_details,
                        "failed": len(failed_source_ids),
                        "pending": len(selected) - int(current["summary"].get("completed") or 0) - len(failed_source_ids),
                    }
                    self.core.update_competitor_breakdown_backlog_task(
                        backlog_task_id=backlog_task_id,
                        status="running",
                        summary=failed_summary,
                    )
                report(phase="replacement", position=position)
            final = self.core.competitor_breakdown_backlog_task_progress(
                backlog_task_id=backlog_task_id
            )
            terminal_status = (
                "completed" if int(final["summary"]["failed"]) == 0 else "completed_with_failures"
            )
            final = self.core.update_competitor_breakdown_backlog_task(
                backlog_task_id=backlog_task_id,
                status=terminal_status,
                summary=final["summary"],
            )
            if progress_callback is not None:
                progress_callback({
                    "phase": terminal_status,
                    "position": len(selected),
                    "summary": final["summary"],
                })
            return final

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
                approval = task.get("approval") if isinstance(task.get("approval"), dict) else {}
                actor = str(approval.get("actor") or "").strip()
                if not actor:
                    raise StateTransitionError("formal breakdown backlog approval has no user identity")
                self.core.discard_failed_breakdown_for_replacement(
                    cold_start_id=str(task["cold_start_id"]),
                    registration_id=registration_id,
                    source_id=source_id,
                    actor=actor,
                    actor_kind="user",
                    reason="用户已明确授权重做当前失败的冷启动拆解",
                    idempotency_key=f"{backlog_task_id}:discard-failed:{registration_id}:{source_id}",
                )
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
        self,
        *,
        registration_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Run exactly one unfinished registration stage in the strict stage flow."""
        if not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("competitor registration execution requires an actor and idempotency key")
        registration = self.core.get_competitor_registration(registration_id=registration_id)
        if registration["status"] == "awaiting_human_review":
            pending = self.core.get_competitor_registration_history_shortfall(
                registration_id=registration_id
            )
            if pending is not None:
                return pending
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
                pending = self.core.get_competitor_registration_history_shortfall(
                    registration_id=registration_id
                )
                if pending is not None:
                    return pending
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
    def _run_registration_batch_by_stage(
        self,
        *,
        registration_ids: tuple[str, ...],
        actor: str,
        idempotency_key: str,
    ) -> list[dict[str, Any]]:
        """Run a batch through the shared stage order, never account-by-account."""
        step_index = {
            step_name: index
            for index, step_name in enumerate(COMPETITOR_REGISTRATION_STEPS)
        }
        result_by_id: dict[str, dict[str, Any]] = {}

        for step_name in COMPETITOR_REGISTRATION_STEPS:
            expected_index = step_index[step_name]
            for registration_id in registration_ids:
                registration = self.core.get_competitor_registration(
                    registration_id=registration_id
                )
                if registration["status"] == "completed":
                    result_by_id[registration_id] = {
                        "registration_id": registration_id,
                        "status": "completed",
                    }
                    continue
                if registration["status"] == "failed":
                    raise StateTransitionError(
                        f"incremental registration {registration_id} is already failed"
                    )
                if registration["status"] == "awaiting_human_review":
                    transition = self.run_competitor_registration_step(
                        registration_id=registration_id,
                        actor=actor,
                        idempotency_key=f"{idempotency_key}:{registration_id}:complete:{step_name}",
                    )
                    result_by_id[registration_id] = transition
                    continue
                if registration["status"] != "processing":
                    raise StateTransitionError(
                        "incremental competitor registration is not ready for staged execution"
                    )
                current_step = str(registration["current_step"])
                current_index = step_index.get(current_step)
                if current_index is None:
                    raise StateTransitionError(
                        f"incremental registration {registration_id} has unsupported step {current_step}"
                    )
                if current_index > expected_index:
                    continue
                if current_index < expected_index:
                    raise StateTransitionError(
                        f"incremental registration {registration_id} has unfinished prior step {current_step}"
                    )
                result_by_id[registration_id] = self.run_competitor_registration_step(
                    registration_id=registration_id,
                    actor=actor,
                    idempotency_key=f"{idempotency_key}:{registration_id}:{step_name}",
                )

        for registration_id in registration_ids:
            registration = self.core.get_competitor_registration(
                registration_id=registration_id
            )
            if registration["status"] == "awaiting_human_review":
                result_by_id[registration_id] = self.run_competitor_registration_step(
                    registration_id=registration_id,
                    actor=actor,
                    idempotency_key=f"{idempotency_key}:{registration_id}:complete:final",
                )
            elif registration["status"] == "failed":
                raise StateTransitionError(
                    f"incremental registration {registration_id} failed during staged execution"
                )
            result_by_id.setdefault(
                registration_id,
                {"registration_id": registration_id, "status": registration["status"]},
            )
        results: list[dict[str, Any]] = []
        for registration_id in registration_ids:
            registration = self.core.get_competitor_registration(
                registration_id=registration_id
            )
            result = dict(result_by_id[registration_id])
            result.update({
                "registration_id": registration_id,
                "status": registration["status"],
                "current_step": registration["current_step"],
            })
            results.append(result)
        return results

    def run_incremental_competitor_registrations(
        self,
        *,
        cold_start_id: str,
        competitor_accounts: tuple[dict[str, Any], ...],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Register and process only the supplied accounts on a completed run."""
        batch = self.core.create_incremental_competitor_registrations(
            cold_start_id=cold_start_id,
            competitor_accounts=competitor_accounts,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        registration_ids = tuple(str(value) for value in batch["registration_ids"])
        results = self._run_registration_batch_by_stage(
            registration_ids=registration_ids,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        result_status = (
            "completed"
            if all(str(item.get("status") or "") == "completed" for item in results)
            else "awaiting_human_review"
            if any(bool(item.get("history_insufficient")) for item in results)
            else "completed_with_failures"
        )
        return {**batch, "status": result_status, "results": results}


def _load_competitor_breakdown_external_material(
    core: Stage0ContentProductionCore,
    *,
    registration_id: str,
    source_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    registration = core.get_competitor_registration(registration_id=registration_id)
    prepared = next(
        (
            item
            for item in core.list_competitor_registration_items(
                registration_id=registration_id,
                step_name="transcripts_and_comments",
            )
            if str(item.get("item_ref") or "") == source_id
            and item.get("status") == "completed"
            and isinstance(item.get("artifact"), dict)
        ),
        None,
    )
    if prepared is None:
        raise StateTransitionError(
            "competitor breakdown external task requires completed spoken material"
        )
    return registration, dict(prepared["artifact"])


def _build_competitor_breakdown_external_task(
    core: Stage0ContentProductionCore,
    *,
    registration: dict[str, Any],
    material: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_id = str(material.get("source_id") or "").strip()
    if not source_id:
        raise StateTransitionError("prepared competitor material has no source identity")
    transcript_ref = str(material.get("transcript_ref") or "").strip()
    if not transcript_ref:
        raise StateTransitionError("prepared competitor material has no transcript reference")
    transcript_text = Path(transcript_ref).read_text(encoding="utf-8")
    domain_label = str(registration.get("domain_label") or "generic")
    content_type_lifecycle = (
        "discover"
        if str(registration.get("status") or "").strip() == "processing"
        else "classify"
    )
    input_payload = {
        "correlation_id": f"{registration['registration_id']}:breakdown:{source_id}",
        "source_id": source_id,
        "transcript": transcript_text,
        "metrics": dict(material.get("metrics") or {}),
        "comments": list(material.get("comments") or []),
        "domain_label": domain_label,
        "domain_context": _breakdown_domain_context(
            domain_label,
            observed_content_types=core.observed_breakdown_content_types(
                domain_label=domain_label
            ),
            content_type_lifecycle=content_type_lifecycle,
        ),
        "schema_version": "competitor_breakdown.input.v1",
    }
    task, _ = prepare_external_skill_task(
        FormalSkillContract.from_runtime_skill("competitor_breakdown"),
        input_payload,
        constraints={
            "use_only_supplied_material": True,
            "preserve_source_identity": True,
            "cannot_change_business_state": True,
            "do_not_search": True,
            "no_fuzzy_evidence_matching": True,
        },
        business_context={
            "registration_id": str(registration["registration_id"]),
            "source_id": source_id,
            "origin": (
                "cold_start_intelligent_judgment"
                if registration.get("cold_start_id")
                else "competitor_breakdown"
            ),
            "data_identity": core.data_identity,
        },
    )
    task["task_identity"] = {
        "registration_id": str(registration["registration_id"]),
        "source_id": source_id,
    }
    return task, input_payload


def prepare_competitor_breakdown_external_task(
    core: Stage0ContentProductionCore,
    *,
    registration_id: str,
    source_id: str,
) -> dict[str, Any]:
    """Return one existing competitor breakdown task without executing it."""
    registration, material = _load_competitor_breakdown_external_material(
        core,
        registration_id=registration_id,
        source_id=source_id,
    )
    task, _ = _build_competitor_breakdown_external_task(
        core,
        registration=registration,
        material=material,
    )
    return task


def submit_competitor_breakdown_external_result(
    core: Stage0ContentProductionCore,
    *,
    registration_id: str,
    source_id: str,
    execution_id: str,
    executor_id: str,
    model_ref: str | None,
    submitted_at: str | None,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Accept one competitor result using the existing item/attempt records."""
    registration, material = _load_competitor_breakdown_external_material(
        core,
        registration_id=registration_id,
        source_id=source_id,
    )
    _, input_payload = _build_competitor_breakdown_external_task(
        core,
        registration=registration,
        material=material,
    )
    if not isinstance(output, dict):
        raise StateTransitionError("competitor breakdown external result must be structured fields")
    model_run_id = core.record_external_competitor_execution(
        registration_id=registration_id,
        source_id=source_id,
        execution_id=execution_id,
        executor_id=executor_id,
        model_ref=model_ref,
        submitted_at=submitted_at,
        input_payload=input_payload,
        output_payload=output,
    )
    try:
        artifact = validate_external_skill_output(
            FormalSkillContract.from_runtime_skill("competitor_breakdown"),
            input_payload,
            output,
        )
        ConfiguredCompetitorRegistrationExecutor._validate_core_breakdown_artifact(
            artifact=artifact,
            source_id=source_id,
        )
    except Exception as exc:
        core.conn.execute(
            "UPDATE stage0_competitor_registration_model_run SET error_json=? "
            "WHERE registration_model_run_id=?",
            (
                json.dumps({"validation_error": str(exc)}, ensure_ascii=False),
                model_run_id,
            ),
        )
        raise
    stored_artifact = {
        "artifact_kind": "deep_breakdown",
        "source_id": source_id,
        "model_run_id": model_run_id,
        "raw_model_output": json.dumps(artifact, ensure_ascii=False, sort_keys=True),
        "deep_breakdown": artifact,
    }
    core.record_competitor_breakdown_attempt(
        registration_id=registration_id,
        source_id=source_id,
        attempt_kind="initial",
        outcome="completed",
        raw_model_output=stored_artifact["raw_model_output"],
        raw_model_output_status="available",
        model_run_id=model_run_id,
    )
    if registration["status"] == "processing":
        core.record_competitor_registration_item(
            registration_id=registration_id,
            step_name="breakdown",
            item_ref=source_id,
            status="completed",
            artifact=stored_artifact,
            error=None,
        )
    else:
        core.record_independent_competitor_breakdown(
            registration_id=registration_id,
            item_ref=source_id,
            status="completed",
            artifact=stored_artifact,
            error=None,
        )
    try:
        optional = core.register_breakdown_question_expansions(
            domain_label=str(registration.get("domain_label") or "generic"),
            breakdown=dict(artifact),
            parent_source_ref={
                "source_type": "competitor_breakdown",
                "source_object_id": source_id,
                "registration_id": registration_id,
                "source_object_version": model_run_id,
            },
            actor="competitor_breakdown",
            content_type_lifecycle=(
                "discover"
                if str(registration.get("status") or "").strip() == "processing"
                else "classify"
            ),
        )
        optional_result = {"status": "completed", "result": optional}
    except Exception as exc:
        optional_result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    try:
        core.record_competitor_breakdown_optional_result(
            registration_id=registration_id,
            item_ref=source_id,
            optional_result=optional_result,
        )
    except Exception:
        pass
    return {
        "registration_id": registration_id,
        "source_id": source_id,
        "status": "completed",
        "model_run_id": model_run_id,
        "artifact": stored_artifact,
    }
