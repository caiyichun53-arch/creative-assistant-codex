"""Stage 0's only formal write boundary for the first content-production chain.

This module deliberately implements state, version, audit, input-assembly, and
data-identity controls only.  It does not generate research or content.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    freeze_content_type_registry,
    formal_domain_labels,
    get_content_workflow_mode,
    get_domain_pack,
    get_discovery_policy,
    load_domain_packs,
    project_content_type,
)
from scripts.core.business_data.domain_boundaries import (
    evaluate_production_boundary,
    freeze_production_boundary_registry,
    get_production_boundary_registry,
    require_frozen_production_boundary,
)
from scripts.core.business_data.run_domain_search import TAG_CANDIDATE_LIKE_FLOOR
from scripts.core.model_gateway.configured_provider import build_configured_model_provider
from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoute,
    ModelRunEnvelope,
)
from scripts.core.model_gateway.model_router import (
    HermesTaskModelBinding,
    ModelRouter,
    ModelRouterError,
)
from scripts.core.runtime.liveness import budget_for
from scripts.core.production.high_signal_policy import (
    FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
    FIRST_REGISTRATION_MAX_ITEMS,
    HIGH_SIGNAL_POLICY_VERSION,
    HISTORICAL_MATURITY_DAYS,
    HISTORICAL_METRICS,
    MATURE_HISTORY_WINDOW_DAYS,
    MIN_RELIABLE_HISTORY_ITEMS,
    build_historical_collection_artifact,
    build_high_signal_artifact,
    derive_account_maturity_state,
    judge_against_formal_d_baseline,
    judge_against_mature_history,
    validate_historical_collection_artifact,
    validate_high_signal_artifact,
)
from scripts.core.production.business_runtime_guard import (
    enforce_atomic_skill_runtime_guard,
    enforce_runtime_startup_guard,
    record_runtime_guard_event,
)
from scripts.core.runtime.runtime_storage import (
    RuntimeStorageError,
    formal_database_path,
    runtime_root_for_identity,
)


ROOT = Path(__file__).resolve().parents[3]
FORMAL_DB_PATH = formal_database_path()
DataIdentity = Literal["production", "test", "fixture", "synthetic", "replay", "mock"]
NON_PRODUCTION_IDENTITIES = frozenset({"test", "fixture", "synthetic", "replay", "mock"})
DISCOVERY_EXECUTION_MODES = frozenset({"test_isolated", "real_daily_validation", "production_daily"})
DISCOVERY_RUN_OUTCOMES = frozenset(
    {"processing", "completed", "completed_with_failures", "timed_out", "interrupted", "failed", "cancelled"}
)
EXTERNAL_INTELLIGENCE_EXECUTION_VERSION = "external_intelligence.v1"
MANUAL_SOURCE_KINDS = frozenset({"direction", "link", "person", "work", "playlist"})
MANUAL_SOURCE_TOPIC_ROUTES = {
    "direction": "user_unclear_input",
    "link": "user_unclear_input",
    "person": "person_exploration",
    "work": "single_object_exploration",
    "playlist": "object_collection_exploration",
}
EXPLORATION_KIND_TO_TOPIC_ROUTE = {
    "person_exploration": "person_exploration",
    "work_exploration": "single_object_exploration",
    "playlist_exploration": "object_collection_exploration",
}
MANUAL_SOURCE_TARGET_KINDS = frozenset(
    {"saved_user_direction", "candidate", "formal_topic", "person_exploration", "work_exploration", "playlist_exploration"}
)
CONTENT_ACCOUNT_ROLES = frozenset({"owned", "competitor"})
COLD_START_CONTRACT_VERSION = "cold_start_guard_v14"
COLD_START_COMPETITOR_MIN = 20
COLD_START_COMPETITOR_MAX = 20
COMPETITOR_REGISTRATION_STEPS = (
    "historical_material",
    "high_signal_identification",
    "transcripts_and_comments",
    "breakdown",
)
COMPETITOR_TRACKING_ACTIVATION_STEPS = (
    "historical_material",
    "high_signal_identification",
    "transcripts_and_comments",
    "breakdown",
)
CANDIDATE_SCORE_WEIGHTS = {
    "demand_strength": 22,
    "information_increment": 22,
    "expression_pull": 18,
    "distinct_angle": 16,
    "question_focus": 12,
    "timeliness": 10,
}
DAILY_PRIORITY_REPORT_LIMIT = 10
EXPERIENCE_CANDIDATE_BATCH_SIZE = 8
EXPERIENCE_CANDIDATE_MIN_SOURCES = 3
_SOURCE_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")
_TAG_EDGE_PUNCTUATION = "，,。.！!?？:：;；、|/\\()（）[]【】<>《》“”'\"`~·…"

CHAIN_NODES = (
    "formal_topic",
    "research_plan",
    "research_plan_confirmation",
    "deep_research",
    "research_result_confirmation",
    "content_plan",
    "content_plan_confirmation",
    "formal_draft",
    "initial_draft_confirmation",
    "copy_optimization",
    "de_ai_revision",
    "review",
    "user_final_confirmation",
)
ARTIFACT_NODES = frozenset(
    {
        "formal_topic",
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
    }
)
UPSTREAM_NODE = {
    "research_plan": "formal_topic",
    "deep_research": "research_plan",
    "content_plan": "deep_research",
    "formal_draft": "content_plan",
    "copy_optimization": "formal_draft",
    "de_ai_revision": "copy_optimization",
    "review": "de_ai_revision",
}
CONFIRMATION_NODE = {
    "research_plan": "research_plan_confirmation",
    "deep_research": "research_result_confirmation",
    "content_plan": "content_plan_confirmation",
    "formal_draft": "initial_draft_confirmation",
    "review": "user_final_confirmation",
}
NEXT_ARTIFACT_NODE = {
    "formal_topic": "research_plan",
    "research_plan": "deep_research",
    "deep_research": "content_plan",
    "content_plan": "formal_draft",
    "formal_draft": "copy_optimization",
    "copy_optimization": "de_ai_revision",
    "de_ai_revision": "review",
}
MODEL_BINDINGS = {
    "research_plan": ("research_planning", "external_research_planning"),
    "deep_research": ("business_analysis", "material_summary"),
    "content_plan": ("business_analysis", "business_planning"),
    "formal_draft": ("writing_generation", "rough_draft"),
    "copy_optimization": ("writing_generation", "polishing"),
    "de_ai_revision": ("writing_generation", "de_ai_style"),
    "review": ("writing_generation", "final_copy_review"),
}
FORMAL_SKILL_BY_NODE = {
    "research_plan": "research_plan",
    "deep_research": "content_deep_research",
    "content_plan": "content_plan_generation",
    "formal_draft": "formal_draft_generate",
    "copy_optimization": "copy_optimization",
    "de_ai_revision": "de_ai_revision",
    "review": "final_content_review",
}


class Stage0CoreError(RuntimeError):
    pass


class DataIdentityError(Stage0CoreError):
    pass


class StateTransitionError(Stage0CoreError):
    pass


class StaleResultError(Stage0CoreError):
    pass


class ModelGatewayRequiredError(Stage0CoreError):
    pass


class ModelBindingUnavailableError(Stage0CoreError):
    pass


class LegacyProductionEntryDisabledError(Stage0CoreError):
    pass


@dataclass(frozen=True)
class InputAssembly:
    task_id: str
    node: str
    upstream_version_id: str | None
    user_requirements: str
    material_refs: tuple[dict[str, Any], ...]
    research_refs: tuple[dict[str, Any], ...]
    content_plan_ref: dict[str, Any] | None
    considered_experience: tuple[dict[str, Any], ...]
    adopted_experience: tuple[dict[str, Any], ...]
    rejected_experience: tuple[dict[str, Any], ...]
    omitted_materials: tuple[dict[str, Any], ...]
    prompt_version: str
    skill_version: str
    model_config_version: str

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "node": self.node,
            "upstream_version_id": self.upstream_version_id,
            "user_requirements": self.user_requirements,
            "material_refs": list(self.material_refs),
            "research_refs": list(self.research_refs),
            "content_plan_ref": self.content_plan_ref,
            "considered_experience": list(self.considered_experience),
            "adopted_experience": list(self.adopted_experience),
            "rejected_experience": list(self.rejected_experience),
            "omitted_materials": list(self.omitted_materials),
            "prompt_version": self.prompt_version,
            "skill_version": self.skill_version,
            "model_config_version": self.model_config_version,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decode_json_object(output_text: str) -> dict[str, Any]:
    text = str(output_text or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip() == fence:
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
        last_error = ValueError("model boundary output must be an object")
    raise StateTransitionError(f"model boundary output is not valid JSON: {last_error or 'empty output'}")


_TOPIC_IDENTITY_STOPWORDS = frozenset({
    "为什么", "为何", "怎么", "如何", "怎样", "是否", "能否", "可以", "应该", "到底",
    "什么", "哪些", "哪个", "这个", "那个", "一个", "现在", "关于", "对于", "的", "了",
    "吗", "呢", "啊", "会不会", "有没有",
})


def _topic_identity_tokens(value: str) -> set[str]:
    """Build a small deterministic identity for merging rephrased questions.

    This is deliberately a merge aid, not a new ranking or quality gate. It
    removes question-function words and compares stable Chinese character
    pairs, so equivalent wording can share one candidate card while different
    angles remain separate.
    """
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    for stopword in sorted(_TOPIC_IDENTITY_STOPWORDS, key=len, reverse=True):
        normalized = normalized.replace(stopword, " ")
    tokens: set[str] = set(re.findall(r"[a-z0-9]+", normalized))
    for chunk in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return tokens


def _topic_identity_matches(left: str, right: str) -> bool:
    if unicodedata.normalize("NFKC", str(left or "")).strip().casefold() == unicodedata.normalize(
        "NFKC", str(right or "")
    ).strip().casefold():
        return True
    left_tokens, right_tokens = _topic_identity_tokens(left), _topic_identity_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    if overlap < 2:
        return False
    similarity = overlap / min(len(left_tokens), len(right_tokens))
    return (overlap >= 3 and similarity >= 0.5) or similarity >= 0.7


def canonicalize_competitor_content_type(value: str) -> str:
    """Keep one dominant content engine for retrieval without making a taxonomy."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return "其他内容"
    compact = re.sub(r"[\s/、+&|]+", "", text)
    if compact in {
        "人物故事", "人物盘点", "人物观点评论", "人物解读", "人物经历",
        "作品故事", "作品盘点", "作品背景说明", "作品观点评论", "作品解读",
        "事件背景说明", "事件观点评论", "事件解读", "概念盘点", "概念观点评论",
        "概念解读", "案例故事", "案例背景说明", "案例观点评论", "案例解读",
        "合集故事", "合集盘点", "合集经历", "合集观点评论", "合集解读",
    }:
        return compact
    if "盘点" in compact or "歌单" in compact or "清单" in compact:
        if any(token in compact for token in ("人物", "歌手", "音乐人", "组合")):
            return "人物盘点"
        return "作品盘点"
    if any(token in compact for token in ("故事", "经历")):
        if any(token in compact for token in ("人物", "歌手", "音乐人", "组合")):
            return "人物故事"
        if any(token in compact for token in ("作品", "歌曲", "专辑")):
            return "作品故事"
        if "事件" in compact:
            return "事件解读"
    if "背景" in compact:
        if any(token in compact for token in ("人物", "歌手", "音乐人", "组合")):
            return "人物解读"
        if any(token in compact for token in ("作品", "歌曲", "专辑")):
            return "作品背景说明"
        if "事件" in compact:
            return "事件背景说明"
    if any(token in compact for token in ("观点", "评论")):
        if any(token in compact for token in ("人物", "歌手", "音乐人", "组合")):
            return "人物观点评论"
        if any(token in compact for token in ("作品", "歌曲", "专辑")):
            return "作品观点评论"
        if "事件" in compact:
            return "事件观点评论"
        return "概念观点评论"
    if any(token in compact for token in ("解读", "解释", "机制")):
        if any(token in compact for token in ("人物", "歌手", "音乐人", "组合")):
            return "人物解读"
        if any(token in compact for token in ("作品", "歌曲", "专辑")):
            return "作品解读"
        if "事件" in compact:
            return "事件解读"
        return "概念解读"
    return "其他内容"


_CONTENT_TYPE_SUBJECT_LABELS = {
    "person": "人物",
    "work": "作品",
    "event": "事件",
    "concept": "概念",
    "case": "案例",
    "method": "方法",
    "collection": "合集",
}
_CONTENT_TYPE_EXPRESSION_LABELS = {
    "story": "故事",
    "profile": "经历",
    "list": "盘点",
    "analysis": "解读",
    "explanation": "背景说明",
    "commentary": "观点评论",
    "event_response": "事件回应",
    "interview": "访谈",
}


def _content_type_group_key(raw_type: str) -> tuple[str, str]:
    """Return a deterministic semantic group without inventing a type."""
    raw = unicodedata.normalize("NFKC", str(raw_type or "")).strip()
    canonical = canonicalize_competitor_content_type(raw)
    # ``canonicalize_competitor_content_type`` remains a legacy retrieval
    # helper.  The cold-start proposal step may make only this explicit,
    # evidence-backed equivalence: an observed person profile is the same
    # dominant story engine as a person story.  No other unseen grouping is
    # inferred here.
    if canonical == "人物经历":
        canonical = "人物故事"
    if canonical != "其他内容":
        return ("canonical", canonical.casefold())
    compact = re.sub(r"\s+", "", raw).casefold()
    return ("raw", compact or "其他内容")


def _content_type_candidate_id(domain_label: str, group_key: tuple[str, str]) -> str:
    return "content_type_" + hashlib.sha256(
        _canonical({"domain_label": domain_label, "group_key": list(group_key)}).encode("utf-8")
    ).hexdigest()[:20]


def _content_type_candidate_canonical_id(domain_label: str, group_key: tuple[str, str]) -> str:
    return "ct_" + hashlib.sha256(
        _canonical({"domain_label": domain_label, "group_key": list(group_key)}).encode("utf-8")
    ).hexdigest()[:16]


def _content_type_candidate_definition(
    *,
    display_name: str,
    observations: list[dict[str, Any]],
) -> dict[str, str]:
    subjects = sorted(
        {
            _CONTENT_TYPE_SUBJECT_LABELS.get(
                str(item.get("content_subject_type") or "").strip(),
                str(item.get("content_subject_type") or "").strip(),
            )
            for item in observations
            if str(item.get("content_subject_type") or "").strip()
            not in {"", "mixed", "unclear"}
        }
    )
    expressions = sorted(
        {
            _CONTENT_TYPE_EXPRESSION_LABELS.get(
                str(item.get("expression_form") or "").strip(),
                str(item.get("expression_form") or "").strip(),
            )
            for item in observations
            if str(item.get("expression_form") or "").strip()
            not in {"", "mixed", "unclear"}
        }
    )
    subject_text = "、".join(subjects) or "当前样本观察到的内容对象"
    expression_text = "、".join(expressions) or "当前样本观察到的表达方式"
    definition = f"主要围绕{subject_text}，采用{expression_text}组织一篇完整内容。"
    return {
        "definition": definition,
        "content_expression": f"内容对象为{subject_text}；组织方式为{expression_text}。",
        "distinction": f"与其它候选的区别：本候选主要由{subject_text}和{expression_text}共同决定，不因单个关键词相似自动归入。",
        "core_subject": subject_text,
        "content_promise": definition,
        "required_delivery": f"明确呈现{subject_text}，并按{expression_text}形成可独立完成的内容推进。",
        "scope_boundary": f"只覆盖本次样本中已观察到的{subject_text}与{expression_text}组合；不能仅凭标签、人物名称或领域关联扩展到其它类型。",
    }


def _contains_build_root_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_build_root_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_build_root_path(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.replace("/", "\\").casefold()
    build_root = str(ROOT.resolve()).replace("/", "\\").casefold()
    return normalized == build_root or normalized.startswith(build_root + "\\")


def _hide_build_root_paths(value: Any, *, key: str = "") -> Any:
    """Hide stale build-root references when immutable history is read.

    Historical registration records remain unchanged for auditability, but a
    cleaned runtime must never expose their removed archive paths as usable
    inputs.
    """
    if isinstance(value, dict):
        return {name: _hide_build_root_paths(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [_hide_build_root_paths(item, key=key) for item in value]
    if not isinstance(value, str) or not _contains_build_root_path(value):
        return value
    lowered = key.casefold()
    if lowered.endswith("_ref") or lowered.endswith("_path") or lowered in {"path", "database", "backup_ref"}:
        return None
    return "build_path_removed"


def _experience_context_matches(
    *, context_text: str, applicable_when: list[str]
) -> list[str]:
    """Use only explicit wording overlap; never infer that an experience is relevant."""
    compact_context = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", context_text).casefold()
    if not compact_context:
        return []
    matches: list[str] = []
    for condition in applicable_when:
        compact_condition = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", condition).casefold()
        if len(compact_condition) < 4:
            continue
        # A four-character (or longer) literal slice keeps this deliberately
        # conservative for Chinese text without pretending to understand a
        # topic from broad domain labels alone.
        found = any(
            compact_condition[start : start + width] in compact_context
            for width in range(min(8, len(compact_condition)), 3, -1)
            for start in range(0, len(compact_condition) - width + 1)
        )
        if found:
            matches.append(condition)
    return matches


def _deterministic_named_entity_filter_reason(
    tag: str,
    *,
    topic_policy: dict[str, Any],
) -> str:
    """Reject person and other named-entity tags without a model call."""
    normalized = tag.strip().casefold()
    if not normalized:
        return ""
    allowed_concepts = {
        str(value).strip().casefold()
        for value in topic_policy.get("person_name_concept_allowlist", [])
        if str(value).strip()
    }
    if normalized in allowed_concepts:
        return ""
    named_entities = {
        str(value).strip().casefold()
        for value in topic_policy.get("named_entity_terms", [])
        if str(value).strip()
    }
    if any(entity in normalized for entity in named_entities):
        return "人物、组合或机构名称，不作为领域话题标签"
    work_titles = {
        str(value).strip().casefold()
        for value in topic_policy.get("work_title_terms", [])
        if str(value).strip()
    }
    if any(title == normalized for title in work_titles):
        return "作品名称，不作为领域话题标签"
    return ""


def _hotspot_cluster_key(title: str) -> str:
    normalized = "".join(ch for ch in title.casefold() if ch.isalnum())
    return normalized or _hash({"title": title})[:20]


def _hotspot_rank_value(row: sqlite3.Row) -> int:
    value = row["source_rank"]
    return int(value) if isinstance(value, int) or str(value).isdigit() else 9999


def _hotspot_time_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _build_hotspot_event_cluster_sources(
    rows: list[sqlite3.Row], *, per_source_limit: int | None
) -> list[dict[str, Any]]:
    clusters: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        title = str(row["title"] or "").strip()
        url = str(row["url"] or "").strip()
        if not title or not url:
            continue
        clusters.setdefault(_hotspot_cluster_key(title), []).append(row)

    queued: list[tuple[int, int, int, str, list[sqlite3.Row]]] = []
    for key, members in clusters.items():
        ranks = [_hotspot_rank_value(row) for row in members]
        platform_count = len({str(row["source_channel"]) for row in members})
        # Select review signals by their hotspot strength, never crawl time.
        queued.append((-platform_count, min(ranks), sum(ranks), key, members))
    queued.sort(key=lambda item: item[:4])

    result: list[dict[str, Any]] = []
    selected_clusters = queued if per_source_limit is None else queued[:per_source_limit]
    for negative_platform_count, best_rank, rank_sum, key, members in selected_clusters:
        representative = min(members, key=_hotspot_rank_value)
        observed_at = max(str(row["observed_at"]) for row in members)
        merged_sources = [
            {
                "observation_id": str(row["observation_id"]),
                "title": str(row["title"]),
                "url": str(row["url"]),
                "channel": str(row["source_channel"]),
                "rank": row["source_rank"],
                "observed_at": str(row["observed_at"]),
                "trendradar_record": json.loads(str(row["raw_json"])),
            }
            for row in sorted(members, key=_hotspot_rank_value)
        ]
        cluster_payload = {
            "cluster_id": f"hotspot_cluster_{_hash({'key': key})[:20]}",
            "representative_source": str(representative["title"]),
            "merged_sources": merged_sources,
            "dedupe_reason": "deterministic title-normalized global event cluster",
            "selection_signal": {
                "supporting_platform_count": -negative_platform_count,
                "best_original_rank": best_rank,
                "original_rank_sum": rank_sum,
                "rule": "more supporting platforms first; then better original ranks; then stable event id",
            },
        }
        source_content = "；".join(dict.fromkeys(item["title"] for item in merged_sources[:8]))
        representative_raw_hash = _hash(json.loads(str(representative["raw_json"])))
        result.append(
            {
                "source_type": "hotspot",
                "source_object_id": str(representative["observation_id"]),
                "source_object_version": str(representative["observed_at"]),
                "source_time": str(representative["observed_at"]),
                "payload": {
                    "source_id": cluster_payload["cluster_id"],
                    "title": str(representative["title"]),
                    "url": str(representative["url"]),
                    "account_name": "TrendRadar/event_cluster",
                    "source_time": observed_at,
                    "hotspot_event_cluster": cluster_payload,
                    "material_packet": {
                        "fact_summary": source_content,
                        "key_source_refs": [item["url"] for item in merged_sources[:8]],
                        "audience_relation": "是否与目标受众相关由一次热点机会判断决定",
                        "domain_fit": "热点不按标题词预先归入领域",
                        "possible_questions": [item["title"] for item in merged_sources[:5]],
                        "uncertainty": "cluster is still a discovery source; formal research evidence has not started",
                        "material_gaps": ["热点只提供发现材料；正式研究证据尚未开始。"],
                        "risks": [],
                        "stop_reason": "none",
                    },
                    "formal_source": {
                        "table": "trendradar_hotspot_observation",
                        "object_id": str(representative["observation_id"]),
                        "object_version": str(representative["observed_at"]),
                        "raw_metadata_hash": representative_raw_hash,
                    },
                },
            }
        )
    return result


def is_formal_database(path: Path | str) -> bool:
    return Path(path).resolve() == FORMAL_DB_PATH.resolve()


def reject_legacy_cli_production_write(path: Path | str, entrypoint: str) -> None:
    """Fail closed before a legacy CLI can open the formal Stage 0 database."""
    if is_formal_database(path):
        raise LegacyProductionEntryDisabledError(
            f"{entrypoint} is not a formal production entrypoint; use Stage0ContentProductionCore"
        )


def require_legacy_test_identity(data_identity: str) -> str:
    if data_identity not in NON_PRODUCTION_IDENTITIES:
        raise DataIdentityError("legacy CLI may only run with an explicit non-production data identity")
    return data_identity


class CoreModelRunMaterializer:
    """ModelGateway materializer that persists envelopes through Stage 0 Core."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        metadata = dict(envelope.metadata or {})
        binding = metadata.get("stage0_core")
        if not isinstance(binding, dict):
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 0 Core binding")
        return self._core._persist_gateway_envelope(envelope, binding)


class CoreDiscoveryModelRunMaterializer:
    """ModelGateway materializer for Stage 1B candidates; never creates a production task."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("stage1b_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 1B Core binding")
        return self._core.persist_discovery_model_envelope(envelope, binding)


class CoreExperienceCandidateModelRunMaterializer:
    """Persist one source-frozen experience-candidate model call without publishing experience."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("experience_candidate_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks experience-candidate Core binding")
        return self._core._persist_experience_candidate_gateway_envelope(envelope, binding)


class CoreCompetitorRegistrationModelRunMaterializer:
    """Persist model calls used by competitor breakdown and tag extraction."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("competitor_registration_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks competitor-registration Core binding")
        return self._core._persist_competitor_registration_gateway_envelope(envelope, binding)


class CoreDailyHitModelRunMaterializer:
    """Persist one daily-hit breakdown without pretending it is a registration step."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("daily_hit_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks daily-hit Core binding")
        return self._core._persist_daily_hit_gateway_envelope(envelope, binding)


class Stage0ContentProductionCore:
    """The sole formal Core API for Stage 0's first vertical production chain."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        db_path: Path,
        data_identity: DataIdentity,
        domain_config_dir: Path | None = None,
    ) -> None:
        self.conn = connection
        self.db_path = db_path.resolve()
        self.data_identity = data_identity
        self.domain_config_dir = (domain_config_dir or DOMAIN_CONFIG_DIR).resolve()
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def _validate_database_path(
        cls, db_path: Path | str, *, data_identity: DataIdentity
    ) -> Path:
        resolved = Path(db_path).resolve()
        if data_identity == "production":
            if resolved != FORMAL_DB_PATH.resolve():
                raise DataIdentityError("production identity may only use the configured formal runtime database")
        elif data_identity in NON_PRODUCTION_IDENTITIES:
            if resolved == FORMAL_DB_PATH.resolve():
                raise DataIdentityError("non-production identity must never open the formal production database")
            try:
                resolved.relative_to(runtime_root_for_identity("production").resolve())
            except (ValueError, RuntimeStorageError):
                pass
            else:
                raise DataIdentityError("non-production identity must never open any database inside the formal runtime")
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                pass
            else:
                raise DataIdentityError("non-production identity must not open a database inside the build root")
        else:
            raise DataIdentityError(f"unsupported data identity: {data_identity}")
        return resolved

    @classmethod
    def open(cls, db_path: Path | str, *, data_identity: DataIdentity) -> "Stage0ContentProductionCore":
        resolved = cls._validate_database_path(db_path, data_identity=data_identity)
        connection = sqlite3.connect(resolved)
        core = cls(connection, db_path=resolved, data_identity=data_identity)
        core.install_schema()
        return core

    @classmethod
    def open_read_only(
        cls, db_path: Path | str, *, data_identity: DataIdentity
    ) -> "Stage0ContentProductionCore":
        """Open an existing database without schema installation or writes."""

        resolved = cls._validate_database_path(db_path, data_identity=data_identity)
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        return cls(connection, db_path=resolved, data_identity=data_identity)

    def close(self) -> None:
        self.conn.close()

    def install_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stage0_content_task (
                task_id TEXT PRIMARY KEY,
                topic_version_id TEXT NOT NULL,
                current_node TEXT NOT NULL,
                current_version_id TEXT,
                current_status TEXT NOT NULL,
                task_revision INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cancelled_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_content_node_version (
                version_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                parent_version_id TEXT,
                upstream_version_id TEXT,
                input_assembly_id TEXT,
                status TEXT NOT NULL,
                output_ref TEXT,
                validation_status TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                task_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_decision (
                decision_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                decision TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_node_failure (
                failure_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                failed_version_id TEXT NOT NULL UNIQUE REFERENCES stage0_content_node_version(version_id),
                request_version_id TEXT NOT NULL UNIQUE REFERENCES stage0_content_node_version(version_id),
                model_run_id TEXT,
                failure_stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL CHECK(raw_model_output_status IN ('available', 'not_available')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                upstream_version_id TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_model_run (
                model_run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                node_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage0_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT NOT NULL,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                output_version_id TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS stage0_command_receipt (
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(command_scope, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS stage0_audit_event (
                audit_id TEXT PRIMARY KEY,
                task_id TEXT,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1a_artifact_payload (
                version_id TEXT PRIMARY KEY REFERENCES stage0_content_node_version(version_id),
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('formal_topic', 'research_plan')),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_artifact_payload (
                version_id TEXT PRIMARY KEY REFERENCES stage0_content_node_version(version_id),
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('deep_research', 'content_plan', 'formal_draft', 'copy_optimization', 'de_ai_revision', 'review')),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_research_material (
                research_material_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                source_ref TEXT NOT NULL,
                title TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role IN ('fact_evidence', 'professional_interpretation', 'audience_perception', 'research_clue')),
                material_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(task_id, source_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_source (
                manual_source_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('direction', 'link', 'person', 'work', 'playlist')),
                original_content_json TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role = 'user_origin_not_fact'),
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_source_link (
                link_id TEXT PRIMARY KEY,
                manual_source_id TEXT NOT NULL REFERENCES stage0_manual_source(manual_source_id),
                target_kind TEXT NOT NULL CHECK(target_kind IN ('saved_user_direction', 'candidate', 'formal_topic', 'person_exploration', 'work_exploration', 'playlist_exploration')),
                target_id TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                UNIQUE(manual_source_id, target_kind, target_id)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration (
                exploration_id TEXT PRIMARY KEY,
                manual_source_id TEXT NOT NULL REFERENCES stage0_manual_source(manual_source_id),
                exploration_kind TEXT NOT NULL CHECK(exploration_kind IN ('person_exploration', 'work_exploration', 'playlist_exploration')),
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_material_collection', 'collecting', 'awaiting_human_direction', 'completed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(manual_source_id, exploration_kind)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration_state (
                exploration_state_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                status TEXT NOT NULL CHECK(status IN ('awaiting_material_collection', 'collecting', 'awaiting_human_direction', 'completed', 'cancelled')),
                reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration_material (
                exploration_material_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                material_ref_json TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role IN ('fact_evidence', 'professional_interpretation', 'audience_perception', 'video_material', 'research_clue')),
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(exploration_id, material_ref_json)
            );
            CREATE TABLE IF NOT EXISTS stage0_music_audience_echo (
                audience_echo_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                platform TEXT NOT NULL CHECK(platform IN ('netease_music', 'douban')),
                work_title TEXT NOT NULL,
                subject_kind TEXT NOT NULL CHECK(subject_kind IN ('song', 'album')),
                material_kind TEXT NOT NULL CHECK(material_kind IN ('comment', 'short_review', 'long_review')),
                text TEXT NOT NULL,
                useful_count INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                viewed_position INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(exploration_id, platform, source_url, material_kind, viewed_position)
            );
            CREATE TABLE IF NOT EXISTS stage0_voice_profile (
                voice_profile_id TEXT PRIMARY KEY,
                profile_label TEXT NOT NULL,
                reference_audio_ref TEXT NOT NULL,
                emotion_reference_audio_ref TEXT,
                mode TEXT NOT NULL CHECK(mode IN ('basic', 'ultimate')),
                prompt_text TEXT NOT NULL,
                emotion_prompt_text TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_confirmation', 'active', 'retired')),
                data_identity TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_production (
                audio_production_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                approved_content_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                voice_profile_id TEXT NOT NULL REFERENCES stage0_voice_profile(voice_profile_id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                title TEXT NOT NULL,
                script_text_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'awaiting_human_review', 'approved', 'returned', 'failed')),
                audio_ref TEXT,
                metadata_ref TEXT,
                synthesis_json TEXT NOT NULL,
                quality_review_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                audio_delivery_id TEXT,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, approved_content_version_id, attempt_number)
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_decision (
                audio_decision_id TEXT PRIMARY KEY,
                audio_production_id TEXT NOT NULL UNIQUE REFERENCES stage0_audio_production(audio_production_id),
                decision TEXT NOT NULL CHECK(decision IN ('approved', 'returned')),
                issue_scope TEXT NOT NULL CHECK(issue_scope IN ('none', 'audio_regeneration', 'content_revision')),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_human_decision_carrier_binding (
                carrier_binding_id TEXT PRIMARY KEY,
                carrier_kind TEXT NOT NULL,
                entry_ref TEXT NOT NULL,
                context_strategy TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('proposed', 'validated', 'retired')),
                validation_evidence_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                proposed_by TEXT NOT NULL,
                proposed_at TEXT NOT NULL,
                validated_by TEXT,
                validated_at TEXT,
                UNIQUE(carrier_kind, entry_ref, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_human_decision_command (
                command_id TEXT PRIMARY KEY,
                carrier_binding_id TEXT NOT NULL REFERENCES stage0_human_decision_carrier_binding(carrier_binding_id),
                session_ref TEXT NOT NULL,
                action TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
                status TEXT NOT NULL CHECK(status IN ('received', 'completed', 'rejected')),
                result_json TEXT NOT NULL,
                error_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                received_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(carrier_binding_id, command_id, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_live_cold_start_preflight (
                preflight_receipt_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                owned_account_id TEXT NOT NULL,
                competitor_account_ids_json TEXT NOT NULL,
                voice_profile_id TEXT NOT NULL,
                report_json TEXT NOT NULL,
                report_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'consumed')),
                consumed_by_cold_start_id TEXT,
                data_identity TEXT NOT NULL,
                inspected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_delivery (
                audio_delivery_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                approved_content_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                audio_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('delivered')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, audio_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_experience_candidate_run (
                experience_candidate_run_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'completed_with_gaps', 'superseded')),
                source_snapshot_json TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_experience_candidate (
                experience_candidate_id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES stage0_content_task(task_id),
                experience_candidate_run_id TEXT,
                domain_label TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                frozen_sources_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preparing', 'awaiting_human_decision', 'no_proposal', 'failed', 'accepted', 'rejected')),
                proposal_json TEXT,
                failure_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT,
                UNIQUE(task_id, source_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS stage0_experience_candidate_model_run (
                experience_candidate_model_run_id TEXT PRIMARY KEY,
                experience_candidate_id TEXT NOT NULL REFERENCES stage0_experience_candidate(experience_candidate_id),
                status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                envelope_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_confirmed_experience (
                experience_id TEXT PRIMARY KEY,
                experience_candidate_id TEXT NOT NULL UNIQUE REFERENCES stage0_experience_candidate(experience_candidate_id),
                domain_label TEXT NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN ('shared_pattern', 'single_source_feature')),
                summary TEXT NOT NULL,
                applicable_when_json TEXT NOT NULL,
                method_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                boundary_json TEXT NOT NULL,
                experience_layer TEXT NOT NULL DEFAULT 'section_method',
                use_positions_json TEXT NOT NULL DEFAULT '["body"]',
                trigger_signals_json TEXT NOT NULL DEFAULT '[]',
                not_applicable_when_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL CHECK(status IN ('active', 'paused')),
                data_identity TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_unregistered_account_video (
                account_video_observation_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT NOT NULL,
                account_display_name TEXT NOT NULL,
                video_ref TEXT NOT NULL,
                video_url TEXT NOT NULL,
                video_title TEXT NOT NULL,
                qualified INTEGER NOT NULL CHECK(qualified IN (0, 1)),
                source_ref_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                UNIQUE(domain_label, account_ref, video_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_unregistered_account_review (
                account_review_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT NOT NULL,
                account_display_name TEXT NOT NULL,
                triggering_video_refs_json TEXT NOT NULL,
                recent_accessible_videos_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT,
                UNIQUE(domain_label, account_ref, status)
            );
            CREATE TABLE IF NOT EXISTS stage0_content_account (
                content_account_id TEXT PRIMARY KEY,
                account_role TEXT NOT NULL CHECK(account_role IN ('owned', 'competitor')),
                display_name TEXT NOT NULL,
                domain_label TEXT NOT NULL,
                external_account_ref TEXT,
                status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_role, domain_label, external_account_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_configuration (
                configuration_id TEXT PRIMARY KEY,
                domain_mode TEXT NOT NULL CHECK(domain_mode IN ('reuse', 'create')),
                domain_label TEXT NOT NULL,
                domain_name TEXT NOT NULL,
                domain_boundary TEXT NOT NULL,
                platform TEXT NOT NULL,
                owned_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                competitor_account_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('confirmed', 'started', 'completed', 'cancelled')),
                data_identity TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                cold_start_id TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start (
                cold_start_id TEXT PRIMARY KEY,
                owned_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                domain_label TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running', 'stopped', 'failed', 'waiting_human', 'completed')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_domain_activation (
                activation_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                configuration_id TEXT NOT NULL REFERENCES stage0_cold_start_configuration(configuration_id),
                data_identity TEXT NOT NULL,
                is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
                created_at TEXT NOT NULL,
                released_at TEXT,
                released_by TEXT,
                release_reason TEXT,
                UNIQUE(cold_start_id, data_identity),
                UNIQUE(configuration_id, data_identity)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS stage0_domain_activation_current_unique
            ON stage0_domain_activation(domain_label, data_identity)
            WHERE is_current=1;
            CREATE TABLE IF NOT EXISTS stage0_cold_start_run_contract (
                cold_start_id TEXT PRIMARY KEY REFERENCES stage0_cold_start(cold_start_id),
                domain_label TEXT NOT NULL,
                owned_account_id TEXT NOT NULL,
                competitor_account_ids_json TEXT NOT NULL,
                input_snapshot_json TEXT NOT NULL,
                cold_start_contract_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_onboarding_failure (
                failure_id TEXT PRIMARY KEY,
                configuration_id TEXT,
                domain_label TEXT NOT NULL,
                input_snapshot_json TEXT NOT NULL,
                cold_start_contract_version TEXT NOT NULL,
                reason TEXT NOT NULL,
                failed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration (
                registration_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                competitor_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                current_step TEXT NOT NULL CHECK(current_step IN ('historical_material', 'high_signal_identification', 'transcripts_and_comments', 'breakdown', 'tag_candidates', 'domain_summary', 'awaiting_human_review', 'completed', 'failed')),
                status TEXT NOT NULL CHECK(status IN ('processing', 'awaiting_human_review', 'completed', 'failed')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(cold_start_id, competitor_account_id)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_step (
                step_record_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('historical_material', 'high_signal_identification', 'transcripts_and_comments', 'breakdown', 'tag_candidates', 'domain_summary')),
                artifact_refs_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                UNIQUE(registration_id, step_name)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_model_run (
                registration_model_run_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('breakdown', 'tag_candidates')),
                status TEXT NOT NULL,
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                error_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_daily_hit_model_run (
                daily_hit_model_run_id TEXT PRIMARY KEY,
                hit_id TEXT NOT NULL REFERENCES hits(hit_id),
                status TEXT NOT NULL,
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                error_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_daily_hit_breakdown (
                hit_id TEXT PRIMARY KEY REFERENCES hits(hit_id),
                version INTEGER NOT NULL,
                artifact_json TEXT NOT NULL,
                model_run_id TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_daily_hit_processing_failure (
                failure_id TEXT PRIMARY KEY,
                hit_id TEXT NOT NULL REFERENCES hits(hit_id),
                stage_name TEXT NOT NULL,
                error_json TEXT NOT NULL,
                run_id TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_item (
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('transcripts_and_comments', 'breakdown')),
                item_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'excluded')),
                artifact_json TEXT NOT NULL,
                error_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(registration_id, step_name, item_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_breakdown_attempt (
                breakdown_attempt_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                source_id TEXT NOT NULL,
                attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('initial', 'post_batch_delivery_retry')),
                outcome TEXT NOT NULL CHECK(outcome IN ('completed', 'delivery_interrupted', 'failed')),
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL,
                model_run_id TEXT,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS stage0_competitor_breakdown_attempt_source_idx
            ON stage0_competitor_breakdown_attempt(registration_id, source_id, data_identity, created_at);
            CREATE TABLE IF NOT EXISTS stage0_competitor_breakdown_backlog_task (
                backlog_task_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'completed_with_failures')),
                source_snapshot_json TEXT NOT NULL,
                approval_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS stage0_competitor_breakdown_backlog_task_active_idx
            ON stage0_competitor_breakdown_backlog_task(cold_start_id, data_identity, status, created_at);
            CREATE TABLE IF NOT EXISTS stage0_competitor_material_collection_checkpoint (
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                item_ref TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                comments_json TEXT NOT NULL,
                collection_ref TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(registration_id, item_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_tag_library (
                tag_library_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL UNIQUE REFERENCES stage0_cold_start(cold_start_id),
                domain_label TEXT NOT NULL,
                extraction_method TEXT NOT NULL,
                source_item_count INTEGER NOT NULL,
                minimum_source_support INTEGER NOT NULL,
                candidate_set_json TEXT NOT NULL,
                tag_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_content_type_candidate (
                content_type_candidate_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                domain_label TEXT NOT NULL,
                candidate_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preparing', 'awaiting_human_decision', 'failed', 'accepted', 'rejected', 'frozen')),
                source_snapshot_json TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                review_json TEXT NOT NULL,
                freeze_provenance_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                UNIQUE(cold_start_id, candidate_version, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_domain_boundary_candidate (
                boundary_candidate_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                domain_label TEXT NOT NULL,
                candidate_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preparing', 'awaiting_human_decision', 'failed', 'frozen')),
                source_snapshot_json TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                review_json TEXT NOT NULL,
                freeze_provenance_json TEXT NOT NULL,
                model_run_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                UNIQUE(cold_start_id, candidate_version, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_two_week_tag_library_review (
                tag_review_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                candidate_set_json TEXT NOT NULL,
                tag_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                UNIQUE(domain_label, window_start, window_end, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_knowledge_mirror_run (
                mirror_run_id TEXT PRIMARY KEY,
                cold_start_id TEXT REFERENCES stage0_cold_start(cold_start_id),
                mirror_root TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                relationship_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
                data_identity TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_domain_summary (
                cold_start_id TEXT PRIMARY KEY REFERENCES stage0_cold_start(cold_start_id),
                artifact_refs_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS stage0_competitor_registration_step_immutable_update
            BEFORE UPDATE ON stage0_competitor_registration_step
            BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_competitor_registration_step_immutable_delete
            BEFORE DELETE ON stage0_competitor_registration_step
            BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_immutable_update
            BEFORE UPDATE ON stage0_manual_source
            BEGIN SELECT RAISE(ABORT, 'manual sources are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_immutable_delete
            BEFORE DELETE ON stage0_manual_source
            BEGIN SELECT RAISE(ABORT, 'manual sources are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_link_immutable_update
            BEFORE UPDATE ON stage0_manual_source_link
            BEGIN SELECT RAISE(ABORT, 'manual source links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_link_immutable_delete
            BEFORE DELETE ON stage0_manual_source_link
            BEGIN SELECT RAISE(ABORT, 'manual source links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_exploration_immutable_update
            BEFORE UPDATE ON stage0_manual_exploration
            BEGIN SELECT RAISE(ABORT, 'manual explorations are immutable; append the next formal state instead'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_exploration_immutable_delete
            BEFORE DELETE ON stage0_manual_exploration
            BEGIN SELECT RAISE(ABORT, 'manual explorations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_update
            BEFORE UPDATE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_delete
            BEFORE DELETE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_failure_immutable_update
            BEFORE UPDATE ON stage0_content_node_failure
            BEGIN SELECT RAISE(ABORT, 'stage0 node failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_failure_immutable_delete
            BEFORE DELETE ON stage0_content_node_failure
            BEGIN SELECT RAISE(ABORT, 'stage0 node failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_update
            BEFORE UPDATE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_delete
            BEFORE DELETE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TABLE IF NOT EXISTS stage0_daily_run (
                daily_run_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                business_date TEXT NOT NULL,
                lifecycle TEXT NOT NULL CHECK(lifecycle IN ('running', 'failed', 'stopped', 'completed')),
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                data_identity TEXT NOT NULL,
                cold_start_id TEXT REFERENCES stage0_cold_start(cold_start_id),
                UNIQUE(domain_label, business_date, cold_start_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_discovery_run (
                run_id TEXT PRIMARY KEY,
                discovery_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                failure_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage1b_run_execution_context (
                run_id TEXT PRIMARY KEY REFERENCES stage1b_discovery_run(run_id),
                execution_mode TEXT NOT NULL CHECK(execution_mode IN ('test_isolated', 'real_daily_validation', 'production_daily', 'retired_legacy_validation')),
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('processing', 'completed', 'completed_with_failures', 'timed_out', 'interrupted', 'failed', 'cancelled')),
                classification_reason TEXT NOT NULL,
                classified_by TEXT NOT NULL,
                classified_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                daily_run_id TEXT REFERENCES stage0_daily_run(daily_run_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_run_domain_scope (
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(run_id, domain_label)
            );
            CREATE TABLE IF NOT EXISTS stage1_question_expansion_source (
                expansion_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                parent_source_ref_json TEXT NOT NULL,
                validation_outcome TEXT NOT NULL CHECK(validation_outcome = 'supported'),
                validated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1_question_expansion_qualification (
                qualification_id TEXT PRIMARY KEY,
                expansion_id TEXT NOT NULL,
                domain_label TEXT NOT NULL,
                parent_source_ref_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('qualified', 'rejected', 'unresolved', 'blocked')),
                rejection_reason TEXT NOT NULL,
                material_refs_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(expansion_id, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage1_saved_user_direction_source (
                direction_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_source_version (
                source_version_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('daily_competitor_content', 'historical_high_signal', 'hotspot', 'tag_discovery', 'question_expansion', 'saved_user_direction')),
                source_object_id TEXT NOT NULL,
                source_object_version TEXT NOT NULL,
                source_time TEXT NOT NULL,
                expires_at TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_type, source_object_id, source_object_version)
            );
            CREATE TABLE IF NOT EXISTS stage1b_filter_result (
                filter_result_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                outcome TEXT NOT NULL CHECK(outcome IN ('eligible', 'excluded')),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                model_config_version TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_model_run (
                model_run_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage1b_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT NOT NULL,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway IN (0, 1)),
                UNIQUE(run_id, source_version_id, input_assembly_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_version (
                candidate_version_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                parent_candidate_version_id TEXT,
                model_run_id TEXT NOT NULL REFERENCES stage1b_model_run(model_run_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_user_decision', 'selected', 'deferred', 'rejected', 'evergreen', 'angle_change_requested')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_assessment (
                assessment_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                dimension_scores_json TEXT NOT NULL,
                dimension_reasons_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                assessed_by TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                assessed_at TEXT NOT NULL,
                UNIQUE(candidate_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_assessment_revision (
                assessment_revision_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                dimension_scores_json TEXT NOT NULL,
                dimension_reasons_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                assessed_by TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                assessed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_support (
                support_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                support_kind TEXT NOT NULL CHECK(support_kind IN ('initial_discovery', 'same_topic_same_angle_material')),
                relation_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_version_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_pool_state (
                pool_state_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                pool_status TEXT NOT NULL CHECK(pool_status IN ('current', 'historical')),
                reason TEXT NOT NULL,
                parameter_version TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_pool_parameter_version (
                parameter_version TEXT PRIMARY KEY,
                observation_window_days INTEGER NOT NULL CHECK(observation_window_days > 0),
                changed_by TEXT NOT NULL,
                change_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_relation (
                relation_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                related_candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                relation_kind TEXT NOT NULL CHECK(relation_kind IN ('same_topic_same_angle', 'same_topic_different_angle')),
                relation_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_version_id, related_candidate_version_id),
                CHECK(candidate_version_id <> related_candidate_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_daily_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                candidate_version_id TEXT REFERENCES stage1b_candidate_version(candidate_version_id),
                display_position INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, domain_label, display_position)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_decision (
                decision_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                decision TEXT NOT NULL CHECK(decision IN ('selected', 'deferred', 'rejected', 'angle_change_requested', 'evergreen')),
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                formal_topic_task_id TEXT,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_absence (
                absence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                model_run_id TEXT REFERENCES stage1b_model_run(model_run_id),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_source_failure (
                failure_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                model_run_id TEXT REFERENCES stage1b_model_run(model_run_id),
                failure_stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL CHECK(raw_model_output_status IN ('available', 'not_available')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_update
            BEFORE UPDATE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_delete
            BEFORE DELETE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_update
            BEFORE UPDATE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_delete
            BEFORE DELETE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_failure_immutable_update
            BEFORE UPDATE ON stage1b_source_failure
            BEGIN SELECT RAISE(ABORT, 'stage1b source failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_failure_immutable_delete
            BEFORE DELETE ON stage1b_source_failure
            BEGIN SELECT RAISE(ABORT, 'stage1b source failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_execution_context_mode_immutable
            BEFORE UPDATE OF execution_mode ON stage1b_run_execution_context
            BEGIN SELECT RAISE(ABORT, 'stage1b execution mode is immutable; create a new run instead'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_domain_scope_immutable_update
            BEFORE UPDATE ON stage1b_run_domain_scope
            BEGIN SELECT RAISE(ABORT, 'stage1b run domain scope is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_domain_scope_immutable_delete
            BEFORE DELETE ON stage1b_run_domain_scope
            BEGIN SELECT RAISE(ABORT, 'stage1b run domain scope is immutable'); END;
            """
        )
        competitor_schema = Path(__file__).resolve().parents[1] / "business_data" / "competitor_accounts_schema.sqlite.sql"
        self.conn.executescript(competitor_schema.read_text(encoding="utf-8"))
        self._migrate_cold_start_reuse_constraint()
        self._migrate_daily_batch_dates()
        discovery_schema = Path(__file__).resolve().parents[1] / "business_data" / "domain_search_schema.sqlite.sql"
        self.conn.executescript(discovery_schema.read_text(encoding="utf-8"))
        self.conn.execute("DROP TABLE IF EXISTS stage1b_candidate_cooldown")
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_candidate_pool_parameter_version VALUES (?, ?, ?, ?, ?, ?)",
            ("candidate_pool_window_v1", 14, "system", "initial confirmed two-week observation window", self.data_identity, _now()),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_run_domain_scope "
            "(run_id, domain_label, data_identity, recorded_at) "
            "SELECT run_id, domain_label, data_identity, MIN(created_at) "
            "FROM stage1b_source_version "
            "GROUP BY run_id, domain_label, data_identity"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_run_domain_scope "
            "(run_id, domain_label, data_identity, recorded_at) "
            "SELECT run_id, domain_label, data_identity, MIN(created_at) "
            "FROM stage1b_daily_snapshot "
            "GROUP BY run_id, domain_label, data_identity"
        )
        self._migrate_daily_run_schema()
        from scripts.core.production.publication_feedback import install_schema as install_publication_feedback_schema
        install_publication_feedback_schema(self.conn)
        # The former per-material trigger route has been retired.  Removing a
        # leftover table here makes an old process or an older database layout
        # unable to revive that route after the batch boundary was introduced.
        self.conn.execute("DROP TABLE IF EXISTS stage0_deep_breakdown_trigger")
        self._migrate_hit_comments_purpose()
        self._migrate_competitor_registration_item_statuses()
        self._migrate_question_expansion_qualification_statuses()
        self._migrate_experience_candidate_scope()
        self._migrate_experience_candidate_runs()
        self._repair_experience_candidate_foreign_keys()
        self._migrate_experience_usage_fields()
        self.conn.execute(
            "DROP INDEX IF EXISTS stage0_experience_candidate_pre_topic_unique"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX stage0_experience_candidate_pre_topic_unique "
            "ON stage0_experience_candidate(domain_label, experience_candidate_run_id, source_fingerprint) "
            "WHERE task_id IS NULL AND experience_candidate_run_id IS NOT NULL "
            "AND status IN ('preparing', 'awaiting_human_decision', 'accepted')"
        )
        self.conn.commit()

    def _migrate_daily_run_schema(self) -> None:
        """Install the one formal daily lifecycle and its stage-run link."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS stage0_daily_run ("
            "daily_run_id TEXT PRIMARY KEY, "
            "domain_label TEXT NOT NULL, "
            "business_date TEXT NOT NULL, "
            "lifecycle TEXT NOT NULL CHECK(lifecycle IN ('running', 'failed', 'stopped', 'completed')), "
            "created_at TEXT NOT NULL, "
            "started_at TEXT, "
            "finished_at TEXT, "
            "data_identity TEXT NOT NULL, "
            "cold_start_id TEXT REFERENCES stage0_cold_start(cold_start_id), "
            "UNIQUE(domain_label, business_date, cold_start_id)"
            ")"
        )
        columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(stage0_daily_run)"
            ).fetchall()
        }
        if "cold_start_id" not in columns:
            self.conn.execute(
                "ALTER TABLE stage0_daily_run ADD COLUMN cold_start_id TEXT "
                "REFERENCES stage0_cold_start(cold_start_id)"
            )
        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_daily_run'"
        ).fetchone()
        schema_sql = str(schema_row["sql"] or "") if schema_row is not None else ""
        if "UNIQUE(domain_label,business_date)" in schema_sql.replace(" ", "").replace("\n", ""):
            self._migrate_daily_run_reuse_constraint()
        context_columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(stage1b_run_execution_context)"
            ).fetchall()
        }
        if "daily_run_id" not in context_columns:
            self.conn.execute(
                "ALTER TABLE stage1b_run_execution_context "
                "ADD COLUMN daily_run_id TEXT REFERENCES stage0_daily_run(daily_run_id)"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS stage1b_run_execution_context_daily_run_idx "
            "ON stage1b_run_execution_context(daily_run_id, execution_mode)"
        )

    def _migrate_cold_start_reuse_constraint(self) -> None:
        """Allow the same account identity to start a later cold-start round."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_cold_start'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        compact = schema_sql.replace(" ", "").replace("\n", "")
        if "UNIQUE(owned_account_id,domain_label,data_identity)" not in compact:
            return
        foreign_keys_enabled = bool(self.conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys_enabled:
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute(
                "CREATE TABLE stage0_cold_start__v2 ("
                "cold_start_id TEXT PRIMARY KEY, "
                "owned_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id), "
                "domain_label TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('running', 'stopped', 'failed', 'waiting_human', 'completed')), "
                "data_identity TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT"
                ")"
            )
            self.conn.execute(
                "INSERT INTO stage0_cold_start__v2 "
                "SELECT cold_start_id, owned_account_id, domain_label, status, data_identity, "
                "created_by, created_at, completed_at FROM stage0_cold_start"
            )
            self.conn.execute("DROP TABLE stage0_cold_start")
            self.conn.execute(
                "ALTER TABLE stage0_cold_start__v2 RENAME TO stage0_cold_start"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            if foreign_keys_enabled:
                self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_daily_run_reuse_constraint(self) -> None:
        """Allow a new activation to use the same business date as old history."""
        foreign_keys_enabled = bool(self.conn.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys_enabled:
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute(
                "CREATE TABLE stage0_daily_run__v2 ("
                "daily_run_id TEXT PRIMARY KEY, domain_label TEXT NOT NULL, "
                "business_date TEXT NOT NULL, "
                "lifecycle TEXT NOT NULL CHECK(lifecycle IN ('running', 'failed', 'stopped', 'completed')), "
                "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, data_identity TEXT NOT NULL, "
                "cold_start_id TEXT REFERENCES stage0_cold_start(cold_start_id), "
                "UNIQUE(domain_label, business_date, cold_start_id)"
                ")"
            )
            self.conn.execute(
                "INSERT INTO stage0_daily_run__v2 "
                "SELECT daily_run_id, domain_label, business_date, lifecycle, created_at, "
                "started_at, finished_at, data_identity, cold_start_id FROM stage0_daily_run"
            )
            self.conn.execute("DROP TABLE stage0_daily_run")
            self.conn.execute(
                "ALTER TABLE stage0_daily_run__v2 RENAME TO stage0_daily_run"
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            if foreign_keys_enabled:
                self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_daily_batch_dates(self) -> None:
        """Keep real observation clocks separate from daily business ownership."""

        video_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(competitor_videos)").fetchall()
        }
        if "first_seen_business_date" not in video_columns:
            self.conn.execute(
                "ALTER TABLE competitor_videos ADD COLUMN first_seen_business_date TEXT"
            )
        check_columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(video_checks)").fetchall()
        }
        if "business_date" not in check_columns:
            self.conn.execute("ALTER TABLE video_checks ADD COLUMN business_date TEXT")
        self.conn.execute(
            "UPDATE competitor_videos "
            "SET first_seen_business_date=date(datetime(first_seen_at), '+8 hours') "
            "WHERE first_seen_business_date IS NULL OR first_seen_business_date=''"
        )
        self.conn.execute(
            "UPDATE video_checks "
            "SET business_date=date(datetime(checked_at), '+8 hours') "
            "WHERE business_date IS NULL OR business_date=''"
        )

    def _migrate_hit_comments_purpose(self) -> None:
        """Allow the formal daily-hit comment collection purpose on existing DBs."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='hit_comments'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        if "'daily_hit'" in schema_sql:
            return
        with self.conn:
            self.conn.execute("ALTER TABLE hit_comments RENAME TO hit_comments_legacy")
            self.conn.execute(
                "CREATE TABLE hit_comments ("
                "hit_id TEXT NOT NULL REFERENCES hits(hit_id) ON DELETE RESTRICT, "
                "comment_id TEXT NOT NULL, text TEXT NOT NULL, like_count INTEGER NOT NULL DEFAULT 0, "
                "parent_comment_id TEXT, sample_rank INTEGER NOT NULL, "
                "purpose TEXT NOT NULL CHECK(purpose IN ('early_topic', 'mature_analysis', 'mature_history', 'external_snapshot', 'daily_hit')), "
                "observation_point TEXT, sampling_strategy TEXT, run_id TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (hit_id, comment_id, purpose))"
            )
            self.conn.execute(
                "INSERT INTO hit_comments "
                "(hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, "
                "observation_point, sampling_strategy, run_id, fetched_at) "
                "SELECT hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, "
                "observation_point, sampling_strategy, run_id, fetched_at "
                "FROM hit_comments_legacy"
            )
            self.conn.execute("DROP TABLE hit_comments_legacy")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hit_comments_hit "
                "ON hit_comments(hit_id, sample_rank)"
            )

    def _migrate_competitor_registration_item_statuses(self) -> None:
        """Allow a final, non-retry exclusion without mislabeling it as a model failure."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_competitor_registration_item'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        if "'excluded'" in schema_sql:
            return
        with self.conn:
            self.conn.execute("ALTER TABLE stage0_competitor_registration_item RENAME TO stage0_competitor_registration_item_legacy")
            self.conn.execute(
                "CREATE TABLE stage0_competitor_registration_item ("
                "registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id), "
                "step_name TEXT NOT NULL CHECK(step_name IN ('transcripts_and_comments', 'breakdown')), "
                "item_ref TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'excluded')), "
                "artifact_json TEXT NOT NULL, error_json TEXT NOT NULL, attempt_count INTEGER NOT NULL, "
                "data_identity TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(registration_id, step_name, item_ref))"
            )
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item "
                "SELECT registration_id, step_name, item_ref, status, artifact_json, error_json, "
                "attempt_count, data_identity, updated_at FROM stage0_competitor_registration_item_legacy"
            )
            self.conn.execute("DROP TABLE stage0_competitor_registration_item_legacy")

    def _migrate_question_expansion_qualification_statuses(self) -> None:
        """Keep unresolved external checks distinct from final rejection."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage1_question_expansion_qualification'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        if all(status in schema_sql for status in ("'unresolved'", "'blocked'")):
            return
        with self.conn:
            self.conn.execute(
                "ALTER TABLE stage1_question_expansion_qualification "
                "RENAME TO stage1_question_expansion_qualification_legacy"
            )
            self.conn.execute(
                "CREATE TABLE stage1_question_expansion_qualification ("
                "qualification_id TEXT PRIMARY KEY, expansion_id TEXT NOT NULL, "
                "domain_label TEXT NOT NULL, parent_source_ref_json TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('qualified', 'rejected', 'unresolved', 'blocked')), "
                "rejection_reason TEXT NOT NULL, material_refs_json TEXT NOT NULL, "
                "checks_json TEXT NOT NULL, evaluated_at TEXT NOT NULL, data_identity TEXT NOT NULL, "
                "created_by TEXT NOT NULL, UNIQUE(expansion_id, data_identity))"
            )
            self.conn.execute(
                "INSERT INTO stage1_question_expansion_qualification "
                "(qualification_id, expansion_id, domain_label, parent_source_ref_json, status, "
                "rejection_reason, material_refs_json, checks_json, evaluated_at, data_identity, created_by) "
                "SELECT qualification_id, expansion_id, domain_label, parent_source_ref_json, status, "
                "rejection_reason, material_refs_json, checks_json, evaluated_at, data_identity, created_by "
                "FROM stage1_question_expansion_qualification_legacy"
            )
            self.conn.execute("DROP TABLE stage1_question_expansion_qualification_legacy")

    def _migrate_experience_candidate_scope(self) -> None:
        """Allow one shared experience-candidate path before a content task exists."""

        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_experience_candidate'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        if "task_id TEXT NOT NULL" not in schema_sql:
            return
        with self.conn:
            self.conn.execute(
                "ALTER TABLE stage0_experience_candidate RENAME TO stage0_experience_candidate_legacy"
            )
            self.conn.execute(
                "CREATE TABLE stage0_experience_candidate ("
                "experience_candidate_id TEXT PRIMARY KEY, "
                "task_id TEXT REFERENCES stage0_content_task(task_id), "
                "experience_candidate_run_id TEXT, "
                "domain_label TEXT NOT NULL, source_fingerprint TEXT NOT NULL, "
                "frozen_sources_json TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('preparing', 'awaiting_human_decision', 'no_proposal', 'failed', 'accepted', 'rejected')), "
                "proposal_json TEXT, failure_json TEXT NOT NULL, data_identity TEXT NOT NULL, "
                "created_by TEXT NOT NULL, created_at TEXT NOT NULL, decided_by TEXT, "
                "decided_at TEXT, decision_reason TEXT, "
                "UNIQUE(task_id, source_fingerprint))"
            )
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate "
                "(experience_candidate_id, task_id, experience_candidate_run_id, domain_label, source_fingerprint, "
                "frozen_sources_json, status, proposal_json, failure_json, data_identity, "
                "created_by, created_at, decided_by, decided_at, decision_reason) "
                "SELECT experience_candidate_id, task_id, NULL, domain_label, source_fingerprint, "
                "frozen_sources_json, status, proposal_json, failure_json, data_identity, "
                "created_by, created_at, decided_by, decided_at, decision_reason "
                "FROM stage0_experience_candidate_legacy"
            )
            self.conn.execute("DROP TABLE stage0_experience_candidate_legacy")

    def _migrate_experience_candidate_runs(self) -> None:
        """Add an explicit boundary for each independent pre-topic pass."""

        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS stage0_experience_candidate_run ("
            "experience_candidate_run_id TEXT PRIMARY KEY, "
            "domain_label TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'completed_with_gaps', 'superseded')), "
            "source_snapshot_json TEXT NOT NULL, source_count INTEGER NOT NULL, "
            "data_identity TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, "
            "completed_at TEXT)"
        )
        columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(stage0_experience_candidate)"
            ).fetchall()
        }
        if "experience_candidate_run_id" not in columns:
            self.conn.execute(
                "ALTER TABLE stage0_experience_candidate "
                "ADD COLUMN experience_candidate_run_id TEXT"
            )

    def _migrate_experience_usage_fields(self) -> None:
        """Add the explicit layer and usage conditions to confirmed experiences."""

        columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(stage0_confirmed_experience)"
            ).fetchall()
        }
        additions = {
            "experience_layer": "TEXT NOT NULL DEFAULT 'section_method'",
            "use_positions_json": "TEXT NOT NULL DEFAULT '[\"body\"]'",
            "trigger_signals_json": "TEXT NOT NULL DEFAULT '[]'",
            "not_applicable_when_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE stage0_confirmed_experience ADD COLUMN {name} {definition}"
                )
        self.conn.execute(
            "UPDATE stage0_confirmed_experience "
            "SET trigger_signals_json=applicable_when_json "
            "WHERE trigger_signals_json='[]' OR trigger_signals_json IS NULL"
        )
        self.conn.execute(
            "UPDATE stage0_confirmed_experience "
            "SET not_applicable_when_json=boundary_json "
            "WHERE not_applicable_when_json='[]' OR not_applicable_when_json IS NULL"
        )

    def _repair_experience_candidate_foreign_keys(self) -> None:
        """Repair child tables left pointing at the retired candidate table."""

        stale_target = "stage0_experience_candidate_legacy"
        table_definitions = {
            "stage0_experience_candidate_model_run": (
                "CREATE TABLE stage0_experience_candidate_model_run ("
                "experience_candidate_model_run_id TEXT PRIMARY KEY, "
                "experience_candidate_id TEXT NOT NULL REFERENCES stage0_experience_candidate(experience_candidate_id), "
                "status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')), "
                "route_name TEXT NOT NULL, provider_name TEXT NOT NULL, model_name TEXT NOT NULL, "
                "input_hash TEXT NOT NULL, output_hash TEXT, envelope_json TEXT NOT NULL, "
                "data_identity TEXT NOT NULL, created_at TEXT NOT NULL)",
                (
                    "experience_candidate_model_run_id", "experience_candidate_id", "status",
                    "route_name", "provider_name", "model_name", "input_hash", "output_hash",
                    "envelope_json", "data_identity", "created_at",
                ),
            ),
            "stage0_confirmed_experience": (
                "CREATE TABLE stage0_confirmed_experience ("
                "experience_id TEXT PRIMARY KEY, "
                "experience_candidate_id TEXT NOT NULL UNIQUE REFERENCES stage0_experience_candidate(experience_candidate_id), "
                "domain_label TEXT NOT NULL, "
                "classification TEXT NOT NULL CHECK(classification IN ('shared_pattern', 'single_source_feature')), "
                "summary TEXT NOT NULL, applicable_when_json TEXT NOT NULL, method_json TEXT NOT NULL, "
                "source_refs_json TEXT NOT NULL, boundary_json TEXT NOT NULL, "
                "experience_layer TEXT NOT NULL DEFAULT 'section_method', "
                "use_positions_json TEXT NOT NULL DEFAULT '[\"body\"]', "
                "trigger_signals_json TEXT NOT NULL DEFAULT '[]', "
                "not_applicable_when_json TEXT NOT NULL DEFAULT '[]', "
                "status TEXT NOT NULL CHECK(status IN ('active', 'paused')), "
                "data_identity TEXT NOT NULL, confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL)",
                (
                    "experience_id", "experience_candidate_id", "domain_label", "classification",
                    "summary", "applicable_when_json", "method_json", "source_refs_json",
                    "boundary_json", "experience_layer", "use_positions_json", "trigger_signals_json",
                    "not_applicable_when_json", "status", "data_identity", "confirmed_by", "confirmed_at",
                ),
            ),
        }
        for table_name, (create_sql, columns) in table_definitions.items():
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            if row is None or stale_target not in str(row["sql"] or ""):
                continue
            legacy_name = f"{table_name}_legacy_fk"
            if self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (legacy_name,),
            ).fetchone() is not None:
                raise StateTransitionError(
                    "experience foreign-key repair found an unfinished legacy table"
                )
            column_list = ", ".join(columns)
            legacy_columns = {
                str(item["name"])
                for item in self.conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            legacy_defaults = {
                "experience_layer": "'section_method'",
                "use_positions_json": "'[\"body\"]'",
                "trigger_signals_json": "applicable_when_json",
                "not_applicable_when_json": "boundary_json",
            }
            select_list = ", ".join(
                name if name in legacy_columns else legacy_defaults.get(name, "NULL")
                for name in columns
            )
            with self.conn:
                self.conn.execute(f"ALTER TABLE {table_name} RENAME TO {legacy_name}")
                self.conn.execute(create_sql)
                self.conn.execute(
                    f"INSERT INTO {table_name} ({column_list}) "
                    f"SELECT {select_list} FROM {legacy_name}"
                )
                self.conn.execute(f"DROP TABLE {legacy_name}")

    def create_task(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if self.data_identity == "production":
            raise StateTransitionError("production formal topics must be submitted and explicitly confirmed separately")
        if actor_kind != "user":
            raise StateTransitionError("formal topic creation requires a user confirmation")
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("create_task", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, NULL, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "research_plan", "not_started", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)",
                (topic_version_id, task_id, "formal_topic", "approved", "topic_confirmed", self.data_identity, actor, now, 0),
            )
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            result = {"task_id": task_id, "topic_version_id": topic_version_id}
            self._receipt("create_task", idempotency_key, request, result)
            self._audit(task_id, "task_created", result)
        return result

    def _require_configured_domain(self, domain_label: str, *, context: str) -> str:
        normalized = str(domain_label or "").strip()
        if normalized not in formal_domain_labels():
            raise StateTransitionError(f"{context} requires a configured formal domain")
        return normalized

    def _require_owned_account_for_domain(self, *, domain_label: str, account_ref: str) -> None:
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            raise StateTransitionError(
                "the requested domain has no current owned-account configuration"
            )
        if activation is not None:
            configuration = self.get_cold_start_configuration(
                configuration_id=str(activation["configuration_id"])
            )
            row = self.conn.execute(
                "SELECT 1 FROM stage0_content_account "
                "WHERE content_account_id=? AND account_role='owned' "
                "AND external_account_ref=? AND data_identity=? LIMIT 1",
                (
                    configuration["owned_account_id"],
                    account_ref,
                    self.data_identity,
                ),
            ).fetchone()
        if row is None:
            raise StateTransitionError("the service account does not belong to the requested domain")

    def submit_formal_topic(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Create a user-supplied formal-topic version, awaiting confirmation."""
        if actor_kind != "user":
            raise StateTransitionError("formal topic submission requires a user actor")
        topic_domain = self._require_configured_domain(
            str(topic_payload.get("domain_label") or topic_payload.get("domain") or ""),
            context="formal topic submission",
        )
        topic_payload = {**topic_payload, "domain": topic_domain}
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("submit_formal_topic", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "formal_topic", topic_version_id, "awaiting_human_review", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    topic_version_id,
                    task_id,
                    "formal_topic",
                    "awaiting_human_review",
                    "formal_topic_payload",
                    "topic_submitted",
                    self.data_identity,
                    "",
                    now,
                    0,
                ),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "task_revision": "0"}
            self._receipt("submit_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_submitted", result)
        return result

    def confirm_formal_topic(
        self,
        *,
        task_id: str,
        topic_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if actor_kind != "user":
            raise StateTransitionError("only a user may confirm a formal topic")
        task, version = self._task(task_id), self._version(topic_version_id)
        topic_payload = self.get_artifact_payload(topic_version_id)["payload"]
        self._require_configured_domain(
            str(topic_payload.get("domain_label") or topic_payload.get("domain") or ""),
            context="formal topic confirmation",
        )
        self._assert_current_node(task, "formal_topic", "awaiting_human_review", topic_version_id)
        if version["node"] != "formal_topic":
            raise StateTransitionError("version is not a formal topic")
        request = {"task_id": task_id, "topic_version_id": topic_version_id, "actor": actor, "reason": reason}
        replay = self._replay("confirm_formal_topic", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            self._set_task(task_id, node="research_plan", version_id=topic_version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": "research_plan", "task_revision": str(revision)}
            self._receipt("confirm_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_confirmed", result)
        return result

    def create_input_assembly(self, assembly: InputAssembly, *, idempotency_key: str) -> dict[str, str]:
        if assembly.node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{assembly.node} has no model-input assembly in Stage 0")
        task = self._task(assembly.task_id)
        if task["current_node"] != assembly.node or task["current_status"] not in {"not_started", "awaiting_human_review"}:
            raise StateTransitionError("input assembly may only be prepared for the current new or returned node")
        upstream = self._approved_upstream(task, assembly.node, assembly.upstream_version_id)
        request = assembly.payload()
        replay = self._replay("create_input_assembly", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("assembly"), _hash(request)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, assembly.task_id, assembly.node, upstream, _canonical(request), integrity_hash, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash}
            self._receipt("create_input_assembly", idempotency_key, request, result)
            self._audit(assembly.task_id, "input_assembly_created", result)
        return result

    def create_node_request(
        self,
        *,
        task_id: str,
        node: str,
        input_assembly_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{node} is not a model-backed Stage 0 node")
        task = self._task(task_id)
        self._assert_current_node(task, node, "not_started")
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != node or assembly["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not belong to this task/node/identity")
        upstream = self._approved_upstream(task, node, assembly["upstream_version_id"])
        request = {"task_id": task_id, "node": node, "input_assembly_id": input_assembly_id, "actor": actor}
        replay = self._replay("create_node_request", idempotency_key, request)
        if replay:
            return replay
        version_id = _id("version")
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (version_id, task_id, node, upstream, input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=node, version_id=version_id, status="processing", revision=revision)
            result = {"node_version_id": version_id, "task_revision": str(revision)}
            self._receipt("create_node_request", idempotency_key, request, result)
            self._audit(task_id, "node_request_created", {**result, "node": node})
        return result

    def prepare_atomic_skill_binding(self, *, task_id: str, node_version_id: str) -> dict[str, Any]:
        """Return Core-owned receipt metadata; the atomic Skill alone creates the model request."""
        version = self._version(node_version_id)
        task = self._task(task_id)
        if version["task_id"] != task_id or version["node"] not in MODEL_BINDINGS:
            raise StateTransitionError("node version is not a model-backed version of this task")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        enforce_atomic_skill_runtime_guard(
            entrypoint="stage0_content_core.prepare_atomic_skill_binding",
            operation=FORMAL_SKILL_BY_NODE[version["node"]],
            data_identity=self.data_identity,
        )
        assembly = self._assembly(version["input_assembly_id"])
        payload = json.loads(assembly["payload_json"])
        return {
            "stage0_core": {
                "task_id": task_id,
                "node": version["node"],
                "node_version_id": node_version_id,
                "input_assembly_id": version["input_assembly_id"],
                "task_revision": int(task["task_revision"]),
                "prompt_version": payload["prompt_version"],
                "skill_version": payload["skill_version"],
                "data_identity": self.data_identity,
            }
        }

    def complete_node_from_model(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        output_ref: str,
        validation_status: str,
        actor: str,
        expected_task_revision: int,
        idempotency_key: str,
        artifact_payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        return self._complete_node_from_execution(
            task_id=task_id, node_version_id=node_version_id, model_run_id=model_run_id,
            output_ref=output_ref, validation_status=validation_status, actor=actor,
            expected_task_revision=expected_task_revision, idempotency_key=idempotency_key,
            artifact_payload=artifact_payload, expected_via_model_gateway=1,
            command_name="complete_node_from_model",
        )

    def complete_node_from_external_result(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        output_ref: str,
        validation_status: str,
        actor: str,
        expected_task_revision: int,
        idempotency_key: str,
        artifact_payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Accept one structured result supplied by an outside executor."""
        return self._complete_node_from_execution(
            task_id=task_id, node_version_id=node_version_id, model_run_id=model_run_id,
            output_ref=output_ref, validation_status=validation_status, actor=actor,
            expected_task_revision=expected_task_revision, idempotency_key=idempotency_key,
            artifact_payload=artifact_payload, expected_via_model_gateway=0,
            command_name="complete_node_from_external_result",
        )

    def _complete_node_from_execution(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        output_ref: str,
        validation_status: str,
        actor: str,
        expected_task_revision: int,
        idempotency_key: str,
        artifact_payload: dict[str, Any] | None,
        expected_via_model_gateway: int,
        command_name: str,
    ) -> dict[str, str]:
        if validation_status != "passed":
            raise StateTransitionError("only schema-validated output may await human review")
        task, request_version = self._task(task_id), self._version(node_version_id)
        if int(task["task_revision"]) != expected_task_revision:
            raise StaleResultError("execution result is stale because the task revision changed")
        run = self._model_run(model_run_id)
        self._assert_current_node(task, request_version["node"], "processing", node_version_id)
        if run["task_id"] != task_id or run["node_version_id"] != node_version_id or run["status"] != "succeeded":
            raise ModelGatewayRequiredError("execution result is not the successful current run")
        if int(run["via_model_gateway"]) != expected_via_model_gateway or run["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("execution result does not match the expected execution boundary")
        request = {"task_id": task_id, "node_version_id": node_version_id, "model_run_id": model_run_id, "output_ref": output_ref}
        replay = self._replay(command_name, idempotency_key, request)
        if replay:
            return replay
        output_version_id, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (output_version_id, task_id, request_version["node"], node_version_id, request_version["upstream_version_id"], request_version["input_assembly_id"], "awaiting_human_review", output_ref, "passed", self.data_identity, actor, _now(), revision),
            )
            if artifact_payload is not None:
                if request_version["node"] not in ARTIFACT_NODES:
                    raise StateTransitionError("artifact payload persistence is only enabled for formal artifact nodes")
                self._insert_artifact_payload(output_version_id, request_version["node"], artifact_payload)
            self.conn.execute("UPDATE stage0_model_run SET output_version_id=? WHERE model_run_id=?", (output_version_id, model_run_id))
            self._set_task(task_id, node=request_version["node"], version_id=output_version_id, status="awaiting_human_review", revision=revision)
            result = {"node_version_id": output_version_id, "task_revision": str(revision)}
            self._receipt(command_name, idempotency_key, request, result)
            self._audit(task_id, "external_output_awaiting_human_review" if expected_via_model_gateway == 0 else "model_output_awaiting_human_review", {**result, "model_run_id": model_run_id})
        return result

    def record_external_node_execution(
        self,
        *,
        task_id: str,
        node_version_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output_payload: dict[str, Any],
    ) -> str:
        """Record an outside execution as an audit fact, without a model call."""
        task, version = self._task(task_id), self._version(node_version_id)
        execution_id, executor_id = str(execution_id or "").strip(), str(executor_id or "").strip()
        if not execution_id or not executor_id or not isinstance(output_payload, dict):
            raise StateTransitionError("external result requires execution identity and structured fields")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if version["task_id"] != task_id:
            raise StateTransitionError("external result does not belong to the current task")
        existing = self.conn.execute(
            "SELECT model_run_id FROM stage0_model_run WHERE task_id=? AND node_version_id=? AND input_assembly_id=?",
            (task_id, node_version_id, version["input_assembly_id"]),
        ).fetchone()
        if existing is not None:
            raise StateTransitionError("this intelligent input already has an execution result")
        assembly = self._assembly(str(version["input_assembly_id"]))
        assembly_payload = json.loads(str(assembly["payload_json"]))
        model_run_id = _id("external_model_run")
        model_ref = str(model_ref or "not_reported").strip() or "not_reported"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, task_id, version["node"], node_version_id, version["input_assembly_id"],
                    "succeeded", execution_id, str(assembly_payload.get("prompt_version") or version["node"]),
                    str(assembly_payload.get("skill_version") or FORMAL_SKILL_BY_NODE.get(version["node"], version["node"])),
                    f"external.{version['node']}", EXTERNAL_INTELLIGENCE_EXECUTION_VERSION,
                    executor_id, "external_executor", model_ref, str(assembly["integrity_hash"]), _hash(output_payload),
                    None, "not_validated", _canonical({}), "not_retried", _canonical({}), _canonical({}), 0,
                    self.data_identity, _now(), 0,
                ),
            )
            self._audit(task_id, "external_intelligence_result_recorded", {
                "model_run_id": model_run_id, "execution_id": execution_id,
                "executor_id": executor_id, "model_ref": model_ref, "submitted_at": submitted_at,
            })
        return model_run_id

    def approve_current_node(self, *, task_id: str, version_id: str, actor: str, actor_kind: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, version = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, version["node"], "awaiting_human_review", version_id)
        topic_payload = self.get_artifact_payload(str(task["topic_version_id"]))["payload"]
        domain_label = str(topic_payload.get("domain_label") or topic_payload.get("domain") or "").strip()
        workflow_mode = get_content_workflow_mode(domain_label) if domain_label else "manual_guard"
        user_only_nodes = {"research_plan", "content_plan", "review"}
        if workflow_mode == "manual_guard":
            user_only_nodes.update({"deep_research", "formal_draft"})
        if version["node"] in user_only_nodes and actor_kind != "user":
            raise StateTransitionError("this confirmation point requires an explicit user decision")
        request = {"task_id": task_id, "version_id": version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("approve_current_node", idempotency_key, request)
        if replay:
            return replay
        decision_node = CONFIRMATION_NODE.get(version["node"], version["node"])
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, decision_node, version_id, "approved", actor, actor_kind, reason)
            next_node = NEXT_ARTIFACT_NODE.get(version["node"])
            if next_node is None:
                self._set_task(task_id, node="user_final_confirmation", version_id=version_id, status="approved", revision=revision)
            else:
                self._set_task(task_id, node=next_node, version_id=version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": next_node or "user_final_confirmation", "task_revision": str(revision)}
            self._receipt("approve_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_approved", {**result, "decision_node": decision_node})
        return result

    def return_current_node(self, *, task_id: str, version_id: str, input_assembly_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, previous = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, previous["node"], "awaiting_human_review", version_id)
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != previous["node"] or assembly["upstream_version_id"] != previous["upstream_version_id"]:
            raise StateTransitionError("returned node needs a new matching input assembly")
        request = {"task_id": task_id, "version_id": version_id, "input_assembly_id": input_assembly_id, "actor": actor, "reason": reason}
        replay = self._replay("return_current_node", idempotency_key, request)
        if replay:
            return replay
        replacement, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, previous["node"], version_id, "returned", actor, "user", reason)
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (replacement, task_id, previous["node"], version_id, previous["upstream_version_id"], input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=previous["node"], version_id=replacement, status="processing", revision=revision)
            result = {"node_version_id": replacement, "task_revision": str(revision)}
            self._receipt("return_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_returned_new_version", {**result, "returned_version_id": version_id})
        return result

    def requeue_failed_node_for_manual_retry(
        self,
        *,
        task_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Requeue one failed node only after an explicit user retry request."""
        task = self._task(task_id)
        if task["current_status"] != "failed":
            raise StateTransitionError("manual retry is available only for a failed node")
        failed_version = self._version(str(task["current_version_id"]))
        if failed_version["node"] != task["current_node"] or failed_version["status"] != "failed":
            raise StateTransitionError("the current failed node version is inconsistent")
        upstream_version_id = str(failed_version["upstream_version_id"] or "")
        self._approved_upstream(task, str(failed_version["node"]), upstream_version_id)
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("manual retry requires the user and a reason")
        request = {
            "task_id": task_id,
            "node": str(failed_version["node"]),
            "failed_version_id": str(failed_version["version_id"]),
            "actor": actor,
            "reason": reason,
        }
        replay = self._replay("manual_retry_failed_node", idempotency_key, request)
        if replay:
            return {str(key): str(value) for key, value in replay.items()}
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._set_task(
                task_id,
                node=str(failed_version["node"]),
                version_id=upstream_version_id,
                status="not_started",
                revision=revision,
            )
            result = {
                "task_id": task_id,
                "current_node": str(failed_version["node"]),
                "current_status": "not_started",
                "task_revision": str(revision),
            }
            self._receipt("manual_retry_failed_node", idempotency_key, request, result)
            self._audit(
                task_id,
                "manual_retry_requested",
                {**result, "failed_version_id": str(failed_version["version_id"]), "automatic_retry": False},
            )
        return result

    def cancel_current_task(self, *, task_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task = self._task(task_id)
        if task["current_status"] in {"approved", "cancelled"}:
            raise StateTransitionError("task is already terminal")
        request = {"task_id": task_id, "actor": actor, "reason": reason}
        replay = self._replay("cancel_current_task", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute("UPDATE stage0_content_task SET current_status='cancelled', task_revision=?, cancelled_reason=? WHERE task_id=?", (revision, reason, task_id))
            result = {"task_id": task_id, "task_revision": str(revision)}
            self._receipt("cancel_current_task", idempotency_key, request, result)
            self._audit(task_id, "task_cancelled", result)
        return result

    def record_external_experience_candidate_execution(
        self,
        *,
        experience_candidate_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
    ) -> str:
        candidate = self._experience_candidate(experience_candidate_id)
        if str(candidate["status"]) != "preparing":
            raise StateTransitionError("experience candidate is not ready for an external result")
        execution_id, executor_id = str(execution_id or "").strip(), str(executor_id or "").strip()
        if not execution_id or not executor_id or not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
            raise StateTransitionError("external experience result needs identity and structured fields")
        model_run_id = _id("experience_external_execution")
        model_ref = str(model_ref or "not_reported").strip() or "not_reported"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, experience_candidate_id, "succeeded",
                    "external.experience_candidate_propose", executor_id, model_ref,
                    _hash(input_payload), _hash(output_payload),
                    _canonical({"execution_id": execution_id, "executor_id": executor_id, "model_ref": model_ref, "submitted_at": submitted_at}),
                    self.data_identity, _now(),
                ),
            )
            self._audit(candidate["task_id"], "external_experience_candidate_result_recorded", {
                "experience_candidate_id": experience_candidate_id,
                "experience_candidate_model_run_id": model_run_id,
                "execution_id": execution_id,
                "executor_id": executor_id,
                "model_ref": model_ref,
            })
        return model_run_id

    def record_external_competitor_execution(
        self,
        *,
        registration_id: str,
        source_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
    ) -> str:
        registration = self.get_competitor_registration(registration_id=registration_id)
        if not str(source_id or "").strip() or not str(execution_id or "").strip() or not str(executor_id or "").strip():
            raise StateTransitionError("external competitor result needs registration, source and execution identity")
        material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND status='completed' AND data_identity=?",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if material is None:
            raise StateTransitionError("external competitor result requires retained spoken material")
        if not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
            raise StateTransitionError("external competitor result must be structured fields")
        model_run_id = _id("competitor_external_execution")
        model_ref = str(model_ref or "not_reported").strip() or "not_reported"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, registration_id, "breakdown", "succeeded",
                    "external.competitor_breakdown", executor_id, model_ref,
                    _hash(input_payload), _hash(output_payload), _canonical({}), _canonical({}), _canonical({}), 0,
                    self.data_identity, _now(),
                ),
            )
            self._audit(None, "external_competitor_breakdown_result_recorded", {
                "registration_id": registration_id, "source_id": source_id,
                "registration_status": registration["status"], "model_run_id": model_run_id,
                "execution_id": execution_id, "executor_id": executor_id,
                "model_ref": model_ref, "submitted_at": submitted_at,
            })
        return model_run_id

    def record_external_daily_hit_execution(
        self,
        *,
        hit_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
    ) -> str:
        hit = self.conn.execute(
            "SELECT preparation_status FROM hits WHERE hit_id=? AND data_identity=?",
            (hit_id, self.data_identity),
        ).fetchone()
        if hit is None or hit["preparation_status"] != "completed":
            raise StateTransitionError("external daily result requires completed hit material")
        if not str(execution_id or "").strip() or not str(executor_id or "").strip() or not isinstance(input_payload, dict) or not isinstance(output_payload, dict):
            raise StateTransitionError("external daily result needs identity and structured fields")
        model_run_id = _id("daily_hit_external_execution")
        model_ref = str(model_ref or "not_reported").strip() or "not_reported"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_daily_hit_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, hit_id, "succeeded", "external.competitor_breakdown",
                    executor_id, model_ref, _hash(input_payload), _hash(output_payload),
                    _canonical({"execution_id": str(execution_id), "submitted_at": submitted_at}),
                    _canonical({}), _canonical({}), 0, self.data_identity, _now(),
                ),
            )
            self._audit(None, "external_daily_hit_breakdown_result_recorded", {
                "hit_id": hit_id, "model_run_id": model_run_id,
                "execution_id": str(execution_id), "executor_id": str(executor_id),
                "model_ref": model_ref,
            })
        return model_run_id

    def _persist_experience_candidate_gateway_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("experience candidate model run identity does not match Core identity")
        candidate_id = str(binding.get("experience_candidate_id") or "")
        candidate = self.conn.execute(
            "SELECT status FROM stage0_experience_candidate WHERE experience_candidate_id=? AND data_identity=?",
            (candidate_id, self.data_identity),
        ).fetchone()
        if candidate is None or str(candidate["status"]) != "preparing":
            raise ModelGatewayRequiredError("experience candidate model run does not belong to an open candidate")
        model_run_id = _id("experience_candidate_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, candidate_id, envelope.status, envelope.route_name, envelope.provider_name,
                    envelope.model_name, envelope.input_hash, envelope.output_hash,
                    _canonical(envelope.as_payload()), self.data_identity, _now(),
                ),
            )
            self._audit(None, "experience_candidate_model_run_recorded", {
                "experience_candidate_id": candidate_id,
                "experience_candidate_model_run_id": model_run_id,
                "status": envelope.status,
            })
        return model_run_id

    def _persist_gateway_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway data identity does not match Core identity")
        task_id, node_version_id = str(binding.get("task_id") or ""), str(binding.get("node_version_id") or "")
        task, version = self._task(task_id), self._version(node_version_id)
        if int(binding.get("task_revision", -1)) != int(task["task_revision"]):
            raise StaleResultError("ModelGateway envelope belongs to a stale task revision")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if version["node"] != binding.get("node") or version["input_assembly_id"] != binding.get("input_assembly_id"):
            raise ModelGatewayRequiredError("ModelGateway envelope does not match the active node input")
        assembly = self._assembly(version["input_assembly_id"])
        model_run_id = _id("model_run")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_model_run(
                    model_run_id, task_id, node, node_version_id, input_assembly_id, status, request_id,
                    prompt_version, skill_version, route_id, route_version, provider_ref, provider_name,
                    model_name, input_integrity_hash, output_hash, output_version_id, validation_status,
                    error_json, retry_status, usage_json, cost_json, duration_ms, data_identity, created_at,
                    via_model_gateway
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_run_id, task_id, version["node"], node_version_id, version["input_assembly_id"],
                    envelope.status, envelope.correlation_id, binding["prompt_version"], binding["skill_version"],
                    envelope.route_id or envelope.route_name, envelope.config_version, envelope.provider_ref,
                    envelope.provider_name, envelope.model_name, assembly["integrity_hash"], envelope.output_hash,
                    None, "not_validated", _canonical(envelope.error or {}), "not_retried",
                    _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                    _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1,
                ),
            )
            self._audit(task_id, "model_gateway_envelope_recorded", {"model_run_id": model_run_id, "node": version["node"], "status": envelope.status})
        return model_run_id

    def _task(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_task WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("task does not exist in this data identity")
        return row

    def _version(self, version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_node_version WHERE version_id=?", (version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("node version does not exist in this data identity")
        return row

    def _assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not exist in this data identity")
        return row

    def _model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None:
            raise ModelGatewayRequiredError("model run does not exist")
        return row

    def _discovery_run(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_discovery_run WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery run does not exist in this data identity")
        return row

    def _validate_discovery_execution_mode(self, execution_mode: str) -> None:
        if execution_mode not in DISCOVERY_EXECUTION_MODES:
            raise StateTransitionError("discovery execution mode is invalid")
        if self.data_identity in NON_PRODUCTION_IDENTITIES and execution_mode != "test_isolated":
            raise DataIdentityError("non-production discovery data must use test_isolated mode")
        if self.data_identity == "production" and execution_mode == "test_isolated":
            raise DataIdentityError("production discovery data cannot use test_isolated mode")

    def _discovery_context_optional(self, run_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM stage1b_run_execution_context WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery execution context does not exist in this data identity")
        return row

    def _discovery_context(self, run_id: str) -> sqlite3.Row:
        row = self._discovery_context_optional(run_id)
        if row is None:
            raise StateTransitionError(
                "discovery run has no execution classification; it cannot be viewed or used"
            )
        return row

    def _discovery_source(self, source_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_source_version WHERE source_version_id=?", (source_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery source does not exist in this data identity")
        return row

    def _discovery_assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery input assembly does not exist in this data identity")
        return row

    def _discovery_model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("discovery model run does not exist in this data identity")
        return row

    def _discovery_candidate(self, candidate_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_candidate_version WHERE candidate_version_id=?", (candidate_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery candidate does not exist in this data identity")
        return row

    def _candidate_pool_state(self, candidate_version_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM stage1b_candidate_pool_state WHERE candidate_version_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, pool_state_id DESC LIMIT 1",
            (candidate_version_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("discovery candidate has no formal pool state")
        return row

    def _candidate_pool_parameter(self, as_of: str | None = None) -> sqlite3.Row:
        if as_of is None:
            row = self.conn.execute(
                "SELECT * FROM stage1b_candidate_pool_parameter_version WHERE data_identity=? "
                "ORDER BY effective_at DESC, parameter_version DESC LIMIT 1",
                (self.data_identity,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM stage1b_candidate_pool_parameter_version WHERE data_identity=? AND effective_at<=? "
                "ORDER BY effective_at DESC, parameter_version DESC LIMIT 1",
                (self.data_identity, as_of),
            ).fetchone()
        if row is None:
            raise StateTransitionError("candidate pool observation window is not configured")
        return row

    def find_related_discovery_candidates(
        self, *, domain_label: str, core_question: str, topic_angle: str
    ) -> dict[str, list[str]]:
        """Use the already-frozen candidate judgement to reuse the same question/angle instead of silently duplicating it."""
        same_angle: list[str] = []
        different_angle: list[str] = []
        rows = self.conn.execute(
            "SELECT candidate_version_id, payload_json FROM stage1b_candidate_version "
            "WHERE domain_label=? AND data_identity=? AND status='awaiting_user_decision' "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision decision WHERE decision.candidate_version_id=stage1b_candidate_version.candidate_version_id)",
            (domain_label, self.data_identity),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not _topic_identity_matches(core_question, str(payload.get("core_question", ""))):
                continue
            if _topic_identity_matches(topic_angle, str(payload.get("topic_angle", ""))):
                same_angle.append(str(row["candidate_version_id"]))
            else:
                different_angle.append(str(row["candidate_version_id"]))
        return {"same_angle": same_angle, "different_angle": different_angle}

    def _resolve_discovery_model_route(self):
        router = ModelRouter.from_file()
        definition = router.routes.get("business_analysis")
        if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
            raise ModelBindingUnavailableError("daily discovery requires the explicit business topic_screening route")
        try:
            return router.resolve_bound_route(
                "business_analysis",
                route_name="business.source_to_topic",
                parameters={"stream": False},
            )
        except Exception as exc:
            raise ModelBindingUnavailableError("daily discovery has no explicit resolved model binding") from exc

    def _assert_discovery_source_provenance(
        self,
        *,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        payload: dict[str, Any],
    ) -> None:
        """Accept only a source whose precise formal origin still matches the Core facts."""
        origin = payload.get("formal_source")
        if not isinstance(origin, dict):
            raise StateTransitionError("daily discovery source requires a formal_source mapping")
        if source_type == "daily_competitor_content":
            row = self.conn.execute(
                "SELECT video.video_id, video.last_checked_at, video.publish_time, video.title, video.url, video.raw_json, "
                "video.raw_archive_ref, video.excluded_reason, account.domain_label, account.registration_status, "
                "account.source_config_ref FROM competitor_videos video JOIN competitor_accounts account "
                "ON account.account_id=video.account_id JOIN stage0_content_account formal_account "
                "ON formal_account.content_account_id=account.account_id AND formal_account.data_identity=? "
                "AND formal_account.account_role='competitor' AND formal_account.status='active' "
                "WHERE video.video_id=?",
                (self.data_identity, source_object_id),
            ).fetchone()
            expected_table, expected_version = "competitor_videos", "last_checked_at"
        elif source_type == "historical_high_signal":
            row = self.conn.execute(
                "SELECT hit.hit_id, hit.promoted_at, hit.publish_time, hit.title, hit.url, hit.judgment_confidence, "
                "video.raw_json, video.raw_archive_ref, video.excluded_reason, account.domain_label, "
                "account.registration_status, account.source_config_ref FROM hits hit "
                "JOIN competitor_videos video ON video.video_id=hit.video_id "
                "JOIN competitor_accounts account ON account.account_id=hit.account_id "
                "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
                "AND formal_account.data_identity=? AND formal_account.account_role='competitor' "
                "AND formal_account.status='active' WHERE hit.hit_id=?",
                (self.data_identity, source_object_id),
            ).fetchone()
            expected_table, expected_version = "hits", "promoted_at"
        elif source_type == "hotspot":
            row = self.conn.execute(
                "SELECT observation.*, run.status FROM trendradar_hotspot_observation observation "
                "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
                "WHERE observation.observation_id=? AND run.status IN ('completed', 'completed_with_failures')",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "trendradar_hotspot_observation", "observed_at"
        elif source_type == "tag_discovery":
            row = self.conn.execute(
                "SELECT video.*, tag.tag FROM discovered_external_videos video "
                "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
                "WHERE video.discovered_video_id=? AND video.domain_label=? AND tag.status='active'",
                (source_object_id, domain_label),
            ).fetchone()
            expected_table, expected_version = "discovered_external_videos", "discovered_at"
        elif source_type == "question_expansion":
            row = self.conn.execute(
                "SELECT source.* FROM stage1_question_expansion_source source "
                "JOIN stage1_question_expansion_qualification qualification "
                "ON qualification.expansion_id=source.expansion_id "
                "AND qualification.data_identity=source.data_identity "
                "AND qualification.status='qualified' "
                "WHERE source.expansion_id=? AND source.domain_label=? "
                "AND source.validation_outcome='supported' AND source.data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_question_expansion_source", "integrity_hash"
        elif source_type == "saved_user_direction":
            row = self.conn.execute(
                "SELECT * FROM stage1_saved_user_direction_source WHERE direction_id=? "
                "AND domain_label=? AND status='active' AND data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_saved_user_direction_source", "integrity_hash"
        else:
            raise StateTransitionError("daily discovery source type is not enabled in this Stage 1B slice")
        if row is None:
            raise StateTransitionError("daily discovery source is not registered in the formal source facts")
        if source_type == "hotspot":
            match_terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
            folded_title = str(row["title"]).casefold()
            if not payload.get("hotspot_event_cluster") and (
                not match_terms or not any(term.casefold() in folded_title for term in match_terms)
            ):
                raise StateTransitionError("hotspot does not match the versioned domain policy")
        if source_type == "tag_discovery":
            if bool(row["is_tracked_account"]):
                raise StateTransitionError("tag discovery source belongs to a tracked competitor account")
            if int(row["like_count"] or 0) < TAG_CANDIDATE_LIKE_FLOOR:
                raise StateTransitionError("tag discovery source does not meet the 10000-like candidate floor")
        if origin.get("table") != expected_table or origin.get("object_id") != source_object_id:
            raise StateTransitionError("daily discovery source mapping does not identify the formal origin")
        formal_version = str(row[expected_version])
        if origin.get("object_version") != formal_version or source_object_version != formal_version:
            raise StaleResultError("daily discovery source version does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            expected_time_field = "publish_time"
        elif source_type == "question_expansion":
            expected_time_field = "validated_at"
        elif source_type == "saved_user_direction":
            expected_time_field = "saved_at"
        else:
            expected_time_field = expected_version
        if source_time != str(row[expected_time_field]):
            raise StateTransitionError("daily discovery source time does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            if domain_label != str(row["domain_label"]):
                raise StateTransitionError("daily discovery source domain does not match the formal origin")
            if row["registration_status"] != "active" or not str(row["source_config_ref"] or "").strip():
                raise StateTransitionError("daily discovery source account is not qualified")
            if not str(row["raw_archive_ref"] or "").strip() or row["excluded_reason"] is not None:
                raise StateTransitionError("daily discovery source lacks qualified formal material")
            if source_type == "historical_high_signal" and row["judgment_confidence"] not in {"rough", "formal"}:
                raise StateTransitionError("historical source lacks a recorded high-signal qualification")
        if source_type in {"question_expansion", "saved_user_direction"}:
            if not str(row["core_question"] or "").strip():
                raise StateTransitionError("daily discovery source lacks a concrete core question")
            raw_payload = row["payload_json"]
            if source_type == "question_expansion":
                try:
                    stored_parent = json.loads(str(row["parent_source_ref_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise StateTransitionError("question expansion parent reference is unreadable") from exc
                supplied_parent = payload.get("parent_source_ref")
                if stored_parent != supplied_parent:
                    raise StaleResultError("question expansion parent reference does not match the formal source")
                parent_type = str(stored_parent.get("source_type") or "") if isinstance(stored_parent, dict) else ""
                parent_id = str(stored_parent.get("source_object_id") or "") if isinstance(stored_parent, dict) else ""
                if not parent_type or not parent_id:
                    raise StateTransitionError("question expansion parent reference is incomplete")
                if parent_type == "question_expansion":
                    raise StateTransitionError("question expansion cannot be derived from another question expansion")
                if parent_type not in {"hit_breakdown", "competitor_breakdown"}:
                    raise StateTransitionError("question expansion must be derived during a formal hit breakdown")
        else:
            if not str(row["title"] or "").strip() or not str(row["url"] or "").strip():
                raise StateTransitionError("daily discovery source lacks required formal material")
            raw_payload = row["raw_json"]
        try:
            raw_hash = _hash(json.loads(str(raw_payload)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("daily discovery source has unreadable formal raw payload") from exc
        if origin.get("raw_metadata_hash") != raw_hash:
            raise StaleResultError("daily discovery source raw payload does not match the formal origin")

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._task(task_id)
        return {key: row[key] for key in row.keys()}

    def get_node_version(self, version_id: str) -> dict[str, Any]:
        row = self._version(version_id)
        return {key: row[key] for key in row.keys()}

    def get_input_assembly_payload(self, assembly_id: str) -> dict[str, Any]:
        row = self._assembly(assembly_id)
        return json.loads(row["payload_json"])

    def find_command_replay(self, command: str, idempotency_key: str, request: dict[str, Any]) -> dict[str, str] | None:
        return self._replay(command, idempotency_key, request)

    def record_completed_command(
        self,
        *,
        command: str,
        idempotency_key: str,
        request: dict[str, Any],
        task_id: str,
        event: str,
        result: dict[str, str],
    ) -> None:
        """Persist an orchestration receipt and audit event through the Core boundary."""
        with self.conn:
            self._receipt(command, idempotency_key, request, result)
            self._audit(task_id, event, result)

    def record_model_validation_failure(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        reason: str,
        raw_model_output: str | None = None,
    ) -> None:
        """Record a rejected structured output without creating a business artifact."""
        self.fail_current_node_from_model(
            task_id=task_id,
            node_version_id=node_version_id,
            model_run_id=model_run_id,
            failure_stage="model_output_validation",
            reason=reason,
            raw_model_output=raw_model_output,
        )

    def fail_current_node_from_model(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str | None,
        failure_stage: str,
        reason: str,
        raw_model_output: str | None = None,
    ) -> None:
        """Make a model failure terminal and visible without fabricating an artifact."""
        task, version = self._task(task_id), self._version(node_version_id)
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if not failure_stage.strip() or not reason.strip():
            raise StateTransitionError("a model failure needs a stage and reason")
        model_run = self._model_run(model_run_id) if model_run_id is not None else None
        if model_run is not None and (
            model_run["data_identity"] != self.data_identity
            or model_run["task_id"] != task_id
            or model_run["node_version_id"] != node_version_id
        ):
            raise ModelGatewayRequiredError("model run does not belong to the active node request")
        failure = {
            "failure_stage": failure_stage,
            "reason": reason,
            "raw_model_output": raw_model_output,
            "raw_model_output_status": "available" if raw_model_output is not None else "not_available",
            "automatic_retry": False,
        }
        revision = int(task["task_revision"]) + 1
        failed_version_id = _id("version")
        with self.conn:
            if model_run_id is not None:
                self.conn.execute(
                    "UPDATE stage0_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?",
                    (_canonical(failure), model_run_id),
                )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    failed_version_id, task_id, version["node"], node_version_id,
                    version["upstream_version_id"], version["input_assembly_id"], "failed", "failed",
                    self.data_identity, "model_failure_record", _now(), revision,
                ),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id("node_failure"), task_id, failed_version_id, node_version_id,
                    model_run_id, failure_stage, reason, raw_model_output,
                    failure["raw_model_output_status"], self.data_identity, _now(),
                ),
            )
            self._set_task(
                task_id, node=str(version["node"]), version_id=failed_version_id,
                status="failed", revision=revision,
            )
            self._audit(task_id, "model_node_failed", {
                "node_version_id": failed_version_id,
                "request_version_id": node_version_id,
                "model_run_id": model_run_id,
                **failure,
            })

    def get_daily_run(self, *, daily_run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM stage0_daily_run WHERE daily_run_id=?",
            (str(daily_run_id).strip(),),
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else None

    def get_daily_run_for_domain_date(
        self, *, domain_label: str, business_date: str
    ) -> dict[str, Any] | None:
        domain = str(domain_label).strip()
        selected_date = str(business_date).strip()
        activation = self.get_current_domain_activation(domain_label=domain)
        if activation is not None:
            row = self.conn.execute(
                "SELECT * FROM stage0_daily_run WHERE domain_label=? "
                "AND business_date=? AND cold_start_id=?",
                (domain, selected_date, activation["cold_start_id"]),
            ).fetchone()
        else:
            row = None
        return {key: row[key] for key in row.keys()} if row is not None else None

    def get_or_create_daily_run(
        self, *, domain_label: str, business_date: str, actor: str
    ) -> dict[str, Any]:
        domain = str(domain_label).strip()
        selected_date = str(business_date).strip()
        if not domain:
            raise StateTransitionError("daily run requires a formal domain")
        try:
            date.fromisoformat(selected_date)
        except ValueError as exc:
            raise StateTransitionError("daily run requires a valid business date") from exc
        if not str(actor).strip():
            raise StateTransitionError("daily run requires an actor")
        activation = self.get_current_domain_activation(domain_label=domain)
        if activation is None:
            raise StateTransitionError("daily run requires a current cold-start activation")
        existing = self.get_daily_run_for_domain_date(
            domain_label=domain, business_date=selected_date
        )
        if existing is not None:
            return existing
        daily_run_id = _id("daily_run")
        created_at = _now()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO stage0_daily_run("
                "daily_run_id, domain_label, business_date, lifecycle, created_at, "
                "started_at, finished_at, data_identity, cold_start_id"
                ") VALUES (?, ?, ?, 'running', ?, NULL, NULL, ?, ?)",
                (
                    daily_run_id, domain, selected_date, created_at,
                    self.data_identity,
                    str(activation["cold_start_id"]) if activation is not None else None,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM stage0_daily_run WHERE domain_label=? "
                "AND business_date=? AND ((cold_start_id=? AND ? IS NOT NULL) "
                "OR (cold_start_id IS NULL AND ? IS NULL))",
                (
                    domain, selected_date,
                    str(activation["cold_start_id"]) if activation is not None else None,
                    str(activation["cold_start_id"]) if activation is not None else None,
                    str(activation["cold_start_id"]) if activation is not None else None,
                ),
            ).fetchone()
            if row is None:
                raise StateTransitionError("daily run could not be created")
            result = {key: row[key] for key in row.keys()}
            if str(row["daily_run_id"]) == daily_run_id:
                self._audit(
                    daily_run_id,
                    "daily_run_created",
                    {
                        "daily_run_id": daily_run_id,
                        "domain_label": domain,
                        "business_date": selected_date,
                        "actor": str(actor).strip(),
                    },
                )
        return result

    def _require_current_daily_run(self, run: dict[str, Any]) -> None:
        """Prevent a released activation's daily run from being resumed or finished."""
        domain = str(run.get("domain_label") or "").strip()
        activation = self.get_current_domain_activation(domain_label=domain)
        if activation is None or str(run.get("cold_start_id") or "") != str(
            activation["cold_start_id"]
        ):
            raise StateTransitionError(
                "the daily run does not belong to the domain's current activation"
            )

    def start_daily_run(
        self, *, daily_run_id: str, resume: bool, actor: str
    ) -> dict[str, Any]:
        run = self.get_daily_run(daily_run_id=daily_run_id)
        if run is None:
            raise StateTransitionError("daily run does not exist")
        self._require_current_daily_run(run)
        lifecycle = str(run["lifecycle"])
        if lifecycle == "completed":
            return run
        if lifecycle in {"failed", "stopped"} and not resume:
            return run
        if lifecycle not in {"running", "failed", "stopped"}:
            raise StateTransitionError("daily run lifecycle is invalid")
        if not str(actor).strip():
            raise StateTransitionError("daily run start requires an actor")
        started_at = _now()
        with self.conn:
            if lifecycle == "running":
                self.conn.execute(
                    "UPDATE stage0_daily_run SET started_at=COALESCE(started_at, ?) "
                    "WHERE daily_run_id=?",
                    (started_at, daily_run_id),
                )
                event = "daily_run_started"
            else:
                self.conn.execute(
                    "UPDATE stage0_daily_run SET lifecycle='running', started_at=?, "
                    "finished_at=NULL WHERE daily_run_id=?",
                    (started_at, daily_run_id),
                )
                event = "daily_run_resumed"
            self._audit(
                daily_run_id,
                event,
                {
                    "daily_run_id": daily_run_id,
                    "domain_label": run["domain_label"],
                    "business_date": run["business_date"],
                    "actor": str(actor).strip(),
                    "resume": bool(resume),
                },
            )
        refreshed = self.get_daily_run(daily_run_id=daily_run_id)
        if refreshed is None:
            raise StateTransitionError("daily run disappeared after start")
        return refreshed

    def finish_daily_run(
        self,
        *,
        daily_run_id: str,
        lifecycle: str,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if lifecycle not in {"failed", "stopped", "completed"}:
            raise StateTransitionError("daily run completion lifecycle is invalid")
        if not str(actor).strip():
            raise StateTransitionError("daily run completion requires an actor")
        run = self.get_daily_run(daily_run_id=daily_run_id)
        if run is None:
            raise StateTransitionError("daily run does not exist")
        self._require_current_daily_run(run)
        current = str(run["lifecycle"])
        if current == lifecycle:
            return run
        if current == "completed":
            raise StateTransitionError("completed daily run cannot change lifecycle")
        if current != "running":
            raise StateTransitionError("only a running daily run can finish")
        finished_at = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_daily_run SET lifecycle=?, finished_at=? "
                "WHERE daily_run_id=?",
                (lifecycle, finished_at, daily_run_id),
            )
            self._audit(
                daily_run_id,
                "daily_run_finished",
                {
                    "daily_run_id": daily_run_id,
                    "domain_label": run["domain_label"],
                    "business_date": run["business_date"],
                    "lifecycle": lifecycle,
                    "actor": str(actor).strip(),
                    "reason": str(reason or "").strip() or None,
                },
            )
        refreshed = self.get_daily_run(daily_run_id=daily_run_id)
        if refreshed is None:
            raise StateTransitionError("daily run disappeared after finish")
        return refreshed

    def daily_candidate_discovery_run(
        self, *, daily_run_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT run.run_id, run.discovery_date, run.status, run.completed_at, "
            "run.failure_reason, context.lifecycle_status, context.execution_mode "
            "FROM stage1b_run_execution_context context "
            "JOIN stage1b_discovery_run run ON run.run_id=context.run_id "
            "WHERE context.daily_run_id=? AND context.execution_mode='production_daily' "
            "AND run.data_identity=? ORDER BY run.created_at DESC, run.run_id DESC LIMIT 1",
            (str(daily_run_id).strip(), self.data_identity),
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else None

    def reconcile_prior_daily_discovery_for_resume(
        self,
        *,
        daily_run_id: str,
        prior_daily_lifecycle: str,
        resume_started_at: str,
        execution_attempt_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reconcile only prior processing discovery children during explicit resume."""
        normalized_daily_run_id = str(daily_run_id or "").strip()
        normalized_prior_lifecycle = str(prior_daily_lifecycle or "").strip()
        normalized_started_at = str(resume_started_at or "").strip()
        normalized_attempt_ref = str(execution_attempt_ref or "").strip()
        if not normalized_daily_run_id or not normalized_started_at or not normalized_attempt_ref:
            raise StateTransitionError(
                "daily resume discovery reconciliation requires the exact daily run, start time, and execution attempt"
            )
        if normalized_prior_lifecycle not in {"failed", "stopped"}:
            return {
                "daily_run_id": normalized_daily_run_id,
                "prior_daily_lifecycle": normalized_prior_lifecycle,
                "resume_execution_attempt": normalized_attempt_ref,
                "finalized_run_ids": [],
                "finalized_count": 0,
                "skipped": "parent daily run was not failed or stopped before resume",
            }
        daily_run = self.get_daily_run(daily_run_id=normalized_daily_run_id)
        if daily_run is None:
            raise StateTransitionError("daily run does not exist")
        if str(daily_run["lifecycle"]) != "running":
            raise StateTransitionError(
                "daily resume discovery reconciliation requires the resumed daily run to be running"
            )
        request = {
            "daily_run_id": normalized_daily_run_id,
            "prior_daily_lifecycle": normalized_prior_lifecycle,
            "resume_started_at": normalized_started_at,
            "execution_attempt_ref": normalized_attempt_ref,
        }
        replay = self._replay(
            "stage0_reconcile_prior_daily_discovery_for_resume",
            idempotency_key,
            request,
        )
        if replay:
            return replay
        rows = self.conn.execute(
            "SELECT run.run_id, run.created_at, run.failure_reason "
            "FROM stage1b_discovery_run run "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "WHERE context.daily_run_id=? AND context.execution_mode='production_daily' "
            "AND run.data_identity=? AND context.data_identity=? "
            "AND run.status='processing' AND context.lifecycle_status='processing' "
            "AND run.created_at < ? "
            "ORDER BY run.created_at, run.run_id",
            (
                normalized_daily_run_id,
                self.data_identity,
                self.data_identity,
                normalized_started_at,
            ),
        ).fetchall()
        finalized: list[dict[str, Any]] = []
        for row in rows:
            domain_rows = self.conn.execute(
                "SELECT domain_label FROM stage1b_run_domain_scope "
                "WHERE run_id=? AND data_identity=? ORDER BY domain_label",
                (row["run_id"], self.data_identity),
            ).fetchall()
            domains = tuple(str(domain_row["domain_label"]) for domain_row in domain_rows)
            if not domains or str(daily_run["domain_label"]) not in domains:
                raise StateTransitionError(
                    "prior discovery run does not match the resumed daily domain"
                )
            failure_reason = str(row["failure_reason"] or "").strip()
            if not failure_reason:
                audit_row = self.conn.execute(
                    "SELECT payload_json FROM stage0_audit_event "
                    "WHERE task_id=? AND action='daily_run_finished' AND data_identity=? "
                    "ORDER BY created_at DESC, audit_id DESC LIMIT 1",
                    (normalized_daily_run_id, self.data_identity),
                ).fetchone()
                if audit_row is not None:
                    try:
                        audit_payload = json.loads(str(audit_row["payload_json"]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        audit_payload = {}
                    if isinstance(audit_payload, dict):
                        failure_reason = str(audit_payload.get("reason") or "").strip()
            if not failure_reason:
                failure_reason = (
                    "prior daily execution ended before candidate discovery failure finalization"
                )
            finalized.append(
                self.complete_discovery_run(
                    run_id=str(row["run_id"]),
                    domains=domains,
                    lifecycle_status="failed",
                    failure_reason=failure_reason,
                    idempotency_key=f"{idempotency_key}:discovery:{row['run_id']}",
                )
            )
        result = {
            "daily_run_id": normalized_daily_run_id,
            "prior_daily_lifecycle": normalized_prior_lifecycle,
            "resume_started_at": normalized_started_at,
            "resume_execution_attempt": normalized_attempt_ref,
            "finalized_run_ids": [item["run_id"] for item in finalized],
            "finalized_count": len(finalized),
        }
        with self.conn:
            self._receipt(
                "stage0_reconcile_prior_daily_discovery_for_resume",
                idempotency_key,
                request,
                result,
            )
            self._audit(
                normalized_daily_run_id,
                "daily_resume_prior_discovery_reconciled",
                result,
            )
        return result

    def daily_collection_account_ids(self, *, daily_run_id: str) -> set[str]:
        normalized_run_id = str(daily_run_id).strip()
        if not normalized_run_id:
            return set()
        rows = self.conn.execute(
            "SELECT account_id FROM daily_collection_account_completion "
            "WHERE daily_run_id=? ORDER BY account_id",
            (normalized_run_id,),
        ).fetchall()
        return {
            str(row["account_id"]).strip()
            for row in rows
            if str(row["account_id"] or "").strip()
        }

    def create_discovery_run(
        self,
        *,
        discovery_date: str,
        actor: str,
        execution_mode: str,
        domains: tuple[str, ...],
        idempotency_key: str,
        daily_run_id: str | None = None,
    ) -> dict[str, str]:
        self._validate_discovery_execution_mode(execution_mode)
        domain_scope = tuple(str(domain).strip() for domain in domains)
        if not domain_scope or any(not domain for domain in domain_scope):
            raise StateTransitionError("discovery run requires at least one formal domain")
        if len(set(domain_scope)) != len(domain_scope):
            raise StateTransitionError("discovery run domain scope may not contain duplicates")
        if execution_mode == "production_daily" and not str(daily_run_id or "").strip():
            raise StateTransitionError("production daily discovery requires a formal daily run")
        unknown_domains = sorted(set(domain_scope) - set(formal_domain_labels()))
        if unknown_domains:
            raise StateTransitionError(
                f"discovery run contains unconfigured formal domains: {unknown_domains}"
            )
        request = {
            "discovery_date": discovery_date,
            "actor": actor,
            "execution_mode": execution_mode,
            "domains": list(domain_scope),
            "data_identity": self.data_identity,
            "daily_run_id": str(daily_run_id or "").strip() or None,
        }
        replay = self._replay("stage1b_create_discovery_run", idempotency_key, request)
        if replay:
            return replay
        run_id = _id("discovery_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_discovery_run VALUES (?, ?, 'processing', ?, ?, ?, NULL, NULL)",
                (run_id, discovery_date, self.data_identity, actor, _now()),
            )
            self.conn.execute(
                "INSERT INTO stage1b_run_execution_context("
                "run_id, execution_mode, lifecycle_status, classification_reason, "
                "classified_by, classified_at, data_identity, daily_run_id"
                ") VALUES (?, ?, 'processing', ?, ?, ?, ?, ?)",
                (
                    run_id,
                    execution_mode,
                    "run created with explicit execution mode",
                    actor,
                    _now(),
                    self.data_identity,
                    str(daily_run_id or "").strip() or None,
                ),
            )
            recorded_at = _now()
            self.conn.executemany(
                "INSERT INTO stage1b_run_domain_scope "
                "(run_id, domain_label, data_identity, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    (run_id, domain_label, self.data_identity, recorded_at)
                    for domain_label in domain_scope
                ),
            )
            result = {"run_id": run_id, "discovery_date": discovery_date}
            self._receipt("stage1b_create_discovery_run", idempotency_key, request, result)
            self._audit(
                run_id,
                "stage1b_discovery_run_started",
                {
                    **result,
                    "execution_mode": execution_mode,
                    "domains": list(domain_scope),
                    "daily_run_id": str(daily_run_id or "").strip() or None,
                },
            )
        return result

    def _question_expansion_material_refs(
        self, *, parent_source_ref: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Read the already-recorded parent material without doing research."""
        parent_type = str(parent_source_ref.get("source_type") or "").strip()
        parent_id = str(parent_source_ref.get("source_object_id") or "").strip()
        refs: list[dict[str, str]] = [{
            "kind": "parent_source",
            "ref": f"{parent_type}:{parent_id}",
        }]
        if parent_type == "competitor_breakdown":
            registration_id = str(parent_source_ref.get("registration_id") or "").strip()
            if not registration_id:
                return []
            row = self.conn.execute(
                "SELECT artifact_json FROM stage0_competitor_registration_item "
                "WHERE registration_id=? AND step_name='transcripts_and_comments' "
                "AND item_ref=? AND status='completed' AND data_identity=? LIMIT 1",
                (registration_id, parent_id, self.data_identity),
            ).fetchone()
            if row is None:
                return []
            try:
                material = json.loads(str(row["artifact_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
            if not isinstance(material, dict):
                return []
            for key in ("source_url", "transcript_ref", "comment_collection_ref"):
                value = str(material.get(key) or "").strip()
                if value:
                    refs.append({"kind": key, "ref": value})
            return refs
        if parent_type == "hit_breakdown":
            row = self.conn.execute(
                "SELECT hit.url, analysis.artifact_json "
                "FROM stage0_daily_hit_breakdown analysis "
                "LEFT JOIN hits hit ON hit.hit_id=analysis.hit_id "
                "WHERE analysis.hit_id=? AND analysis.data_identity=? "
                "ORDER BY analysis.version DESC LIMIT 1",
                (parent_id, self.data_identity),
            ).fetchone()
            if row is None:
                return []
            url = str(row["url"] or "").strip()
            if url:
                refs.append({"kind": "source_url", "ref": url})
            try:
                artifact = json.loads(str(row["artifact_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                artifact = {}
            if isinstance(artifact, dict):
                for key in ("transcript_ref", "comment_collection_ref"):
                    value = str(artifact.get(key) or "").strip()
                    if value:
                        refs.append({"kind": key, "ref": value})
            return refs
        return []

    @staticmethod
    def _question_expansion_requires_external_check(reason: str) -> bool:
        """Detect an explicitly unverified lead without judging its subject matter."""
        return any(
            marker in str(reason or "")
            for marker in ("待核实", "有待确认", "需要后续研究核实", "尚需核实")
        )

    def _minimum_external_check(
        self,
        *,
        question: str,
        external_probe: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Do one bounded premise/material check, never formal research."""
        if external_probe is not None:
            try:
                result = external_probe(question)
            except Exception as exc:  # provider failure is unresolved, not rejection
                return {
                    "status": "unresolved",
                    "rejection_reason": "",
                    "material_refs": [],
                    "checks": {
                        "external_check": "blocked",
                        "blocked_reason": "external_search_unavailable",
                        "error_type": type(exc).__name__,
                    },
                }
            if not isinstance(result, dict):
                return {
                    "status": "blocked",
                    "rejection_reason": "",
                    "material_refs": [],
                    "checks": {
                        "external_check": "blocked",
                        "blocked_reason": "external_probe_invalid_result",
                    },
                }
            status = str(result.get("status") or "").strip().casefold()
            if status in {"passed", "qualified"}:
                material_refs = [
                    item for item in (result.get("material_refs") or [])
                    if isinstance(item, dict) and str(item.get("ref") or "").strip()
                ]
                if not material_refs:
                    return {
                        "status": "rejected",
                        "rejection_reason": "lead_no_reliable_public_material",
                        "material_refs": [],
                        "checks": {
                            "external_check": "completed_without_usable_material",
                        },
                    }
                return {
                    "status": "passed",
                    "rejection_reason": "",
                    "material_refs": material_refs,
                    "checks": dict(result.get("checks") or {}) | {
                        "external_check": "passed",
                    },
                }
            if status == "rejected":
                return {
                    "status": "rejected",
                    "rejection_reason": str(result.get("rejection_reason") or "lead_no_reliable_public_material"),
                    "material_refs": list(result.get("material_refs") or []),
                    "checks": dict(result.get("checks") or {}) | {
                        "external_check": "completed_no_support",
                    },
                }
            if status in {"unresolved", "blocked"}:
                return {
                    "status": status,
                    "rejection_reason": "",
                    "material_refs": list(result.get("material_refs") or []),
                    "checks": dict(result.get("checks") or {}) | {
                        "external_check": "blocked",
                        "blocked_reason": str(
                            (result.get("checks") or {}).get("blocked_reason")
                            or "external_search_unavailable"
                        ),
                    },
                }
            return {
                "status": "blocked",
                "rejection_reason": "",
                "material_refs": [],
                "checks": {
                    "external_check": "blocked",
                    "blocked_reason": "external_probe_invalid_status",
                },
            }

        try:
            from scripts.core.external_adapters.anysearch_executor import AnySearchExecutor

            executor = AnySearchExecutor(core=self)
            raw = executor._call("search", question, "--max_results", "5")
            results = executor._parse_results(raw)
        except Exception as exc:  # missing service, timeout, or network failure
            return {
                "status": "unresolved",
                "rejection_reason": "",
                "material_refs": [],
                "checks": {
                    "external_check": "blocked",
                    "blocked_reason": "external_search_unavailable",
                    "error_type": type(exc).__name__,
                },
            }

        generic_terms = {"是否", "确实", "存在", "关系", "合作", "音乐", "作品", "共同", "录制"}
        relation_groups: list[set[str]] = []
        for group in re.split(r"与|和|及|同|、|共同|合作|在|中的|是否|\band\b|\bwith\b|\bfeaturing\b|\bin\b", question, flags=re.IGNORECASE):
            terms = {
                token.casefold()
                for token in re.findall(r"[A-Za-z0-9]{2,}", group)
                if token.casefold() not in generic_terms
            }
            for run in re.findall(r"[\u4e00-\u9fff]{2,}", group):
                terms.update(
                    run[index:index + 2]
                    for index in range(len(run) - 1)
                    if run[index:index + 2] not in generic_terms
                )
            if terms:
                relation_groups.append(terms)
        material_refs: list[dict[str, str]] = []
        relation_supported = False
        for result in results:
            source_ref = str(result.get("source_ref") or "").strip()
            if not source_ref or AnySearchExecutor._is_formal_blocked_source(source_ref):
                continue
            evidence_text = " ".join(
                str(result.get(key) or "") for key in ("title", "snippet")
            ).casefold()
            matched_groups = sum(
                1 for terms in relation_groups
                if any(
                    re.search(
                        rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
                        evidence_text,
                    )
                    if re.fullmatch(r"[a-z0-9]+", term)
                    else term in evidence_text
                    for term in terms
                )
            )
            if matched_groups < 2:
                continue
            relation_supported = True
            material_refs.append({
                "kind": "external_public_source",
                "ref": source_ref,
                "title": str(result.get("title") or "").strip(),
                "snippet": str(result.get("snippet") or "").strip()[:500],
            })
            if len(material_refs) >= 2:
                break
        if not material_refs or not relation_supported:
            return {
                "status": "rejected",
                "rejection_reason": "lead_no_reliable_public_material",
                "material_refs": [],
                "checks": {
                    "external_check": "completed_without_same_source_relation_evidence",
                },
            }
        return {
            "status": "passed",
            "rejection_reason": "",
            "material_refs": material_refs,
            "checks": {
                "external_check": "passed",
                "minimum_premise": "reliable_public_material_found",
            },
        }

    def qualify_question_expansion_lead(
        self,
        *,
        domain_label: str,
        question: str,
        content_type: str,
        reason: str,
        parent_source_ref: dict[str, Any],
        existing_questions: list[str] | None = None,
        external_probe: Callable[[str], dict[str, Any]] | None = None,
        content_type_projected: bool = False,
        canonical_type_id: str | None = None,
        exclude_expansion_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply the small pre-candidate gate; this is not formal research."""
        checks: dict[str, str] = {}
        parent_type = str(parent_source_ref.get("source_type") or "").strip()
        parent_id = str(parent_source_ref.get("source_object_id") or "").strip()
        if parent_type not in {"hit_breakdown", "competitor_breakdown"} or not parent_id:
            return {"status": "rejected", "rejection_reason": "lead_parent_incomplete", "material_refs": [], "checks": checks}
        checks["parent_relation"] = "passed"
        if len(question.strip()) < 6 or not content_type.strip() or not reason.strip():
            return {"status": "rejected", "rejection_reason": "lead_missing_concrete_question", "material_refs": [], "checks": checks}

        material_refs = self._question_expansion_material_refs(parent_source_ref=parent_source_ref)
        if len(material_refs) < 2:
            checks["material_availability"] = "failed"
            return {"status": "rejected", "rejection_reason": "lead_material_unavailable", "material_refs": material_refs, "checks": checks}
        checks["material_availability"] = "passed"
        if self._question_expansion_requires_external_check(reason):
            external_result = self._minimum_external_check(
                question=question,
                external_probe=external_probe,
            )
            checks.update({f"minimum_external_{key}": value for key, value in external_result.get("checks", {}).items()})
            external_status = str(external_result.get("status") or "blocked")
            if external_status in {"unresolved", "blocked"}:
                return {
                    "status": external_status,
                    "rejection_reason": "",
                    "material_refs": material_refs + list(external_result.get("material_refs") or []),
                    "checks": checks,
                }
            if external_status == "rejected":
                return {
                    "status": "rejected",
                    "rejection_reason": str(external_result.get("rejection_reason") or "lead_no_reliable_public_material"),
                    "material_refs": material_refs + list(external_result.get("material_refs") or []),
                    "checks": checks,
                }
            material_refs.extend(external_result.get("material_refs") or [])
            checks["minimum_premise"] = "passed_by_bounded_external_check"
        else:
            checks["minimum_premise"] = "passed_from_registered_parent_material"

        if content_type_projected:
            projected_type = project_content_type(
                domain_label,
                lifecycle="classify",
                canonical_id=canonical_type_id or content_type,
            )
            if projected_type.get("status") != "matched":
                checks["approved_type_projection"] = "failed"
                return {
                    "status": "rejected",
                    "rejection_reason": "lead_missing_approved_type_projection",
                    "material_refs": material_refs,
                    "checks": checks,
                }
            checks["approved_type_projection"] = "passed"
            checks["domain_carrier"] = "delegated_to_approved_type_registry"
        else:
            try:
                domain_pack = get_domain_pack(domain_label)
            except ValueError:
                domain_pack = {}
            policy = domain_pack.get("question_expansion_policy") if isinstance(domain_pack, dict) else None
            if not isinstance(policy, dict) or not policy.get("primary_content_carrier_required") or not str(policy.get("primary_content_carrier_rule") or "").strip():
                checks["domain_carrier"] = "failed"
                return {"status": "rejected", "rejection_reason": "lead_domain_carrier_rule_missing", "material_refs": material_refs, "checks": checks}
            signal_terms = [
                str(term).strip().casefold()
                for term in (policy.get("primary_carrier_signal_terms") or [])
                if str(term).strip()
            ]
            if signal_terms and not any(term in question.casefold() for term in signal_terms):
                checks["domain_carrier"] = "failed"
                return {"status": "rejected", "rejection_reason": "lead_domain_carrier_not_evidenced", "material_refs": material_refs, "checks": checks}
            checks["domain_carrier"] = "legacy_domain_policy_and_minimum_signal"

        prior_questions = list(existing_questions or [])
        prior_query = (
            "SELECT core_question FROM stage1_question_expansion_source "
            "WHERE domain_label=? AND data_identity=?"
        )
        prior_params: list[Any] = [domain_label, self.data_identity]
        if exclude_expansion_id:
            prior_query += " AND expansion_id<>?"
            prior_params.append(exclude_expansion_id)
        prior_questions.extend(
            str(row["core_question"] or "").strip()
            for row in self.conn.execute(prior_query, tuple(prior_params)).fetchall()
        )
        if any(_topic_identity_matches(question, prior) for prior in prior_questions if prior):
            checks["independent_value"] = "failed"
            return {"status": "rejected", "rejection_reason": "lead_duplicate_or_repeated", "material_refs": material_refs, "checks": checks}
        checks["independent_value"] = "passed"
        return {"status": "qualified", "rejection_reason": "", "material_refs": material_refs, "checks": checks}

    def _record_question_expansion_qualification(
        self,
        *,
        expansion_id: str,
        domain_label: str,
        parent_source_ref: dict[str, Any],
        qualification: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        status = str(qualification.get("status") or "rejected")
        if status not in {"qualified", "rejected", "unresolved", "blocked"}:
            status = "blocked"
        reason = str(qualification.get("rejection_reason") or "")
        evaluated_at = _now()
        qualification_id = "question_expansion_qualification_" + _hash({
            "expansion_id": expansion_id,
            "data_identity": self.data_identity,
        })[:24]
        material_refs = list(qualification.get("material_refs") or [])
        checks = dict(qualification.get("checks") or {})
        existing = self.conn.execute(
            "SELECT * FROM stage1_question_expansion_qualification "
            "WHERE expansion_id=? AND data_identity=?",
            (expansion_id, self.data_identity),
        ).fetchone()
        retryable_existing = existing is not None and (
            str(existing["status"] or "") in {"unresolved", "blocked"}
            or str(existing["rejection_reason"] or "") == "lead_premise_requires_verification"
        )
        if existing is not None and not retryable_existing:
            try:
                stored_material_refs = json.loads(str(existing["material_refs_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored_material_refs = []
            try:
                stored_checks = json.loads(str(existing["checks_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored_checks = {}
            return {
                "qualification_id": str(existing["qualification_id"]),
                "status": str(existing["status"]),
                "rejection_reason": str(existing["rejection_reason"] or ""),
                "material_refs": stored_material_refs if isinstance(stored_material_refs, list) else [],
                "checks": stored_checks if isinstance(stored_checks, dict) else {},
                "evaluated_at": str(existing["evaluated_at"]),
            }
        with self.conn:
            if existing is None:
                self.conn.execute(
                    "INSERT INTO stage1_question_expansion_qualification "
                    "(qualification_id, expansion_id, domain_label, parent_source_ref_json, status, "
                    "rejection_reason, material_refs_json, checks_json, evaluated_at, data_identity, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        qualification_id, expansion_id, domain_label, _canonical(parent_source_ref), status,
                        reason, _canonical(material_refs), _canonical(checks), evaluated_at,
                        self.data_identity, actor,
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE stage1_question_expansion_qualification SET status=?, rejection_reason=?, "
                    "material_refs_json=?, checks_json=?, evaluated_at=?, created_by=? "
                    "WHERE expansion_id=? AND data_identity=?",
                    (
                        status, reason, _canonical(material_refs), _canonical(checks), evaluated_at, actor,
                        expansion_id, self.data_identity,
                    ),
                )
        return {
            "qualification_id": qualification_id,
            "status": status,
            "rejection_reason": reason,
            "material_refs": material_refs,
            "checks": checks,
            "evaluated_at": evaluated_at,
        }

    def qualify_pending_question_expansion_sources(
        self,
        *,
        actor: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Apply the existing qualification gate to recorded, unqualified leads."""
        query = (
            "SELECT source.* FROM stage1_question_expansion_source source "
            "LEFT JOIN stage1_question_expansion_qualification qualification "
            "ON qualification.expansion_id=source.expansion_id "
            "AND qualification.data_identity=source.data_identity "
            "WHERE source.data_identity=? AND qualification.expansion_id IS NULL "
            "ORDER BY source.validated_at ASC, source.expansion_id ASC"
        )
        params: list[Any] = [self.data_identity]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(query, tuple(params)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"] or "{}"))
            derivation = payload.get("derivation") if isinstance(payload.get("derivation"), dict) else {}
            parent_source_ref = payload.get("parent_source_ref")
            if not isinstance(parent_source_ref, dict):
                parent_source_ref = json.loads(str(row["parent_source_ref_json"] or "{}"))
            projection = derivation.get("content_type_projection")
            projection = projection if isinstance(projection, dict) else {}
            canonical_id = str(
                derivation.get("canonical_id")
                or projection.get("canonical_id")
                or ""
            ).strip()
            # Current production qualification is governed by the approved
            # projection.  Legacy labels are not a substitute for that
            # projection: using them here would let out-of-scope material
            # re-enter the V1 candidate path.
            content_type = canonical_id
            qualification = self.qualify_question_expansion_lead(
                domain_label=str(row["domain_label"]),
                question=str(row["core_question"]),
                content_type=content_type,
                reason=str(derivation.get("reason") or ""),
                parent_source_ref=parent_source_ref if isinstance(parent_source_ref, dict) else {},
                content_type_projected=True,
                canonical_type_id=canonical_id or None,
                exclude_expansion_id=str(row["expansion_id"]),
            )
            record = self._record_question_expansion_qualification(
                expansion_id=str(row["expansion_id"]),
                domain_label=str(row["domain_label"]),
                parent_source_ref=parent_source_ref if isinstance(parent_source_ref, dict) else {},
                qualification=qualification,
                actor=actor,
            )
            items.append({
                "expansion_id": str(row["expansion_id"]),
                "question": str(row["core_question"]),
                "status": record["status"],
                "rejection_reason": record.get("rejection_reason", ""),
                "checks": record.get("checks", {}),
            })
        counts = {status: sum(1 for item in items if item["status"] == status) for status in ("qualified", "rejected", "unresolved", "blocked")}
        return {"processed": len(items), "counts": counts, "items": items}

    def register_breakdown_question_expansions(
        self,
        *,
        domain_label: str,
        breakdown: dict[str, Any],
        parent_source_ref: dict[str, Any],
        actor: str = "competitor_breakdown",
        external_probe: Callable[[str], dict[str, Any]] | None = None,
        content_type_lifecycle: str = "discover",
    ) -> dict[str, Any]:
        """Persist only the questions emitted together with one hit breakdown."""
        if not isinstance(breakdown, dict):
            raise StateTransitionError("breakdown question expansions require a breakdown object")
        source_content_type = str(breakdown.get("source_content_type") or "").strip()
        if not source_content_type:
            raise StateTransitionError("breakdown question expansions require the source content type")
        lifecycle = str(content_type_lifecycle or "discover").strip().casefold()
        if lifecycle == "classify":
            supplied_type_id = str(breakdown.get("source_content_type_id") or "").strip()
            if supplied_type_id and supplied_type_id != source_content_type:
                raise StateTransitionError(
                    "production source_content_type_id must match the canonical source_content_type"
                )
        source_projection = project_content_type(
            domain_label,
            lifecycle=content_type_lifecycle,
            canonical_id=source_content_type,
        )
        if lifecycle == "classify" and source_projection["status"] != "matched":
            return {
                "status": "completed",
                "content_type_lifecycle": content_type_lifecycle,
                "source_projection": source_projection,
                "signals": [],
                "typed_leads": [],
                "created": [],
                "rejected": [],
                "unresolved": [],
                "blocked": [],
                "no_match": [{
                    "status": "NO_MATCH" if source_projection["status"] == "no_match" else "OUT_OF_SCOPE",
                    "reason": source_projection["status"],
                }],
                "count": 0,
                "rejected_count": 0,
                "unresolved_count": 0,
                "blocked_count": 0,
                "no_match_count": 1,
            }
        parent = dict(parent_source_ref or {})
        parent_type = str(parent.get("source_type") or "").strip()
        parent_id = str(parent.get("source_object_id") or "").strip()
        if parent_type not in {"hit_breakdown", "competitor_breakdown"} or not parent_id:
            raise StateTransitionError("question expansions must point to the completed hit breakdown")

        signals = breakdown.get("expansion_signals")
        typed_leads = breakdown.get("typed_expansion_leads")
        if signals is not None or typed_leads is not None:
            signals = [] if signals is None else signals
            typed_leads = [] if typed_leads is None else typed_leads
            if not isinstance(signals, list) or not isinstance(typed_leads, list):
                raise StateTransitionError("expansion signals and typed leads must be arrays")
            if len(typed_leads) > 3:
                raise StateTransitionError("one breakdown may contain at most three typed expansion leads")
            signal_ids: set[str] = set()
            normalized_signals: list[dict[str, Any]] = []
            for position, item in enumerate(signals, start=1):
                if not isinstance(item, dict):
                    raise StateTransitionError("expansion signal is not an object")
                signal_id = str(item.get("signal_id") or f"signal_{position:02d}").strip()
                signal_text = str(item.get("signal_text") or "").strip()
                if not signal_text or signal_id in signal_ids:
                    raise StateTransitionError("expansion signal needs unique identity and signal text")
                signal_ids.add(signal_id)
                normalized_signals.append({
                    "signal_id": signal_id,
                    "signal_kind": str(item.get("signal_kind") or "observation").strip(),
                    "signal_text": signal_text,
                    "source_anchor": str(item.get("source_anchor") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                    "status": "observed",
                })
            created: list[dict[str, str]] = []
            if lifecycle != "classify":
                return {
                    "status": "observed",
                    "content_type_lifecycle": content_type_lifecycle,
                    "signals": normalized_signals,
                    "typed_leads": typed_leads,
                    "created": [],
                    "rejected": [],
                    "unresolved": [],
                    "blocked": [],
                    "no_match": [],
                    "count": 0,
                    "rejected_count": 0,
                    "unresolved_count": 0,
                    "blocked_count": 0,
                    "no_match_count": 0,
                }

            rejected: list[dict[str, str]] = []
            unresolved: list[dict[str, str]] = []
            blocked: list[dict[str, str]] = []
            no_match: list[dict[str, str]] = []
            seen: set[str] = set()
            prior_batch_questions: list[str] = []
            for item in typed_leads:
                if not isinstance(item, dict):
                    raise StateTransitionError("typed expansion lead is not an object")
                signal_id = str(item.get("signal_id") or "").strip()
                question = str(item.get("core_question") or "").strip()
                reason = str(item.get("reason") or "").strip()
                canonical_id = str(item.get("canonical_id") or "").strip()
                if signal_id not in signal_ids:
                    no_match.append({
                        "signal_id": signal_id,
                        "status": "NO_MATCH",
                        "reason": "typed_lead_missing_expansion_signal",
                    })
                    continue
                projection = project_content_type(
                    domain_label,
                    lifecycle=content_type_lifecycle,
                    canonical_id=canonical_id,
                )
                if projection["status"] != "matched":
                    no_match.append({
                        "signal_id": signal_id,
                        "status": "NO_MATCH"
                        if projection["status"] in {"no_match", "observed", "registry_not_frozen"}
                        else "OUT_OF_SCOPE",
                        "reason": projection["status"],
                    })
                    continue
                if len(question) < 6 or not reason:
                    rejected.append({
                        "signal_id": signal_id,
                        "rejection_reason": "lead_missing_concrete_question",
                    })
                    continue
                identity = "".join(question.casefold().split())
                if identity in seen:
                    rejected.append({
                        "signal_id": signal_id,
                        "rejection_reason": "lead_duplicate_or_repeated",
                    })
                    continue
                seen.add(identity)
                expansion_id = "question_expansion_" + _hash({
                    "parent": parent,
                    "core_question": question,
                })[:24]
                qualification = self.qualify_question_expansion_lead(
                    domain_label=domain_label,
                    question=question,
                    content_type=canonical_id,
                    reason=reason,
                    parent_source_ref=parent,
                    existing_questions=prior_batch_questions,
                    external_probe=external_probe,
                    content_type_projected=True,
                    canonical_type_id=canonical_id,
                )
                prior_batch_questions.append(question)
                qualification_record = self._record_question_expansion_qualification(
                    expansion_id=expansion_id,
                    domain_label=domain_label,
                    parent_source_ref=parent,
                    qualification=qualification,
                    actor=actor,
                )
                if qualification_record["status"] == "rejected":
                    rejected.append({
                        "signal_id": signal_id,
                        "rejection_reason": str(qualification_record.get("rejection_reason") or "lead_not_qualified"),
                    })
                    continue
                if qualification_record["status"] == "unresolved":
                    unresolved.append({
                        "signal_id": signal_id,
                        "blocked_reason": str(qualification_record.get("checks", {}).get("blocked_reason") or "external_search_unavailable"),
                    })
                    continue
                if qualification_record["status"] == "blocked":
                    blocked.append({
                        "signal_id": signal_id,
                        "blocked_reason": str(qualification_record.get("checks", {}).get("blocked_reason") or "qualification_blocked"),
                    })
                    continue
                derivation = {
                    "stage": "competitor_breakdown",
                    "expansion_signal": next(
                        value for value in normalized_signals if value["signal_id"] == signal_id
                    ),
                    "content_type_projection": projection,
                    "canonical_id": canonical_id,
                    "reason": reason,
                    "qualification": qualification_record,
                }
                existing = self.conn.execute(
                    "SELECT core_question, parent_source_ref_json, integrity_hash, validated_at "
                    "FROM stage1_question_expansion_source WHERE expansion_id=? AND data_identity=?",
                    (expansion_id, self.data_identity),
                ).fetchone()
                if existing is None:
                    result = self.register_question_expansion_source(
                        expansion_id=expansion_id,
                        domain_label=domain_label,
                        core_question=question,
                        parent_source_ref=parent,
                        actor=actor,
                        derivation=derivation,
                    )
                else:
                    if str(existing["core_question"]) != question or str(existing["parent_source_ref_json"]) != _canonical(parent):
                        raise StateTransitionError("question expansion identity conflicts with an existing formal source")
                    result = {
                        "expansion_id": expansion_id,
                        "source_object_version": str(existing["integrity_hash"]),
                        "validated_at": str(existing["validated_at"]),
                    }
                created.append({**result, "canonical_id": canonical_id, "signal_id": signal_id})
            return {
                "status": "completed",
                "content_type_lifecycle": content_type_lifecycle,
                "signals": normalized_signals,
                "typed_leads": typed_leads,
                "created": created,
                "rejected": rejected,
                "unresolved": unresolved,
                "blocked": blocked,
                "no_match": no_match,
                "count": len(created),
                "rejected_count": len(rejected),
                "unresolved_count": len(unresolved),
                "blocked_count": len(blocked),
                "no_match_count": len(no_match),
            }
        expansions = breakdown.get("question_expansions")
        if expansions is None:
            return {"status": "not_present", "created": [], "count": 0}
        if lifecycle != "classify":
            return {
                "status": "observed",
                "content_type_lifecycle": content_type_lifecycle,
                "question_expansions": expansions,
                "created": [],
                "rejected": [],
                "unresolved": [],
                "blocked": [],
                "count": 0,
                "rejected_count": 0,
                "unresolved_count": 0,
                "blocked_count": 0,
            }

        if str(content_type_lifecycle).casefold() == "classify":
            return {
                "status": "completed",
                "content_type_lifecycle": content_type_lifecycle,
                "source_projection": source_projection,
                "created": [],
                "rejected": [],
                "unresolved": [],
                "blocked": [],
                "no_match": [{
                    "status": "OUT_OF_SCOPE",
                    "reason": "legacy_question_expansion_requires_approved_type_projection",
                }],
                "count": 0,
                "rejected_count": 0,
                "unresolved_count": 0,
                "blocked_count": 0,
                "no_match_count": 1,
            }
        if not isinstance(expansions, list) or len(expansions) > 3:
            raise StateTransitionError("one breakdown may contain at most three question expansions")
        created: list[dict[str, str]] = []
        rejected: list[dict[str, str]] = []
        unresolved: list[dict[str, str]] = []
        blocked: list[dict[str, str]] = []
        seen: set[str] = set()
        prior_batch_questions: list[str] = []
        for item in expansions:
            if not isinstance(item, dict):
                raise StateTransitionError("breakdown question expansion is not an object")
            question = str(item.get("core_question") or "").strip()
            content_type = str(item.get("content_type") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if len(question) < 6 or not content_type or not reason:
                raise StateTransitionError("breakdown question expansion is not a concrete question")
            identity = "".join(question.casefold().split())
            if identity in seen:
                raise StateTransitionError("breakdown question expansions must be distinct")
            seen.add(identity)
            expansion_id = "question_expansion_" + _hash({
                "parent": parent,
                "core_question": question,
            })[:24]
            qualification = self.qualify_question_expansion_lead(
                domain_label=domain_label,
                question=question,
                content_type=content_type,
                reason=reason,
                parent_source_ref=parent,
                existing_questions=prior_batch_questions,
                external_probe=external_probe,
            )
            prior_batch_questions.append(question)
            qualification_record = self._record_question_expansion_qualification(
                expansion_id=expansion_id,
                domain_label=domain_label,
                parent_source_ref=parent,
                qualification=qualification,
                actor=actor,
            )
            if qualification_record["status"] == "rejected":
                rejected.append({
                    "expansion_id": expansion_id,
                    "rejection_reason": str(qualification_record.get("rejection_reason") or "lead_not_qualified"),
                })
                continue
            if qualification_record["status"] == "unresolved":
                unresolved.append({
                    "expansion_id": expansion_id,
                    "blocked_reason": str(
                        qualification_record.get("checks", {}).get("blocked_reason")
                        or "external_search_unavailable"
                    ),
                })
                continue
            if qualification_record["status"] == "blocked":
                blocked.append({
                    "expansion_id": expansion_id,
                    "blocked_reason": str(
                        qualification_record.get("checks", {}).get("blocked_reason")
                        or "qualification_blocked"
                    ),
                })
                continue
            derivation = {
                "stage": "competitor_breakdown",
                "source_content_type": source_content_type,
                "canonical_source_content_type": canonicalize_competitor_content_type(source_content_type),
                "content_type": content_type,
                "reason": reason,
                "qualification": qualification_record,
            }
            existing = self.conn.execute(
                "SELECT core_question, parent_source_ref_json, integrity_hash, validated_at "
                "FROM stage1_question_expansion_source WHERE expansion_id=? AND data_identity=?",
                (expansion_id, self.data_identity),
            ).fetchone()
            if existing is None:
                result = self.register_question_expansion_source(
                    expansion_id=expansion_id,
                    domain_label=domain_label,
                    core_question=question,
                    parent_source_ref=parent,
                    actor=actor,
                    derivation=derivation,
                )
            else:
                if str(existing["core_question"]) != question or str(existing["parent_source_ref_json"]) != _canonical(parent):
                    raise StateTransitionError("question expansion identity conflicts with an existing formal source")
                result = {
                    "expansion_id": expansion_id,
                    "source_object_version": str(existing["integrity_hash"]),
                    "validated_at": str(existing["validated_at"]),
                }
            created.append(result)
        return {
            "status": "completed",
            "created": created,
            "rejected": rejected,
            "count": len(created),
            "rejected_count": len(rejected),
            "unresolved": unresolved,
            "unresolved_count": len(unresolved),
            "blocked": blocked,
            "blocked_count": len(blocked),
        }

    def observed_breakdown_content_types(self, *, domain_label: str) -> list[str]:
        """Return the content types already observed across completed breakdowns in one domain."""
        label = str(domain_label or "").strip()
        if not label:
            return []
        rows = self.conn.execute(
            "SELECT item.artifact_json FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "JOIN stage0_cold_start cold_start "
            "ON cold_start.cold_start_id=registration.cold_start_id "
            "AND cold_start.data_identity=registration.data_identity "
            "WHERE item.step_name='breakdown' AND item.status='completed' "
            "AND cold_start.domain_label=? AND item.data_identity=?",
            (label, self.data_identity),
        ).fetchall()
        daily_rows = self.conn.execute(
            "SELECT analysis.artifact_json FROM stage0_daily_hit_breakdown analysis "
            "JOIN hits hit ON hit.hit_id=analysis.hit_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "WHERE account.domain_label=? AND analysis.data_identity=?",
            (self.data_identity, label, self.data_identity),
        ).fetchall()
        values: set[str] = set()

        subject_labels = {
            "person": "人物",
            "work": "作品",
            "event": "事件",
            "concept": "概念",
            "case": "案例",
            "method": "方法",
            "collection": "合集",
        }
        expression_labels = {
            "story": "故事",
            "profile": "经历",
            "list": "盘点",
            "analysis": "解读",
            "explanation": "背景说明",
            "commentary": "观点评论",
            "event_response": "事件回应",
            "interview": "访谈",
        }

        def legacy_label(subject: str, expression: str) -> str:
            subject_label = subject_labels.get(subject, subject)
            expression_label = expression_labels.get(expression, expression)
            return f"{subject_label}{expression_label}"

        def collect(raw: Any) -> None:
            try:
                artifact = json.loads(str(raw or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return
            breakdown = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
            breakdown = breakdown if isinstance(breakdown, dict) else artifact
            if not isinstance(breakdown, dict):
                return
            observed = str(breakdown.get("source_content_type") or "").strip()
            if observed:
                values.add(canonicalize_competitor_content_type(observed))
                return
            subject = str(breakdown.get("content_subject_type") or "").strip()
            expression = str(breakdown.get("expression_form") or "").strip()
            if subject and expression and subject not in {"mixed", "unclear"} and expression not in {"mixed", "unclear"}:
                values.add(canonicalize_competitor_content_type(legacy_label(subject, expression)))

        for row in (*rows, *daily_rows):
            collect(row["artifact_json"])
        return sorted(values, key=str.casefold)

    def get_cold_start_content_type_observations(
        self, *, cold_start_id: str
    ) -> dict[str, Any]:
        """Read only successful breakdown observations from one cold-start run.

        This is deliberately separate from ``observed_breakdown_content_types``.
        The older helper is a production-context convenience lookup by domain;
        this lifecycle needs the exact run identity and must never read daily or
        neighboring-run breakdowns.
        """
        cold_start = self.conn.execute(
            "SELECT cold_start_id, domain_label, status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("cold start content-type observations require the current data identity")
        if str(cold_start["status"]) != "running":
            raise StateTransitionError("only running cold starts can produce content-type candidates")
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.artifact_json, "
            "registration.competitor_account_id, account.display_name "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "AND account.account_role='competitor' "
            "WHERE registration.cold_start_id=? AND item.data_identity=? "
            "AND item.step_name='breakdown' AND item.status='completed' "
            "ORDER BY registration.competitor_account_id, item.item_ref",
            (cold_start_id, self.data_identity),
        ).fetchall()
        observations: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []
        for row in rows:
            item_ref = str(row["item_ref"] or "").strip()
            try:
                artifact = json.loads(str(row["artifact_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                ignored.append({
                    "registration_id": str(row["registration_id"]),
                    "source_id": item_ref,
                    "reason": "breakdown_artifact_is_not_valid_json",
                })
                continue
            breakdown = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
            breakdown = breakdown if isinstance(breakdown, dict) else artifact
            if not isinstance(breakdown, dict):
                ignored.append({
                    "registration_id": str(row["registration_id"]),
                    "source_id": item_ref,
                    "reason": "breakdown_artifact_has_no_deep_breakdown_object",
                })
                continue
            raw_type = str(breakdown.get("source_content_type") or "").strip()
            source_id = str(breakdown.get("source_id") or item_ref).strip()
            if not raw_type:
                ignored.append({
                    "registration_id": str(row["registration_id"]),
                    "source_id": source_id,
                    "reason": "source_content_type_is_empty",
                })
                continue
            evidence = breakdown.get("content_type_evidence")
            observations.append({
                "source_id": source_id,
                "source_content_type": raw_type,
                "canonical_observed_type": canonicalize_competitor_content_type(raw_type),
                "content_subject_type": str(breakdown.get("content_subject_type") or "").strip(),
                "expression_form": str(breakdown.get("expression_form") or "").strip(),
                "content_type_evidence": evidence if isinstance(evidence, list) else [],
                "source_ref": {
                    "cold_start_id": cold_start_id,
                    "registration_id": str(row["registration_id"]),
                    "competitor_account_id": str(row["competitor_account_id"]),
                    "account_display_name": str(row["display_name"] or ""),
                    "source_id": source_id,
                    "item_ref": item_ref,
                    "data_identity": self.data_identity,
                },
            })
        return {
            "cold_start_id": cold_start_id,
            "domain_label": str(cold_start["domain_label"]),
            "cold_start_status": str(cold_start["status"]),
            "valid_observations": observations,
            "ignored_observations": ignored,
            "valid_count": len(observations),
            "ignored_count": len(ignored),
        }

    @staticmethod
    def _content_type_candidate_view(row: sqlite3.Row) -> dict[str, Any]:
        def read_json(name: str, fallback: Any) -> Any:
            try:
                value = json.loads(str(row[name] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return fallback
            return value

        proposal = read_json("proposal_json", {})
        source_snapshot = read_json("source_snapshot_json", [])
        failure = read_json("failure_json", {})
        review = read_json("review_json", {})
        provenance = read_json("freeze_provenance_json", {})
        candidates = proposal.get("candidates", []) if isinstance(proposal, dict) else []
        return {
            "content_type_candidate_id": str(row["content_type_candidate_id"]),
            "cold_start_id": str(row["cold_start_id"]),
            "domain_label": str(row["domain_label"]),
            "candidate_version": str(row["candidate_version"]),
            "status": str(row["status"]),
            "source_snapshot": source_snapshot if isinstance(source_snapshot, (dict, list)) else [],
            "proposal": proposal if isinstance(proposal, dict) else {},
            "candidates": candidates if isinstance(candidates, list) else [],
            "failure": failure if isinstance(failure, dict) else {},
            "review": review if isinstance(review, dict) else {},
            "freeze_provenance": provenance if isinstance(provenance, dict) else {},
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_reason": row["review_reason"],
        }

    def _latest_cold_start_content_type_candidate(
        self, *, cold_start_id: str
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM stage0_cold_start_content_type_candidate "
            "WHERE cold_start_id=? AND data_identity=? "
            "ORDER BY created_at DESC, content_type_candidate_id DESC LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()

    def get_cold_start_content_type_candidate(
        self, *, cold_start_id: str
    ) -> dict[str, Any] | None:
        row = self._latest_cold_start_content_type_candidate(cold_start_id=cold_start_id)
        return self._content_type_candidate_view(row) if row is not None else None

    @staticmethod
    def _validate_cold_start_content_type_proposal(
        *,
        observations: list[dict[str, Any]],
        proposal: dict[str, Any],
    ) -> None:
        candidates = proposal.get("candidates") if isinstance(proposal, dict) else None
        assignments = proposal.get("observation_assignments") if isinstance(proposal, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise StateTransitionError("content-type candidate proposal is empty")
        if not isinstance(assignments, list):
            raise StateTransitionError("content-type candidate proposal lacks observation assignments")
        observation_keys = {
            f"{item['source_ref']['registration_id']}:{item['source_id']}"
            for item in observations
        }
        candidate_ids: set[str] = set()
        candidate_source_keys: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise StateTransitionError("content-type candidate must be an object")
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            canonical_id = str(candidate.get("canonical_id") or "").strip()
            source_refs = candidate.get("source_refs")
            if not candidate_id or not canonical_id or candidate_id in candidate_ids:
                raise StateTransitionError("content-type candidates need unique identities")
            if not isinstance(source_refs, list) or not source_refs:
                raise StateTransitionError("every content-type candidate needs source evidence")
            candidate_ids.add(candidate_id)
            for source_ref in source_refs:
                if not isinstance(source_ref, dict):
                    raise StateTransitionError("content-type source evidence must be an object")
                key = f"{source_ref.get('registration_id')}:{source_ref.get('source_id')}"
                if key not in observation_keys or key in candidate_source_keys:
                    raise StateTransitionError("content-type candidate contains an unknown or repeated source")
                candidate_source_keys.add(key)
        assigned: set[str] = set()
        for assignment in assignments:
            if not isinstance(assignment, dict):
                raise StateTransitionError("content-type observation assignment must be an object")
            key = f"{assignment.get('registration_id')}:{assignment.get('source_id')}"
            candidate_id = str(assignment.get("candidate_id") or "").strip()
            if key not in observation_keys or key in assigned or candidate_id not in candidate_ids:
                raise StateTransitionError("content-type observation assignment is incomplete or invalid")
            assigned.add(key)
        if assigned != observation_keys or candidate_source_keys != observation_keys:
            raise StateTransitionError("every valid content-type observation must remain traceable")

    def build_cold_start_content_type_candidates(
        self, *, cold_start_id: str, actor: str | None = None
    ) -> dict[str, Any]:
        """Build one deterministic, run-scoped candidate proposal.

        No model is called here.  Existing deterministic normalization groups
        known equivalent observations; unknown observations remain separate so
        they cannot be silently merged or discarded.
        """
        existing = self._latest_cold_start_content_type_candidate(cold_start_id=cold_start_id)
        if existing is not None and str(existing["status"]) in {
            "preparing", "awaiting_human_decision", "accepted", "frozen",
        }:
            return self._content_type_candidate_view(existing)
        observations_payload = self.get_cold_start_content_type_observations(
            cold_start_id=cold_start_id
        )
        observations = list(observations_payload["valid_observations"])
        previous_rows = self.conn.execute(
            "SELECT candidate_version FROM stage0_cold_start_content_type_candidate "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchall()
        version_number = max(
            [
                int(str(row["candidate_version"])[1:])
                for row in previous_rows
                if str(row["candidate_version"]).startswith("v")
                and str(row["candidate_version"])[1:].isdigit()
            ]
            or [0]
        ) + 1
        candidate_version = f"v{version_number}"
        candidate_id = _id("content_type_candidate")
        now = _now()
        if not observations:
            failure = {
                "reason": "当前冷启动没有可用的成功逐条拆解内容类型观察",
                "retry_allowed": True,
                "model_used": False,
                "ignored_observations": observations_payload["ignored_observations"],
            }
            with self.conn:
                self.conn.execute(
                    "INSERT INTO stage0_cold_start_content_type_candidate "
                    "VALUES (?, ?, ?, ?, 'failed', ?, '{}', ?, '{}', '{}', ?, ?, ?, NULL, NULL, NULL)",
                    (
                        candidate_id, cold_start_id, observations_payload["domain_label"],
                        candidate_version, _canonical([]), _canonical(failure),
                        self.data_identity, "", now,
                    ),
                )
                self._audit(None, "cold_start_content_type_candidate_failed", {
                    "cold_start_id": cold_start_id,
                    "content_type_candidate_id": candidate_id,
                    "reason": failure["reason"],
                })
            return self.get_cold_start_content_type_candidate(cold_start_id=cold_start_id) or {}

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for observation in observations:
            grouped.setdefault(
                _content_type_group_key(str(observation["source_content_type"])),
                [],
            ).append(observation)
        candidates: list[dict[str, Any]] = []
        assignments: list[dict[str, Any]] = []
        for group_key, group_observations in sorted(grouped.items(), key=lambda item: item[0]):
            canonical_observation = str(group_key[1])
            display_name = (
                canonical_observation
                if group_key[0] == "canonical"
                else str(group_observations[0]["source_content_type"])
            )
            definition = _content_type_candidate_definition(
                display_name=display_name,
                observations=group_observations,
            )
            candidate = {
                "candidate_id": _content_type_candidate_id(
                    observations_payload["domain_label"], group_key
                ),
                "canonical_id": _content_type_candidate_canonical_id(
                    observations_payload["domain_label"], group_key
                ),
                "name": display_name,
                **definition,
                "source_observation_types": sorted(
                    {str(item["source_content_type"]) for item in group_observations},
                    key=str.casefold,
                ),
                "support_sample_count": len(group_observations),
                "representative_source_ids": [
                    str(item["source_id"]) for item in group_observations[:5]
                ],
                "source_refs": [dict(item["source_ref"]) for item in group_observations],
            }
            candidates.append(candidate)
            for item in group_observations:
                assignments.append({
                    "registration_id": item["source_ref"]["registration_id"],
                    "source_id": item["source_id"],
                    "source_content_type": item["source_content_type"],
                    "candidate_id": candidate["candidate_id"],
                })
        proposal = {
            "schema_version": "cold_start_content_type_candidate.v1",
            "model_used": False,
            "source_rule": "current_cold_start_successful_breakdown_observations_only",
            "candidates": candidates,
            "observation_assignments": assignments,
            "ignored_observations": observations_payload["ignored_observations"],
            "unmapped_observations": [],
        }
        self._validate_cold_start_content_type_proposal(
            observations=observations, proposal=proposal
        )
        source_snapshot = {
            "cold_start_id": cold_start_id,
            "domain_label": observations_payload["domain_label"],
            "data_identity": self.data_identity,
            "observations": observations,
            "ignored_observations": observations_payload["ignored_observations"],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_cold_start_content_type_candidate "
                "VALUES (?, ?, ?, ?, 'awaiting_human_decision', ?, ?, '{}', '{}', '{}', ?, ?, ?, NULL, NULL, NULL)",
                (
                    candidate_id, cold_start_id, observations_payload["domain_label"],
                    candidate_version, _canonical(source_snapshot), _canonical(proposal),
                    self.data_identity, "", now,
                ),
            )
            self._audit(None, "cold_start_content_type_candidates_built", {
                "cold_start_id": cold_start_id,
                "content_type_candidate_id": candidate_id,
                "candidate_version": candidate_version,
                "candidate_count": len(candidates),
                "observation_count": len(observations),
                "model_used": False,
            })
        return self.get_cold_start_content_type_candidate(cold_start_id=cold_start_id) or {}

    def review_cold_start_content_types(
        self,
        *,
        cold_start_id: str,
        decisions: tuple[dict[str, Any], ...],
        actor_kind: str,
        reason: str,
        decision_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        """Apply one whole-run user review and freeze the same formal registry."""
        if actor_kind != "user" or not reason.strip():
            raise StateTransitionError("content-type review requires an explicit user decision")
        current = self.get_cold_start_content_type_candidate(cold_start_id=cold_start_id)
        if current is None:
            raise StateTransitionError("content-type candidates do not exist for this cold start")
        if current["status"] == "frozen":
            completion = self.try_complete_cold_start(
                cold_start_id=cold_start_id,
                trigger="content_type_freeze",
                actor=actor,
            )
            result = dict(current)
            result["cold_start_completion"] = completion
            return result
        if current["status"] != "awaiting_human_decision":
            raise StateTransitionError("content-type candidates are not awaiting review")
        source_candidates = [item for item in current["candidates"] if isinstance(item, dict)]
        by_id = {str(item.get("candidate_id")): item for item in source_candidates}
        submitted: dict[str, dict[str, Any]] = {}
        for item in decisions:
            if not isinstance(item, dict):
                raise StateTransitionError("every content-type review item must be an object")
            item_id = str(item.get("candidate_id") or "").strip()
            decision = str(item.get("decision") or "").strip()
            if item_id not in by_id or decision not in {"accepted", "rejected", "merged"}:
                raise StateTransitionError("content-type review contains an unknown candidate or decision")
            if item_id in submitted:
                raise StateTransitionError("content-type review contains a duplicate candidate")
            submitted[item_id] = dict(item)
        if set(submitted) != set(by_id):
            raise StateTransitionError("the whole content-type candidate set must be reviewed once")
        accepted: dict[str, dict[str, Any]] = {}
        merged: dict[str, str] = {}
        editable = (
            "name", "definition", "content_expression", "distinction",
            "core_subject", "content_promise", "required_delivery", "scope_boundary",
        )
        for item_id, review in submitted.items():
            decision = str(review["decision"])
            if decision == "rejected":
                continue
            if decision == "merged":
                target = str(review.get("merge_into_candidate_id") or "").strip()
                if not target or target == item_id or target not in by_id:
                    raise StateTransitionError("a merged content type must name another candidate")
                merged[item_id] = target
                continue
            candidate = dict(by_id[item_id])
            for field in editable:
                edit_key = f"edited_{field}"
                if edit_key in review:
                    candidate[field] = str(review.get(edit_key) or "").strip()
            if "edited_name" in review and not candidate.get("name"):
                raise StateTransitionError("an accepted content type needs a name")
            if "edited_definition" in review:
                candidate["content_promise"] = str(candidate.get("definition") or "").strip()
            if not all(str(candidate.get(field) or "").strip() for field in (
                "name", "definition", "core_subject", "content_promise",
                "required_delivery", "scope_boundary",
            )):
                raise StateTransitionError("an accepted content type needs a complete definition")
            accepted[item_id] = candidate
        if not accepted:
            raise StateTransitionError("the frozen content-type registry cannot be empty")
        for merged_id, target_id in merged.items():
            if target_id not in accepted:
                raise StateTransitionError("a merged content type must merge into an accepted candidate")
            target = accepted[target_id]
            source = by_id[merged_id]
            target["source_observation_types"] = sorted(
                {
                    *list(target.get("source_observation_types") or []),
                    *list(source.get("source_observation_types") or []),
                },
                key=str.casefold,
            )
            target["source_refs"] = [
                *list(target.get("source_refs") or []),
                *[
                    item for item in list(source.get("source_refs") or [])
                    if item not in list(target.get("source_refs") or [])
                ],
            ]
            target["support_sample_count"] = len(target["source_refs"])
            target["representative_source_ids"] = [
                str(item.get("source_id") or "")
                for item in target["source_refs"][:5]
            ]
            target.setdefault("merged_candidate_ids", []).append(merged_id)
        final_candidates = list(accepted.values())
        registry_types = [
            {
                "canonical_id": str(item["canonical_id"]),
                "name": str(item["name"]),
                "core_subject": str(item["core_subject"]),
                "content_promise": str(item["content_promise"]),
                "required_delivery": str(item["required_delivery"]),
                "scope_boundary": str(item["scope_boundary"]),
            }
            for item in final_candidates
        ]
        reviewed_at = _now()
        provenance = {
            "cold_start_id": cold_start_id,
            "content_type_candidate_id": current["content_type_candidate_id"],
            "candidate_version": current["candidate_version"],
            "human_decision_id": decision_id.strip() or "direct_content_type_review",
            "frozen_at": reviewed_at,
        }
        pack = get_domain_pack(current["domain_label"])
        pack_path = Path(str(pack["config_path"])).resolve()
        original_pack = pack_path.read_text(encoding="utf-8")
        try:
            registry = freeze_content_type_registry(
                current["domain_label"], registry_types, provenance=provenance
            )
            review_payload = {
                "decision_id": decision_id.strip() or "direct_content_type_review",
                "decisions": [dict(item) for item in decisions],
                "final_candidates": final_candidates,
                "rejected_candidate_ids": [
                    item_id for item_id, item in submitted.items()
                    if str(item["decision"]) == "rejected"
                ],
                "merged_candidate_ids": dict(merged),
            }
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_cold_start_content_type_candidate SET status='frozen', "
                    "review_json=?, freeze_provenance_json=?, reviewed_by=?, reviewed_at=?, review_reason=? "
                    "WHERE content_type_candidate_id=? AND data_identity=? AND status='awaiting_human_decision'",
                    (
                        _canonical(review_payload), _canonical(provenance), None, reviewed_at,
                        reason.strip(), current["content_type_candidate_id"], self.data_identity,
                    ),
                )
                self._audit(None, "cold_start_content_types_frozen", {
                    "cold_start_id": cold_start_id,
                    "content_type_candidate_id": current["content_type_candidate_id"],
                    "candidate_version": current["candidate_version"],
                    "registry_version": registry["version"],
                    "type_count": len(registry["types"]),
                    "human_decision_id": provenance["human_decision_id"],
                })
        except Exception:
            try:
                pack_path.write_text(original_pack, encoding="utf-8")
            except Exception:
                pass
            raise
        result = self.get_cold_start_content_type_candidate(cold_start_id=cold_start_id) or {}
        result["cold_start_completion"] = self.try_complete_cold_start(
            cold_start_id=cold_start_id,
            trigger="content_type_freeze",
            actor=actor,
        )
        return result

    def cold_start_content_types_are_frozen(self, *, cold_start_id: str) -> bool:
        current = self.get_cold_start_content_type_candidate(cold_start_id=cold_start_id)
        if current is None or current["status"] != "frozen":
            return False
        provenance = current.get("freeze_provenance") or {}
        if str(provenance.get("cold_start_id") or "") != cold_start_id:
            return False
        try:
            registry = get_domain_pack(current["domain_label"]).get("content_type_registry")
            registry_provenance = (registry or {}).get("provenance") or {}
            return (
                isinstance(registry, dict)
                and str(registry.get("status") or "").casefold() == "frozen"
                and str(registry_provenance.get("cold_start_id") or "") == cold_start_id
                and str(registry_provenance.get("content_type_candidate_id") or "")
                == str(current["content_type_candidate_id"])
            )
        except ValueError:
            return False

    def register_question_expansion_source(
        self,
        *,
        expansion_id: str,
        domain_label: str,
        core_question: str,
        parent_source_ref: dict[str, Any],
        actor: str,
        derivation: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Register only an already-supported bounded expansion as a source."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("question expansion requires a configured formal domain")
        if len(core_question.strip()) < 6 or not isinstance(parent_source_ref, dict):
            raise StateTransitionError("question expansion requires a concrete question and parent source")
        parent_type = str(parent_source_ref.get("source_type") or "").strip()
        parent_id = str(parent_source_ref.get("source_object_id") or "").strip()
        if not parent_type or not parent_id:
            raise StateTransitionError("question expansion parent requires source_type and source_object_id")
        if parent_type == "question_expansion":
            raise StateTransitionError("question expansion cannot be derived from another question expansion")
        parent_tables = {
            "hit_breakdown": ("stage0_daily_hit_breakdown", "hit_id"),
            "competitor_breakdown": ("stage0_competitor_registration_item", "item_ref"),
        }
        parent_table = parent_tables.get(parent_type)
        if parent_table is None:
            raise StateTransitionError("question expansion must be derived during a formal hit breakdown")
        table_name, id_column = parent_table
        table_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        if table_exists is None:
            raise StateTransitionError("question expansion parent table is not available in the formal Core")
        if parent_type == "competitor_breakdown":
            registration_id = str(parent_source_ref.get("registration_id") or "").strip()
            if not registration_id:
                raise StateTransitionError("competitor breakdown parent requires registration_id")
            parent_exists = self.conn.execute(
                "SELECT 1 FROM stage0_competitor_registration_item "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? "
                "AND status='completed' AND data_identity=? LIMIT 1",
                (registration_id, parent_id, self.data_identity),
            ).fetchone()
        elif table_name in {"hits", "hit_transcripts", "stage0_daily_hit_breakdown"}:
            parent_exists = self.conn.execute(
                f"SELECT 1 FROM {table_name} WHERE {id_column}=? LIMIT 1", (parent_id,)
            ).fetchone()
        else:
            parent_exists = self.conn.execute(
                f"SELECT 1 FROM {table_name} WHERE {id_column}=? AND data_identity=? LIMIT 1",
                (parent_id, self.data_identity),
            ).fetchone()
        if parent_exists is None:
            raise StateTransitionError("question expansion parent is not a registered formal material")
        qualification = self.conn.execute(
            "SELECT status FROM stage1_question_expansion_qualification "
            "WHERE expansion_id=? AND data_identity=?",
            (expansion_id, self.data_identity),
        ).fetchone()
        if qualification is None or str(qualification["status"] or "") != "qualified":
            raise StateTransitionError("question expansion must pass lightweight qualification before source registration")
        payload = {
            "title": core_question.strip(),
            "core_question": core_question.strip(),
            "parent_source_ref": parent_source_ref,
            "derivation": dict(derivation or {}),
            "qualification_status": "qualified",
            "validation_outcome": "supported",
        }
        integrity_hash = _hash(payload)
        validated_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage1_question_expansion_source(
                    expansion_id, domain_label, core_question, parent_source_ref_json,
                    validation_outcome, validated_at, payload_json, integrity_hash,
                    data_identity, created_by
                ) VALUES (?, ?, ?, ?, 'supported', ?, ?, ?, ?, ?)
                """,
                (expansion_id, domain_label, core_question.strip(), _canonical(parent_source_ref),
                 validated_at, _canonical(payload), integrity_hash, self.data_identity, actor),
            )
        return {"expansion_id": expansion_id, "source_object_version": integrity_hash, "validated_at": validated_at}

    def register_saved_user_direction_source(
        self,
        *,
        direction_id: str,
        domain_label: str,
        core_question: str,
        submitted_by: str,
        account_ref: str | None = None,
    ) -> dict[str, str]:
        """Register a user-supplied concrete question without inventing an angle."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("saved user direction requires a configured formal domain")
        if len(core_question.strip()) < 6 or not submitted_by.strip():
            raise StateTransitionError("saved user direction requires a concrete question and user identity")
        normalized_account_ref = (
            account_ref.strip() if account_ref and account_ref.strip() else None
        )
        payload = {
            "title": core_question.strip(),
            "core_question": core_question.strip(),
            "submitted_by": submitted_by.strip(),
            "account_ref": normalized_account_ref,
        }
        integrity_hash = _hash(payload)
        saved_at = _now()
        with self.conn:
            manual_source_id = f"manual_direction_{direction_id}"
            manual_original_content = {"core_question": core_question.strip()}
            manual_integrity_hash = _hash(
                {
                    "source_kind": "direction",
                    "original_content": manual_original_content,
                    "account_ref": normalized_account_ref,
                    "evidence_role": "user_origin_not_fact",
                }
            )
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source(
                    manual_source_id, domain_label, account_ref, source_kind, original_content_json,
                    submitted_by, submitted_at, evidence_role, integrity_hash, data_identity
                ) VALUES (?, ?, ?, 'direction', ?, ?, ?, 'user_origin_not_fact', ?, ?)
                """,
                (
                    manual_source_id, domain_label, normalized_account_ref,
                    _canonical(manual_original_content), submitted_by.strip(),
                    saved_at, manual_integrity_hash, self.data_identity,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO stage1_saved_user_direction_source(
                    direction_id, domain_label, core_question, submitted_by, saved_at,
                    payload_json, integrity_hash, status, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (direction_id, domain_label, core_question.strip(), submitted_by.strip(), saved_at,
                 _canonical(payload), integrity_hash, self.data_identity),
            )
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source_link(
                    link_id, manual_source_id, target_kind, target_id, data_identity, linked_at
                ) VALUES (?, ?, 'saved_user_direction', ?, ?, ?)
                """,
                (_id("manual_source_link"), manual_source_id, direction_id, self.data_identity, saved_at),
            )
        return {"direction_id": direction_id, "source_object_version": integrity_hash, "saved_at": saved_at}

    def register_manual_source(
        self,
        *,
        manual_source_id: str,
        domain_label: str,
        account_ref: str | None,
        source_kind: str,
        original_content: Any,
        submitted_by: str,
    ) -> dict[str, str]:
        """Persist the user's original input as origin only, never as factual proof."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("manual source requires a configured formal domain")
        if source_kind not in MANUAL_SOURCE_KINDS:
            raise StateTransitionError("manual source kind is not supported")
        if not manual_source_id.strip() or not submitted_by.strip():
            raise StateTransitionError("manual source requires an identifier and user identity")
        if original_content is None or (isinstance(original_content, str) and not original_content.strip()):
            raise StateTransitionError("manual source requires the original user content")
        payload = {
            "source_kind": source_kind,
            "original_content": original_content,
            "account_ref": account_ref.strip() if account_ref and account_ref.strip() else None,
            "evidence_role": "user_origin_not_fact",
        }
        integrity_hash = _hash(payload)
        submitted_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source(
                    manual_source_id, domain_label, account_ref, source_kind, original_content_json,
                    submitted_by, submitted_at, evidence_role, integrity_hash, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'user_origin_not_fact', ?, ?)
                """,
                (
                    manual_source_id.strip(), domain_label, payload["account_ref"], source_kind,
                    _canonical(original_content), submitted_by.strip(), submitted_at,
                    integrity_hash, self.data_identity,
                ),
            )
        return {"manual_source_id": manual_source_id.strip(), "source_object_version": integrity_hash, "submitted_at": submitted_at}

    def list_manual_source_workbench(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_manual_source WHERE data_identity=? "
            "ORDER BY submitted_at DESC, manual_source_id DESC",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            exploration = self.conn.execute(
                "SELECT exploration.exploration_id, exploration.exploration_kind, "
                "exploration.scope_json, state.status, state.reason, state.effective_at, "
                "(SELECT COUNT(*) FROM stage0_manual_exploration_material material "
                "WHERE material.exploration_id=exploration.exploration_id "
                "AND material.data_identity=?) AS material_count "
                "FROM stage0_manual_exploration exploration "
                "LEFT JOIN stage0_manual_exploration_state state "
                "ON state.exploration_state_id=("
                "SELECT newest.exploration_state_id "
                "FROM stage0_manual_exploration_state newest "
                "WHERE newest.exploration_id=exploration.exploration_id "
                "AND newest.data_identity=? "
                "ORDER BY newest.effective_at DESC, newest.exploration_state_id DESC LIMIT 1"
                ") WHERE exploration.manual_source_id=? AND exploration.data_identity=?",
                (
                    self.data_identity,
                    self.data_identity,
                    row["manual_source_id"],
                    self.data_identity,
                ),
            ).fetchone()
            links = self.conn.execute(
                "SELECT target_kind, target_id, linked_at "
                "FROM stage0_manual_source_link "
                "WHERE manual_source_id=? AND data_identity=? "
                "ORDER BY linked_at, link_id",
                (row["manual_source_id"], self.data_identity),
            ).fetchall()
            exploration_materials = (
                self.conn.execute(
                    "SELECT material_ref_json, evidence_role, collected_at "
                    "FROM stage0_manual_exploration_material "
                    "WHERE exploration_id=? AND data_identity=? "
                    "ORDER BY collected_at, exploration_material_id",
                    (exploration["exploration_id"], self.data_identity),
                ).fetchall()
                if exploration is not None
                else []
            )
            results.append(
                {
                    "manual_source_id": str(row["manual_source_id"]),
                    "domain_label": str(row["domain_label"]),
                    "account_ref": row["account_ref"],
                    "source_kind": str(row["source_kind"]),
                    "original_content": json.loads(str(row["original_content_json"])),
                    "submitted_by": str(row["submitted_by"]),
                    "submitted_at": str(row["submitted_at"]),
                    "evidence_role": str(row["evidence_role"]),
                    "exploration": (
                        {
                            **{
                                key: exploration[key]
                                for key in exploration.keys()
                                if key != "scope_json"
                            },
                            "scope": json.loads(str(exploration["scope_json"])),
                            "material_count": int(exploration["material_count"] or 0),
                            "materials": [
                                {
                                    "material": json.loads(
                                        str(material["material_ref_json"])
                                    ),
                                    "evidence_role": str(
                                        material["evidence_role"]
                                    ),
                                    "collected_at": str(material["collected_at"]),
                                }
                                for material in exploration_materials
                            ],
                        }
                        if exploration is not None
                        else None
                    ),
                    "links": [
                        {key: link[key] for key in link.keys()} for link in links
                    ],
                }
            )
        return results

    def _pre_topic_source_snapshot(self, *, domain_label: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT item.item_ref, item.artifact_json FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
            "WHERE item.step_name='breakdown' AND item.status='completed' AND item.data_identity=? "
            "AND registration.data_identity=? AND account.data_identity=? AND account.domain_label=? "
            "AND account.status='active' ORDER BY item.updated_at, item.item_ref",
            (self.data_identity, self.data_identity, self.data_identity, domain_label),
        ).fetchall()
        source_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            try:
                artifact = json.loads(str(row["artifact_json"]))
                breakdown = artifact.get("deep_breakdown")
                if not isinstance(breakdown, dict):
                    continue
                subject = str(breakdown.get("content_subject_type") or "unclear")
                form = str(breakdown.get("expression_form") or "unclear")
                structure_level = str(
                    (breakdown.get("structure_assessment") or {}).get("level") or "unclear"
                )
                if subject == "unclear" or form == "unclear" or structure_level == "unclear":
                    continue
                source_id = str(breakdown.get("source_id") or row["item_ref"]).strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if source_id and source_id not in seen:
                seen.add(source_id)
                source_ids.append(source_id)
        return source_ids

    @staticmethod
    def _experience_candidate_run_view(row: sqlite3.Row) -> dict[str, Any]:
        try:
            source_snapshot = json.loads(str(row["source_snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            source_snapshot = []
        return {
            "experience_candidate_run_id": str(row["experience_candidate_run_id"]),
            "domain_label": str(row["domain_label"]),
            "status": str(row["status"]),
            "source_snapshot": source_snapshot if isinstance(source_snapshot, list) else [],
            "source_count": int(row["source_count"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "completed_at": str(row["completed_at"]) if row["completed_at"] is not None else None,
        }

    def start_pre_topic_experience_run(
        self, *, domain_label: str, actor: str
    ) -> dict[str, Any]:
        """Start an isolated pre-topic pass over a frozen source snapshot."""

        normalized = self._require_configured_domain(
            domain_label, context="pre-topic experience review"
        )
        if not actor.strip():
            raise StateTransitionError("pre-topic experience run requires a user")
        source_snapshot = self._pre_topic_source_snapshot(domain_label=normalized)
        run_id = _id("experience_candidate_run")
        created_at = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate_run SET status='superseded', completed_at=? "
                "WHERE domain_label=? AND data_identity=? AND status!='superseded'",
                (created_at, normalized, self.data_identity),
            )
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate_run VALUES (?, ?, 'running', ?, ?, ?, ?, ?, NULL)",
                (
                    run_id,
                    normalized,
                    _canonical(source_snapshot),
                    len(source_snapshot),
                    self.data_identity,
                    actor.strip(),
                    created_at,
                ),
            )
            self._audit(
                None,
                "experience_candidate_run_started",
                {
                    "experience_candidate_run_id": run_id,
                    "domain_label": normalized,
                    "source_count": len(source_snapshot),
                },
            )
        return self.get_pre_topic_experience_run(run_id=run_id)

    def get_pre_topic_experience_run(self, *, run_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate_run "
            "WHERE experience_candidate_run_id=? AND data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("pre-topic experience run does not exist in this data identity")
        return self._experience_candidate_run_view(row)

    def restore_superseded_pre_topic_experience_run(
        self, *, run_id: str, actor: str, reason: str
    ) -> dict[str, Any]:
        """Restore a superseded pre-topic run after an interrupted administrative action."""
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("experience candidate run restoration requires an actor and reason")
        target = self.get_pre_topic_experience_run(run_id=run_id)
        if target["status"] != "superseded":
            raise StateTransitionError("only a superseded experience candidate run can be restored")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate_run SET status='superseded', completed_at=? "
                "WHERE data_identity=? AND domain_label=? AND status!='superseded'",
                (now, self.data_identity, target["domain_label"]),
            )
            self.conn.execute(
                "UPDATE stage0_experience_candidate_run SET status='completed', completed_at=? "
                "WHERE experience_candidate_run_id=? AND data_identity=?",
                (target["completed_at"] or now, run_id, self.data_identity),
            )
            self._audit(
                None,
                "experience_candidate_run_restored",
                {
                    "experience_candidate_run_id": run_id,
                    "actor": actor.strip(),
                    "reason": reason.strip(),
                },
            )
        return self.get_pre_topic_experience_run(run_id=run_id)

    def summarize_pre_topic_experience_run(self, *, run_id: str) -> dict[str, Any]:
        run = self.get_pre_topic_experience_run(run_id=run_id)
        snapshot_ids = {
            str(source_id).strip()
            for source_id in run["source_snapshot"]
            if str(source_id).strip()
        }
        rows = self.conn.execute(
            "SELECT frozen_sources_json, status FROM stage0_experience_candidate "
            "WHERE task_id IS NULL AND experience_candidate_run_id=? AND data_identity=?",
            (run_id, self.data_identity),
        ).fetchall()
        processed_ids: set[str] = set()
        failed_ids: set[str] = set()
        for row in rows:
            try:
                frozen_sources = json.loads(str(row["frozen_sources_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(frozen_sources, list):
                continue
            ids = {
                str(item.get("source_id") or "").strip()
                for item in frozen_sources
                if isinstance(item, dict) and str(item.get("source_id") or "").strip()
            }
            if str(row["status"]) == "failed":
                failed_ids.update(ids)
            else:
                processed_ids.update(ids)
        processed_ids &= snapshot_ids
        failed_ids = (failed_ids & snapshot_ids) - processed_ids
        unprocessed_ids = snapshot_ids - processed_ids
        return {
            "experience_candidate_run_id": run_id,
            "domain_label": run["domain_label"],
            "run_status": run["status"],
            "active_source_count": len(snapshot_ids),
            "processed_source_count": len(processed_ids),
            "failed_source_count": len(failed_ids),
            "unprocessed_source_count": len(unprocessed_ids),
            "failed_source_ids": sorted(failed_ids),
            "unprocessed_source_ids": sorted(unprocessed_ids),
        }

    def complete_pre_topic_experience_run(self, *, run_id: str) -> dict[str, Any]:
        summary = self.summarize_pre_topic_experience_run(run_id=run_id)
        status = "completed" if summary["unprocessed_source_count"] == 0 else "completed_with_gaps"
        completed_at = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate_run SET status=?, completed_at=? "
                "WHERE experience_candidate_run_id=? AND data_identity=?",
                (status, completed_at, run_id, self.data_identity),
            )
            self._audit(
                None,
                "experience_candidate_run_completed",
                {**summary, "run_status": status},
            )
        summary["run_status"] = status
        return summary

    def _select_experience_candidate_sources_for_domain(
        self,
        *,
        domain_label: str,
        task_id: str | None,
        additional_covered_source_ids: set[str] | None = None,
        experience_candidate_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Choose one bounded, same-expression source set without inferring a conclusion."""

        if task_id is not None:
            existing = self.conn.execute(
                "SELECT experience_candidate_id FROM stage0_experience_candidate "
                "WHERE task_id=? AND data_identity=? LIMIT 1",
                (task_id, self.data_identity),
            ).fetchone()
            if existing is not None:
                return None
        run_source_ids: set[str] | None = None
        if task_id is None:
            if not experience_candidate_run_id:
                raise StateTransitionError(
                    "pre-topic experience selection requires an explicit run"
                )
            run = self.get_pre_topic_experience_run(
                run_id=experience_candidate_run_id
            )
            if run["domain_label"] != domain_label:
                raise StateTransitionError("pre-topic experience run domain does not match selection domain")
            if run["status"] == "superseded":
                raise StateTransitionError("pre-topic experience run has been superseded")
            run_source_ids = {
                str(source_id).strip()
                for source_id in run["source_snapshot"]
                if str(source_id).strip()
            }
        rows = self.conn.execute(
            "SELECT item.item_ref, item.artifact_json, account.content_account_id AS account_ref, "
            "account.display_name AS account_name FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
            "WHERE item.step_name='breakdown' AND item.status='completed' AND item.data_identity=? "
            "AND registration.data_identity=? AND account.data_identity=? AND account.domain_label=? "
            "AND account.status='active' "
            "ORDER BY item.updated_at, item.item_ref",
            (self.data_identity, self.data_identity, self.data_identity, domain_label),
        ).fetchall()
        covered_source_ids: set[str] = set()
        covered_source_ids.update(additional_covered_source_ids or set())
        if task_id is None:
            candidate_rows = self.conn.execute(
                "SELECT frozen_sources_json FROM stage0_experience_candidate "
                "WHERE task_id IS NULL AND experience_candidate_run_id=? "
                "AND domain_label=? AND data_identity=?",
                (experience_candidate_run_id, domain_label, self.data_identity),
            ).fetchall()
            for candidate_row in candidate_rows:
                try:
                    frozen_sources = json.loads(str(candidate_row["frozen_sources_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(frozen_sources, list):
                    continue
                covered_source_ids.update(
                    str(item.get("source_id") or "").strip()
                    for item in frozen_sources
                    if isinstance(item, dict) and str(item.get("source_id") or "").strip()
                )
        if run_source_ids is not None:
            covered_source_ids -= {
                source_id for source_id in covered_source_ids if source_id not in run_source_ids
            }
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            try:
                artifact = json.loads(str(row["artifact_json"]))
                breakdown = artifact.get("deep_breakdown")
                if not isinstance(breakdown, dict):
                    continue
                subject = str(breakdown.get("content_subject_type") or "unclear")
                form = str(breakdown.get("expression_form") or "unclear")
                structure_assessment = breakdown.get("structure_assessment") or {}
                structure_level = str(
                    structure_assessment.get("level") or "unclear"
                )
                if subject == "unclear" or form == "unclear" or structure_level == "unclear":
                    continue
                source_id = str(breakdown.get("source_id") or row["item_ref"])
                source = {
                    "source_id": source_id,
                    "account_ref": str(row["account_ref"]),
                    "account_name": str(row["account_name"]),
                    "content_subject_type": subject,
                    "expression_form": form,
                    "content_type_evidence": breakdown.get("content_type_evidence") or [],
                    "structure_assessment": structure_assessment,
                    "structure_grasp": breakdown.get("structure_grasp") or {},
                    "spoken_progression": breakdown.get("spoken_progression") or [],
                    "recurring_evidence_patterns": breakdown.get("recurring_evidence_patterns") or [],
                    "audience_reactions": breakdown.get("audience_reactions") or [],
                    "full_analysis": breakdown.get("full_analysis") or {},
                    "cannot_infer": breakdown.get("cannot_infer") or [],
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if run_source_ids is not None and source["source_id"] not in run_source_ids:
                continue
            groups.setdefault((form, structure_level), []).append(source)

        def balanced_batch(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            by_account: dict[str, list[dict[str, Any]]] = {}
            for value in values:
                account_ref = str(value.get("account_ref") or value["source_id"])
                by_account.setdefault(account_ref, []).append(value)
            selected: list[dict[str, Any]] = []
            while len(selected) < EXPERIENCE_CANDIDATE_BATCH_SIZE:
                added = False
                for account_ref in list(by_account):
                    account_values = by_account[account_ref]
                    if not account_values:
                        continue
                    selected.append(account_values.pop(0))
                    added = True
                    if len(selected) >= EXPERIENCE_CANDIDATE_BATCH_SIZE:
                        break
                if not added:
                    break
            return selected

        eligible = []
        for key, values in groups.items():
            remaining = [
                value for value in values
                if str(value["source_id"]) not in covered_source_ids
            ]
            remaining_accounts = {
                str(value.get("account_ref") or "") for value in remaining
                if str(value.get("account_ref") or "")
            }
            if (
                len(remaining) >= EXPERIENCE_CANDIDATE_MIN_SOURCES
                and len(remaining_accounts) >= 2
            ):
                eligible.append((key, remaining))
        for (form, structure_level), remaining in sorted(
            eligible, key=lambda item: (-len(item[1]), item[0][0], item[0][1])
        ):
            sources = balanced_batch(remaining)
            fingerprint = _hash({"domain_label": domain_label, "source_ids": sorted(item["source_id"] for item in sources)})
            if task_id is None:
                seen = self.conn.execute(
                    "SELECT 1 FROM stage0_experience_candidate "
                    "WHERE task_id IS NULL AND experience_candidate_run_id=? "
                    "AND domain_label=? AND source_fingerprint=? "
                    "AND data_identity=? LIMIT 1",
                    (experience_candidate_run_id, domain_label, fingerprint, self.data_identity),
                ).fetchone()
            else:
                seen = self.conn.execute(
                    "SELECT 1 FROM stage0_experience_candidate "
                    "WHERE task_id=? AND source_fingerprint=? AND data_identity=? LIMIT 1",
                    (task_id, fingerprint, self.data_identity),
                ).fetchone()
            if seen is None:
                return {
                    "task_id": task_id,
                    "experience_candidate_run_id": experience_candidate_run_id,
                    "domain_label": domain_label,
                    "content_type": {
                        "expression_form": form,
                        "structure_level": structure_level,
                        "content_subject_types": sorted(
                            {
                                str(item["content_subject_type"])
                                for item in sources
                            }
                        ),
                    },
                    "sources": sources,
                }
        return None

    def select_experience_candidate_sources(self, *, task_id: str) -> dict[str, Any] | None:
        """Choose sources for the existing content-plan experience suggestion."""

        task = self._task(task_id)
        if task["current_node"] != "content_plan" or task["current_status"] != "not_started":
            return None
        topic = self.get_artifact_payload(str(task["topic_version_id"]))["payload"]
        domain_label = str(topic.get("domain_label") or "").strip()
        if not domain_label:
            return None
        return self._select_experience_candidate_sources_for_domain(
            domain_label=domain_label, task_id=task_id
        )

    def select_pre_topic_experience_candidate_sources(
        self,
        *,
        domain_label: str,
        additional_covered_source_ids: set[str] | None = None,
        experience_candidate_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Choose sources for the quality-first experience review before topics exist."""

        normalized = self._require_configured_domain(domain_label, context="pre-topic experience review")
        return self._select_experience_candidate_sources_for_domain(
            domain_label=normalized,
            task_id=None,
            additional_covered_source_ids=additional_covered_source_ids,
            experience_candidate_run_id=experience_candidate_run_id,
        )

    def open_experience_candidate(
        self,
        *,
        task_id: str | None,
        domain_label: str,
        frozen_sources: list[dict[str, Any]],
        actor: str,
        experience_candidate_run_id: str | None = None,
    ) -> dict[str, str]:
        if (
            not actor.strip()
            or len(frozen_sources) < EXPERIENCE_CANDIDATE_MIN_SOURCES
            or len(frozen_sources) > EXPERIENCE_CANDIDATE_BATCH_SIZE
        ):
            raise StateTransitionError(
                "experience candidate requires three to eight frozen source breakdowns and an actor"
            )
        source_ids = [str(item.get("source_id") or "").strip() for item in frozen_sources]
        if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
            raise StateTransitionError("experience candidate sources need unique source IDs")
        account_refs = {
            str(item.get("account_ref") or "").strip()
            for item in frozen_sources
            if str(item.get("account_ref") or "").strip()
        }
        if len(account_refs) < 2:
            raise StateTransitionError(
                "experience candidate requires source breakdowns from at least two accounts"
            )
        fingerprint = _hash({"domain_label": domain_label, "source_ids": sorted(source_ids)})
        if task_id is None:
            if not experience_candidate_run_id:
                raise StateTransitionError(
                    "pre-topic experience candidate requires an explicit run"
                )
            run = self.get_pre_topic_experience_run(
                run_id=experience_candidate_run_id
            )
            if run["domain_label"] != domain_label or run["status"] == "superseded":
                raise StateTransitionError("pre-topic experience candidate run is not valid for this candidate")
            existing = self.conn.execute(
                "SELECT experience_candidate_id, status FROM stage0_experience_candidate "
                "WHERE task_id IS NULL AND experience_candidate_run_id=? "
                "AND domain_label=? AND source_fingerprint=? AND data_identity=?",
                (experience_candidate_run_id, domain_label, fingerprint, self.data_identity),
            ).fetchone()
        else:
            existing = self.conn.execute(
                "SELECT experience_candidate_id, status FROM stage0_experience_candidate "
                "WHERE task_id=? AND source_fingerprint=? AND data_identity=?",
                (task_id, fingerprint, self.data_identity),
            ).fetchone()
        if existing is not None:
            return {"experience_candidate_id": str(existing["experience_candidate_id"]), "status": str(existing["status"])}
        candidate_id = _id("experience_candidate")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate "
                "(experience_candidate_id, task_id, experience_candidate_run_id, domain_label, "
                "source_fingerprint, frozen_sources_json, status, proposal_json, failure_json, "
                "data_identity, created_by, created_at, decided_by, decided_at, decision_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 'preparing', NULL, '{}', ?, ?, ?, NULL, NULL, NULL)",
                (
                    candidate_id,
                    task_id,
                    experience_candidate_run_id,
                    domain_label,
                    fingerprint,
                    _canonical(frozen_sources),
                    self.data_identity,
                    actor.strip(),
                    _now(),
                ),
            )
            self._audit(
                task_id,
                "experience_candidate_opened",
                {
                    "experience_candidate_id": candidate_id,
                    "experience_candidate_run_id": experience_candidate_run_id,
                    "source_count": len(source_ids),
                },
            )
        return {"experience_candidate_id": candidate_id, "status": "preparing"}

    def complete_experience_candidate(
        self, *, experience_candidate_id: str, proposal: dict[str, Any], model_run_id: str
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "preparing" or not model_run_id:
            raise StateTransitionError("experience candidate is not ready to receive a model result")
        status = "awaiting_human_decision" if proposal.get("decision") == "proposal" else "no_proposal"
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status=?, proposal_json=? WHERE experience_candidate_id=?",
                (status, _canonical(proposal), experience_candidate_id),
            )
            self._audit(candidate["task_id"], "experience_candidate_completed", {"experience_candidate_id": experience_candidate_id, "status": status, "model_run_id": model_run_id})
        return {"experience_candidate_id": experience_candidate_id, "status": status}

    def fail_experience_candidate(
        self, *, experience_candidate_id: str, reason: str, model_run_id: str | None,
        raw_model_output: str | None = None,
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "preparing" or not reason.strip():
            raise StateTransitionError("experience candidate failure needs an open candidate and a reason")
        failure = {
            "reason": reason.strip(),
            "model_run_id": model_run_id,
            "raw_model_output": raw_model_output,
            "raw_model_output_status": "available" if raw_model_output is not None else "not_available",
            "automatic_retry": False,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status='failed', failure_json=? WHERE experience_candidate_id=?",
                (_canonical(failure), experience_candidate_id),
            )
            self._audit(candidate["task_id"], "experience_candidate_failed", {"experience_candidate_id": experience_candidate_id, **failure})
        return {"experience_candidate_id": experience_candidate_id, "status": "failed"}

    def decide_experience_candidate(
        self, *, experience_candidate_id: str, decision: str, actor: str, actor_kind: str, reason: str
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "awaiting_human_decision" or decision not in {"accepted", "rejected"}:
            raise StateTransitionError("experience candidate is not awaiting an accept or reject decision")
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("experience candidate decisions require an explicit user and reason")
        proposal = json.loads(str(candidate["proposal_json"] or "{}"))
        result = {"experience_candidate_id": experience_candidate_id, "decision": decision}
        with self.conn:
            if decision == "accepted":
                body = proposal["candidate"]
                experience_id = _id("confirmed_experience")
                sources = list(proposal["source_ids"])
                self.conn.execute(
                    "INSERT INTO stage0_confirmed_experience "
                    "(experience_id, experience_candidate_id, domain_label, classification, summary, "
                    "applicable_when_json, method_json, source_refs_json, boundary_json, "
                    "experience_layer, use_positions_json, trigger_signals_json, not_applicable_when_json, "
                    "status, data_identity, confirmed_by, confirmed_at) "
                    "VALUES (?, ?, ?, 'shared_pattern', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        experience_id, experience_candidate_id, candidate["domain_label"], body["summary"],
                        _canonical(body["applicable_when"]), _canonical(body["method"]), _canonical(sources),
                        _canonical(body["boundary"]), body.get("experience_layer", "section_method"),
                        _canonical(body.get("use_positions") or ["body"]),
                        _canonical(body.get("trigger_signals") or body["applicable_when"]),
                        _canonical(body.get("not_applicable_when") or body["boundary"]),
                        self.data_identity, "", _now(),
                    ),
                )
                result["experience_id"] = experience_id
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status=?, decided_by=?, decided_at=?, decision_reason=? WHERE experience_candidate_id=?",
                (decision, actor.strip(), _now(), reason.strip(), experience_candidate_id),
            )
            self._audit(candidate["task_id"], "experience_candidate_decided", result)
        return result

    def purge_stale_experience_candidates(
        self, *, actor: str, actor_kind: str, reason: str, idempotency_key: str
    ) -> dict[str, int | str]:
        """Delete only superseded or unbound candidate history, never the latest run."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("stale experience cleanup requires an explicit user and reason")
        request = {"actor": actor, "reason": reason}
        replay = self._replay("purge_stale_experience_candidates", idempotency_key, request)
        if replay:
            return replay
        rows = self.conn.execute(
            "SELECT candidate.experience_candidate_id, candidate.experience_candidate_run_id, "
            "candidate.status, run.status AS run_status "
            "FROM stage0_experience_candidate candidate "
            "LEFT JOIN stage0_experience_candidate_run run "
            "ON run.experience_candidate_run_id=candidate.experience_candidate_run_id "
            "AND run.data_identity=candidate.data_identity "
            "WHERE candidate.data_identity=? AND candidate.task_id IS NULL "
            "AND (candidate.experience_candidate_run_id IS NULL OR run.status='superseded')",
            (self.data_identity,),
        ).fetchall()
        candidate_ids = [str(row["experience_candidate_id"]) for row in rows]
        run_ids = sorted(
            {
                str(row["experience_candidate_run_id"])
                for row in rows
                if row["experience_candidate_run_id"] is not None
            }
        )
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            confirmed = self.conn.execute(
                "SELECT experience_candidate_id FROM stage0_confirmed_experience "
                f"WHERE data_identity=? AND experience_candidate_id IN ({placeholders})",
                [self.data_identity, *candidate_ids],
            ).fetchall()
            if confirmed:
                raise StateTransitionError(
                    "stale experience cleanup found a confirmed experience and stopped without deleting anything"
                )
        status_counts: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        model_run_count = 0
        deleted_run_count = 0
        with self.conn:
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                model_run_count = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM stage0_experience_candidate_model_run "
                        f"WHERE data_identity=? AND experience_candidate_id IN ({placeholders})",
                        [self.data_identity, *candidate_ids],
                    ).fetchone()[0]
                )
                self.conn.execute(
                    "DELETE FROM stage0_experience_candidate_model_run "
                    f"WHERE data_identity=? AND experience_candidate_id IN ({placeholders})",
                    [self.data_identity, *candidate_ids],
                )
                self.conn.execute(
                    "DELETE FROM stage0_experience_candidate "
                    f"WHERE data_identity=? AND experience_candidate_id IN ({placeholders})",
                    [self.data_identity, *candidate_ids],
                )
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                deleted_run_count = int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM stage0_experience_candidate_run run "
                        f"WHERE run.data_identity=? AND run.status='superseded' "
                        f"AND run.experience_candidate_run_id IN ({placeholders}) "
                        "AND NOT EXISTS (SELECT 1 FROM stage0_experience_candidate candidate "
                        "WHERE candidate.experience_candidate_run_id=run.experience_candidate_run_id "
                        "AND candidate.data_identity=run.data_identity)",
                        [self.data_identity, *run_ids],
                    ).fetchone()[0]
                )
                self.conn.execute(
                    "DELETE FROM stage0_experience_candidate_run "
                    f"WHERE data_identity=? AND status='superseded' "
                    f"AND experience_candidate_run_id IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM stage0_experience_candidate candidate "
                    "WHERE candidate.experience_candidate_run_id=stage0_experience_candidate_run.experience_candidate_run_id "
                    "AND candidate.data_identity=stage0_experience_candidate_run.data_identity)",
                    [self.data_identity, *run_ids],
                )
            result: dict[str, int | str] = {
                "status": "completed",
                "deleted_candidate_count": len(candidate_ids),
                "deleted_model_run_count": model_run_count,
                "deleted_superseded_run_count": deleted_run_count,
                "deleted_status_counts": _canonical(status_counts),
            }
            self._receipt("purge_stale_experience_candidates", idempotency_key, request, result)
            self._audit(None, "stale_experience_candidates_purged", result)
        return result

    def converge_knowledge_data(self, *, actor: str) -> dict[str, Any]:
        """Collapse formal knowledge data to one current, readable path.

        This is an explicit, user-authorized cleanup boundary.  It removes
        disabled account data, obsolete discovery inputs, superseded experience
        candidates, duplicate test tasks, and mirror-run history.  It keeps
        active accounts, active user directions, current workbench tasks,
        current experience review items, confirmed experience, and runtime
        data required for future collection.
        """
        if not actor.strip():
            raise StateTransitionError("knowledge convergence requires an explicit actor")

        table_cache: dict[str, set[str]] = {}

        def table_columns(table: str) -> set[str]:
            if table not in table_cache:
                table_cache[table] = {
                    str(row[1])
                    for row in self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
            return table_cache[table]

        def table_exists(table: str) -> bool:
            return bool(
                self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
            )

        def delete_rows(table: str, where: str = "1=1", params: tuple[Any, ...] = ()) -> int:
            if not table_exists(table):
                return 0
            before = int(self.conn.total_changes)
            self.conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)
            return int(self.conn.total_changes) - before

        def delete_identity_rows(table: str, extra: str = "", params: tuple[Any, ...] = ()) -> int:
            if not table_exists(table):
                return 0
            columns = table_columns(table)
            if "data_identity" in columns:
                suffix = f"data_identity=?{(' AND ' + extra) if extra else ''}"
                try:
                    return delete_rows(table, suffix, (self.data_identity, *params))
                except sqlite3.IntegrityError as exc:
                    raise StateTransitionError(f"knowledge convergence cannot remove {table}: {exc}") from exc
            try:
                return delete_rows(table, extra or "1=1", params)
            except sqlite3.IntegrityError as exc:
                raise StateTransitionError(f"knowledge convergence cannot remove {table}: {exc}") from exc

        immutable_triggers = [
            (str(row["name"]), str(row["sql"]))
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE '%immutable%' AND sql IS NOT NULL"
            ).fetchall()
        ]
        for trigger_name, _ in immutable_triggers:
            self.conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

        deleted: dict[str, int] = {}
        kept_task_ids: list[str] = []
        try:
            with self.conn:
                # 1. Keep only active formal accounts as the account authority.
                active_competitor_ids = {
                    str(row["content_account_id"])
                    for row in self.conn.execute(
                        "SELECT content_account_id FROM stage0_content_account "
                        "WHERE data_identity=? AND account_role='competitor' AND status='active'",
                        (self.data_identity,),
                    ).fetchall()
                }
                obsolete_formal_competitor_ids = {
                    str(row["content_account_id"])
                    for row in self.conn.execute(
                        "SELECT content_account_id FROM stage0_content_account "
                        "WHERE data_identity=? AND account_role='competitor' AND status!='active'",
                        (self.data_identity,),
                    ).fetchall()
                }
                obsolete_collection_ids = {
                    str(row["account_id"])
                    for row in self.conn.execute("SELECT account_id FROM competitor_accounts").fetchall()
                    if str(row["account_id"]) not in active_competitor_ids
                }

                # Remove all registration details belonging to a disabled formal account.
                registration_ids = {
                    str(row["registration_id"])
                    for row in self.conn.execute(
                        "SELECT registration_id FROM stage0_competitor_registration "
                        "WHERE data_identity=? AND competitor_account_id IN ({})".format(
                            ",".join("?" for _ in obsolete_formal_competitor_ids) or "NULL"
                        ),
                        (self.data_identity, *sorted(obsolete_formal_competitor_ids)),
                    ).fetchall()
                } if obsolete_formal_competitor_ids else set()
                if registration_ids:
                    marks = ",".join("?" for _ in registration_ids)
                    registration_params = tuple(sorted(registration_ids))
                    card_ids = {
                        str(row["evidence_card_id"])
                        for row in self.conn.execute(
                            f"SELECT evidence_card_id FROM stage0_evidence_card "
                            f"WHERE registration_id IN ({marks})",
                            registration_params,
                        ).fetchall()
                    } if table_exists("stage0_evidence_card") else set()
                    if card_ids and table_exists("stage0_evidence_card_group_member"):
                        card_marks = ",".join("?" for _ in card_ids)
                        deleted["旧证据卡关联"] = delete_rows(
                            "stage0_evidence_card_group_member",
                            f"evidence_card_id IN ({card_marks})",
                            tuple(sorted(card_ids)),
                        )
                    if card_ids:
                        card_marks = ",".join("?" for _ in card_ids)
                        deleted["旧证据卡"] = delete_rows(
                            "stage0_evidence_card", f"evidence_card_id IN ({card_marks})", tuple(sorted(card_ids))
                        )
                    for table in (
                        "stage0_competitor_registration_step",
                        "stage0_competitor_registration_model_run",
                        "stage0_competitor_registration_item",
                        "stage0_competitor_breakdown_attempt",
                        "stage0_competitor_material_collection_checkpoint",
                    ):
                        deleted[table] = delete_rows(
                            table, f"registration_id IN ({marks})", registration_params
                        )
                    deleted["失效账号的登记记录"] = delete_rows(
                        "stage0_competitor_registration",
                        f"registration_id IN ({marks})",
                        registration_params,
                    )

                if obsolete_collection_ids:
                    marks = ",".join("?" for _ in obsolete_collection_ids)
                    account_params = tuple(sorted(obsolete_collection_ids))
                    video_ids = {
                        str(row["video_id"])
                        for row in self.conn.execute(
                            f"SELECT video_id FROM competitor_videos WHERE account_id IN ({marks})", account_params
                        ).fetchall()
                    }
                    hit_ids = {
                        str(row["hit_id"])
                        for row in self.conn.execute(
                            f"SELECT hit_id FROM hits WHERE account_id IN ({marks})", account_params
                        ).fetchall()
                    }
                    if hit_ids:
                        hit_marks = ",".join("?" for _ in hit_ids)
                        hit_params = tuple(sorted(hit_ids))
                        for table in (
                            "hit_comments",
                            "hit_transcripts",
                            "hit_deep_analysis",
                            "stage0_daily_hit_model_run",
                            "stage0_daily_hit_breakdown",
                            "stage0_daily_hit_processing_failure",
                        ):
                            deleted[table] = delete_rows(table, f"hit_id IN ({hit_marks})", hit_params)
                        deleted["高信号记录"] = delete_rows("hits", f"hit_id IN ({hit_marks})", hit_params)
                    if video_ids:
                        video_marks = ",".join("?" for _ in video_ids)
                        video_params = tuple(sorted(video_ids))
                        deleted["视频检查"] = delete_rows("video_checks", f"video_id IN ({video_marks})", video_params)
                        deleted["视频记录"] = delete_rows("competitor_videos", f"video_id IN ({video_marks})", video_params)
                    deleted["账号基线"] = delete_rows("baselines", f"account_id IN ({marks})", account_params)
                    deleted["采集账号"] = delete_rows("competitor_accounts", f"account_id IN ({marks})", account_params)

                if obsolete_formal_competitor_ids:
                    marks = ",".join("?" for _ in obsolete_formal_competitor_ids)
                    deleted["停用对标账号"] = delete_rows(
                        "stage0_content_account",
                        f"data_identity=? AND content_account_id IN ({marks})",
                        (self.data_identity, *sorted(obsolete_formal_competitor_ids)),
                    )

                # Prevent an old cold-start snapshot from resurrecting a removed account.
                if table_exists("stage0_cold_start_configuration"):
                    config_rows = self.conn.execute(
                        "SELECT configuration_id, competitor_account_ids_json FROM stage0_cold_start_configuration "
                        "WHERE data_identity=?",
                        (self.data_identity,),
                    ).fetchall()
                    for row in config_rows:
                        try:
                            raw_ids = json.loads(str(row["competitor_account_ids_json"] or "[]"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            raw_ids = []
                        clean_ids = [
                            str(item.get("content_account_id") or item.get("account_id") or "").strip()
                            if isinstance(item, dict) else str(item).strip()
                            for item in (raw_ids if isinstance(raw_ids, list) else [])
                        ]
                        clean_ids = [item for item in clean_ids if item in active_competitor_ids]
                        if clean_ids != raw_ids:
                            self.conn.execute(
                                "UPDATE stage0_cold_start_configuration SET competitor_account_ids_json=? "
                                "WHERE configuration_id=? AND data_identity=?",
                                (_canonical(clean_ids), row["configuration_id"], self.data_identity),
                            )

                # 2. Keep the newest valid task for each topic/account/domain and remove test duplicates.
                task_rows = self.conn.execute(
                    "SELECT task_id, topic_version_id, current_status, created_at FROM stage0_content_task "
                    "WHERE data_identity=? ORDER BY created_at DESC, task_id DESC",
                    (self.data_identity,),
                ).fetchall()
                protected_task_ids = {
                    str(row["task_id"])
                    for row in self.conn.execute(
                        "SELECT candidate.task_id FROM stage0_confirmed_experience experience "
                        "JOIN stage0_experience_candidate candidate "
                        "ON candidate.experience_candidate_id=experience.experience_candidate_id "
                        "WHERE experience.data_identity=? AND experience.status='active' "
                        "AND candidate.task_id IS NOT NULL",
                        (self.data_identity,),
                    ).fetchall()
                }
                task_descriptors: list[dict[str, Any]] = []
                for row in task_rows:
                    try:
                        topic = self.get_artifact_payload(str(row["topic_version_id"]))
                    except StateTransitionError:
                        topic = {}
                    if isinstance(topic, dict) and isinstance(topic.get("payload"), dict):
                        topic = topic["payload"]
                    title = str((topic or {}).get("title") or (topic or {}).get("core_question") or "").strip()
                    domain = str((topic or {}).get("domain_label") or (topic or {}).get("domain") or "").strip()
                    account = str(
                        (topic or {}).get("account_ref")
                        or (topic or {}).get("account_id")
                        or (topic or {}).get("content_account_id")
                        or ""
                    ).strip()
                    invalid_title = (
                        not title
                        or "\ufffd" in title
                        or title.count("?") >= max(3, len(title) // 4)
                    )
                    task_descriptors.append({
                        "task_id": str(row["task_id"]),
                        "title": title,
                        "domain": domain,
                        "account": account,
                        "created_at": str(row["created_at"]),
                        "invalid": invalid_title,
                    })

                def same_topic(left: dict[str, Any], right: dict[str, Any]) -> bool:
                    if left["domain"] != right["domain"] or left["account"] != right["account"]:
                        return False
                    a = re.sub(r"[^\w\u4e00-\u9fff]+", "", left["title"]).lower()
                    b = re.sub(r"[^\w\u4e00-\u9fff]+", "", right["title"]).lower()
                    return bool(a and b and (a == b or a in b or b in a or a[:3] == b[:3]))

                keep_descriptors: list[dict[str, Any]] = []
                task_ids_to_delete: list[str] = []
                for descriptor in task_descriptors:
                    if descriptor["invalid"] or (
                        descriptor["task_id"] not in protected_task_ids
                        and any(same_topic(descriptor, kept) for kept in keep_descriptors)
                    ):
                        task_ids_to_delete.append(descriptor["task_id"])
                    else:
                        keep_descriptors.append(descriptor)
                        kept_task_ids.append(descriptor["task_id"])

                for task_id in task_ids_to_delete:
                    version_ids = [
                        str(row["version_id"])
                        for row in self.conn.execute(
                            "SELECT version_id FROM stage0_content_node_version WHERE task_id=? AND data_identity=?",
                            (task_id, self.data_identity),
                        ).fetchall()
                    ]
                    assembly_ids = [
                        str(row["assembly_id"])
                        for row in self.conn.execute(
                            "SELECT assembly_id FROM stage0_input_assembly WHERE task_id=? AND data_identity=?",
                            (task_id, self.data_identity),
                        ).fetchall()
                    ]
                    candidate_ids = [
                        str(row["experience_candidate_id"])
                        for row in self.conn.execute(
                            "SELECT experience_candidate_id FROM stage0_experience_candidate WHERE task_id=? AND data_identity=?",
                            (task_id, self.data_identity),
                        ).fetchall()
                    ]
                    publication_ids = [
                        str(row["publication_id"])
                        for row in self.conn.execute(
                            "SELECT publication_id FROM stage0_publication_registration WHERE task_id=? AND data_identity=?",
                            (task_id, self.data_identity),
                        ).fetchall()
                    ] if table_exists("stage0_publication_registration") else []
                    if publication_ids:
                        marks = ",".join("?" for _ in publication_ids)
                        deleted["旧发布复盘"] = delete_rows(
                            "stage0_publication_observation", f"publication_id IN ({marks})", tuple(publication_ids)
                        ) + delete_rows(
                            "stage0_publication_p7_review", f"publication_id IN ({marks})", tuple(publication_ids)
                        )
                        deleted["旧发布记录"] = delete_rows(
                            "stage0_publication_registration", f"publication_id IN ({marks})", tuple(publication_ids)
                        )
                    if candidate_ids:
                        marks = ",".join("?" for _ in candidate_ids)
                        deleted["旧经验模型记录"] = delete_rows(
                            "stage0_experience_candidate_model_run",
                            f"experience_candidate_id IN ({marks}) AND data_identity=?",
                            (*candidate_ids, self.data_identity),
                        )
                        deleted["旧任务经验"] = delete_rows(
                            "stage0_experience_candidate",
                            f"experience_candidate_id IN ({marks}) AND data_identity=?",
                            (*candidate_ids, self.data_identity),
                        )
                    if version_ids:
                        marks = ",".join("?" for _ in version_ids)
                        version_params = tuple(version_ids)
                        audio_ids = [
                            str(row["audio_production_id"])
                            for row in self.conn.execute(
                                f"SELECT audio_production_id FROM stage0_audio_production WHERE approved_content_version_id IN ({marks})",
                                version_params,
                            ).fetchall()
                        ] if table_exists("stage0_audio_production") else []
                        if audio_ids:
                            audio_marks = ",".join("?" for _ in audio_ids)
                            deleted["旧音频审核"] = delete_rows("stage0_audio_decision", f"audio_production_id IN ({audio_marks})", tuple(audio_ids))
                            deleted["旧音频记录"] = delete_rows("stage0_audio_production", f"audio_production_id IN ({audio_marks})", tuple(audio_ids))
                        deleted["旧音频交付"] = delete_rows("stage0_audio_delivery", f"approved_content_version_id IN ({marks})", version_params)
                        deleted["旧模型运行"] = delete_rows("stage0_model_run", f"node_version_id IN ({marks})", version_params)
                        deleted["旧失败记录"] = delete_rows("stage0_content_node_failure", f"failed_version_id IN ({marks}) OR request_version_id IN ({marks})", (*version_params, *version_params))
                        deleted["旧人工决定"] = delete_rows("stage0_content_decision", f"version_id IN ({marks})", version_params)
                        deleted["旧内容载荷"] = delete_rows("stage0_content_artifact_payload", f"version_id IN ({marks})", version_params)
                        deleted["旧研究载荷"] = delete_rows("stage1a_artifact_payload", f"version_id IN ({marks})", version_params)
                    if assembly_ids:
                        marks = ",".join("?" for _ in assembly_ids)
                        deleted["旧输入组装"] = delete_rows("stage0_model_run", f"input_assembly_id IN ({marks})", tuple(assembly_ids))
                        deleted["旧输入记录"] = delete_rows("stage0_input_assembly", f"assembly_id IN ({marks})", tuple(assembly_ids))
                    deleted["旧研究材料"] = delete_rows("stage0_research_material", "task_id=? AND data_identity=?", (task_id, self.data_identity))
                    deleted["旧任务审计"] = delete_rows("stage0_audit_event", "task_id=? AND data_identity=?", (task_id, self.data_identity))
                    deleted["旧节点版本"] = delete_rows("stage0_content_node_version", "task_id=? AND data_identity=?", (task_id, self.data_identity))
                    deleted["旧任务"] = delete_rows("stage0_content_task", "task_id=? AND data_identity=?", (task_id, self.data_identity))

                # 3. Delete discovery history and question expansions completely.
                for table in (
                    "stage1b_candidate_absence",
                    "stage1b_source_failure",
                    "stage1b_candidate_decision",
                    "stage1b_candidate_support",
                    "stage1b_candidate_relation",
                    "stage1b_candidate_assessment_revision",
                    "stage1b_candidate_assessment",
                    "stage1b_candidate_pool_state",
                    "stage1b_daily_snapshot",
                    "stage1b_candidate_version",
                    "stage1b_filter_result",
                    "stage1b_model_run",
                    "stage1b_input_assembly",
                    "stage1b_source_version",
                    "stage1b_run_execution_context",
                    "stage1b_run_domain_scope",
                    "stage1b_discovery_run",
                    "stage1_question_expansion_source",
                ):
                    deleted[table] = delete_identity_rows(table)
                deleted["已关闭的用户方向"] = delete_identity_rows("stage1_saved_user_direction_source", "status='closed'")
                for table in (
                    "domain_search_page_observation",
                    "discovered_external_videos",
                    "discovered_account_review",
                    "trendradar_hotspot_observation",
                    "trendradar_collection_run",
                ):
                    deleted[table] = delete_rows(table)

                # 4. Remove obsolete mirror/evidence history, not active runtime settings.
                deleted["旧知识库同步记录"] = delete_identity_rows("stage0_knowledge_mirror_run")
                deleted["旧证据卡关联"] = delete_identity_rows("stage0_evidence_card_group_member")
                deleted["旧证据卡"] = delete_identity_rows("stage0_evidence_card")
                deleted["旧证据卡分组"] = delete_identity_rows("stage0_evidence_card_group")
                deleted["已消费的启动检查"] = delete_identity_rows("stage0_live_cold_start_preflight", "status='consumed'")
                deleted["已结束的账号发现"] = delete_identity_rows("stage0_unregistered_account_review", "status='rejected'")
                deleted["已结束的账号视频发现"] = delete_identity_rows("stage0_unregistered_account_video", "qualified=0")
                deleted["旧标签复核记录"] = delete_identity_rows("stage0_two_week_tag_library_review", "status!='awaiting_human_review'")
                deleted["已结束拆解任务"] = delete_identity_rows(
                    "stage0_competitor_breakdown_backlog_task",
                    "status IN ('completed', 'completed_with_failures')",
                )

                # Keep only the current experience run and its preparing/awaiting candidates.
                active_experience_ids = {
                    str(row["experience_candidate_id"])
                    for row in self.conn.execute(
                        "SELECT experience_candidate_id FROM stage0_confirmed_experience "
                        "WHERE data_identity=? AND status='active'",
                        (self.data_identity,),
                    ).fetchall()
                }
                latest_run = self.conn.execute(
                    "SELECT experience_candidate_run_id FROM stage0_experience_candidate_run "
                    "WHERE data_identity=? AND status!='superseded' "
                    "ORDER BY created_at DESC, experience_candidate_run_id DESC LIMIT 1",
                    (self.data_identity,),
                ).fetchone()
                latest_run_id = str(latest_run["experience_candidate_run_id"]) if latest_run else ""
                keep_candidate_ids = set(active_experience_ids)
                if latest_run_id:
                    keep_candidate_ids.update(
                        str(row["experience_candidate_id"])
                        for row in self.conn.execute(
                            "SELECT experience_candidate_id FROM stage0_experience_candidate "
                            "WHERE data_identity=? AND experience_candidate_run_id=? "
                            "AND status IN ('preparing', 'awaiting_human_decision')",
                            (self.data_identity, latest_run_id),
                        ).fetchall()
                    )
                if kept_task_ids:
                    marks = ",".join("?" for _ in kept_task_ids)
                    keep_candidate_ids.update(
                        str(row["experience_candidate_id"])
                        for row in self.conn.execute(
                            f"SELECT experience_candidate_id FROM stage0_experience_candidate "
                            f"WHERE data_identity=? AND task_id IN ({marks}) AND status IN ('preparing', 'awaiting_human_decision', 'accepted')",
                            (self.data_identity, *kept_task_ids),
                        ).fetchall()
                    )
                all_candidate_ids = {
                    str(row["experience_candidate_id"])
                    for row in self.conn.execute(
                        "SELECT experience_candidate_id FROM stage0_experience_candidate WHERE data_identity=?",
                        (self.data_identity,),
                    ).fetchall()
                }
                stale_candidate_ids = sorted(all_candidate_ids - keep_candidate_ids)
                if stale_candidate_ids:
                    marks = ",".join("?" for _ in stale_candidate_ids)
                    deleted["旧经验模型记录"] = delete_rows(
                        "stage0_experience_candidate_model_run",
                        f"data_identity=? AND experience_candidate_id IN ({marks})",
                        (self.data_identity, *stale_candidate_ids),
                    )
                    deleted["旧经验候选"] = delete_rows(
                        "stage0_experience_candidate",
                        f"data_identity=? AND experience_candidate_id IN ({marks})",
                        (self.data_identity, *stale_candidate_ids),
                    )
                deleted["暂停经验"] = delete_identity_rows("stage0_confirmed_experience", "status!='active'")
                deleted["旧经验运行"] = delete_identity_rows(
                    "stage0_experience_candidate_run",
                    "status!='running' AND NOT EXISTS (SELECT 1 FROM stage0_experience_candidate candidate "
                    "WHERE candidate.experience_candidate_run_id=stage0_experience_candidate_run.experience_candidate_run_id "
                    "AND candidate.data_identity=stage0_experience_candidate_run.data_identity)",
                )

                result = {
                    "status": "completed",
                    "actor": actor.strip(),
                    "kept_task_ids": kept_task_ids,
                    "deleted": {key: value for key, value in deleted.items() if value},
                    "active_competitor_count": len(active_competitor_ids),
                    "current_experience_run_id": latest_run_id or None,
                    "current_experience_candidate_count": len(keep_candidate_ids),
                }
                self._audit(None, "knowledge_data_converged", result)
        finally:
            for _, trigger_sql in immutable_triggers:
                self.conn.execute(trigger_sql)
            self.conn.commit()
        return result

    def list_active_experiences(
        self, *, domain_label: str, context_text: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_confirmed_experience WHERE domain_label=? AND status='active' AND data_identity=? ORDER BY confirmed_at, experience_id",
            (domain_label, self.data_identity),
        ).fetchall()
        experiences: list[dict[str, Any]] = []
        for row in rows:
            applicable_when = json.loads(str(row["applicable_when_json"]))
            trigger_signals = json.loads(str(row["trigger_signals_json"] or "[]"))
            explicit_context_match = (
                _experience_context_matches(
                    context_text=context_text,
                    applicable_when=[*trigger_signals, *applicable_when],
                )
                if context_text is not None
                else []
            )
            if context_text is not None and not explicit_context_match:
                continue
            experiences.append({
                "experience_id": str(row["experience_id"]), "classification": str(row["classification"]),
                "summary": str(row["summary"]), "claim": str(row["summary"]),
                "accepted_at": str(row["confirmed_at"] or ""),
                "evidence_count": len(json.loads(str(row["source_refs_json"] or "[]"))),
                "applicable_when": applicable_when,
                "method": json.loads(str(row["method_json"])), "source_ids": json.loads(str(row["source_refs_json"])),
                "boundary": json.loads(str(row["boundary_json"])),
                "experience_layer": str(row["experience_layer"] or "section_method"),
                "use_positions": json.loads(str(row["use_positions_json"] or "[\"body\"]")),
                "trigger_signals": trigger_signals,
                "not_applicable_when": json.loads(str(row["not_applicable_when_json"] or "[]")),
                "explicit_context_match": explicit_context_match,
            })
        layer_order = {"structure": 0, "section_method": 1, "local_detail": 2}
        experiences.sort(
            key=lambda item: (
                -len(item["explicit_context_match"]),
                layer_order.get(str(item["experience_layer"]), 9),
                str(item["experience_id"]),
            )
        )
        return experiences[:limit] if limit is not None else experiences

    @staticmethod
    def _experience_candidate_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "experience_candidate_id": str(row["experience_candidate_id"]),
            "task_id": str(row["task_id"]) if row["task_id"] is not None else None,
            "experience_candidate_run_id": (
                str(row["experience_candidate_run_id"])
                if row["experience_candidate_run_id"] is not None
                else None
            ),
            "domain_label": str(row["domain_label"]),
            "status": str(row["status"]),
            "proposal": json.loads(str(row["proposal_json"] or "{}")),
            "failure": json.loads(str(row["failure_json"])),
            "source_count": len(json.loads(str(row["frozen_sources_json"]))),
        }

    def list_task_experience_candidates(self, *, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate WHERE task_id=? AND data_identity=? ORDER BY created_at, experience_candidate_id",
            (task_id, self.data_identity),
        ).fetchall()
        return [self._experience_candidate_view(row) for row in rows]

    def list_pre_topic_experience_candidates(
        self, *, domain_label: str | None = None, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        if run_id is not None:
            clauses = [
                "candidate.task_id IS NULL",
                "candidate.experience_candidate_run_id=?",
                "candidate.data_identity=?",
            ]
            params: list[Any] = [run_id, self.data_identity]
            if domain_label is not None:
                normalized = self._require_configured_domain(
                    domain_label, context="pre-topic experience review"
                )
                clauses.append("candidate.domain_label=?")
                params.append(normalized)
            rows = self.conn.execute(
                "SELECT candidate.* FROM stage0_experience_candidate candidate WHERE "
                + " AND ".join(clauses)
                + " ORDER BY candidate.created_at, candidate.experience_candidate_id",
                tuple(params),
            ).fetchall()
        elif domain_label is None:
            rows = self.conn.execute(
                "SELECT candidate.* FROM stage0_experience_candidate candidate "
                "JOIN stage0_experience_candidate_run run "
                "ON run.experience_candidate_run_id=candidate.experience_candidate_run_id "
                "AND run.data_identity=candidate.data_identity "
                "WHERE candidate.task_id IS NULL AND candidate.data_identity=? "
                "AND run.status!='superseded' AND run.experience_candidate_run_id=("
                "SELECT current_run.experience_candidate_run_id FROM stage0_experience_candidate_run current_run "
                "WHERE current_run.data_identity=? AND current_run.status!='superseded' "
                "ORDER BY current_run.created_at DESC, current_run.experience_candidate_run_id DESC LIMIT 1"
                ") ORDER BY candidate.created_at, candidate.experience_candidate_id",
                (self.data_identity, self.data_identity),
            ).fetchall()
        else:
            normalized = self._require_configured_domain(domain_label, context="pre-topic experience review")
            rows = self.conn.execute(
                "SELECT candidate.* FROM stage0_experience_candidate candidate "
                "JOIN stage0_experience_candidate_run run "
                "ON run.experience_candidate_run_id=candidate.experience_candidate_run_id "
                "AND run.data_identity=candidate.data_identity "
                "WHERE candidate.task_id IS NULL AND candidate.domain_label=? "
                "AND candidate.data_identity=? AND run.status!='superseded' "
                "AND run.experience_candidate_run_id=("
                "SELECT current_run.experience_candidate_run_id FROM stage0_experience_candidate_run current_run "
                "WHERE current_run.data_identity=? AND current_run.status!='superseded' "
                "ORDER BY current_run.created_at DESC, current_run.experience_candidate_run_id DESC LIMIT 1"
                ") "
                "ORDER BY candidate.created_at, candidate.experience_candidate_id",
                (normalized, self.data_identity, self.data_identity),
            ).fetchall()
        return [self._experience_candidate_view(row) for row in rows]

    def list_experience_candidate_source_cards(
        self, *, candidate_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Resolve candidate evidence to human-readable formal source cards."""
        if not candidate_ids:
            return {}
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.conn.execute(
            "SELECT experience_candidate_id, frozen_sources_json "
            "FROM stage0_experience_candidate "
            f"WHERE data_identity=? AND experience_candidate_id IN ({placeholders})",
            [self.data_identity, *candidate_ids],
        ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            try:
                frozen_sources = json.loads(str(row["frozen_sources_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                frozen_sources = []
            cards: list[dict[str, Any]] = []
            if not isinstance(frozen_sources, list):
                result[str(row["experience_candidate_id"])] = cards
                continue
            for frozen in frozen_sources:
                if not isinstance(frozen, dict):
                    continue
                source_id = str(frozen.get("source_id") or "").strip()
                if not source_id:
                    continue
                source = self.conn.execute(
                    "SELECT hit.hit_id, hit.title, hit.url, hit.publish_time, hit.platform, "
                    "account.account_name, video.raw_archive_ref "
                    "FROM hits hit "
                    "JOIN competitor_videos video ON video.video_id=hit.video_id "
                    "JOIN competitor_accounts account ON account.account_id=hit.account_id "
                    "WHERE hit.platform_item_id=? OR hit.video_id=? OR video.platform_item_id=? "
                    "OR video.video_id=? LIMIT 1",
                    (source_id, source_id, source_id, source_id),
                ).fetchone()
                evidence_actions: list[str] = []
                evidence_quotes: list[str] = []
                for pattern in frozen.get("recurring_evidence_patterns") or []:
                    if not isinstance(pattern, dict):
                        continue
                    action = str(pattern.get("spoken_action") or "").strip()
                    if action and action not in evidence_actions:
                        evidence_actions.append(action)
                    for evidence in pattern.get("source_evidence") or []:
                        if not isinstance(evidence, dict):
                            continue
                        quote = str(evidence.get("text") or "").strip()
                        if quote and quote not in evidence_quotes:
                            evidence_quotes.append(quote)
                        if len(evidence_quotes) >= 6:
                            break
                    if len(evidence_quotes) >= 6:
                        break
                if not evidence_actions:
                    assessment = frozen.get("structure_assessment") or {}
                    statement = str(assessment.get("statement") or "").strip()
                    if statement:
                        evidence_actions.append(statement)
                cards.append({
                    "source_key": source_id,
                    "title": str(source["title"] if source is not None else "") or "未找到标题",
                    "url": str(source["url"] if source is not None else "").strip(),
                    "account_name": str(
                        source["account_name"] if source is not None else frozen.get("account_name") or ""
                    ).strip() or "未找到账号",
                    "publish_time": str(source["publish_time"] if source is not None else "").strip(),
                    "platform": str(source["platform"] if source is not None else "").strip(),
                    "archive_status": (
                        "已找到正式来源"
                        if source is not None
                        else "只找到拆解记录，未找到正式来源"
                    ),
                    "content_subject_type": str(
                        frozen.get("content_subject_type") or "未说明"
                    ),
                    "expression_form": str(
                        frozen.get("expression_form") or "未说明"
                    ),
                    "content_type_evidence": frozen.get("content_type_evidence") or [],
                    "structure_assessment": frozen.get("structure_assessment") or {},
                    "structure_grasp": frozen.get("structure_grasp") or {},
                    "spoken_progression": frozen.get("spoken_progression") or [],
                    "recurring_evidence_patterns": frozen.get("recurring_evidence_patterns") or [],
                    "audience_reactions": frozen.get("audience_reactions") or [],
                    "full_analysis": frozen.get("full_analysis") or {},
                    "cannot_infer": frozen.get("cannot_infer") or [],
                    "evidence_actions": evidence_actions[:4],
                    "evidence_quotes": evidence_quotes[:6],
                })
            result[str(row["experience_candidate_id"])] = cards
        return result

    def _experience_candidate(self, experience_candidate_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate WHERE experience_candidate_id=? AND data_identity=?",
            (experience_candidate_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("experience candidate does not exist in this data identity")
        return row

    def list_content_workbench(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_content_task WHERE data_identity=? "
            "ORDER BY created_at DESC, task_id DESC",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            topic = self.get_artifact_payload(str(row["topic_version_id"]))
            current = None
            if row["current_version_id"]:
                try:
                    current = self.get_artifact_payload(
                        str(row["current_version_id"])
                    )
                except StateTransitionError:
                    current = None
            failure_row = (
                self.conn.execute(
                    "SELECT failure_stage, reason, raw_model_output, raw_model_output_status, created_at "
                    "FROM stage0_content_node_failure WHERE failed_version_id=? AND data_identity=?",
                    (row["current_version_id"], self.data_identity),
                ).fetchone()
                if row["current_version_id"]
                else None
            )
            materials = self.conn.execute(
                "SELECT research_material_id, source_ref, title, evidence_role, "
                "material_json, integrity_hash, collected_at "
                "FROM stage0_research_material "
                "WHERE task_id=? AND data_identity=? "
                "ORDER BY collected_at, research_material_id",
                (row["task_id"], self.data_identity),
            ).fetchall()
            audio_attempts = self.conn.execute(
                "SELECT * FROM stage0_audio_production "
                "WHERE task_id=? AND data_identity=? "
                "ORDER BY attempt_number DESC, created_at DESC",
                (row["task_id"], self.data_identity),
            ).fetchall()
            version_rows = self.conn.execute(
                "SELECT version_id, node, status, validation_status, output_ref, "
                "created_by, created_at, task_revision "
                "FROM stage0_content_node_version "
                "WHERE task_id=? AND data_identity=? "
                "ORDER BY created_at, version_id",
                (row["task_id"], self.data_identity),
            ).fetchall()
            artifact_versions: list[dict[str, Any]] = []
            for version in version_rows:
                artifact: dict[str, Any] | None = None
                try:
                    artifact = self.get_artifact_payload(str(version["version_id"]))
                except StateTransitionError:
                    artifact = None
                artifact_versions.append(
                    {
                        "version_id": str(version["version_id"]),
                        "node": str(version["node"]),
                        "status": str(version["status"]),
                        "validation_status": str(version["validation_status"]),
                        "output_ref": version["output_ref"],
                        "created_by": str(version["created_by"]),
                        "created_at": str(version["created_at"]),
                        "task_revision": int(version["task_revision"] or 0),
                        "artifact": artifact,
                    }
                )
            decision_rows = self.conn.execute(
                "SELECT node, version_id, decision, actor, actor_kind, reason, created_at "
                "FROM stage0_content_decision WHERE task_id=? AND data_identity=? "
                "ORDER BY created_at, decision_id",
                (row["task_id"], self.data_identity),
            ).fetchall()
            results.append(
                {
                    "task_id": str(row["task_id"]),
                    "current_node": str(row["current_node"]),
                    "current_status": str(row["current_status"]),
                    "created_at": str(row["created_at"]),
                    "topic": topic,
                    "current_artifact": current,
                    "failure": (
                        {
                            "failure_stage": str(failure_row["failure_stage"]),
                            "reason": str(failure_row["reason"]),
                            "raw_model_output": failure_row["raw_model_output"],
                            "raw_model_output_status": str(failure_row["raw_model_output_status"]),
                            "created_at": str(failure_row["created_at"]),
                        }
                        if failure_row is not None
                        else None
                    ),
                    "experience_candidates": self.list_task_experience_candidates(task_id=str(row["task_id"])),
                    "research_materials": [
                        {
                            "research_material_id": str(material["research_material_id"]),
                            "source_ref": str(material["source_ref"]),
                            "title": str(material["title"]),
                            "evidence_role": str(material["evidence_role"]),
                            "material": json.loads(str(material["material_json"])),
                            "integrity_hash": str(material["integrity_hash"]),
                            "collected_at": str(material["collected_at"]),
                        }
                        for material in materials
                    ],
                    "audio_attempts": [
                        {
                            **{
                                key: attempt[key]
                                for key in attempt.keys()
                                if key
                                not in {
                                    "synthesis_json",
                                    "quality_review_json",
                                    "failure_json",
                                }
                            },
                            "synthesis": json.loads(str(attempt["synthesis_json"])),
                            "quality_review": json.loads(
                                str(attempt["quality_review_json"])
                            ),
                            "failure": json.loads(str(attempt["failure_json"])),
                        }
                        for attempt in audio_attempts
                    ],
                    "artifact_versions": artifact_versions,
                    "content_decisions": [
                        {
                            "node": str(decision["node"]),
                            "version_id": str(decision["version_id"]),
                            "decision": str(decision["decision"]),
                            "actor": str(decision["actor"]),
                            "actor_kind": str(decision["actor_kind"]),
                            "reason": str(decision["reason"]),
                            "created_at": str(decision["created_at"]),
                        }
                        for decision in decision_rows
                    ],
                }
            )
        return results

    @staticmethod
    def _knowledge_json(value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return _hide_build_root_paths(value)
        try:
            return _hide_build_root_paths(json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return value

    def list_knowledge_source_records(self) -> list[dict[str, Any]]:
        """Return all collected source records needed for readable operation."""
        video_rows = self.conn.execute(
            "SELECT video.*, account.account_name, account.domain_label, account.platform AS account_platform "
            "FROM competitor_videos video JOIN competitor_accounts account "
            "ON account.account_id=video.account_id "
            "JOIN stage0_content_account formal_account "
            "ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' "
            "AND formal_account.status='active' "
            "WHERE account.registration_status='active' AND COALESCE(video.excluded_reason, '')='' "
            "ORDER BY COALESCE(video.publish_time, ''), video.video_id",
            (self.data_identity,),
        ).fetchall()
        hit_rows = self.conn.execute(
            "SELECT hit.* FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' "
            "AND formal_account.status='active' "
            "WHERE account.registration_status='active' AND COALESCE(video.excluded_reason, '')='' "
            "ORDER BY COALESCE(hit.publish_time, ''), hit.hit_id",
            (self.data_identity,),
        ).fetchall()
        transcript_rows = self.conn.execute(
            "WITH ranked AS (SELECT transcript.*, ROW_NUMBER() OVER (PARTITION BY hit_id "
            "ORDER BY CASE WHEN processing_status='completed' THEN 0 ELSE 1 END, version DESC, created_at DESC, transcript_id DESC) AS row_no "
            "FROM hit_transcripts transcript) SELECT * FROM ranked WHERE row_no=1 ORDER BY hit_id"
        ).fetchall()
        comment_rows = self.conn.execute(
            "WITH ranked AS (SELECT comments.*, DENSE_RANK() OVER (PARTITION BY hit_id, purpose "
            "ORDER BY fetched_at DESC, run_id DESC) AS batch_no FROM hit_comments comments) "
            "SELECT * FROM ranked WHERE batch_no=1 ORDER BY hit_id, purpose, sample_rank, comment_id"
        ).fetchall()
        analysis_rows = self.conn.execute(
            "WITH ranked AS (SELECT analysis.*, ROW_NUMBER() OVER (PARTITION BY hit_id "
            "ORDER BY version DESC, created_at DESC, analysis_id DESC) AS row_no FROM hit_deep_analysis analysis) "
            "SELECT * FROM ranked WHERE row_no=1 ORDER BY hit_id"
        ).fetchall()
        check_rows = self.conn.execute(
            "SELECT * FROM video_checks ORDER BY video_id, checked_at, check_id"
        ).fetchall()
        baseline_rows = self.conn.execute(
            "WITH ranked AS (SELECT baseline.*, ROW_NUMBER() OVER (PARTITION BY account_id, baseline_mode, metric, observation_point "
            "ORDER BY computed_at DESC, baseline_id DESC) AS row_no FROM baselines baseline) "
            "SELECT * FROM ranked WHERE row_no=1 ORDER BY account_id, metric, observation_point"
        ).fetchall()

        hits_by_video: dict[str, list[dict[str, Any]]] = {}
        for row in hit_rows:
            hits_by_video.setdefault(str(row["video_id"]), []).append({
                "hit_id": str(row["hit_id"]),
                "title": str(row["title"] or ""),
                "url": str(row["url"] or ""),
                "publish_time": str(row["publish_time"] or ""),
                "metrics": {
                    key: row[key]
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                },
                "hit_channel": str(row["hit_channel"] or ""),
                "judgment_confidence": str(row["judgment_confidence"] or ""),
                "preparation_status": str(row["preparation_status"] or ""),
                "promoted_at": str(row["promoted_at"] or ""),
            })

        transcripts_by_hit: dict[str, list[dict[str, Any]]] = {}
        for row in transcript_rows:
            transcripts_by_hit.setdefault(str(row["hit_id"]), []).append({
                "transcript_id": str(row["transcript_id"]),
                "version": int(row["version"] or 0),
                "raw_text": str(row["raw_transcript_text"] or ""),
                "cleaned_text": str(row["cleaned_transcript_text"] or ""),
                "char_count": int(row["char_count"] or 0),
                "asr_model": str(row["asr_model"] or ""),
                "vad_model": str(row["vad_model"] or ""),
                "processing_method": str(row["processing_method"] or ""),
                "quality_flags": self._knowledge_json(row["quality_flags"]),
                "processing_status": str(row["processing_status"] or ""),
                "created_at": str(row["created_at"] or ""),
            })

        comments_by_hit: dict[str, list[dict[str, Any]]] = {}
        for row in comment_rows:
            comments_by_hit.setdefault(str(row["hit_id"]), []).append({
                "comment_id": str(row["comment_id"]),
                "text": str(row["text"] or ""),
                "like_count": int(row["like_count"] or 0),
                "sample_rank": int(row["sample_rank"] or 0),
                "purpose": str(row["purpose"] or ""),
                "observation_point": str(row["observation_point"] or ""),
                "sampling_strategy": str(row["sampling_strategy"] or ""),
                "fetched_at": str(row["fetched_at"] or ""),
            })

        analysis_by_hit: dict[str, list[dict[str, Any]]] = {}
        for row in analysis_rows:
            analysis_by_hit.setdefault(str(row["hit_id"]), []).append({
                "analysis_id": str(row["analysis_id"]),
                "version": int(row["version"] or 0),
                "topic_pattern": self._knowledge_json(row["topic_pattern"]),
                "hook_pattern": self._knowledge_json(row["hook_pattern"]),
                "structure_pattern": self._knowledge_json(row["structure_pattern"]),
                "model_name": str(row["model_name"] or ""),
                "created_at": str(row["created_at"] or ""),
            })

        checks_by_video: dict[str, list[dict[str, Any]]] = {}
        for row in check_rows:
            checks_by_video.setdefault(str(row["video_id"]), []).append({
                "checked_at": str(row["checked_at"] or ""),
                "metrics": {
                    key: row[key]
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                },
                "day_since_publish": row["day_since_publish"],
            })

        baselines_by_account: dict[str, list[dict[str, Any]]] = {}
        for row in baseline_rows:
            baselines_by_account.setdefault(str(row["account_id"]), []).append({
                "baseline_mode": str(row["baseline_mode"] or ""),
                "metric": str(row["metric"] or ""),
                "observation_point": str(row["observation_point"] or ""),
                "sample_count": int(row["sample_count"] or 0),
                "median_value": row["median_value"],
                "computed_at": str(row["computed_at"] or ""),
            })

        records: list[dict[str, Any]] = []
        for row in video_rows:
            video_id = str(row["video_id"])
            records.append({
                "source_id": video_id,
                "source_kind": "账号视频",
                "account_name": str(row["account_name"] or "未登记账号"),
                "domain_label": str(row["domain_label"] or ""),
                "platform": str(row["platform"] or row["account_platform"] or ""),
                "platform_item_id": str(row["platform_item_id"] or ""),
                "title": str(row["title"] or video_id),
                "url": str(row["url"] or ""),
                "publish_time": str(row["publish_time"] or ""),
                "duration_sec": row["duration_sec"],
                "metrics": {
                    key: row[key]
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                },
                "tracking_completed": bool(row["tracking_completed"]),
                "excluded_reason": str(row["excluded_reason"] or ""),
                "raw_archive_ref": str(row["raw_archive_ref"] or ""),
                "first_seen_at": str(row["first_seen_at"] or ""),
                "last_checked_at": str(row["last_checked_at"] or ""),
                "checks": checks_by_video.get(video_id, []),
                "baselines": baselines_by_account.get(str(row["account_id"]), []),
                "hits": hits_by_video.get(video_id, []),
                "transcripts": [
                    transcript
                    for hit in hits_by_video.get(video_id, [])
                    for transcript in transcripts_by_hit.get(str(hit["hit_id"]), [])
                ],
                "comments": [
                    comment
                    for hit in hits_by_video.get(video_id, [])
                    for comment in comments_by_hit.get(str(hit["hit_id"]), [])
                ],
                "deep_analysis": [
                    analysis
                    for hit in hits_by_video.get(video_id, [])
                    for analysis in analysis_by_hit.get(str(hit["hit_id"]), [])
                ],
            })
        return records

    def list_all_experience_candidates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate WHERE data_identity=? "
            "ORDER BY created_at, experience_candidate_id",
            (self.data_identity,),
        ).fetchall()
        return [self._experience_candidate_view(row) for row in rows]

    def list_knowledge_account_registry(self) -> dict[str, list[dict[str, Any]]]:
        formal_accounts = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE data_identity=? "
            "AND status='active' ORDER BY domain_label, account_role, display_name, content_account_id",
            (self.data_identity,),
        ).fetchall()
        collection_accounts = self.conn.execute(
            "SELECT collection.* FROM competitor_accounts collection "
            "JOIN stage0_content_account formal ON formal.content_account_id=collection.account_id "
            "AND formal.data_identity=? AND formal.account_role='competitor' AND formal.status='active' "
            "WHERE collection.registration_status='active' "
            "ORDER BY collection.domain_label, collection.account_name, collection.account_id",
            (self.data_identity,),
        ).fetchall()
        baseline_rows = self.conn.execute(
            "SELECT * FROM baselines ORDER BY account_id, metric, observation_point, baseline_id"
        ).fetchall()
        return {
            "formal_accounts": [
                {key: row[key] for key in row.keys()} for row in formal_accounts
            ],
            "collection_accounts": [
                {key: row[key] for key in row.keys()} for row in collection_accounts
            ],
            "baselines": [
                {key: row[key] for key in row.keys()} for row in baseline_rows
            ],
        }

    def resolve_owned_account_ref(self, *, domain_label: str, content_account_id: str) -> str:
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            raise StateTransitionError(
                "the requested domain has no current owned-account configuration"
            )
        if activation is not None:
            configuration = self.get_cold_start_configuration(
                configuration_id=str(activation["configuration_id"])
            )
            if str(configuration["owned_account_id"]) != content_account_id.strip():
                raise StateTransitionError(
                    "the selected owned account is not current in the requested domain"
                )
            row = self.conn.execute(
                "SELECT external_account_ref FROM stage0_content_account "
                "WHERE content_account_id=? AND account_role='owned' "
                "AND status='active' AND data_identity=?",
                (content_account_id.strip(), self.data_identity),
            ).fetchone()
        if row is None or not str(row["external_account_ref"] or "").strip():
            raise StateTransitionError("the selected owned account is not active in the requested domain")
        return str(row["external_account_ref"]).strip()

    def list_knowledge_processing_status(self) -> dict[str, list[dict[str, Any]]]:
        registrations = self.conn.execute(
            "SELECT registration.*, account.display_name, account.domain_label "
            "FROM stage0_competitor_registration registration "
            "LEFT JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "WHERE registration.data_identity=? ORDER BY registration.created_at, registration.registration_id",
            (self.data_identity,),
        ).fetchall()
        steps = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration_step WHERE data_identity=? "
            "ORDER BY completed_at, step_record_id",
            (self.data_identity,),
        ).fetchall()
        items = self.conn.execute(
            "SELECT registration_id, step_name, item_ref, status, artifact_json, error_json, "
            "attempt_count, updated_at FROM stage0_competitor_registration_item "
            "WHERE data_identity=? ORDER BY updated_at, registration_id, step_name, item_ref",
            (self.data_identity,),
        ).fetchall()
        attempts = self.conn.execute(
            "SELECT registration_id, source_id, attempt_kind, outcome, reason, "
            "raw_model_output_status, created_at FROM stage0_competitor_breakdown_attempt "
            "WHERE data_identity=? ORDER BY created_at, breakdown_attempt_id",
            (self.data_identity,),
        ).fetchall()
        checkpoints = self.conn.execute(
            "SELECT registration_id, item_ref, detail_json, comments_json, collection_ref, recorded_at "
            "FROM stage0_competitor_material_collection_checkpoint WHERE data_identity=? "
            "ORDER BY recorded_at, registration_id, item_ref",
            (self.data_identity,),
        ).fetchall()
        backlog = self.conn.execute(
            "SELECT * FROM stage0_competitor_breakdown_backlog_task WHERE data_identity=? "
            "ORDER BY created_at, backlog_task_id",
            (self.data_identity,),
        ).fetchall()
        daily_breakdown = self.conn.execute(
            "SELECT * FROM stage0_daily_hit_breakdown WHERE data_identity=? "
            "ORDER BY created_at, hit_id, version",
            (self.data_identity,),
        ).fetchall()
        preflight = self.conn.execute(
            "SELECT * FROM stage0_live_cold_start_preflight WHERE data_identity=? "
            "ORDER BY inspected_at, preflight_receipt_id",
            (self.data_identity,),
        ).fetchall()
        return {
            "registrations": [
                {key: row[key] for key in row.keys()} for row in registrations
            ],
            "steps": [
                {
                    "registration_id": str(row["registration_id"]),
                    "step_name": str(row["step_name"]),
                    "artifact_refs": self._knowledge_json(row["artifact_refs_json"]),
                    "completed_by": str(row["completed_by"] or ""),
                    "completed_at": str(row["completed_at"] or ""),
                }
                for row in steps
            ],
            "items": [
                {
                    "registration_id": str(row["registration_id"]),
                    "step_name": str(row["step_name"]),
                    "item_ref": str(row["item_ref"]),
                    "status": str(row["status"]),
                    "artifact": self._knowledge_json(row["artifact_json"]),
                    "error": self._knowledge_json(row["error_json"]),
                    "attempt_count": int(row["attempt_count"] or 0),
                    "updated_at": str(row["updated_at"] or ""),
                }
                for row in items
            ],
            "attempts": [
                {
                    "registration_id": str(row["registration_id"]),
                    "source_id": str(row["source_id"]),
                    "attempt_kind": str(row["attempt_kind"]),
                    "outcome": str(row["outcome"]),
                    "reason": str(row["reason"] or ""),
                    "raw_model_output_status": str(row["raw_model_output_status"] or ""),
                    "created_at": str(row["created_at"] or ""),
                }
                for row in attempts
            ],
            "checkpoints": [
                {
                    "registration_id": str(row["registration_id"]),
                    "item_ref": str(row["item_ref"]),
                    "detail": self._knowledge_json(row["detail_json"]),
                    "comments": self._knowledge_json(row["comments_json"]),
                    "collection_ref": str(row["collection_ref"] or ""),
                    "recorded_at": str(row["recorded_at"] or ""),
                }
                for row in checkpoints
            ],
            "backlog": [
                {
                    key: self._knowledge_json(row[key]) if key.endswith("_json") else row[key]
                    for key in row.keys()
                }
                for row in backlog
            ],
            "daily_breakdown": [
                {
                    key: self._knowledge_json(row[key]) if key == "artifact_json" else row[key]
                    for key in row.keys()
                }
                for row in daily_breakdown
            ],
            "preflight": [
                {
                    key: self._knowledge_json(row[key]) if key == "report_json" else row[key]
                    for key in row.keys()
                }
                for row in preflight
            ],
        }

    def list_knowledge_selection_inputs(self) -> dict[str, list[dict[str, Any]]]:
        tags = self.conn.execute(
            "SELECT * FROM domain_search_tags WHERE status='active' ORDER BY domain_label, tag, tag_id"
        ).fetchall()
        tag_library = self.conn.execute(
            "SELECT * FROM stage0_cold_start_tag_library WHERE data_identity=? "
            "ORDER BY created_at, tag_library_id",
            (self.data_identity,),
        ).fetchall()
        hotspots = self.conn.execute(
            "SELECT observation.*, run.status AS collection_status, run.discovery_run_id "
            "FROM trendradar_hotspot_observation observation "
            "LEFT JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "ORDER BY observation.observed_at, observation.observation_id"
        ).fetchall()
        question_sources = self.conn.execute(
            "SELECT source.* FROM stage1_question_expansion_source source "
            "JOIN stage1_question_expansion_qualification qualification "
            "ON qualification.expansion_id=source.expansion_id "
            "AND qualification.data_identity=source.data_identity "
            "AND qualification.status='qualified' "
            "WHERE source.data_identity=? AND source.validation_outcome='supported' "
            "ORDER BY validated_at, expansion_id",
            (self.data_identity,),
        ).fetchall()
        saved_directions = self.conn.execute(
            "SELECT * FROM stage1_saved_user_direction_source WHERE data_identity=? "
            "AND status='active' "
            "ORDER BY saved_at, direction_id",
            (self.data_identity,),
        ).fetchall()
        discovered_videos = self.conn.execute(
            "SELECT * FROM discovered_external_videos ORDER BY discovered_at, discovered_video_id"
        ).fetchall()
        return {
            "tags": [{key: row[key] for key in row.keys()} for row in tags],
            "tag_library": [
                {
                    key: self._knowledge_json(row[key]) if key.endswith("_json") else row[key]
                    for key in row.keys()
                }
                for row in tag_library
            ],
            "hotspots": [
                {
                    key: self._knowledge_json(row[key]) if key == "raw_json" else row[key]
                    for key in row.keys()
                }
                for row in hotspots
            ],
            "question_sources": [
                {
                    key: self._knowledge_json(row[key]) if key == "payload_json" else row[key]
                    for key in row.keys()
                }
                for row in question_sources
            ],
            "saved_directions": [
                {
                    key: self._knowledge_json(row[key]) if key == "payload_json" else row[key]
                    for key in row.keys()
                }
                for row in saved_directions
            ],
            "discovered_videos": [
                {
                    key: self._knowledge_json(row[key]) if key == "raw_json" else row[key]
                    for key in row.keys()
                }
                for row in discovered_videos
            ],
        }

    def list_knowledge_candidate_pool(self) -> list[dict[str, Any]]:
        """Return actual Stage 1B candidates awaiting a user decision.

        Question expansions, search tags and other discovery inputs are not
        candidates.  The mirror must read this candidate table instead of
        presenting every input as a candidate direction.
        """
        current_activation_filter = ""
        query_params: list[Any] = [self.data_identity]
        if self.domain_activation_schema_available():
            current_activation_filter = (
                "AND ("
                "  EXISTS ("
                "    SELECT 1 FROM stage1b_source_version current_source "
                "    JOIN stage1b_run_execution_context current_context "
                "      ON current_context.run_id=current_source.run_id "
                "      AND current_context.data_identity=current_source.data_identity "
                "    JOIN stage0_daily_run current_daily "
                "      ON current_daily.daily_run_id=current_context.daily_run_id "
                "    JOIN stage0_domain_activation current_activation "
                "      ON current_activation.domain_label=candidate.domain_label "
                "      AND current_activation.cold_start_id=current_daily.cold_start_id "
                "      AND current_activation.data_identity=candidate.data_identity "
                "      AND current_activation.is_current=1 "
                "    WHERE current_source.source_version_id=candidate.source_version_id "
                "      AND current_source.data_identity=candidate.data_identity"
                "  ) "
                "  OR NOT EXISTS ("
                "    SELECT 1 FROM stage0_domain_activation activation_history "
                "    WHERE activation_history.domain_label=candidate.domain_label "
                "      AND activation_history.data_identity=candidate.data_identity"
                "  )"
                ") "
            )
        query_params.extend([_now()])
        rows = self.conn.execute(
            "SELECT candidate.*, source.source_type, source.source_time, "
            "source.expires_at, source.payload_json AS source_payload_json "
            "FROM stage1b_candidate_version candidate "
            "LEFT JOIN stage1b_source_version source "
            "ON source.source_version_id=candidate.source_version_id "
            "AND source.data_identity=candidate.data_identity "
            "WHERE candidate.data_identity=? "
            "AND candidate.status='awaiting_user_decision' "
            "AND COALESCE((SELECT state.pool_status FROM stage1b_candidate_pool_state state "
            "WHERE state.candidate_version_id=candidate.candidate_version_id AND state.data_identity=candidate.data_identity "
            "ORDER BY state.effective_at DESC, state.pool_state_id DESC LIMIT 1), 'current')='current' "
            "AND (source.expires_at IS NULL OR source.expires_at >= ?) "
            + current_activation_filter
            + "AND NOT EXISTS ("
            "  SELECT 1 FROM stage1b_candidate_decision decision "
            "  WHERE decision.candidate_version_id=candidate.candidate_version_id "
            "  AND decision.data_identity=candidate.data_identity"
            ") "
            "ORDER BY candidate.domain_label, candidate.created_at, candidate.candidate_version_id",
            tuple(query_params),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            assessment = self.conn.execute(
                "SELECT total_score, dimension_scores_json, dimension_reasons_json, "
                "assessed_by, assessed_at "
                "FROM stage1b_candidate_assessment_revision "
                "WHERE candidate_version_id=? AND data_identity=? "
                "ORDER BY assessed_at DESC, assessment_revision_id DESC LIMIT 1",
                (row["candidate_version_id"], self.data_identity),
            ).fetchone()
            if assessment is None:
                assessment = self.conn.execute(
                    "SELECT total_score, dimension_scores_json, dimension_reasons_json, "
                    "assessed_by, assessed_at "
                    "FROM stage1b_candidate_assessment "
                    "WHERE candidate_version_id=? AND data_identity=? "
                    "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
                    (row["candidate_version_id"], self.data_identity),
                ).fetchone()
            pool_state = self.conn.execute(
                "SELECT pool_status, reason, effective_at "
                "FROM stage1b_candidate_pool_state "
                "WHERE candidate_version_id=? AND data_identity=? "
                "ORDER BY effective_at DESC, pool_state_id DESC LIMIT 1",
                (row["candidate_version_id"], self.data_identity),
            ).fetchone()
            support_rows = self.conn.execute(
                "SELECT support.support_kind, support.relation_reason, "
                "source.source_type, source.source_time, source.payload_json "
                "FROM stage1b_candidate_support support "
                "LEFT JOIN stage1b_source_version source "
                "ON source.source_version_id=support.source_version_id "
                "AND source.data_identity=support.data_identity "
                "WHERE support.candidate_version_id=? AND support.data_identity=? "
                "ORDER BY support.created_at, support.support_id",
                (row["candidate_version_id"], self.data_identity),
            ).fetchall()
            results.append(
                {
                    "candidate_version_id": str(row["candidate_version_id"]),
                    "candidate_id": str(row["candidate_id"]),
                    "run_id": str(row["run_id"]),
                    "domain_label": str(row["domain_label"]),
                    "source_version_id": str(row["source_version_id"]),
                    "source_type": str(row["source_type"] or ""),
                    "source_time": str(row["source_time"] or ""),
                    "expires_at": str(row["expires_at"] or ""),
                    "candidate": self._knowledge_json(row["payload_json"]),
                    "source": self._knowledge_json(row["source_payload_json"]),
                    "created_at": str(row["created_at"]),
                    "pool_status": str(pool_state["pool_status"] if pool_state else "current"),
                    "pool_reason": str(pool_state["reason"] if pool_state else ""),
                    "pool_effective_at": str(pool_state["effective_at"] if pool_state else ""),
                    "assessment": (
                        {
                            "total_score": assessment["total_score"],
                            "dimension_scores": self._knowledge_json(assessment["dimension_scores_json"]),
                            "dimension_reasons": self._knowledge_json(assessment["dimension_reasons_json"]),
                            "assessed_by": str(assessment["assessed_by"] or ""),
                            "assessed_at": str(assessment["assessed_at"] or ""),
                        }
                        if assessment is not None
                        else None
                    ),
                    "support_materials": [
                        {
                            "support_kind": str(support["support_kind"] or ""),
                            "reason": str(support["relation_reason"] or ""),
                            "source_type": str(support["source_type"] or ""),
                            "source_time": str(support["source_time"] or ""),
                            "source": self._knowledge_json(support["payload_json"]),
                        }
                        for support in support_rows
                    ],
                }
            )
        return results

    def list_knowledge_human_decisions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT command.*, binding.carrier_kind, binding.entry_ref, binding.context_strategy "
            "FROM stage0_human_decision_command command "
            "LEFT JOIN stage0_human_decision_carrier_binding binding "
            "ON binding.carrier_binding_id=command.carrier_binding_id "
            "AND binding.data_identity=command.data_identity "
            "WHERE command.data_identity=? ORDER BY command.received_at, command.command_id",
            (self.data_identity,),
        ).fetchall()
        return [
            {
                key: self._knowledge_json(row[key]) if key.endswith("_json") else row[key]
                for key in row.keys()
            }
            for row in rows
        ]

    def list_publication_workbench(self) -> list[dict[str, Any]]:
        publications = self.conn.execute(
            "SELECT * FROM stage0_publication_registration WHERE data_identity=? "
            "ORDER BY created_at, publication_id",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for publication in publications:
            publication_id = str(publication["publication_id"])
            observations = self.conn.execute(
                "SELECT * FROM stage0_publication_observation WHERE publication_id=? AND data_identity=? "
                "ORDER BY observed_at, observation_id",
                (publication_id, self.data_identity),
            ).fetchall()
            reviews = self.conn.execute(
                "SELECT * FROM stage0_publication_p7_review WHERE publication_id=? AND data_identity=? "
                "ORDER BY decided_at, review_id",
                (publication_id, self.data_identity),
            ).fetchall()
            results.append({
                "publication": {key: row[key] for key in publication.keys()},
                "observations": [
                    {
                        key: self._knowledge_json(row[key]) if key == "metrics_json" else row[key]
                        for key in observation.keys()
                    }
                    for observation in observations
                ],
                "reviews": [
                    {
                        key: self._knowledge_json(row[key]) if key == "feedback_candidate_json" else row[key]
                        for key in review.keys()
                    }
                    for review in reviews
                ],
            })
        return results

    @staticmethod
    def _split_platform_account_ref(external_account_ref: str) -> tuple[str, str]:
        value = str(external_account_ref or "").strip()
        platform, separator, account_ref = value.partition(":")
        if not separator:
            return "", value
        return platform.strip().casefold(), account_ref.strip()

    def _assert_account_domain_ownership(
        self,
        *,
        external_account_ref: str,
        domain_label: str,
        account_role: str,
    ) -> None:
        """Reject cross-domain reuse before any account row or sync update."""
        ref = str(external_account_ref or "").strip()
        if not ref:
            return
        existing = self.conn.execute(
            "SELECT account_role, domain_label FROM stage0_content_account "
            "WHERE external_account_ref=? AND data_identity=?",
            (ref, self.data_identity),
        ).fetchall()
        for row in existing:
            existing_domain = str(row["domain_label"] or "").strip()
            if existing_domain and existing_domain != domain_label:
                raise StateTransitionError(
                    f"external account {ref} already belongs to domain {existing_domain}; "
                    "cross-domain reuse and overwrite are forbidden"
                )
            if existing_domain == domain_label and str(row["account_role"] or "") != account_role:
                raise StateTransitionError(
                    f"external account {ref} is already registered with a different account role"
                )
        platform, account_ref = self._split_platform_account_ref(ref)
        if platform and account_ref:
            formal_rows = self.conn.execute(
                "SELECT domain_label FROM competitor_accounts WHERE platform=? AND sec_uid=?",
                (platform, account_ref.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]),
            ).fetchall()
            for row in formal_rows:
                existing_domain = str(row["domain_label"] or "").strip()
                if existing_domain and existing_domain != domain_label:
                    raise StateTransitionError(
                        f"external account {ref} is already active in domain {existing_domain}; "
                        "the original domain cannot be overwritten"
                    )

    def register_content_account(
        self,
        *,
        content_account_id: str,
        account_role: str,
        display_name: str,
        domain_label: str,
        external_account_ref: str | None,
        actor: str,
    ) -> dict[str, str]:
        """Register an owned or competitor account as a formal content subject."""
        if account_role not in CONTENT_ACCOUNT_ROLES:
            raise StateTransitionError("content account role is not supported")
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("content account requires a configured formal domain")
        if not content_account_id.strip() or not display_name.strip() or not actor.strip():
            raise StateTransitionError("content account requires an identity, display name and actor")
        if account_role == "competitor" and not str(external_account_ref or "").strip():
            raise StateTransitionError("competitor account requires its external account reference")
        self._assert_account_domain_ownership(
            external_account_ref=str(external_account_ref or ""),
            domain_label=domain_label,
            account_role=account_role,
        )
        now = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_content_account(
                    content_account_id, account_role, display_name, domain_label, external_account_ref,
                    status, data_identity, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    content_account_id.strip(), account_role, display_name.strip(), domain_label,
                    str(external_account_ref).strip() if external_account_ref else None,
                    self.data_identity, actor.strip(), now,
                ),
            )
        return {"content_account_id": content_account_id.strip(), "account_role": account_role, "created_at": now}

    def get_current_domain_activation(self, *, domain_label: str) -> dict[str, Any] | None:
        """Return the one current activation, without consulting old domain rows."""
        label = str(domain_label or "").strip()
        if not label:
            raise StateTransitionError("current domain activation requires a domain")
        self.require_domain_activation_schema()
        row = self.conn.execute(
            "SELECT * FROM stage0_domain_activation "
            "WHERE domain_label=? AND data_identity=? AND is_current=1",
            (label, self.data_identity),
        ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else None

    def domain_activation_schema_available(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='stage0_domain_activation'"
        ).fetchone()
        return row is not None

    def require_domain_activation_schema(self) -> None:
        """Reject pre-activation databases instead of treating them as zero-state."""
        if not self.domain_activation_schema_available():
            raise StateTransitionError(
                "activation schema not initialized; migration required"
            )

    def domain_has_activation_history(self, *, domain_label: str) -> bool:
        """Tell callers whether this domain has entered the activation-aware layout."""
        label = str(domain_label or "").strip()
        if not label:
            raise StateTransitionError("domain activation history requires a domain")
        self.require_domain_activation_schema()
        row = self.conn.execute(
            "SELECT 1 FROM stage0_domain_activation "
            "WHERE domain_label=? AND data_identity=? LIMIT 1",
            (label, self.data_identity),
        ).fetchone()
        return row is not None

    def get_current_domain_configuration(self, *, domain_label: str) -> dict[str, Any] | None:
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            return None
        return self.get_cold_start_configuration(
            configuration_id=str(activation["configuration_id"])
        )

    def current_domain_tag_ids(
        self, *, domain_label: str
    ) -> tuple[str, ...] | None:
        """Return tags approved by the current cold-start, or legacy fallback state."""
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            return None if not self.domain_has_activation_history(domain_label=domain_label) else ()
        row = self.conn.execute(
            "SELECT tag_ids_json FROM stage0_cold_start_tag_library "
            "WHERE cold_start_id=? AND domain_label=? AND data_identity=? "
            "AND status='accepted'",
            (
                activation["cold_start_id"],
                str(domain_label).strip(),
                self.data_identity,
            ),
        ).fetchone()
        if row is None:
            return ()
        try:
            values = json.loads(str(row["tag_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("current cold-start tag library is not valid JSON") from exc
        if not isinstance(values, list):
            raise StateTransitionError("current cold-start tag library must contain tag IDs")
        return tuple(str(value).strip() for value in values if str(value).strip())

    def current_domain_content_types_frozen(self, *, domain_label: str) -> bool | None:
        """Check content-type readiness for the current activation only."""
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            return None if not self.domain_has_activation_history(domain_label=domain_label) else False
        return bool(
            self.cold_start_content_types_are_frozen(
                cold_start_id=str(activation["cold_start_id"])
            )
        )

    def current_domain_boundary_frozen(self, *, domain_label: str) -> bool | None:
        """Check production-boundary readiness for the current activation only."""
        activation = self.get_current_domain_activation(domain_label=domain_label)
        if activation is None:
            return None if not self.domain_has_activation_history(domain_label=domain_label) else False
        return bool(
            self.cold_start_domain_boundary_is_frozen(
                cold_start_id=str(activation["cold_start_id"])
            )
        )

    def _ensure_current_domain_activation(
        self, *, domain_label: str, cold_start_id: str, configuration_id: str
    ) -> dict[str, Any]:
        """Bind a newly started run to the domain's current view."""
        label = str(domain_label or "").strip()
        run_id = str(cold_start_id or "").strip()
        config_id = str(configuration_id or "").strip()
        if not label or not run_id or not config_id:
            raise StateTransitionError("current domain activation requires domain, run and configuration")
        current = self.get_current_domain_activation(domain_label=label)
        if current is not None:
            if (
                str(current["cold_start_id"]) == run_id
                and str(current["configuration_id"]) == config_id
            ):
                return current
            raise StateTransitionError("the domain already has another current cold-start activation")
        activation = {
            "activation_id": _id("domain_activation"),
            "domain_label": label,
            "cold_start_id": run_id,
            "configuration_id": config_id,
            "data_identity": self.data_identity,
            "is_current": 1,
            "created_at": _now(),
        }
        self.conn.execute(
            "INSERT INTO stage0_domain_activation("
            "activation_id, domain_label, cold_start_id, configuration_id, data_identity, "
            "is_current, created_at, released_at, released_by, release_reason) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, NULL, NULL, NULL)",
            (
                activation["activation_id"], activation["domain_label"],
                activation["cold_start_id"], activation["configuration_id"],
                activation["data_identity"], activation["created_at"],
            ),
        )
        self._audit(None, "domain_activation_created", {
            "activation_id": activation["activation_id"],
            "domain_label": label,
            "cold_start_id": run_id,
            "configuration_id": config_id,
        })
        return activation

    def _require_current_cold_start_configuration(
        self, configuration: dict[str, Any]
    ) -> None:
        """Keep lifecycle commands attached to the current activation only."""
        domain = str(configuration.get("domain_label") or "").strip()
        activation = self.get_current_domain_activation(domain_label=domain)
        if activation is None:
            raise StateTransitionError(
                "the cold-start configuration belongs to a released domain activation"
            )
        if str(activation["configuration_id"]) != str(configuration["configuration_id"]):
            raise StateTransitionError(
                "the cold-start configuration is not the domain's current activation"
            )
        configured_run = str(configuration.get("cold_start_id") or "").strip()
        if configured_run and str(activation["cold_start_id"]) != configured_run:
            raise StateTransitionError(
                "the cold-start run is not the domain's current activation"
            )

    def reset_domain(self, *, domain_label: str, actor: str) -> dict[str, Any]:
        """Release only the current domain view; all historical facts remain."""
        label = str(domain_label or "").strip()
        actor_value = str(actor or "").strip()
        if not label or not actor_value:
            raise StateTransitionError("domain reset requires a domain and actor")
        current = self.get_current_domain_activation(domain_label=label)
        if current is None:
            return {
                "domain_label": label,
                "reset": False,
                "status": "no_current_activation",
                "released_activation_id": None,
            }
        released_at = _now()
        with self.conn:
            updated = self.conn.execute(
                "UPDATE stage0_domain_activation SET is_current=0, released_at=?, "
                "released_by=?, release_reason=? "
                "WHERE activation_id=? AND data_identity=? AND is_current=1",
                (
                    released_at, actor_value, "user_requested_domain_reset",
                    current["activation_id"], self.data_identity,
                ),
            ).rowcount
            if not updated:
                raise StateTransitionError("the domain current activation changed before reset completed")
            self._audit(None, "domain_activation_released", {
                "activation_id": current["activation_id"],
                "domain_label": label,
                "cold_start_id": current["cold_start_id"],
                "configuration_id": current["configuration_id"],
                "actor": actor_value,
            })
        return {
            "domain_label": label,
            "reset": True,
            "status": "reset",
            "released_activation_id": str(current["activation_id"]),
            "cold_start_id": str(current["cold_start_id"]),
            "configuration_id": str(current["configuration_id"]),
        }

    def _legacy_domain_business_state(self, *, domain_label: str) -> dict[str, Any]:
        """Read the pre-activation layout only for databases not yet migrated."""
        label = str(domain_label or "").strip()
        if not label:
            raise StateTransitionError("domain zero-state check requires a domain")
        blockers: list[dict[str, Any]] = []
        ignored_tables = {
            "stage0_content_account",
            "stage0_unregistered_account_video",
            "stage0_unregistered_account_review",
            "stage0_cold_start_onboarding_failure",
        }
        table_rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table_row in table_rows:
            table = str(table_row["name"])
            if table in ignored_tables:
                continue
            columns = {
                str(column["name"])
                for column in self.conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")').fetchall()
            }
            if "domain_label" not in columns or "data_identity" not in columns:
                continue
            if table == "stage0_cold_start_configuration":
                rows = self.conn.execute(
                    "SELECT configuration_id, status, cold_start_id FROM stage0_cold_start_configuration "
                    "WHERE domain_label=? AND data_identity=? "
                    "AND (status IN ('started', 'completed') OR COALESCE(cold_start_id, '')<>'')",
                    (label, self.data_identity),
                ).fetchall()
            else:
                status_column = "status" if "status" in columns else None
                if status_column:
                    rows = self.conn.execute(
                        f'SELECT 1 AS _rowid, status FROM "{table.replace(chr(34), chr(34) * 2)}" '
                        f'WHERE domain_label=? AND data_identity=? '
                        f"AND COALESCE(status, '') NOT IN ('cancelled', 'rejected', 'failed', 'draft', 'pending_confirmation')",
                        (label, self.data_identity),
                    ).fetchall()
                else:
                    rows = self.conn.execute(
                        f'SELECT 1 AS _rowid FROM "{table.replace(chr(34), chr(34) * 2)}" '
                        "WHERE domain_label=? AND data_identity=?",
                        (label, self.data_identity),
                    ).fetchall()
            for row in rows:
                row_keys = set(row.keys())
                row_id = (
                    row["_rowid"] if "_rowid" in row_keys
                    else row["configuration_id"] if "configuration_id" in row_keys
                    else ""
                )
                blockers.append({
                    "table": table,
                    "row_id": str(row_id or ""),
                    "status": str(row["status"] if "status" in row_keys else "active"),
                })
        return {"domain_label": label, "zero_state": not blockers, "blockers": blockers}

    def domain_business_state(self, *, domain_label: str) -> dict[str, Any]:
        """Return current activation state; old rows are not a current-state source."""
        label = str(domain_label or "").strip()
        if not label:
            raise StateTransitionError("domain zero-state check requires a domain")
        activation = self.get_current_domain_activation(domain_label=label)
        if activation is None:
            return {"domain_label": label, "zero_state": True, "blockers": []}

        blockers: list[dict[str, Any]] = [{
            "table": "stage0_domain_activation",
            "row_id": str(activation["activation_id"]),
            "status": "current",
        }]
        configuration = self.get_cold_start_configuration(
            configuration_id=str(activation["configuration_id"])
        )
        blockers.append({
            "table": "stage0_cold_start_configuration",
            "row_id": str(configuration["configuration_id"]),
            "status": str(configuration["status"]),
        })
        run = self.conn.execute(
            "SELECT status FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (activation["cold_start_id"], self.data_identity),
        ).fetchone()
        if run is not None:
            blockers.append({
                "table": "stage0_cold_start",
                "row_id": str(activation["cold_start_id"]),
                "status": str(run["status"]),
            })
        return {"domain_label": label, "zero_state": False, "blockers": blockers}

    def require_domain_zero_state(self, *, domain_label: str) -> dict[str, Any]:
        state = self.domain_business_state(domain_label=domain_label)
        if not state["zero_state"]:
            summary = "; ".join(
                f"{item['table']}:{item['status']}" for item in state["blockers"][:8]
            )
            raise StateTransitionError(
                f"domain {domain_label} already has formal cold-start/business state; "
                f"new cold start is forbidden ({summary})"
            )
        return state

    def record_cold_start_onboarding_failure(
        self,
        *,
        configuration_id: str | None,
        domain_label: str,
        input_snapshot: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        failure = {
            "failure_id": _id("cold_start_onboarding_failure"),
            "configuration_id": str(configuration_id or "") or None,
            "domain_label": str(domain_label or "").strip(),
            "input_snapshot": input_snapshot,
            "cold_start_contract_version": COLD_START_CONTRACT_VERSION,
            "reason": str(reason or "").strip() or "cold-start creation failed",
            "failed_at": _now(),
            "data_identity": self.data_identity,
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_cold_start_onboarding_failure("
                "failure_id, configuration_id, domain_label, input_snapshot_json, "
                "cold_start_contract_version, reason, failed_at, data_identity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    failure["failure_id"], failure["configuration_id"], failure["domain_label"],
                    _canonical(input_snapshot), failure["cold_start_contract_version"],
                    failure["reason"], failure["failed_at"], self.data_identity,
                ),
            )
        return failure

    def discard_unstarted_cold_start_configuration(
        self,
        *,
        configuration_id: str,
        preflight_receipt_id: str | None = None,
    ) -> None:
        """Remove only the just-created, never-started onboarding records."""
        configuration = self.get_cold_start_configuration(configuration_id=configuration_id)
        cold_start_id = str(configuration.get("cold_start_id") or "").strip()
        if cold_start_id:
            existing_run = self.conn.execute(
                "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.data_identity),
            ).fetchone()
            if existing_run is not None:
                raise StateTransitionError("a cold-start run already exists and cannot be discarded")
        account_ids = [str(configuration["owned_account_id"]), *[
            str(item) for item in configuration.get("competitor_account_ids") or []
        ]]
        other_account_ids: set[str] = set()
        for row in self.conn.execute(
            "SELECT configuration_id, owned_account_id, competitor_account_ids_json "
            "FROM stage0_cold_start_configuration WHERE data_identity=? AND configuration_id<>?",
            (self.data_identity, configuration_id),
        ).fetchall():
            other_account_ids.add(str(row["owned_account_id"]))
            try:
                other_account_ids.update(str(item) for item in json.loads(row["competitor_account_ids_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        with self.conn:
            if preflight_receipt_id:
                self.conn.execute(
                    "DELETE FROM stage0_live_cold_start_preflight WHERE preflight_receipt_id=? "
                    "AND data_identity=? AND status='ready'",
                    (preflight_receipt_id, self.data_identity),
                )
            self.conn.execute(
                "DELETE FROM stage0_cold_start_configuration WHERE configuration_id=? AND data_identity=?",
                (configuration_id, self.data_identity),
            )
            for account_id in account_ids:
                if account_id in other_account_ids:
                    continue
                registration = self.conn.execute(
                    "SELECT 1 FROM stage0_competitor_registration WHERE competitor_account_id=? AND data_identity=?",
                    (account_id, self.data_identity),
                ).fetchone()
                if registration is None:
                    self.conn.execute(
                        "DELETE FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
                        (account_id, self.data_identity),
                    )

    def configure_cold_start_subjects(
        self,
        *,
        domain_mode: str,
        domain_label: str,
        domain_name: str,
        platform: str,
        owned_account: dict[str, str],
        competitor_accounts: tuple[dict[str, str], ...],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Confirm one complete cold-start configuration through the formal Core entry."""
        required = (
            domain_mode, domain_label, domain_name,
            platform, str(owned_account.get("display_name") or ""),
            str(owned_account.get("external_account_ref") or ""),
        )
        if not all(str(value).strip() for value in required):
            raise StateTransitionError("cold-start configuration requires domain, owned account and platform")
        if domain_mode not in {"reuse", "create"}:
            raise StateTransitionError("cold-start domain mode must be reuse or create")
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("cold-start configuration requires a configured formal domain")
        self.require_domain_zero_state(domain_label=domain_label)
        if len(competitor_accounts) != COLD_START_COMPETITOR_MIN:
            raise StateTransitionError(
                f"cold-start configuration requires exactly {COLD_START_COMPETITOR_MIN} competitor accounts"
            )
        competitor_refs = [str(item.get("external_account_ref") or "").strip() for item in competitor_accounts]
        competitor_names = [str(item.get("display_name") or "").strip() for item in competitor_accounts]
        owned_ref = str(owned_account.get("external_account_ref") or "").strip()
        if not all(competitor_refs) or not all(competitor_names):
            raise StateTransitionError("every competitor account requires a name and platform identity")
        if len(set(competitor_refs)) != len(competitor_refs) or owned_ref in set(competitor_refs):
            raise StateTransitionError("owned and competitor account identities must be distinct")
        self._assert_account_domain_ownership(
            external_account_ref=owned_ref, domain_label=domain_label, account_role="owned"
        )
        for competitor_ref in competitor_refs:
            self._assert_account_domain_ownership(
                external_account_ref=competitor_ref, domain_label=domain_label, account_role="competitor"
            )

        def ensure_account(role: str, display_name: str, external_ref: str) -> str:
            row = self.conn.execute(
                "SELECT content_account_id FROM stage0_content_account WHERE account_role=? AND domain_label=? "
                "AND external_account_ref=? AND data_identity=?",
                (role, domain_label, external_ref, self.data_identity),
            ).fetchone()
            if row is not None:
                return str(row["content_account_id"])
            account_id = _id(f"{role}_account")
            self.conn.execute(
                "INSERT INTO stage0_content_account(content_account_id, account_role, display_name, domain_label, "
                "external_account_ref, status, data_identity, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (account_id, role, display_name, domain_label, external_ref, self.data_identity, "", _now()),
            )
            return account_id

        configuration_id = _id("cold_start_configuration")
        now = _now()
        with self.conn:
            owned_account_id = ensure_account(
                "owned", str(owned_account["display_name"]).strip(), owned_ref,
            )
            competitor_account_ids = [
                ensure_account("competitor", name, ref)
                for name, ref in zip(competitor_names, competitor_refs)
            ]
            self.conn.execute(
                "INSERT INTO stage0_cold_start_configuration(configuration_id, domain_mode, "
                "domain_label, domain_name, domain_boundary, platform, owned_account_id, "
                "competitor_account_ids_json, status, data_identity, confirmed_by, confirmed_at, cold_start_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, NULL)",
                (
                    configuration_id, domain_mode, domain_label,
                    domain_name.strip(), "", platform.strip(), owned_account_id,
                    _canonical(competitor_account_ids), self.data_identity, "", now,
                ),
            )
            self._audit(None, "cold_start_configuration_confirmed_by_user", {
                "configuration_id": configuration_id, "domain_mode": domain_mode,
                "domain_label": domain_label, "owned_account_id": owned_account_id,
                "competitor_account_ids": competitor_account_ids,
            })
        return self.get_cold_start_configuration(configuration_id=configuration_id)

    def get_cold_start_configuration(self, *, configuration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT configuration_id, domain_mode, domain_label, domain_name, domain_boundary, "
            "platform, owned_account_id, competitor_account_ids_json, status, data_identity, "
            "confirmed_by, confirmed_at, cold_start_id "
            "FROM stage0_cold_start_configuration WHERE configuration_id=? AND data_identity=?",
            (configuration_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("cold-start configuration does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        result["competitor_account_ids"] = json.loads(result.pop("competitor_account_ids_json"))
        account_ids = [result["owned_account_id"], *result["competitor_account_ids"]]
        result["accounts"] = [
            {key: account[key] for key in account.keys()}
            for account_id in account_ids
            for account in [self.conn.execute(
                "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
                (account_id, self.data_identity),
            ).fetchone()]
            if account is not None
        ]
        return result

    def list_cold_start_configurations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT configuration_id FROM stage0_cold_start_configuration WHERE data_identity=? "
            "ORDER BY confirmed_at DESC, configuration_id DESC",
            (self.data_identity,),
        ).fetchall()
        return [self.get_cold_start_configuration(configuration_id=row["configuration_id"]) for row in rows]

    def get_cold_start_run_model_binding(
        self, *, cold_start_id: str
    ) -> dict[str, Any]:
        """Return the credential-free model binding for the current execution."""
        row = self.conn.execute(
            "SELECT input_snapshot_json FROM stage0_cold_start_run_contract "
            "WHERE cold_start_id=? AND data_identity=?",
            (str(cold_start_id or "").strip(), self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("cold-start run has no execution model binding")
        try:
            snapshot = json.loads(str(row["input_snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("cold-start run contract is not valid JSON") from exc
        binding = snapshot.get("run_model") if isinstance(snapshot, dict) else None
        required = ("route_id", "provider_ref", "provider_name", "provider_type", "model_name", "endpoint", "source")
        if not isinstance(binding, dict) or any(
            not str(binding.get(key) or "").strip() for key in required
        ):
            raise StateTransitionError("cold-start run has no complete execution model binding")
        return dict(binding)

    def _replace_cold_start_run_model_binding(
        self,
        *,
        cold_start_id: str,
        task_model_binding: dict[str, Any],
    ) -> dict[str, Any]:
        """Store the model binding for the next execution of an existing run."""
        try:
            normalized = HermesTaskModelBinding.from_payload(task_model_binding).as_payload()
        except Exception as exc:
            raise StateTransitionError(
                f"cold-start resume requires a complete current model binding: {exc}"
            ) from exc
        row = self.conn.execute(
            "SELECT input_snapshot_json FROM stage0_cold_start_run_contract "
            "WHERE cold_start_id=? AND data_identity=?",
            (str(cold_start_id or "").strip(), self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("cold-start run has no run contract to update")
        try:
            snapshot = json.loads(str(row["input_snapshot_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("cold-start run contract is not valid JSON") from exc
        if not isinstance(snapshot, dict):
            raise StateTransitionError("cold-start run contract snapshot must be an object")
        snapshot["run_model"] = dict(normalized)
        self.conn.execute(
            "UPDATE stage0_cold_start_run_contract SET input_snapshot_json=? "
            "WHERE cold_start_id=? AND data_identity=?",
            (_canonical(snapshot), str(cold_start_id or "").strip(), self.data_identity),
        )
        return normalized

    def start_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        preflight_receipt_id: str | None = None,
        task_model_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or continue exactly one run for one confirmed configuration."""
        # Kept in the compatibility signature only.  A Business Run does not
        # carry or resolve a provider/model binding.
        del task_model_binding
        configuration = self.get_cold_start_configuration(configuration_id=configuration_id)
        cold_start_id = str(configuration.get("cold_start_id") or f"cold_start_{configuration_id}")
        competitor_snapshot = []
        for account_id in configuration["competitor_account_ids"]:
            account = next(
                (
                    item for item in configuration["accounts"]
                    if str(item.get("content_account_id")) == str(account_id)
                ),
                None,
            )
            if account is not None:
                competitor_snapshot.append({
                    "display_name": str(account.get("display_name") or ""),
                    "external_account_ref": str(account.get("external_account_ref") or ""),
                })
        input_snapshot = {
            "domain_label": str(configuration["domain_label"]),
            "domain_name": str(configuration["domain_name"]),
            "platform": str(configuration["platform"]),
            "owned_account": next(
                (
                    {
                        "display_name": str(item.get("display_name") or ""),
                        "external_account_ref": str(item.get("external_account_ref") or ""),
                    }
                    for item in configuration["accounts"]
                    if item.get("account_role") == "owned"
                ),
                {},
            ),
            "competitor_accounts": competitor_snapshot,
        }
        existing_run = self.conn.execute(
            "SELECT * FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        current_activation = self.get_current_domain_activation(
            domain_label=str(configuration["domain_label"])
        )
        if current_activation is not None and str(current_activation["cold_start_id"]) != cold_start_id:
            raise StateTransitionError(
                "the configured cold-start is not the domain's current activation"
            )
        if existing_run is not None:
            registration_rows = self.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? AND data_identity=? ORDER BY created_at, registration_id",
                (cold_start_id, self.data_identity),
            ).fetchall()
            result = {
                "cold_start_id": cold_start_id,
                "registration_ids": [str(row["registration_id"]) for row in registration_rows],
                "status": str(existing_run["status"]),
            }
        else:
            if configuration["status"] not in {"confirmed", "started"}:
                raise StateTransitionError(
                    "only a confirmed configuration can create or continue a cold-start run"
                )
            try:
                result = self.start_cold_start(
                    cold_start_id=cold_start_id,
                    owned_account_id=configuration["owned_account_id"],
                    competitor_account_ids=tuple(configuration["competitor_account_ids"]),
                    actor=actor,
                    preflight_receipt_id=preflight_receipt_id,
                    run_contract={
                        "domain_label": configuration["domain_label"],
                        "owned_account_id": configuration["owned_account_id"],
                        "competitor_account_ids": list(configuration["competitor_account_ids"]),
                        "input_snapshot": input_snapshot,
                        "cold_start_contract_version": COLD_START_CONTRACT_VERSION,
                    },
                )
            except Exception:
                if preflight_receipt_id:
                    with self.conn:
                        self.conn.execute(
                            "DELETE FROM stage0_live_cold_start_preflight WHERE preflight_receipt_id=? "
                            "AND data_identity=? AND status='ready'",
                            (preflight_receipt_id, self.data_identity),
                        )
                raise
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='started', cold_start_id=? "
                "WHERE configuration_id=? AND data_identity=? AND status IN ('confirmed', 'started')",
                (cold_start_id, configuration_id, self.data_identity),
            )
            self._ensure_current_domain_activation(
                domain_label=str(configuration["domain_label"]),
                cold_start_id=cold_start_id,
                configuration_id=configuration_id,
            )
            self._audit(None, "configured_cold_start_started", {
                "configuration_id": configuration_id, "cold_start_id": cold_start_id,
                "registration_ids": result["registration_ids"],
                "cold_start_contract_version": COLD_START_CONTRACT_VERSION,
            })
        return {
            **result,
            "configuration_id": configuration_id,
            "run_model": "",
            "run_model_binding": {},
        }

    def pause_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Pause external execution while preserving resumable formal progress."""
        if not actor.strip() or not reason.strip() or not idempotency_key.strip():
            raise StateTransitionError("pausing a configured cold start requires an actor, reason and idempotency key")
        configuration = self.get_cold_start_configuration(configuration_id=configuration_id)
        request = {
            "configuration_id": configuration_id,
            "actor": actor.strip(),
            "reason": reason.strip(),
        }
        replay = self._replay("pause_configured_cold_start", idempotency_key, request)
        if replay:
            return replay
        self._require_current_cold_start_configuration(configuration)
        if configuration["status"] != "started" or not str(configuration.get("cold_start_id") or "").strip():
            raise StateTransitionError("only a started configured cold start can be paused")
        result = {
            "configuration_id": configuration_id,
            "cold_start_id": str(configuration["cold_start_id"]),
            "status": "confirmed",
            "resumable": True,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='confirmed' "
                "WHERE configuration_id=? AND data_identity=? AND status='started'",
                (configuration_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_paused", {
                **result,
                "actor": actor.strip(),
                "reason": reason.strip(),
            })
            self._receipt("pause_configured_cold_start", idempotency_key, request, result)
        return result

    def stop_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Stop one existing run without command-level replay/dedup state."""
        reason_value = str(reason or "").strip()
        if not reason_value:
            raise StateTransitionError("stopping a configured cold start requires a reason")
        configuration = self.get_cold_start_configuration(
            configuration_id=configuration_id
        )
        self._require_current_cold_start_configuration(configuration)
        cold_start_id = str(configuration.get("cold_start_id") or "").strip()
        if not cold_start_id:
            raise StateTransitionError("there is no existing cold-start run to stop")
        run = self.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("the configured cold-start run does not exist")
        prior_status = str(run["status"])
        if prior_status == "stopped":
            return {
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "status": "stopped",
                "prior_status": "stopped",
                "stopped": True,
                "created_new_run": False,
                "message": "当前冷启动已经停止。",
            }
        if prior_status != "running":
            raise StateTransitionError("only a running cold start can be stopped")
        result = {
            "configuration_id": configuration_id,
            "cold_start_id": cold_start_id,
            "status": "stopped",
            "prior_status": prior_status,
            "resumable": True,
            "reason": reason_value,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start SET status='stopped' "
                "WHERE cold_start_id=? AND data_identity=? AND status='running'",
                (cold_start_id, self.data_identity),
            )
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='cancelled' "
                "WHERE configuration_id=? AND data_identity=? AND status<>'completed'",
                (configuration_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_stopped", {
                **result,
            })
        return result

    def resume_stopped_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str | None = None,
        task_model_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reactivate the same run without changing its business identity."""
        del task_model_binding
        configuration = self.get_cold_start_configuration(
            configuration_id=configuration_id
        )
        self._require_current_cold_start_configuration(configuration)
        cold_start_id = str(configuration.get("cold_start_id") or "").strip()
        if not cold_start_id:
            raise StateTransitionError("there is no existing cold-start run to resume")
        run = self.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("the configured cold-start run does not exist")
        prior_status = str(run["status"])
        if prior_status == "running":
            return {
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "status": "running",
                "prior_status": "running",
                "resumed": False,
                "created_new_run": False,
                "message": "当前冷启动已经在运行。",
            }
        if prior_status not in {"stopped", "failed"}:
            raise StateTransitionError("only a stopped or failed cold start can be resumed")
        if configuration["status"] not in {"started", "cancelled"}:
            raise StateTransitionError("the interrupted cold-start configuration cannot be resumed")
        with self.conn:
            result = {
                "configuration_id": configuration_id,
                "cold_start_id": cold_start_id,
                "status": "running",
                "prior_status": prior_status,
                "resumed": True,
                "created_new_run": False,
                "run_model": "",
                "run_model_binding": {},
            }
            self.conn.execute(
                "UPDATE stage0_cold_start SET status='running', "
                "completed_at=NULL WHERE cold_start_id=? AND data_identity=? "
                "AND status IN ('stopped', 'failed')",
                (cold_start_id, self.data_identity),
            )
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='started' "
                "WHERE configuration_id=? AND data_identity=? AND status IN ('started', 'cancelled')",
                (configuration_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_resumed", {
                **result,
            })
        return result

    def fail_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Record an execution failure without changing completed work."""

        reason_value = str(reason or "").strip()
        if not reason_value:
            raise StateTransitionError("failing a configured cold start requires a reason")
        configuration = self.get_cold_start_configuration(
            configuration_id=configuration_id
        )
        self._require_current_cold_start_configuration(configuration)
        cold_start_id = str(configuration.get("cold_start_id") or "").strip()
        if not cold_start_id:
            raise StateTransitionError("there is no existing cold-start run to fail")
        run = self.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if run is None or str(run["status"]) != "running":
            raise StateTransitionError("only a running cold start can fail")
        result = {
            "configuration_id": configuration_id,
            "cold_start_id": cold_start_id,
            "status": "failed",
            "prior_status": "running",
            "resumable": True,
            "reason": reason_value,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start SET status='failed' "
                "WHERE cold_start_id=? AND data_identity=? AND status='running'",
                (cold_start_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_failed", {
                **result,
            })
        return result

    def refresh_cold_start_run_lifecycle(
        self, *, cold_start_id: str, actor: str = "system"
    ) -> dict[str, Any]:
        """Derive running or waiting-human from existing detailed progress."""

        run = self.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("cold-start lifecycle refresh requires an existing run")
        prior_status = str(run["status"])
        if prior_status in {"stopped", "failed", "completed"}:
            return {
                "cold_start_id": cold_start_id,
                "status": prior_status,
                "prior_status": prior_status,
                "changed": False,
            }
        registrations = self.conn.execute(
            "SELECT current_step, status FROM stage0_competitor_registration "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchall()
        if any(str(row["status"]) == "failed" for row in registrations):
            target_status = "failed"
        else:
            automatic_registration_work = any(
                str(row["status"]) == "processing"
                or (
                    str(row["status"]) == "awaiting_human_review"
                    and str(row["current_step"]) != "historical_material"
                )
                for row in registrations
            )
            orchestration = self.get_cold_start_orchestration_status(
                cold_start_id=cold_start_id
            )
            tag_exists = self.conn.execute(
                "SELECT 1 FROM stage0_cold_start_tag_library "
                "WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.data_identity),
            ).fetchone() is not None
            content_type = self.conn.execute(
                "SELECT status FROM stage0_cold_start_content_type_candidate "
                "WHERE cold_start_id=? AND data_identity=? "
                "ORDER BY created_at DESC, candidate_version DESC LIMIT 1",
                (cold_start_id, self.data_identity),
            ).fetchone()
            boundary = self.conn.execute(
                "SELECT status FROM stage0_cold_start_domain_boundary_candidate "
                "WHERE cold_start_id=? AND data_identity=? "
                "ORDER BY created_at DESC, candidate_version DESC LIMIT 1",
                (cold_start_id, self.data_identity),
            ).fetchone()
            if any(
                row is not None and str(row["status"]) == "failed"
                for row in (content_type, boundary)
            ):
                target_status = "failed"
            else:
                automatic_branch_work = (
                    bool(orchestration["tag_input_ready"]) and not tag_exists
                ) or (
                    bool(orchestration["breakdown_complete"])
                    and (content_type is None or boundary is None)
                )
                target_status = (
                    "running"
                    if automatic_registration_work or automatic_branch_work
                    else "waiting_human"
                )
        changed = target_status != prior_status
        if changed:
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_cold_start SET status=? "
                    "WHERE cold_start_id=? AND data_identity=? "
                    "AND status IN ('running', 'waiting_human')",
                    (target_status, cold_start_id, self.data_identity),
                )
                self._audit(None, "cold_start_run_lifecycle_refreshed", {
                    "cold_start_id": cold_start_id,
                    "prior_status": prior_status,
                    "status": target_status,
                    "actor": str(actor or "system").strip() or "system",
                })
        return {
            "cold_start_id": cold_start_id,
            "status": target_status,
            "prior_status": prior_status,
            "changed": changed,
        }


    def record_live_cold_start_preflight(
        self,
        *,
        domain_label: str,
        owned_account_id: str,
        competitor_account_ids: tuple[str, ...],
        voice_profile_id: str,
        report: dict[str, Any],
    ) -> dict[str, str]:
        """Issue a short-lived receipt only for a machine report whose every required check passed."""
        checks = report.get("checks")
        if (
            self.data_identity != "production" or report.get("ready") is not True
            or report.get("domain_label") != domain_label or not isinstance(checks, list) or not checks
            or any(
                not isinstance(item, dict)
                or (
                    item.get("required_for_cold_start") is not False
                    and item.get("passed") is not True
                )
                for item in checks
            )
        ):
            raise StateTransitionError("live cold-start preflight receipt requires a complete passing production report")
        if not owned_account_id.strip() or not competitor_account_ids:
            raise StateTransitionError("live cold-start preflight receipt requires all cold-start subjects")
        receipt_id, inspected_at = _id("live_preflight"), _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_live_cold_start_preflight VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?)",
                (
                    receipt_id, domain_label, owned_account_id,
                    _canonical(list(competitor_account_ids)), voice_profile_id,
                    _canonical(report), _hash(report), self.data_identity, inspected_at,
                ),
            )
            self._audit(None, "live_cold_start_preflight_ready", {
                "preflight_receipt_id": receipt_id, "domain_label": domain_label,
            })
        return {"preflight_receipt_id": receipt_id, "status": "ready", "inspected_at": inspected_at}

    def start_cold_start(
        self,
        *,
        cold_start_id: str,
        owned_account_id: str,
        competitor_account_ids: tuple[str, ...],
        actor: str | None = None,
        preflight_receipt_id: str | None = None,
        run_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start all required competitor registrations from one owned-account and domain decision."""
        if not cold_start_id.strip() or not competitor_account_ids:
            raise StateTransitionError("cold start requires an owned account and 20 competitors")
        if len(competitor_account_ids) != COLD_START_COMPETITOR_MIN:
            raise StateTransitionError(
                f"a complete cold start requires exactly {COLD_START_COMPETITOR_MIN} competitor accounts"
            )
        if len(set(competitor_account_ids)) != len(competitor_account_ids):
            raise StateTransitionError("cold start competitor accounts must be distinct")
        owned = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?", (owned_account_id, self.data_identity)
        ).fetchone()
        if owned is None or owned["account_role"] != "owned" or owned["status"] != "active":
            raise StateTransitionError("cold start requires an active owned account")
        competitors = [
            self.conn.execute(
                "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?", (account_id, self.data_identity)
            ).fetchone()
            for account_id in competitor_account_ids
        ]
        if any(row is None or row["account_role"] != "competitor" or row["status"] != "active" for row in competitors):
            raise StateTransitionError("cold start requires active registered competitor accounts")
        if any(str(row["domain_label"]) != str(owned["domain_label"]) for row in competitors if row is not None):
            raise StateTransitionError("cold start accounts must belong to one domain")
        preflight = None
        if self.data_identity == "production" and preflight_receipt_id:
            preflight = self.conn.execute(
                "SELECT * FROM stage0_live_cold_start_preflight WHERE preflight_receipt_id=? AND data_identity=?",
                (str(preflight_receipt_id or ""), self.data_identity),
            ).fetchone()
            if (
                preflight is None or (
                    preflight["status"] != "ready"
                    and not (preflight["status"] == "consumed" and preflight["consumed_by_cold_start_id"] == cold_start_id)
                )
                or preflight["domain_label"] != owned["domain_label"]
                or preflight["owned_account_id"] != owned_account_id
                or json.loads(preflight["competitor_account_ids_json"]) != list(competitor_account_ids)
            ):
                raise StateTransitionError("production cold start requires a matching unused passing live preflight receipt")
            try:
                inspected_at = datetime.fromisoformat(str(preflight["inspected_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise StateTransitionError("live preflight receipt time is invalid") from exc
            if datetime.now(timezone.utc) - inspected_at > timedelta(minutes=15):
                raise StateTransitionError("live preflight receipt expired; all real connectors must be checked again")
        now = _now()
        registration_ids = [_id("competitor_registration") for _ in competitor_account_ids]
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_cold_start(
                    cold_start_id, owned_account_id, domain_label, status, data_identity, created_by, created_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (cold_start_id, owned_account_id, owned["domain_label"], self.data_identity, "", now),
            )
            if run_contract is not None:
                input_snapshot = run_contract.get("input_snapshot")
                if not isinstance(input_snapshot, dict):
                    raise StateTransitionError(
                        "cold-start run contract requires an input snapshot"
                    )
                self.conn.execute(
                    "INSERT INTO stage0_cold_start_run_contract("
                    "cold_start_id, domain_label, owned_account_id, competitor_account_ids_json,"
                    "input_snapshot_json, cold_start_contract_version, created_at, data_identity"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cold_start_id, str(run_contract["domain_label"]), owned_account_id,
                        _canonical(list(competitor_account_ids)), _canonical(input_snapshot),
                        str(run_contract["cold_start_contract_version"]), now, self.data_identity,
                    ),
                )
            if preflight is not None and preflight["status"] == "ready":
                self.conn.execute(
                    "UPDATE stage0_live_cold_start_preflight SET status='consumed', consumed_by_cold_start_id=? WHERE preflight_receipt_id=?",
                    (cold_start_id, preflight["preflight_receipt_id"]),
                )
            for registration_id, competitor_id in zip(registration_ids, competitor_account_ids):
                self.conn.execute(
                    """
                    INSERT INTO stage0_competitor_registration(
                        registration_id, cold_start_id, competitor_account_id, current_step, status, data_identity, created_at
                    ) VALUES (?, ?, ?, 'historical_material', 'processing', ?, ?)
                    """,
                    (registration_id, cold_start_id, competitor_id, self.data_identity, now),
                )
            result = {"cold_start_id": cold_start_id, "registration_ids": registration_ids, "status": "running"}
            self._audit(None, "cold_start_started", result)
        return result

    def cold_start_original_competitor_account_ids(
        self, *, cold_start_id: str
    ) -> tuple[str, ...] | None:
        """Return the frozen basic-chain account set, excluding later additions."""
        row = self.conn.execute(
            "SELECT competitor_account_ids_json FROM stage0_cold_start_run_contract "
            "WHERE cold_start_id=? AND data_identity=?",
            (str(cold_start_id), self.data_identity),
        ).fetchone()
        if row is None:
            return None
        values = json.loads(str(row["competitor_account_ids_json"]))
        if not isinstance(values, list) or any(not str(value).strip() for value in values):
            raise StateTransitionError("cold-start run contract has invalid competitor accounts")
        return tuple(str(value).strip() for value in values)

    def create_incremental_competitor_registrations(
        self,
        *,
        cold_start_id: str,
        competitor_accounts: tuple[dict[str, Any], ...],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Attach new competitors to an already completed run, without a new run."""
        run_id = str(cold_start_id or "").strip()
        actor_value = str(actor or "").strip() or "system"
        key = str(idempotency_key or "").strip()
        if not run_id or not key or not competitor_accounts:
            raise StateTransitionError("incremental competitor registration requires a run, accounts and idempotency key")
        run = self.conn.execute(
            "SELECT domain_label, status FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        configuration = self.conn.execute(
            "SELECT status FROM stage0_cold_start_configuration WHERE cold_start_id=? AND data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        if run is None or configuration is None or str(run["status"]) != "completed" or str(configuration["status"]) != "completed":
            raise StateTransitionError("incremental competitors require a completed cold-start and configuration")
        normalized: list[dict[str, str]] = []
        seen_refs: set[str] = set()
        for item in competitor_accounts:
            if not isinstance(item, dict):
                raise StateTransitionError("incremental competitor account must be an object")
            external_ref = str(item.get("external_account_ref") or item.get("account_ref") or "").strip()
            display_name = str(item.get("display_name") or item.get("name") or "").strip()
            if not external_ref or not display_name:
                raise StateTransitionError("incremental competitor account requires a display name and external account reference")
            if external_ref in seen_refs:
                raise StateTransitionError("incremental competitor accounts must be distinct")
            seen_refs.add(external_ref)
            account_id = str(item.get("content_account_id") or "").strip()
            if not account_id:
                account_id = f"content_account_{_hash({'domain': run['domain_label'], 'ref': external_ref})[:24]}"
            normalized.append({"content_account_id": account_id, "display_name": display_name, "external_account_ref": external_ref})
        request = {"cold_start_id": run_id, "competitor_accounts": normalized}
        replay = self._replay("create_incremental_competitor_registrations", key, request)
        if replay:
            return replay
        registration_ids: list[str] = []
        now = _now()
        with self.conn:
            for item in normalized:
                self._assert_account_domain_ownership(
                    external_account_ref=item["external_account_ref"],
                    domain_label=str(run["domain_label"]), account_role="competitor",
                )
                existing = self.conn.execute(
                    "SELECT content_account_id, account_role, domain_label, status FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
                    (item["content_account_id"], self.data_identity),
                ).fetchone()
                if existing is not None:
                    if str(existing["account_role"]) != "competitor" or str(existing["domain_label"]) != str(run["domain_label"]) or str(existing["status"]) != "active":
                        raise StateTransitionError("incremental competitor account identity is already used by another account")
                    account_id = str(existing["content_account_id"])
                else:
                    self.conn.execute(
                        "INSERT INTO stage0_content_account(content_account_id, account_role, display_name, domain_label, external_account_ref, status, data_identity, created_by, created_at) VALUES (?, 'competitor', ?, ?, ?, 'active', ?, ?, ?)",
                        (item["content_account_id"], item["display_name"], str(run["domain_label"]), item["external_account_ref"], self.data_identity, actor_value, now),
                    )
                    account_id = item["content_account_id"]
                duplicate = self.conn.execute(
                    "SELECT registration_id FROM stage0_competitor_registration WHERE cold_start_id=? AND competitor_account_id=? AND data_identity=?",
                    (run_id, account_id, self.data_identity),
                ).fetchone()
                if duplicate is not None:
                    raise StateTransitionError("incremental competitor is already registered in this cold-start run")
                registration_id = _id("competitor_registration")
                self.conn.execute(
                    "INSERT INTO stage0_competitor_registration(registration_id, cold_start_id, competitor_account_id, current_step, status, data_identity, created_at) VALUES (?, ?, ?, 'historical_material', 'processing', ?, ?)",
                    (registration_id, run_id, account_id, self.data_identity, now),
                )
                registration_ids.append(registration_id)
            result = {"cold_start_id": run_id, "registration_ids": registration_ids, "account_count": len(registration_ids), "status": "processing", "created_new_run": False}
            self._receipt("create_incremental_competitor_registrations", key, request, result)
            self._audit(None, "incremental_competitor_registrations_created", {**result, "actor": actor_value})
        return result


    def get_cold_start_orchestration_status(self, *, cold_start_id: str) -> dict[str, Any]:
        """Return the two independent cold-start hand-off counters.

        The tag branch is allowed to start as soon as every registration has
        a completed high-signal artifact.  It deliberately does not inspect
        transcript, comment, or breakdown results.  The counters are derived
        from the existing step receipts so no second run identifier or new
        business state column is needed.
        """
        if not str(cold_start_id or "").strip():
            raise StateTransitionError("cold-start orchestration status requires a run identity")
        cold_start = self.conn.execute(
            "SELECT cold_start_id, domain_label, status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("cold-start run does not exist")
        registration_rows = self.conn.execute(
            "SELECT registration_id, competitor_account_id FROM stage0_competitor_registration "
            "WHERE cold_start_id=? AND data_identity=? ORDER BY created_at, registration_id",
            (cold_start_id, self.data_identity),
        ).fetchall()
        original_account_ids = self.cold_start_original_competitor_account_ids(
            cold_start_id=cold_start_id
        )
        if original_account_ids is not None:
            original_set = set(original_account_ids)
            registration_rows = [
                row for row in registration_rows
                if str(row["competitor_account_id"]) in original_set
            ]
        registration_ids = [str(row["registration_id"]) for row in registration_rows]
        if registration_ids:
            placeholders = ",".join("?" for _ in registration_ids)
            step_rows = self.conn.execute(
                "SELECT registration_id, step_name FROM stage0_competitor_registration_step "
                f"WHERE data_identity=? AND registration_id IN ({placeholders})",
                (self.data_identity, *registration_ids),
            ).fetchall()
        else:
            step_rows = []
        completed_by_step: dict[str, set[str]] = {}
        for row in step_rows:
            completed_by_step.setdefault(str(row["step_name"]), set()).add(
                str(row["registration_id"])
            )

        total = len(registration_ids)
        selection_ids = completed_by_step.get("high_signal_identification", set())
        preparation_ids = completed_by_step.get("transcripts_and_comments", set())
        breakdown_ids = completed_by_step.get("breakdown", set())
        tag_candidate_ids = completed_by_step.get("tag_candidates", set())
        expected = COLD_START_COMPETITOR_MIN
        selection_complete = total == expected and len(selection_ids) == expected
        breakdown_complete = total == expected and len(breakdown_ids) == expected
        return {
            "cold_start_id": str(cold_start["cold_start_id"]),
            "domain_label": str(cold_start["domain_label"]),
            "cold_start_status": str(cold_start["status"]),
            "registration_total": total,
            "expected_registration_total": expected,
            "selection_completed_count": len(selection_ids),
            "selection_completed_registration_ids": [
                registration_id for registration_id in registration_ids if registration_id in selection_ids
            ],
            "selection_complete": selection_complete,
            "tag_input_ready": selection_complete,
            "tag_input_source": "high_signal_identification.selected_items[].title",
            "preparation_completed_count": len(preparation_ids),
            "breakdown_completed_count": len(breakdown_ids),
            "breakdown_complete": breakdown_complete,
            "tag_candidate_step_completed_count": len(tag_candidate_ids),
            "tag_branch_waits_for_breakdown": False,
        }

    def record_cold_start_orchestration_failure(
        self,
        *,
        cold_start_id: str,
        registration_id: str,
        step_name: str,
        actor: str,
        error_type: str,
        reason: str,
    ) -> dict[str, str]:
        """Keep an account-step failure without closing its resumable state."""
        values = {
            "cold_start_id": str(cold_start_id).strip(),
            "registration_id": str(registration_id).strip(),
            "step_name": str(step_name).strip(),
            "actor": str(actor).strip(),
            "error_type": str(error_type).strip() or "unknown",
            "reason": str(reason).strip() or "unknown failure",
        }
        if not values["cold_start_id"] or not values["registration_id"] or not values["step_name"] or not values["actor"]:
            raise StateTransitionError("cold-start orchestration failure needs its run, account, step and actor")
        registration = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration "
            "WHERE registration_id=? AND cold_start_id=? AND data_identity=?",
            (values["registration_id"], values["cold_start_id"], self.data_identity),
        ).fetchone()
        if registration is None:
            raise StateTransitionError("cold-start orchestration failure refers to an unrelated account")
        with self.conn:
            self._audit(None, "cold_start_registration_step_failed", values)
        return {
            "cold_start_id": values["cold_start_id"],
            "registration_id": values["registration_id"],
            "step_name": values["step_name"],
            "status": "recorded_and_resumable",
        }

    def record_competitor_historical_page_progress(
        self,
        *,
        registration_id: str,
        phase: str,
        status: str,
        evaluated_at: int,
        next_cursor: str,
        has_more: bool,
        reached_time_boundary: bool,
        stop_reason: str,
        page_request_count: int,
        raw_archive_refs: list[str] | tuple[str, ...],
        mature_item_count: int,
        recent_item_count: int,
    ) -> dict[str, Any]:
        """Keep resumable page progress in the existing audit stream.

        A cursor is execution progress only.  The page archives remain the
        source for reconstructing collected items after an interruption.
        """
        phase_value = str(phase or "").strip()
        status_value = str(status or "").strip()
        if phase_value not in {"recent_window", "older_backfill"}:
            raise StateTransitionError("historical page progress phase is invalid")
        if status_value not in {"running", "completed", "history_exhausted_insufficient"}:
            raise StateTransitionError("historical page progress status is invalid")
        registration = self.conn.execute(
            "SELECT registration_id, cold_start_id, competitor_account_id "
            "FROM stage0_competitor_registration "
            "WHERE registration_id=? AND data_identity=?",
            (registration_id, self.data_identity),
        ).fetchone()
        if registration is None:
            raise StateTransitionError("historical page progress refers to an unknown registration")
        refs = list(dict.fromkeys(str(value).strip() for value in raw_archive_refs if str(value).strip()))
        if not refs or int(page_request_count) < 1:
            raise StateTransitionError("historical page progress requires a page count and archive receipt")
        payload = {
            "registration_id": str(registration["registration_id"]),
            "cold_start_id": str(registration["cold_start_id"]),
            "phase": phase_value,
            "status": status_value,
            "evaluated_at": int(evaluated_at),
            "next_cursor": str(next_cursor or "").strip(),
            "has_more": bool(has_more),
            "reached_time_boundary": bool(reached_time_boundary),
            "stop_reason": str(stop_reason or "unknown").strip() or "unknown",
            "page_request_count": int(page_request_count),
            "raw_archive_refs": refs,
            "mature_item_count": max(0, int(mature_item_count)),
            "recent_item_count": max(0, int(recent_item_count)),
        }
        with self.conn:
            self._audit(None, "competitor_historical_page_progress", payload)
        return payload

    def get_competitor_historical_page_progress(
        self, *, registration_id: str
    ) -> dict[str, Any] | None:
        self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT payload_json FROM stage0_audit_event "
            "WHERE action='competitor_historical_page_progress' AND data_identity=? "
            "ORDER BY created_at DESC, audit_id DESC",
            (self.data_identity,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and str(payload.get("registration_id") or "") == str(registration_id):
                return payload
        return None

    def get_competitor_registration_history_shortfall(
        self, *, registration_id: str
    ) -> dict[str, Any] | None:
        registration = self.get_competitor_registration(registration_id=registration_id)
        if registration["status"] != "awaiting_human_review":
            return None
        rows = self.conn.execute(
            "SELECT payload_json FROM stage0_audit_event "
            "WHERE action='competitor_historical_collection_exhausted_before_mature_target' "
            "AND data_identity=? ORDER BY created_at DESC, audit_id DESC",
            (self.data_identity,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and str(payload.get("registration_id") or "") == str(registration_id):
                return payload
        return None

    def record_competitor_registration_step(
        self,
        *,
        registration_id: str,
        step_name: str,
        artifact_refs: tuple[dict[str, Any], ...],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Record one completed required registration step; steps cannot be skipped or overwritten."""
        if step_name not in COMPETITOR_REGISTRATION_STEPS or not artifact_refs or not actor.strip():
            raise StateTransitionError("registration step requires its expected name, formal artifacts and actor")
        registration = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration WHERE registration_id=? AND data_identity=?", (registration_id, self.data_identity)
        ).fetchone()
        if registration is None:
            raise StateTransitionError("registration step refers to an unknown registration")
        request = {"registration_id": registration_id, "step_name": step_name, "artifact_refs": list(artifact_refs), "actor": actor}
        replay = self._replay("record_competitor_registration_step", idempotency_key, request)
        if replay:
            return replay
        if registration["status"] != "processing" or registration["current_step"] != step_name:
            raise StateTransitionError("registration step is not the next required step")
        if step_name == "historical_material":
            if len(artifact_refs) != 1:
                raise StateTransitionError("historical collection must contain one formal artifact")
            try:
                validate_historical_collection_artifact(artifact_refs[0])
            except ValueError as exc:
                record_runtime_guard_event(
                    event="formal_historical_collection_write",
                    outcome="blocked",
                    details={"registration_id": registration_id, "error": str(exc)},
                    data_identity=self.data_identity,
                )
                raise StateTransitionError(
                    f"historical collection runtime guard rejected the result: {exc}"
                ) from exc
            record_runtime_guard_event(
                event="formal_historical_collection_write",
                outcome="passed",
                details={"registration_id": registration_id},
                data_identity=self.data_identity,
            )
            if artifact_refs[0].get("collection_status") == "history_exhausted_insufficient":
                result = {
                    "registration_id": registration_id,
                    "status": "awaiting_human_review",
                    "current_step": "awaiting_human_review",
                    "history_insufficient": True,
                    "mature_item_count": int(
                        sum(
                            0 < int(item.get("published_at") or 0)
                            <= int(artifact_refs[0].get("evaluated_at") or 0) - 7 * 86400
                            for item in artifact_refs[0].get("items", [])
                            if isinstance(item, dict)
                        )
                    ),
                }
                with self.conn:
                    self.conn.execute(
                        "UPDATE stage0_competitor_registration "
                        "SET current_step='awaiting_human_review', status='awaiting_human_review' "
                        "WHERE registration_id=? AND data_identity=?",
                        (registration_id, self.data_identity),
                    )
                    self._receipt("record_competitor_registration_step", idempotency_key, request, result)
                    self._audit(
                        None,
                        "competitor_historical_collection_exhausted_before_mature_target",
                        {
                            **result,
                            "cold_start_id": str(registration["cold_start_id"]),
                            "artifact": artifact_refs[0],
                        },
                    )
                return result
        if step_name == "high_signal_identification":
            historical_row = self.conn.execute(
                "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
                "WHERE registration_id=? AND step_name='historical_material' AND data_identity=?",
                (registration_id, self.data_identity),
            ).fetchone()
            if historical_row is None:
                raise StateTransitionError("high-signal screening lacks its historical collection")
            historical_payload = _hide_build_root_paths(json.loads(historical_row["artifact_refs_json"]))
            historical_artifacts = historical_payload.get("artifact_refs", [])
            if (
                len(historical_artifacts) != 1
                or not isinstance(historical_artifacts[0].get("items"), list)
                or len(artifact_refs) != 1
            ):
                raise StateTransitionError("high-signal screening inputs are incomplete")
            try:
                validate_high_signal_artifact(
                    historical_items=historical_artifacts[0]["items"],
                    artifact=artifact_refs[0],
                )
            except ValueError as exc:
                record_runtime_guard_event(
                    event="formal_high_signal_write",
                    outcome="blocked",
                    details={"registration_id": registration_id, "error": str(exc)},
                    data_identity=self.data_identity,
                )
                raise StateTransitionError(f"high-signal runtime guard rejected the result: {exc}") from exc
            record_runtime_guard_event(
                event="formal_high_signal_write",
                outcome="passed",
                details={"registration_id": registration_id},
                data_identity=self.data_identity,
            )
        step_index = COMPETITOR_REGISTRATION_STEPS.index(step_name)
        next_step = (
            COMPETITOR_REGISTRATION_STEPS[step_index + 1]
            if step_index + 1 < len(COMPETITOR_REGISTRATION_STEPS)
            else "awaiting_human_review"
        )
        next_status = "awaiting_human_review" if next_step == "awaiting_human_review" else "processing"
        now = _now()
        payload = {"artifact_refs": list(artifact_refs)}
        if _contains_build_root_path(payload):
            raise StateTransitionError("registration artifacts must not point into the build root")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_competitor_registration_step(
                    step_record_id, registration_id, step_name, artifact_refs_json, integrity_hash,
                    completed_by, completed_at, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_id("competitor_registration_step"), registration_id, step_name, _canonical(payload), _hash(payload), actor, now, self.data_identity),
            )
            self.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step=?, status=? WHERE registration_id=?",
                (next_step, next_status, registration_id),
            )
            result = {"registration_id": registration_id, "completed_step": step_name, "next_step": next_step, "status": next_status}
            self._receipt("record_competitor_registration_step", idempotency_key, request, result)
            self._audit(None, "competitor_registration_step_completed", result)
        return result

    def get_competitor_registration(self, *, registration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT registration.*, account.display_name, account.domain_label, account.external_account_ref "
            "FROM stage0_competitor_registration registration "
            "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
            "WHERE registration.registration_id=? AND registration.data_identity=? AND account.data_identity=?",
            (registration_id, self.data_identity, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("competitor registration does not exist in this data identity")
        return {key: row[key] for key in row.keys()}

    def list_competitor_registration_steps(self, *, registration_id: str) -> list[dict[str, Any]]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT step_name, artifact_refs_json, integrity_hash, completed_by, completed_at "
            "FROM stage0_competitor_registration_step WHERE registration_id=? AND data_identity=? "
            "ORDER BY completed_at, step_record_id",
            (registration["registration_id"], self.data_identity),
        ).fetchall()
        return [
            {
                "step_name": row["step_name"],
                "artifact_refs": _hide_build_root_paths(json.loads(row["artifact_refs_json"]))["artifact_refs"],
                "integrity_hash": row["integrity_hash"],
                "completed_by": row["completed_by"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]

    def repair_completed_registration_breakdown_step(
        self,
        *,
        registration_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Rebuild one missing aggregate breakdown step from completed item records."""
        if not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("legacy registration repair requires an actor and idempotency key")
        registration = self.get_competitor_registration(registration_id=registration_id)
        if registration["status"] != "completed":
            raise StateTransitionError("legacy registration repair only accepts completed registrations")
        steps = {
            str(item["step_name"]): item["artifact_refs"]
            for item in self.list_competitor_registration_steps(registration_id=registration_id)
        }
        if "breakdown" in steps:
            return {
                "registration_id": registration_id,
                "status": "already_present",
                "step_name": "breakdown",
                "artifact_count": len(steps["breakdown"]),
            }
        if any(step not in steps for step in ("historical_material", "high_signal_identification")):
            raise StateTransitionError("legacy registration repair lacks its historical or high-signal step")
        selected_ids = {
            str(item.get("source_id") or "").strip()
            for item in steps["high_signal_identification"][0].get("selected_items", [])
            if isinstance(item, dict) and str(item.get("source_id") or "").strip()
        }
        completed_items = [
            item for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            )
            if item["status"] == "completed" and isinstance(item.get("artifact"), dict)
        ]
        artifacts: list[dict[str, Any]] = []
        artifact_source_ids: set[str] = set()
        for item in completed_items:
            artifact = dict(item["artifact"])
            source_id = str(artifact.get("source_id") or item["item_ref"]).strip()
            if not source_id or source_id != str(item["item_ref"]).strip():
                raise StateTransitionError("legacy breakdown item source identity is inconsistent")
            if artifact.get("artifact_kind") != "deep_breakdown":
                raise StateTransitionError("legacy breakdown item is not a deep-breakdown artifact")
            if source_id in artifact_source_ids:
                raise StateTransitionError("legacy breakdown items contain duplicate source identities")
            artifact_source_ids.add(source_id)
            artifacts.append(artifact)
        material_ids = {
            str(item["item_ref"]).strip()
            for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            )
            if item["status"] == "completed"
        }
        if artifact_source_ids != selected_ids or material_ids != selected_ids:
            raise StateTransitionError(
                "legacy registration repair requires complete selected material and breakdown item coverage"
            )
        payload = {"artifact_refs": artifacts}
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_step("
                "step_record_id, registration_id, step_name, artifact_refs_json, integrity_hash, "
                "completed_by, completed_at, data_identity"
                ") VALUES (?, ?, 'breakdown', ?, ?, ?, ?, ?)",
                (
                    _id("competitor_registration_step_repair"),
                    registration_id,
                    _canonical(payload),
                    _hash(payload),
                    actor.strip(),
                    now,
                    self.data_identity,
                ),
            )
            self._audit(
                None,
                "completed_registration_breakdown_step_repaired",
                {
                    "registration_id": registration_id,
                    "artifact_count": len(artifacts),
                    "actor": actor.strip(),
                },
            )
            self._receipt(
                "repair_completed_registration_breakdown_step",
                idempotency_key,
                {"registration_id": registration_id, "actor": actor.strip()},
                {
                    "registration_id": registration_id,
                    "status": "repaired",
                    "step_name": "breakdown",
                    "artifact_count": len(artifacts),
                },
            )
        return {
            "registration_id": registration_id,
            "status": "repaired",
            "step_name": "breakdown",
            "artifact_count": len(artifacts),
        }

    def list_competitor_registration_items(
        self, *, registration_id: str, step_name: str
    ) -> list[dict[str, Any]]:
        self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name=? AND data_identity=? ORDER BY item_ref",
            (registration_id, step_name, self.data_identity),
        ).fetchall()
        return [
            {
                "item_ref": row["item_ref"],
                "status": row["status"],
                "artifact": _hide_build_root_paths(json.loads(row["artifact_json"])),
                "error": _hide_build_root_paths(json.loads(row["error_json"])),
                "attempt_count": row["attempt_count"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def repair_completed_competitor_material_comments(
        self,
        *,
        registration_id: str,
        item_ref: str,
        comments: list[dict[str, Any]],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Restore comments from the matching saved collection checkpoint.

        This repair changes only the comment portion of an already completed
        material.  It accepts comment records only when their identities are
        present in the same formal collection checkpoint, so a repair cannot
        inject unrelated text into a source material.
        """
        if not registration_id.strip() or not item_ref.strip() or not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("comment repair requires a registration, source, actor and idempotency key")
        if not isinstance(comments, list):
            raise StateTransitionError("comment repair requires a comment list")
        registration = self.get_competitor_registration(registration_id=registration_id)
        if registration["status"] != "completed":
            raise StateTransitionError("comment repair only accepts completed registrations")
        checkpoint = self.conn.execute(
            "SELECT comments_json, collection_ref FROM stage0_competitor_material_collection_checkpoint "
            "WHERE registration_id=? AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if checkpoint is None:
            raise StateTransitionError("comment repair has no matching saved collection checkpoint")
        try:
            collected_comments = json.loads(str(checkpoint["comments_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("saved comment checkpoint is invalid") from exc
        collected_ids = {
            str(value.get("comment_id") or value.get("id") or "").strip()
            for value in collected_comments
            if isinstance(value, dict)
        }

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position, comment in enumerate(comments, start=1):
            if not isinstance(comment, dict):
                continue
            comment_id = str(comment.get("comment_id") or comment.get("id") or "").strip()
            text = str(comment.get("text") or comment.get("content") or "").strip()
            if not comment_id or comment_id not in collected_ids or not text or comment_id in seen:
                continue
            seen.add(comment_id)
            normalized.append({
                "comment_id": comment_id,
                "text": text,
                "like_count": int(comment.get("like_count") or 0),
                "sample_rank": int(comment.get("sample_rank") or position),
            })
        request = {
            "registration_id": registration_id,
            "item_ref": item_ref.strip(),
            "comments": normalized,
            "actor": actor.strip(),
        }
        replay = self._replay("repair_completed_competitor_material_comments", idempotency_key, request)
        if replay:
            return replay
        row = self.conn.execute(
            "SELECT artifact_json, status FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if row is None or str(row["status"]) != "completed":
            raise StateTransitionError("comment repair requires a completed material item")
        try:
            artifact = json.loads(str(row["artifact_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("completed material artifact is invalid") from exc
        if not isinstance(artifact, dict) or artifact.get("artifact_kind") != "transcript_and_comments":
            raise StateTransitionError("comment repair target is not a transcript-and-comments material")
        current_comments = artifact.get("comments")
        if isinstance(current_comments, list) and current_comments:
            result = {
                "registration_id": registration_id,
                "item_ref": item_ref.strip(),
                "status": "already_present",
                "comment_count": len(current_comments),
            }
            with self.conn:
                self._receipt("repair_completed_competitor_material_comments", idempotency_key, request, result)
            return result
        artifact["comments"] = normalized
        if str(checkpoint["collection_ref"] or "").strip():
            artifact["comment_collection_ref"] = str(checkpoint["collection_ref"]).strip()
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET artifact_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
                (
                    _canonical(artifact), now, registration_id, item_ref.strip(), self.data_identity,
                ),
            )
            result = {
                "registration_id": registration_id,
                "item_ref": item_ref.strip(),
                "status": "repaired",
                "comment_count": len(normalized),
            }
            self._receipt("repair_completed_competitor_material_comments", idempotency_key, request, result)
            self._audit(None, "completed_competitor_material_comments_repaired", {
                **result, "actor": actor.strip(), "checkpoint_comment_count": len(collected_ids),
            })
        return result

    def record_competitor_registration_item(
        self,
        *,
        registration_id: str,
        step_name: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        allowed_current_steps = (
            {"transcripts_and_comments", "breakdown"}
            if step_name == "breakdown"
            else {step_name}
        )
        if (
            registration["status"] != "processing"
            or registration["current_step"] not in allowed_current_steps
        ):
            raise StateTransitionError("registration item belongs to a stale or inactive step")
        if step_name not in {"transcripts_and_comments", "breakdown"} or status not in {"completed", "failed"}:
            raise StateTransitionError("registration item has an unsupported step or status")
        if not item_ref.strip() or (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("registration item needs an identity and matching result")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(registration_id, step_name, item_ref) DO UPDATE SET "
                "status=excluded.status, artifact_json=excluded.artifact_json, error_json=excluded.error_json, "
                "attempt_count=stage0_competitor_registration_item.attempt_count+1, updated_at=excluded.updated_at",
                (
                    registration_id, step_name, item_ref.strip(), status, _canonical(artifact or {}),
                    _canonical(error or {}), self.data_identity, _now(),
                ),
            )
        return {"registration_id": registration_id, "step_name": step_name, "item_ref": item_ref, "status": status}

    def record_competitor_breakdown_optional_result(
        self,
        *,
        registration_id: str,
        item_ref: str,
        optional_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach optional expansion status without changing core success."""
        if not str(registration_id or "").strip() or not str(item_ref or "").strip():
            raise StateTransitionError("optional breakdown result needs a registration and item")
        if not isinstance(optional_result, dict):
            raise StateTransitionError("optional breakdown result must be an object")
        row = self.conn.execute(
            "SELECT status, artifact_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if row is None or str(row["status"] or "") != "completed":
            raise StateTransitionError("optional breakdown result requires a completed core breakdown")
        try:
            artifact = json.loads(str(row["artifact_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("completed breakdown artifact is not valid JSON") from exc
        if not isinstance(artifact, dict):
            raise StateTransitionError("completed breakdown artifact must be an object")
        artifact["optional_enhancements"] = dict(optional_result)
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item SET artifact_json=?, updated_at=? "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=?",
                (
                    _canonical(artifact), _now(), registration_id, item_ref.strip(), self.data_identity,
                ),
            )
            self._audit(None, "competitor_breakdown_optional_result_recorded", {
                "registration_id": registration_id,
                "item_ref": item_ref.strip(),
                "status": str(optional_result.get("status") or "unknown"),
            })
        return {
            "registration_id": registration_id,
            "item_ref": item_ref.strip(),
            "status": str(optional_result.get("status") or "unknown"),
        }

    def record_competitor_breakdown_attempt(
        self,
        *,
        registration_id: str,
        source_id: str,
        attempt_kind: str,
        outcome: str,
        reason: str = "",
        raw_model_output: str | None = None,
        raw_model_output_status: str,
        model_run_id: str | None = None,
    ) -> dict[str, str]:
        """Append one immutable delivery record for a source-bound breakdown call."""
        if attempt_kind not in {"initial", "post_batch_delivery_retry"}:
            raise StateTransitionError("breakdown attempt has an unsupported kind")
        if outcome not in {"completed", "delivery_interrupted", "failed"}:
            raise StateTransitionError("breakdown attempt has an unsupported outcome")
        if not source_id.strip() or not raw_model_output_status.strip():
            raise StateTransitionError("breakdown attempt needs a source and output status")
        material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND status='completed' AND data_identity=?",
            (registration_id, source_id.strip(), self.data_identity),
        ).fetchone()
        if material is None:
            raise StateTransitionError("breakdown attempt requires retained spoken material")
        attempt_id = _id("competitor_breakdown_attempt")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_attempt VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id, registration_id, source_id.strip(), attempt_kind, outcome,
                    reason, raw_model_output, raw_model_output_status, model_run_id,
                    self.data_identity, _now(),
                ),
            )
        return {"breakdown_attempt_id": attempt_id, "outcome": outcome}

    def latest_competitor_breakdown_attempt(
        self,
        *,
        registration_id: str,
        source_id: str,
    ) -> dict[str, str] | None:
        row = self.conn.execute(
            "SELECT attempt_kind, outcome FROM stage0_competitor_breakdown_attempt "
            "WHERE registration_id=? AND source_id=? AND data_identity=? "
            "ORDER BY created_at DESC, breakdown_attempt_id DESC LIMIT 1",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if row is None:
            return None
        return {"attempt_kind": str(row["attempt_kind"]), "outcome": str(row["outcome"])}

    def record_replenished_competitor_material_item(
        self,
        *,
        registration_id: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Write only a user-authorized replacement for an incorrectly excluded transcript.

        This deliberately does not reopen registration progression.  It cannot
        trigger breakdown work, and it cannot touch a normal completed or
        failed material item.
        """
        if status not in {"completed", "failed"}:
            raise StateTransitionError("replenished material has an unsupported status")
        if not item_ref.strip() or (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("replenished material needs an identity and matching result")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "excluded":
            raise StateTransitionError("replenished material must replace only an excluded source")
        try:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("excluded material has an invalid exclusion record") from exc
        if prior_error.get("disposition") not in {
            "excluded_by_user_cleanup", "repair_retry_authorized_by_user",
        }:
            raise StateTransitionError("replenished material is not an explicitly authorized recovery source")
        signal = self.conn.execute(
            "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
            "WHERE registration_id=? AND step_name='high_signal_identification' AND data_identity=?",
            (registration_id, self.data_identity),
        ).fetchone()
        if signal is None:
            raise StateTransitionError("replenished material has no original high-signal selection evidence")
        payload = _hide_build_root_paths(json.loads(str(signal["artifact_refs_json"])))
        selected_ids = {
            str(item.get("source_id") or "")
            for artifact in payload.get("artifact_refs", [])
            if isinstance(artifact, dict)
            for item in artifact.get("selected_items", [])
            if isinstance(item, dict)
        }
        if item_ref.strip() not in selected_ids:
            raise StateTransitionError("replenished material is outside the original high-signal selection")
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET status=?, artifact_json=?, error_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=? AND status='excluded'",
                (
                    status, _canonical(artifact or {}), _canonical(error or {}), _now(),
                    registration_id, item_ref.strip(), self.data_identity,
                ),
            )
        return {"registration_id": registration_id, "step_name": "transcripts_and_comments", "item_ref": item_ref, "status": status}

    def record_independent_competitor_breakdown(
        self,
        *,
        registration_id: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
        automatic_delivery_retry: bool = False,
    ) -> dict[str, Any]:
        """Persist one source-bound breakdown after its original registration is closed."""
        if status not in {"completed", "failed"} or not item_ref.strip():
            raise StateTransitionError("independent breakdown needs a valid result and source")
        if (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("independent breakdown needs a matching result payload")
        material = self.conn.execute(
            "SELECT status FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if material is None or str(material["status"]) != "completed":
            raise StateTransitionError("independent breakdown requires a completed spoken material")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        prior_error: dict[str, Any] = {}
        prior_status = str(prior["status"] or "") if prior is not None else ""
        if prior is not None:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
            if prior_status == "completed":
                raise StateTransitionError("independent breakdown will not replace a completed result")
            if prior_status not in {"failed", "excluded"}:
                raise StateTransitionError("independent breakdown has an unsupported prior result")
            if prior_status == "excluded":
                allowed_dispositions = {
                    "retired_by_user", "repair_retry_authorized_by_user",
                }
                if automatic_delivery_retry:
                    allowed_dispositions.add("one_post_batch_delivery_retry_pending")
                if prior_error.get("disposition") not in allowed_dispositions and prior_error.get("retry_disposition") not in allowed_dispositions:
                    raise StateTransitionError("independent breakdown cannot replace a non-retired record")
        persisted_error = dict(error or {})
        if prior_status == "failed" and status == "completed":
            persisted_error = {
                "disposition": "replaced_failed_by_retry",
                "previous_breakdown": prior_error,
                "automatic_retry": bool(automatic_delivery_retry),
            }
        if prior_error.get("disposition") == "retired_by_user":
            persisted_error = {
                "disposition": "replaced_by_user_prompt_rerun",
                "replacement_reason": str(prior_error.get("reason") or ""),
                "previous_breakdown": prior_error,
                "automatic_retry": False,
            }
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item VALUES (?, 'breakdown', ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(registration_id, step_name, item_ref) DO UPDATE SET "
                "status=excluded.status, artifact_json=excluded.artifact_json, error_json=excluded.error_json, "
                "attempt_count=stage0_competitor_registration_item.attempt_count+1, updated_at=excluded.updated_at",
                (registration_id, item_ref.strip(), status, _canonical(artifact or {}), _canonical(persisted_error), self.data_identity, now),
            )
        return {"registration_id": registration_id, "step_name": "breakdown", "item_ref": item_ref, "status": status}

    def record_human_quality_rejected_competitor_breakdown(
        self,
        *,
        registration_id: str,
        item_ref: str,
        reason: str,
        failure_stage: str = "human_quality_acceptance",
        failure_type: str = "HumanQualityRejected",
    ) -> dict[str, Any]:
        """Turn a human-rejected completed breakdown into a retained failure.

        The completed artifact remains byte-for-byte in place.  This is a status
        correction, never a replacement or deletion of the prior formal result.
        """
        if not item_ref.strip() or not reason.strip():
            raise StateTransitionError("quality rejection needs an item identity and reason")
        prior = self.conn.execute(
            "SELECT status, artifact_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "completed":
            raise StateTransitionError("quality rejection requires a currently completed breakdown")
        artifact_json = str(prior["artifact_json"])
        try:
            artifact = json.loads(artifact_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("completed breakdown artifact cannot be preserved") from exc
        raw_output = artifact.get("raw_model_output") if isinstance(artifact, dict) else None
        error = {
            "reason": reason.strip(),
            "failure_stage": failure_stage,
            "failure_type": failure_type,
            "raw_model_output": raw_output if isinstance(raw_output, str) else None,
            "raw_model_output_status": "available" if isinstance(raw_output, str) else "not_available_legacy_completed_record",
            "automatic_retry": False,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET status='failed', artifact_json=?, error_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=? AND status='completed'",
                (
                    artifact_json,
                    _canonical(error),
                    _now(),
                    registration_id,
                    item_ref.strip(),
                    self.data_identity,
                ),
            )
        return {"registration_id": registration_id, "step_name": "breakdown", "item_ref": item_ref, "status": "failed"}

    def record_human_excluded_competitor_breakdown(
        self,
        *,
        registration_id: str,
        item_ref: str,
        actor: str,
        reason: str,
        prompt_rerun: bool = False,
    ) -> dict[str, Any]:
        """Retire one breakdown by an explicit user decision.

        A completed result can be retired for an explicit prompt rerun.  Its
        prior artifact is retained in the exclusion record and is carried into
        the replacement audit when the new formal result is written.
        """
        if not item_ref.strip() or not actor.strip() or not reason.strip():
            raise StateTransitionError("breakdown exclusion needs an item, the user and a reason")
        prior = self.conn.execute(
            "SELECT status, artifact_json, error_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) not in {"failed", "completed"}:
            raise StateTransitionError("breakdown exclusion requires a current failed or completed breakdown")
        try:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("failed breakdown record cannot be preserved") from exc
        now = _now()
        if str(prior["status"]) == "completed":
            try:
                prior_artifact = json.loads(str(prior["artifact_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateTransitionError("completed breakdown artifact cannot be preserved") from exc
            exclusion_error = {
                "reason": reason.strip(),
                "failure_stage": "manual_prompt_rerun",
                "failure_type": "UserRequestedPromptRerun",
                "disposition": "retired_by_user",
                "exclusion_scope": "manual_prompt_rerun",
                "excluded_by": actor.strip(),
                "excluded_at": now,
                "retry_allowed": True,
                "automatic_retry": False,
                "prior_artifact": prior_artifact,
                "prior_error": prior_error,
            }
        else:
            if prompt_rerun:
                try:
                    prior_artifact = json.loads(str(prior["artifact_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise StateTransitionError("previous breakdown artifact cannot be preserved") from exc
                exclusion_error = {
                    **prior_error,
                    "reason": reason.strip(),
                    "failure_stage": "manual_prompt_rerun",
                    "failure_type": "UserRequestedPromptRerun",
                    "disposition": "retired_by_user",
                    "exclusion_scope": "manual_prompt_rerun",
                    "excluded_by": actor.strip(),
                    "excluded_at": now,
                    "retry_allowed": True,
                    "automatic_retry": False,
                    "prior_artifact": prior_artifact,
                    "prior_error": prior_error,
                }
            else:
                exclusion_error = {
                    **prior_error,
                    "original_failure": prior_error,
                    "reason": reason.strip(),
                    "disposition": "excluded_by_user",
                    "exclusion_scope": "current_cold_start_review",
                    "exclusion_reason": reason.strip(),
                    "excluded_by": actor.strip(),
                    "excluded_at": now,
                    "retry_allowed": False,
                    "automatic_retry": False,
                }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET status='excluded', error_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? "
                "AND data_identity=? AND status IN ('failed', 'completed')",
                (
                    _canonical(exclusion_error),
                    now,
                    registration_id,
                    item_ref.strip(),
                    self.data_identity,
                ),
            )
        return {
            "registration_id": registration_id,
            "step_name": "breakdown",
            "item_ref": item_ref,
            "status": "excluded",
            "disposition": str(exclusion_error["disposition"]),
        }

    def record_replenished_competitor_material_collection_checkpoint(
        self,
        *,
        registration_id: str,
        item_ref: str,
        detail: dict[str, Any],
        comments: list[dict[str, Any]],
        collection_ref: str,
    ) -> dict[str, Any]:
        """Keep a returned platform detail before download or transcription begins.

        This is intentionally limited to the user-authorized replacement set.
        It makes a stopped repair resumable without turning the registration
        back on or scheduling any analysis.
        """
        source_id = item_ref.strip()
        if not source_id or str(detail.get("source_id") or "").strip() != source_id:
            raise StateTransitionError("collected detail must match its selected source")
        if not collection_ref.strip():
            raise StateTransitionError("collected detail needs its raw collection reference")
        if not isinstance(comments, list):
            raise StateTransitionError("collected comments must be a list")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "excluded":
            raise StateTransitionError("collected detail must belong to an excluded recovery source")
        try:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("excluded material has an invalid exclusion record") from exc
        if prior_error.get("disposition") not in {
            "excluded_by_user_cleanup", "repair_retry_authorized_by_user",
        }:
            raise StateTransitionError("collected detail is not an explicitly authorized recovery source")
        signal = self.conn.execute(
            "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
            "WHERE registration_id=? AND step_name='high_signal_identification' AND data_identity=?",
            (registration_id, self.data_identity),
        ).fetchone()
        if signal is None:
            raise StateTransitionError("collected detail has no original high-signal selection evidence")
        payload = _hide_build_root_paths(json.loads(str(signal["artifact_refs_json"])))
        selected_ids = {
            str(item.get("source_id") or "")
            for artifact in payload.get("artifact_refs", [])
            if isinstance(artifact, dict)
            for item in artifact.get("selected_items", [])
            if isinstance(item, dict)
        }
        if source_id not in selected_ids:
            raise StateTransitionError("collected detail is outside the original high-signal selection")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_material_collection_checkpoint VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(registration_id, item_ref) DO UPDATE SET "
                "detail_json=excluded.detail_json, comments_json=excluded.comments_json, "
                "collection_ref=excluded.collection_ref, recorded_at=excluded.recorded_at",
                (
                    registration_id, source_id, _canonical(detail), _canonical(comments),
                    collection_ref.strip(), self.data_identity, _now(),
                ),
            )
        return {"registration_id": registration_id, "item_ref": source_id, "status": "collected"}

    def list_replenished_competitor_material_collection_checkpoints(
        self, *, registration_id: str
    ) -> list[dict[str, Any]]:
        self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT * FROM stage0_competitor_material_collection_checkpoint "
            "WHERE registration_id=? AND data_identity=? ORDER BY item_ref",
            (registration_id, self.data_identity),
        ).fetchall()
        return [
            {
                "item_ref": str(row["item_ref"]),
                "detail": json.loads(str(row["detail_json"])),
                "comments": json.loads(str(row["comments_json"])),
                "collection_ref": str(row["collection_ref"]),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in rows
        ]

    def record_competitor_registration_tag_candidates(
        self,
        *,
        registration_id: str,
        candidates: tuple[dict[str, Any], ...],
        actor: str,
    ) -> tuple[dict[str, Any], ...]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        if registration["status"] != "processing" or registration["current_step"] != "tag_candidates":
            raise StateTransitionError("registration tag candidates belong to a stale or inactive step")
        policy = get_discovery_policy(str(registration["domain_label"]))
        topic_policy = policy.get("topic_search") if isinstance(policy.get("topic_search"), dict) else {}
        generic_tags = {str(value).strip().casefold() for value in topic_policy.get("generic_tags", [])}
        activity_terms = tuple(str(value).strip() for value in topic_policy.get("activity_review_terms", []))
        retained: list[dict[str, Any]] = []
        now = _now()
        for candidate in candidates:
            tag = str(candidate.get("tag") or "").strip().lstrip("#")
            source_ids = candidate.get("source_ids")
            if not tag or tag.casefold() in generic_tags or not isinstance(source_ids, list) or not source_ids:
                continue
            active_activity = self.conn.execute(
                "SELECT 1 FROM domain_search_activity_tag_registry WHERE domain_label=? AND tag=? "
                "AND status='active' AND valid_from<=? AND valid_until>=? LIMIT 1",
                (registration["domain_label"], tag, now, now),
            ).fetchone()
            if active_activity is not None:
                continue
            status = "pending_review" if any(term and term in tag for term in activity_terms) else "suggested"
            tag_id = f"registration_tag_{_hash({'domain': registration['domain_label'], 'tag': tag})[:20]}"
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, human_review_status"
                    ") VALUES (?, ?, ?, ?, 'discovered', ?, 'pending_review')",
                    (tag_id, tag, registration["domain_label"], status, str(source_ids[0])),
                )
            retained.append({
                "tag_id": tag_id,
                "tag": tag,
                "status": status,
                "source_ids": [str(value) for value in source_ids],
                "reason": str(candidate.get("reason") or "").strip(),
                "human_confirmation_required": True,
                "recorded_by": actor,
            })
        return tuple(retained)

    def list_competitor_registration_tag_candidates(self, *, registration_id: str) -> list[dict[str, Any]]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        step = next(
            (
                item for item in self.list_competitor_registration_steps(registration_id=registration_id)
                if item["step_name"] == "tag_candidates"
            ),
            None,
        )
        candidate_ids: list[str] = []
        if step is not None:
            for artifact in step["artifact_refs"]:
                if not isinstance(artifact, dict):
                    continue
                for candidate in artifact.get("candidates", []):
                    if isinstance(candidate, dict) and str(candidate.get("tag_id") or "").strip():
                        candidate_ids.append(str(candidate["tag_id"]))
        if not candidate_ids:
            return []
        rows = self.conn.execute(
            "SELECT tag_id, tag, status, human_review_status, source_video_id, reviewed_at, reviewed_note "
            "FROM domain_search_tags WHERE domain_label=? AND tag_id IN ("
            + ",".join("?" for _ in candidate_ids)
            + ") ORDER BY created_at, tag_id",
            (registration["domain_label"], *candidate_ids),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]


    def build_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if existing is not None:
            return self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        cold_start = self.conn.execute(
            "SELECT * FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("cold start does not exist")
        registrations = self.conn.execute(
            "SELECT registration.registration_id, registration.competitor_account_id "
            "FROM stage0_competitor_registration registration "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "ORDER BY registration.registration_id",
            (cold_start_id, self.data_identity),
        ).fetchall()
        if not registrations:
            raise StateTransitionError("the whole tag library requires registered competitor accounts")
        missing_high_signal = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration registration "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM stage0_competitor_registration_step step "
            "WHERE step.registration_id=registration.registration_id "
            "AND step.data_identity=registration.data_identity "
            "AND step.step_name='high_signal_identification'"
            ") LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if missing_high_signal is not None:
            raise StateTransitionError(
                "the whole tag library can be built only after every account completes baseline and hit filtering"
            )

        raw_candidates: dict[str, dict[str, Any]] = {}
        source_item_ids: set[str] = set()
        source_author_names: set[str] = set()
        for registration in registrations:
            effective_steps = {
                item["step_name"]: item["artifact_refs"]
                for item in self.list_competitor_registration_steps(
                    registration_id=str(registration["registration_id"])
                )
            }
            artifacts = effective_steps.get("high_signal_identification")
            if artifacts is None:
                raise StateTransitionError("a completed registration lacks its high-signal source artifact")
            for artifact in artifacts if isinstance(artifacts, list) else []:
                if not isinstance(artifact, dict):
                    continue
                for item in artifact.get("selected_items", []):
                    if not isinstance(item, dict):
                        continue
                    source_id = str(item.get("source_id") or "").strip()
                    if not source_id:
                        continue
                    source_item_ids.add(source_id)
                    author = str(item.get("author") or "").strip().casefold()
                    if author:
                        source_author_names.add(author)
                    for raw_tag in _SOURCE_HASHTAG_PATTERN.findall(str(item.get("title") or "")):
                        tag = raw_tag.strip().strip(_TAG_EDGE_PUNCTUATION)
                        if not tag:
                            continue
                        key = tag.casefold()
                        candidate = raw_candidates.setdefault(
                            key,
                            {
                                "tag": tag,
                                "source_ids": [],
                                "registration_ids": [],
                            },
                        )
                        if source_id not in candidate["source_ids"]:
                            candidate["source_ids"].append(source_id)
                        registration_id = str(registration["registration_id"])
                        if registration_id not in candidate["registration_ids"]:
                            candidate["registration_ids"].append(registration_id)

        policy = get_discovery_policy(str(cold_start["domain_label"]))
        topic_policy = policy.get("topic_search") if isinstance(policy.get("topic_search"), dict) else {}
        topic_policy = {
            **topic_policy,
            "named_entity_terms": sorted({
                *(
                    str(value).strip()
                    for value in topic_policy.get("named_entity_terms", [])
                    if str(value).strip()
                ),
                *source_author_names,
            }),
        }
        generic = {
            str(value).strip().casefold()
            for value in (
                list(topic_policy.get("generic_tags", []))
                + list(topic_policy.get("broad_domain_tags", []))
            )
            if str(value).strip()
        }
        platform_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("platform_generic_terms", [])
            if str(value).strip()
        )
        activity_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("activity_review_terms", [])
            if str(value).strip()
        )
        support_floor = max(2, int(topic_policy.get("minimum_source_support_floor") or 2))
        support_ratio = max(0.0, float(topic_policy.get("minimum_source_support_ratio") or 0.0))
        minimum_support = max(support_floor, math.ceil(len(source_item_ids) * support_ratio))
        existing_rows = self.conn.execute(
            "SELECT tag FROM domain_search_tags WHERE domain_label=?",
            (cold_start["domain_label"],),
        ).fetchall()
        existing_tags = {str(row["tag"]).strip().casefold() for row in existing_rows}

        retained: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for key, candidate in raw_candidates.items():
            source_count = len(candidate["source_ids"])
            reason = ""
            named_entity_reason = _deterministic_named_entity_filter_reason(
                str(candidate["tag"]),
                topic_policy=topic_policy,
            )
            if key in generic:
                reason = "平台通用或领域过宽"
            elif any(term in candidate["tag"] for term in platform_terms):
                reason = "平台套话"
            elif any(term in candidate["tag"] for term in activity_terms):
                reason = "限时活动"
            elif named_entity_reason:
                reason = named_entity_reason
            elif len(candidate["tag"]) > 24 or candidate["tag"].isdigit():
                reason = "不是稳定可复用的话题标签"
            elif source_count < minimum_support:
                reason = f"仅有{source_count}条高信号作品使用，尚不足以进入可复用话题库"
            elif key in existing_tags:
                reason = "已存在于领域标签库"
            enriched = {
                **candidate,
                "source_count": source_count,
                "account_count": len(candidate["registration_ids"]),
            }
            if reason:
                enriched["filter_reason"] = reason
                filtered.append(enriched)
            else:
                enriched["retention_reason"] = (
                    f"来自{source_count}条历史高信号作品，达到本轮重复证据门槛{minimum_support}条"
                )
                retained.append(enriched)
        retained.sort(
            key=lambda item: (
                -int(item["source_count"]),
                -int(item["account_count"]),
                str(item["tag"]).casefold(),
            )
        )
        filtered.sort(
            key=lambda item: (
                str(item["filter_reason"]),
                -int(item["source_count"]),
                str(item["tag"]).casefold(),
            )
        )

        tag_ids: list[str] = []
        now = _now()
        library_id = _id("cold_start_tag_library")
        with self.conn:
            for candidate in retained:
                tag_id = f"cold_start_tag_{_hash({'domain': cold_start['domain_label'], 'tag': str(candidate['tag']).casefold()})[:20]}"
                self.conn.execute(
                    "INSERT INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, human_review_status"
                    ") VALUES (?, ?, ?, 'suggested', 'discovered', ?, 'pending_review')",
                    (
                        tag_id,
                        candidate["tag"],
                        cold_start["domain_label"],
                        candidate["source_ids"][0],
                    ),
                )
                candidate["tag_id"] = tag_id
                tag_ids.append(tag_id)
            candidate_set = {
                "rules": {
                    "source": "historical_high_signal_source_hashtags_only",
                    "generic_and_broad_tags": sorted(generic),
                    "platform_generic_terms": list(platform_terms),
                    "limited_activity_terms": list(activity_terms),
                    "named_entity_filter": (
                        "configured named entities plus retained source author names"
                    ),
                    "minimum_source_support": minimum_support,
                },
                "retained": retained,
                "filtered": filtered,
            }
            self.conn.execute(
                "INSERT INTO stage0_cold_start_tag_library VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "'awaiting_human_review', ?, ?, ?, NULL, NULL, NULL)",
                (
                    library_id,
                    cold_start_id,
                    cold_start["domain_label"],
                    "source_hashtag_deterministic_v3",
                    len(source_item_ids),
                    minimum_support,
                    _canonical(candidate_set),
                    _canonical(tag_ids),
                    self.data_identity,
                    "",
                    now,
                ),
            )
            self._audit(
                None,
                "cold_start_tag_library_built",
                {
                    "tag_library_id": library_id,
                    "cold_start_id": cold_start_id,
                    "source_item_count": len(source_item_ids),
                    "raw_unique_count": len(raw_candidates),
                    "retained_count": len(retained),
                    "filtered_count": len(filtered),
                    "minimum_source_support": minimum_support,
                    "model_used": False,
                },
            )
        return self.get_cold_start_tag_library(cold_start_id=cold_start_id)


    def get_cold_start_tag_library(self, *, cold_start_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if row is None:
            return None
        candidate_set = json.loads(row["candidate_set_json"])
        candidates_by_id = {
            str(item.get("tag_id")): item
            for item in candidate_set.get("retained", [])
            if isinstance(item, dict) and item.get("tag_id")
        }
        tag_ids = json.loads(row["tag_ids_json"])
        tag_rows: dict[str, sqlite3.Row] = {}
        if tag_ids:
            placeholders = ",".join("?" for _ in tag_ids)
            rows = self.conn.execute(
                "SELECT tag_id, tag, status, human_review_status, source_video_id, reviewed_at, reviewed_note "
                f"FROM domain_search_tags WHERE tag_id IN ({placeholders}) ORDER BY created_at, tag_id",
                tuple(tag_ids),
            ).fetchall()
            tag_rows = {str(item["tag_id"]): item for item in rows}
        candidates: list[dict[str, Any]] = []
        for tag_id in tag_ids:
            tag_row = tag_rows.get(str(tag_id))
            evidence = candidates_by_id.get(str(tag_id), {})
            if tag_row is None:
                continue
            candidates.append({
                **{key: tag_row[key] for key in tag_row.keys()},
                "source_ids": list(evidence.get("source_ids", [])),
                "registration_ids": list(evidence.get("registration_ids", [])),
                "source_count": int(evidence.get("source_count") or 0),
                "account_count": int(evidence.get("account_count") or 0),
                "retention_reason": str(evidence.get("retention_reason") or ""),
            })
        return {
            "tag_library_id": row["tag_library_id"],
            "cold_start_id": cold_start_id,
            "domain_label": row["domain_label"],
            "status": row["status"],
            "extraction_method": row["extraction_method"],
            "source_item_count": int(row["source_item_count"]),
            "minimum_source_support": int(row["minimum_source_support"]),
            "raw_unique_count": len(candidate_set.get("retained", [])) + len(candidate_set.get("filtered", [])),
            "retained_count": len(candidate_set.get("retained", [])),
            "filtered_count": len(candidate_set.get("filtered", [])),
            "candidates": candidates,
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_reason": row["review_reason"],
        }

    def _hard_delete_domain_tag(self, *, tag_id: str) -> None:
        """Permanently remove one tag and every tag-owned search record."""
        raise StateTransitionError(
            "hard deletion of retained business tags and search records is retired"
        )
        libraries = self.conn.execute(
            "SELECT cold_start_id, candidate_set_json, tag_ids_json "
            "FROM stage0_cold_start_tag_library WHERE data_identity=?",
            (self.data_identity,),
        ).fetchall()
        for library in libraries:
            original_ids = [str(value) for value in json.loads(library["tag_ids_json"])]
            if tag_id not in original_ids:
                continue
            candidate_set = json.loads(library["candidate_set_json"])
            for key in ("retained", "filtered"):
                candidate_set[key] = [
                    item
                    for item in candidate_set.get(key, [])
                    if not isinstance(item, dict)
                    or str(item.get("tag_id") or "") != tag_id
                ]
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library SET candidate_set_json=?, tag_ids_json=? "
                "WHERE cold_start_id=? AND data_identity=?",
                (
                    _canonical(candidate_set),
                    _canonical([value for value in original_ids if value != tag_id]),
                    library["cold_start_id"],
                    self.data_identity,
                ),
            )
        self.conn.execute(
            "DELETE FROM discovered_external_videos WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_page_observation WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_cursor WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_tags WHERE tag_id=?",
            (tag_id,),
        )

    def _remove_cold_start_tag_records(self, *, cold_start_id: str, tag_id: str) -> None:
        """Remove one tag owned by the current cold-start library.

        The generic domain-tag deletion path is intentionally retired for
        already-established search assets.  A pending or explicitly revised
        cold-start library still needs its existing one-review delete/edit
        semantics, so remove only records proven to belong to this library.
        """
        row = self.conn.execute(
            "SELECT candidate_set_json, tag_ids_json FROM stage0_cold_start_tag_library "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("the tag library does not exist")
        original_ids = [str(value) for value in json.loads(row["tag_ids_json"])]
        if tag_id not in set(original_ids):
            raise StateTransitionError("the tag does not belong to this cold-start library")
        candidate_set = json.loads(row["candidate_set_json"])
        for key in ("retained", "filtered"):
            candidate_set[key] = [
                item
                for item in candidate_set.get(key, [])
                if not isinstance(item, dict) or str(item.get("tag_id") or "") != tag_id
            ]
        self.conn.execute(
            "UPDATE stage0_cold_start_tag_library SET candidate_set_json=?, tag_ids_json=? "
            "WHERE cold_start_id=? AND data_identity=?",
            (
                _canonical(candidate_set),
                _canonical([value for value in original_ids if value != tag_id]),
                cold_start_id,
                self.data_identity,
            ),
        )
        self.conn.execute("DELETE FROM discovered_external_videos WHERE tag_id=?", (tag_id,))
        self.conn.execute("DELETE FROM domain_search_page_observation WHERE tag_id=?", (tag_id,))
        self.conn.execute("DELETE FROM domain_search_cursor WHERE tag_id=?", (tag_id,))
        self.conn.execute("DELETE FROM domain_search_tags WHERE tag_id=?", (tag_id,))

    def _hard_delete_cold_start_tag(self, *, cold_start_id: str, tag_id: str) -> None:
        """Compatibility name for the current cold-start review paths."""
        self._remove_cold_start_tag_records(cold_start_id=cold_start_id, tag_id=tag_id)

    def delete_pending_cold_start_tag(
        self,
        *,
        cold_start_id: str,
        tag_id: str,
        actor_kind: str,
        reason: str,
        actor: str = "",
    ) -> dict[str, Any]:
        """Permanently remove one pending tag when the user clicks delete."""
        if actor_kind != "user" or not reason.strip():
            raise StateTransitionError("tag deletion requires an explicit user decision")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "awaiting_human_review":
            raise StateTransitionError("the tag library is not awaiting review")
        candidate = next(
            (
                item
                for item in library["candidates"]
                if str(item["tag_id"]) == tag_id
            ),
            None,
        )
        if candidate is None or candidate["human_review_status"] != "pending_review":
            raise StateTransitionError("the pending tag no longer exists")
        now = _now()
        with self.conn:
            self._hard_delete_cold_start_tag(cold_start_id=cold_start_id, tag_id=tag_id)
            result = {
                "cold_start_id": cold_start_id,
                "tag_id": tag_id,
                "tag": candidate["tag"],
                "status": "deleted",
                "deleted_at": now,
                "deleted_by": None,
            }
            self._audit(None, "cold_start_tag_deleted_by_user", result)
        return result

    def revise_accepted_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        deleted_tag_ids: tuple[str, ...],
        actor_kind: str,
        reason: str,
        actor: str = "",
    ) -> dict[str, Any]:
        """Apply one confirmed batch of deletions to an accepted tag library."""
        if actor_kind != "user" or not reason.strip():
            raise StateTransitionError("accepted tag-library revision requires an explicit user decision")
        normalized_ids = tuple(dict.fromkeys(
            str(tag_id).strip() for tag_id in deleted_tag_ids if str(tag_id).strip()
        ))
        if not normalized_ids:
            raise StateTransitionError("accepted tag-library revision requires at least one deletion")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "accepted":
            raise StateTransitionError("the tag library has not completed its first review")
        approved = {
            str(item["tag_id"]): item
            for item in library["candidates"]
            if item["status"] == "active" and item["human_review_status"] == "approved"
        }
        unknown_ids = set(normalized_ids) - set(approved)
        if unknown_ids:
            raise StateTransitionError("one or more selected tags are no longer active in this library")
        if len(approved) <= len(normalized_ids):
            raise StateTransitionError("an accepted tag library must keep at least one active tag")
        now = _now()
        deleted_items = [
            {"tag_id": tag_id, "tag": str(approved[tag_id]["tag"])}
            for tag_id in normalized_ids
        ]
        with self.conn:
            for tag_id in normalized_ids:
                self._hard_delete_cold_start_tag(cold_start_id=cold_start_id, tag_id=tag_id)
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library "
                "SET reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE cold_start_id=? AND data_identity=? AND status='accepted'",
                (None, now, reason, cold_start_id, self.data_identity),
            )
            result = {
                "tag_library_id": library["tag_library_id"],
                "cold_start_id": cold_start_id,
                "status": "accepted",
                "deleted_count": len(deleted_items),
                "remaining_count": len(approved) - len(deleted_items),
                "deleted_items": deleted_items,
                "reviewed_by": None,
                "reviewed_at": now,
            }
            self._audit(None, "accepted_cold_start_tag_library_revised_by_user", result)
        return result

    def review_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        decisions: tuple[dict[str, str], ...],
        actor_kind: str,
        reason: str,
        actor: str = "",
    ) -> dict[str, Any]:
        if actor_kind != "user" or not reason.strip():
            raise StateTransitionError("whole tag-library review requires an explicit user decision")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "awaiting_human_review":
            raise StateTransitionError("the whole tag library is not awaiting review")
        pending = {
            str(item["tag_id"]): item
            for item in library["candidates"]
            if item["human_review_status"] == "pending_review"
        }
        submitted: dict[str, dict[str, str]] = {}
        accepted_names: set[str] = set()
        for item in decisions:
            tag_id = str(item.get("tag_id") or "").strip()
            decision = str(item.get("decision") or "").strip()
            edited_tag = str(item.get("edited_tag") or "").strip().lstrip("#")
            if not tag_id or decision not in {"accepted", "rejected"}:
                raise StateTransitionError("every whole-library item must be kept or removed")
            if decision == "accepted" and not edited_tag:
                raise StateTransitionError("a kept tag cannot be empty")
            normalized = edited_tag.casefold()
            if decision == "accepted" and normalized in accepted_names:
                raise StateTransitionError("the reviewed tag library contains duplicate tags")
            if decision == "accepted":
                accepted_names.add(normalized)
            submitted[tag_id] = {
                "decision": decision,
                "edited_tag": edited_tag,
            }
        if set(submitted) != set(pending):
            raise StateTransitionError("the whole pending tag library must be reviewed in one submission")
        library_tag_ids = set(pending)
        if library_tag_ids:
            placeholders = ",".join("?" for _ in library_tag_ids)
            other_rows = self.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=? "
                f"AND tag_id NOT IN ({placeholders})",
                (library["domain_label"], *sorted(library_tag_ids)),
            ).fetchall()
        else:
            other_rows = self.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=?",
                (library["domain_label"],),
            ).fetchall()
        outside_names = {str(row["tag"]).strip().casefold() for row in other_rows}
        if accepted_names & outside_names:
            raise StateTransitionError("the reviewed tag library duplicates an existing domain tag")

        accepted = 0
        rejected = 0
        with self.conn:
            for tag_id in sorted(library_tag_ids):
                item = submitted[tag_id]
                original = pending[tag_id]
                if item["decision"] == "accepted":
                    accepted += 1
                    self.conn.execute(
                        "UPDATE domain_search_tags SET tag=?, status='active', human_review_status='approved', "
                        "reviewed_at=?, reviewed_note=? WHERE tag_id=? AND human_review_status='pending_review'",
                        (item["edited_tag"] or original["tag"], _now(), reason, tag_id),
                    )
                else:
                    rejected += 1
                    self._hard_delete_cold_start_tag(
                        cold_start_id=cold_start_id,
                        tag_id=tag_id,
                    )
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library SET status='accepted', reviewed_by=?, reviewed_at=?, "
                "review_reason=? WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_review'",
                (None, _now(), reason, cold_start_id, self.data_identity),
            )
            result = {
                "tag_library_id": library["tag_library_id"],
                "cold_start_id": cold_start_id,
                "status": "accepted",
                "reviewed_count": len(pending),
                "accepted_count": accepted,
                "rejected_count": rejected,
            }
            self._audit(None, "cold_start_tag_library_reviewed_as_a_whole", result)
        result["cold_start_completion"] = self.try_complete_cold_start(
            cold_start_id=cold_start_id,
            trigger="tag_library_review",
            actor="system",
        )
        return result

    def build_two_week_tag_library_review(
        self,
        *,
        domain_label: str,
        window_start: str,
        window_end: str,
        actor: str,
    ) -> dict[str, Any]:
        """Build one deterministic whole-library review without a model call."""
        if (
            domain_label not in formal_domain_labels()
            or not window_start.strip()
            or not window_end.strip()
            or not actor.strip()
        ):
            raise StateTransitionError(
                "two-week whole-library review requires a formal domain, window and actor"
            )
        existing = self.conn.execute(
            "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
            "WHERE domain_label=? AND window_start=? AND window_end=? AND data_identity=?",
            (domain_label, window_start, window_end, self.data_identity),
        ).fetchone()
        if existing is not None:
            return self.get_two_week_tag_library_review(
                tag_review_id=str(existing["tag_review_id"])
            )
        completed = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start_configuration "
            "WHERE domain_label=? AND status='completed' AND data_identity=? LIMIT 1",
            (domain_label, self.data_identity),
        ).fetchone()
        if completed is None:
            raise StateTransitionError(
                "two-week tag-library review requires a completed cold start"
            )
        hit_rows = self.conn.execute(
            "SELECT hit.hit_id, hit.platform_item_id, hit.title, account.account_name "
            "FROM hits hit JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "WHERE account.domain_label=? AND hit.promoted_at>=? AND hit.promoted_at<? "
            "ORDER BY hit.promoted_at, hit.hit_id",
            (self.data_identity, domain_label, window_start, window_end),
        ).fetchall()
        raw_candidates: dict[str, dict[str, Any]] = {}
        author_names = {
            str(row["account_name"] or "").strip().casefold()
            for row in hit_rows
            if str(row["account_name"] or "").strip()
        }
        for row in hit_rows:
            for raw_tag in _SOURCE_HASHTAG_PATTERN.findall(str(row["title"] or "")):
                tag = raw_tag.strip().strip(_TAG_EDGE_PUNCTUATION)
                if not tag:
                    continue
                candidate = raw_candidates.setdefault(
                    tag.casefold(),
                    {"tag": tag, "source_ids": [], "hit_ids": []},
                )
                source_id = str(row["platform_item_id"])
                hit_id = str(row["hit_id"])
                if source_id not in candidate["source_ids"]:
                    candidate["source_ids"].append(source_id)
                if hit_id not in candidate["hit_ids"]:
                    candidate["hit_ids"].append(hit_id)

        policy = get_discovery_policy(domain_label)
        topic_policy = (
            policy.get("topic_search")
            if isinstance(policy.get("topic_search"), dict)
            else {}
        )
        topic_policy = {
            **topic_policy,
            "named_entity_terms": sorted(
                {
                    *(
                        str(value).strip()
                        for value in topic_policy.get("named_entity_terms", [])
                        if str(value).strip()
                    ),
                    *author_names,
                }
            ),
        }
        generic = {
            str(value).strip().casefold()
            for value in (
                list(topic_policy.get("generic_tags", []))
                + list(topic_policy.get("broad_domain_tags", []))
            )
            if str(value).strip()
        }
        platform_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("platform_generic_terms", [])
            if str(value).strip()
        )
        activity_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("activity_review_terms", [])
            if str(value).strip()
        )
        support_floor = max(
            2, int(topic_policy.get("minimum_source_support_floor") or 2)
        )
        support_ratio = max(
            0.0, float(topic_policy.get("minimum_source_support_ratio") or 0.0)
        )
        minimum_support = max(
            support_floor, math.ceil(len(hit_rows) * support_ratio)
        )
        current_rows = self.conn.execute(
            "SELECT tag.tag_id, tag.tag, tag.status, tag.created_at, "
            "MAX(observation.observed_at) AS last_searched_at, "
            "COUNT(DISTINCT observation.run_id) AS search_count, "
            "COUNT(observation.observation_id) AS result_count, "
            "SUM(CASE WHEN observation.filter_outcome='eligible' THEN 1 ELSE 0 END) AS eligible_count "
            "FROM domain_search_tags tag "
            "LEFT JOIN domain_search_page_observation observation ON observation.tag_id=tag.tag_id "
            "WHERE tag.domain_label=? AND tag.status='active' "
            "AND tag.human_review_status='approved' "
            "GROUP BY tag.tag_id, tag.tag, tag.status, tag.created_at "
            "ORDER BY tag.created_at, tag.tag_id",
            (domain_label,),
        ).fetchall()
        current = [
            {
                "tag_id": str(row["tag_id"]),
                "tag": str(row["tag"]),
                "origin": "current_library",
                "created_at": row["created_at"],
                "last_searched_at": row["last_searched_at"],
                "search_count": int(row["search_count"] or 0),
                "result_count": int(row["result_count"] or 0),
                "eligible_result_count": int(row["eligible_count"] or 0),
            }
            for row in current_rows
        ]
        existing_names = {item["tag"].casefold() for item in current}
        retained: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for key, candidate in raw_candidates.items():
            source_count = len(candidate["source_ids"])
            named_entity_reason = _deterministic_named_entity_filter_reason(
                str(candidate["tag"]), topic_policy=topic_policy
            )
            reason = ""
            if key in existing_names:
                reason = "已存在于领域标签库"
            elif key in generic:
                reason = "平台通用或领域过宽"
            elif any(term in candidate["tag"] for term in platform_terms):
                reason = "平台套话"
            elif any(term in candidate["tag"] for term in activity_terms):
                reason = "限时活动"
            elif named_entity_reason:
                reason = named_entity_reason
            elif len(candidate["tag"]) > 24 or candidate["tag"].isdigit():
                reason = "不是稳定可复用的话题标签"
            elif source_count < minimum_support:
                reason = (
                    f"仅有{source_count}条新爆款使用，"
                    f"尚不足本轮{minimum_support}条重复证据门槛"
                )
            enriched = {**candidate, "source_count": source_count}
            if reason:
                enriched["filter_reason"] = reason
                filtered.append(enriched)
            else:
                retained.append(enriched)
        retained.sort(key=lambda item: (-int(item["source_count"]), item["tag"].casefold()))
        filtered.sort(
            key=lambda item: (
                str(item["filter_reason"]),
                -int(item["source_count"]),
                item["tag"].casefold(),
            )
        )
        review_id = _id("two_week_tag_review")
        now = _now()
        suggested: list[dict[str, Any]] = []
        with self.conn:
            for candidate in retained:
                tag_id = (
                    "two_week_tag_"
                    + _hash(
                        {
                            "domain": domain_label,
                            "window_start": window_start,
                            "tag": str(candidate["tag"]).casefold(),
                        }
                    )[:20]
                )
                self.conn.execute(
                    "INSERT INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, "
                    "human_review_status) VALUES (?, ?, ?, 'suggested', 'discovered', ?, 'pending_review')",
                    (
                        tag_id,
                        candidate["tag"],
                        domain_label,
                        candidate["source_ids"][0],
                    ),
                )
                suggested.append(
                    {**candidate, "tag_id": tag_id, "origin": "new_hit_hashtag"}
                )
            tag_ids = [
                *[item["tag_id"] for item in current],
                *[item["tag_id"] for item in suggested],
            ]
            candidate_set = {
                "rules": {
                    "model_calls": 0,
                    "source": "new_hit_source_hashtags_only",
                    "minimum_source_support": minimum_support,
                    "whole_library_review": True,
                    "automatic_pause_or_delete": False,
                },
                "current": current,
                "suggested": suggested,
                "filtered": filtered,
            }
            self.conn.execute(
                "INSERT INTO stage0_two_week_tag_library_review VALUES "
                "(?, ?, ?, ?, ?, ?, 'awaiting_human_review', ?, ?, ?, NULL, NULL, NULL)",
                (
                    review_id,
                    domain_label,
                    window_start,
                    window_end,
                    _canonical(candidate_set),
                    _canonical(tag_ids),
                    self.data_identity,
                    actor.strip(),
                    now,
                ),
            )
            self._audit(
                None,
                "two_week_tag_library_review_built_without_model",
                {
                    "tag_review_id": review_id,
                    "domain_label": domain_label,
                    "current_count": len(current),
                    "suggested_count": len(suggested),
                    "filtered_count": len(filtered),
                    "model_calls": 0,
                },
            )
        return self.get_two_week_tag_library_review(tag_review_id=review_id)

    def get_two_week_tag_library_review(
        self, *, tag_review_id: str
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_two_week_tag_library_review "
            "WHERE tag_review_id=? AND data_identity=?",
            (tag_review_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("two-week whole-library review does not exist")
        candidate_set = json.loads(row["candidate_set_json"])
        return {
            "tag_review_id": row["tag_review_id"],
            "domain_label": row["domain_label"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "status": row["status"],
            "model_calls": 0,
            "current": candidate_set.get("current", []),
            "suggested": candidate_set.get("suggested", []),
            "filtered": candidate_set.get("filtered", []),
            "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_reason": row["review_reason"],
        }

    def list_open_two_week_tag_library_reviews(
        self, *, domain_label: str | None = None
    ) -> list[dict[str, Any]]:
        if domain_label:
            rows = self.conn.execute(
                "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
                "WHERE domain_label=? AND status='awaiting_human_review' "
                "AND data_identity=? ORDER BY window_end, tag_review_id",
                (domain_label, self.data_identity),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
                "WHERE status='awaiting_human_review' AND data_identity=? "
                "ORDER BY window_end, tag_review_id",
                (self.data_identity,),
            ).fetchall()
        return [
            self.get_two_week_tag_library_review(
                tag_review_id=str(row["tag_review_id"])
            )
            for row in rows
        ]

    def review_two_week_tag_library(
        self,
        *,
        tag_review_id: str,
        decisions: tuple[dict[str, Any], ...],
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError(
                "two-week whole-library changes require an explicit user decision"
            )
        review = self.get_two_week_tag_library_review(tag_review_id=tag_review_id)
        if review["status"] != "awaiting_human_review":
            raise StateTransitionError("two-week whole-library review is not open")
        entries = {
            str(item["tag_id"]): item
            for item in [*review["current"], *review["suggested"]]
        }
        submitted: dict[str, dict[str, Any]] = {}
        kept_names: set[str] = set()
        for decision in decisions:
            tag_id = str(decision.get("tag_id") or "").strip()
            keep = decision.get("keep")
            final_tag = str(decision.get("edited_tag") or "").strip().lstrip("#")
            if tag_id not in entries or not isinstance(keep, bool):
                raise StateTransitionError(
                    "every whole-library entry must have one keep or delete decision"
                )
            if keep and not final_tag:
                final_tag = str(entries[tag_id]["tag"])
            normalized = final_tag.casefold()
            if keep and normalized in kept_names:
                raise StateTransitionError(
                    "the reviewed whole tag library contains duplicate tags"
                )
            if keep:
                kept_names.add(normalized)
            submitted[tag_id] = {
                "keep": keep,
                "edited_tag": final_tag,
            }
        if set(submitted) != set(entries):
            raise StateTransitionError(
                "the complete current and suggested tag library must be reviewed once"
            )
        deleted = 0
        kept = 0
        with self.conn:
            for tag_id, decision in submitted.items():
                if decision["keep"]:
                    duplicate = self.conn.execute(
                        "SELECT tag_id FROM domain_search_tags "
                        "WHERE domain_label=? AND lower(tag)=lower(?) AND tag_id!=? LIMIT 1",
                        (
                            review["domain_label"],
                            decision["edited_tag"],
                            tag_id,
                        ),
                    ).fetchone()
                    if duplicate is not None:
                        raise StateTransitionError(
                            "an edited tag duplicates another domain tag"
                        )
                    self.conn.execute(
                        "UPDATE domain_search_tags SET tag=?, status='active', "
                        "human_review_status='approved', reviewed_at=?, reviewed_note=? "
                        "WHERE tag_id=? AND domain_label=?",
                        (
                            decision["edited_tag"],
                            _now(),
                            reason.strip(),
                            tag_id,
                            review["domain_label"],
                        ),
                    )
                    kept += 1
                else:
                    self._hard_delete_domain_tag(tag_id=tag_id)
                    deleted += 1
            now = _now()
            self.conn.execute(
                "UPDATE stage0_two_week_tag_library_review "
                "SET status='accepted', reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE tag_review_id=? AND data_identity=? "
                "AND status='awaiting_human_review'",
                (
                    actor.strip(),
                    now,
                    reason.strip(),
                    tag_review_id,
                    self.data_identity,
                ),
            )
            result = {
                "tag_review_id": tag_review_id,
                "status": "accepted",
                "kept_count": kept,
                "deleted_count": deleted,
                "reviewed_by": actor.strip(),
                "reviewed_at": now,
            }
            self._audit(None, "two_week_tag_library_reviewed_as_a_whole", result)
        return result

    def decide_competitor_registration_tag_candidate(
        self,
        *,
        registration_id: str,
        tag_id: str,
        decision: str,
        edited_tag: str,
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, str]:
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("tag candidate decision requires an explicit user decision and reason")
        if decision not in {"accepted", "rejected"}:
            raise StateTransitionError("tag candidate decision must be accepted or rejected")
        candidates = {item["tag_id"]: item for item in self.list_competitor_registration_tag_candidates(registration_id=registration_id)}
        candidate = candidates.get(tag_id)
        if candidate is None:
            raise StateTransitionError("tag candidate does not belong to this competitor registration")
        if candidate["human_review_status"] != "pending_review":
            raise StateTransitionError("tag candidate has already received a user decision")
        final_tag = edited_tag.strip().lstrip("#") if decision == "accepted" else str(candidate["tag"])
        if decision == "accepted" and not final_tag:
            raise StateTransitionError("an accepted tag candidate requires a non-empty tag")
        registration = self.get_competitor_registration(registration_id=registration_id)
        duplicate = self.conn.execute(
            "SELECT tag_id FROM domain_search_tags WHERE domain_label=? AND tag=? AND tag_id!=? LIMIT 1",
            (registration["domain_label"], final_tag, tag_id),
        ).fetchone()
        if duplicate is not None:
            raise StateTransitionError("the edited tag already exists in this domain")
        review_status = "approved" if decision == "accepted" else "rejected"
        status = "active" if decision == "accepted" else "closed"
        with self.conn:
            self.conn.execute(
                "UPDATE domain_search_tags SET tag=?, status=?, human_review_status=?, reviewed_at=?, reviewed_note=? "
                "WHERE tag_id=? AND domain_label=? AND human_review_status='pending_review'",
                (final_tag, status, review_status, _now(), reason.strip(), tag_id, registration["domain_label"]),
            )
            result = {
                "registration_id": registration_id,
                "tag_id": tag_id,
                "tag": final_tag,
                "status": status,
                "decision": decision,
            }
            self._audit(None, "competitor_registration_tag_candidate_decided", {**result, "actor": actor.strip()})
        return result

    def _persist_competitor_registration_gateway_envelope(
        self, envelope: ModelRunEnvelope, binding: dict[str, Any]
    ) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("competitor-registration model result belongs to another data identity")
        registration_id = str(binding.get("registration_id") or "")
        step_name = str(binding.get("step_name") or "")
        if step_name != "breakdown":
            raise ModelGatewayRequiredError(
                "competitor-registration model runs are allowed only for individual hit breakdowns"
            )
        registration = self.get_competitor_registration(registration_id=registration_id)
        prepared_breakdown_steps = {"transcripts_and_comments", "breakdown"}
        active_registration = (
            registration["status"] == "processing"
            and registration["current_step"] in prepared_breakdown_steps
        )
        closed_registration_material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND status='completed' AND data_identity=?",
            (registration_id, str(binding.get("source_id") or ""), self.data_identity),
        ).fetchone() is not None
        if not active_registration and not closed_registration_material:
            raise StaleResultError("competitor-breakdown model result lacks a completed source material")
        model_run_id = _id("competitor_registration_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, registration_id, step_name, envelope.status, envelope.route_name,
                    envelope.provider_name, envelope.model_name, envelope.input_hash, envelope.output_hash,
                    _canonical(envelope.error or {}),
                    _canonical({
                        "prompt_tokens": envelope.usage.prompt_tokens,
                        "completion_tokens": envelope.usage.completion_tokens,
                        "total_tokens": envelope.usage.total_tokens,
                    }),
                    _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(),
                ),
            )
        return model_run_id

    def _persist_daily_hit_gateway_envelope(
        self, envelope: ModelRunEnvelope, binding: dict[str, Any]
    ) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("daily-hit model result belongs to another data identity")
        hit_id = str(binding.get("hit_id") or "")
        hit = self.conn.execute(
            "SELECT preparation_status FROM hits WHERE hit_id=?",
            (hit_id,),
        ).fetchone()
        if hit is None or hit["preparation_status"] != "completed":
            raise StaleResultError("daily-hit model result lacks a completed prepared hit")
        model_run_id = _id("daily_hit_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_daily_hit_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id,
                    hit_id,
                    envelope.status,
                    envelope.route_name,
                    envelope.provider_name,
                    envelope.model_name,
                    envelope.input_hash,
                    envelope.output_hash,
                    _canonical(envelope.error or {}),
                    _canonical({
                        "prompt_tokens": envelope.usage.prompt_tokens,
                        "completion_tokens": envelope.usage.completion_tokens,
                        "total_tokens": envelope.usage.total_tokens,
                    }),
                    _canonical(envelope.cost),
                    envelope.duration_ms,
                    self.data_identity,
                    _now(),
                ),
            )
        return model_run_id

    def complete_daily_hit_material(
        self,
        *,
        hit_id: str,
        transcript_ref: str,
        transcript_hash: str,
        asr_model_ref: str,
        vad_model_ref: str,
        source_media_hash: str,
        comments: list[dict[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        """Persist one daily hit's retained material before model analysis."""
        hit = self.conn.execute(
            "SELECT hit_id, preparation_status FROM hits WHERE hit_id=?",
            (hit_id,),
        ).fetchone()
        if hit is None:
            raise StateTransitionError("daily hit material refers to a missing hit")
        transcript_path = Path(str(transcript_ref or "").strip())
        if not transcript_path.is_file():
            raise StateTransitionError("daily hit material lacks its retained transcript file")
        transcript_text = transcript_path.read_text(encoding="utf-8").strip()
        if not transcript_text:
            raise StateTransitionError("daily hit material has an empty transcript")
        normalized_comments: list[dict[str, Any]] = []
        for position, comment in enumerate(comments, start=1):
            if not isinstance(comment, dict):
                continue
            comment_id = str(comment.get("comment_id") or "").strip()
            text = str(comment.get("text") or "").strip()
            if not comment_id or not text:
                continue
            normalized_comments.append({
                "comment_id": comment_id,
                "text": text,
                "like_count": int(comment.get("like_count") or 0),
                "sample_rank": int(comment.get("sample_rank") or position),
            })
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO hit_transcripts("
                "transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text, char_count, asr_model, "
                "vad_model, processing_method, audio_sha256, quality_flags, processing_status, run_id"
                ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'local_sensevoice', ?, '', 'completed', ?)",
                (
                    f"{hit_id}_v1",
                    hit_id,
                    transcript_text,
                    transcript_text,
                    len(transcript_text),
                    str(asr_model_ref or "not_reported"),
                    str(vad_model_ref or "not_reported"),
                    str(source_media_hash or ""),
                    str(run_id or "daily_material"),
                ),
            )
            for comment in normalized_comments:
                self.conn.execute(
                    "INSERT OR IGNORE INTO hit_comments("
                    "hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, observation_point, sampling_strategy, run_id"
                    ") VALUES (?, ?, ?, ?, NULL, ?, 'daily_hit', NULL, 'top_n_by_platform_popularity', ?)",
                    (
                        hit_id,
                        comment["comment_id"],
                        comment["text"],
                        comment["like_count"],
                        comment["sample_rank"],
                        str(run_id or "daily_material"),
                    ),
                )
            self.conn.execute(
                "UPDATE hits SET preparation_status='completed' WHERE hit_id=?",
                (hit_id,),
            )
        return {
            "hit_id": hit_id,
            "status": "completed",
            "transcript_hash": str(transcript_hash or ""),
            "comment_count": len(normalized_comments),
        }

    def repair_daily_hit_comments(
        self,
        *,
        hit_id: str,
        platform_item_id: str,
        comments: list[dict[str, Any]],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Restore comments for a completed daily hit from a saved detail capture."""
        if not hit_id.strip() or not platform_item_id.strip() or not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("daily comment repair requires a hit, source, actor and idempotency key")
        if not isinstance(comments, list):
            raise StateTransitionError("daily comment repair requires a comment list")
        hit = self.conn.execute(
            "SELECT hit_id, preparation_status, platform_item_id FROM hits WHERE hit_id=?",
            (hit_id.strip(),),
        ).fetchone()
        if hit is None or str(hit["platform_item_id"]) != platform_item_id.strip():
            raise StateTransitionError("daily comment repair source does not match the formal hit")
        if str(hit["preparation_status"]) != "completed":
            raise StateTransitionError("daily comment repair requires a completed hit material")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position, comment in enumerate(comments, start=1):
            if not isinstance(comment, dict):
                continue
            comment_id = str(comment.get("comment_id") or comment.get("id") or "").strip()
            text = str(comment.get("text") or comment.get("content") or "").strip()
            if not comment_id or not text or comment_id in seen:
                continue
            seen.add(comment_id)
            normalized.append({
                "comment_id": comment_id,
                "text": text,
                "like_count": int(comment.get("like_count") or 0),
                "sample_rank": int(comment.get("sample_rank") or position),
            })
        request = {
            "hit_id": hit_id.strip(),
            "platform_item_id": platform_item_id.strip(),
            "comments": normalized,
            "actor": actor.strip(),
        }
        replay = self._replay("repair_daily_hit_comments", idempotency_key, request)
        if replay:
            return replay
        inserted = 0
        with self.conn:
            for comment in normalized:
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO hit_comments("
                    "hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, "
                    "observation_point, sampling_strategy, run_id"
                    ") VALUES (?, ?, ?, ?, NULL, ?, 'daily_hit', NULL, 'top_n_by_platform_popularity', ?)",
                    (
                        hit_id.strip(), comment["comment_id"], comment["text"], comment["like_count"],
                        comment["sample_rank"], f"{actor.strip()}:{platform_item_id.strip()}",
                    ),
                )
                del cursor
                inserted += int(self.conn.execute("SELECT changes()").fetchone()[0] or 0)
            result = {
                "hit_id": hit_id.strip(),
                "platform_item_id": platform_item_id.strip(),
                "status": "repaired" if inserted else "already_present",
                "comment_count": inserted,
            }
            self._receipt("repair_daily_hit_comments", idempotency_key, request, result)
            self._audit(None, "daily_hit_comments_repaired", {
                **result, "actor": actor.strip(), "candidate_comment_count": len(normalized),
            })
        return result

    def record_daily_hit_breakdown(
        self,
        *,
        hit_id: str,
        artifact: dict[str, Any],
        model_run_id: str,
    ) -> dict[str, Any]:
        """Persist one validated daily breakdown as the current formal result."""
        if not isinstance(artifact, dict) or not str(artifact.get("source_id") or "").strip():
            raise StateTransitionError("daily breakdown artifact is missing its source identity")
        hit = self.conn.execute(
            "SELECT preparation_status FROM hits WHERE hit_id=?",
            (hit_id,),
        ).fetchone()
        if hit is None or hit["preparation_status"] != "completed":
            raise StateTransitionError("daily breakdown requires completed hit material")
        existing = self.conn.execute(
            "SELECT hit_id, model_run_id FROM stage0_daily_hit_breakdown "
            "WHERE hit_id=? AND data_identity=?",
            (hit_id, self.data_identity),
        ).fetchone()
        if existing is not None:
            return {
                "hit_id": hit_id,
                "status": "already_completed",
                "model_run_id": str(existing["model_run_id"]),
            }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_daily_hit_breakdown("
                "hit_id, version, artifact_json, model_run_id, data_identity, created_at"
                ") VALUES (?, 1, ?, ?, ?, ?)",
                (
                    hit_id,
                    _canonical(artifact),
                    str(model_run_id or "configured_business_analysis"),
                    self.data_identity,
                    _now(),
                ),
            )
        return {"hit_id": hit_id, "status": "completed", "model_run_id": str(model_run_id)}

    def record_daily_hit_processing_failure(
        self,
        *,
        hit_id: str,
        stage_name: str,
        error: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        """Keep one durable failure receipt without falsely completing the hit."""
        if not str(stage_name or "").strip() or not isinstance(error, dict):
            raise StateTransitionError("daily hit failure record is incomplete")
        hit = self.conn.execute(
            "SELECT hit_id FROM hits WHERE hit_id=?",
            (hit_id,),
        ).fetchone()
        if hit is None:
            raise StateTransitionError("daily hit failure refers to a missing hit")
        failure_id = _id("daily_hit_processing_failure")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_daily_hit_processing_failure("
                "failure_id, hit_id, stage_name, error_json, run_id, data_identity, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    failure_id,
                    hit_id,
                    str(stage_name).strip(),
                    _canonical(error),
                    str(run_id or "daily_processing"),
                    self.data_identity,
                    _now(),
                ),
            )
        return {
            "failure_id": failure_id,
            "hit_id": hit_id,
            "status": "recorded",
        }

    def observe_unregistered_account(
        self,
        *,
        domain_label: str,
        account_ref: str,
        account_display_name: str,
        video_ref: str,
        video_url: str,
        video_title: str,
        qualified: bool,
        source_ref: dict[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        """Retain an outside-account observation and raise review only at the confirmed three-in-thirty-days boundary."""
        if domain_label not in formal_domain_labels() or not all(
            str(value or "").strip() for value in (account_ref, account_display_name, video_ref, video_title, observed_at)
        ) or not video_url.startswith(("https://", "http://")) or not source_ref:
            raise StateTransitionError("unregistered account observation needs formal identity, accessible video and source evidence")
        try:
            current_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StateTransitionError("unregistered account observation time must be ISO-8601") from exc
        if current_time.tzinfo is None:
            raise StateTransitionError("unregistered account observation time must include a timezone")
        existing = self.conn.execute(
            "SELECT account_video_observation_id FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND video_ref=? AND data_identity=?",
            (domain_label, account_ref.strip(), video_ref.strip(), self.data_identity),
        ).fetchone()
        if existing is None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO stage0_unregistered_account_video VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_id("unregistered_account_video"), domain_label, account_ref.strip(), account_display_name.strip(),
                     video_ref.strip(), video_url.strip(), video_title.strip(), int(qualified), _canonical(source_ref),
                     observed_at, self.data_identity),
                )
        window_start = (current_time - timedelta(days=30)).isoformat()
        qualified_rows = self.conn.execute(
            "SELECT video_ref, video_url, video_title, observed_at, source_ref_json FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND qualified=1 AND observed_at>=? AND data_identity=? "
            "ORDER BY observed_at DESC, account_video_observation_id DESC",
            (domain_label, account_ref.strip(), window_start, self.data_identity),
        ).fetchall()
        already_open = self.conn.execute(
            "SELECT account_review_id FROM stage0_unregistered_account_review "
            "WHERE domain_label=? AND account_ref=? AND status='awaiting_human_review' AND data_identity=?",
            (domain_label, account_ref.strip(), self.data_identity),
        ).fetchone()
        if len(qualified_rows) < 3 or already_open is not None:
            return {
                "account_ref": account_ref.strip(), "qualified_video_count_30d": len(qualified_rows),
                "account_review_id": already_open["account_review_id"] if already_open else None,
                "human_confirmation_required": already_open is not None,
            }
        trigger_rows = qualified_rows[:3]
        recent_rows = self.conn.execute(
            "SELECT video_ref, video_url, video_title, observed_at FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND data_identity=? ORDER BY observed_at DESC, account_video_observation_id DESC LIMIT 10",
            (domain_label, account_ref.strip(), self.data_identity),
        ).fetchall()
        review_id = _id("unregistered_account_review")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_unregistered_account_review VALUES (?, ?, ?, ?, ?, ?, 'awaiting_human_review', ?, ?, NULL, NULL, NULL)",
                (
                    review_id, domain_label, account_ref.strip(), account_display_name.strip(),
                    _canonical([{"video_ref": row["video_ref"], "video_url": row["video_url"], "video_title": row["video_title"], "observed_at": row["observed_at"], "source_ref": json.loads(row["source_ref_json"])} for row in trigger_rows]),
                    _canonical([{"video_ref": row["video_ref"], "video_url": row["video_url"], "video_title": row["video_title"], "observed_at": row["observed_at"]} for row in recent_rows]),
                    self.data_identity, _now(),
                ),
            )
            self._audit(None, "unregistered_account_ready_for_human_review", {"account_review_id": review_id, "account_ref": account_ref.strip()})
        return {"account_ref": account_ref.strip(), "qualified_video_count_30d": len(qualified_rows), "account_review_id": review_id, "human_confirmation_required": True}

    def decide_unregistered_account_review(
        self,
        *,
        account_review_id: str,
        decision: str,
        owned_account_id: str | None,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if decision not in {"accepted", "rejected"} or actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("unregistered account review requires an explicit user decision and reason")
        review = self.conn.execute(
            "SELECT * FROM stage0_unregistered_account_review WHERE account_review_id=? AND data_identity=?",
            (account_review_id, self.data_identity),
        ).fetchone()
        if review is None or review["status"] != "awaiting_human_review":
            raise StateTransitionError("unregistered account review is not awaiting a user decision")
        if decision == "rejected":
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_unregistered_account_review SET status='rejected', decided_by=?, decided_at=?, decision_reason=? WHERE account_review_id=?",
                    (actor.strip(), _now(), reason.strip(), account_review_id),
                )
            return {"account_review_id": account_review_id, "status": "rejected"}
        owned = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (owned_account_id, self.data_identity),
        ).fetchone()
        if owned is None or owned["account_role"] != "owned" or owned["domain_label"] != review["domain_label"]:
            raise StateTransitionError("accepting an unregistered account requires an owned account in the same domain")
        competitor_id = _id("competitor_account")
        self.register_content_account(
            content_account_id=competitor_id, account_role="competitor", display_name=review["account_display_name"],
            domain_label=review["domain_label"], external_account_ref=review["account_ref"], actor=actor,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_unregistered_account_review SET status='accepted', decided_by=?, decided_at=?, decision_reason=? WHERE account_review_id=?",
                (actor.strip(), _now(), reason.strip(), account_review_id),
            )
        return {
            "account_review_id": account_review_id, "status": "accepted", "competitor_account_id": competitor_id,
            "registration_id": None,
            "registration_status": "pending_full_cold_start",
        }

    def _activate_competitor_daily_tracking(self, *, registration_id: str) -> str:
        registration = self.get_competitor_registration(registration_id=registration_id)
        steps = {item["step_name"]: item["artifact_refs"] for item in self.list_competitor_registration_steps(registration_id=registration_id)}
        if any(step not in steps for step in COMPETITOR_TRACKING_ACTIVATION_STEPS):
            raise StateTransitionError("daily tracking activation requires every competitor registration step")
        account = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (registration["competitor_account_id"], self.data_identity),
        ).fetchone()
        if account is None:
            raise StateTransitionError("competitor account disappeared before daily tracking activation")
        history = steps["historical_material"]
        signals = steps["high_signal_identification"]
        if len(history) != 1 or len(signals) != 1:
            raise StateTransitionError("daily tracking activation requires one historical and one signal artifact")
        platform = str(history[0].get("platform") or "").strip()
        external_ref = str(account["external_account_ref"] or "").strip()
        source_value = external_ref[len(platform) + 1:] if external_ref.startswith(f"{platform}:") else external_ref
        if not platform or not source_value:
            raise StateTransitionError("daily tracking activation lacks a platform account identity")
        homepage_url = source_value if source_value.startswith(("https://", "http://")) else (
            f"https://www.douyin.com/user/{source_value}" if platform == "douyin" else source_value
        )
        sec_uid = source_value.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        existing_account = self.conn.execute(
            "SELECT account_id, domain_label FROM competitor_accounts WHERE platform=? AND sec_uid=?", (platform, sec_uid)
        ).fetchone()
        if existing_account is not None and str(existing_account["domain_label"] or "") != str(account["domain_label"]):
            raise StateTransitionError(
                f"external account {external_ref} already belongs to domain {existing_account['domain_label']}; "
                "daily tracking cannot overwrite the original domain"
            )
        tracking_account_id = str(existing_account["account_id"]) if existing_account else str(account["content_account_id"])
        now = datetime.now(timezone.utc)
        selected = {str(item.get("source_id")): item for item in signals[0].get("selected_items", [])}
        materials = {
            item["item_ref"]: item["artifact"] for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        }
        breakdowns = {
            item["item_ref"]: item["artifact"] for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            ) if item["status"] == "completed"
        }
        with self.conn:
            if existing_account is None:
                self.conn.execute(
                    "INSERT INTO competitor_accounts("
                    "account_id, platform, domain_label, domain_name, account_name, sec_uid, homepage_url, source_config_ref, "
                    "registration_status, first_crawl_policy, comments_policy"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'stock_snapshot_archived', 'promoted_hits_only')",
                    (
                        tracking_account_id, platform, account["domain_label"], account["domain_label"], account["display_name"],
                        sec_uid, homepage_url, f"stage0_competitor_registration:{registration_id}",
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE competitor_accounts SET account_name=?, homepage_url=?, "
                    "source_config_ref=?, registration_status='active' WHERE account_id=?",
                    (
                        account["display_name"], homepage_url,
                        f"stage0_competitor_registration:{registration_id}", tracking_account_id,
                    ),
                )
            for item in history[0].get("items", []):
                source_id = str(item.get("source_id") or "").strip()
                title, url = str(item.get("title") or "").strip(), str(item.get("url") or "").strip()
                if not source_id or not title or not url:
                    continue
                publish_value = item.get("published_at")
                publish_time = None
                if isinstance(publish_value, (int, float)):
                    publish_time = datetime.fromtimestamp(float(publish_value), tz=timezone.utc).isoformat()
                elif str(publish_value or "").strip():
                    raw_publish = str(publish_value).strip()
                    try:
                        parsed_publish = datetime.fromisoformat(raw_publish.replace("Z", "+00:00"))
                        if parsed_publish.tzinfo is None:
                            parsed_publish = parsed_publish.replace(tzinfo=timezone.utc)
                        publish_time = parsed_publish.isoformat()
                    except ValueError:
                        publish_time = None
                category = None
                if publish_time:
                    category = "historical_mature" if datetime.fromisoformat(publish_time) <= now - timedelta(days=7) else "transition"
                metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
                video_id = "competitor_video_" + _hash({"account_id": tracking_account_id, "source_id": source_id})[:20]
                signal = selected.get(source_id)
                trigger_rules = list(signal.get("signal_channels", [])) if signal else []
                self.conn.execute(
                    "INSERT INTO competitor_videos("
                    "video_id, account_id, platform, platform_item_id, title, url, publish_time, duration_sec, "
                    "like_count, comment_count, share_count, collect_count, first_contact_category, excluded_reason, "
                    "registration_run_id, raw_archive_ref, raw_json, first_trigger_observation, first_trigger_at, "
                    "trigger_rules, peak_observation, baseline_mode, judgment_confidence"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, platform_item_id) DO UPDATE SET "
                    "title=excluded.title, url=excluded.url, publish_time=excluded.publish_time, duration_sec=excluded.duration_sec, "
                    "like_count=excluded.like_count, comment_count=excluded.comment_count, share_count=excluded.share_count, "
                    "collect_count=excluded.collect_count, registration_run_id=excluded.registration_run_id, "
                    "raw_archive_ref=excluded.raw_archive_ref, raw_json=excluded.raw_json, last_checked_at=CURRENT_TIMESTAMP, "
                    "check_count=competitor_videos.check_count+1",
                    (
                        video_id, tracking_account_id, platform, source_id, title, url, publish_time,
                        int(item.get("duration_seconds") or 0), int(metrics.get("like_count") or 0),
                        int(metrics.get("comment_count") or 0), int(metrics.get("share_count") or 0),
                        int(metrics.get("collect_count") or 0), category, None if category else "missing_publish_time",
                        registration_id, str(history[0].get("raw_archive_ref") or ""), _canonical(item),
                        "registration_history" if signal else None, _now() if signal else None,
                        _canonical(trigger_rules) if signal else None, "registration_history" if signal else None,
                        "mature_history" if signal else None, "rough" if signal else None,
                    ),
                )
                if not signal:
                    continue
                hit_id = "competitor_hit_" + _hash({"account_id": tracking_account_id, "source_id": source_id})[:20]
                self.conn.execute(
                    "INSERT OR IGNORE INTO hits("
                    "hit_id, video_id, account_id, platform, platform_item_id, title, url, publish_time, like_count, "
                    "comment_count, share_count, collect_count, hit_channel, judgment_confidence, baseline_id, run_id, preparation_status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rough', NULL, ?, 'completed')",
                    (
                        hit_id, video_id, tracking_account_id, platform, source_id, title, url, publish_time,
                        int(metrics.get("like_count") or 0), int(metrics.get("comment_count") or 0),
                        int(metrics.get("share_count") or 0), int(metrics.get("collect_count") or 0),
                        ",".join(trigger_rules), registration_id,
                    ),
                )
                material = materials.get(source_id)
                if material:
                    transcript_path = Path(str(material.get("transcript_ref") or ""))
                    if not transcript_path.is_file():
                        raise StateTransitionError("completed competitor material lacks its retained transcript file")
                    transcript_text = transcript_path.read_text(encoding="utf-8").strip()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO hit_transcripts("
                        "transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text, char_count, asr_model, "
                        "vad_model, processing_method, audio_sha256, quality_flags, processing_status, run_id"
                        ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'local_sensevoice', ?, '', 'completed', ?)",
                        (
                            f"{hit_id}_v1", hit_id, transcript_text, transcript_text, len(transcript_text),
                            str(material.get("asr_model_ref") or "not_reported"),
                            str(material.get("vad_model_ref") or "not_reported"),
                            str(material.get("source_media_hash") or ""), registration_id,
                        ),
                    )
                    for comment in material.get("comments", []):
                        self.conn.execute(
                            "INSERT OR IGNORE INTO hit_comments("
                            "hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, observation_point, sampling_strategy, run_id"
                            ") VALUES (?, ?, ?, ?, NULL, ?, 'mature_history', NULL, 'top_n_by_platform_popularity', ?)",
                            (
                                hit_id, comment["comment_id"], comment["text"], int(comment["like_count"]),
                                int(comment["sample_rank"]), registration_id,
                            ),
                        )
                breakdown = breakdowns.get(source_id)
                if breakdown:
                    analysis = breakdown.get("analysis") or {}
                    model_run_id = str(breakdown.get("model_run_id") or "configured_business_analysis")
                    model_row = self.conn.execute(
                        "SELECT model_name FROM stage0_competitor_registration_model_run WHERE registration_model_run_id=?",
                        (model_run_id,),
                    ).fetchone()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO hit_deep_analysis("
                        "analysis_id, hit_id, version, request_id, correlation_id, topic_pattern, hook_pattern, "
                        "structure_pattern, model_name, run_id"
                        ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "competitor_analysis_" + _hash({"registration_id": registration_id, "source_id": source_id})[:20],
                            hit_id, model_run_id, f"{registration_id}:{source_id}", str(analysis.get("topic_pattern") or ""),
                            str(analysis.get("hook_pattern") or ""), str(analysis.get("structure_pattern") or ""),
                            str(model_row["model_name"]) if model_row else "configured_business_analysis", registration_id,
                        ),
                    )
        return tracking_account_id

    def reconcile_completed_competitor_hit_index(
        self,
        *,
        domain_label: str | None = None,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reconcile completed cold-start selections into the formal hit index.

        This only replays already-saved registration artifacts.  It never
        collects, downloads, transcribes, or calls a model.
        """
        if not actor.strip() or not idempotency_key.strip():
            raise StateTransitionError("hit-index reconciliation requires an actor and idempotency key")
        query = (
            "SELECT registration_id FROM stage0_competitor_registration "
            "WHERE status='completed' AND data_identity=?"
        )
        params: list[str] = [self.data_identity]
        if domain_label is not None:
            query += (
                " AND competitor_account_id IN ("
                "SELECT content_account_id FROM stage0_content_account "
                "WHERE domain_label=? AND data_identity=?"
                ")"
            )
            params.extend([str(domain_label), self.data_identity])
        query += " ORDER BY registration_id"
        registrations = self.conn.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in registrations:
            registration_id = str(row["registration_id"])
            step_names = {
                str(step["step_name"])
                for step in self.list_competitor_registration_steps(
                    registration_id=registration_id
                )
            }
            missing_steps = sorted(
                set(COMPETITOR_TRACKING_ACTIVATION_STEPS) - step_names
            )
            if missing_steps:
                if missing_steps != ["breakdown"]:
                    results.append({
                        "registration_id": registration_id,
                        "status": "blocked_incomplete_formal_steps",
                        "missing_steps": missing_steps,
                    })
                    continue
                repair = self.repair_completed_registration_breakdown_step(
                    registration_id=registration_id,
                    actor=actor,
                    idempotency_key=f"{idempotency_key}:{registration_id}:breakdown",
                )
                if repair["status"] not in {"repaired", "already_present"}:
                    raise StateTransitionError("legacy registration breakdown repair did not complete")
            before = self.conn.execute(
                "SELECT COUNT(*) AS count FROM hits hit "
                "JOIN competitor_accounts account ON account.account_id=hit.account_id "
                "JOIN stage0_competitor_registration registration "
                "ON account.source_config_ref='stage0_competitor_registration:' || registration.registration_id "
                "WHERE registration.registration_id=?",
                (registration_id,),
            ).fetchone()
            tracking_account_id = self._activate_competitor_daily_tracking(
                registration_id=registration_id,
            )
            after = self.conn.execute(
                "SELECT COUNT(*) AS count FROM hits WHERE account_id=?",
                (tracking_account_id,),
            ).fetchone()
            results.append({
                "registration_id": registration_id,
                "status": "completed",
                "tracking_account_id": tracking_account_id,
                "hit_count_before": int(before["count"] if before else 0),
                "hit_count_after": int(after["count"] if after else 0),
            })
        blocked = any(
            item.get("status") == "blocked_incomplete_formal_steps"
            for item in results
        )
        return {
            "status": "completed_with_blocks" if blocked else "completed",
            "registration_count": len(results),
            "registrations": results,
        }

    def complete_competitor_registration(
        self,
        *,
        registration_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Complete registration automatically after every required artifact exists."""
        if not actor.strip():
            raise StateTransitionError("competitor registration completion requires an actor")
        registration = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration WHERE registration_id=? AND data_identity=?", (registration_id, self.data_identity)
        ).fetchone()
        if registration is None or registration["status"] != "awaiting_human_review":
            raise StateTransitionError("competitor registration is not ready for automatic completion")
        request = {"registration_id": registration_id, "actor": actor}
        replay = self._replay("complete_competitor_registration", idempotency_key, request)
        if replay:
            # A previous process can have written the completion receipt and
            # daily-tracking material before an interrupted state update became
            # visible.  Replaying must reconcile that proven completed work,
            # never launch collection or model work again.
            tracking_account_id = str(replay.get("daily_tracking_account_id") or "").strip()
            tracking_exists = self.conn.execute(
                "SELECT 1 FROM competitor_accounts WHERE account_id=?",
                (tracking_account_id,),
            ).fetchone()
            if str(replay.get("status") or "") != "completed" or not tracking_account_id or tracking_exists is None:
                raise StateTransitionError(
                    "stored registration completion receipt cannot be reconciled safely"
                )
            now = _now()
            result = {
                "registration_id": registration_id,
                "status": "completed",
                "daily_tracking_account_id": tracking_account_id,
            }
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_competitor_registration "
                    "SET current_step='completed', status='completed', completed_at=? "
                    "WHERE registration_id=? AND data_identity=? "
                    "AND status='awaiting_human_review'",
                    (now, registration_id, self.data_identity),
                )
                self._audit(
                    None,
                    "competitor_registration_completion_reconciled_from_receipt",
                    result,
                )
            result["cold_start_completion"] = self.try_complete_cold_start(
                cold_start_id=str(registration["cold_start_id"]),
                trigger="competitor_registration_ready",
                actor=actor,
            )
            return result
        tracking_account_id = self._activate_competitor_daily_tracking(registration_id=registration_id)
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step='completed', status='completed', completed_at=? WHERE registration_id=?",
                (now, registration_id),
            )
            result = {"registration_id": registration_id, "status": "completed", "daily_tracking_account_id": tracking_account_id}
            self._receipt("complete_competitor_registration", idempotency_key, request, result)
            self._audit(None, "competitor_registration_completed_automatically", result)
        result["cold_start_completion"] = self.try_complete_cold_start(
            cold_start_id=str(registration["cold_start_id"]),
            trigger="competitor_registration_ready",
            actor=actor,
        )
        return result

    def discard_cold_start_breakdown_history(
        self,
        *,
        cold_start_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, int | str]:
        """Permanently remove every obsolete breakdown result before one full replacement run.

        Original collected material is intentionally retained: the replacement
        Skill must use the same formal source, transcript, and comments rather
        than silently recollecting or falling back to a retired analysis.
        """
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("breakdown replacement requires an explicit user decision")
        cold_start = self.conn.execute(
            "SELECT status FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None or str(cold_start["status"]) != "waiting_human":
            raise StateTransitionError("breakdown replacement requires a cold start awaiting final review")
        request = {"cold_start_id": cold_start_id, "actor": actor, "reason": reason}
        replay = self._replay("discard_cold_start_breakdown_history", idempotency_key, request)
        if replay:
            return replay
        registrations = self.conn.execute(
            "SELECT registration_id FROM stage0_competitor_registration "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchall()
        registration_ids = [str(row["registration_id"]) for row in registrations]
        if not registration_ids:
            raise StateTransitionError("breakdown replacement requires registered competitor accounts")
        placeholders = ",".join("?" for _ in registration_ids)
        parameters = tuple(registration_ids) + (self.data_identity,)
        material_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_item "
            f"WHERE registration_id IN ({placeholders}) AND data_identity=? "
            "AND step_name='transcripts_and_comments' AND status='completed'",
            parameters,
        ).fetchone()[0])
        if not material_count:
            raise StateTransitionError("breakdown replacement requires retained completed source materials")
        with self.conn:
            breakdown_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration_item "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            ).fetchone()[0])
            model_run_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration_model_run "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            ).fetchone()[0])
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_model_run "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            # A replacement run is the one explicit exception to the normal
            # immutable-step rule: the user has retired this entire prior
            # breakdown route and requested physical removal, not a fallback.
            self.conn.execute(
                "DROP TRIGGER IF EXISTS stage0_competitor_registration_step_immutable_delete"
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_step "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            self.conn.execute(
                "CREATE TRIGGER stage0_competitor_registration_step_immutable_delete "
                "BEFORE DELETE ON stage0_competitor_registration_step "
                "BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END"
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_item "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            result: dict[str, int | str] = {
                "cold_start_id": cold_start_id,
                "status": "breakdown_history_discarded",
                "retained_source_materials": material_count,
                "discarded_breakdown_records": breakdown_count,
                "discarded_breakdown_model_runs": model_run_count,
            }
            self._receipt("discard_cold_start_breakdown_history", idempotency_key, request, result)
            self._audit(None, "cold_start_breakdown_history_discarded", result)
        return result

    def list_prepared_material_cards(self) -> list[dict[str, Any]]:
        """Return source-bound material cards for the readable knowledge mirror.

        The formal registration item is the source of truth.  This read model
        joins the completed or failed preparation record with the original
        high-signal selection and the latest breakdown record, so the mirror
        can update one stable card as the source moves through the pipeline.
        """
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.status, "
            "item.artifact_json, item.error_json, item.updated_at, "
            "registration.cold_start_id, account.display_name, account.domain_label "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "WHERE item.data_identity=? AND item.step_name='transcripts_and_comments' "
            "AND item.status IN ('completed', 'failed') "
            "ORDER BY item.updated_at, item.registration_id, item.item_ref",
            (self.data_identity,),
        ).fetchall()
        if not rows:
            return []

        registration_ids = sorted({str(row["registration_id"]) for row in rows})
        selection_by_registration: dict[str, dict[str, dict[str, Any]]] = {}
        for registration_id in registration_ids:
            step = self.conn.execute(
                "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
                "WHERE registration_id=? AND step_name='high_signal_identification' "
                "AND data_identity=? ORDER BY completed_at DESC, step_record_id DESC LIMIT 1",
                (registration_id, self.data_identity),
            ).fetchone()
            if step is None:
                continue
            try:
                payload = _hide_build_root_paths(json.loads(str(step["artifact_refs_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            selected: dict[str, dict[str, Any]] = {}
            for artifact in payload.get("artifact_refs", []) if isinstance(payload, dict) else []:
                if not isinstance(artifact, dict):
                    continue
                for item in artifact.get("selected_items", []):
                    if not isinstance(item, dict):
                        continue
                    source_id = str(item.get("source_id") or "").strip()
                    if source_id:
                        selected[source_id] = item
            selection_by_registration[registration_id] = selected

        breakdown_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        breakdown_rows = self.conn.execute(
            "SELECT registration_id, item_ref, status, artifact_json, error_json, updated_at "
            "FROM stage0_competitor_registration_item "
            "WHERE data_identity=? AND step_name='breakdown' "
            "AND registration_id IN (" + ",".join("?" for _ in registration_ids) + ")",
            [self.data_identity, *registration_ids],
        ).fetchall()
        for row in breakdown_rows:
            try:
                artifact = _hide_build_root_paths(json.loads(str(row["artifact_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                artifact = {}
            try:
                error = _hide_build_root_paths(json.loads(str(row["error_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                error = {}
            breakdown_by_key[(str(row["registration_id"]), str(row["item_ref"]))] = {
                "status": str(row["status"]),
                "artifact": artifact if isinstance(artifact, dict) else {},
                "error": error if isinstance(error, dict) else {},
                "updated_at": str(row["updated_at"]),
            }

        cards: list[dict[str, Any]] = []
        for row in rows:
            registration_id = str(row["registration_id"])
            source_id = str(row["item_ref"])
            try:
                material = _hide_build_root_paths(json.loads(str(row["artifact_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                material = {}
            try:
                material_error = _hide_build_root_paths(json.loads(str(row["error_json"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                material_error = {}
            selected = dict(selection_by_registration.get(registration_id, {}).get(source_id) or {})
            breakdown = breakdown_by_key.get((registration_id, source_id))
            cards.append({
                "registration_id": registration_id,
                "cold_start_id": str(row["cold_start_id"]),
                "source_id": source_id,
                "domain_label": str(row["domain_label"]),
                "account_name": str(row["display_name"]),
                "title": str(selected.get("title") or source_id),
                "url": str(selected.get("url") or (material.get("source_url") if isinstance(material, dict) else "") or ""),
                "publish_time": str(selected.get("publish_time") or selected.get("published_at") or ""),
                "metrics": dict((material or {}).get("metrics") or {}) if isinstance(material, dict) else {},
                "material_status": str(row["status"]),
                "material": material if isinstance(material, dict) else {},
                "material_error": material_error if isinstance(material_error, dict) else {},
                "material_updated_at": str(row["updated_at"]),
                "breakdown_status": str(breakdown["status"]) if breakdown else "pending",
                "breakdown": dict(breakdown["artifact"]) if breakdown else {},
                "breakdown_error": dict(breakdown["error"]) if breakdown else {},
                "breakdown_updated_at": str(breakdown["updated_at"]) if breakdown else "",
            })
        return cards

    def list_cold_start_breakdown_materials(self, *, cold_start_id: str) -> list[dict[str, Any]]:
        """Return retained formal source materials for the one current replacement run."""
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.artifact_json "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.data_identity=? "
            "AND item.step_name='transcripts_and_comments' AND item.status='completed' "
            "ORDER BY item.registration_id, item.item_ref",
            (cold_start_id, self.data_identity),
        ).fetchall()
        return [
            {
                "registration_id": str(row["registration_id"]),
                "source_id": str(row["item_ref"]),
                "material": _hide_build_root_paths(json.loads(str(row["artifact_json"]))),
            }
            for row in rows
        ]

    def create_competitor_breakdown_backlog_task(
        self,
        *,
        cold_start_id: str,
        approval: dict[str, str],
        expected_pending_count: int,
    ) -> dict[str, Any]:
        """Freeze one explicit formal backlog before any model call begins."""
        if expected_pending_count < 1:
            raise StateTransitionError("formal breakdown backlog needs a positive expected material count")
        if not str(approval.get("actor") or "").strip() or not str(approval.get("conversation_ref") or "").strip():
            raise StateTransitionError("formal breakdown backlog needs recorded user approval")
        cold_start = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("formal breakdown backlog does not belong to this data identity")
        active = self.conn.execute(
            "SELECT backlog_task_id FROM stage0_competitor_breakdown_backlog_task "
            "WHERE cold_start_id=? AND data_identity=? AND status IN ('queued', 'running') LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if active is not None:
            raise StateTransitionError("this cold-start already has an active formal breakdown backlog task")
        materials = self.list_cold_start_breakdown_materials(cold_start_id=cold_start_id)
        existing_rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.status "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.data_identity=? AND item.step_name='breakdown'",
            (cold_start_id, self.data_identity),
        ).fetchall()
        existing = {(str(row["registration_id"]), str(row["item_ref"])): str(row["status"]) for row in existing_rows}
        snapshot = [
            {"registration_id": str(item["registration_id"]), "source_id": str(item["source_id"])}
            for item in materials
            if existing.get((str(item["registration_id"]), str(item["source_id"]))) in {None, "failed"}
        ]
        if len(snapshot) != expected_pending_count:
            raise StateTransitionError(
                f"formal breakdown backlog changed before launch: expected {expected_pending_count}, found {len(snapshot)}"
            )
        now = _now()
        task_id = _id("competitor_breakdown_backlog")
        summary = {"source_count": len(snapshot), "completed": 0, "failed": 0, "pending": len(snapshot)}
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_backlog_task("
                "backlog_task_id, cold_start_id, status, source_snapshot_json, approval_json, summary_json, "
                "data_identity, created_at, updated_at, completed_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, NULL)",
                (task_id, cold_start_id, _canonical(snapshot), _canonical(approval), _canonical(summary), self.data_identity, now, now),
            )
            result = self.get_competitor_breakdown_backlog_task(backlog_task_id=task_id)
            self._audit(None, "competitor_breakdown_backlog_task_created", result)
        return result

    def get_competitor_breakdown_backlog_task(self, *, backlog_task_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_competitor_breakdown_backlog_task WHERE backlog_task_id=? AND data_identity=?",
            (backlog_task_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("formal breakdown backlog task does not exist in this data identity")
        return {
            "backlog_task_id": str(row["backlog_task_id"]),
            "cold_start_id": str(row["cold_start_id"]),
            "status": str(row["status"]),
            "source_snapshot": json.loads(str(row["source_snapshot_json"])),
            "approval": json.loads(str(row["approval_json"])),
            "summary": json.loads(str(row["summary_json"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "completed_at": row["completed_at"],
        }

    def create_active_mixed_breakdown_replacement_task(
        self,
        *,
        approval: dict[str, str],
        expected_count: int,
        reason: str,
    ) -> dict[str, Any]:
        """Freeze exactly the active completed records still using mixed or unclear labels."""
        actor = str(approval.get("actor") or "").strip()
        conversation_ref = str(
            approval.get("session_ref") or approval.get("conversation_ref") or ""
        ).strip()
        if not actor or not conversation_ref or expected_count < 1 or not reason.strip():
            raise StateTransitionError("mixed breakdown replacement needs explicit approval, count and reason")
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.artifact_json, registration.cold_start_id "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "WHERE item.data_identity=? AND item.step_name='breakdown' "
            "AND item.status='completed' AND account.status='active' "
            "ORDER BY item.item_ref",
            (self.data_identity,),
        ).fetchall()
        selected: list[dict[str, str]] = []
        cold_start_ids: set[str] = set()
        for row in rows:
            try:
                artifact = json.loads(str(row["artifact_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            deep = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
            if not isinstance(deep, dict):
                continue
            subject = str(deep.get("content_subject_type") or "unclear")
            expression = str(deep.get("expression_form") or "unclear")
            if subject not in {"mixed", "unclear"} and expression not in {"mixed", "unclear"}:
                continue
            selected.append({
                "registration_id": str(row["registration_id"]),
                "source_id": str(row["item_ref"]),
            })
            cold_start_ids.add(str(row["cold_start_id"]))
        if len(selected) != expected_count:
            raise StateTransitionError(
                f"active mixed breakdown set changed before launch: expected {expected_count}, found {len(selected)}"
            )
        if len(cold_start_ids) != 1:
            raise StateTransitionError("active mixed breakdown replacement must belong to one cold start")
        cold_start_id = next(iter(cold_start_ids))
        active = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_breakdown_backlog_task "
            "WHERE cold_start_id=? AND data_identity=? AND status IN ('queued', 'running') LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if active is not None:
            raise StateTransitionError("this cold start already has an active formal breakdown task")
        task_id = _id("competitor_breakdown_backlog")
        now = _now()
        summary = {
            "mode": "replace_active_mixed",
            "source_count": len(selected),
            "completed": 0,
            "failed": 0,
            "pending": len(selected),
            "failed_source_ids": [],
            "failure_reasons": {},
            "failure_details": {},
            "automatic_retry": False,
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_backlog_task("
                "backlog_task_id, cold_start_id, status, source_snapshot_json, approval_json, summary_json, "
                "data_identity, created_at, updated_at, completed_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, NULL)",
                (
                    task_id,
                    cold_start_id,
                    _canonical(selected),
                    _canonical(approval),
                    _canonical(summary),
                    self.data_identity,
                    now,
                    now,
                ),
            )
            result = self.get_competitor_breakdown_backlog_task(backlog_task_id=task_id)
            self._receipt(
                "create_active_mixed_breakdown_replacement_task",
                f"{conversation_ref}:{task_id}",
                {"expected_count": expected_count, "reason": reason.strip(), "actor": actor},
                result,
            )
            self._audit(None, "active_mixed_breakdown_replacement_task_created", result)
        return result

    @staticmethod
    def _breakdown_uses_mixed_or_unclear(artifact_json: str) -> bool:
        try:
            artifact = json.loads(artifact_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        deep = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
        if not isinstance(deep, dict):
            return False
        subject = str(deep.get("content_subject_type") or "unclear")
        expression = str(deep.get("expression_form") or "unclear")
        return subject in {"mixed", "unclear"} or expression in {"mixed", "unclear"}

    def replace_completed_mixed_competitor_breakdown(
        self,
        *,
        registration_id: str,
        source_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically swap one validated candidate for its current mixed result."""
        deep = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
        if not isinstance(deep, dict) or str(artifact.get("source_id") or "") != source_id:
            raise StateTransitionError("replacement breakdown candidate is invalid")
        if (
            str(deep.get("content_subject_type") or "unclear") in {"mixed", "unclear"}
            or str(deep.get("expression_form") or "unclear") in {"mixed", "unclear"}
        ):
            raise StateTransitionError("replacement breakdown still uses mixed or unclear classification")
        prior = self.conn.execute(
            "SELECT item.artifact_json FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id AND registration.data_identity=item.data_identity "
            "JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "WHERE item.registration_id=? AND item.item_ref=? AND item.step_name='breakdown' "
            "AND item.status='completed' AND item.data_identity=? AND account.status='active'",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if prior is None or not self._breakdown_uses_mixed_or_unclear(str(prior["artifact_json"])):
            raise StateTransitionError("replacement target is no longer an active mixed breakdown")
        new_model_run_id = str(artifact.get("model_run_id") or "").strip()
        old_model_run_ids = [
            str(row["model_run_id"])
            for row in self.conn.execute(
                "SELECT DISTINCT model_run_id FROM stage0_competitor_breakdown_attempt "
                "WHERE registration_id=? AND source_id=? AND data_identity=? AND model_run_id IS NOT NULL",
                (registration_id, source_id, self.data_identity),
            ).fetchall()
            if str(row["model_run_id"]) != new_model_run_id
        ]
        now = _now()
        with self.conn:
            self.conn.execute(
                "DELETE FROM stage0_competitor_breakdown_attempt "
                "WHERE registration_id=? AND source_id=? AND data_identity=?",
                (registration_id, source_id, self.data_identity),
            )
            if old_model_run_ids:
                placeholders = ",".join("?" for _ in old_model_run_ids)
                self.conn.execute(
                    "DELETE FROM stage0_competitor_registration_model_run "
                    f"WHERE registration_model_run_id IN ({placeholders}) AND data_identity=?",
                    [*old_model_run_ids, self.data_identity],
                )
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET artifact_json=?, error_json='{}', attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? "
                "AND data_identity=? AND status='completed'",
                (_canonical(artifact), now, registration_id, source_id, self.data_identity),
            )
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_attempt VALUES (?, ?, ?, 'initial', 'completed', '', ?, "
                "'available', ?, ?, ?)",
                (
                    _id("competitor_breakdown_attempt"),
                    registration_id,
                    source_id,
                    str(artifact.get("raw_model_output") or ""),
                    new_model_run_id or None,
                    self.data_identity,
                    now,
                ),
            )
        return {"registration_id": registration_id, "source_id": source_id, "status": "completed"}

    def competitor_breakdown_backlog_task_progress(self, *, backlog_task_id: str) -> dict[str, Any]:
        task = self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        snapshot = task["source_snapshot"]
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.status, item.artifact_json "
            "FROM stage0_competitor_registration_item item "
            "WHERE item.data_identity=? AND item.step_name='breakdown'",
            (self.data_identity,),
        ).fetchall()
        states = {
            (str(row["registration_id"]), str(row["item_ref"])): (
                str(row["status"]), str(row["artifact_json"])
            )
            for row in rows
        }
        prior_summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
        mode = str(prior_summary.get("mode") or "")
        failed_source_ids = {
            str(value) for value in prior_summary.get("failed_source_ids") or [] if str(value)
        }
        completed = failed = 0
        for item in snapshot:
            source_id = str(item["source_id"])
            state = states.get((str(item["registration_id"]), source_id))
            status = state[0] if state is not None else None
            if mode == "replace_active_mixed":
                if source_id in failed_source_ids:
                    failed += 1
                elif status == "completed" and state is not None and not self._breakdown_uses_mixed_or_unclear(state[1]):
                    completed += 1
            elif status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
        summary = {
            "source_count": len(snapshot),
            "completed": completed,
            "failed": failed,
            "pending": len(snapshot) - completed - failed,
        }
        if mode:
            summary.update({
                "mode": mode,
                "failed_source_ids": sorted(failed_source_ids),
                "failure_reasons": dict(prior_summary.get("failure_reasons") or {}),
                "failure_details": dict(prior_summary.get("failure_details") or {}),
                "automatic_retry": bool(prior_summary.get("automatic_retry")),
            })
        return {
            **task,
            "summary": summary,
        }

    def update_competitor_breakdown_backlog_task(
        self,
        *,
        backlog_task_id: str,
        status: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "completed", "completed_with_failures"}:
            raise StateTransitionError("formal breakdown backlog task has an unsupported status")
        self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        now = _now()
        completed_at = now if status in {"completed", "completed_with_failures"} else None
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_breakdown_backlog_task SET status=?, summary_json=?, updated_at=?, completed_at=? "
                "WHERE backlog_task_id=? AND data_identity=?",
                (status, _canonical(summary), now, completed_at, backlog_task_id, self.data_identity),
            )
        return self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)

    def replace_competitor_breakdown_batch(
        self,
        *,
        cold_start_id: str,
        approval: dict[str, str],
        reason: str,
    ) -> dict[str, Any]:
        """Delete one cold-start's old breakdown material and queue a clean replacement batch."""
        actor = str(approval.get("actor") or "").strip()
        conversation_ref = str(approval.get("conversation_ref") or "").strip()
        if not actor or not conversation_ref or not reason.strip():
            raise StateTransitionError("breakdown batch replacement needs explicit user approval and a reason")
        cold_start = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("breakdown batch replacement does not belong to this data identity")
        active = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_breakdown_backlog_task "
            "WHERE cold_start_id=? AND data_identity=? AND status IN ('queued', 'running') LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if active is not None:
            raise StateTransitionError("cannot replace a breakdown batch while another batch is running")
        materials = self.list_cold_start_breakdown_materials(cold_start_id=cold_start_id)
        if not materials:
            raise StateTransitionError("breakdown batch replacement found no retained prepared materials")
        registration_ids = sorted({str(item["registration_id"]) for item in materials})
        placeholders = ",".join("?" for _ in registration_ids)
        query_args = [*registration_ids, self.data_identity]
        snapshot = [
            {"registration_id": str(item["registration_id"]), "source_id": str(item["source_id"])}
            for item in materials
        ]
        old_attempt_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_breakdown_attempt "
            f"WHERE registration_id IN ({placeholders}) AND data_identity=?",
            query_args,
        ).fetchone()[0])
        old_item_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_item "
            f"WHERE registration_id IN ({placeholders}) AND step_name=? AND data_identity=?",
            [*registration_ids, "breakdown", self.data_identity],
        ).fetchone()[0])
        old_model_run_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_model_run "
            f"WHERE registration_id IN ({placeholders}) AND step_name=? AND data_identity=?",
            [*registration_ids, "breakdown", self.data_identity],
        ).fetchone()[0])
        old_backlog_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_breakdown_backlog_task "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()[0])
        task_id = _id("competitor_breakdown_backlog")
        now = _now()
        summary = {"source_count": len(snapshot), "completed": 0, "failed": 0, "pending": len(snapshot)}
        request = {
            "cold_start_id": cold_start_id,
            "actor": actor,
            "conversation_ref": conversation_ref,
            "reason": reason.strip(),
            "source_count": len(snapshot),
        }
        with self.conn:
            self.conn.execute(
                "DELETE FROM stage0_competitor_breakdown_attempt "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=?",
                query_args,
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_model_run "
                f"WHERE registration_id IN ({placeholders}) AND step_name=? AND data_identity=?",
                [*registration_ids, "breakdown", self.data_identity],
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_item "
                f"WHERE registration_id IN ({placeholders}) AND step_name=? AND data_identity=?",
                [*registration_ids, "breakdown", self.data_identity],
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_breakdown_backlog_task "
                "WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.data_identity),
            )
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_backlog_task("
                "backlog_task_id, cold_start_id, status, source_snapshot_json, approval_json, summary_json, "
                "data_identity, created_at, updated_at, completed_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, NULL)",
                (
                    task_id,
                    cold_start_id,
                    _canonical(snapshot),
                    _canonical({"kind": "natural_dialogue", "actor": actor, "conversation_ref": conversation_ref}),
                    _canonical(summary),
                    self.data_identity,
                    now,
                    now,
                ),
            )
            result = self.get_competitor_breakdown_backlog_task(backlog_task_id=task_id)
            result.update({
                "replacement": True,
                "source_count": len(snapshot),
                "deleted_breakdown_attempts": old_attempt_count,
                "deleted_breakdown_items": old_item_count,
                "deleted_breakdown_model_runs": old_model_run_count,
                "deleted_backlog_tasks": old_backlog_count,
                "formal_business_data_written": True,
            })
            self._receipt("replace_competitor_breakdown_batch", f"{conversation_ref}:{cold_start_id}", request, result)
            self._audit(None, "competitor_breakdown_batch_replaced", result)
        return result

    def discard_failed_breakdown_for_replacement(
        self,
        *,
        cold_start_id: str,
        registration_id: str,
        source_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, int | str]:
        """Remove one current invalid failed result before its explicitly authorized retry."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("breakdown retry requires an explicit user decision")
        request = {
            "cold_start_id": cold_start_id,
            "registration_id": registration_id,
            "source_id": source_id,
            "actor": actor,
            "reason": reason,
        }
        replay = self._replay("discard_failed_breakdown_for_replacement", idempotency_key, request)
        if replay:
            return replay
        material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.registration_id=? AND item.item_ref=? "
            "AND item.data_identity=? AND item.step_name='transcripts_and_comments' AND item.status='completed'",
            (cold_start_id, registration_id, source_id, self.data_identity),
        ).fetchone()
        failed = self.conn.execute(
            "SELECT status FROM stage0_competitor_registration_item WHERE registration_id=? AND item_ref=? "
            "AND data_identity=? AND step_name='breakdown'",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if material is None or failed is None or str(failed["status"]) != "failed":
            raise StateTransitionError("only one current failed breakdown with retained source material may be retried")
        with self.conn:
            failed_model_run_ids = [
                str(row["model_run_id"])
                for row in self.conn.execute(
                    "SELECT DISTINCT model_run_id FROM stage0_competitor_breakdown_attempt "
                    "WHERE registration_id=? AND source_id=? AND data_identity=? "
                    "AND outcome!='completed' AND model_run_id IS NOT NULL",
                    (registration_id, source_id, self.data_identity),
                ).fetchall()
            ]
            model_run_count = len(failed_model_run_ids)
            if failed_model_run_ids:
                placeholders = ",".join("?" for _ in failed_model_run_ids)
                self.conn.execute(
                    "DELETE FROM stage0_competitor_registration_model_run "
                    f"WHERE registration_model_run_id IN ({placeholders}) "
                    "AND registration_id=? AND data_identity=? AND step_name='breakdown'",
                    [*failed_model_run_ids, registration_id, self.data_identity],
                )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_item WHERE registration_id=? AND item_ref=? "
                "AND data_identity=? AND step_name='breakdown' AND status='failed'",
                (registration_id, source_id, self.data_identity),
            )
            result: dict[str, int | str] = {
                "cold_start_id": cold_start_id,
                "registration_id": registration_id,
                "source_id": source_id,
                "status": "failed_breakdown_discarded_for_replacement",
                "discarded_breakdown_records": 1,
                "discarded_breakdown_model_runs": model_run_count,
            }
            self._receipt("discard_failed_breakdown_for_replacement", idempotency_key, request, result)
            self._audit(None, "failed_breakdown_discarded_for_replacement", result)
        return result

    def record_knowledge_mirror_run(
        self,
        *,
        cold_start_id: str | None,
        mirror_root: str,
        record_count: int,
        relationship_count: int,
        actor: str,
    ) -> dict[str, Any]:
        if not mirror_root.strip() or record_count < 0 or relationship_count < 0 or not actor.strip():
            raise StateTransitionError("knowledge mirror completion requires a concrete result and actor")
        if cold_start_id:
            row = self.conn.execute(
                "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.data_identity),
            ).fetchone()
            if row is None:
                raise StateTransitionError("knowledge mirror cold start does not exist")
        run_id = _id("knowledge_mirror_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_knowledge_mirror_run VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
                (
                    run_id,
                    cold_start_id,
                    mirror_root,
                    record_count,
                    relationship_count,
                    self.data_identity,
                    actor.strip(),
                    _now(),
                ),
            )
        return {
            "mirror_run_id": run_id,
            "cold_start_id": cold_start_id,
            "status": "completed",
            "record_count": record_count,
            "relationship_count": relationship_count,
        }

    def link_manual_source(self, *, manual_source_id: str, target_kind: str, target_id: str) -> dict[str, str]:
        """Keep the same manual origin attached when its handling path changes."""
        if target_kind not in MANUAL_SOURCE_TARGET_KINDS:
            raise StateTransitionError("manual source target kind is not supported")
        if not target_id.strip():
            raise StateTransitionError("manual source link requires a target")
        source = self.conn.execute(
            "SELECT data_identity FROM stage0_manual_source WHERE manual_source_id=?", (manual_source_id,)
        ).fetchone()
        if source is None or source["data_identity"] != self.data_identity:
            raise StateTransitionError("manual source does not exist in this data identity")
        link_id = _id("manual_source_link")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source_link(
                    link_id, manual_source_id, target_kind, target_id, data_identity, linked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (link_id, manual_source_id, target_kind, target_id.strip(), self.data_identity, _now()),
            )
        return {"link_id": link_id, "manual_source_id": manual_source_id, "target_kind": target_kind, "target_id": target_id.strip()}

    def route_manual_source_to_exploration(
        self,
        *,
        manual_source_id: str,
        actor: str,
        actor_kind: str,
        scope: dict[str, Any],
    ) -> dict[str, str]:
        """Route a user-provided person, one work, or a work set without turning it into a topic by itself."""
        if actor_kind != "user" or not actor.strip() or not scope:
            raise StateTransitionError("manual exploration requires a user, a retained source and an explicit scope")
        source = self.conn.execute(
            "SELECT source_kind, data_identity FROM stage0_manual_source WHERE manual_source_id=?",
            (manual_source_id,),
        ).fetchone()
        if source is None or source["data_identity"] != self.data_identity:
            raise StateTransitionError("manual source does not exist in this data identity")
        exploration_kind_by_source = {
            "person": "person_exploration",
            "work": "work_exploration",
            "playlist": "playlist_exploration",
        }
        exploration_kind = exploration_kind_by_source.get(source["source_kind"])
        if exploration_kind is None:
            raise StateTransitionError("only a person, one work, or a work set may enter an exploration route")
        exploration_id = _id("manual_exploration")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration VALUES (?, ?, ?, ?, 'awaiting_material_collection', ?, ?, ?)",
                (exploration_id, manual_source_id, exploration_kind, _canonical(scope), self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'awaiting_material_collection', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, "user source retained and routed before any formal topic is created", self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_manual_source_link VALUES (?, ?, ?, ?, ?, ?)",
                (_id("manual_source_link"), manual_source_id, exploration_kind, exploration_id, self.data_identity, now),
            )
            self._audit(None, "manual_source_routed_to_exploration", {"manual_source_id": manual_source_id, "exploration_id": exploration_id, "exploration_kind": exploration_kind})
        return {"manual_source_id": manual_source_id, "exploration_id": exploration_id, "exploration_kind": exploration_kind, "status": "awaiting_material_collection"}

    def begin_manual_exploration_collection(self, *, exploration_id: str, actor: str, reason: str) -> dict[str, str]:
        """Open the one selected person/work/playlist exploration for controlled material collection."""
        exploration = self.conn.execute(
            "SELECT * FROM stage0_manual_exploration WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        if exploration is None or not actor.strip() or not reason.strip():
            raise StateTransitionError("manual exploration collection requires a retained exploration, actor and reason")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if current is None or current["status"] != "awaiting_material_collection":
            raise StateTransitionError("manual exploration is not awaiting collection")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'collecting', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, reason.strip(), self.data_identity, _now()),
            )
        return {"exploration_id": exploration_id, "exploration_kind": str(exploration["exploration_kind"]), "status": "collecting"}

    def record_manual_exploration_material(
        self,
        *,
        exploration_id: str,
        material_ref: dict[str, Any],
        evidence_role: str,
        collected_at: str,
    ) -> dict[str, str]:
        """Retain one external material reference without replacing the user's origin source."""
        if evidence_role not in {"fact_evidence", "professional_interpretation", "audience_perception", "video_material", "research_clue"}:
            raise StateTransitionError("manual exploration material has an unsupported evidence role")
        if not material_ref or not str(material_ref.get("source_ref") or material_ref.get("url") or "").strip() or not collected_at.strip():
            raise StateTransitionError("manual exploration material needs a traceable collected source reference and time")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if current is None or current["status"] != "collecting":
            raise StateTransitionError("manual exploration materials may only be retained while collecting")
        material_id = _id("manual_exploration_material")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_material VALUES (?, ?, ?, ?, ?, ?)",
                (material_id, exploration_id, _canonical(material_ref), evidence_role, self.data_identity, collected_at),
            )
        return {"exploration_material_id": material_id, "exploration_id": exploration_id, "evidence_role": evidence_role}

    def record_music_audience_echo(
        self, *, exploration_id: str, materials: tuple[dict[str, Any], ...], collected_at: str
    ) -> dict[str, Any]:
        exploration = self.conn.execute(
            "SELECT exploration_kind FROM stage0_manual_exploration WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if exploration is None or exploration["exploration_kind"] != "person_exploration" or current is None or current["status"] != "collecting":
            raise StateTransitionError("music audience materials require an active person exploration")
        retained = 0
        with self.conn:
            for material in materials:
                platform = str(material.get("platform") or "")
                subject_kind = str(material.get("subject_kind") or "")
                material_kind = str(material.get("material_kind") or "")
                text = str(material.get("text") or "").strip()
                useful = int(material.get("useful_count") or 0)
                source_url = str(material.get("source_url") or "").strip()
                if (
                    platform not in {"netease_music", "douban"}
                    or subject_kind not in {"song", "album"}
                    or material_kind not in {"comment", "short_review", "long_review"}
                    or useful < 1 or len(text) < 5 or not source_url.startswith(("http://", "https://"))
                ):
                    raise StateTransitionError("music audience material violates its retained-source boundary")
                echo_id = _id("music_audience_echo")
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO stage0_music_audience_echo VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        echo_id, exploration_id, platform, str(material.get("work_title") or "").strip(),
                        subject_kind, material_kind, text, useful, source_url,
                        int(material.get("viewed_position") or 0), self.data_identity, collected_at,
                    ),
                )
                if cursor.rowcount:
                    retained += 1
                    material_ref = {
                        "source_ref": f"music_audience_echo:{echo_id}", "url": source_url,
                        "platform": platform, "work_title": str(material.get("work_title") or "").strip(),
                        "subject_kind": subject_kind, "material_kind": material_kind,
                    }
                    self.conn.execute(
                        "INSERT INTO stage0_manual_exploration_material VALUES (?, ?, ?, 'audience_perception', ?, ?)",
                        (_id("manual_exploration_material"), exploration_id, _canonical(material_ref), self.data_identity, collected_at),
                    )
        return {"exploration_id": exploration_id, "retained_count": retained, "evidence_role": "audience_perception"}

    def complete_manual_exploration_collection(self, *, exploration_id: str, actor: str, reason: str) -> dict[str, str]:
        """Finish collection and stop for a user topic or direction decision; it never creates a formal topic."""
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("manual exploration completion requires an actor and reason")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        material_count = self.conn.execute(
            "SELECT COUNT(*) FROM stage0_manual_exploration_material WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()[0]
        if current is None or current["status"] != "collecting" or int(material_count) < 1:
            raise StateTransitionError("manual exploration needs collected material before it can await a user direction")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'awaiting_human_direction', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, reason.strip(), self.data_identity, _now()),
            )
        return {"exploration_id": exploration_id, "status": "awaiting_human_direction", "material_count": str(material_count)}

    def complete_manual_exploration_direction(
        self,
        *,
        exploration_id: str,
        formal_topic_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, str]:
        """Close an exploration only after the user-selected direction became a formal topic."""
        if not formal_topic_id.strip() or not actor.strip() or not reason.strip():
            raise StateTransitionError(
                "manual exploration completion requires the formal topic, user and reason"
            )
        exploration = self.conn.execute(
            "SELECT manual_source_id FROM stage0_manual_exploration "
            "WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state "
            "WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        linked = (
            self.conn.execute(
                "SELECT 1 FROM stage0_manual_source_link "
                "WHERE manual_source_id=? AND target_kind='formal_topic' "
                "AND target_id=? AND data_identity=?",
                (
                    exploration["manual_source_id"],
                    formal_topic_id.strip(),
                    self.data_identity,
                ),
            ).fetchone()
            if exploration is not None
            else None
        )
        if (
            exploration is None
            or current is None
            or current["status"] != "awaiting_human_direction"
            or linked is None
        ):
            raise StateTransitionError(
                "manual exploration may complete only after its retained source is linked to the user-confirmed formal topic"
            )
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'completed', ?, ?, ?)",
                (
                    _id("manual_exploration_state"),
                    exploration_id,
                    reason.strip(),
                    self.data_identity,
                    now,
                ),
            )
        return {
            "exploration_id": exploration_id,
            "formal_topic_id": formal_topic_id.strip(),
            "status": "completed",
        }

    def get_manual_exploration_packet(self, *, exploration_id: str) -> dict[str, Any]:
        """Return the retained manual origin and collected materials for the required human direction decision."""
        exploration = self.conn.execute(
            "SELECT exploration.*, source.original_content_json, source.source_kind, "
            "source.domain_label, source.account_ref, source.submitted_by "
            "FROM stage0_manual_exploration exploration "
            "JOIN stage0_manual_source source ON source.manual_source_id=exploration.manual_source_id "
            "WHERE exploration.exploration_id=? AND exploration.data_identity=? AND source.data_identity=?",
            (exploration_id, self.data_identity, self.data_identity),
        ).fetchone()
        if exploration is None:
            raise StateTransitionError("manual exploration does not exist in this data identity")
        state = self.conn.execute(
            "SELECT status, reason, effective_at FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        materials = self.conn.execute(
            "SELECT material_ref_json, evidence_role, collected_at FROM stage0_manual_exploration_material "
            "WHERE exploration_id=? AND data_identity=? ORDER BY collected_at, exploration_material_id",
            (exploration_id, self.data_identity),
        ).fetchall()
        return {
            "exploration_id": exploration_id,
            "manual_source_id": exploration["manual_source_id"],
            "exploration_kind": exploration["exploration_kind"],
            "scope": json.loads(exploration["scope_json"]),
            "manual_origin": {"source_kind": exploration["source_kind"], "original_content": json.loads(exploration["original_content_json"])},
            "domain_label": exploration["domain_label"],
            "account_ref": exploration["account_ref"],
            "submitted_by": exploration["submitted_by"],
            "status": state["status"],
            "status_reason": state["reason"],
            "materials": [
                {"material": json.loads(row["material_ref_json"]), "evidence_role": row["evidence_role"], "collected_at": row["collected_at"]}
                for row in materials
            ],
            "user_direction_required": state["status"] == "awaiting_human_direction",
        }

    def create_direct_formal_topic(
        self,
        *,
        domain_label: str,
        account_ref: str,
        core_question: str,
        scope_or_requirement: str,
        original_instruction: Any,
        actor: str,
        actor_kind: str,
        existing_manual_source_ids: tuple[str, ...] = (),
        known_materials: list[dict[str, Any]] | None = None,
        material_gaps: list[str] | None = None,
        timeliness: dict[str, Any] | None = None,
        risks: list[str] | None = None,
        related_topic_refs: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Record an explicit user-issued formal topic: it bypasses candidates and is already topic-confirmed."""
        domain_label = self._require_configured_domain(domain_label, context="direct formal topic")
        if actor_kind != "user":
            raise StateTransitionError("direct formal topic requires a user and a configured domain")
        if not account_ref.strip() or len(core_question.strip()) < 6 or not scope_or_requirement.strip() or original_instruction is None:
            raise StateTransitionError("direct formal topic requires account, core question, scope and the original user instruction")
        self._require_owned_account_for_domain(domain_label=domain_label, account_ref=account_ref.strip())
        request = {
            "domain_label": domain_label, "account_ref": account_ref.strip(), "core_question": core_question.strip(),
            "scope_or_requirement": scope_or_requirement.strip(), "original_instruction": original_instruction,
            "existing_manual_source_ids": list(existing_manual_source_ids), "known_materials": known_materials or [],
            "material_gaps": material_gaps or [], "timeliness": timeliness or {}, "risks": risks or [],
            "related_topic_refs": related_topic_refs or [], "actor": actor,
        }
        replay = self._replay("create_direct_formal_topic", idempotency_key, request)
        if replay:
            return replay
        for manual_source_id in existing_manual_source_ids:
            source = self.conn.execute(
                "SELECT data_identity, domain_label FROM stage0_manual_source WHERE manual_source_id=?",
                (manual_source_id,),
            ).fetchone()
            if source is None or source["data_identity"] != self.data_identity:
                raise StateTransitionError("an attached manual source does not exist in this data identity")
            if source["domain_label"] != domain_label:
                raise StateTransitionError("an attached manual source belongs to another domain")
        task_id, topic_version_id, manual_source_id = _id("task"), _id("version"), _id("manual_direction")
        now = _now()
        instruction_source = {
            "instruction": original_instruction,
            "core_question": core_question.strip(),
            "scope_or_requirement": scope_or_requirement.strip(),
        }
        topic_payload = {
            "title": core_question.strip(), "core_question": core_question.strip(), "domain": domain_label,
            "account_ref": account_ref.strip(), "scope_or_requirement": scope_or_requirement.strip(),
            "topic_origin": "direct_user_instruction", "known_materials": known_materials or [],
            "material_gaps": material_gaps or [], "timeliness": timeliness or {}, "risks": risks or [],
            "related_topic_refs": related_topic_refs or [],
            "source_refs": [{"kind": "manual_source", "manual_source_id": manual_source_id}],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_source VALUES (?, ?, ?, 'direction', ?, ?, ?, 'user_origin_not_fact', ?, ?)",
                (manual_source_id, domain_label, account_ref.strip(), _canonical(instruction_source), actor.strip(), now, _hash({"source_kind": "direction", "original_content": instruction_source, "account_ref": account_ref.strip(), "evidence_role": "user_origin_not_fact"}), self.data_identity),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, 'research_plan', NULL, 'not_started', 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, 'formal_topic', NULL, NULL, NULL, 'approved', 'topic_directly_confirmed', 'passed', ?, ?, ?, 0)",
                (topic_version_id, task_id, self.data_identity, actor.strip(), now),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor.strip(), actor_kind, "direct user instruction is the formal-topic confirmation")
            self.conn.execute(
                "INSERT INTO stage0_manual_source_link VALUES (?, ?, 'formal_topic', ?, ?, ?)",
                (_id("manual_source_link"), manual_source_id, task_id, self.data_identity, now),
            )
            for attached_source_id in existing_manual_source_ids:
                self.conn.execute(
                    "INSERT INTO stage0_manual_source_link VALUES (?, ?, 'formal_topic', ?, ?, ?)",
                    (_id("manual_source_link"), attached_source_id, task_id, self.data_identity, now),
                )
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "manual_source_id": manual_source_id, "current_node": "research_plan"}
            self._receipt("create_direct_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "direct_formal_topic_confirmed_and_research_plan_queued", result)
        return result

    def record_research_material(
        self,
        *,
        task_id: str,
        source_ref: str,
        title: str,
        evidence_role: str,
        material: dict[str, Any],
        collected_at: str,
    ) -> dict[str, str]:
        """Retain a traceable external research source before the deep-research model step may use it."""
        task = self._task(task_id)
        if task["current_node"] != "deep_research" or task["current_status"] not in {"not_started", "processing"}:
            raise StateTransitionError("research material may only be retained for the active deep-research step")
        if evidence_role not in {"fact_evidence", "professional_interpretation", "audience_perception", "research_clue"}:
            raise StateTransitionError("research material has an unsupported evidence role")
        normalized_source = source_ref.strip().lower()
        if any(
            host in normalized_source
            for host in (
                "douyin.com",
                "tiktok.com",
                "bilibili.com",
                "xiaohongshu.com",
                "kuaishou.com",
            )
        ):
            raise StateTransitionError(
                "video-platform links cannot be retained as formal research evidence"
            )
        if not normalized_source.startswith(("https://", "http://")) or not title.strip() or not material or not collected_at.strip():
            raise StateTransitionError("research material requires a traceable external source, title, retained material and collection time")
        material_id = _id("research_material")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_research_material VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (material_id, task_id, source_ref.strip(), title.strip(), evidence_role, _canonical(material), _hash(material), self.data_identity, collected_at),
            )
        return {"research_material_id": material_id, "task_id": task_id, "source_ref": source_ref.strip()}

    def list_research_materials(self, *, task_id: str) -> list[dict[str, Any]]:
        self._task(task_id)
        rows = self.conn.execute(
            "SELECT research_material_id, source_ref, title, evidence_role, material_json, integrity_hash, collected_at "
            "FROM stage0_research_material WHERE task_id=? AND data_identity=? ORDER BY collected_at, research_material_id",
            (task_id, self.data_identity),
        ).fetchall()
        return [
            {"research_material_id": row["research_material_id"], "source_ref": row["source_ref"], "title": row["title"], "evidence_role": row["evidence_role"], "material": json.loads(row["material_json"]), "integrity_hash": row["integrity_hash"], "collected_at": row["collected_at"]}
            for row in rows
        ]

    def queue_research_plan_generation(
        self,
        *,
        task_id: str,
        actor: str,
        prompt_version: str,
        skill_version: str,
        model_config_version: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Start the configured research-plan model step from an already confirmed topic; it never starts research itself."""
        task = self._task(task_id)
        self._assert_current_node(task, "research_plan", "not_started")
        topic_version_id = str(task["topic_version_id"])
        topic = self.get_artifact_payload(topic_version_id)["payload"]
        user_requirements = _canonical(
            {
                "core_question": topic.get("core_question"), "scope_or_requirement": topic.get("scope_or_requirement"),
                "material_gaps": topic.get("material_gaps", []), "risks": topic.get("risks", []),
                "timeliness": topic.get("timeliness", {}),
            }
        )
        assembly = InputAssembly(
            task_id=task_id, node="research_plan", upstream_version_id=topic_version_id,
            user_requirements=user_requirements,
            material_refs=tuple(topic.get("source_refs", [])) + tuple(topic.get("known_materials", [])),
            research_refs=(), content_plan_ref=None, considered_experience=(), adopted_experience=(), rejected_experience=(), omitted_materials=(),
            prompt_version=prompt_version, skill_version=skill_version, model_config_version=model_config_version,
        )
        assembly_result = self.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        request_result = self.create_node_request(
            task_id=task_id, node="research_plan", input_assembly_id=assembly_result["assembly_id"], actor=actor,
            idempotency_key=f"{idempotency_key}:request",
        )
        return {"task_id": task_id, "assembly_id": assembly_result["assembly_id"], "node_version_id": request_result["node_version_id"], "status": "research_plan_generating"}

    def submit_voice_profile(
        self,
        *,
        voice_profile_id: str,
        profile_label: str,
        reference_audio_ref: str,
        emotion_reference_audio_ref: str | None,
        mode: str,
        prompt_text: str,
        emotion_prompt_text: str,
        settings: dict[str, Any],
        actor: str,
        actor_kind: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Retain a proposed voice configuration; it is unusable until a user confirms it."""
        if actor_kind != "user" or not actor.strip() or not voice_profile_id.strip() or not profile_label.strip():
            raise StateTransitionError("voice profile submission requires an explicit user identity and label")
        if not reference_audio_ref.strip() or mode not in {"basic", "ultimate"}:
            raise StateTransitionError("voice profile requires a reference audio and supported mode")
        if mode == "ultimate" and not (emotion_prompt_text or prompt_text).strip():
            raise StateTransitionError("ultimate voice mode requires the matching reference transcript")
        allowed_settings = {
            "style_prompt", "emotion_preset", "emotion_strength", "segment_strategy", "max_chars",
            "silence_ms", "cfg_value", "inference_timesteps",
        }
        if set(settings) != allowed_settings:
            raise StateTransitionError("voice profile settings do not match the controlled production contract")
        if settings["segment_strategy"] not in {"auto", "segment"}:
            raise StateTransitionError("formal voice profiles may not force one unsegmented long generation")
        request = {
            "voice_profile_id": voice_profile_id, "profile_label": profile_label,
            "reference_audio_ref": reference_audio_ref,
            "emotion_reference_audio_ref": emotion_reference_audio_ref, "mode": mode,
            "prompt_text": prompt_text, "emotion_prompt_text": emotion_prompt_text, "settings": settings,
            "actor": actor,
        }
        replay = self._replay("submit_voice_profile", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_voice_profile VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_human_confirmation', ?, ?, ?, NULL, NULL)",
                (
                    voice_profile_id.strip(), profile_label.strip(), reference_audio_ref.strip(),
                    str(emotion_reference_audio_ref or "").strip() or None, mode, prompt_text.strip(),
                    emotion_prompt_text.strip(), _canonical(settings), self.data_identity, actor.strip(), now,
                ),
            )
            result = {"voice_profile_id": voice_profile_id.strip(), "status": "awaiting_human_confirmation"}
            self._receipt("submit_voice_profile", idempotency_key, request, result)
            self._audit(None, "voice_profile_awaiting_human_confirmation", result)
        return result

    def confirm_voice_profile(
        self, *, voice_profile_id: str, actor: str, actor_kind: str, idempotency_key: str
    ) -> dict[str, str]:
        if actor_kind != "user" or not actor.strip():
            raise StateTransitionError("voice profile activation requires an explicit user decision")
        profile = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE voice_profile_id=? AND data_identity=?",
            (voice_profile_id, self.data_identity),
        ).fetchone()
        if profile is None or profile["status"] != "awaiting_human_confirmation":
            raise StateTransitionError("voice profile is not awaiting confirmation")
        request = {"voice_profile_id": voice_profile_id, "actor": actor}
        replay = self._replay("confirm_voice_profile", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_voice_profile SET status='active', confirmed_by=?, confirmed_at=? WHERE voice_profile_id=?",
                (actor.strip(), now, voice_profile_id),
            )
            result = {"voice_profile_id": voice_profile_id, "status": "active"}
            self._receipt("confirm_voice_profile", idempotency_key, request, result)
            self._audit(None, "voice_profile_activated_by_user", result)
        return result

    def get_voice_profile(self, *, voice_profile_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE voice_profile_id=? AND data_identity=?",
            (voice_profile_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("voice profile does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        result["settings"] = json.loads(result.pop("settings_json"))
        return result

    def list_voice_profiles(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE data_identity=? "
            "ORDER BY submitted_at DESC, voice_profile_id",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["settings"] = json.loads(str(item.pop("settings_json")))
            results.append(item)
        return results

    def begin_audio_production(
        self,
        *,
        task_id: str,
        approved_content_version_id: str,
        voice_profile_id: str,
        title: str,
        script_text: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        task, version = self._task(task_id), self._version(approved_content_version_id)
        if (
            task["current_node"] != "user_final_confirmation" or task["current_status"] != "approved"
            or task["current_version_id"] != approved_content_version_id or version["node"] != "review"
        ):
            raise StateTransitionError("audio production requires the currently user-approved final review version")
        approval = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE task_id=? AND version_id=? AND decision='approved' AND actor_kind='user' AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()
        profile = self.get_voice_profile(voice_profile_id=voice_profile_id)
        if approval is None or profile["status"] != "active" or not title.strip() or not script_text.strip() or not actor.strip():
            raise StateTransitionError("audio production requires approved content, an active voice profile, title, script and actor")
        request = {
            "task_id": task_id, "approved_content_version_id": approved_content_version_id,
            "voice_profile_id": voice_profile_id, "title": title, "script_text_hash": _hash(script_text.strip()),
            "actor": actor,
        }
        replay = self._replay("begin_audio_production", idempotency_key, request)
        if replay:
            return replay
        attempt = int(self.conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) FROM stage0_audio_production WHERE task_id=? AND approved_content_version_id=? AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()[0]) + 1
        audio_production_id, now = _id("audio_production"), _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_production VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', NULL, NULL, '{}', '{}', '{}', NULL, ?, ?, ?, ?)",
                (
                    audio_production_id, task_id, approved_content_version_id, voice_profile_id, attempt,
                    title.strip(), _hash(script_text.strip()), self.data_identity, actor.strip(), now, now,
                ),
            )
            result = {"audio_production_id": audio_production_id, "status": "processing", "attempt_number": str(attempt)}
            self._receipt("begin_audio_production", idempotency_key, request, result)
            self._audit(task_id, "audio_production_started", result)
        return result

    def complete_audio_production(
        self,
        *,
        audio_production_id: str,
        audio_ref: str,
        metadata_ref: str,
        synthesis: dict[str, Any],
        quality_review: dict[str, Any],
        actor: str,
    ) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "processing":
            raise StateTransitionError("audio production is not processing")
        if not audio_ref.strip() or not metadata_ref.strip() or not actor.strip():
            raise StateTransitionError("completed audio production requires formal audio, metadata and actor")
        if quality_review.get("technical_status") != "passed" or quality_review.get("asr_status") != "passed":
            raise StateTransitionError("audio may reach human review only after technical and ASR checks run successfully")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_audio_production SET status='awaiting_human_review', audio_ref=?, metadata_ref=?, synthesis_json=?, quality_review_json=?, updated_at=? WHERE audio_production_id=?",
                (audio_ref.strip(), metadata_ref.strip(), _canonical(synthesis), _canonical(quality_review), now, audio_production_id),
            )
            self._audit(production["task_id"], "audio_awaiting_human_review", {
                "audio_production_id": audio_production_id,
                "text_consistency_status": quality_review.get("text_consistency_status"),
            })
        return {"audio_production_id": audio_production_id, "status": "awaiting_human_review"}

    def fail_audio_production(self, *, audio_production_id: str, error: dict[str, Any], actor: str) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "processing" or not error or not actor.strip():
            raise StateTransitionError("only a processing audio attempt may record a formal failure")
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_audio_production SET status='failed', failure_json=?, updated_at=? WHERE audio_production_id=?",
                (_canonical(error), _now(), audio_production_id),
            )
            self._audit(production["task_id"], "audio_production_failed", {"audio_production_id": audio_production_id})
        return {"audio_production_id": audio_production_id, "status": "failed"}

    def review_audio_production(
        self,
        *,
        audio_production_id: str,
        decision: str,
        issue_scope: str,
        reason: str,
        actor: str,
        actor_kind: str,
    ) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "awaiting_human_review":
            raise StateTransitionError("audio production is not awaiting human review")
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("audio review requires an explicit user decision and reason")
        if decision == "approved" and issue_scope != "none":
            raise StateTransitionError("approved audio cannot carry a return scope")
        if decision == "returned" and issue_scope not in {"audio_regeneration", "content_revision"}:
            raise StateTransitionError("returned audio must identify audio or content revision scope")
        if decision not in {"approved", "returned"}:
            raise StateTransitionError("unsupported audio review decision")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_decision VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?)",
                (_id("audio_decision"), audio_production_id, decision, issue_scope, reason.strip(), actor.strip(), self.data_identity, now),
            )
            if decision == "approved":
                delivery_id = _id("audio_delivery")
                self.conn.execute(
                    "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                    (
                        delivery_id, production["task_id"], production["approved_content_version_id"],
                        production["audio_ref"], self.data_identity, actor.strip(), now,
                    ),
                )
                self.conn.execute(
                    "UPDATE stage0_audio_production SET status='approved', audio_delivery_id=?, updated_at=? WHERE audio_production_id=?",
                    (delivery_id, now, audio_production_id),
                )
                result = {"audio_production_id": audio_production_id, "status": "approved", "audio_delivery_id": delivery_id}
                self._audit(production["task_id"], "audio_delivered_text_and_audio_only", result)
            else:
                self.conn.execute(
                    "UPDATE stage0_audio_production SET status='returned', updated_at=? WHERE audio_production_id=?",
                    (now, audio_production_id),
                )
                if issue_scope == "content_revision":
                    version = self._version(production["approved_content_version_id"])
                    if version["node"] != "review" or not version["upstream_version_id"]:
                        raise StateTransitionError("audio content issue cannot locate the approved review upstream")
                    task = self._task(production["task_id"])
                    self._set_task(
                        production["task_id"], node="review", version_id=version["upstream_version_id"],
                        status="not_started", revision=int(task["task_revision"]) + 1,
                    )
                result = {"audio_production_id": audio_production_id, "status": "returned", "issue_scope": issue_scope}
                self._audit(production["task_id"], "audio_returned_by_user", result)
        return result

    def get_audio_production(self, *, audio_production_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("audio production does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        for key in ("synthesis_json", "quality_review_json", "failure_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result

    def propose_human_decision_carrier(
        self,
        *,
        carrier_binding_id: str,
        carrier_kind: str,
        entry_ref: str,
        context_strategy: str,
        actor: str,
    ) -> dict[str, str]:
        """Retain a carrier proposal without treating an untested transport as operational."""
        if not all(str(value).strip() for value in (carrier_binding_id, carrier_kind, entry_ref, context_strategy, actor)):
            raise StateTransitionError("human decision carrier proposal requires complete identity and context information")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_human_decision_carrier_binding VALUES (?, ?, ?, ?, 'proposed', '{}', ?, ?, ?, NULL, NULL)",
                (
                    carrier_binding_id.strip(), carrier_kind.strip(), entry_ref.strip(), context_strategy.strip(),
                    self.data_identity, actor.strip(), now,
                ),
            )
            self._audit(None, "human_decision_carrier_proposed", {
                "carrier_binding_id": carrier_binding_id.strip(), "carrier_kind": carrier_kind.strip(),
            })
        return {"carrier_binding_id": carrier_binding_id.strip(), "status": "proposed"}

    def validate_human_decision_carrier(
        self,
        *,
        carrier_binding_id: str,
        validation_evidence: dict[str, Any],
        actor: str,
        actor_kind: str,
    ) -> dict[str, str]:
        """Activate a transport only after a user accepts real round-trip and identity evidence."""
        required = {
            "inbound_round_trip", "outbound_round_trip", "same_context_verified",
            "decision_identity_verified", "evidence_ref",
        }
        if set(validation_evidence) != required or any(
            validation_evidence[key] is not True for key in required - {"evidence_ref"}
        ) or not str(validation_evidence["evidence_ref"]).strip():
            raise StateTransitionError("human decision carrier validation requires complete real round-trip evidence")
        if actor_kind != "user" or not actor.strip():
            raise StateTransitionError("human decision carrier validation requires an explicit user decision")
        binding = self.conn.execute(
            "SELECT * FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
            (carrier_binding_id, self.data_identity),
        ).fetchone()
        if binding is None or binding["status"] != "proposed":
            raise StateTransitionError("human decision carrier is not awaiting validation")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_human_decision_carrier_binding SET status='validated', validation_evidence_json=?, validated_by=?, validated_at=? WHERE carrier_binding_id=?",
                (_canonical(validation_evidence), actor.strip(), now, carrier_binding_id),
            )
            self._audit(None, "human_decision_carrier_validated_by_user", {"carrier_binding_id": carrier_binding_id})
        return {"carrier_binding_id": carrier_binding_id, "status": "validated"}

    def list_validated_human_decision_carriers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_human_decision_carrier_binding WHERE status='validated' AND data_identity=? ORDER BY validated_at, carrier_binding_id",
            (self.data_identity,),
        ).fetchall()
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["validation_evidence"] = json.loads(item.pop("validation_evidence_json"))
            result.append(item)
        return result

    def retire_human_decision_carrier(
        self,
        *,
        carrier_binding_id: str,
        reason: str,
        actor: str,
    ) -> dict[str, str]:
        """Retire a removed transport without leaving it as an active carrier."""
        if not all(str(value).strip() for value in (carrier_binding_id, reason, actor)):
            raise StateTransitionError("retiring a human decision carrier requires an identity, reason and actor")
        binding = self.conn.execute(
            "SELECT * FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
            (carrier_binding_id.strip(), self.data_identity),
        ).fetchone()
        if binding is None:
            raise StateTransitionError("human decision carrier does not exist in this data identity")
        if binding["status"] == "retired":
            return {"carrier_binding_id": carrier_binding_id.strip(), "status": "retired"}
        try:
            evidence = json.loads(binding["validation_evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
        evidence.update({"retired_reason": reason.strip(), "retired_at": _now()})
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_human_decision_carrier_binding SET status='retired', validation_evidence_json=?, validated_by=?, validated_at=? WHERE carrier_binding_id=? AND data_identity=?",
                (_canonical(evidence), actor.strip(), now, carrier_binding_id.strip(), self.data_identity),
            )
            self._audit(None, "human_decision_carrier_retired", {
                "carrier_binding_id": carrier_binding_id.strip(),
                "reason": reason.strip(),
            })
        return {"carrier_binding_id": carrier_binding_id.strip(), "status": "retired"}

    def receive_human_decision_command(
        self,
        *,
        command_id: str,
        carrier_binding_id: str,
        session_ref: str,
        action: str,
        target_ref: str,
        payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        trusted_internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if actor_kind != "user" or not all(str(value).strip() for value in (command_id, session_ref, action, target_ref, actor)):
            raise StateTransitionError("formal human command requires a user, session, action and target")
        binding = self.conn.execute(
            "SELECT status, carrier_kind, entry_ref FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
            (carrier_binding_id, self.data_identity),
        ).fetchone()
        if binding is None:
            raise StateTransitionError("formal human command requires a registered carrier binding")
        existing = self.conn.execute(
            "SELECT * FROM stage0_human_decision_command WHERE command_id=? AND data_identity=?",
            (command_id, self.data_identity),
        ).fetchone()
        canonical_payload = _canonical(payload)
        if existing is not None:
            if (
                existing["carrier_binding_id"] != carrier_binding_id or existing["session_ref"] != session_ref
                or existing["action"] != action or existing["target_ref"] != target_ref
                or existing["payload_json"] != canonical_payload or existing["actor"] != actor
            ):
                raise StateTransitionError("human command id was replayed with different content")
            return {key: existing[key] for key in existing.keys()}
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_human_decision_command VALUES (?, ?, ?, ?, ?, ?, ?, 'user', 'received', '{}', '{}', ?, ?, NULL)",
                (
                    command_id.strip(), carrier_binding_id, session_ref.strip(), action.strip(), target_ref.strip(),
                    canonical_payload, actor.strip(), self.data_identity, now,
                ),
            )
            self._audit(None, "formal_human_decision_command_received", {
                "command_id": command_id.strip(), "action": action.strip(), "target_ref": target_ref.strip(),
            })
        return {"command_id": command_id.strip(), "status": "received"}

    def finish_human_decision_command(
        self, *, command_id: str, status: str, result: dict[str, Any], error: dict[str, Any]
    ) -> dict[str, Any]:
        command = self.conn.execute(
            "SELECT * FROM stage0_human_decision_command WHERE command_id=? AND data_identity=?",
            (command_id, self.data_identity),
        ).fetchone()
        if command is None or command["status"] != "received" or status not in {"completed", "rejected"}:
            raise StateTransitionError("human decision command is not open or has an unsupported completion status")
        if status == "completed" and (not result or error):
            raise StateTransitionError("completed human decision command requires a result and no error")
        if status == "rejected" and (not error or result):
            raise StateTransitionError("rejected human decision command requires an error and no result")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_human_decision_command SET status=?, result_json=?, error_json=?, completed_at=? WHERE command_id=?",
                (status, _canonical(result), _canonical(error), now, command_id),
            )
            self._audit(None, "formal_human_decision_command_finished", {"command_id": command_id, "status": status})
        return {"command_id": command_id, "status": status, "result": result, "error": error}

    def record_audio_delivery(
        self,
        *,
        task_id: str,
        approved_content_version_id: str,
        audio_ref: str,
        approved_audio_production_id: str,
        actor: str,
    ) -> dict[str, str]:
        """Compatibility boundary: only a user-approved controlled production may become a delivery."""
        version = self._version(approved_content_version_id)
        if version["task_id"] != task_id or version["node"] not in {"formal_draft", "copy_optimization", "de_ai_revision", "review"} or not audio_ref.strip() or not actor.strip():
            raise StateTransitionError("audio delivery requires a task-owned approved content version and a formal audio reference")
        approval = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE task_id=? AND version_id=? AND decision='approved' AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()
        if approval is None:
            raise StateTransitionError("audio delivery can only follow an explicitly approved content version")
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (approved_audio_production_id, self.data_identity),
        ).fetchone()
        if (
            production is None or production["status"] != "approved"
            or production["task_id"] != task_id
            or production["approved_content_version_id"] != approved_content_version_id
            or production["audio_ref"] != audio_ref.strip()
        ):
            raise StateTransitionError("formal audio delivery cannot bypass controlled production and user audio review")
        if production["audio_delivery_id"]:
            return {"audio_delivery_id": production["audio_delivery_id"], "status": "delivered"}
        audio_delivery_id = _id("audio_delivery")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                (audio_delivery_id, task_id, approved_content_version_id, audio_ref.strip(), self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "UPDATE stage0_audio_production SET audio_delivery_id=?, updated_at=? WHERE audio_production_id=?",
                (audio_delivery_id, now, approved_audio_production_id),
            )
            self._audit(task_id, "audio_delivered_text_and_audio_only", {"audio_delivery_id": audio_delivery_id})
        return {"audio_delivery_id": audio_delivery_id, "status": "delivered"}

    def record_discovery_source(
        self,
        *,
        run_id: str,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        expires_at: str | None = None,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        if run["status"] != "processing":
            raise StateTransitionError("discovery sources may only be recorded while a run is processing")
        self._assert_discovery_source_provenance(
            domain_label=domain_label,
            source_type=source_type,
            source_object_id=source_object_id,
            source_object_version=source_object_version,
            source_time=source_time,
            payload=payload,
        )
        request = {
            "run_id": run_id, "domain_label": domain_label, "source_type": source_type,
            "source_object_id": source_object_id, "source_object_version": source_object_version,
            "source_time": source_time, "expires_at": expires_at, "payload": payload,
        }
        replay = self._replay("stage1b_record_discovery_source", idempotency_key, request)
        if replay:
            return replay
        source_version_id = _id("discovery_source")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_source_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_version_id, run_id, domain_label, source_type, source_object_id, source_object_version,
                 source_time, expires_at, _canonical(payload), _hash(payload), self.data_identity, _now()),
            )
            result = {"source_version_id": source_version_id, "input_integrity_hash": _hash(payload)}
            self._receipt("stage1b_record_discovery_source", idempotency_key, request, result)
            self._audit(run_id, "stage1b_source_recorded", {**result, "source_type": source_type})
        return result

    def record_discovery_filter(
        self,
        *,
        source_version_id: str,
        outcome: str,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        source = self._discovery_source(source_version_id)
        if outcome not in {"eligible", "excluded"}:
            raise StateTransitionError("discovery filter outcome is invalid")
        request = {"source_version_id": source_version_id, "outcome": outcome, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_discovery_filter", idempotency_key, request)
        if replay:
            return replay
        result_id = _id("discovery_filter")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_filter_result VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, source_version_id, outcome, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            result = {"filter_result_id": result_id, "source_version_id": source_version_id, "outcome": outcome}
            self._receipt("stage1b_record_discovery_filter", idempotency_key, request, result)
            self._audit(source["run_id"], "stage1b_source_filtered", {**result, "reason_code": reason_code})
        return result

    def create_discovery_input_assembly(
        self,
        *,
        run_id: str,
        source_version_id: str,
        payload: dict[str, Any],
        prompt_version: str,
        skill_version: str,
        idempotency_key: str,
        model_route: ModelRoute | None = None,
        external_execution: bool = False,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("discovery input must belong to an active run source")
        filter_row = self.conn.execute("SELECT outcome FROM stage1b_filter_result WHERE source_version_id=?", (source_version_id,)).fetchone()
        if filter_row is None or filter_row["outcome"] != "eligible":
            raise StateTransitionError("LLM input may only be assembled for deterministically eligible sources")
        if self.data_identity == "production" and not external_execution:
            raise StateTransitionError(
                "formal discovery input must use the external intelligence boundary"
            )
        if external_execution and model_route is not None:
            raise StateTransitionError("external intelligence assembly cannot contain a model route")
        route = None if external_execution else (model_route or self._resolve_discovery_model_route())
        stored_payload = dict(payload)
        if external_execution:
            stored_payload["execution_boundary"] = "external_intelligence"
            model_config_version = EXTERNAL_INTELLIGENCE_EXECUTION_VERSION
            model_config_hash = _hash({"execution_boundary": "external_intelligence", "version": model_config_version})
        else:
            assert route is not None
            stored_payload["model_binding"] = {
                "route_id": route.route_id,
                "provider_name": route.provider_name,
                "provider_ref": route.provider_ref,
                "model_name": route.model_name,
                "config_version": route.config_version,
                "config_hash": route.config_hash,
            }
            model_config_version = route.config_version
            model_config_hash = route.config_hash
        request = {"run_id": run_id, "source_version_id": source_version_id, "payload": stored_payload, "prompt_version": prompt_version, "skill_version": skill_version, "model_config_version": model_config_version, "external_execution": external_execution}
        replay = self._replay("stage1b_create_discovery_input", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("discovery_assembly"), _hash(stored_payload)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, run_id, source_version_id, _canonical(stored_payload), integrity_hash, prompt_version, skill_version, model_config_version, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash, "model_config_hash": model_config_hash}
            self._receipt("stage1b_create_discovery_input", idempotency_key, request, result)
            self._audit(run_id, "stage1b_input_assembly_created", result)
        return result

    def prepare_discovery_external_task(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        skill: dict[str, Any],
        input_payload: dict[str, Any],
        constraints: dict[str, Any],
        output_requirements: dict[str, Any],
    ) -> dict[str, Any]:
        """Prepare one external intelligent task from the current Core facts.

        This reuses the processing discovery run and input assembly.  It does
        not create a task queue or a second lifecycle, and it deliberately
        carries no model or provider choice.
        """
        run, source, assembly = (
            self._discovery_run(run_id),
            self._discovery_source(source_version_id),
            self._discovery_assembly(assembly_id),
        )
        filter_row = self.conn.execute(
            "SELECT outcome FROM stage1b_filter_result WHERE source_version_id=?",
            (source_version_id,),
        ).fetchone()
        if (
            run["status"] != "processing"
            or source["run_id"] != run_id
            or assembly["run_id"] != run_id
            or assembly["source_version_id"] != source_version_id
            or filter_row is None
            or filter_row["outcome"] != "eligible"
        ):
            raise StateTransitionError("external intelligence task has stale or ineligible input")
        stored_payload = json.loads(str(assembly["payload_json"]))
        if stored_payload.get("execution_boundary") != "external_intelligence":
            raise StateTransitionError("external intelligence task requires an external execution assembly")
        if not isinstance(skill, dict) or not str(skill.get("formal_skill_id") or "").strip():
            raise StateTransitionError("external intelligence task requires a formal Skill")
        if not isinstance(input_payload, dict) or not isinstance(constraints, dict) or not isinstance(output_requirements, dict):
            raise StateTransitionError("external intelligence task payload is malformed")
        source_to_topic_material_matches = (
            str(input_payload.get("fixture_id") or "") == str(stored_payload.get("request_id") or "")
            and str(input_payload.get("domain_label") or "") == str(stored_payload.get("domain_label") or "")
            and list(input_payload.get("source_evidence_refs") or [])
            == list(stored_payload.get("source_evidence_items") or [])
            and " ".join(str(input_payload.get("source_content") or "").split())
            == " ".join(str(stored_payload.get("source_content") or "").split())
        )
        generic_material_matches = dict(input_payload) == {
            key: value
            for key, value in stored_payload.items()
            if key != "execution_boundary"
        }
        if not (source_to_topic_material_matches or generic_material_matches):
            raise StateTransitionError("external intelligence task material does not match the Core assembly")
        context = self._discovery_context(run_id)
        return {
            "task_type": str(skill["formal_skill_id"]),
            "business_context": {
                "daily_run_id": str(context["daily_run_id"] or "") or None,
                "discovery_run_id": run_id,
                "source_version_id": source_version_id,
                "input_assembly_id": assembly_id,
                "data_identity": self.data_identity,
            },
            "skill": dict(skill),
            "input": dict(input_payload),
            "constraints": dict(constraints),
            "output_requirements": dict(output_requirements),
            "source_identity": {
                "source_version_id": source_version_id,
                "source_type": str(source["source_type"]),
                "source_object_id": str(source["source_object_id"]),
                "source_object_version": str(source["source_object_version"]),
            },
        }

    def load_discovery_external_assembly_payload(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
    ) -> dict[str, Any]:
        """Load one Core-owned external assembly for a transport adapter.

        The transport receives only the stable Core identifiers.  It cannot
        supply or replace the material that the Core assembled for the task.
        This is a read of the existing input assembly, not a second task
        record or a second lifecycle.
        """
        run, source, assembly = (
            self._discovery_run(run_id),
            self._discovery_source(source_version_id),
            self._discovery_assembly(assembly_id),
        )
        if (
            run["status"] != "processing"
            or source["run_id"] != run_id
            or assembly["run_id"] != run_id
            or assembly["source_version_id"] != source_version_id
        ):
            raise StateTransitionError("external assembly is stale or mismatched")
        payload = json.loads(str(assembly["payload_json"]))
        if not isinstance(payload, dict) or payload.get("execution_boundary") != "external_intelligence":
            raise StateTransitionError("external assembly is not an external intelligence input")
        payload.pop("execution_boundary", None)
        return payload

    def get_discovery_external_execution_result(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
    ) -> dict[str, Any]:
        """Read the Core result for one external execution without changing it."""
        run, source, assembly = (
            self._discovery_run(run_id),
            self._discovery_source(source_version_id),
            self._discovery_assembly(assembly_id),
        )
        if (
            source["run_id"] != run_id
            or assembly["run_id"] != run_id
            or assembly["source_version_id"] != source_version_id
        ):
            raise StateTransitionError("external execution identifiers do not belong together")
        row = self.conn.execute(
            "SELECT model_run_id, status, request_id, provider_ref, model_name, "
            "validation_status, error_json, via_model_gateway, created_at "
            "FROM stage1b_model_run WHERE run_id=? AND source_version_id=? "
            "AND input_assembly_id=?",
            (run_id, source_version_id, assembly_id),
        ).fetchone()
        candidates = self.conn.execute(
            "SELECT COUNT(*) AS count FROM stage1b_candidate_version "
            "WHERE run_id=? AND source_version_id=?",
            (run_id, source_version_id),
        ).fetchone()
        return {
            "run_id": run_id,
            "source_version_id": source_version_id,
            "assembly_id": assembly_id,
            "discovery_run_status": str(run["status"]),
            "result": None if row is None else {
                "model_run_id": str(row["model_run_id"]),
                "status": str(row["status"]),
                "execution_id": str(row["request_id"]),
                "executor_id": str(row["provider_ref"]),
                "model_ref": str(row["model_name"]),
                "validation_status": str(row["validation_status"]),
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "via_model_gateway": bool(row["via_model_gateway"]),
                "created_at": str(row["created_at"]),
            },
            "candidate_count": int(candidates["count"]),
        }

    def record_discovery_external_execution(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output_payload: dict[str, Any],
    ) -> str:
        """Record an externally executed result as an auditable fact."""
        run, source, assembly = (
            self._discovery_run(run_id),
            self._discovery_source(source_version_id),
            self._discovery_assembly(assembly_id),
        )
        execution_id = str(execution_id or "").strip()
        executor_id = str(executor_id or "").strip()
        if not execution_id or not executor_id:
            raise StateTransitionError("external result requires execution and executor identity")
        if not isinstance(output_payload, dict):
            raise StateTransitionError("external result must be structured fields")
        if (
            run["status"] != "processing"
            or source["run_id"] != run_id
            or assembly["run_id"] != run_id
            or assembly["source_version_id"] != source_version_id
        ):
            raise StateTransitionError("external result has stale or mismatched input")
        stored_payload = json.loads(str(assembly["payload_json"]))
        if stored_payload.get("execution_boundary") != "external_intelligence":
            raise StateTransitionError("external result requires an external execution assembly")
        if self.conn.execute(
            "SELECT 1 FROM stage1b_model_run WHERE run_id=? AND source_version_id=? AND input_assembly_id=?",
            (run_id, source_version_id, assembly_id),
        ).fetchone():
            raise StateTransitionError("this intelligent input already has an execution result")
        model_run_id = _id("discovery_external_execution")
        model_ref = str(model_ref or "not_reported").strip() or "not_reported"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id,
                    run_id,
                    source_version_id,
                    assembly_id,
                    "succeeded",
                    execution_id,
                    str(assembly["prompt_version"]),
                    str(assembly["skill_version"]),
                    None,
                    EXTERNAL_INTELLIGENCE_EXECUTION_VERSION,
                    executor_id,
                    "external_executor",
                    model_ref,
                    str(assembly["integrity_hash"]),
                    _hash(output_payload),
                    "not_validated",
                    _canonical({}),
                    "not_requested",
                    _canonical({}),
                    _canonical({}),
                    0,
                    self.data_identity,
                    _now(),
                    0,
                ),
            )
            self._audit(
                run_id,
                "stage1b_external_intelligence_result_recorded",
                {
                    "model_run_id": model_run_id,
                    "execution_id": execution_id,
                    "executor_id": executor_id,
                    "model_ref": model_ref,
                    "submitted_at": submitted_at,
                },
            )
        return model_run_id

    def pending_hotspot_judgement(self, *, run_id: str) -> dict[str, Any]:
        """Return one frozen hotspot judgement that an authorized user may resubmit once."""
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        allowed_modes = {"real_daily_validation"} if self.data_identity == "production" else {"test_isolated"}
        if run["status"] != "processing" or context["execution_mode"] not in allowed_modes:
            raise StateTransitionError("only a processing real daily hotspot run can resubmit its judgement")
        rows = self.conn.execute(
            "SELECT source.source_version_id, source.domain_label, source.payload_json, "
            "assembly.assembly_id, assembly.payload_json AS assembly_payload_json "
            "FROM stage1b_source_version source "
            "JOIN stage1b_input_assembly assembly ON assembly.source_version_id=source.source_version_id "
            "WHERE source.run_id=? AND source.source_type='hotspot' "
            "ORDER BY source.created_at, source.source_version_id",
            (run_id,),
        ).fetchall()
        if len(rows) != 1:
            raise StateTransitionError("resubmission requires exactly one frozen hotspot judgement input")
        row = rows[0]
        source_version_id = str(row["source_version_id"])
        for table in ("stage1b_model_run", "stage1b_candidate_version", "stage1b_candidate_absence", "stage1b_source_failure"):
            if self.conn.execute(
                f"SELECT 1 FROM {table} WHERE run_id=? AND source_version_id=?",
                (run_id, source_version_id),
            ).fetchone():
                raise StateTransitionError("a hotspot judgement with a recorded outcome cannot be resubmitted")
        input_payload = json.loads(str(row["assembly_payload_json"]))
        input_payload.pop("model_binding", None)
        return {
            "run_id": run_id,
            "source_version_id": source_version_id,
            "domain_label": str(row["domain_label"]),
            "source_payload": json.loads(str(row["payload_json"])),
            "assembly_id": str(row["assembly_id"]),
            "input_payload": input_payload,
        }

    def prepare_discovery_model_request(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        prompt: str,
        skill_name: str = "source_to_topic",
        skill_version: str | None = None,
        skill_hash: str | None = None,
        binding_name: str | None = None,
        binding_version: str | None = None,
        binding_hash: str | None = None,
        model_input_payload: dict[str, Any] | None = None,
        model_route: ModelRoute | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ModelRequest:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        assembly = self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StateTransitionError("discovery model request has stale or mismatched input")
        if self.data_identity == "production":
            raise ModelGatewayRequiredError(
                "formal discovery model requests must be submitted by an external executor"
            )
        route = model_route or self._resolve_discovery_model_route()
        payload = json.loads(assembly["payload_json"])
        expected_binding = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        if payload.get("model_binding") != expected_binding or assembly["model_config_version"] != route.config_version:
            raise StaleResultError("discovery input assembly no longer matches the explicit model binding")
        binding_name = binding_name or route.route_name
        binding_version = binding_version or route.config_version
        binding_hash = binding_hash or route.config_hash
        return ModelRequest(
            route_name=route.route_name,
            prompt=prompt,
            input_payload=model_input_payload or payload,
            correlation_id=run_id,
            skill_name=skill_name,
            skill_version=skill_version or assembly["skill_version"],
            skill_hash=skill_hash,
            binding_name=binding_name,
            binding_version=binding_version,
            binding_hash=binding_hash,
            response_format=dict(response_format) if response_format else None,
            metadata={"stage1b_core": {"run_id": run_id, "source_version_id": source_version_id, "input_assembly_id": assembly_id, "data_identity": self.data_identity, "prompt_version": assembly["prompt_version"], "skill_version": skill_version or assembly["skill_version"], "expected_binding": {**expected_binding, "binding_name": binding_name, "binding_version": binding_version, "binding_hash": binding_hash}}},
        )

    def persist_discovery_model_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway discovery identity does not match Core identity")
        run_id = str(binding.get("run_id") or "")
        source_version_id = str(binding.get("source_version_id") or "")
        assembly_id = str(binding.get("input_assembly_id") or "")
        run, source, assembly = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StaleResultError("discovery ModelGateway envelope belongs to stale input")
        expected_request_binding = dict(binding.get("expected_binding") or {})
        expected_model_binding = {
            key: expected_request_binding.get(key)
            for key in (
                "route_id",
                "provider_name",
                "provider_ref",
                "model_name",
                "config_version",
                "config_hash",
            )
        }
        stored_payload = json.loads(assembly["payload_json"])
        stored_model_binding = stored_payload.get("model_binding")
        if (
            not isinstance(stored_model_binding, dict)
            or stored_model_binding != expected_model_binding
            or any(not str(value or "").strip() for value in expected_model_binding.values())
            or str(assembly["model_config_version"] or "") != str(expected_model_binding["config_version"] or "")
        ):
            raise ModelGatewayRequiredError("discovery ModelGateway request lacks the frozen explicit binding")
        expected_binding_name = str(expected_request_binding.get("binding_name") or "")
        expected_binding_version = str(expected_request_binding.get("binding_version") or "")
        expected_binding_hash = str(expected_request_binding.get("binding_hash") or "")
        if (
            envelope.route_name != "business.source_to_topic"
            or envelope.route_id != expected_model_binding["route_id"]
            or envelope.provider_name != expected_model_binding["provider_name"]
            or envelope.provider_ref != expected_model_binding["provider_ref"]
            or envelope.model_name != expected_model_binding["model_name"]
            or envelope.config_version != expected_model_binding["config_version"]
            or envelope.config_hash != expected_model_binding["config_hash"]
            or envelope.binding_name != expected_binding_name
            or envelope.binding_version != expected_binding_version
            or envelope.binding_hash != expected_binding_hash
        ):
            raise ModelGatewayRequiredError("discovery ModelGateway envelope does not match source_to_topic binding")
        model_run_id = _id("discovery_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model_run_id, run_id, source_version_id, assembly_id, envelope.status, envelope.correlation_id,
                 binding["prompt_version"], binding["skill_version"], envelope.route_id or envelope.route_name,
                 envelope.config_version, envelope.provider_ref, envelope.provider_name, envelope.model_name,
                 assembly["integrity_hash"], envelope.output_hash, "not_validated", _canonical(envelope.error or {}),
                 "not_retried", _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                 _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1),
            )
            self._audit(run_id, "stage1b_model_gateway_envelope_recorded", {"model_run_id": model_run_id, "status": envelope.status})
        return model_run_id

    def record_discovery_model_validation_failure(
        self, *, model_run_id: str, reason: str, raw_model_output: str | None = None
    ) -> None:
        model_run = self._discovery_model_run(model_run_id)
        with self.conn:
            self.conn.execute(
                "UPDATE stage1b_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?",
                (_canonical({"validation_error": reason, "raw_model_output": raw_model_output}), model_run_id),
            )
            self._audit(model_run["run_id"], "stage1b_model_output_validation_failed", {"model_run_id": model_run_id, "reason": reason})

    def record_discovery_source_failure(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str | None,
        failure_stage: str,
        reason: str,
        raw_model_output: str | None,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Leave a failed source as a failure, never as a zero-candidate result."""
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("source failure must belong to an active source run")
        if model_run_id is not None:
            model_run = self._discovery_model_run(model_run_id)
            if model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
                raise ModelGatewayRequiredError("source failure model run does not belong to the source")
        request = {
            "run_id": run_id,
            "source_version_id": source_version_id,
            "model_run_id": model_run_id,
            "failure_stage": failure_stage,
            "reason": reason,
            "raw_model_output": raw_model_output,
        }
        replay = self._replay("stage1b_record_source_failure", idempotency_key, request)
        if replay:
            return replay
        failure_id = _id("discovery_source_failure")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_source_failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    failure_id, run_id, source_version_id, model_run_id, failure_stage, reason,
                    raw_model_output,
                    "available" if raw_model_output is not None else "not_available",
                    self.data_identity, _now(),
                ),
            )
            result = {"failure_id": failure_id, "failure_stage": failure_stage}
            self._receipt("stage1b_record_source_failure", idempotency_key, request, result)
            self._audit(run_id, "stage1b_source_failed", result)
        return result

    def record_discovery_no_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str | None,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("candidate absence must belong to an active source run")
        if model_run_id is not None:
            model_run = self._discovery_model_run(model_run_id)
            if model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
                raise ModelGatewayRequiredError("candidate absence model run does not belong to the source")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_candidate_absence", idempotency_key, request)
        if replay:
            return replay
        absence_id = _id("candidate_absence")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_absence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (absence_id, run_id, source_version_id, model_run_id, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            if model_run_id is not None:
                valid_business_absences = {
                    "model_returned_no_candidate",
                    "candidate_outside_domain_policy",
                    "candidate_material_insufficient",
                }
                validation_status = "passed" if reason_code in valid_business_absences else "failed"
                self.conn.execute("UPDATE stage1b_model_run SET validation_status=? WHERE model_run_id=?", (validation_status, model_run_id))
            result = {"absence_id": absence_id, "reason_code": reason_code}
            self._receipt("stage1b_record_candidate_absence", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_absent", result)
        return result

    def create_discovery_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str,
        candidate_id: str,
        payload: dict[str, Any],
        candidate_domain_label: str | None = None,
        allow_multiple_from_model_run: bool = False,
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source, model_run = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_model_run(model_run_id)
        if run["status"] != "processing" or source["run_id"] != run_id or model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
            raise StateTransitionError("candidate does not belong to the active source run")
        if model_run["status"] != "succeeded" or (
            model_run["validation_status"] != "not_validated"
            and not (allow_multiple_from_model_run and model_run["validation_status"] == "passed")
        ):
            raise ModelGatewayRequiredError("candidate requires one successful unconsumed intelligent execution")
        forbidden_fields = {"score", "rank", "weight", "recommendation_score", "quality_rank"}
        if forbidden_fields & set(payload):
            raise StateTransitionError("discovery candidates must not contain business-ranking fields")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_absence WHERE run_id=? AND source_version_id=?", (run_id, source_version_id)).fetchone():
            raise StateTransitionError("a source recorded as zero-candidate cannot create a candidate")
        domain_label = candidate_domain_label or str(source["domain_label"])
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("candidate domain is not a formal domain")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "candidate_id": candidate_id, "payload": payload, "candidate_domain_label": domain_label, "allow_multiple_from_model_run": allow_multiple_from_model_run}
        replay = self._replay("stage1b_create_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        candidate_version_id = _id("candidate_version")
        with self.conn:
            now = _now()
            self.conn.execute(
                "INSERT INTO stage1b_candidate_version VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'awaiting_user_decision', ?, ?)",
                (candidate_version_id, candidate_id, run_id, domain_label, source_version_id, model_run_id, _canonical(payload), _hash(payload), self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage1b_candidate_support VALUES (?, ?, ?, 'initial_discovery', ?, ?, ?)",
                (_id("candidate_support"), candidate_version_id, source_version_id, "candidate first formed from this formal discovery source", self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'current', ?, 'candidate_pool_window_v1', ?, ?)",
                (_id("candidate_pool_state"), candidate_version_id, "candidate first entered the active pool", self.data_identity, now),
            )
            self.conn.execute("UPDATE stage1b_model_run SET validation_status='passed' WHERE model_run_id=?", (model_run_id,))
            result = {"candidate_version_id": candidate_version_id, "candidate_id": candidate_id}
            self._receipt("stage1b_create_discovery_candidate", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_created", result)
        return result

    def complete_discovery_run(
        self,
        *,
        run_id: str,
        domains: tuple[str, ...],
        lifecycle_status: str = "completed",
        failure_reason: str | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        if lifecycle_status not in DISCOVERY_RUN_OUTCOMES - {"processing"}:
            raise StateTransitionError("discovery lifecycle status is invalid")
        if run["status"] == "completed":
            return {"run_id": run_id, "status": context["lifecycle_status"]}
        if run["status"] != "processing":
            raise StateTransitionError("only a processing discovery run can complete")
        request = {
            "run_id": run_id,
            "domains": list(domains),
            "lifecycle_status": lifecycle_status,
            "failure_reason": failure_reason,
        }
        replay = self._replay("stage1b_complete_discovery_run", idempotency_key, request)
        if replay:
            return replay
        with self.conn:
            for domain_label in domains:
                rows = self.conn.execute(
                    "SELECT candidate.candidate_version_id FROM stage1b_candidate_version candidate "
                    "JOIN stage1b_candidate_pool_state pool ON pool.pool_state_id=(SELECT newest.pool_state_id FROM stage1b_candidate_pool_state newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.effective_at DESC, newest.pool_state_id DESC LIMIT 1) "
                    "LEFT JOIN stage1b_candidate_assessment_revision assessment ON assessment.assessment_revision_id=(SELECT newest.assessment_revision_id FROM stage1b_candidate_assessment_revision newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.assessed_at DESC, newest.assessment_revision_id DESC LIMIT 1) "
                    "WHERE candidate.domain_label=? AND candidate.data_identity=? AND candidate.status='awaiting_user_decision' AND pool.pool_status='current' "
                    "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision decision WHERE decision.candidate_version_id=candidate.candidate_version_id) "
                    "ORDER BY assessment.total_score IS NULL, assessment.total_score DESC, candidate.created_at, candidate.candidate_version_id LIMIT ?",
                    (
                        self.data_identity,
                        self.data_identity,
                        domain_label,
                        self.data_identity,
                        DAILY_PRIORITY_REPORT_LIMIT,
                    ),
                ).fetchall()
                if rows:
                    for position, row in enumerate(rows, start=1):
                        snapshot_id = _id("snapshot")
                        self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)", (snapshot_id, run_id, domain_label, row["candidate_version_id"], position, self.data_identity, _now()))
                else:
                    self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, NULL, 0, ?, ?)", (_id("snapshot"), run_id, domain_label, self.data_identity, _now()))
            core_status = "completed" if lifecycle_status in {"completed", "completed_with_failures"} else "failed"
            completed_at = _now()
            self.conn.execute(
                "UPDATE stage1b_discovery_run SET status=?, completed_at=?, failure_reason=? WHERE run_id=?",
                (core_status, completed_at, failure_reason, run_id),
            )
            self.conn.execute(
                "UPDATE stage1b_run_execution_context SET lifecycle_status=? WHERE run_id=?",
                (lifecycle_status, run_id),
            )
            result = {"run_id": run_id, "status": lifecycle_status, "execution_mode": context["execution_mode"]}
            self._receipt("stage1b_complete_discovery_run", idempotency_key, request, result)
            self._audit(run_id, "stage1b_daily_snapshot_finalized", {**result, "failure_reason": failure_reason})
        return result

    def record_candidate_assessment(
        self,
        *,
        candidate_version_id: str,
        dimension_scores: dict[str, int | float],
        dimension_reasons: dict[str, str],
        assessed_by: str,
    ) -> dict[str, Any]:
        """Persist the six visible candidate dimensions; ranking is computed, never invented by a model field."""
        candidate = self._discovery_candidate(candidate_version_id)
        if candidate["status"] != "awaiting_user_decision" or not assessed_by.strip():
            raise StateTransitionError("only an undecided candidate may receive a scored assessment")
        if self._candidate_pool_state(candidate_version_id)["pool_status"] != "current":
            raise StateTransitionError("only a current-pool candidate may be scored; reactivate it with new same-angle material first")
        if set(dimension_scores) != set(CANDIDATE_SCORE_WEIGHTS) or set(dimension_reasons) != set(CANDIDATE_SCORE_WEIGHTS):
            raise StateTransitionError("candidate assessment requires all six agreed dimensions and reasons")
        normalized_scores: dict[str, float] = {}
        for key, value in dimension_scores.items():
            score = float(value)
            if score < 0 or score > 5:
                raise StateTransitionError("candidate dimension scores must stay between 0 and 5")
            normalized_scores[key] = score
            if not str(dimension_reasons[key]).strip():
                raise StateTransitionError("candidate assessment requires a reason for every dimension")
        total = sum(normalized_scores[key] * CANDIDATE_SCORE_WEIGHTS[key] / 5 for key in CANDIDATE_SCORE_WEIGHTS)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_assessment_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_id("candidate_assessment"), candidate_version_id, _canonical(normalized_scores), _canonical(dimension_reasons), total, assessed_by.strip(), self.data_identity, _now()),
            )
        return {"candidate_version_id": candidate_version_id, "total_score": total, "dimension_scores": normalized_scores}

    def record_candidate_relation(
        self,
        *,
        candidate_version_id: str,
        related_candidate_version_id: str,
        relation_kind: str,
        relation_reason: str,
    ) -> dict[str, str]:
        """Keep same-topic material together without silently deleting a different valid angle."""
        if relation_kind not in {"same_topic_same_angle", "same_topic_different_angle"} or not relation_reason.strip():
            raise StateTransitionError("candidate relation requires an agreed kind and reason")
        self._discovery_candidate(candidate_version_id)
        self._discovery_candidate(related_candidate_version_id)
        with self.conn:
            relation_id = _id("candidate_relation")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_relation VALUES (?, ?, ?, ?, ?, ?, ?)",
                (relation_id, candidate_version_id, related_candidate_version_id, relation_kind, relation_reason.strip(), self.data_identity, _now()),
            )
        return {"relation_id": relation_id, "candidate_version_id": candidate_version_id, "related_candidate_version_id": related_candidate_version_id, "relation_kind": relation_kind}

    def record_candidate_support(
        self,
        *,
        candidate_version_id: str,
        source_version_id: str,
        relation_reason: str,
    ) -> dict[str, str]:
        """Attach same-topic/same-angle material to the existing candidate and reactivate it when needed."""
        candidate = self._discovery_candidate(candidate_version_id)
        source = self._discovery_source(source_version_id)
        if candidate["status"] != "awaiting_user_decision" or candidate["domain_label"] != source["domain_label"]:
            raise StateTransitionError("candidate support must target an undecided candidate in the same domain")
        if not relation_reason.strip():
            raise StateTransitionError("candidate support requires a traceable same-angle reason")
        previous_state = self._candidate_pool_state(candidate_version_id)
        with self.conn:
            support_id = _id("candidate_support")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_support VALUES (?, ?, ?, 'same_topic_same_angle_material', ?, ?, ?)",
                (support_id, candidate_version_id, source_version_id, relation_reason.strip(), self.data_identity, _now()),
            )
            if previous_state["pool_status"] == "historical":
                now = _now()
                previous_effective_at = datetime.fromisoformat(str(previous_state["effective_at"]).replace("Z", "+00:00"))
                current_effective_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if current_effective_at <= previous_effective_at:
                    now = (previous_effective_at + timedelta(microseconds=1)).isoformat()
                self.conn.execute(
                    "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'current', ?, ?, ?, ?)",
                    (_id("candidate_pool_state"), candidate_version_id, "new same-topic same-angle source reactivated historical candidate", self._candidate_pool_parameter()["parameter_version"], self.data_identity, now),
                )
            self._audit(candidate["run_id"], "stage1b_candidate_support_recorded", {"candidate_version_id": candidate_version_id, "source_version_id": source_version_id, "support_id": support_id})
        return {"support_id": support_id, "candidate_version_id": candidate_version_id, "source_version_id": source_version_id, "pool_status": "current"}

    def count_candidate_support_materials(self, *, candidate_version_id: str) -> int:
        """Return retained same-angle materials so a new source can trigger a visible reassessment."""
        self._discovery_candidate(candidate_version_id)
        row = self.conn.execute(
            "SELECT COUNT(*) AS material_count FROM stage1b_candidate_support "
            "WHERE candidate_version_id=? AND data_identity=?",
            (candidate_version_id, self.data_identity),
        ).fetchone()
        return int(row["material_count"])

    def set_candidate_pool_observation_window(
        self,
        *,
        observation_window_days: int,
        actor: str,
        actor_kind: str,
        reason: str,
        effective_at: str,
    ) -> dict[str, Any]:
        """Version a human-approved future pool window without rewriting historical state."""
        if actor_kind != "user" or not actor.strip() or not reason.strip() or observation_window_days <= 0:
            raise StateTransitionError("candidate pool window requires an explicit user decision, positive days and reason")
        parameter_version = _id("candidate_pool_window")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_pool_parameter_version VALUES (?, ?, ?, ?, ?, ?)",
                (parameter_version, observation_window_days, actor.strip(), reason.strip(), self.data_identity, effective_at),
            )
        return {"parameter_version": parameter_version, "observation_window_days": observation_window_days, "effective_at": effective_at}

    def move_expired_candidates_to_history(self, *, as_of: str) -> list[dict[str, Any]]:
        """Apply the confirmed two-week pool rule; it preserves all evidence and only removes expired items from ranking."""
        parameter = self._candidate_pool_parameter(as_of)
        cutoff = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=int(parameter["observation_window_days"]))).isoformat()
        rows = self.conn.execute(
            "SELECT candidate.candidate_version_id, candidate.run_id FROM stage1b_candidate_version candidate "
            "JOIN stage1b_candidate_pool_state state ON state.pool_state_id=(SELECT newest.pool_state_id FROM stage1b_candidate_pool_state newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.effective_at DESC, newest.pool_state_id DESC LIMIT 1) "
            "WHERE candidate.data_identity=? AND candidate.status='awaiting_user_decision' AND state.pool_status='current' "
            "AND candidate.created_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_daily_snapshot snapshot WHERE snapshot.candidate_version_id=candidate.candidate_version_id AND snapshot.display_position>0 AND snapshot.created_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_support support WHERE support.candidate_version_id=candidate.candidate_version_id AND support.created_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_support support JOIN stage1b_source_version source ON source.source_version_id=support.source_version_id WHERE support.candidate_version_id=candidate.candidate_version_id AND source.expires_at IS NOT NULL AND source.expires_at>?)",
            (self.data_identity, self.data_identity, cutoff, cutoff, cutoff, as_of),
        ).fetchall()
        moved: list[dict[str, Any]] = []
        with self.conn:
            for row in rows:
                state_id = _id("candidate_pool_state")
                self.conn.execute(
                    "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'historical', ?, ?, ?, ?)",
                    (state_id, row["candidate_version_id"], "outside report top 10 with no new support and no valid time window during observation period", parameter["parameter_version"], self.data_identity, as_of),
                )
                result = {"candidate_version_id": row["candidate_version_id"], "pool_status": "historical", "parameter_version": parameter["parameter_version"]}
                self._audit(row["run_id"], "stage1b_candidate_moved_to_history", result)
                moved.append(result)
        return moved

    def get_discovery_snapshot(self, *, run_id: str, domain_label: str) -> list[dict[str, Any]]:
        self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        rows = self.conn.execute(
            "SELECT snapshot.display_position, candidate.candidate_version_id, candidate.candidate_id, candidate.payload_json, source.source_type, source.source_time, source.expires_at, source.payload_json AS source_payload_json, assessment.total_score, assessment.dimension_scores_json, assessment.dimension_reasons_json FROM stage1b_daily_snapshot snapshot LEFT JOIN stage1b_candidate_version candidate ON candidate.candidate_version_id=snapshot.candidate_version_id LEFT JOIN stage1b_source_version source ON source.source_version_id=candidate.source_version_id LEFT JOIN stage1b_candidate_assessment_revision assessment ON assessment.assessment_revision_id=(SELECT newest.assessment_revision_id FROM stage1b_candidate_assessment_revision newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.assessed_at DESC, newest.assessment_revision_id DESC LIMIT 1) WHERE snapshot.run_id=? AND snapshot.domain_label=? ORDER BY snapshot.display_position, snapshot.snapshot_id",
            (self.data_identity, run_id, domain_label),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["candidate_version_id"] is None:
                continue
            candidate_payload = json.loads(row["payload_json"])
            source_payload = json.loads(row["source_payload_json"])
            support_rows = self.conn.execute(
                "SELECT support.support_id, support.support_kind, support.relation_reason, support.created_at, source.source_version_id, source.source_type, source.source_time, source.expires_at, source.payload_json FROM stage1b_candidate_support support JOIN stage1b_source_version source ON source.source_version_id=support.source_version_id WHERE support.candidate_version_id=? AND support.data_identity=? ORDER BY support.created_at, support.support_id",
                (row["candidate_version_id"], self.data_identity),
            ).fetchall()
            support_materials = [
                {
                    "support_id": support["support_id"],
                    "kind": support["support_kind"],
                    "reason": support["relation_reason"],
                    "added_at": support["created_at"],
                    "source_version_id": support["source_version_id"],
                    "source_type": support["source_type"],
                    "source_time": support["source_time"],
                    "expires_at": support["expires_at"],
                    "source": json.loads(support["payload_json"]),
                }
                for support in support_rows
            ]
            relation_rows = self.conn.execute(
                "SELECT related_candidate_version_id, relation_kind, relation_reason FROM stage1b_candidate_relation WHERE candidate_version_id=? AND data_identity=? ORDER BY created_at, relation_id",
                (row["candidate_version_id"], self.data_identity),
            ).fetchall()
            source_reference = candidate_payload.get("source_reference")
            if not isinstance(source_reference, dict):
                source_reference = {
                    "source_version_id": None,
                    "source_type": row["source_type"],
                    "source_time": row["source_time"],
                }
            stage1a_handoff_packet = {
                "candidate_version_id": row["candidate_version_id"],
                "candidate_id": row["candidate_id"],
                "source_type": row["source_type"],
                "core_question": candidate_payload.get("core_question"),
                "domain_label": domain_label,
                "recommendation_reason": candidate_payload.get("why_attention") or candidate_payload.get("new_angle"),
                "source_evidence_refs": [
                    source_reference,
                    source_payload.get("formal_source", {}),
                ],
                "risk_limits": candidate_payload.get("risk_limits"),
                "material_gap": candidate_payload.get("material_readiness"),
                "timeliness_limits": row["expires_at"] or "not_time_limited",
                "user_confirmation_required": True,
            }
            result.append({"display_position": row["display_position"], "candidate_version_id": row["candidate_version_id"], "candidate_id": row["candidate_id"], "candidate": candidate_payload, "score": {"total": row["total_score"], "dimensions": json.loads(row["dimension_scores_json"]) if row["dimension_scores_json"] else None, "reasons": json.loads(row["dimension_reasons_json"]) if row["dimension_reasons_json"] else None}, "source_type": row["source_type"], "source_time": row["source_time"], "expires_at": row["expires_at"], "source": source_payload, "support_materials": support_materials, "related_candidates": [{"candidate_version_id": relation["related_candidate_version_id"], "kind": relation["relation_kind"], "reason": relation["relation_reason"]} for relation in relation_rows], "stage1a_handoff_packet": stage1a_handoff_packet, "execution_mode": context["execution_mode"], "lifecycle_status": context["lifecycle_status"], "formal_candidate_pool": context["execution_mode"] == "production_daily" and context["lifecycle_status"] == "completed"})
        return result

    def record_discovery_decision(
        self,
        *,
        candidate_version_id: str,
        decision: str,
        actor: str,
        reason: str,
        formal_topic_task_id: str | None,
        idempotency_key: str,
    ) -> dict[str, str]:
        candidate = self._discovery_candidate(candidate_version_id)
        if decision not in {"selected", "deferred", "rejected", "angle_change_requested", "evergreen"}:
            raise StateTransitionError("unsupported discovery decision")
        if decision == "selected":
            raise StateTransitionError("selected candidates must use select_discovery_candidate")
        if not reason.strip():
            raise StateTransitionError("a user decision requires a reason")
        if formal_topic_task_id is not None:
            raise StateTransitionError("only the atomic selection action may create a formal-topic link")
        if candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("candidate is not awaiting a user decision")
        request = {"candidate_version_id": candidate_version_id, "decision": decision, "actor": actor, "reason": reason, "formal_topic_task_id": formal_topic_task_id}
        replay = self._replay("stage1b_record_discovery_decision", idempotency_key, request)
        if replay:
            return replay
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        with self.conn:
            decision_id = _id("candidate_decision")
            self.conn.execute("INSERT INTO stage1b_candidate_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (decision_id, candidate_version_id, decision, actor, reason, formal_topic_task_id, self.data_identity, _now()))
            result = {"decision_id": decision_id, "candidate_version_id": candidate_version_id, "decision": decision}
            self._receipt("stage1b_record_discovery_decision", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_user_decision", result)
        return result

    def select_discovery_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Atomically hand one exact Stage 1B candidate to Stage 1A awaiting confirmation."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("candidate selection requires an explicit user and reason")
        candidate = self._discovery_candidate(candidate_version_id)
        run = self._discovery_run(candidate["run_id"])
        context = self._discovery_context(candidate["run_id"])
        if context["execution_mode"] != "production_daily" or context["lifecycle_status"] not in {"completed", "completed_with_failures"}:
            raise StateTransitionError("only a completed production_daily candidate may enter Stage 1A")
        if run["status"] != "completed" or candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("only a completed awaiting-user-decision candidate may be selected")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        payload = json.loads(candidate["payload_json"])
        domain_label = str(candidate["domain_label"])
        now = _now()
        if self.formal_topic_title_seen(domain_label=domain_label, normalized_title=str(payload.get("normalized_title") or "")):
            raise StateTransitionError("a matching formal topic already exists in this domain")
        source_ref = payload.get("source_reference")
        if not isinstance(source_ref, dict) or source_ref.get("source_version_id") != candidate["source_version_id"]:
            raise StateTransitionError("candidate source reference is not the exact candidate source version")
        support_refs = [
            {"kind": "candidate_support", "source_version_id": row["source_version_id"], "support_kind": row["support_kind"], "reason": row["relation_reason"]}
            for row in self.conn.execute(
                "SELECT source_version_id, support_kind, relation_reason FROM stage1b_candidate_support WHERE candidate_version_id=? AND data_identity=? ORDER BY created_at, support_id",
                (candidate_version_id, self.data_identity),
            ).fetchall()
        ]
        request = {"candidate_version_id": candidate_version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("stage1b_select_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id, decision_id = _id("task"), _id("version"), _id("candidate_decision")
        topic_payload = {
            "title": payload["title"],
            "core_question": payload["core_question"],
            "domain": domain_label,
            "source_refs": [source_ref, *support_refs, {"kind": "daily_candidate", "candidate_version_id": candidate_version_id}],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, 'research_plan', NULL, 'not_started', 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, 'formal_topic', NULL, NULL, NULL, 'approved', 'topic_selected_from_candidate', 'passed', ?, ?, ?, 0)",
                (topic_version_id, task_id, self.data_identity, actor, now),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, "candidate selection is the formal-topic confirmation")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_decision VALUES (?, ?, 'selected', ?, ?, ?, ?, ?)",
                (decision_id, candidate_version_id, actor, reason, task_id, self.data_identity, now),
            )
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "decision_id": decision_id, "candidate_version_id": candidate_version_id}
            self._receipt("stage1b_select_discovery_candidate", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_selected_and_research_plan_queued", result)
            self._audit(task_id, "formal_topic_confirmed_from_stage1b_candidate", result)
        return result

    def latest_stage1_production_handoff(self) -> dict[str, str]:
        """Return the latest completed production-daily candidate handoff for Stage 1 evidence."""
        row = self.conn.execute(
            "SELECT run.run_id, candidate.candidate_version_id, decision.decision_id, decision.formal_topic_task_id "
            "FROM stage1b_candidate_decision decision "
            "JOIN stage1b_candidate_version candidate ON candidate.candidate_version_id=decision.candidate_version_id "
            "JOIN stage1b_discovery_run run ON run.run_id=candidate.run_id "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "WHERE decision.decision='selected' AND decision.formal_topic_task_id IS NOT NULL "
            "AND run.status='completed' AND context.execution_mode='production_daily' "
            "AND context.lifecycle_status IN ('completed', 'completed_with_failures') "
            "ORDER BY decision.created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise StateTransitionError(
                "Stage 1 closure requires a completed production_daily candidate selected by the user and handed off to Stage 1A"
            )
        return {
            "run_id": row["run_id"],
            "candidate_version_id": row["candidate_version_id"],
            "decision_id": row["decision_id"],
            "formal_topic_task_id": row["formal_topic_task_id"],
        }

    def discovery_run_diagnostics(self, *, run_id: str) -> dict[str, Any]:
        """Return the persisted outcome of one discovery run without changing it."""
        run = self.conn.execute(
            "SELECT run.run_id, run.status, run.failure_reason, context.execution_mode, context.lifecycle_status "
            "FROM stage1b_discovery_run run "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "WHERE run.run_id=? AND run.data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("discovery run was not found in this data identity")
        candidates = self.conn.execute(
            "SELECT candidate.candidate_version_id, candidate.domain_label, source.source_type, candidate.payload_json "
            "FROM stage1b_candidate_version candidate "
            "JOIN stage1b_source_version source ON source.source_version_id=candidate.source_version_id "
            "WHERE candidate.run_id=? ORDER BY candidate.created_at, candidate.candidate_version_id",
            (run_id,),
        ).fetchall()
        absences = self.conn.execute(
            "SELECT source.source_type, source.source_object_id, absence.reason_code, absence.detail_json "
            "FROM stage1b_candidate_absence absence "
            "JOIN stage1b_source_version source ON source.source_version_id=absence.source_version_id "
            "WHERE absence.run_id=? ORDER BY absence.created_at, absence.absence_id",
            (run_id,),
        ).fetchall()
        failures = self.conn.execute(
            "SELECT source.source_type, source.source_object_id, failure.failure_stage, failure.reason, "
            "failure.raw_model_output, failure.raw_model_output_status "
            "FROM stage1b_source_failure failure "
            "JOIN stage1b_source_version source ON source.source_version_id=failure.source_version_id "
            "WHERE failure.run_id=? ORDER BY failure.created_at, failure.failure_id",
            (run_id,),
        ).fetchall()
        source_ledger = self.conn.execute(
            "SELECT source.source_version_id, source.source_type, source.source_object_id, source.source_object_version, "
            "source.source_time, source.payload_json, filter.outcome AS filter_outcome, filter.reason_code AS filter_reason, "
            "filter.detail_json AS filter_detail, model.model_run_id, model.status AS model_status, "
            "model.validation_status AS model_validation_status, model.error_json AS model_error, "
            "assembly.payload_json AS assembly_payload, assembly.integrity_hash AS assembly_integrity_hash, "
            "candidate.candidate_version_id, candidate.payload_json AS candidate_payload, "
            "absence.reason_code AS absence_reason, absence.detail_json AS absence_detail, "
            "failure.failure_stage, failure.reason AS failure_reason, failure.raw_model_output, "
            "failure.raw_model_output_status "
            "FROM stage1b_source_version source "
            "LEFT JOIN stage1b_filter_result filter ON filter.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_input_assembly assembly ON assembly.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_model_run model ON model.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_candidate_version candidate ON candidate.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_candidate_absence absence ON absence.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_source_failure failure ON failure.source_version_id=source.source_version_id "
            "WHERE source.run_id=? ORDER BY source.created_at, source.source_version_id",
            (run_id,),
        ).fetchall()
        searched_topics = self.conn.execute(
            "SELECT tag.tag, tag.tag_id, tag.domain_label, cursor.last_searched_at "
            "FROM domain_search_cursor cursor JOIN domain_search_tags tag ON tag.tag_id=cursor.tag_id "
            "WHERE cursor.run_id=? ORDER BY tag.domain_label, tag.tag_id",
            (run_id,),
        ).fetchall()
        source_history_rows = self.conn.execute(
            "SELECT prior.source_type, prior.source_object_id, prior.source_object_version, prior.run_id, prior.created_at, "
            "context.execution_mode, context.lifecycle_status, filter.outcome, filter.reason_code "
            "FROM stage1b_source_version prior "
            "JOIN stage1b_run_execution_context context ON context.run_id=prior.run_id "
            "LEFT JOIN stage1b_filter_result filter ON filter.source_version_id=prior.source_version_id "
            "WHERE prior.data_identity=? AND prior.run_id<>? "
            "AND EXISTS (SELECT 1 FROM stage1b_source_version current "
            "WHERE current.run_id=? AND current.source_type=prior.source_type "
            "AND current.source_object_id=prior.source_object_id "
            "AND current.source_object_version=prior.source_object_version) "
            "ORDER BY prior.created_at, prior.run_id",
            (self.data_identity, run_id, run_id),
        ).fetchall()
        source_history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in source_history_rows:
            source_history.setdefault(
                (row["source_type"], row["source_object_id"], row["source_object_version"]), []
            ).append({
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "execution_mode": row["execution_mode"],
                "lifecycle_status": row["lifecycle_status"],
                "filter_outcome": row["outcome"],
                "filter_reason": row["reason_code"],
            })
        return {
            "run_id": run["run_id"],
            "execution_mode": run["execution_mode"],
            "status": run["status"],
            "lifecycle_status": run["lifecycle_status"],
            "failure_reason": run["failure_reason"],
            "searched_topics": [
                {"topic": row["tag"], "topic_id": row["tag_id"], "domain_label": row["domain_label"], "searched_at": row["last_searched_at"]}
                for row in searched_topics
            ],
            "candidates": [
                {
                    "candidate_version_id": row["candidate_version_id"],
                    "domain_label": row["domain_label"],
                    "source_type": row["source_type"],
                    "title": json.loads(row["payload_json"])["title"],
                }
                for row in candidates
            ],
            "absences": [
                {
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "reason_code": row["reason_code"],
                    "detail": json.loads(row["detail_json"]),
                }
                for row in absences
            ],
            "failures": [
                {
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "failure_stage": row["failure_stage"],
                    "reason": row["reason"],
                    "raw_model_output": row["raw_model_output"],
                    "raw_model_output_status": row["raw_model_output_status"],
                }
                for row in failures
            ],
            "source_ledger": [
                {
                    "source_version_id": row["source_version_id"],
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "source_object_version": row["source_object_version"],
                    "source_time": row["source_time"],
                    "source": json.loads(row["payload_json"]),
                    "filter": {
                        "outcome": row["filter_outcome"],
                        "reason": row["filter_reason"],
                        "detail": json.loads(row["filter_detail"]) if row["filter_detail"] else None,
                    },
                    "model_input": {
                        "payload": json.loads(row["assembly_payload"]) if row["assembly_payload"] else None,
                        "integrity_hash": row["assembly_integrity_hash"],
                    },
                    "model": {
                        "model_run_id": row["model_run_id"],
                        "status": row["model_status"],
                        "validation_status": row["model_validation_status"],
                        "error": json.loads(row["model_error"]) if row["model_error"] else None,
                        "raw_output": row["raw_model_output"],
                        "raw_output_status": row["raw_model_output_status"] or "not_available",
                    },
                    "candidate": json.loads(row["candidate_payload"]) if row["candidate_payload"] else None,
                    "absence": {
                        "reason": row["absence_reason"],
                        "detail": json.loads(row["absence_detail"]) if row["absence_detail"] else None,
                    } if row["absence_reason"] else None,
                    "failure": {
                        "stage": row["failure_stage"],
                        "reason": row["failure_reason"],
                        "raw_model_output": row["raw_model_output"],
                        "raw_model_output_status": row["raw_model_output_status"],
                    } if row["failure_stage"] else None,
                    "prior_records": source_history.get(
                        (row["source_type"], row["source_object_id"], row["source_object_version"]), []
                    ),
                }
                for row in source_ledger
            ],
        }

    def record_daily_competitor_snapshot(
        self,
        *,
        account_id: str,
        items: tuple[dict[str, Any], ...],
        raw_archive_ref: str,
        collection_run_id: str,
        business_date: str,
        daily_run_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Retain one real daily creator snapshot through the formal Core boundary."""
        normalized_daily_run_id = str(daily_run_id).strip()
        account = self.conn.execute(
            "SELECT * FROM competitor_accounts WHERE account_id=? AND registration_status='active'",
            (account_id,),
        ).fetchone()
        if account is None:
            raise StateTransitionError("daily competitor snapshot requires an active registered account")
        if str(account["platform"]) != "douyin":
            raise StateTransitionError("daily competitor snapshot currently supports Douyin only")
        if not raw_archive_ref.strip() or not collection_run_id.strip():
            raise StateTransitionError("daily competitor snapshot requires retained raw evidence and a collection run identity")
        if normalized_daily_run_id:
            daily_run = self.conn.execute(
                "SELECT domain_label, business_date FROM stage0_daily_run WHERE daily_run_id=?",
                (normalized_daily_run_id,),
            ).fetchone()
            if daily_run is None:
                raise StateTransitionError("daily competitor snapshot requires an existing daily run")
            if (
                str(daily_run["domain_label"]) != str(account["domain_label"])
                or str(daily_run["business_date"]) != str(business_date)
            ):
                raise StateTransitionError(
                    "daily competitor snapshot daily run does not match the account domain and business date"
                )
        try:
            batch_date = date.fromisoformat(business_date)
        except ValueError as exc:
            raise StateTransitionError("daily competitor snapshot requires a valid business date") from exc
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now.astimezone(timezone(timedelta(hours=8))).date() != batch_date:
            raise StateTransitionError(
                "daily competitor snapshot effective time must match its business date"
            )

        def published_time(value: Any) -> datetime | None:
            if value is None or value == "":
                return None
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
                numeric = float(value)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                try:
                    return datetime.fromtimestamp(numeric, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    return None
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        inserted = updated = checks_recorded = 0
        skipped_older_than_window = skipped_missing_publish_time = 0
        skipped_published_after_effective_at = 0
        source_refs: list[dict[str, str]] = []
        daily_cutoff = now - timedelta(days=HISTORICAL_MATURITY_DAYS)
        with self.conn:
            for item in items:
                if not isinstance(item, dict):
                    raise StateTransitionError("daily competitor snapshot item must be an object")
                source_id = str(item.get("source_id") or "").strip()
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
                if not source_id or not url:
                    raise StateTransitionError("daily competitor snapshot item lacks its platform identity or URL")
                metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
                counts = {
                    key: int(metrics.get(key) or 0)
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                }
                published = published_time(item.get("published_at"))
                if published is None:
                    skipped_missing_publish_time += 1
                    continue
                if published > now:
                    skipped_published_after_effective_at += 1
                    continue
                publish_iso = published.isoformat() if published is not None else None
                video_id = "competitor_video_" + _hash({"account_id": account_id, "source_id": source_id})[:20]
                existing = self.conn.execute(
                    "SELECT * FROM competitor_videos WHERE account_id=? AND platform_item_id=?",
                    (account_id, source_id),
                ).fetchone()
                if published < daily_cutoff:
                    retain_for_due_tracking = False
                    if existing is not None:
                        category = str(existing["first_contact_category"] or "")
                        if category == "formal_new" and not int(existing["tracking_completed"] or 0):
                            try:
                                first_batch_date = date.fromisoformat(
                                    str(existing["first_seen_business_date"] or "")
                                )
                            except ValueError:
                                first_batch_date = batch_date
                            batch_age = (batch_date - first_batch_date).days
                            retain_for_due_tracking = 0 <= batch_age <= 7
                        elif category == "transition" and existing["mature_snapshot_taken_at"] is None:
                            existing_published = published_time(existing["publish_time"]) or published
                            retain_for_due_tracking = 0 <= (now - existing_published).days <= 7
                    if not retain_for_due_tracking:
                        skipped_older_than_window += 1
                        continue
                if existing is None:
                    delay_hours = round((now - published).total_seconds() / 3600, 2) if published is not None else None
                    if delay_hours is None:
                        first_contact_category = "formal_new"
                    elif delay_hours <= 36:
                        first_contact_category = "formal_new"
                    elif delay_hours < HISTORICAL_MATURITY_DAYS * 24:
                        first_contact_category = "transition"
                    else:
                        first_contact_category = "historical_mature"
                    self.conn.execute(
                        "INSERT INTO competitor_videos("
                        "video_id, account_id, platform, platform_item_id, title, url, publish_time, duration_sec, "
                        "like_count, comment_count, share_count, collect_count, first_contact_category, discovery_delay_hours, "
                        "excluded_reason, registration_run_id, raw_archive_ref, raw_json, first_seen_business_date"
                        ") VALUES (?, ?, 'douyin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            video_id, account_id, source_id, title, url, publish_iso,
                            int(item.get("duration_seconds") or 0), counts["like_count"], counts["comment_count"],
                            counts["share_count"], counts["collect_count"], first_contact_category, delay_hours,
                            None if published is not None else "missing_publish_time", collection_run_id,
                            raw_archive_ref, _canonical(item), business_date,
                        ),
                    )
                    if first_contact_category in {"formal_new", "transition"}:
                        check_index = 0 if first_contact_category == "formal_new" else None
                        day_since_publish = (
                            None
                            if first_contact_category == "formal_new" or delay_hours is None
                            else max(0, int(delay_hours // 24))
                        )
                        self.conn.execute(
                            "INSERT OR IGNORE INTO video_checks("
                            "check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, "
                            "share_count, collect_count, run_id, business_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "video_check_" + _hash({
                                    "video_id": video_id,
                                    "d_index": check_index,
                                    "day_since_publish": day_since_publish,
                                    "business_date": business_date,
                                })[:20],
                                video_id, check_index, day_since_publish, counts["like_count"],
                                counts["comment_count"], counts["share_count"],
                                counts["collect_count"], collection_run_id, business_date,
                            ),
                        )
                        checks_recorded += 1
                    inserted += 1
                else:
                    category = str(existing["first_contact_category"] or "")
                    tracking_completed = int(existing["tracking_completed"] or 0)
                    mature_at = existing["mature_snapshot_taken_at"]
                    self.conn.execute(
                        "UPDATE competitor_videos SET title=?, url=?, publish_time=COALESCE(?, publish_time), duration_sec=?, "
                        "like_count=?, comment_count=?, share_count=?, collect_count=?, raw_archive_ref=?, raw_json=?, "
                        "last_checked_at=CURRENT_TIMESTAMP, check_count=check_count+1 WHERE video_id=?",
                        (
                            title, url, publish_iso, int(item.get("duration_seconds") or 0), counts["like_count"],
                            counts["comment_count"], counts["share_count"], counts["collect_count"], raw_archive_ref,
                            _canonical(item), existing["video_id"],
                        ),
                    )
                    check_index: int | None = None
                    day_since_publish: int | None = None
                    if category == "formal_new" and not tracking_completed:
                        first_seen_business_date = str(existing["first_seen_business_date"] or "").strip()
                        try:
                            first_batch_date = date.fromisoformat(first_seen_business_date)
                        except ValueError as exc:
                            raise StateTransitionError(
                                "tracked video lacks a valid first-seen business date"
                            ) from exc
                        batch_age = (batch_date - first_batch_date).days
                        check_index = batch_age if 0 <= batch_age <= 7 else None
                    elif category == "transition" and mature_at is None:
                        existing_published = published_time(existing["publish_time"]) or published
                        if existing_published is not None:
                            day_since_publish = max((now - existing_published).days, 0)
                            if day_since_publish >= 7:
                                self.conn.execute(
                                    "UPDATE competitor_videos SET mature_snapshot_taken_at=? WHERE video_id=?",
                                    (now.isoformat(), existing["video_id"]),
                                )
                    if check_index is not None or day_since_publish is not None:
                        check_identity = (
                            {
                                "video_id": existing["video_id"],
                                "d_index": check_index,
                                "business_date": business_date,
                            }
                            if check_index is not None
                            else {
                                "video_id": existing["video_id"],
                                "day_since_publish": day_since_publish,
                                "business_date": business_date,
                            }
                        )
                        same_business_check = self.conn.execute(
                            "SELECT 1 FROM video_checks WHERE video_id=? "
                            "AND discovery_batch_index IS ? AND day_since_publish IS ? "
                            "AND business_date=? LIMIT 1",
                            (existing["video_id"], check_index, day_since_publish, business_date),
                        ).fetchone()
                        if same_business_check is None:
                            cursor = self.conn.execute(
                                "INSERT OR IGNORE INTO video_checks("
                                "check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, "
                                "share_count, collect_count, run_id, business_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    "video_check_" + _hash(check_identity)[:20],
                                    existing["video_id"], check_index, day_since_publish, counts["like_count"],
                                    counts["comment_count"], counts["share_count"], counts["collect_count"], collection_run_id,
                                    business_date,
                                ),
                            )
                            checks_recorded += int(cursor.rowcount > 0)
                        if category == "formal_new":
                            completed_points = int(self.conn.execute(
                                "SELECT COUNT(DISTINCT discovery_batch_index) FROM video_checks "
                                "WHERE video_id=? AND discovery_batch_index BETWEEN 0 AND 7",
                                (existing["video_id"],),
                            ).fetchone()[0])
                            if completed_points == 8:
                                self.conn.execute(
                                    "UPDATE competitor_videos SET tracking_completed=1 WHERE video_id=?",
                                    (existing["video_id"],),
                                )
                    updated += 1
                source_refs.append({"video_id": video_id, "platform_item_id": source_id})
            result = {
                "account_id": account_id,
                "collection_run_id": collection_run_id,
                "business_date": business_date,
                "effective_at": now.isoformat(),
                "inserted": inserted,
                "updated": updated,
                "checks_recorded": checks_recorded,
                "skipped_older_than_window": skipped_older_than_window,
                "skipped_missing_publish_time": skipped_missing_publish_time,
                "skipped_published_after_effective_at": skipped_published_after_effective_at,
                "source_refs": source_refs,
            }
            if normalized_daily_run_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO daily_collection_account_completion "
                    "(daily_run_id, account_id, completed_at) VALUES (?, ?, ?)",
                    (normalized_daily_run_id, account_id, _now()),
                )
            self._audit(None, "daily_competitor_snapshot_recorded", result)
        return result

    def repair_delayed_daily_batch_d_points(
        self,
        *,
        video_ids: tuple[str, ...],
        prior_run_id: str,
        catchup_run_id: str,
        prior_business_date: str,
        catchup_business_date: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Repair only verified cross-midnight D0 rows from one delayed batch."""

        requested = tuple(dict.fromkeys(str(value).strip() for value in video_ids))
        if not requested or any(not value for value in requested):
            raise StateTransitionError("delayed daily batch repair requires explicit video identities")
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("delayed daily batch repair requires actor and reason")
        try:
            prior_date = date.fromisoformat(prior_business_date)
            catchup_date = date.fromisoformat(catchup_business_date)
        except ValueError as exc:
            raise StateTransitionError("delayed daily batch repair requires valid business dates") from exc
        if catchup_date - prior_date != timedelta(days=1):
            raise StateTransitionError("delayed daily batch repair only supports adjacent business dates")
        if f":{prior_business_date}:" not in prior_run_id or f":{catchup_business_date}:" not in catchup_run_id:
            raise StateTransitionError("delayed daily batch repair run identities do not match their dates")

        repaired: list[dict[str, str]] = []
        with self.conn:
            for video_id in requested:
                video = self.conn.execute(
                    "SELECT registration_run_id, first_seen_business_date FROM competitor_videos "
                    "WHERE video_id=? AND first_contact_category='formal_new'",
                    (video_id,),
                ).fetchone()
                if (
                    video is None
                    or str(video["registration_run_id"] or "") != prior_run_id
                    or str(video["first_seen_business_date"] or "") != catchup_business_date
                ):
                    raise StateTransitionError(
                        "delayed daily batch repair target no longer matches the verified video state"
                    )
                prior_check = self.conn.execute(
                    "SELECT check_id FROM video_checks WHERE video_id=? AND run_id=? "
                    "AND discovery_batch_index=0 AND business_date=?",
                    (video_id, prior_run_id, catchup_business_date),
                ).fetchall()
                catchup_check = self.conn.execute(
                    "SELECT check_id FROM video_checks WHERE video_id=? AND run_id=? "
                    "AND discovery_batch_index=0 AND business_date=?",
                    (video_id, catchup_run_id, catchup_business_date),
                ).fetchall()
                if len(prior_check) != 1 or len(catchup_check) != 1:
                    raise StateTransitionError(
                        "delayed daily batch repair requires exactly one verified row from each batch"
                    )
                self.conn.execute(
                    "UPDATE competitor_videos SET first_seen_business_date=? WHERE video_id=?",
                    (prior_business_date, video_id),
                )
                self.conn.execute(
                    "UPDATE video_checks SET business_date=? WHERE check_id=?",
                    (prior_business_date, prior_check[0]["check_id"]),
                )
                self.conn.execute(
                    "UPDATE video_checks SET discovery_batch_index=1 WHERE check_id=?",
                    (catchup_check[0]["check_id"],),
                )
                repaired.append({
                    "video_id": video_id,
                    "prior_check_id": str(prior_check[0]["check_id"]),
                    "catchup_check_id": str(catchup_check[0]["check_id"]),
                })
            result = {
                "prior_business_date": prior_business_date,
                "catchup_business_date": catchup_business_date,
                "prior_run_id": prior_run_id,
                "catchup_run_id": catchup_run_id,
                "repaired": repaired,
                "actor": actor.strip(),
                "reason": reason.strip(),
            }
            self._audit(None, "delayed_daily_batch_d_points_repaired", result)
        return result

    def repair_cross_midnight_daily_run_check_dates(
        self,
        *,
        run_id: str,
        business_date: str,
        mistaken_date: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Move only one verified run's post-midnight check rows back to its business date."""

        try:
            expected_date = date.fromisoformat(business_date)
            wrong_date = date.fromisoformat(mistaken_date)
        except ValueError as exc:
            raise StateTransitionError("cross-midnight repair requires valid business dates") from exc
        if wrong_date - expected_date != timedelta(days=1):
            raise StateTransitionError("cross-midnight repair only supports the following calendar date")
        if f":{business_date}:" not in run_id:
            raise StateTransitionError("cross-midnight repair run identity does not match its business date")
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("cross-midnight repair requires actor and reason")
        rows = self.conn.execute(
            "SELECT check_id FROM video_checks WHERE run_id=? AND business_date=? ORDER BY check_id",
            (run_id, mistaken_date),
        ).fetchall()
        if not rows:
            raise StateTransitionError("cross-midnight repair found no matching rows")
        unexpected = self.conn.execute(
            "SELECT DISTINCT business_date FROM video_checks WHERE run_id=? "
            "AND business_date NOT IN (?, ?)",
            (run_id, business_date, mistaken_date),
        ).fetchall()
        if unexpected:
            raise StateTransitionError("cross-midnight repair found unexpected dates in the target run")
        check_ids = [str(row["check_id"]) for row in rows]
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE video_checks SET business_date=? WHERE run_id=? AND business_date=?",
                (business_date, run_id, mistaken_date),
            )
            if cursor.rowcount != len(check_ids):
                raise StateTransitionError("cross-midnight repair row count changed during the transaction")
            result = {
                "run_id": run_id,
                "business_date": business_date,
                "mistaken_date": mistaken_date,
                "repaired_check_count": len(check_ids),
                "actor": actor.strip(),
                "reason": reason.strip(),
            }
            self._audit(None, "cross_midnight_daily_run_check_dates_repaired", result)
        return result

    def discard_superseded_daily_check_duplicates(
        self,
        *,
        superseded_run_id: str,
        authoritative_run_id: str,
        business_date: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Remove one older run's exact duplicate D rows after retaining an audited copy."""

        try:
            date.fromisoformat(business_date)
        except ValueError as exc:
            raise StateTransitionError("duplicate daily-check cleanup requires a valid business date") from exc
        if (
            not actor.strip()
            or not reason.strip()
            or f":{business_date}:" not in superseded_run_id
            or f":{business_date}:" not in authoritative_run_id
            or superseded_run_id == authoritative_run_id
        ):
            raise StateTransitionError("duplicate daily-check cleanup scope is invalid")
        rows = self.conn.execute(
            "SELECT old.* FROM video_checks old WHERE old.run_id=? AND old.business_date=? "
            "AND EXISTS (SELECT 1 FROM video_checks kept WHERE kept.run_id=? "
            "AND kept.business_date=old.business_date AND kept.video_id=old.video_id "
            "AND kept.discovery_batch_index IS old.discovery_batch_index "
            "AND kept.day_since_publish IS old.day_since_publish) "
            "ORDER BY old.check_id",
            (superseded_run_id, business_date, authoritative_run_id),
        ).fetchall()
        if not rows:
            raise StateTransitionError("duplicate daily-check cleanup found no exact duplicates")
        archived_rows = [{key: row[key] for key in row.keys()} for row in rows]
        check_ids = [str(row["check_id"]) for row in rows]
        placeholders = ",".join("?" for _ in check_ids)
        with self.conn:
            self._audit(None, "superseded_daily_check_duplicates_archived", {
                "superseded_run_id": superseded_run_id,
                "authoritative_run_id": authoritative_run_id,
                "business_date": business_date,
                "actor": actor.strip(),
                "reason": reason.strip(),
                "rows": archived_rows,
            })
            cursor = self.conn.execute(
                f"DELETE FROM video_checks WHERE check_id IN ({placeholders})",
                check_ids,
            )
            if cursor.rowcount != len(check_ids):
                raise StateTransitionError("duplicate daily-check cleanup row count changed during the transaction")
            result = {
                "superseded_run_id": superseded_run_id,
                "authoritative_run_id": authoritative_run_id,
                "business_date": business_date,
                "removed_check_count": len(check_ids),
                "archived_in_audit": True,
                "actor": actor.strip(),
                "reason": reason.strip(),
            }
            self._audit(None, "superseded_daily_check_duplicates_removed", result)
        return result

    @staticmethod
    def _daily_run_business_date(run_id: Any) -> str | None:
        match = re.match(
            r"^daily_competitor:[^:]+:(\d{4}-\d{2}-\d{2}):",
            str(run_id or ""),
        )
        return match.group(1) if match else None

    def _daily_observation_reconciliation_state(
        self, *, as_of_business_date: str
    ) -> dict[str, Any]:
        try:
            as_of_date = date.fromisoformat(as_of_business_date)
        except ValueError as exc:
            raise StateTransitionError(
                "daily observation reconciliation requires a valid business date"
            ) from exc

        videos = {
            str(row["video_id"]): {key: row[key] for key in row.keys()}
            for row in self.conn.execute("SELECT * FROM competitor_videos").fetchall()
        }
        checks = [
            {key: row[key] for key in row.keys()}
            for row in self.conn.execute("SELECT * FROM video_checks").fetchall()
        ]
        canonical_first_seen: dict[str, str] = {}
        for video_id, video in videos.items():
            if str(video.get("first_contact_category") or "") != "formal_new":
                continue
            first_seen = self._daily_run_business_date(video.get("registration_run_id"))
            if not first_seen:
                first_seen = str(video.get("first_seen_business_date") or "").strip()
            try:
                date.fromisoformat(first_seen)
            except ValueError as exc:
                raise StateTransitionError(
                    f"formal daily video lacks a recoverable first business date: {video_id}"
                ) from exc
            canonical_first_seen[video_id] = first_seen

        canonical_rows: list[dict[str, Any]] = []
        for row in checks:
            video_id = str(row["video_id"])
            video = videos.get(video_id)
            if video is None:
                raise StateTransitionError("daily check references an unknown video")
            business_date = self._daily_run_business_date(row.get("run_id")) or str(
                row.get("business_date") or ""
            ).strip()
            try:
                business_day = date.fromisoformat(business_date)
            except ValueError as exc:
                raise StateTransitionError(
                    f"daily check lacks a recoverable business date: {row['check_id']}"
                ) from exc
            discovery_batch_index = row.get("discovery_batch_index")
            day_since_publish = row.get("day_since_publish")
            if video_id in canonical_first_seen:
                discovery_batch_index = (
                    business_day - date.fromisoformat(canonical_first_seen[video_id])
                ).days
                day_since_publish = None
                if not 0 <= discovery_batch_index <= 7:
                    raise StateTransitionError(
                        f"formal daily check falls outside D0-D7: {row['check_id']}"
                    )
            canonical_rows.append({
                "row": row,
                "business_date": business_date,
                "discovery_batch_index": discovery_batch_index,
                "day_since_publish": day_since_publish,
            })

        rows_by_video_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in canonical_rows:
            key = (str(item["row"]["video_id"]), str(item["business_date"]))
            rows_by_video_day.setdefault(key, []).append(item)
        kept_rows: list[dict[str, Any]] = []
        duplicate_rows: list[dict[str, Any]] = []
        for group in rows_by_video_day.values():
            authoritative = max(
                group,
                key=lambda item: (
                    str(item["row"].get("checked_at") or ""),
                    str(item["row"].get("run_id") or ""),
                    str(item["row"]["check_id"]),
                ),
            )
            kept_rows.append(authoritative)
            duplicate_rows.extend(
                item for item in group if item["row"]["check_id"] != authoritative["row"]["check_id"]
            )

        source_runs: dict[tuple[str, str], list[dict[str, str]]] = {}
        audit_rows = self.conn.execute(
            "SELECT payload_json, created_at FROM stage0_audit_event "
            "WHERE action='daily_competitor_snapshot_recorded' AND data_identity=?",
            (self.data_identity,),
        ).fetchall()
        for audit_row in audit_rows:
            try:
                payload = json.loads(str(audit_row["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            run_id = str(payload.get("collection_run_id") or "")
            business_date = self._daily_run_business_date(run_id)
            if not business_date:
                continue
            for source_ref in payload.get("source_refs") or []:
                if not isinstance(source_ref, dict):
                    continue
                video_id = str(source_ref.get("video_id") or "").strip()
                if not video_id:
                    continue
                source_runs.setdefault((business_date, video_id), []).append({
                    "run_id": run_id,
                    "created_at": str(audit_row["created_at"] or ""),
                })

        present = {
            (
                str(item["row"]["video_id"]),
                str(item["business_date"]),
                int(item["discovery_batch_index"]),
            )
            for item in kept_rows
            if item["discovery_batch_index"] is not None
        }
        gaps: list[dict[str, Any]] = []
        for video_id, first_seen in canonical_first_seen.items():
            first_date = date.fromisoformat(first_seen)
            maximum_d = min(7, (as_of_date - first_date).days)
            if maximum_d < 0:
                continue
            for expected_d in range(maximum_d + 1):
                business_date = (first_date + timedelta(days=expected_d)).isoformat()
                if (video_id, business_date, expected_d) in present:
                    continue
                source_evidence = source_runs.get((business_date, video_id), [])
                latest_source = (
                    max(source_evidence, key=lambda item: (item["created_at"], item["run_id"]))
                    if source_evidence
                    else None
                )
                video = videos[video_id]
                gaps.append({
                    "video_id": video_id,
                    "platform_item_id": str(video.get("platform_item_id") or ""),
                    "url": str(video.get("url") or ""),
                    "business_date": business_date,
                    "expected_d": expected_d,
                    "source_seen": latest_source is not None,
                    "source_run_id": latest_source["run_id"] if latest_source else None,
                    "metrics": {
                        key: int(video.get(key) or 0)
                        for key in ("like_count", "comment_count", "share_count", "collect_count")
                    },
                    "raw_archive_ref": str(video.get("raw_archive_ref") or ""),
                    "raw_json": str(video.get("raw_json") or "{}"),
                })
        return {
            "as_of_business_date": as_of_business_date,
            "videos": videos,
            "canonical_first_seen": canonical_first_seen,
            "kept_rows": kept_rows,
            "duplicate_rows": duplicate_rows,
            "gaps": gaps,
        }

    def daily_observation_reconciliation_plan(
        self, *, as_of_business_date: str
    ) -> dict[str, Any]:
        state = self._daily_observation_reconciliation_state(
            as_of_business_date=as_of_business_date
        )
        changed_first_seen = [
            video_id
            for video_id, value in state["canonical_first_seen"].items()
            if value != str(state["videos"][video_id].get("first_seen_business_date") or "")
        ]
        changed_business_dates = [
            item for item in state["kept_rows"]
            if item["business_date"] != str(item["row"].get("business_date") or "")
        ]
        changed_d_points = [
            item for item in state["kept_rows"]
            if item["discovery_batch_index"] != item["row"].get("discovery_batch_index")
        ]
        target_gaps = [
            item for item in state["gaps"]
            if item["business_date"] == as_of_business_date
        ]
        return {
            "as_of_business_date": as_of_business_date,
            "first_seen_dates_to_change": len(changed_first_seen),
            "business_dates_to_change": len(changed_business_dates),
            "d_points_to_change": len(changed_d_points),
            "duplicate_rows_to_archive_remove": len(state["duplicate_rows"]),
            "target_date_gaps": len(target_gaps),
            "target_date_source_backed_gaps": sum(bool(item["source_seen"]) for item in target_gaps),
            "target_date_live_detail_gaps": sum(not item["source_seen"] for item in target_gaps),
            "historical_gaps_to_register": len(state["gaps"]),
            "recovery_targets": target_gaps,
        }

    def reconcile_daily_observation_history(
        self,
        *,
        as_of_business_date: str,
        recovery_observations: tuple[dict[str, Any], ...],
        repair_run_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        if not repair_run_id.strip() or not actor.strip() or not reason.strip():
            raise StateTransitionError("daily observation reconciliation requires audited repair metadata")
        state = self._daily_observation_reconciliation_state(
            as_of_business_date=as_of_business_date
        )
        formal_baselines = int(self.conn.execute(
            "SELECT COUNT(*) FROM baselines WHERE baseline_mode='formal_d_series'"
        ).fetchone()[0])
        formal_hits = int(self.conn.execute(
            "SELECT COUNT(*) FROM hits WHERE baseline_id LIKE 'formal_d_baseline_%'"
        ).fetchone()[0])
        if formal_baselines or formal_hits:
            raise StateTransitionError(
                "daily observation reconciliation requires a separate formal-D baseline rebuild"
            )

        target_gaps = {
            (str(item["video_id"]), str(item["business_date"]), int(item["expected_d"])): item
            for item in state["gaps"]
            if item["business_date"] == as_of_business_date
        }
        supplied = {
            (
                str(item.get("video_id") or ""),
                str(item.get("business_date") or ""),
                int(item.get("expected_d")),
            ): item
            for item in recovery_observations
        }
        if not set(supplied).issubset(set(target_gaps)):
            raise StateTransitionError(
                "daily observation reconciliation received a recovery observation outside the target gaps"
            )
        for key, observation in supplied.items():
            video = state["videos"].get(key[0])
            if video is None or str(observation.get("platform_item_id") or "") != str(
                video.get("platform_item_id") or ""
            ):
                raise StateTransitionError("daily recovery observation does not match its formal video")
            run_id = str(observation.get("run_id") or "")
            if self._daily_run_business_date(run_id) != as_of_business_date:
                raise StateTransitionError("daily recovery run identity does not match the target date")
            metrics = observation.get("metrics")
            if not isinstance(metrics, dict) or any(
                key_name not in metrics
                for key_name in ("like_count", "comment_count", "share_count", "collect_count")
            ):
                raise StateTransitionError("daily recovery observation lacks complete metrics")
            if not str(observation.get("raw_archive_ref") or "").strip():
                raise StateTransitionError("daily recovery observation lacks retained raw evidence")

        changed_rows: list[dict[str, Any]] = []
        for item in state["kept_rows"]:
            row = item["row"]
            if (
                item["business_date"] != str(row.get("business_date") or "")
                or item["discovery_batch_index"] != row.get("discovery_batch_index")
                or item["day_since_publish"] != row.get("day_since_publish")
            ):
                changed_rows.append({
                    "before": row,
                    "after": {
                        "business_date": item["business_date"],
                        "discovery_batch_index": item["discovery_batch_index"],
                        "day_since_publish": item["day_since_publish"],
                    },
                })
        changed_first_seen = [
            {
                "video_id": video_id,
                "before": str(state["videos"][video_id].get("first_seen_business_date") or ""),
                "after": value,
            }
            for video_id, value in state["canonical_first_seen"].items()
            if value != str(state["videos"][video_id].get("first_seen_business_date") or "")
        ]
        duplicate_rows = [item["row"] for item in state["duplicate_rows"]]

        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS daily_observation_gaps ("
                "gap_id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES competitor_videos(video_id) ON DELETE RESTRICT, "
                "business_date TEXT NOT NULL, expected_d INTEGER NOT NULL CHECK(expected_d BETWEEN 0 AND 7), "
                "reason TEXT NOT NULL, repair_run_id TEXT NOT NULL, data_identity TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "UNIQUE(video_id, business_date, expected_d, data_identity))"
            )
            self._audit(None, "daily_observation_reconciliation_archived", {
                "repair_run_id": repair_run_id,
                "as_of_business_date": as_of_business_date,
                "actor": actor.strip(),
                "reason": reason.strip(),
                "changed_first_seen": changed_first_seen,
                "changed_rows": changed_rows,
                "removed_duplicate_rows": duplicate_rows,
            })
            for item in state["duplicate_rows"]:
                self.conn.execute(
                    "DELETE FROM video_checks WHERE check_id=?",
                    (item["row"]["check_id"],),
                )
            for item in state["kept_rows"]:
                self.conn.execute(
                    "UPDATE video_checks SET business_date=?, discovery_batch_index=?, day_since_publish=? "
                    "WHERE check_id=?",
                    (
                        item["business_date"],
                        item["discovery_batch_index"],
                        item["day_since_publish"],
                        item["row"]["check_id"],
                    ),
                )
            for video_id, first_seen in state["canonical_first_seen"].items():
                self.conn.execute(
                    "UPDATE competitor_videos SET first_seen_business_date=? WHERE video_id=?",
                    (first_seen, video_id),
                )
            for (video_id, business_date, expected_d), observation in supplied.items():
                metrics = {
                    key: int(observation["metrics"].get(key) or 0)
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                }
                check_id = "video_check_" + _hash({
                    "video_id": video_id,
                    "business_date": business_date,
                    "d_index": expected_d,
                    "repair_run_id": repair_run_id,
                })[:20]
                self.conn.execute(
                    "INSERT INTO video_checks(check_id, video_id, discovery_batch_index, day_since_publish, "
                    "like_count, comment_count, share_count, collect_count, run_id, business_date) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                    (
                        check_id,
                        video_id,
                        expected_d,
                        metrics["like_count"],
                        metrics["comment_count"],
                        metrics["share_count"],
                        metrics["collect_count"],
                        observation["run_id"],
                        business_date,
                    ),
                )
                self.conn.execute(
                    "UPDATE competitor_videos SET like_count=?, comment_count=?, share_count=?, collect_count=?, "
                    "raw_archive_ref=?, raw_json=?, last_checked_at=CURRENT_TIMESTAMP WHERE video_id=?",
                    (
                        metrics["like_count"],
                        metrics["comment_count"],
                        metrics["share_count"],
                        metrics["collect_count"],
                        str(observation["raw_archive_ref"]),
                        str(observation.get("raw_json") or "{}"),
                        video_id,
                    ),
                )

            self.conn.execute(
                "DELETE FROM daily_observation_gaps WHERE data_identity=? AND business_date<=?",
                (self.data_identity, as_of_business_date),
            )
            refreshed = self._daily_observation_reconciliation_state(
                as_of_business_date=as_of_business_date
            )
            for gap in refreshed["gaps"]:
                self.conn.execute(
                    "INSERT INTO daily_observation_gaps(gap_id, video_id, business_date, expected_d, reason, "
                    "repair_run_id, data_identity) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "daily_gap_" + _hash({
                            "video_id": gap["video_id"],
                            "business_date": gap["business_date"],
                            "expected_d": gap["expected_d"],
                        })[:20],
                        gap["video_id"],
                        gap["business_date"],
                        gap["expected_d"],
                        "no_retained_observation; excluded_from_complete_d_baseline",
                        repair_run_id,
                        self.data_identity,
                    ),
                )
            self.conn.execute(
                "UPDATE competitor_videos SET tracking_completed=0 "
                "WHERE first_contact_category='formal_new'"
            )
            self.conn.execute(
                "UPDATE competitor_videos SET tracking_completed=1 "
                "WHERE first_contact_category='formal_new' AND video_id IN ("
                "SELECT video_id FROM video_checks WHERE discovery_batch_index BETWEEN 0 AND 7 "
                "GROUP BY video_id HAVING COUNT(*)=8 AND COUNT(DISTINCT discovery_batch_index)=8)"
            )
            target_remaining = [
                gap for gap in refreshed["gaps"]
                if gap["business_date"] == as_of_business_date
            ]
            result = {
                "repair_run_id": repair_run_id,
                "as_of_business_date": as_of_business_date,
                "first_seen_dates_changed": len(changed_first_seen),
                "check_rows_changed": len(changed_rows),
                "duplicate_rows_removed": len(duplicate_rows),
                "target_date_checks_recovered": len(supplied),
                "target_date_gaps_remaining": len(target_remaining),
                "historical_gaps_registered": len(refreshed["gaps"]),
                "tracking_completed_videos": int(self.conn.execute(
                    "SELECT COUNT(*) FROM competitor_videos "
                    "WHERE first_contact_category='formal_new' AND tracking_completed=1"
                ).fetchone()[0]),
                "formal_d_baselines_affected": 0,
                "actor": actor.strip(),
                "reason": reason.strip(),
            }
            self._audit(None, "daily_observation_history_reconciled", result)
        return result

    def judge_daily_competitor_hits(
        self,
        *,
        account_id: str,
        source_video_ids: tuple[str, ...],
        run_id: str,
        evaluated_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Apply the same small cold-start contract after every daily snapshot."""
        if not source_video_ids or not run_id.strip():
            return {"account_id": account_id, "judged": 0, "new_hit_ids": []}
        account = self.conn.execute(
            "SELECT * FROM competitor_accounts WHERE account_id=? AND registration_status='active'",
            (account_id,),
        ).fetchone()
        if account is None:
            raise StateTransitionError("daily hit judgement requires one active competitor account")

        evaluation_time = evaluated_at or datetime.now(timezone.utc)
        if evaluation_time.tzinfo is None:
            raise StateTransitionError("daily hit evaluation time must include a timezone")
        evaluation_time = evaluation_time.astimezone(timezone.utc)
        mature_cutoff = evaluation_time - timedelta(days=HISTORICAL_MATURITY_DAYS)
        mature_rows = self.conn.execute(
            "SELECT * FROM competitor_videos WHERE account_id=? AND excluded_reason IS NULL "
            "AND publish_time IS NOT NULL AND datetime(publish_time)<=datetime(?) "
            "ORDER BY datetime(publish_time) DESC, video_id DESC",
            (account_id, mature_cutoff.isoformat()),
        ).fetchall()
        recent_cutoff = evaluation_time - timedelta(days=MATURE_HISTORY_WINDOW_DAYS)
        def normalized_publish_time(row: sqlite3.Row) -> datetime:
            value = datetime.fromisoformat(str(row["publish_time"]).replace("Z", "+00:00"))
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

        recent = [
            row for row in mature_rows
            if normalized_publish_time(row) >= recent_cutoff
        ][:FIRST_REGISTRATION_MAX_ITEMS]
        if len(recent) < MIN_RELIABLE_HISTORY_ITEMS:
            known = {str(row["video_id"]) for row in recent}
            older = [row for row in mature_rows if str(row["video_id"]) not in known]
            recent.extend(older[: max(0, MIN_RELIABLE_HISTORY_ITEMS - len(recent))])
        baseline_rows = recent[:FIRST_REGISTRATION_MAX_ITEMS]
        baseline_metrics = [
            {
                metric: int(row[metric] or 0)
                for metric in HISTORICAL_METRICS
            }
            for row in baseline_rows
        ]
        account_maturity = derive_account_maturity_state(len(baseline_rows))
        mature_reference = judge_against_mature_history(
            candidate_metrics={metric: 0 for metric in HISTORICAL_METRICS},
            baseline_metrics=baseline_metrics,
        )
        baseline_group_id = "mature_baseline_" + _hash({
            "account_id": account_id,
            "run_id": run_id,
            "samples": [str(row["video_id"]) for row in baseline_rows],
        })[:20]

        new_hit_ids: list[str] = []
        judged = 0
        with self.conn:
            if mature_reference["baseline_active"]:
                for metric, median in mature_reference["medians"].items():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO baselines(baseline_id, account_id, baseline_mode, metric, observation_point, "
                        "sample_count, median_value, run_id) VALUES (?, ?, 'mature_history', ?, NULL, ?, ?, ?)",
                        (
                            f"{baseline_group_id}_{metric}",
                            account_id,
                            metric,
                            len(baseline_rows),
                            median,
                            run_id,
                        ),
                    )
            for video_id in source_video_ids:
                video = self.conn.execute(
                    "SELECT * FROM competitor_videos WHERE video_id=? AND account_id=?",
                    (video_id, account_id),
                ).fetchone()
                if video is None or video["excluded_reason"] is not None:
                    continue
                metrics = {
                    metric: int(video[metric] or 0)
                    for metric in HISTORICAL_METRICS
                }
                checks = self.conn.execute(
                    "SELECT * FROM video_checks WHERE video_id=? ORDER BY checked_at, check_id",
                    (video_id,),
                ).fetchall()
                latest = checks[-1] if checks else None
                category = str(video["first_contact_category"] or "")
                observation = "history"
                channels: list[str] = []
                baseline_mode: str | None = None
                baseline_id: str | None = None

                ratio_result = judge_against_mature_history(
                    candidate_metrics=metrics,
                    baseline_metrics=[],
                )
                ratio_channels = [
                    channel for channel in ratio_result["channels"]
                    if channel.startswith("comment_like_ratio:")
                ]
                channels.extend(ratio_channels)

                if category in {"historical_mature", "transition"}:
                    history_result = judge_against_mature_history(
                        candidate_metrics=metrics,
                        baseline_metrics=baseline_metrics,
                    )
                    channels.extend(
                        channel for channel in history_result["channels"]
                        if not channel.startswith("comment_like_ratio:")
                    )
                    if any(not channel.startswith("comment_like_ratio:") for channel in channels):
                        baseline_mode = "mature_history"
                        baseline_id = baseline_group_id
                    if latest is not None and latest["day_since_publish"] is not None:
                        observation = f"day{int(latest['day_since_publish'])}"
                elif category == "formal_new" and latest is not None:
                    d_index = latest["discovery_batch_index"]
                    if d_index is not None:
                        observation = f"D{int(d_index)}"
                        predecessor_rows = self.conn.execute(
                            "SELECT video_id FROM competitor_videos WHERE account_id=? "
                            "AND first_contact_category='formal_new' AND tracking_completed=1 "
                            "AND discovery_delay_hours<=36 AND video_id<>? "
                            "ORDER BY first_seen_at, video_id",
                            (account_id, video_id),
                        ).fetchall()
                        sequences: list[dict[int, dict[str, int]]] = []
                        for predecessor in predecessor_rows:
                            sequence_rows = self.conn.execute(
                                "SELECT discovery_batch_index, like_count, comment_count, share_count, collect_count "
                                "FROM video_checks WHERE video_id=? AND discovery_batch_index BETWEEN 0 AND 7 "
                                "ORDER BY discovery_batch_index, checked_at",
                                (predecessor["video_id"],),
                            ).fetchall()
                            sequence = {
                                int(row["discovery_batch_index"]): {
                                    metric: int(row[metric] or 0)
                                    for metric in HISTORICAL_METRICS
                                }
                                for row in sequence_rows
                            }
                            if set(sequence) >= set(range(8)):
                                sequences.append(sequence)
                        formal_result = judge_against_formal_d_baseline(
                            candidate_metrics=metrics,
                            predecessor_sequences=sequences,
                            discovery_batch_index=int(d_index),
                        )
                        channels.extend(formal_result["channels"])
                        if formal_result["baseline_active"]:
                            baseline_mode = "formal_d_series"
                            baseline_id = "formal_d_baseline_" + _hash({
                                "account_id": account_id,
                                "run_id": run_id,
                                "observation": observation,
                            })[:20]
                            for metric, median in formal_result["medians"].items():
                                self.conn.execute(
                                    "INSERT OR IGNORE INTO baselines(baseline_id, account_id, baseline_mode, metric, "
                                    "observation_point, sample_count, median_value, run_id) "
                                    "VALUES (?, ?, 'formal_d_series', ?, ?, ?, ?, ?)",
                                    (
                                        f"{baseline_id}_{metric}",
                                        account_id,
                                        metric,
                                        observation,
                                        formal_result["qualifying_sequence_count"],
                                        median,
                                        run_id,
                                    ),
                                )
                        elif int(d_index) >= 7:
                            rough_result = judge_against_mature_history(
                                candidate_metrics=metrics,
                                baseline_metrics=baseline_metrics,
                            )
                            channels.extend(
                                channel for channel in rough_result["channels"]
                                if not channel.startswith("comment_like_ratio:")
                            )
                            if any(
                                not channel.startswith("comment_like_ratio:")
                                for channel in rough_result["channels"]
                            ):
                                baseline_mode = "mature_history"
                                baseline_id = baseline_group_id

                channels = list(dict.fromkeys(channels))
                judged += 1
                if not channels:
                    continue
                confidence = (
                    "formal"
                    if any(
                        channel.startswith(("formal_d_", "comment_like_ratio:"))
                        for channel in channels
                    )
                    else "rough"
                )
                previous_rules = []
                if video["trigger_rules"]:
                    try:
                        previous_rules = list(json.loads(str(video["trigger_rules"])))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        previous_rules = []
                cumulative = list(dict.fromkeys([*previous_rules, *channels]))
                self.conn.execute(
                    "UPDATE competitor_videos SET first_trigger_observation=COALESCE(first_trigger_observation, ?), "
                    "first_trigger_at=COALESCE(first_trigger_at, ?), trigger_rules=?, peak_observation=?, "
                    "baseline_mode=COALESCE(baseline_mode, ?), judgment_confidence=? WHERE video_id=?",
                    (
                        observation,
                        _now(),
                        _canonical(cumulative),
                        observation,
                        baseline_mode,
                        confidence,
                        video_id,
                    ),
                )
                hit_id = "competitor_hit_" + _hash({
                    "account_id": account_id,
                    "source_id": str(video["platform_item_id"]),
                })[:20]
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url, "
                    "publish_time, like_count, comment_count, share_count, collect_count, hit_channel, "
                    "judgment_confidence, baseline_id, run_id, preparation_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (
                        hit_id,
                        video_id,
                        account_id,
                        video["platform"],
                        video["platform_item_id"],
                        video["title"],
                        video["url"],
                        video["publish_time"],
                        metrics["like_count"],
                        metrics["comment_count"],
                        metrics["share_count"],
                        metrics["collect_count"],
                        ",".join(channels),
                        confidence,
                        baseline_id,
                        run_id,
                    ),
                )
                if cursor.rowcount:
                    new_hit_ids.append(hit_id)
        result = {
            "account_id": account_id,
            "judged": judged,
            "mature_baseline_sample_count": len(baseline_rows),
            "account_maturity_state": account_maturity["state"],
            "mature_history_ready": account_maturity["mature_history_ready"],
            "new_hit_ids": new_hit_ids,
        }
        self._audit(None, "daily_competitor_hits_judged", result)
        return result

    def discovery_source_readiness(
        self, *, domain_label: str, daily_since: str, hotspot_discovery_run_id: str | None = None
    ) -> dict[str, Any]:
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("daily discovery requires a configured formal domain")
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        daily_sources = self.conn.execute(
            "SELECT COUNT(*) FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (self.data_identity, domain_label, daily_since),
        ).fetchone()[0] if competitor_ready else 0
        historical_sources = self.conn.execute(
            "SELECT COUNT(*) FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence IN ('rough', 'formal') AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (self.data_identity, domain_label),
        ).fetchone()[0] if competitor_ready else 0
        hotspot_scope = ""
        hotspot_params: list[Any] = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=?"
            hotspot_params.append(hotspot_discovery_run_id)
        hotspot_rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures')"
            + hotspot_scope,
            tuple(hotspot_params),
        ).fetchall()
        hotspot_sources = len(
            _build_hotspot_event_cluster_sources(
                list(hotspot_rows),
                per_source_limit=len(hotspot_rows) or 1,
            )
        )
        tag_sources = self.conn.execute(
            "SELECT COUNT(*) FROM discovered_external_videos video JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
            "WHERE video.domain_label=? AND tag.status='active' AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>''",
            (domain_label,),
        ).fetchone()[0]
        question_expansion_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_question_expansion_source source "
            "JOIN stage1_question_expansion_qualification qualification "
            "ON qualification.expansion_id=source.expansion_id "
            "AND qualification.data_identity=source.data_identity "
            "AND qualification.status='qualified' "
            "WHERE source.domain_label=? AND source.validation_outcome='supported' "
            "AND source.data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        saved_user_direction_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        counts = {
            "hotspot_sources": hotspot_sources,
            "daily_sources": daily_sources,
            "historical_sources": historical_sources,
            "tag_sources": tag_sources,
            "question_expansion_sources": question_expansion_sources,
            "saved_user_direction_sources": saved_user_direction_sources,
        }
        if not any(counts.values()):
            return {"status": "blocked", "reason": "no_qualified_formal_source", **counts}
        return {"status": "ready", "reason": "qualified_formal_source_available", **counts}

    def load_hotspot_event_clusters(
        self,
        *,
        daily_since: str,
        per_source_limit: int | None,
        hotspot_discovery_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one temporary source per TrendRadar event, without domain keyword filtering."""
        hotspot_scope = ""
        hotspot_params = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=? "
            hotspot_params.append(hotspot_discovery_run_id)
        rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures') "
            + hotspot_scope
            + "ORDER BY observation.observed_at DESC, observation.observation_id ASC",
            tuple(hotspot_params),
        ).fetchall()
        return _build_hotspot_event_cluster_sources(
            list(rows), per_source_limit=per_source_limit
        )

    def reusable_hotspot_batch_status(self, *, discovery_run_id: str) -> dict[str, Any]:
        """Return the one successful temporary hotspot batch that may be reused in a validation."""
        rows = self.conn.execute(
            "SELECT collection_run_id, status, item_count FROM trendradar_collection_run "
            "WHERE discovery_run_id=? ORDER BY started_at DESC, collection_run_id ASC",
            (discovery_run_id,),
        ).fetchall()
        if len(rows) != 1:
            raise StateTransitionError("a reusable hotspot batch must have exactly one recorded collection")
        row = rows[0]
        if str(row["status"]) != "completed" or int(row["item_count"] or 0) < 1:
            raise StateTransitionError("only a successful non-empty hotspot collection can be reused")
        observation_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM trendradar_hotspot_observation WHERE collection_run_id=?",
            (str(row["collection_run_id"]),),
        ).fetchone()[0])
        if observation_count < 1:
            raise StateTransitionError("the successful hotspot batch has already been cleared and cannot be reused")
        return {
            "discovery_run_id": discovery_run_id,
            "collection_run_id": str(row["collection_run_id"]),
            "item_count": int(row["item_count"]),
            "observation_count": observation_count,
        }



    def load_real_discovery_sources(
        self,
        *,
        domain_label: str,
        daily_since: str,
        per_source_limit: int,
        hotspot_discovery_run_id: str | None = None,
        resume_source_object_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Read only qualified, already-recorded formal source facts; never collect or invent content."""
        if self.discovery_source_readiness(
            domain_label=domain_label,
            daily_since=daily_since,
            hotspot_discovery_run_id=hotspot_discovery_run_id,
        )["status"] != "ready":
            return []
        result: list[dict[str, Any]] = []
        resume_ids = tuple(dict.fromkeys(str(item).strip() for item in resume_source_object_ids if str(item).strip()))
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        hotspot_scope = ""
        hotspot_params = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=? "
            hotspot_params.append(hotspot_discovery_run_id)
        all_hotspot_rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures') "
            + hotspot_scope +
            "ORDER BY observation.observed_at DESC, observation.observation_id ASC",
            tuple(hotspot_params),
        ).fetchall()
        result.extend(
            _build_hotspot_event_cluster_sources(
                list(all_hotspot_rows),
                per_source_limit=per_source_limit,
            )
        )
        daily_rows = self.conn.execute(
            "SELECT video.video_id, video.title, video.url, video.publish_time, video.last_checked_at, video.raw_json, account.account_name "
            "FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            "ORDER BY video.publish_time DESC, video.video_id ASC LIMIT ?",
            (self.data_identity, domain_label, daily_since, per_source_limit),
        ).fetchall() if competitor_ready else []
        for row in daily_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "daily_competitor_content", "source_object_id": row["video_id"], "source_object_version": row["last_checked_at"], "source_time": row["publish_time"], "payload": {"source_id": row["video_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "formal_source": {"table": "competitor_videos", "object_id": row["video_id"], "object_version": row["last_checked_at"], "raw_metadata_hash": raw_hash}}})
        historical_sql = (
            "AND hit.hit_id IN (" + ",".join("?" for _ in resume_ids) + ") "
            if resume_ids else
            "AND NOT EXISTS (SELECT 1 FROM stage1b_source_version attempted "
            "WHERE attempted.source_type='historical_high_signal' AND attempted.source_object_id=hit.hit_id "
            "AND attempted.source_object_version=hit.promoted_at AND attempted.data_identity=?) "
        )
        historical_params = [self.data_identity, domain_label]
        if resume_ids:
            historical_params.extend(resume_ids)
        else:
            historical_params.append(self.data_identity)
        historical_params.append(per_source_limit)
        historical_rows = self.conn.execute(
            "SELECT hit.hit_id, hit.title, hit.url, hit.publish_time, hit.promoted_at, hit.hit_channel, hit.judgment_confidence, "
            "video.raw_json, video.first_contact_category, account.account_name FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
             "JOIN competitor_accounts account ON account.account_id=hit.account_id "
             "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
             "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
             "WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence IN ('rough', 'formal') AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            + historical_sql +
            "ORDER BY hit.promoted_at DESC, hit.hit_id ASC LIMIT ?",
            tuple(historical_params),
        ).fetchall() if competitor_ready else []
        for row in historical_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "historical_high_signal", "source_object_id": row["hit_id"], "source_object_version": row["promoted_at"], "source_time": row["publish_time"], "payload": {"source_id": row["hit_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "signal_basis": row["hit_channel"], "signal_confidence": row["judgment_confidence"], "hit_origin": "daily_new_hit" if row["first_contact_category"] == "formal_new" else "cold_start_historical", "formal_source": {"table": "hits", "object_id": row["hit_id"], "object_version": row["promoted_at"], "raw_metadata_hash": raw_hash}}})
        tag_rows = self.conn.execute(
            "SELECT video.*, tag.tag FROM discovered_external_videos video "
            "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id WHERE video.domain_label=? "
            "AND tag.status='active' AND video.is_tracked_account=0 "
            "AND COALESCE(video.like_count, 0)>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_source_version attempted "
            "WHERE attempted.source_type='tag_discovery' AND attempted.source_object_id=video.discovered_video_id "
            "AND attempted.source_object_version=video.discovered_at AND attempted.data_identity=?) "
            "ORDER BY video.discovered_at DESC, video.discovered_video_id ASC LIMIT ?",
            (domain_label, TAG_CANDIDATE_LIKE_FLOOR, self.data_identity, per_source_limit),
        ).fetchall()
        for row in tag_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({
                "source_type": "tag_discovery",
                "source_object_id": row["discovered_video_id"],
                "source_object_version": row["discovered_at"],
                "source_time": row["discovered_at"],
                "payload": {
                    "source_id": row["discovered_video_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "platform": row["platform"],
                    "platform_item_id": row["platform_item_id"],
                    "account_platform_id": row["account_platform_id"],
                    "account_name": row["account_handle"] or row["account_platform_id"],
                    "is_tracked_account": bool(row["is_tracked_account"]),
                    "like_count": row["like_count"],
                    "comment_count": row["comment_count"],
                    "share_count": row["share_count"],
                    "collect_count": row["collect_count"],
                    "discovery_tag": row["tag"],
                    "source_time": row["discovered_at"],
                    "formal_source": {
                        "table": "discovered_external_videos",
                        "object_id": row["discovered_video_id"],
                        "object_version": row["discovered_at"],
                        "raw_metadata_hash": raw_hash,
                    },
                },
            })
        expansion_sql = (
            "AND source.expansion_id IN (" + ",".join("?" for _ in resume_ids) + ") "
            if resume_ids else
            "AND NOT EXISTS (SELECT 1 FROM stage1b_source_version attempted "
            "WHERE attempted.source_type='question_expansion' AND attempted.source_object_id=source.expansion_id "
            "AND attempted.source_object_version=source.integrity_hash AND attempted.data_identity=?) "
        )
        expansion_params = [domain_label, self.data_identity]
        if resume_ids:
            expansion_params.extend(resume_ids)
        else:
            expansion_params.append(self.data_identity)
        expansion_params.append(per_source_limit)
        expansion_rows = self.conn.execute(
            "SELECT source.*, qualification.status AS qualification_status, "
            "qualification.material_refs_json AS qualification_material_refs_json, "
            "qualification.checks_json AS qualification_checks_json "
            "FROM stage1_question_expansion_source source "
            "JOIN stage1_question_expansion_qualification qualification "
            "ON qualification.expansion_id=source.expansion_id "
            "AND qualification.data_identity=source.data_identity "
            "AND qualification.status='qualified' "
            "WHERE source.domain_label=? AND source.validation_outcome='supported' "
            "AND source.data_identity=? "
            + expansion_sql +
            "ORDER BY source.validated_at DESC, source.expansion_id ASC LIMIT ?",
            tuple(expansion_params),
        ).fetchall()
        for row in expansion_rows:
            payload = json.loads(row["payload_json"])
            derivation = payload.get("derivation") if isinstance(payload.get("derivation"), dict) else {}
            projection = derivation.get("content_type_projection")
            projection = projection if isinstance(projection, dict) else {}
            canonical_id = str(
                derivation.get("canonical_id") or projection.get("canonical_id") or ""
            ).strip()
            approved_projection = project_content_type(
                domain_label,
                lifecycle="classify",
                canonical_id=canonical_id,
            )
            if approved_projection.get("status") != "matched":
                # A legacy qualification row without a current frozen
                # projection is incomplete input, not a production source.
                continue
            qualification_material_refs = json.loads(str(row["qualification_material_refs_json"] or "[]"))
            qualification_checks = json.loads(str(row["qualification_checks_json"] or "{}"))
            result.append({
                "source_type": "question_expansion",
                "source_object_id": row["expansion_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["validated_at"],
                "payload": {
                    **payload,
                    "qualification_status": str(row["qualification_status"]),
                    "qualification_material_refs": qualification_material_refs if isinstance(qualification_material_refs, list) else [],
                    "qualification_checks": qualification_checks if isinstance(qualification_checks, dict) else {},
                    "source_id": row["expansion_id"],
                    "url": "",
                    "account_name": "已完成拓展验证",
                    "source_time": row["validated_at"],
                    "formal_source": {
                        "table": "stage1_question_expansion_source",
                        "object_id": row["expansion_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        direction_rows = self.conn.execute(
            "SELECT * FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=? "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_source_version attempted "
            "WHERE attempted.source_type='saved_user_direction' AND attempted.source_object_id=stage1_saved_user_direction_source.direction_id "
            "AND attempted.source_object_version=stage1_saved_user_direction_source.integrity_hash AND attempted.data_identity=?) "
            "ORDER BY saved_at DESC, direction_id ASC LIMIT ?",
            (domain_label, self.data_identity, self.data_identity, per_source_limit),
        ).fetchall()
        for row in direction_rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "source_type": "saved_user_direction",
                "source_object_id": row["direction_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["saved_at"],
                "payload": {
                    **payload,
                    "source_id": row["direction_id"],
                    "url": "",
                    "account_name": "用户保存方向",
                    "source_time": row["saved_at"],
                    "formal_source": {
                        "table": "stage1_saved_user_direction_source",
                        "object_id": row["direction_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        return result

    def discovery_source_seen(self, *, source_type: str, source_object_id: str, source_object_version: str) -> bool:
        """Only a user-selected production candidate consumes a source version.

        Validation, interrupted work and unresolved candidate attempts are evidence,
        not an editorial decision.  They must never make the next daily run pretend
        that a source has already been processed.
        """
        if self.data_identity != "production":
            row = self.conn.execute(
                "SELECT 1 FROM stage1b_source_version WHERE source_type=? AND source_object_id=? "
                "AND source_object_version=? AND data_identity=? LIMIT 1",
                (source_type, source_object_id, source_object_version, self.data_identity),
            ).fetchone()
            return row is not None
        row = self.conn.execute(
            "SELECT 1 FROM stage1b_source_version source "
            "JOIN stage1b_discovery_run run ON run.run_id=source.run_id "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "JOIN stage1b_candidate_version candidate ON candidate.source_version_id=source.source_version_id "
            "JOIN stage1b_candidate_decision decision ON decision.candidate_version_id=candidate.candidate_version_id "
            "WHERE source.source_type=? AND source.source_object_id=? AND source.source_object_version=? "
            "AND source.data_identity=? AND context.execution_mode='production_daily' "
            "AND context.lifecycle_status IN ('completed', 'completed_with_failures') "
            "AND decision.decision='selected' LIMIT 1",
            (source_type, source_object_id, source_object_version, self.data_identity),
        ).fetchone()
        return row is not None

    def formal_topic_title_seen(self, *, domain_label: str, normalized_title: str) -> bool:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return False
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=?",
            (self.data_identity,),
        ).fetchall()
        return any(json.loads(row["payload_json"]).get("domain") == domain_label and str(json.loads(row["payload_json"]).get("title", "")).casefold() == normalized_title for row in rows)

    def domain_formal_topic_count(self, *, domain_label: str, current_date: str) -> int:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return 0
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=? AND substr(version.created_at, 1, 10)=?",
            (self.data_identity, current_date),
        ).fetchall()
        return sum(1 for row in rows if json.loads(row["payload_json"]).get("domain") == domain_label)

    def get_discovery_candidate(self, candidate_version_id: str) -> dict[str, Any]:
        row = self._discovery_candidate(candidate_version_id)
        return {key: row[key] for key in row.keys()} | {"payload": json.loads(row["payload_json"])}

    def get_artifact_payload(self, version_id: str) -> dict[str, Any]:
        version = self._version(version_id)
        row = self.conn.execute(
            "SELECT artifact_kind, payload_json, integrity_hash, created_at FROM stage1a_artifact_payload WHERE version_id=? AND data_identity=?",
            (version_id, self.data_identity),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT artifact_kind, payload_json, integrity_hash, created_at FROM stage0_content_artifact_payload WHERE version_id=? AND data_identity=?",
                (version_id, self.data_identity),
            ).fetchone()
        if row is None:
            raise StateTransitionError("artifact payload does not exist for this version")
        return {
            "version_id": version_id,
            "task_id": version["task_id"],
            "node": version["node"],
            "artifact_kind": row["artifact_kind"],
            "payload": json.loads(row["payload_json"]),
            "integrity_hash": row["integrity_hash"],
            "created_at": row["created_at"],
        }

    def _insert_artifact_payload(self, version_id: str, artifact_kind: str, payload: dict[str, Any]) -> None:
        table = "stage1a_artifact_payload" if artifact_kind in {"formal_topic", "research_plan"} else "stage0_content_artifact_payload"
        self.conn.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, artifact_kind, _canonical(payload), _hash(payload), self.data_identity, _now()),
        )

    def _approved_upstream(self, task: sqlite3.Row, node: str, upstream_version_id: str | None) -> str:
        expected_node = UPSTREAM_NODE[node]
        if not upstream_version_id:
            raise StateTransitionError("exact approved upstream version is required")
        upstream = self._version(upstream_version_id)
        if upstream["task_id"] != task["task_id"] or upstream["node"] != expected_node:
            raise StateTransitionError("upstream version is not the adjacent required node")
        approved = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE version_id=? AND decision='approved'", (upstream_version_id,)
        ).fetchone()
        if approved is None:
            raise StateTransitionError("upstream version is not approved")
        return upstream_version_id

    @staticmethod
    def _assert_current_node(task: sqlite3.Row, node: str, status: str, version_id: str | None = None) -> None:
        if task["current_node"] != node or task["current_status"] != status:
            raise StateTransitionError(f"task is at {task['current_node']}/{task['current_status']}, not {node}/{status}")
        if version_id is not None and task["current_version_id"] != version_id:
            raise StaleResultError("node version is no longer current")

    def _set_task(self, task_id: str, *, node: str, version_id: str, status: str, revision: int) -> None:
        self.conn.execute(
            "UPDATE stage0_content_task SET current_node=?, current_version_id=?, current_status=?, task_revision=? WHERE task_id=?",
            (node, version_id, status, revision, task_id),
        )

    def _decision(self, task_id: str, node: str, version_id: str, decision: str, actor: str, actor_kind: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO stage0_content_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("decision"), task_id, node, version_id, decision, actor, actor_kind, reason, _now(), self.data_identity),
        )

    def _replay(self, scope: str, key: str, request: dict[str, Any]) -> dict[str, Any] | None:
        if not key:
            raise StateTransitionError("idempotency_key is required")
        row = self.conn.execute("SELECT request_hash, result_json FROM stage0_command_receipt WHERE command_scope=? AND idempotency_key=?", (scope, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != _hash(request):
            raise StateTransitionError("idempotency key was reused with a different request")
        value = json.loads(row["result_json"])
        if not isinstance(value, dict):
            raise StateTransitionError("stored command result is not an object")
        return {str(key): item for key, item in value.items()}

    def _receipt(self, scope: str, key: str, request: dict[str, Any], result: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO stage0_command_receipt VALUES (?, ?, ?, ?, ?)", (scope, key, _hash(request), _canonical(result), _now()))

    def _audit(self, task_id: str | None, action: str, payload: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO stage0_audit_event VALUES (?, ?, ?, ?, ?, ?)", (_id("audit"), task_id, action, _canonical(payload), self.data_identity, _now()))

from scripts.core.production.domain_boundary_lifecycle import attach_core_methods as _attach_domain_boundary_methods
_attach_domain_boundary_methods(Stage0ContentProductionCore)
from scripts.core.production.cold_start_completion import attach_core_methods as _attach_cold_start_completion_methods
_attach_cold_start_completion_methods(Stage0ContentProductionCore)
