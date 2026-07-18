"""Stage 1B controlled daily candidate discovery.

Only already-recorded, real source facts may enter this path.  It creates
candidate snapshots and user decisions, never a production task by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError, ModelRequest
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig
from scripts.core.model_gateway.model_router import DEFAULT_MODEL_ENV_PATH, ModelRouter, ModelRouterError
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH,
    FormalSkillValidationError,
    SOURCE_TO_TOPIC_CONTRACT_PATH,
    apply_binding,
    parse_model_json,
    preprocess_formal_skill_input,
    validate_payload,
    validate_source_to_topic_output_semantics,
)
from scripts.core.production.stage0_content_core import (
    CoreDiscoveryModelRunMaterializer,
    DataIdentityError,
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.business_data.daily_source_acquisition import DailyDiscoverySourceAcquirer, validate_hotspot_collection_contract
from scripts.core.business_data.run_domain_search import validate_domain_search_execution_contract
from scripts.core.business_data.domain_labels import (
    FORMAL_DOMAIN_LABELS,
    get_discovery_policy,
    get_domain_pack,
    hotspot_global_risk_block_terms,
)
from scripts.core.external_adapters import LocalMediaCrawlerExecutor, LocalTrendRadarExecutor


APPROVED_DOMAINS = tuple(sorted(FORMAL_DOMAIN_LABELS))
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
    "hotspot",
    "daily_competitor_content",
    "historical_high_signal",
    "tag_discovery",
    "question_expansion",
)
HOTSPOT_DAILY_PROCESS_LIMIT = 10
DAILY_SOURCE_VALIDITY_HOURS = 72
SOURCE_READ_LIMIT = 6
DISCOVERY_PROMPT_VERSION = "source_to_topic.prompt.v1"
DISCOVERY_SKILL_VERSION = "source_to_topic.skill.v1"
ORIGINALITY_RELATIONSHIPS = frozenset({"same_topic_original_reconstruction", "problem_expansion", "independent_research"})
HOTSPOT_DOMAIN_BRIDGE_TYPES = frozenset({
    "direct_object", "mechanism", "audience_impact", "cultural_mapping", "downstream_effect",
})
HOTSPOT_DOMAIN_FITS = frozenset({"core", "adjacent"})
HOTSPOT_PRIMARY_LENSES = frozenset({
    "原因解释", "普通人关系", "反向视角", "局部细节", "背景补充", "后续推演",
})
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


class DailyDiscoveryValidationError(StateTransitionError):
    pass


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


def build_production_daily_discovery_gateway(core: Stage0ContentProductionCore) -> ModelGateway:
    """Build the explicitly configured source_to_topic route."""
    if core.data_identity != "production":
        raise StateTransitionError("production daily-discovery gateway requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
        raise ModelRouterError("source_to_topic requires an explicit business topic_screening route with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None or provider.provider_type != "mimo":
        raise ModelRouterError("configured source_to_topic provider is unsupported by the current adapter")
    route = router.resolve_bound_route("business_analysis", route_name="business.source_to_topic")
    if route.provider_name != "hermes":
        raise ModelRouterError("source_to_topic route has an unexpected provider adapter")
    auth_ref, endpoint_ref = str(provider.settings.get("auth_ref") or ""), str(provider.settings.get("endpoint_ref") or "")
    if not auth_ref or not endpoint_ref:
        raise ModelRouterError("configured source_to_topic provider lacks auth_ref or endpoint_ref")
    api_key, base_url = _configured_environment_value(auth_ref), _configured_environment_value(endpoint_ref)
    if not base_url.startswith(("https://", "http://")):
        raise ModelRouterError("configured source_to_topic endpoint must be an HTTP(S) URL")
    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=api_key, base_url=base_url, model=route.model_name, timeout_seconds=90, max_retries=0))
    return ModelGateway(routes={route.route_name: route}, providers={adapter.provider_name: adapter}, materializer=CoreDiscoveryModelRunMaterializer(core))


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
        )
    if "tag_discovery" in source_types:
        validate_domain_search_execution_contract(domain_search_config)
        mediacrawler = LocalMediaCrawlerExecutor()
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
        gateway: ModelGateway,
        source_acquirer: DailyDiscoverySourceAcquirer | None = None,
        source_to_topic_contract: FormalSkillContract | None = None,
        hotspot_to_opportunity_contract: FormalSkillContract | None = None,
    ):
        self.core = core
        self.gateway = gateway
        self.source_acquirer = source_acquirer
        self.source_to_topic_contract = source_to_topic_contract or FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
        self.hotspot_to_opportunity_contract = hotspot_to_opportunity_contract or FormalSkillContract.from_yaml(HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH)

    def run_daily_discovery(
        self,
        *,
        discovery_date: str,
        actor: str,
        idempotency_key: str,
        execution_mode: str,
        batch_timeout_seconds: int = 600,
        now: datetime | None = None,
        domains: tuple[str, ...] = APPROVED_DOMAINS,
        source_types: tuple[str, ...] = DAILY_REPORT_SOURCE_TYPES,
        reuse_hotspot_discovery_run_id: str | None = None,
    ) -> dict[str, Any]:
        requested_domains = tuple(domains)
        requested_source_types = tuple(source_types)
        if not requested_domains or len(set(requested_domains)) != len(requested_domains):
            raise DailyDiscoveryValidationError("daily discovery requires one or more distinct approved domains")
        if any(domain_label not in APPROVED_DOMAINS for domain_label in requested_domains):
            raise DailyDiscoveryValidationError("daily discovery received an unapproved domain")
        if (
            not requested_source_types
            or len(set(requested_source_types)) != len(requested_source_types)
            or any(source_type not in SOURCE_TYPES for source_type in requested_source_types)
        ):
            raise DailyDiscoveryValidationError("daily discovery requires distinct supported source types")
        if execution_mode not in EXECUTION_MODES:
            raise DailyDiscoveryValidationError("daily discovery requires an explicit execution mode")
        if execution_mode == "real_daily_validation" and len(requested_source_types) != 1:
            raise DailyDiscoveryValidationError("real daily validation requires exactly one source type for a complete path")
        if reuse_hotspot_discovery_run_id is not None and set(requested_source_types) != {"hotspot"}:
            raise DailyDiscoveryValidationError("a reused hotspot batch can only run the hotspot conversion path")
        if execution_mode == "production_daily" and set(requested_source_types) != set(DAILY_REPORT_SOURCE_TYPES):
            raise DailyDiscoveryValidationError("production_daily must run the complete five-source daily report set; manual tasks are not daily report sources")
        if self.core.data_identity != "production" and execution_mode != "test_isolated":
            raise DataIdentityError("non-production discovery data must use test_isolated mode")
        if self.core.data_identity == "production" and execution_mode == "test_isolated":
            raise DataIdentityError("production discovery data cannot use test_isolated mode")
        if (
            execution_mode != "test_isolated"
            and {"hotspot", "tag_discovery"} & set(requested_source_types)
            and self.source_acquirer is None
        ):
            raise DailyDiscoveryValidationError("the selected live external source requires its Runtime acquirer")
        if batch_timeout_seconds < 0:
            raise DailyDiscoveryValidationError("batch timeout must be zero or a positive number of seconds")
        now = now or datetime.now(timezone.utc)
        request = {
            "discovery_date": discovery_date,
            "actor": actor,
            "domains": list(requested_domains),
            "execution_mode": execution_mode,
            "source_types": list(requested_source_types),
            "batch_timeout_seconds": batch_timeout_seconds,
            "reuse_hotspot_discovery_run_id": reuse_hotspot_discovery_run_id,
        }
        replay = self.core.find_command_replay("stage1b_execute_daily_discovery", idempotency_key, request)
        if replay:
            return {**replay, "replayed": True}
        run = self.core.create_discovery_run(
            discovery_date=discovery_date,
            actor=actor,
            execution_mode=execution_mode,
            idempotency_key=f"{idempotency_key}:run",
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
            "technical_failures": 0,
        }
        daily_since = (now - timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()
        deadline = time.monotonic() + batch_timeout_seconds
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
                            hotspot["retention"] = self.core.retain_only_latest_hotspot_batch(
                                discovery_run_id=run["run_id"]
                            )
                    summary["acquisition"] = {
                        "execution_order": [
                            "trendradar_hotspot", "hotspot_conversion", "daily_competitor",
                            "historical_high_signal", "tag_search", "tag_conversion",
                            "question_expansion", "saved_user_direction",
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
            if hotspot_discovery_run_id is not None and execution_mode == "production_daily":
                deleted_hotspots = self.core.clear_hotspot_observations_for_discovery_run(
                    discovery_run_id=hotspot_discovery_run_id
                )
                summary["acquisition"].setdefault("hotspot", {})["deleted_raw_items"] = deleted_hotspots
            elif hotspot_discovery_run_id is not None:
                summary["acquisition"].setdefault("hotspot", {})["raw_batch_status"] = (
                    "latest_reusable_batch; it is replaced only after a later successful collection"
                )
            for domain_label in requested_domains:
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
                    if time.monotonic() >= deadline:
                        lifecycle_status, failure_reason = "timed_out", "batch deadline reached before tag search; no request was sent or retried"
                        return
                    try:
                        tag_result = self.source_acquirer.search_tags(
                            discovery_run_id=run["run_id"], domain=domain_label, now=now, deadline_monotonic=deadline
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
                    )
                    yield from (source for source in refreshed if source["source_type"] == "tag_discovery")

                filtered: dict[str, int] = dict(hotspot_summary["filtered"][domain_label])
                candidate_count = hotspot_summary["candidate_counts"][domain_label]
                sources_read = hotspot_summary["sources_read"][domain_label]
                for index, source in enumerate(ordered_sources()):
                    sources_read += 1
                    if time.monotonic() >= deadline:
                        lifecycle_status, failure_reason = "timed_out", "batch deadline reached before the next source; no request was retried"
                        break
                    outcome, reason_code, detail = self._deterministic_filter(domain_label=domain_label, source=source, now=now)
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
                    if outcome != "eligible":
                        filtered[reason_code] = filtered.get(reason_code, 0) + 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = reason_code
                        continue
                    assembly_payload = self._assembly_payload(
                        run_id=run["run_id"],
                        domain_label=domain_label,
                        source=source,
                        source_version_id=source_result["source_version_id"],
                    )
                    assembly = self.core.create_discovery_input_assembly(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], payload=assembly_payload,
                        prompt_version=DISCOVERY_PROMPT_VERSION, skill_version=DISCOVERY_SKILL_VERSION,
                        idempotency_key=f"{idempotency_key}:{domain_label}:assembly:{index}",
                    )
                    model_result = None
                    try:
                        model_result, judgement = self._run_source_to_topic_skill(
                            run_id=run["run_id"],
                            source_version_id=source_result["source_version_id"],
                            assembly_id=assembly["assembly_id"],
                            input_payload=assembly_payload,
                        )
                    except ModelGatewayError as exc:
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=None,
                            reason_code="model_gateway_failed", detail={"reason": str(exc), "retry": "forbidden_after_uncertain_request"},
                            idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                        )
                        filtered["model_gateway_failed"] = filtered.get("model_gateway_failed", 0) + 1
                        summary["technical_failures"] += 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = "model_gateway_failed"
                        continue
                    except (json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"],
                            source_version_id=source_result["source_version_id"],
                            model_run_id=model_result.envelope_version_id if model_result is not None else None,
                            reason_code="model_output_invalid", detail={"reason": str(exc), "retry": "forbidden_after_uncertain_request"},
                            idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                        )
                        filtered["model_output_invalid"] = filtered.get("model_output_invalid", 0) + 1
                        summary["technical_failures"] += 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = "model_output_invalid"
                        continue
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
                        "normalized_title": _normalize_title(judgement["candidate_topic"]),
                        "normalized_source_title": _normalize_title(str(source["payload"]["title"])),
                        "domain": domain_label,
                        "source_reference": {"source_version_id": source_result["source_version_id"], "source_type": source["source_type"], "source_object_id": source["source_object_id"], "source_object_version": source["source_object_version"], "source_time": source["source_time"], "url": source["payload"].get("url", "")},
                    }
                    self.core.create_discovery_candidate(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                        candidate_id=f"candidate_{_stable_id({domain_label: source_result['source_version_id'], 'title': candidate_payload['normalized_title']})}",
                        payload=candidate_payload, idempotency_key=f"{idempotency_key}:{domain_label}:candidate:{index}",
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
                if lifecycle_status == "timed_out":
                    break
        except KeyboardInterrupt:
            lifecycle_status, failure_reason = "interrupted", "batch interrupted; no uncertain request was retried"
        if lifecycle_status == "completed" and summary["technical_failures"]:
            lifecycle_status, failure_reason = "completed_with_failures", "one or more sources failed and were retained without retry"
        finalization = self.core.complete_discovery_run(
            run_id=run["run_id"], domains=requested_domains, lifecycle_status=lifecycle_status,
            failure_reason=failure_reason, idempotency_key=f"{idempotency_key}:snapshot",
        )
        result = {"run_id": run["run_id"], "discovery_date": discovery_date, "status": lifecycle_status, "execution_mode": execution_mode, "summary": summary}
        self.core.record_completed_command(
            command="stage1b_execute_daily_discovery", idempotency_key=idempotency_key, request=request, task_id=run["run_id"],
            event="stage1b_daily_discovery_finalized", result={**finalization, "technical_failures": str(summary["technical_failures"])},
        )
        return result

    def view_daily_snapshot(
        self, *, run_id: str, domains: tuple[str, ...] = APPROVED_DOMAINS
    ) -> dict[str, list[dict[str, Any]]]:
        requested_domains = tuple(domains)
        if not requested_domains or any(domain_label not in APPROVED_DOMAINS for domain_label in requested_domains):
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
    ) -> dict[str, str]:
        return self.core.select_discovery_candidate(
            candidate_version_id=candidate_version_id,
            actor=actor,
            actor_kind="user",
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def resubmit_pending_hotspot_judgement(
        self,
        *,
        run_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Submit exactly one user-authorized retry from an already frozen hotspot input."""
        if not actor.strip() or not reason.strip():
            raise DailyDiscoveryValidationError("hotspot judgement resubmission requires an actor and reason")
        request = {"run_id": run_id, "actor": actor, "reason": reason}
        replay = self.core.find_command_replay("stage1b_resubmit_hotspot_judgement", idempotency_key, request)
        if replay:
            return {**replay, "replayed": True}
        pending = self.core.pending_hotspot_judgement(run_id=run_id)
        input_payload = pending["input_payload"]
        enabled_domains = tuple(
            str(item.get("domain_label"))
            for item in input_payload.get("enabled_domains", [])
            if isinstance(item, dict) and str(item.get("domain_label") or "") in APPROVED_DOMAINS
        )
        if not enabled_domains:
            raise DailyDiscoveryValidationError("the frozen hotspot input has no approved enabled domain")
        source_version_id = str(pending["source_version_id"])
        source_payload = dict(pending["source_payload"])
        lifecycle_status, failure_reason, candidate_count = "completed", None, 0
        try:
            model_result, judgement = self._run_hotspot_to_opportunity_skill(
                run_id=run_id,
                source_version_id=source_version_id,
                assembly_id=str(pending["assembly_id"]),
                input_payload=input_payload,
                enabled_domains=enabled_domains,
            )
            if judgement["outcome"] == "no_candidate":
                self.core.record_discovery_no_candidate(
                    run_id=run_id,
                    source_version_id=source_version_id,
                    model_run_id=model_result.envelope_version_id,
                    reason_code="hotspot_not_converted",
                    detail={"reason": judgement["reason"]},
                    idempotency_key=f"{idempotency_key}:absence",
                )
            else:
                for candidate_index, candidate in enumerate(judgement["candidates"]):
                    domain = candidate["domain_label"]
                    payload = {
                        "title": candidate["candidate_topic"], "why_attention": candidate["audience_relation"],
                        "new_angle": f"{candidate['topic_angle']}；{candidate['content_increment']}",
                        "material_readiness": "热点仅提供发现材料；正式研究仍需独立核验。",
                        "risk_limits": "；".join(candidate["risks"]) or "热点材料仅用于发现，不得替代正式研究证据。",
                        "originality_relation": "problem_expansion", "normalized_title": _normalize_title(candidate["candidate_topic"]),
                        "normalized_source_title": _normalize_title(str(source_payload["title"])), "domain": domain,
                        "source_reference": {"source_version_id": source_version_id, "source_type": "hotspot", "url": source_payload.get("url", "")},
                        **candidate,
                    }
                    self.core.create_discovery_candidate(
                        run_id=run_id,
                        source_version_id=source_version_id,
                        model_run_id=model_result.envelope_version_id,
                        candidate_id=f"candidate_{_stable_id({domain: source_version_id, 'title': payload['normalized_title']})}",
                        payload=payload,
                        candidate_domain_label=domain,
                        allow_multiple_from_model_run=True,
                        idempotency_key=f"{idempotency_key}:candidate:{candidate_index}",
                    )
                    candidate_count += 1
        except (ModelGatewayError, json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
            lifecycle_status, failure_reason = "completed_with_failures", "hotspot judgement resubmission failed; no automatic retry was attempted"
            self.core.record_discovery_no_candidate(
                run_id=run_id,
                source_version_id=source_version_id,
                model_run_id=None,
                reason_code="hotspot_judgement_failed",
                detail={"reason": str(exc), "retry": "forbidden_after_uncertain_request"},
                idempotency_key=f"{idempotency_key}:failed",
            )
        finalization = self.core.complete_discovery_run(
            run_id=run_id,
            domains=enabled_domains,
            lifecycle_status=lifecycle_status,
            failure_reason=failure_reason,
            idempotency_key=f"{idempotency_key}:finalize",
        )
        result = {**finalization, "candidate_count": candidate_count}
        self.core.record_completed_command(
            command="stage1b_resubmit_hotspot_judgement",
            idempotency_key=idempotency_key,
            request=request,
            task_id=run_id,
            event="stage1b_hotspot_judgement_resubmitted",
            result={key: str(value) for key, value in result.items()},
        )
        return result

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
            if selected_index >= HOTSPOT_DAILY_PROCESS_LIMIT:
                audit_entry.update({
                    "status": "not_selected_after_top_ten",
                    "reason_code": "daily_top_ten_limit",
                    "detail": {"selected_before_this_event": HOTSPOT_DAILY_PROCESS_LIMIT},
                })
                result["hotspot_audit"]["not_selected_after_top_ten"] += 1
                continue
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
            )
            try:
                model_result, judgement = self._run_hotspot_to_opportunity_skill(
                    run_id=run_id, source_version_id=source_result["source_version_id"],
                    assembly_id=assembly["assembly_id"], input_payload=input_payload,
                    enabled_domains=requested_domains,
                )
            except (ModelGatewayError, json.JSONDecodeError, DailyDiscoveryValidationError, FormalSkillValidationError) as exc:
                audit_entry.update({"status": "judgement_failed", "reason_code": "hotspot_judgement_failed", "detail": {"reason": str(exc)}})
                self.core.record_discovery_no_candidate(
                    run_id=run_id, source_version_id=source_result["source_version_id"], model_run_id=None,
                    reason_code="hotspot_judgement_failed", detail={"reason": str(exc), "retry": "forbidden_after_uncertain_request"},
                    idempotency_key=f"{idempotency_key}:hotspot:absence:{index}",
                )
                for domain in requested_domains:
                    result["filtered"][domain]["hotspot_judgement_failed"] = result["filtered"][domain].get("hotspot_judgement_failed", 0) + 1
                    result["source_evidence"][domain] = {"status": "no_candidate", "reason": "hotspot_judgement_failed", "source_refs": []}
                continue
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

    def _run_source_to_topic_skill(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
    ):
        contract = self.source_to_topic_contract
        contract.validate_contract()
        validate_payload(input_payload, contract.input_schema)
        preprocessed = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
        model_input = apply_binding(contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, contract.model_input_schema)
        prompt = contract.portable_skill().render_prompt(model_input)
        request = self.core.prepare_discovery_model_request(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            prompt=prompt,
            skill_name=contract.formal_skill_id,
            skill_version=contract.version,
            skill_hash=contract.skill_hash,
            binding_name=contract.binding_name,
            binding_version=contract.binding_version,
            binding_hash=contract.binding_hash,
            model_input_payload=model_input,
        )
        model_result = self.gateway.complete(request)
        model_output = parse_model_json(model_result.output_text)
        validate_payload(model_output, contract.model_output_schema)
        output_payload = apply_binding(contract.output_map, input_payload, model_output, preprocessed)
        validate_payload(output_payload, contract.output_schema)
        validate_source_to_topic_output_semantics(input_payload, output_payload)
        return model_result, output_payload

    def _run_hotspot_to_opportunity_skill(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        input_payload: dict[str, Any],
        enabled_domains: tuple[str, ...],
    ):
        contract = self.hotspot_to_opportunity_contract
        contract.validate_contract()
        validate_payload(input_payload, contract.input_schema)
        model_input = apply_binding(contract.input_map, input_payload, {}, {})
        validate_payload(model_input, contract.model_input_schema)
        prompt = contract.portable_skill().render_prompt(model_input)
        request = self.core.prepare_discovery_model_request(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            prompt=prompt,
            skill_name=contract.formal_skill_id,
            skill_version=contract.version,
            skill_hash=contract.skill_hash,
            binding_name=contract.binding_name,
            binding_version=contract.binding_version,
            binding_hash=contract.binding_hash,
            model_input_payload=model_input,
        )
        model_result = self.gateway.complete(request)
        model_output = parse_model_json(model_result.output_text)
        validate_payload(model_output, contract.model_output_schema)
        output_payload = apply_binding(contract.output_map, input_payload, model_output, {})
        validate_payload(output_payload, contract.output_schema)
        return model_result, validate_hotspot_opportunity_judgement(
            output_payload, enabled_domains=enabled_domains
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

    def _deterministic_filter(self, *, domain_label: str, source: dict[str, Any], now: datetime) -> tuple[str, str, dict[str, Any]]:
        if source["source_type"] not in {
            "hotspot", "daily_competitor_content", "historical_high_signal", "tag_discovery",
            "question_expansion", "saved_user_direction",
        }:
            return "excluded", "source_not_qualified", {}
        if domain_label not in FORMAL_DOMAIN_LABELS:
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
            if self.core.discovery_source_seen(source_type=source["source_type"], source_object_id=source["source_object_id"], source_object_version=source["source_object_version"]):
                return "excluded", "source_already_processed", {}
            return "eligible", "eligible", {"normalized_source_title": normalized_title}
        title_folded = title.casefold()
        exclude_terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("exclude_terms", []))
        matched_terms = [term for term in exclude_terms if term.casefold() in title_folded]
        if matched_terms:
            return "excluded", "outside_domain_policy", {"matched_terms": matched_terms}
        if self.core.discovery_source_seen(source_type=source["source_type"], source_object_id=source["source_object_id"], source_object_version=source["source_object_version"]):
            return "excluded", "source_already_processed", {}
        if self.core.discovery_candidate_in_cooldown(domain_label=domain_label, normalized_title=normalized_title, now=now.isoformat()):
            return "excluded", "cooldown_active", {}
        if self.core.formal_topic_title_seen(domain_label=domain_label, normalized_title=normalized_title):
            return "excluded", "already_produced", {}
        return "eligible", "eligible", {"normalized_source_title": normalized_title}

    @staticmethod
    def _expires_at(source: dict[str, Any], *, now: datetime) -> str | None:
        if source["source_type"] not in {"hotspot", "daily_competitor_content"}:
            return None
        return (_parse_time(source["source_time"]) + timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()

    @staticmethod
    def _assembly_payload(*, run_id: str, domain_label: str, source: dict[str, Any], source_version_id: str) -> dict[str, Any]:
        payload = source["payload"]
        policy = get_discovery_policy(domain_label)
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
        if account_name:
            source_content_parts.append(f"来源账号/渠道：{account_name}")
        if url:
            source_content_parts.append(f"来源链接：{url}")
        source_content = "；".join(source_content_parts)
        evidence_items = [title]
        if url:
            evidence_items.append(url)
        if account_name:
            evidence_items.append(account_name)
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
        domain_summary = (
            f"当前领域为 {domain_label}。排除类型/词包括：{', '.join(str(term) for term in policy.get('exclude_terms', [])) or '无'}。"
            f"热点匹配词包括：{', '.join(str(term) for term in policy.get('hotspot_match_terms', [])) or '无'}。"
        )
        return {
            "request_id": f"source_to_topic_{source_version_id}",
            "correlation_id": run_id,
            "source_id": source_version_id,
            "source_content": source_content,
            "source_evidence_items": evidence_items[:63],
            "domain_label": domain_label,
            "relation_summary": "deterministic prefilter passed; duplicate, cooldown and prior production checks are clear for this source version.",
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
                "stop_reason": "none",
            },
            "duplicate_cooling_status": {
                "duplicate_status": "clear",
                "cooling_status": "clear",
                "related_refs": [],
            },
            "domain_rule_summary": domain_summary,
            "experience_cards": [],
            "user_direction": "",
            "schema_version": "source_to_topic.input.v1",
        }


def main(argv: list[str] | None = None) -> int:
    """Run one Core-owned Stage 1B batch, reuse frozen input, resubmit, or purge a real daily validation."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=APPROVED_DOMAINS, help="one authorized formal domain for a new batch")
    parser.add_argument(
        "--source-type",
        action="append",
        choices=SOURCE_TYPES,
        help="source type to run; real_daily_validation runs one complete source path, production_daily always uses all six",
    )
    parser.add_argument("--actor", required=True, help="audited actor for the explicit user authorization")
    parser.add_argument("--idempotency-key", required=True, help="stable key for this exact authorized run")
    parser.add_argument("--mode", required=True, choices=EXECUTION_MODES, help="explicit execution identity; no mode can be promoted automatically")
    parser.add_argument("--discovery-date", default=datetime.now(timezone.utc).date().isoformat(), help="YYYY-MM-DD (defaults to UTC today)")
    parser.add_argument("--batch-timeout-seconds", type=int, default=600, help="bounded batch deadline; timed-out or interrupted batches are not retried")
    parser.add_argument("--reuse-hotspot-run", help="successful hotspot collection run whose temporary batch is reused without collecting again")
    parser.add_argument("--resubmit-hotspot-judgement-run", help="processing real daily hotspot run whose frozen judgement the user explicitly authorizes to resubmit")
    parser.add_argument("--purge-real-daily-validation-run", help="existing real daily validation run to physically purge after explicit confirmation")
    parser.add_argument("--expected-candidate-count", type=int, help="required candidate count guard when purging a real daily validation run")
    parser.add_argument("--confirmation", help="exact purge confirmation token")
    parser.add_argument("--reason", help="required audit reason for a resubmission or purge")
    args = parser.parse_args(argv)
    special_actions = sum(bool(value) for value in (args.purge_real_daily_validation_run, args.reuse_hotspot_run, args.resubmit_hotspot_judgement_run))
    if special_actions > 1:
        parser.error("choose only one special action")
    if args.purge_real_daily_validation_run:
        if args.mode != "real_daily_validation" or not args.reason or args.domain or args.expected_candidate_count is None or not args.confirmation:
            parser.error(
                "purge requires --purge-real-daily-validation-run, --mode real_daily_validation, --actor, --reason, "
                "--expected-candidate-count, --confirmation, and no --domain"
            )
    elif args.resubmit_hotspot_judgement_run:
        if args.mode != "real_daily_validation" or not args.reason or args.domain or args.source_type:
            parser.error("resubmission requires --resubmit-hotspot-judgement-run, --mode real_daily_validation, --actor, --reason, and no --domain or --source-type")
    elif not args.domain:
        parser.error("a new batch requires --domain")

    selected_source_types = tuple(args.source_type or SOURCE_TYPES)
    if args.reuse_hotspot_run and set(selected_source_types) != {"hotspot"}:
        parser.error("--reuse-hotspot-run requires exactly --source-type hotspot")
    core = Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="production")
    try:
        if args.purge_real_daily_validation_run:
            result = core.purge_real_daily_validation_run(
                run_id=args.purge_real_daily_validation_run,
                actor=args.actor,
                reason=args.reason or "",
                expected_candidate_count=args.expected_candidate_count if args.expected_candidate_count is not None else -1,
                confirmation=args.confirmation or "",
            )
            print(_canonical(result))
            return 0
        if args.resubmit_hotspot_judgement_run:
            gateway = build_production_daily_discovery_gateway(core)
            service = Stage1BDailyDiscoveryService(core=core, gateway=gateway)
            result = service.resubmit_pending_hotspot_judgement(
                run_id=args.resubmit_hotspot_judgement_run,
                actor=args.actor,
                reason=args.reason or "",
                idempotency_key=args.idempotency_key,
            )
            print(_canonical(result))
            return 0 if result["status"] == "completed" else 2
        source_acquirer = (
            build_production_source_acquirer(core, source_types=selected_source_types)
            if {"hotspot", "tag_discovery"} & set(selected_source_types)
            else None
        )
        gateway = build_production_daily_discovery_gateway(core)
        service = Stage1BDailyDiscoveryService(core=core, gateway=gateway, source_acquirer=source_acquirer)
        result = service.run_daily_discovery(
            discovery_date=args.discovery_date,
            actor=args.actor,
            idempotency_key=args.idempotency_key,
            execution_mode=args.mode,
            batch_timeout_seconds=args.batch_timeout_seconds,
            domains=(args.domain,),
            source_types=selected_source_types,
            reuse_hotspot_discovery_run_id=args.reuse_hotspot_run,
        )
        snapshot = service.view_daily_snapshot(run_id=result["run_id"], domains=(args.domain,))
        print(_canonical({**result, "snapshot": snapshot}))
        return 0 if result["status"] == "completed" else 2
    finally:
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
