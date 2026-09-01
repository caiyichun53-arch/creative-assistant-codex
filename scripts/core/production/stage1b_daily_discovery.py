"""Stage 1B controlled daily candidate discovery.

Only already-recorded, real source facts may enter this path.  A user selection
creates its formal topic and immediately starts its configured research plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH,
    FormalSkillValidationError,
    apply_binding,
    parse_model_json,
    preprocess_formal_skill_input,
    validate_payload,
    validate_source_to_topic_output_semantics,
)
from scripts.core.production.business_runtime_guard import enforce_atomic_skill_runtime_guard
from scripts.core.production.stage0_content_core import (
    DataIdentityError,
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.business_data.daily_source_acquisition import DailyDiscoverySourceAcquirer, validate_hotspot_collection_contract
from scripts.core.business_data.run_domain_search import (
    TAG_CANDIDATE_LIKE_FLOOR,
    load_sources_yaml,
    select_tags_due_for_search,
    seed_active_tags_from_sources_yaml,
    validate_domain_search_execution_contract,
)
from scripts.core.business_data.domain_labels import (
    formal_domain_labels,
    get_discovery_policy,
    get_domain_pack,
    hotspot_global_risk_block_terms,
)
from scripts.core.business_data.domain_boundaries import require_frozen_production_boundary
from scripts.core.external_adapters import LocalMediaCrawlerExecutor, LocalTrendRadarExecutor


EXECUTION_MODES = ("test_isolated", "real_daily_validation", "production_daily")
SOURCE_TYPES = (
    "hotspot",
    "daily_competitor_content",
    "historical_high_signal",
    "tag_discovery",
    "question_expansion",
    "saved_user_direction",
)
DAILY_REPORT_SOURCE_TYPES = (
    "historical_high_signal",
    "tag_discovery",
    "question_expansion",
    "saved_user_direction",
)
DAILY_SOURCE_VALIDITY_HOURS = 72
SOURCE_READ_LIMIT = 6
DISCOVERY_PROMPT_VERSION = "source_to_topic.prompt.v2"
DISCOVERY_SKILL_VERSION = "source_to_topic.skill.v1.2.0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOMAIN_PACK_DIR = REPOSITORY_ROOT / "config" / "domain_packs"
ORIGINALITY_RELATIONSHIPS = frozenset({"same_topic_original_reconstruction", "problem_expansion", "independent_research"})
HOTSPOT_DOMAIN_BRIDGE_TYPES = frozenset({
    "direct_object", "mechanism", "audience_impact", "cultural_mapping", "downstream_effect",
})
HOTSPOT_DOMAIN_FITS = frozenset({"core", "adjacent"})
HOTSPOT_PRIMARY_LENSES = frozenset({
    "原因解释", "普通人关系", "反向视角", "局部细节", "背景补充", "后续推演",
})
SETTINGS_PATH = ROOT / "config" / "external_collection.yaml"


class DailyDiscoveryValidationError(StateTransitionError):
    pass


class ExternalIntelligenceRequired(StateTransitionError):
    """The current Core step is waiting for an outside intelligent executor."""

    def __init__(self, task: dict[str, Any]) -> None:
        super().__init__("current business step requires external intelligent execution")
        self.task = task


@dataclass(frozen=True)
class ExternalIntelligenceReceipt:
    model_run_id: str

    @property
    def envelope_version_id(self) -> str:
        """Keep the existing downstream record reference shape."""
        return self.model_run_id


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:20]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DailyDiscoveryValidationError(f"candidate {key} must be non-empty text")
    return value.strip()


def _required_natural_chinese(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key)
    han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", value))
    if han_count < 4:
        raise DailyDiscoveryValidationError(f"candidate {key} must be natural Chinese for user display")
    return value


_WEAK_INFORMATION_INCREMENT_MARKERS = (
    "暂无具体增量",
    "无具体事实",
    "未提供具体",
    "当前输入材料未提供",
    "源材料仅为问题线索",
    "无法提供具体解释",
    "需进一步研究",
    "需要研究并解释",
    "答案需要通过制作前研究",
    "需正式研究",
)


def _angle_item(angle_discovery: dict[str, Any], key: str) -> dict[str, Any]:
    value = angle_discovery.get(key)
    return value if isinstance(value, dict) else {}


def _angle_found(angle_discovery: dict[str, Any], key: str) -> bool:
    return _angle_item(angle_discovery, key).get("found") is True


def _information_increment_score(
    judgement: dict[str, Any], angle_discovery: dict[str, Any]
) -> tuple[int, str]:
    content_increment = str(judgement.get("content_increment") or "").strip()
    angle = _angle_item(angle_discovery, "content_increment_angle")
    angle_text = " ".join(str(angle.get(key) or "") for key in ("direction", "reason"))
    evidence_text = f"{content_increment} {angle_text}".strip()
    if not content_increment:
        return 0, "没有内容增量说明，不能为信息增量加分"
    if not _angle_found(angle_discovery, "content_increment_angle"):
        return 1, "已明确标记为没有当前材料支持的具体增量，只保留最低分"
    if any(marker in evidence_text for marker in _WEAK_INFORMATION_INCREMENT_MARKERS):
        return 2, "角度存在，但当前材料明确说明具体事实或解释仍需研究，不能按已有增量高分处理"
    return 4, "当前材料已给出可复用的事实、机制、比较或解释方向"


def _candidate_assessment_from_judgement(
    judgement: dict[str, Any], *, expires_at: str | None, supporting_material_count: int = 0
) -> tuple[dict[str, int], dict[str, str]]:
    """Turn the frozen source-to-topic judgement into visible, bounded ranking evidence; it never selects a topic."""
    angle_discovery = judgement.get("angle_discovery") if isinstance(judgement.get("angle_discovery"), dict) else {}
    audience_found = _angle_found(angle_discovery, "audience_relevance_angle")
    distinct = _angle_item(angle_discovery, "distinct_angle")
    problem_found = _angle_found(angle_discovery, "problem_angle")
    tension_found = _angle_found(angle_discovery, "tension_angle")
    information_increment_score, information_increment_reason = _information_increment_score(
        judgement, angle_discovery
    )
    audience_relation = str(judgement.get("audience_relation") or "").strip()
    topic_angle = str(judgement.get("topic_angle") or "").strip()
    core_question = str(judgement.get("core_question") or "").strip()

    # These fields are already required or effectively guaranteed by the
    # qualification/candidate contract. A present field is a middle baseline,
    # not proof of strong demand or focus.
    demand_score = 3 if audience_relation and audience_found else 2 if audience_relation else 0
    question_score = 3 if core_question and problem_found else 2 if core_question else 0
    expression_score = 0 if not topic_angle else 4 if tension_found else 2
    expression_score = min(5, expression_score + min(2, supporting_material_count))
    scores = {
        "demand_strength": demand_score,
        "information_increment": information_increment_score,
        "expression_pull": expression_score,
        "distinct_angle": 4 if _angle_found(angle_discovery, "distinct_angle") else 2,
        "question_focus": question_score,
        "timeliness": 5 if expires_at else 2,
    }
    reasons = {
        "demand_strength": (
            f"{audience_relation or 'no audience relation supplied'}; "
            f"audience-relevance angle found={audience_found}; presence is treated as a baseline, not strong demand"
        ),
        "information_increment": (
            f"{str(judgement.get('content_increment') or 'no information increment supplied')}; "
            f"{information_increment_reason}"
        ),
        "expression_pull": (
            f"{topic_angle or 'no topic angle supplied'}; "
            f"tension angle found={tension_found}; same-angle supporting materials: {supporting_material_count}"
        ),
        "distinct_angle": str(distinct.get("reason") or judgement.get("topic_angle") or "no distinct-angle evidence supplied"),
        "question_focus": (
            f"{core_question or 'no core question supplied'}; "
            f"problem angle found={problem_found}; a passed candidate gets only a focus baseline"
        ),
        "timeliness": "source has an explicit validity window" if expires_at else "no explicit time window; only a low default weight is allowed",
    }
    return scores, reasons


def validate_candidate_judgement(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DailyDiscoveryValidationError("candidate judgement must be an object")
    forbidden_fields = {"score", "rank", "weight", "recommendation_score", "quality_rank"}
    if forbidden_fields & set(payload):
        raise DailyDiscoveryValidationError("candidate judgement must not contain business-ranking fields")
    outcome = _required_text(payload, "outcome")
    if outcome == "no_candidate":
        if set(payload) != {"outcome", "reason"}:
            raise DailyDiscoveryValidationError("zero-candidate output may contain only outcome and reason")
        return {"outcome": outcome, "reason": _required_text(payload, "reason")}
    if outcome != "candidate":
        raise DailyDiscoveryValidationError("candidate outcome is unsupported")
    normalized = {
        key: _required_natural_chinese(payload, key)
        for key in ("title", "core_question", "why_attention", "new_angle", "material_readiness", "risk_limits")
    }
    relation = _required_text(payload, "originality_relation")
    if relation not in ORIGINALITY_RELATIONSHIPS:
        raise DailyDiscoveryValidationError("candidate originality_relation is unsupported")
    normalized["originality_relation"] = relation
    normalized["outcome"] = outcome
    return normalized


def validate_hotspot_opportunity_judgement(
    payload: dict[str, Any], *, enabled_domains: tuple[str, ...]
) -> dict[str, Any]:
    """Validate one event's zero-or-many domain opportunities without ranking them."""
    if not isinstance(payload, dict):
        raise DailyDiscoveryValidationError("hotspot opportunity judgement must be an object")
    if set(payload) != {"outcome", "candidates", "reason"}:
        raise DailyDiscoveryValidationError("hotspot opportunity output has unsupported fields")
    if {"score", "rank", "weight", "recommendation_score", "quality_rank"} & set(payload):
        raise DailyDiscoveryValidationError("hotspot opportunity output must not contain ranking fields")
    outcome = _required_text(payload, "outcome")
    reason = _required_natural_chinese(payload, "reason")
    candidates = payload["candidates"]
    if not isinstance(candidates, list):
        raise DailyDiscoveryValidationError("hotspot opportunity candidates must be an array")
    if outcome == "no_candidate":
        if candidates:
            raise DailyDiscoveryValidationError("zero-candidate outcome must not include candidates")
        return {"outcome": outcome, "candidates": [], "reason": reason}
    if outcome != "candidates" or not candidates or len(candidates) > 10:
        raise DailyDiscoveryValidationError("hotspot opportunity outcome is invalid")
    required = {
        "domain_label", "theme_conflict", "domain_bridge", "audience_problem",
        "domain_fit", "fit_reason", "primary_lens", "candidate_topic", "core_question",
        "audience_relation", "content_increment", "topic_angle", "verification_gap",
        "trendradar_material_refs", "timeliness", "risks", "user_review_reason",
    }
    normalized: list[dict[str, Any]] = []
    seen_domains: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict) or set(item) != required:
            raise DailyDiscoveryValidationError("hotspot candidate fields are invalid")
        domain_label = _required_text(item, "domain_label")
        if domain_label not in enabled_domains:
            raise DailyDiscoveryValidationError("hotspot candidate uses a domain not enabled for this run")
        if domain_label in seen_domains:
            raise DailyDiscoveryValidationError("hotspot event can create at most one candidate per domain")
        domain_bridge = item["domain_bridge"]
        if not isinstance(domain_bridge, dict) or set(domain_bridge) != {"type", "reason"}:
            raise DailyDiscoveryValidationError("hotspot candidate domain_bridge is invalid")
        bridge_type = _required_text(domain_bridge, "type")
        if bridge_type not in HOTSPOT_DOMAIN_BRIDGE_TYPES:
            raise DailyDiscoveryValidationError("hotspot candidate has an unsupported domain bridge type")
        domain_fit = _required_text(item, "domain_fit")
        if domain_fit not in HOTSPOT_DOMAIN_FITS:
            raise DailyDiscoveryValidationError("hotspot candidate has an unsupported domain fit")
        primary_lens = _required_natural_chinese(item, "primary_lens")
        if primary_lens not in HOTSPOT_PRIMARY_LENSES:
            raise DailyDiscoveryValidationError("hotspot candidate has an unsupported primary lens")
        candidate = {
            key: _required_natural_chinese(item, key)
            for key in required - {
                "domain_label", "domain_bridge", "domain_fit", "primary_lens",
                "trendradar_material_refs", "risks",
            }
        }
        bridge_reason = _required_natural_chinese(domain_bridge, "reason")
        refs, risks = item["trendradar_material_refs"], item["risks"]
        # This is format repair only: a single supplied reference or risk keeps
        # exactly the same meaning, but is normalized into the contract's list
        # shape before semantic validation.  It must never invent or remove one.
        if isinstance(refs, str) and refs.strip():
            refs = [refs]
        if isinstance(risks, str) and risks.strip():
            risks = [risks]
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref.strip() for ref in refs):
            raise DailyDiscoveryValidationError("hotspot candidate must cite TrendRadar material")
        if not isinstance(risks, list) or not all(isinstance(risk, str) and risk.strip() for risk in risks):
            raise DailyDiscoveryValidationError("hotspot candidate risks must be a string array")
        seen_domains.add(domain_label)
        normalized.append({
            "domain_label": domain_label,
            **candidate,
            "domain_bridge": {"type": bridge_type, "reason": bridge_reason},
            "domain_fit": domain_fit,
            "primary_lens": primary_lens,
            "trendradar_material_refs": [ref.strip() for ref in refs],
            "risks": [risk.strip() for risk in risks],
        })
    return {"outcome": outcome, "candidates": normalized, "reason": reason}


def parse_candidate_judgement_output(output_text: str) -> dict[str, Any]:
    """Parse one JSON object, tolerating only a single exact JSON code fence."""
    text = output_text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return validate_candidate_judgement(json.loads(text))


def candidate_rejection(domain_label: str, judgement: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Apply deterministic post-model domain and material gates before candidate storage."""
    if judgement.get("topic_status") not in {"generated", "generated_good_candidate", "valid_but_weak", "needs_review"}:
        return None
    policy = get_discovery_policy(domain_label)
    candidate_text = "\n".join(
        str(judgement.get(key) or "")
        for key in ("candidate_topic", "core_question", "audience_relation", "content_increment", "topic_angle")
    ).casefold()
    exclude_terms = tuple(str(term) for term in policy.get("exclude_terms", []))
    matched_terms = sorted({term for term in exclude_terms if term.casefold() in candidate_text})
    if matched_terms:
        return "candidate_outside_domain_policy", {"matched_terms": matched_terms}
    material_readiness = "；".join(str(item) for item in judgement.get("material_gaps", []))
    insufficient_markers = ("材料严重不足", "事实无法确认", "无法确认事实", "无法核实事实")
    matched_markers = [marker for marker in insufficient_markers if marker in material_readiness]
    if matched_markers:
        return "candidate_material_insufficient", {"matched_markers": matched_markers}
    return None


def hotspot_candidate_rejection(domain_label: str, candidate: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Apply a domain boundary after a hotspot has been understood, never from its raw title."""
    policy = get_discovery_policy(domain_label)
    candidate_text = "\n".join(
        str(value or "")
        for value in (
            candidate.get("theme_conflict"),
            candidate.get("audience_problem"),
            candidate.get("fit_reason"),
            candidate.get("candidate_topic"),
            candidate.get("core_question"),
            candidate.get("audience_relation"),
            candidate.get("content_increment"),
            candidate.get("topic_angle"),
            (candidate.get("domain_bridge") or {}).get("reason") if isinstance(candidate.get("domain_bridge"), dict) else "",
        )
    ).casefold()
    exclude_terms = tuple(str(term) for term in policy.get("exclude_terms", []))
    matched_terms = sorted({term for term in exclude_terms if term.casefold() in candidate_text})
    if matched_terms:
        return "candidate_outside_domain_policy", {"matched_terms": matched_terms}
    return None


def daily_discovery_prompt(input_payload: dict[str, Any]) -> str:
    return (
        "你只能根据一条已通过确定性筛选的受控来源，作有限的候选判断。不得搜索、抓取、下载、转写、分析视频，"
        "也不得声称受控输入之外的事实。来源仅是发现线索，绝不是研究证据；不得创建正式选题。"
        "只返回一个裸 JSON 对象，不要使用 Markdown 代码块或附加说明。若不足以形成候选，只返回 outcome=no_candidate 和非空中文 reason。"
        "若形成候选，必须返回 outcome=candidate 以及 title、core_question、why_attention、new_angle、"
        "material_readiness、risk_limits、originality_relation。面向用户的 title（标题）、core_question（核心问题）、"
        "why_attention（价值）和 risk_limits（风险）必须是自然中文；原始来源标题可保留原语言。"
        "必须遵守受控输入中的 domain_policy；若来源或拟议角度属于其排除类型，必须返回 no_candidate，"
        "不得通过改写成社会公平、公众情绪或警示价值来包装。若材料严重不足或事实无法确认，也必须返回 no_candidate。"
        "originality_relation 只能是 same_topic_original_reconstruction、problem_expansion、independent_research。"
        "不得返回 score、rank、weight、recommendation_score 或 quality_rank。必须说明不确定性和材料缺口。\n\n"
        f"受控输入：{_canonical(input_payload)}"
    )


def build_production_source_acquirer(
    core: Stage0ContentProductionCore,
    *,
    source_types: tuple[str, ...],
) -> DailyDiscoverySourceAcquirer:
    """Build only the collectors required by this source-specific run."""
    if core.data_identity != "production":
        raise StateTransitionError("production source acquisition requires production data identity")
    settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    hotspot_config = settings.get("hotspot_collection") or {}
    domain_search_config = settings.get("domain_search") or {}
    trendradar = None
    mediacrawler = None
    if "hotspot" in source_types:
        validate_hotspot_collection_contract(hotspot_config)
        required = ("project_dir", "executable", "normalized_json_path")
        missing = [key for key in required if not str(hotspot_config.get(key) or "").strip()]
        if missing:
            raise StateTransitionError(f"TrendRadar configuration is incomplete: {', '.join(missing)}")
        trendradar = LocalTrendRadarExecutor(
            project_dir=Path(hotspot_config["project_dir"]),
            executable=Path(hotspot_config["executable"]),
            command_args=tuple(str(value) for value in hotspot_config.get("args", [])),
            normalized_json_path=Path(hotspot_config["normalized_json_path"]),
            timeout_seconds=int(hotspot_config.get("timeout_seconds", 180)),
            data_identity=core.data_identity,
        )
    if "tag_discovery" in source_types:
        validate_domain_search_execution_contract(domain_search_config)
        mediacrawler = LocalMediaCrawlerExecutor(data_identity=core.data_identity)
    return DailyDiscoverySourceAcquirer(
        conn=core.conn,
        trendradar_executor=trendradar,
        mediacrawler_executor=mediacrawler,
        hotspot_config=hotspot_config,
        domain_search_config=domain_search_config,
    )


class Stage1BDailyDiscoveryService:
    """The controlled real-source -> candidate snapshot -> user decision path."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        gateway: Any | None = None,
        source_acquirer: DailyDiscoverySourceAcquirer | None = None,
        source_to_topic_contract: FormalSkillContract | None = None,
        hotspot_to_opportunity_contract: FormalSkillContract | None = None,
        model_route: Any | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ):
        if gateway is not None or model_route is not None:
            raise StateTransitionError(
                "formal discovery must use an external executor; direct model gateways and routes are not supported"
            )
        self.core = core
        self.external_executor = external_executor
        self.source_acquirer = source_acquirer
        self.source_to_topic_contract = source_to_topic_contract or FormalSkillContract.from_runtime_skill("source_to_topic")
        self.hotspot_to_opportunity_contract = hotspot_to_opportunity_contract or FormalSkillContract.from_yaml(HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH)

    def _feed_unregistered_account_observation(
        self,
        *,
        domain_label: str,
        source: dict[str, Any],
        source_version_id: str,
        qualified: bool,
    ) -> dict[str, Any] | None:
        """Feed real tag-search videos into the shared thirty-day account rule."""
        if source.get("source_type") != "tag_discovery":
            return None
        payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
        platform = str(payload.get("platform") or "").strip()
        account_platform_id = str(payload.get("account_platform_id") or "").strip()
        platform_item_id = str(payload.get("platform_item_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        url = str(payload.get("url") or "").strip()
        source_time = str(source.get("source_time") or "").strip()
        if not all((platform, account_platform_id, platform_item_id, title, url, source_time)):
            return {
                "status": "skipped_missing_source_identity",
                "source_object_id": source.get("source_object_id"),
            }
        try:
            observed_at = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        except ValueError:
            return {
                "status": "skipped_invalid_source_time",
                "source_object_id": source.get("source_object_id"),
            }
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observation = self.core.observe_unregistered_account(
            domain_label=domain_label,
            account_ref=f"{platform}:{account_platform_id}",
            account_display_name=str(payload.get("account_name") or account_platform_id).strip(),
            video_ref=f"{platform}:{platform_item_id}",
            video_url=url,
            video_title=title,
            qualified=qualified,
            source_ref={
                "source_version_id": source_version_id,
                "source_type": source["source_type"],
                "source_object_id": source["source_object_id"],
                "formal_source": payload.get("formal_source"),
                "discovery_tag": payload.get("discovery_tag"),
            },
            observed_at=observed_at.isoformat(),
        )
        return {"status": "observed", **observation}

    def run_daily_discovery(
        self,
        *,
        discovery_date: str,
        actor: str,
        idempotency_key: str,
        execution_mode: str,
        daily_run_id: str | None = None,
        now: datetime | None = None,
        domains: tuple[str, ...] | None = None,
        source_types: tuple[str, ...] = DAILY_REPORT_SOURCE_TYPES,
        reuse_hotspot_discovery_run_id: str | None = None,
        upstream_failures: tuple[dict[str, Any], ...] = (),
        resume_source_object_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        approved_domains = formal_domain_labels()
        requested_domains = tuple(domains) if domains is not None else tuple(sorted(approved_domains))
        requested_source_types = tuple(source_types)
        if not requested_domains or len(set(requested_domains)) != len(requested_domains):
            raise DailyDiscoveryValidationError("daily discovery requires one or more distinct approved domains")
        if any(domain_label not in approved_domains for domain_label in requested_domains):
            raise DailyDiscoveryValidationError("daily discovery received an unapproved domain")
        if (
            not requested_source_types
            or len(set(requested_source_types)) != len(requested_source_types)
            or any(source_type not in SOURCE_TYPES for source_type in requested_source_types)
        ):
            raise DailyDiscoveryValidationError("daily discovery requires distinct supported source types")
        if execution_mode not in EXECUTION_MODES:
            raise DailyDiscoveryValidationError("daily discovery requires an explicit execution mode")
        if execution_mode == "production_daily" and not str(daily_run_id or "").strip():
            raise DailyDiscoveryValidationError("production daily discovery requires a formal daily run")
        if execution_mode == "real_daily_validation" and len(requested_source_types) != 1:
            raise DailyDiscoveryValidationError("real daily validation requires exactly one source type for a complete path")
        if reuse_hotspot_discovery_run_id is not None and set(requested_source_types) != {"hotspot"}:
            raise DailyDiscoveryValidationError("a reused hotspot batch can only run the hotspot conversion path")
        if execution_mode == "production_daily" and set(requested_source_types) != set(DAILY_REPORT_SOURCE_TYPES):
            raise DailyDiscoveryValidationError("production_daily must run the complete candidate-source set; daily competitor content is tracking-only")
        if self.core.data_identity == "production" and execution_mode == "test_isolated":
            raise DataIdentityError("production discovery data cannot use test_isolated mode")
        if (
            execution_mode != "test_isolated"
            and {"hotspot", "tag_discovery"} & set(requested_source_types)
            and self.source_acquirer is None
        ):
            raise DailyDiscoveryValidationError("the selected live external source requires its Runtime acquirer")
        now = now or datetime.now(timezone.utc)
        request = {
            "discovery_date": discovery_date,
            "actor": actor,
            "domains": list(requested_domains),
            "execution_mode": execution_mode,
            "daily_run_id": str(daily_run_id or "").strip() or None,
            "source_types": list(requested_source_types),
            "reuse_hotspot_discovery_run_id": reuse_hotspot_discovery_run_id,
            "upstream_failures": list(upstream_failures),
            "resume_source_object_ids": list(resume_source_object_ids),
        }
        replay = self.core.find_command_replay("stage1b_execute_daily_discovery", idempotency_key, request)
        if replay:
            return {**replay, "replayed": True}
        run = self.core.create_discovery_run(
            discovery_date=discovery_date,
            actor=actor,
            execution_mode=execution_mode,
            domains=requested_domains,
            idempotency_key=f"{idempotency_key}:run",
            daily_run_id=daily_run_id,
        )
        summary: dict[str, Any] = {
            "run_id": run["run_id"],
            "discovery_date": discovery_date,
            "execution_mode": execution_mode,
            "source_types": list(requested_source_types),
            "domains": {},
            "filtered": {},
            "source_readiness": {},
            "source_evidence": {},
            "acquisition": {"status": "not_run", "reason": "test_isolated may use preloaded fixture sources"},
            "technical_failures": len(upstream_failures),
            "upstream_failures": list(upstream_failures),
            "failure_details": [],
            "topic_catalog": {},
            "account_observation": {"observed": 0, "reviews_ready": [], "skipped": []},
        }
        daily_since = (now - timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()
        deadline: float | None = None
        hotspot_discovery_run_id = reuse_hotspot_discovery_run_id or (
            run["run_id"]
            if self.source_acquirer is not None and "hotspot" in requested_source_types
            else None
        )
        lifecycle_status, failure_reason = "completed", None
        try:
            if self.source_acquirer is not None and "hotspot" in requested_source_types:
                try:
                    if reuse_hotspot_discovery_run_id is not None:
                        hotspot = {
                            "status": "reused",
                            "reuse": self.core.reusable_hotspot_batch_status(
                                discovery_run_id=reuse_hotspot_discovery_run_id
                            ),
                            "reason": "a successful temporary collection is being reused; no TrendRadar collection was started",
                        }
                    else:
                        hotspot = self.source_acquirer.collect_hotspots(
                            discovery_run_id=run["run_id"], now=now, deadline_monotonic=deadline
                        )
                        if hotspot["status"] == "completed" and int(hotspot.get("item_count") or 0) > 0:
                            hotspot["retention"] = {
                                "status": "raw_batch_retained",
                                "automatic_cleanup": False,
                            }
                    summary["acquisition"] = {
                        "execution_order": [
                            "trendradar_hotspot", "hotspot_conversion", "daily_competitor_tracking",
                            "historical_high_signal", "tag_search", "tag_conversion",
                            "question_expansion_from_formal_parent", "saved_user_direction",
                        ],
                        "hotspot": hotspot,
                        "tag_search": {},
                    }
                    hotspot_failed = hotspot["status"] not in {"completed", "completed_with_failures", "reused"}
                    if hotspot_failed or hotspot["status"] == "completed_with_failures":
                        summary["technical_failures"] += 1
                except Exception as exc:
                    summary["acquisition"] = {
                        "execution_order": ["trendradar_hotspot"], "status": "failed", "reason": str(exc), "retry": "forbidden", "tag_search": {}
                    }
                    summary["technical_failures"] += 1
            elif "hotspot" not in requested_source_types:
                summary["acquisition"] = {"status": "not_selected", "reason": "hotspot is outside this source-specific run", "tag_search": {}}
            hotspot_summary = self._process_hotspots_once(
                run_id=run["run_id"],
                requested_domains=requested_domains,
                requested_source_types=requested_source_types,
                daily_since=daily_since,
                hotspot_discovery_run_id=hotspot_discovery_run_id,
                now=now,
                execution_mode=execution_mode,
                idempotency_key=idempotency_key,
                deadline_monotonic=deadline,
            )
            summary["hotspot_audit"] = hotspot_summary["hotspot_audit"]
            if hotspot_summary["technical_failure"] is not None:
                summary["technical_failures"] += 1
                lifecycle_status, failure_reason = "failed", hotspot_summary["technical_failure"]
            if hotspot_discovery_run_id is not None and execution_mode == "production_daily":
                summary["acquisition"].setdefault("hotspot", {})["raw_batch_status"] = (
                    "retained_after_conversion; automatic cleanup is disabled"
                )
            elif hotspot_discovery_run_id is not None:
                summary["acquisition"].setdefault("hotspot", {})["raw_batch_status"] = (
                    "latest_reusable_batch; it is replaced only after a later successful collection"
                )
            for domain_label in (() if lifecycle_status == "failed" else requested_domains):
                if "tag_discovery" in requested_source_types:
                    sources_config = load_sources_yaml(DOMAIN_PACK_DIR / f"{domain_label}.yaml")
                    seed_result = seed_active_tags_from_sources_yaml(self.core.conn, sources_config)
                    summary["topic_catalog"][domain_label] = {
                        "configured_topics": list(sources_config["active_tags"]),
                        "registration": seed_result,
                        "note": "configured domain topics are registered before this run's external topic search; the audit packet records which ones were actually searched",
                    }
                source_evidence: dict[str, dict[str, Any]] = {
                    source_type: {"status": "not_available", "reason": "no_source_available", "source_refs": []}
                    for source_type in DAILY_REPORT_SOURCE_TYPES
                    if source_type in requested_source_types
                }
                if "hotspot" in requested_source_types:
                    source_evidence["hotspot"] = hotspot_summary["source_evidence"][domain_label]
                summary["source_readiness"][domain_label] = self.core.discovery_source_readiness(
                    domain_label=domain_label,
                    daily_since=daily_since,
                    hotspot_discovery_run_id=hotspot_discovery_run_id,
                )
                loaded_sources = [
                    source for source in self.core.load_real_discovery_sources(
                        domain_label=domain_label,
                        daily_since=daily_since,
                        per_source_limit=SOURCE_READ_LIMIT,
                        hotspot_discovery_run_id=hotspot_discovery_run_id,
                        resume_source_object_ids=resume_source_object_ids,
                    )
                    if source["source_type"] in requested_source_types and source["source_type"] != "hotspot"
                ]
                limited_sources: list[dict[str, Any]] = []
                for source in loaded_sources:
                    limited_sources.append(source)
                    if source["source_type"] in source_evidence:
                        source_evidence[source["source_type"]] = {
                            "status": "no_candidate",
                            "reason": "source_seen_pending_candidate_judgement",
                            "source_refs": [{
                                "source_type": source["source_type"],
                                "source_object_id": source["source_object_id"],
                                "source_object_version": source["source_object_version"],
                            }],
                        }
                loaded_sources = limited_sources

                def ordered_sources():  # type: ignore[no-untyped-def]
                    nonlocal lifecycle_status, failure_reason
                    if self.source_acquirer is None:
                        yield from loaded_sources
                        return
                    yield from (source for source in loaded_sources if source["source_type"] != "tag_discovery")
                    if "tag_discovery" not in requested_source_types:
                        return
                    if deadline is not None and time.monotonic() >= deadline:
                        lifecycle_status, failure_reason = "timed_out", "batch deadline reached before tag search; no request was sent or retried"
                        return
                    try:
                        tag_result = self.source_acquirer.search_tags(
                            discovery_run_id=run["run_id"],
                            domain=domain_label,
                            now=now,
                            deadline_monotonic=deadline,
                            allowed_tag_ids=(
                                self.core.current_domain_tag_ids(domain_label=domain_label)
                                if callable(getattr(self.core, "current_domain_tag_ids", None))
                                else None
                            ),
                        )
                    except Exception as exc:
                        tag_result = {"status": "failed", "reason": str(exc), "failed": 1, "retry": "forbidden"}
                    summary["acquisition"].setdefault("tag_search", {})[domain_label] = tag_result
                    tag_failures = int(tag_result.get("failed", 0))
                    summary["technical_failures"] += tag_failures
                    if tag_result.get("status") == "timed_out":
                        lifecycle_status, failure_reason = "timed_out", "batch deadline reached during tag search; no request was retried"
                        return
                    refreshed = self.core.load_real_discovery_sources(
                        domain_label=domain_label,
                        daily_since=daily_since,
                        per_source_limit=SOURCE_READ_LIMIT,
                        hotspot_discovery_run_id=hotspot_discovery_run_id,
                        resume_source_object_ids=resume_source_object_ids,
                    )
                    yield from (source for source in refreshed if source["source_type"] == "tag_discovery")

                filtered: dict[str, int] = dict(hotspot_summary["filtered"][domain_label])
                candidate_count = hotspot_summary["candidate_counts"][domain_label]
                sources_read = hotspot_summary["sources_read"][domain_label]
                for index, source in enumerate(ordered_sources()):
                    sources_read += 1
                    if deadline is not None and time.monotonic() >= deadline:
                        lifecycle_status, failure_reason = "timed_out", "batch deadline reached before the next source; no request was retried"
                        break
                    outcome, reason_code, detail = self._deterministic_filter(
                        domain_label=domain_label,
                        source=source,
                        now=now,
                        allow_processed_source=source["source_object_id"] in set(resume_source_object_ids),
                    )
                    source_result = self.core.record_discovery_source(
                        run_id=run["run_id"], domain_label=domain_label, source_type=source["source_type"],
                        source_object_id=source["source_object_id"], source_object_version=source["source_object_version"],
                        source_time=source["source_time"], expires_at=self._expires_at(source, now=now), payload=source["payload"],
                        idempotency_key=f"{idempotency_key}:{domain_label}:source:{index}",
                    )
                    self.core.record_discovery_filter(
                        source_version_id=source_result["source_version_id"], outcome=outcome, reason_code=reason_code, detail=detail,
                        idempotency_key=f"{idempotency_key}:{domain_label}:filter:{index}",
                    )
                    account_observation = self._feed_unregistered_account_observation(
                        domain_label=domain_label,
                        source=source,
                        source_version_id=source_result["source_version_id"],
                        qualified=outcome == "eligible",
                    )
                    if account_observation is not None:
                        if account_observation["status"] == "observed":
                            summary["account_observation"]["observed"] += 1
                            if account_observation.get("human_confirmation_required"):
                                review_id = account_observation.get("account_review_id")
                                if review_id and review_id not in summary["account_observation"]["reviews_ready"]:
                                    summary["account_observation"]["reviews_ready"].append(review_id)
                        else:
                            summary["account_observation"]["skipped"].append(account_observation)
                    if outcome != "eligible":
                        filtered[reason_code] = filtered.get(reason_code, 0) + 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = reason_code
                        continue
                    experience_cards = self.core.list_active_experiences(
                        domain_label=domain_label,
                        limit=3,
                    )
                    current_boundary_reader = getattr(
                        self.core, "current_domain_boundary_frozen", None
                    )
                    current_boundary = (
                        current_boundary_reader(domain_label=domain_label)
                        if callable(current_boundary_reader)
                        else None
                    )
                    if current_boundary is False:
                        raise DailyDiscoveryValidationError(
                            "the current domain activation has no frozen production boundary"
                        )
                    assembly_payload = self._assembly_payload(
                        run_id=run["run_id"],
                        domain_label=domain_label,
                        source=source,
                        source_version_id=source_result["source_version_id"],
                        experience_cards=experience_cards,
                    )
                    assembly = self.core.create_discovery_input_assembly(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], payload=assembly_payload,
                        prompt_version=DISCOVERY_PROMPT_VERSION, skill_version=DISCOVERY_SKILL_VERSION,
                        idempotency_key=f"{idempotency_key}:{domain_label}:assembly:{index}",
                        external_execution=True,
                    )
                    model_result = None
                    try:
                        model_result, judgement = self._run_source_to_topic_skill(
                            run_id=run["run_id"],
                            source_version_id=source_result["source_version_id"],
                            assembly_id=assembly["assembly_id"],
                            input_payload=assembly_payload,
                        )
                    except ExternalIntelligenceRequired as exc:
                        summary["external_intelligence_required"] = {
                            "task_type": exc.task.get("task_type"),
                            "business_context": exc.task.get("business_context"),
                        }
                        summary["failure_details"].append({
                            "stage": "candidate_discovery",
                            "operation": "source_to_topic",
                            "status": "requires_external_intelligence",
                            "reason": str(exc),
                        })
                        return {
                            "run_id": run["run_id"],
                            "discovery_date": discovery_date,
                            "status": "requires_external_intelligence",
                            "execution_mode": execution_mode,
                            "source_types": list(requested_source_types),
                            "failure_reason": None,
                            "technical_failures": 0,
                            "failure_details": list(summary["failure_details"]),
                            "summary": summary,
                            "task": exc.task,
                        }
                    except (json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
                        failed_model_run_id = (
                            model_result.envelope_version_id if model_result is not None
                            else getattr(exc, "model_run_envelope_version_id", None)
                        )
                        raw_model_output = getattr(exc, "raw_model_output", None)
                        if failed_model_run_id is not None:
                            self.core.record_discovery_model_validation_failure(
                                model_run_id=failed_model_run_id, reason=str(exc), raw_model_output=raw_model_output,
                            )
                        self.core.record_discovery_source_failure(
                            run_id=run["run_id"],
                            source_version_id=source_result["source_version_id"],
                            model_run_id=failed_model_run_id,
                            failure_stage="model_output_validation", reason=str(exc), raw_model_output=raw_model_output,
                            idempotency_key=f"{idempotency_key}:{domain_label}:failure:{index}",
                        )
                        filtered["model_output_invalid"] = filtered.get("model_output_invalid", 0) + 1
                        summary["technical_failures"] += 1
                        summary["failure_details"].append({
                            "stage": "candidate_discovery",
                            "operation": "source_to_topic",
                            "source_type": source["source_type"],
                            "source_object_id": source["source_object_id"],
                            "failure_stage": "model_output_validation",
                            "reason": str(exc),
                        })
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "failed"
                            source_evidence[source["source_type"]]["reason"] = "model_output_invalid"
                        lifecycle_status, failure_reason = "failed", (
                            f"source_to_topic output was invalid for source_type={source['source_type']}; "
                            "batch stopped without retry"
                        )
                        break
                    if judgement["topic_status"] == "no_result":
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                            reason_code="model_returned_no_candidate",
                            detail={
                                "reason": judgement["no_result_reason"],
                                "material_gaps": judgement.get("material_gaps", []),
                                "source_constraints": judgement.get("source_constraints", []),
                            },
                            idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                        )
                        filtered["model_returned_no_candidate"] = filtered.get("model_returned_no_candidate", 0) + 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = "model_returned_no_candidate"
                        continue
                    rejection = candidate_rejection(domain_label, judgement)
                    if rejection is not None:
                        reason_code, detail = rejection
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"],
                            model_run_id=model_result.envelope_version_id, reason_code=reason_code, detail=detail,
                            idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                        )
                        filtered[reason_code] = filtered.get(reason_code, 0) + 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = reason_code
                        continue
                    candidate_payload = {
                        **judgement,
                        "title": judgement["candidate_topic"],
                        "why_attention": judgement["audience_relation"],
                        "new_angle": f"{judgement['topic_angle']}；{judgement['content_increment']}",
                        "material_readiness": "；".join(judgement.get("material_gaps", [])) or "来源转选题 Skill 未列出材料缺口；正式研究仍需独立补证。",
                        "risk_limits": "；".join(judgement.get("risks", [])) or "来源仅作发现线索，不作为正式研究证据。",
                        "originality_relation": "problem_expansion",
                        "hit_origin": source["payload"].get("hit_origin"),
                        "parent_source_ref": source["payload"].get("parent_source_ref"),
                        "normalized_title": _normalize_title(judgement["candidate_topic"]),
                        "normalized_source_title": _normalize_title(str(source["payload"]["title"])),
                        "domain": domain_label,
                        "qualification_material_refs": source["payload"].get("qualification_material_refs", []),
                        "qualification_checks": source["payload"].get("qualification_checks", {}),
                        "source_reference": {"source_version_id": source_result["source_version_id"], "source_type": source["source_type"], "source_object_id": source["source_object_id"], "source_object_version": source["source_object_version"], "source_time": source["source_time"], "url": source["payload"].get("url", "")},
                    }
                    related = self.core.find_related_discovery_candidates(
                        domain_label=domain_label, core_question=judgement["core_question"], topic_angle=judgement["topic_angle"],
                    )
                    if related["same_angle"]:
                        target_candidate = related["same_angle"][0]
                        support = self.core.record_candidate_support(
                            candidate_version_id=target_candidate, source_version_id=source_result["source_version_id"],
                            relation_reason="the same source-to-topic judgement produced the same core question and angle",
                        )
                        support_count = self.core.count_candidate_support_materials(
                            candidate_version_id=support["candidate_version_id"]
                        )
                        scores, reasons = _candidate_assessment_from_judgement(
                            judgement,
                            expires_at=self._expires_at(source, now=now),
                            supporting_material_count=support_count,
                        )
                        self.core.record_candidate_assessment(
                            candidate_version_id=support["candidate_version_id"],
                            dimension_scores=scores,
                            dimension_reasons=reasons,
                            assessed_by="source_to_topic_same_angle_material_reassessment",
                        )
                    else:
                        created = self.core.create_discovery_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                            candidate_id=f"candidate_{_stable_id({domain_label: source_result['source_version_id'], 'title': candidate_payload['normalized_title']})}",
                            payload=candidate_payload, idempotency_key=f"{idempotency_key}:{domain_label}:candidate:{index}",
                        )
                        for related_candidate in related["different_angle"]:
                            self.core.record_candidate_relation(
                                candidate_version_id=created["candidate_version_id"], related_candidate_version_id=related_candidate,
                                relation_kind="same_topic_different_angle", relation_reason="the same core question has a different frozen topic angle",
                            )
                        scores, reasons = _candidate_assessment_from_judgement(
                            judgement, expires_at=self._expires_at(source, now=now)
                        )
                        self.core.record_candidate_assessment(
                            candidate_version_id=created["candidate_version_id"], dimension_scores=scores,
                            dimension_reasons=reasons, assessed_by="source_to_topic_frozen_judgement",
                        )
                        candidate_count += 1
                    if source["source_type"] in source_evidence:
                        source_evidence[source["source_type"]]["status"] = "has_candidate"
                        source_evidence[source["source_type"]]["reason"] = "candidate_created"
                summary["domains"][domain_label] = {"sources_read": sources_read, "candidates": candidate_count}
                summary["source_readiness"][domain_label] = self.core.discovery_source_readiness(
                    domain_label=domain_label,
                    daily_since=daily_since,
                    hotspot_discovery_run_id=hotspot_discovery_run_id,
                )
                summary["filtered"][domain_label] = filtered
                summary["source_evidence"][domain_label] = source_evidence
                if lifecycle_status in {"timed_out", "failed"}:
                    break
        except KeyboardInterrupt:
            lifecycle_status, failure_reason = "interrupted", "batch interrupted; no uncertain request was retried"
        except Exception as exc:
            lifecycle_status = "failed"
            failure_reason = f"daily discovery execution failed: {type(exc).__name__}: {exc}"
            summary["technical_failures"] += 1
            summary["failure_details"].append({
                "stage": "candidate_discovery",
                "failure_stage": "execution",
                "reason": str(exc),
                "error_type": type(exc).__name__,
            })
        if lifecycle_status == "completed" and summary["technical_failures"]:
            lifecycle_status, failure_reason = "completed_with_failures", "one or more sources failed and were retained without retry"
        finalization = self.core.complete_discovery_run(
            run_id=run["run_id"], domains=requested_domains, lifecycle_status=lifecycle_status,
            failure_reason=failure_reason, idempotency_key=f"{idempotency_key}:snapshot",
        )
        result = {
            "run_id": run["run_id"],
            "discovery_date": discovery_date,
            "status": lifecycle_status,
            "execution_mode": execution_mode,
            "source_types": list(requested_source_types),
            "failure_reason": failure_reason,
            "technical_failures": int(summary["technical_failures"]),
            "failure_details": list(summary.get("failure_details") or []),
            "summary": summary,
        }
        self.core.record_completed_command(
            command="stage1b_execute_daily_discovery", idempotency_key=idempotency_key, request=request, task_id=run["run_id"],
            event="stage1b_daily_discovery_finalized", result={**finalization, "technical_failures": str(summary["technical_failures"])},
        )
        return result

    def view_daily_snapshot(
        self, *, run_id: str, domains: tuple[str, ...] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        approved_domains = formal_domain_labels()
        requested_domains = tuple(domains) if domains is not None else tuple(sorted(approved_domains))
        if not requested_domains or any(domain_label not in approved_domains for domain_label in requested_domains):
            raise DailyDiscoveryValidationError("snapshot view received an unapproved domain")
        return {
            domain_label: self.core.get_discovery_snapshot(run_id=run_id, domain_label=domain_label)
            for domain_label in requested_domains
        }

    def record_user_decision(self, *, candidate_version_id: str, decision: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        if decision == "selected":
            raise StateTransitionError("select through handoff_selected_candidate so the formal topic link is recorded")
        return self.core.record_discovery_decision(candidate_version_id=candidate_version_id, decision=decision, actor=actor, reason=reason, formal_topic_task_id=None, idempotency_key=idempotency_key)

    def handoff_selected_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        selected = self.core.select_discovery_candidate(
            candidate_version_id=candidate_version_id,
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=idempotency_key,
        )
        from scripts.core.production.stage1a_research_plan import (
            Stage1AResearchPlanService,
        )
        topic = self.core.get_artifact_payload(selected["topic_version_id"])["payload"]
        plan = Stage1AResearchPlanService(core=self.core).generate_research_plan(
            task_id=selected["task_id"],
            user_requirements=_canonical({
                "scope_or_requirement": topic.get("scope_or_requirement", ""),
                "material_gaps": topic.get("material_gaps", []),
                "risks": topic.get("risks", []),
            }),
            actor=actor,
            idempotency_key=f"{idempotency_key}:research-plan",
        )
        return {
            **selected,
            "research_plan_version_id": plan["node_version_id"],
            "research_plan_status": str(plan.get("status") or "awaiting_human_review"),
            **({"external_task": plan["task"]} if isinstance(plan.get("task"), dict) else {}),
        }

    def verify_stage1_production_closure(self) -> dict[str, str]:
        return self.core.latest_stage1_production_handoff()

    def preview_daily_review(self, *, domain_label: str, now: datetime | None = None) -> dict[str, Any]:
        """Read the next daily review packet without collecting, writing or calling a model."""
        if domain_label not in formal_domain_labels():
            raise DailyDiscoveryValidationError("daily review preview received an unapproved domain")
        now = now or datetime.now(timezone.utc)
        daily_since = (now - timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()
        sources_config = load_sources_yaml(DOMAIN_PACK_DIR / f"{domain_label}.yaml")
        current_tag_reader = getattr(self.core, "current_domain_tag_ids", None)
        current_tag_ids = (
            current_tag_reader(domain_label=domain_label)
            if callable(current_tag_reader)
            else None
        )
        due_topics = select_tags_due_for_search(
            self.core.conn,
            domain_label=domain_label,
            allowed_tag_ids=current_tag_ids,
        )
        if current_tag_ids is None:
            registered_topic_rows = self.core.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=? "
                "AND status='active' ORDER BY tag_id",
                (domain_label,),
            ).fetchall()
        elif not current_tag_ids:
            registered_topic_rows = []
        else:
            placeholders = ",".join("?" for _ in current_tag_ids)
            registered_topic_rows = self.core.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=? "
                "AND status='active' AND tag_id IN (" + placeholders + ") ORDER BY tag_id",
                (domain_label, *current_tag_ids),
            ).fetchall()
        registered_topics = [str(row["tag"]) for row in registered_topic_rows]
        sources = [
            source for source in self.core.load_real_discovery_sources(
                domain_label=domain_label,
                daily_since=daily_since,
                per_source_limit=SOURCE_READ_LIMIT,
            )
            if source["source_type"] in DAILY_REPORT_SOURCE_TYPES
        ]
        return {
            "review_kind": "pre_run_daily_review",
            "domain_label": domain_label,
            "generated_at": now.isoformat(),
            "hotspot_to_topic": "enabled_in_production_not_called_in_preview",
            "configured_topics": list(sources_config["active_tags"]),
            "registered_topics": registered_topics,
            "topics_due_for_external_search": [
                {"topic": row["tag"], "topic_id": row["tag_id"], "last_searched_at": row["last_searched_at"]}
                for row in due_topics
            ],
            "topics_pending_registration": [
                topic for topic in sources_config["active_tags"] if topic not in registered_topics
            ],
            "source_readiness": self.core.discovery_source_readiness(
                domain_label=domain_label, daily_since=daily_since
            ),
            "sources": [
                {
                    "source_type": source["source_type"],
                    "source_object_id": source["source_object_id"],
                    "source_object_version": source["source_object_version"],
                    "source_time": source["source_time"],
                    "source": source["payload"],
                    "rule_result": {
                        "outcome": outcome,
                        "reason": reason,
                        "detail": detail,
                    },
                    "model": "not_called_in_preview",
                }
                for source in sources
                for outcome, reason, detail in [
                    self._deterministic_filter(domain_label=domain_label, source=source, now=now)
                ]
            ],
        }


    def _process_hotspots_once(
        self,
        *,
        run_id: str,
        requested_domains: tuple[str, ...],
        requested_source_types: tuple[str, ...],
        daily_since: str,
        hotspot_discovery_run_id: str | None,
        now: datetime,
        execution_mode: str,
        idempotency_key: str,
        deadline_monotonic: float | None,
    ) -> dict[str, Any]:
        empty_evidence = {"status": "not_available", "reason": "hotspot_not_selected", "source_refs": []}
        result = {
            "candidate_counts": {domain: 0 for domain in requested_domains},
            "sources_read": {domain: 0 for domain in requested_domains},
            "filtered": {domain: {} for domain in requested_domains},
            "source_evidence": {domain: dict(empty_evidence) for domain in requested_domains},
            "hotspot_audit": {"events": [], "hard_excluded": 0, "selected_for_detail": 0, "not_selected_after_top_ten": 0},
            "technical_failure": None,
        }
        if "hotspot" not in requested_source_types:
            return result
        sources = self.core.load_hotspot_event_clusters(
            daily_since=daily_since,
            per_source_limit=None,
            hotspot_discovery_run_id=hotspot_discovery_run_id,
        )
        if not sources:
            for domain in requested_domains:
                result["source_evidence"][domain] = {"status": "no_candidate", "reason": "no_hotspot_event", "source_refs": []}
            return result
        storage_domain = requested_domains[0]
        eligible_sources: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for index, source in enumerate(sources):
            cluster = dict(source["payload"].get("hotspot_event_cluster") or {})
            audit_entry = {
                "event_id": cluster.get("cluster_id", source["source_object_id"]),
                "title": source["payload"].get("title", ""),
                "url": source["payload"].get("url", ""),
                "merged_sources": [
                    {"platform": item.get("channel", ""), "rank": item.get("rank"), "title": item.get("title", ""), "url": item.get("url", "")}
                    for item in cluster.get("merged_sources", [])
                ],
                "selection_signal": dict(cluster.get("selection_signal") or {}),
                "status": "hard_screening",
                "reason_code": "",
                "detail": {},
            }
            result["hotspot_audit"]["events"].append(audit_entry)
            outcome, reason_code, detail = self._deterministic_filter(
                domain_label=storage_domain, source=source, now=now
            )
            if (
                outcome == "excluded"
                and reason_code == "source_already_processed"
                and execution_mode == "real_daily_validation"
                and hotspot_discovery_run_id is not None
            ):
                # A user-authorized validation rerun deliberately reuses a
                # frozen hotspot batch after rules change.  It must reassess
                # the event rather than silently inherit an earlier result.
                outcome, reason_code, detail = "eligible", "eligible", {
                    "normalized_source_title": _normalize_title(str(source["payload"].get("title") or "")),
                    "reassessment": "explicit_real_daily_validation_reuse",
                }
            if outcome != "eligible":
                audit_entry.update({"status": "hard_excluded", "reason_code": reason_code, "detail": detail})
                result["hotspot_audit"]["hard_excluded"] += 1
                source_result = self.core.record_discovery_source(
                    run_id=run_id,
                    domain_label=storage_domain,
                    source_type="hotspot",
                    source_object_id=source["source_object_id"],
                    source_object_version=source["source_object_version"],
                    source_time=source["source_time"],
                    expires_at=self._expires_at(source, now=now),
                    payload=source["payload"],
                    idempotency_key=f"{idempotency_key}:hotspot:source:{index}",
                )
                self.core.record_discovery_filter(
                    source_version_id=source_result["source_version_id"], outcome=outcome,
                    reason_code=reason_code, detail=detail,
                    idempotency_key=f"{idempotency_key}:hotspot:filter:{index}",
                )
                for domain in requested_domains:
                    result["sources_read"][domain] += 1
                    result["filtered"][domain][reason_code] = result["filtered"][domain].get(reason_code, 0) + 1
                    result["source_evidence"][domain] = {"status": "no_candidate", "reason": reason_code, "source_refs": []}
                continue
            audit_entry.update({"status": "hard_passed", "reason_code": "eligible"})
            eligible_sources.append((index, source, audit_entry))

        for selected_index, (index, source, audit_entry) in enumerate(eligible_sources):
            audit_entry.update({"status": "selected_for_detail", "reason_code": "hotspot_signal_priority"})
            result["hotspot_audit"]["selected_for_detail"] += 1
            detail_result: dict[str, Any] = {"status": "not_required"}
            read_detail = getattr(self.source_acquirer, "read_hotspot_event_detail", None)
            if callable(read_detail):
                source, detail_result = read_detail(source=source, deadline_monotonic=deadline_monotonic)
            if detail_result.get("status") not in {"completed", "not_required"}:
                outcome, reason_code, detail = "excluded", "hotspot_detail_unavailable", {
                    "reason": str(detail_result.get("reason") or "original_link_content_unavailable")
                }
                audit_entry.update({"status": "detail_unavailable", "reason_code": reason_code, "detail": detail})
            source_result = self.core.record_discovery_source(
                run_id=run_id,
                domain_label=storage_domain,
                source_type="hotspot",
                source_object_id=source["source_object_id"],
                source_object_version=source["source_object_version"],
                source_time=source["source_time"],
                expires_at=self._expires_at(source, now=now),
                payload=source["payload"],
                idempotency_key=f"{idempotency_key}:hotspot:source:{index}",
            )
            self.core.record_discovery_filter(
                source_version_id=source_result["source_version_id"], outcome=outcome,
                reason_code=reason_code, detail=detail,
                idempotency_key=f"{idempotency_key}:hotspot:filter:{index}",
            )
            for domain in requested_domains:
                result["sources_read"][domain] += 1
            if outcome != "eligible":
                for domain in requested_domains:
                    result["filtered"][domain][reason_code] = result["filtered"][domain].get(reason_code, 0) + 1
                    result["source_evidence"][domain] = {"status": "no_candidate", "reason": reason_code, "source_refs": []}
                continue
            input_payload = self._hotspot_opportunity_input(source=source, enabled_domains=requested_domains)
            assembly = self.core.create_discovery_input_assembly(
                run_id=run_id, source_version_id=source_result["source_version_id"], payload=input_payload,
                prompt_version="hotspot_to_opportunity.v1", skill_version="hotspot_to_opportunity.v1",
                idempotency_key=f"{idempotency_key}:hotspot:assembly:{index}",
                external_execution=True,
            )
            try:
                model_result, judgement = self._run_hotspot_to_opportunity_skill(
                    run_id=run_id, source_version_id=source_result["source_version_id"],
                    assembly_id=assembly["assembly_id"], input_payload=input_payload,
                    enabled_domains=requested_domains,
                )
            except ExternalIntelligenceRequired as exc:
                summary["external_intelligence_required"] = {
                    "task_type": exc.task.get("task_type"),
                    "business_context": exc.task.get("business_context"),
                }
                summary["failure_details"].append({
                    "stage": "candidate_discovery",
                    "operation": "hotspot_to_opportunity",
                    "status": "requires_external_intelligence",
                    "reason": str(exc),
                })
                return {
                    "run_id": run_id,
                    "discovery_date": discovery_date,
                    "status": "requires_external_intelligence",
                    "execution_mode": execution_mode,
                    "source_types": list(requested_source_types),
                    "failure_reason": None,
                    "technical_failures": 0,
                    "failure_details": list(summary["failure_details"]),
                    "summary": summary,
                    "task": exc.task,
                }
            except (json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
                audit_entry.update({"status": "judgement_failed", "reason_code": "hotspot_judgement_failed", "detail": {"reason": str(exc)}})
                failed_model_run_id = getattr(exc, "model_run_envelope_version_id", None)
                raw_model_output = getattr(exc, "raw_model_output", None)
                if failed_model_run_id is not None:
                    self.core.record_discovery_model_validation_failure(
                        model_run_id=failed_model_run_id, reason=str(exc), raw_model_output=raw_model_output,
                    )
                self.core.record_discovery_source_failure(
                    run_id=run_id, source_version_id=source_result["source_version_id"], model_run_id=failed_model_run_id,
                    failure_stage="model_output_validation" if failed_model_run_id else "model_execution",
                    reason=str(exc), raw_model_output=raw_model_output,
                    idempotency_key=f"{idempotency_key}:hotspot:failure:{index}",
                )
                for domain in requested_domains:
                    result["filtered"][domain]["hotspot_judgement_failed"] = result["filtered"][domain].get("hotspot_judgement_failed", 0) + 1
                    result["source_evidence"][domain] = {"status": "failed", "reason": "hotspot_judgement_failed", "source_refs": []}
                result["technical_failure"] = "hotspot model judgement failed; batch stopped without retry"
                break
            if judgement["outcome"] == "no_candidate":
                audit_entry.update({"status": "not_converted", "reason_code": "hotspot_not_converted", "detail": {"reason": judgement["reason"]}})
                self.core.record_discovery_no_candidate(
                    run_id=run_id, source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                    reason_code="hotspot_not_converted", detail={"reason": judgement["reason"]},
                    idempotency_key=f"{idempotency_key}:hotspot:absence:{index}",
                )
                for domain in requested_domains:
                    result["filtered"][domain]["hotspot_not_converted"] = result["filtered"][domain].get("hotspot_not_converted", 0) + 1
                    result["source_evidence"][domain] = {"status": "no_candidate", "reason": "hotspot_not_converted", "source_refs": []}
                continue
            accepted_candidates: list[dict[str, Any]] = []
            rejected_candidates: list[dict[str, Any]] = []
            for candidate in judgement["candidates"]:
                rejection = hotspot_candidate_rejection(candidate["domain_label"], candidate)
                if rejection is None:
                    accepted_candidates.append(candidate)
                    continue
                reason_code, detail = rejection
                rejected_candidates.append({"domain_label": candidate["domain_label"], "reason_code": reason_code, "detail": detail})
                result["filtered"][candidate["domain_label"]][reason_code] = (
                    result["filtered"][candidate["domain_label"]].get(reason_code, 0) + 1
                )
                result["source_evidence"][candidate["domain_label"]] = {
                    "status": "no_candidate", "reason": reason_code, "source_refs": []
                }
            if not accepted_candidates:
                reason_code = rejected_candidates[0]["reason_code"]
                detail = rejected_candidates[0]["detail"]
                audit_entry.update({"status": "not_converted", "reason_code": reason_code, "detail": detail})
                self.core.record_discovery_no_candidate(
                    run_id=run_id, source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                    reason_code=reason_code, detail=detail,
                    idempotency_key=f"{idempotency_key}:hotspot:absence:{index}",
                )
                continue
            for candidate_index, candidate in enumerate(accepted_candidates):
                domain = candidate["domain_label"]
                payload = {
                    "title": candidate["candidate_topic"], "why_attention": candidate["audience_relation"],
                    "new_angle": f"{candidate['topic_angle']}；{candidate['content_increment']}",
                    "material_readiness": "热点仅提供发现材料；正式研究仍需独立核验。",
                    "risk_limits": "；".join(candidate["risks"]) or "热点材料仅用于发现，不得替代正式研究证据。",
                    "originality_relation": "problem_expansion", "normalized_title": _normalize_title(candidate["candidate_topic"]),
                    "normalized_source_title": _normalize_title(str(source["payload"]["title"])), "domain": domain,
                    "source_reference": {"source_version_id": source_result["source_version_id"], "source_type": "hotspot", "source_object_id": source["source_object_id"], "source_object_version": source["source_object_version"], "source_time": source["source_time"], "url": source["payload"].get("url", "")},
                    **candidate,
                }
                self.core.create_discovery_candidate(
                    run_id=run_id, source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                    candidate_id=f"candidate_{_stable_id({domain: source_result['source_version_id'], 'title': payload['normalized_title']})}",
                    payload=payload, candidate_domain_label=domain, allow_multiple_from_model_run=True,
                    idempotency_key=f"{idempotency_key}:hotspot:candidate:{index}:{candidate_index}",
                )
                result["candidate_counts"][domain] += 1
                result["source_evidence"][domain] = {"status": "has_candidate", "reason": "candidate_created", "source_refs": [{"source_type": "hotspot", "source_object_id": source["source_object_id"], "source_object_version": source["source_object_version"]}]}
            audit_entry.update({
                "status": "judged",
                "reason_code": "candidate_created",
                "detail": {"candidate_count": len(accepted_candidates), "rejected_candidates": rejected_candidates},
            })
        return result

    def _prepare_hotspot_to_opportunity_task(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        contract = self.hotspot_to_opportunity_contract
        enforce_atomic_skill_runtime_guard(
            entrypoint="stage1b_daily_discovery.hotspot_to_opportunity",
            operation=contract.formal_skill_id,
            data_identity=self.core.data_identity,
        )
        contract.validate_contract(require_model_route=False)
        validate_payload(input_payload, contract.input_schema)
        preprocessed = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
        model_input = apply_binding(contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, contract.model_input_schema)
        task = self.core.prepare_discovery_external_task(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            skill={
                "formal_skill_id": contract.formal_skill_id,
                "version": contract.version,
                "source_reference": "HOTSPOT_TO_OPPORTUNITY_BUSINESS_CONTRACT.yaml",
                "content": contract.prompt_template,
                "rendered_instructions": contract.portable_skill().render_prompt(model_input),
                "input_schema": contract.model_input_schema,
                "output_schema": contract.model_output_schema,
                "skill_hash": contract.skill_hash,
                "binding": {
                    "name": contract.binding_name,
                    "version": contract.binding_version,
                    "hash": contract.binding_hash,
                },
            },
            input_payload=model_input,
            constraints={
                "use_only_supplied_material": True,
                "do_not_search": True,
                "preserve_source_identity": True,
                "respect_domain_boundary": True,
                "respect_risk_boundary": True,
                "do_not_force_candidate": True,
                "no_score_rank_weight": True,
                "cannot_change_business_state": True,
            },
            output_requirements={
                "submission": "structured_fields",
                "schema": contract.model_output_schema,
                "formal_output_schema": contract.output_schema,
                "response_format": "structured_fields",
            },
        )
        return task, preprocessed

    def _accept_hotspot_to_opportunity_external_result(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
        preprocessed: dict[str, Any],
        enabled_domains: tuple[str, ...],
        submission: Mapping[str, Any],
    ) -> tuple[ExternalIntelligenceReceipt, dict[str, Any]]:
        if not isinstance(submission, Mapping):
            raise FormalSkillValidationError("external intelligent result must be a structured submission")
        external_output = submission.get("output")
        if not isinstance(external_output, dict):
            raise FormalSkillValidationError("external intelligent result must provide structured output fields")
        execution_id = str(submission.get("execution_id") or "").strip()
        executor_id = str(submission.get("executor_id") or "").strip()
        if not execution_id or not executor_id:
            raise FormalSkillValidationError("external intelligent result requires execution and executor identity")
        model_run_id = self.core.record_discovery_external_execution(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            execution_id=execution_id,
            executor_id=executor_id,
            model_ref=str(submission.get("model_ref") or "").strip() or None,
            submitted_at=str(submission.get("submitted_at") or "").strip() or None,
            output_payload=external_output,
        )
        contract = self.hotspot_to_opportunity_contract
        try:
            model_output = dict(external_output)
            validate_payload(model_output, contract.model_output_schema)
            output_payload = apply_binding(contract.output_map, input_payload, model_output, preprocessed)
            validate_payload(output_payload, contract.output_schema)
            judgement = validate_hotspot_opportunity_judgement(
                output_payload, enabled_domains=enabled_domains
            )
        except (json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
            self.core.record_discovery_model_validation_failure(
                model_run_id=model_run_id, reason=str(exc), raw_model_output=None,
            )
            raise FormalSkillValidationError(
                str(exc), model_run_envelope_version_id=model_run_id,
            ) from exc
        return ExternalIntelligenceReceipt(model_run_id), judgement

    def _prepare_source_to_topic_task(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        contract = self.source_to_topic_contract
        data_identity = getattr(self.core, "data_identity", "test")
        enforce_atomic_skill_runtime_guard(
            entrypoint="stage1b_daily_discovery.source_to_topic",
            operation=contract.formal_skill_id,
            data_identity=data_identity,
            event_log_path=(
                Path(self.core.db_path).parent / "business_runtime_guard_events.jsonl"
                if data_identity != "production"
                and getattr(self.core, "db_path", None) is not None
                else None
            ),
        )
        contract.validate_contract(require_model_route=False)
        validate_payload(input_payload, contract.input_schema)
        preprocessed = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
        model_input = apply_binding(contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, contract.model_input_schema)
        prompt = contract.portable_skill().render_prompt(model_input)
        task = self.core.prepare_discovery_external_task(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            skill={
                "formal_skill_id": contract.formal_skill_id,
                "version": contract.version,
                "source_reference": f"runtime_skills/{contract.formal_skill_id}",
                "content": contract.prompt_template,
                "rendered_instructions": prompt,
                "input_schema": contract.model_input_schema,
                "output_schema": contract.model_output_schema,
                "skill_hash": contract.skill_hash,
                "binding": {
                    "name": contract.binding_name,
                    "version": contract.binding_version,
                    "hash": contract.binding_hash,
                },
            },
            input_payload=model_input,
            constraints={
                "use_only_supplied_material": True,
                "do_not_search": True,
                "preserve_source_identity": True,
                "respect_domain_boundary": True,
                "respect_risk_boundary": True,
                "do_not_force_candidate": True,
                "no_score_rank_weight": True,
                "cannot_change_business_state": True,
            },
            output_requirements={
                "submission": "structured_fields",
                "schema": contract.model_output_schema,
                "formal_output_schema": contract.output_schema,
                "response_format": contract.model_response_format,
            },
        )
        return task, preprocessed

    def prepare_source_to_topic_external_task(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
    ) -> dict[str, Any]:
        """Return the canonical Core task for an existing external assembly."""
        input_payload = self.core.load_discovery_external_assembly_payload(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
        )
        task, _ = Stage1BDailyDiscoveryService._prepare_source_to_topic_task(
            self,
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
        )
        return task

    def _accept_source_to_topic_external_result(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
        preprocessed: dict[str, Any],
        submission: Mapping[str, Any],
    ):
        contract = self.source_to_topic_contract
        if not isinstance(submission, Mapping):
            raise FormalSkillValidationError("external intelligent result must be a structured submission")
        external_output = submission.get("output")
        if not isinstance(external_output, dict):
            raise FormalSkillValidationError("external intelligent result must provide structured output fields")
        execution_id = str(submission.get("execution_id") or "").strip()
        executor_id = str(submission.get("executor_id") or "").strip()
        if not execution_id or not executor_id:
            raise FormalSkillValidationError("external intelligent result requires execution and executor identity")
        model_run_id = self.core.record_discovery_external_execution(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            execution_id=execution_id,
            executor_id=executor_id,
            model_ref=str(submission.get("model_ref") or "").strip() or None,
            submitted_at=str(submission.get("submitted_at") or "").strip() or None,
            output_payload=external_output,
        )
        try:
            model_output = dict(external_output)
            validate_payload(model_output, contract.model_output_schema)
            output_payload = apply_binding(contract.output_map, input_payload, model_output, preprocessed)
            validate_payload(output_payload, contract.output_schema)
            validate_source_to_topic_output_semantics(input_payload, output_payload)
            execution_review = output_payload.get("execution_review")
            if not isinstance(execution_review, dict) or execution_review.get("respected_domain_boundary") is not True:
                raise FormalSkillValidationError(
                    "source-to-topic output did not explicitly respect the frozen production boundary"
                )
        except (json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
            self.core.record_discovery_model_validation_failure(
                model_run_id=model_run_id,
                reason=str(exc),
                raw_model_output=None,
            )
            raise FormalSkillValidationError(
                str(exc),
                model_run_envelope_version_id=model_run_id,
            ) from exc
        return ExternalIntelligenceReceipt(model_run_id), output_payload

    def submit_source_to_topic_external_result(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output: dict[str, Any],
    ):
        """Submit one structured result through the existing Core validation path."""
        input_payload = self.core.load_discovery_external_assembly_payload(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
        )
        _, preprocessed = Stage1BDailyDiscoveryService._prepare_source_to_topic_task(
            self,
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
        )
        return Stage1BDailyDiscoveryService._accept_source_to_topic_external_result(
            self,
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
            preprocessed=preprocessed,
            submission={
                "execution_id": execution_id,
                "executor_id": executor_id,
                "model_ref": model_ref,
                "submitted_at": submitted_at,
                "output": output,
            },
        )

    def _run_source_to_topic_skill(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
    ):
        task, preprocessed = Stage1BDailyDiscoveryService._prepare_source_to_topic_task(
            self,
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
        )
        if self.external_executor is None:
            raise ExternalIntelligenceRequired(task)
        submission = self.external_executor(task)
        return Stage1BDailyDiscoveryService._accept_source_to_topic_external_result(
            self,
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
            preprocessed=preprocessed,
            submission=submission,
        )

    def _run_hotspot_to_opportunity_skill(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
        enabled_domains: tuple[str, ...],
    ):
        task, preprocessed = self._prepare_hotspot_to_opportunity_task(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
        )
        if self.external_executor is None:
            raise ExternalIntelligenceRequired(task)
        return self._accept_hotspot_to_opportunity_external_result(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            input_payload=input_payload,
            preprocessed=preprocessed,
            enabled_domains=enabled_domains,
            submission=self.external_executor(task),
        )

    @staticmethod
    def _hotspot_opportunity_input(
        *, source: dict[str, Any], enabled_domains: tuple[str, ...]
    ) -> dict[str, Any]:
        cluster = source["payload"].get("hotspot_event_cluster")
        if not isinstance(cluster, dict):
            raise DailyDiscoveryValidationError("hotspot opportunity requires a complete event cluster")
        return {
            "event_material": cluster,
            "enabled_domains": [
                {
                    "domain_label": domain_label,
                    "direction_card": {
                        key: value
                        for key, value in get_domain_pack(domain_label).items()
                        if key != "discovery"
                    },
                }
                for domain_label in enabled_domains
            ],
        }

    def _deterministic_filter(
        self,
        *,
        domain_label: str,
        source: dict[str, Any],
        now: datetime,
        allow_processed_source: bool = False,
    ) -> tuple[str, str, dict[str, Any]]:
        if source["source_type"] not in {
            "hotspot", "daily_competitor_content", "historical_high_signal", "tag_discovery",
            "question_expansion", "saved_user_direction",
        }:
            return "excluded", "source_not_qualified", {}
        if domain_label not in formal_domain_labels():
            return "excluded", "domain_mismatch", {}
        title, url = str(source["payload"].get("title") or ""), str(source["payload"].get("url") or "")
        if len(title.strip()) < 6 or (source["source_type"] not in {"question_expansion", "saved_user_direction"} and not url):
            return "excluded", "material_obviously_insufficient", {}
        if source["source_type"] in {"hotspot", "daily_competitor_content"} and _parse_time(source["source_time"]) + timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS) < now:
            return "excluded", "freshness_expired", {}
        risk_terms = hotspot_global_risk_block_terms() if source["source_type"] == "hotspot" else tuple(
            str(term) for term in get_discovery_policy(domain_label).get("risk_block_terms", [])
        )
        if any(term in title for term in risk_terms):
            return "excluded", "risk_blocked", {"matched_terms": [term for term in risk_terms if term in title]}
        normalized_title = _normalize_title(title)
        if source["source_type"] == "hotspot":
            # A shared hotspot must not be rejected by one domain's title-word
            # exclusions before the cross-domain judgement sees its full material.
            if not allow_processed_source and self.core.discovery_source_seen(source_type=source["source_type"], source_object_id=source["source_object_id"], source_object_version=source["source_object_version"]):
                return "excluded", "source_already_processed", {}
            return "eligible", "eligible", {"normalized_source_title": normalized_title}
        if source["source_type"] == "daily_competitor_content":
            return "excluded", "tracking_source_not_candidate_source", {}
        if source["source_type"] == "tag_discovery":
            if bool(source["payload"].get("is_tracked_account")):
                return "excluded", "tracked_competitor_account", {}
            try:
                like_count = int(source["payload"].get("like_count") or 0)
            except (TypeError, ValueError):
                like_count = 0
            if like_count < TAG_CANDIDATE_LIKE_FLOOR:
                return "excluded", "below_tag_candidate_like_floor", {
                    "like_count": like_count,
                    "required_like_count": TAG_CANDIDATE_LIKE_FLOOR,
                }
        if source["source_type"] == "question_expansion":
            parent = source["payload"].get("parent_source_ref")
            if not isinstance(parent, dict):
                return "excluded", "expansion_missing_parent_source", {}
            parent_type = str(parent.get("source_type") or "").strip()
            parent_id = str(parent.get("source_object_id") or "").strip()
            if not parent_type or not parent_id:
                return "excluded", "expansion_parent_incomplete", {}
            if parent_type == "question_expansion":
                return "excluded", "expansion_parent_cannot_be_expansion", {}
            if parent_type not in {"hit_breakdown", "competitor_breakdown"}:
                return "excluded", "expansion_parent_not_breakdown", {}
        title_folded = title.casefold()
        exclude_terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("exclude_terms", []))
        matched_terms = [term for term in exclude_terms if term.casefold() in title_folded]
        if matched_terms:
            return "excluded", "outside_domain_policy", {"matched_terms": matched_terms}
        if not allow_processed_source and self.core.discovery_source_seen(source_type=source["source_type"], source_object_id=source["source_object_id"], source_object_version=source["source_object_version"]):
            return "excluded", "source_already_processed", {}
        if self.core.formal_topic_title_seen(domain_label=domain_label, normalized_title=normalized_title):
            return "excluded", "already_produced", {}
        return "eligible", "eligible", {"normalized_source_title": normalized_title}

    @staticmethod
    def _expires_at(source: dict[str, Any], *, now: datetime) -> str | None:
        if source["source_type"] not in {"hotspot", "daily_competitor_content"}:
            return None
        return (_parse_time(source["source_time"]) + timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()

    @staticmethod
    def _assembly_payload(
        *,
        run_id: str,
        domain_label: str,
        source: dict[str, Any],
        source_version_id: str,
        experience_cards: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = source["payload"]
        policy = get_discovery_policy(domain_label)
        try:
            production_boundary = require_frozen_production_boundary(domain_label)
        except ValueError as exc:
            raise DailyDiscoveryValidationError(
                f"正式生产边界尚未冻结，不能组装 {domain_label} 的候选输入"
            ) from exc
        title = str(payload["title"]).strip()
        url = str(payload.get("url") or "").strip()
        account_name = str(payload.get("account_name") or "").strip()
        source_kind_map = {
            "hotspot": "hotspot_event_cluster",
            "daily_competitor_content": "benchmark_daily",
            "historical_high_signal": "benchmark_historical",
            "tag_discovery": "tag_search",
            "question_expansion": "question_expansion",
            "saved_user_direction": "user_direction",
        }
        source_kind = source_kind_map[source["source_type"]]
        source_content_parts = [title]
        parent_source_ref = payload.get("parent_source_ref") if source["source_type"] == "question_expansion" else None
        if isinstance(parent_source_ref, dict):
            parent_type = str(parent_source_ref.get("source_type") or "").strip()
            parent_id = str(parent_source_ref.get("source_object_id") or "").strip()
            if parent_type and parent_id:
                source_content_parts.append(f"母来源：{parent_type}/{parent_id}")
        if account_name:
            source_content_parts.append(f"来源账号/渠道：{account_name}")
        if url:
            source_content_parts.append(f"来源链接：{url}")
        source_content = "；".join(source_content_parts)
        evidence_items = [title]
        qualification_material_refs = payload.get("qualification_material_refs")
        if source["source_type"] == "question_expansion" and isinstance(qualification_material_refs, list):
            for material_ref in qualification_material_refs:
                if not isinstance(material_ref, dict):
                    continue
                for value in (material_ref.get("ref"), material_ref.get("snippet")):
                    text = str(value or "").strip()
                    if text and text not in evidence_items:
                        evidence_items.append(text)
        if url:
            evidence_items.append(url)
        if account_name:
            evidence_items.append(account_name)
        if isinstance(parent_source_ref, dict):
            parent_type = str(parent_source_ref.get("source_type") or "").strip()
            parent_id = str(parent_source_ref.get("source_object_id") or "").strip()
            if parent_type and parent_id:
                evidence_items.append(f"母来源：{parent_type}/{parent_id}")
        hotspot_cluster = payload.get("hotspot_event_cluster") if source["source_type"] == "hotspot" else None
        if isinstance(hotspot_cluster, dict):
            for item in hotspot_cluster.get("merged_sources", []):
                if not isinstance(item, dict):
                    continue
                for key in ("title", "url", "channel"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        evidence_items.append(value)
        event_cluster_summary = (
            hotspot_cluster
            if isinstance(hotspot_cluster, dict)
            else {
                "cluster_id": source_version_id,
                "representative_source": title,
                "merged_sources": [],
                "dedupe_reason": "single currently recorded source version; upstream event clustering must be supplied before live validation can be upgraded.",
            }
        )
        material_packet = payload.get("material_packet") if isinstance(payload.get("material_packet"), dict) else None
        expansion_policy = get_domain_pack(domain_label).get("question_expansion_policy") or {}
        boundary_summary = {
            "status": production_boundary.get("status"),
            "version": production_boundary.get("version"),
            "in_boundary_principles": production_boundary.get("in_boundary_principles", []),
            "out_boundary_principles": production_boundary.get("out_boundary_principles", []),
            "unknown_topic_rule": production_boundary.get("unknown_topic_rule", {}),
        }
        domain_summary = (
            f"当前领域为 {domain_label}。正式生产边界（必须遵守）："
            f"{json.dumps(boundary_summary, ensure_ascii=False, sort_keys=True)}。"
            f"排除类型/词包括：{', '.join(str(term) for term in policy.get('exclude_terms', [])) or '无'}。"
            f"热点匹配词包括：{', '.join(str(term) for term in policy.get('hotspot_match_terms', [])) or '无'}。"
        )
        if isinstance(expansion_policy, dict) and expansion_policy.get("primary_content_carrier_rule"):
            domain_summary += f"问题拓展的内容承载规则：{expansion_policy['primary_content_carrier_rule']}"
        return {
            "request_id": f"source_to_topic_{source_version_id}",
            "correlation_id": run_id,
            "source_id": source_version_id,
            "source_content": source_content,
            "source_evidence_items": evidence_items[:63],
            "domain_label": domain_label,
            "relation_summary": (
                "拓展线索已通过轻量资格化；它仍只是绑定母来源的待研究线索，不能当作研究结论。"
                if source["source_type"] == "question_expansion"
                else "deterministic prefilter passed; source duplication and prior production checks are clear for this source version. Candidate-level same-angle merging is handled with its formal support record."
            ),
            "source_kind": source_kind,
            "event_cluster_summary": event_cluster_summary,
            "deterministic_prefilter": {
                "passed": True,
                "reasons": ["source_traceable", "freshness_checked", "domain_prefilter_passed", "duplicate_and_cooling_clear"],
                "risk_flags": [],
                "domain_precheck": domain_label,
            },
            "material_packet": material_packet or {
                "fact_summary": source_content,
                "key_source_refs": evidence_items[:8],
                "audience_relation": "该来源只提供发现线索；是否与目标受众有强关系需由来源转选题 Skill 基于输入材料判断。",
                "domain_fit": f"确定性前置过滤已判定该来源可进入 {domain_label} 的选题转化尝试。",
                "possible_questions": [title],
                "uncertainty": "当前 Stage 1B 入口只传入已记录来源材料；正式研究证据尚未开始。",
                "material_gaps": ["尚未进入正式深度研究；不得把来源标题当作事实证据。"],
                "risks": [],
                "stop_reason": (
                    "qualification_passed; stop before formal research"
                    if source["source_type"] == "question_expansion"
                    else "none"
                ),
            },
            "duplicate_cooling_status": {
                "duplicate_status": "clear",
                "cooling_status": "clear",
                "related_refs": [],
            },
            "domain_rule_summary": domain_summary,
            # Only confirmed, same-domain experiences enter candidate discovery.
            # Raw breakdowns and pending proposals are not experience cards.
            # Keep the usage conditions with the card so the model can explain
            # why a card is relevant instead of treating every experience as a
            # universal rule.
            "experience_cards": [
                {
                    "experience_id": str(item.get("experience_id") or ""),
                    "summary": str(item.get("summary") or ""),
                    "experience_layer": str(item.get("experience_layer") or "section_method"),
                    "use_positions": list(item.get("use_positions") or ["body"]),
                    "trigger_signals": list(item.get("trigger_signals") or []),
                    "applicable_when": list(item.get("applicable_when") or []),
                    "method": list(item.get("method") or []),
                    "usage_boundary": list(item.get("boundary") or []),
                    "not_applicable_when": list(item.get("not_applicable_when") or []),
                }
                for item in (experience_cards or [])
            ],
            "user_direction": "",
            "schema_version": "source_to_topic.input.v1",
        }


def main(argv: list[str] | None = None) -> int:
    """Run one Core-owned Stage 1B batch, reuse frozen input, resubmit, or purge a real daily validation."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=tuple(sorted(formal_domain_labels())), help="one authorized formal domain for a new batch")
    parser.add_argument(
        "--source-type",
        action="append",
        choices=SOURCE_TYPES,
        help="source type to run; real_daily_validation runs one source path, production_daily uses the complete candidate-source set",
    )
    parser.add_argument("--actor", required=True, help="audited actor for the explicit user authorization")
    parser.add_argument("--idempotency-key", required=True, help="stable key for this exact authorized run")
    parser.add_argument("--mode", choices=EXECUTION_MODES, help="explicit execution identity; no mode can be promoted automatically")
    parser.add_argument("--daily-run-id", help="formal daily run identity for production daily discovery")
    parser.add_argument("--discovery-date", default=datetime.now(timezone.utc).date().isoformat(), help="YYYY-MM-DD (defaults to UTC today)")
    parser.add_argument("--reuse-hotspot-run", help="successful hotspot collection run whose temporary batch is reused without collecting again")
    parser.add_argument("--resume-source-id", action="append", dest="resume_source_ids", help="replay only these already-recorded source objects")
    parser.add_argument("--reason", help="required audit reason for a formal handoff")
    parser.add_argument("--handoff-selected-candidate", help="candidate version selected by the user for Stage 1A handoff")
    parser.add_argument("--verify-stage1-production-closure", action="store_true", help="verify completed production daily, user selection, and Stage 1A handoff evidence")
    parser.add_argument("--inspect-discovery-run", help="read the persisted candidates and failures of one discovery run")
    parser.add_argument("--preview-daily-review", action="store_true", help="read today's topics, sources and rule results without writes or model calls")
    parser.add_argument("--qualify-pending", action="store_true", help="apply the existing qualification gate to recorded expansion leads that have no qualification row")
    args = parser.parse_args(argv)
    special_actions = sum(bool(value) for value in (
        args.reuse_hotspot_run,
        args.handoff_selected_candidate,
        args.verify_stage1_production_closure,
        args.inspect_discovery_run,
        args.preview_daily_review,
        args.qualify_pending,
    ))
    if special_actions > 1:
        parser.error("choose only one special action")
    if args.handoff_selected_candidate:
        if args.mode != "production_daily" or not args.reason or args.domain or args.source_type:
            parser.error("handoff requires --handoff-selected-candidate, --mode production_daily, --actor, --reason, and no --domain or --source-type")
    elif args.verify_stage1_production_closure:
        if args.mode or args.domain or args.source_type:
            parser.error("Stage 1 closure verification requires --verify-stage1-production-closure and --actor, with no --mode, --domain, or --source-type")
    elif args.inspect_discovery_run:
        if args.mode or args.domain or args.source_type or args.reason:
            parser.error("run inspection requires --inspect-discovery-run and --actor, with no --mode, --domain, --source-type, or --reason")
    elif args.preview_daily_review:
        if args.mode or not args.domain or args.source_type or args.reason:
            parser.error("daily review preview requires --preview-daily-review, --domain and --actor, with no --mode, --source-type, or --reason")
    elif args.qualify_pending:
        if args.mode or args.domain or args.source_type or args.reason:
            parser.error("pending qualification requires --qualify-pending and --actor, with no --mode, --domain, --source-type, or --reason")
    elif not args.domain:
        parser.error("a new batch requires --domain")
    elif not args.mode:
        parser.error("a new batch requires --mode")
    elif args.mode == "production_daily" and not str(args.daily_run_id or "").strip():
        parser.error("production_daily requires --daily-run-id")

    selected_source_types = tuple(args.source_type or DAILY_REPORT_SOURCE_TYPES)
    if args.resume_source_ids and args.mode != "real_daily_validation":
        parser.error("--resume-source-id is only available for a real_daily_validation source-specific replay")
    if args.reuse_hotspot_run and set(selected_source_types) != {"hotspot"}:
        parser.error("--reuse-hotspot-run requires exactly --source-type hotspot")
    core = Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="production")
    try:
        business = CreationAssistantFormalBusinessCore(core=core)
        if args.verify_stage1_production_closure:
            print(_canonical(business.verify_stage1_production_closure()))
            return 0
        if args.inspect_discovery_run:
            print(_canonical(business.inspect_daily_discovery_run(run_id=args.inspect_discovery_run)))
            return 0
        if args.preview_daily_review:
            service = Stage1BDailyDiscoveryService(core=core, gateway=None)  # type: ignore[arg-type]
            print(_canonical(service.preview_daily_review(domain_label=args.domain)))
            return 0
        if args.qualify_pending:
            print(_canonical(business.qualify_pending_discovery_sources(actor=args.actor)))
            return 0
        if args.handoff_selected_candidate:
            result = business.handoff_daily_discovery_candidate(
                candidate_version_id=args.handoff_selected_candidate,
                actor=args.actor,
                reason=args.reason or "",
                idempotency_key=args.idempotency_key,
            )
            print(_canonical(result))
            return 0
        source_acquirer = (
            build_production_source_acquirer(core, source_types=selected_source_types)
            if {"hotspot", "tag_discovery"} & set(selected_source_types)
            else None
        )
        result = business.execute_daily_discovery(
            discovery_date=args.discovery_date,
            actor=args.actor,
            idempotency_key=args.idempotency_key,
            execution_mode=args.mode,
            daily_run_id=args.daily_run_id,
            domains=(args.domain,),
            source_types=selected_source_types,
            gateway=None,
            source_acquirer=source_acquirer,
            reuse_hotspot_discovery_run_id=args.reuse_hotspot_run,
            resume_source_object_ids=tuple(args.resume_source_ids or ()),
        )
        snapshot = business.view_daily_discovery_snapshot(
            run_id=result["run_id"], domains=(args.domain,)
        )
        print(_canonical({**result, "snapshot": snapshot}))
        return 0 if result["status"] == "completed" else 2
    finally:
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
