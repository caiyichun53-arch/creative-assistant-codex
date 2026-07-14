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

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig
from scripts.core.model_gateway.model_router import DEFAULT_MODEL_ENV_PATH, ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import (
    CoreDiscoveryModelRunMaterializer,
    DataIdentityError,
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.business_data.daily_source_acquisition import DailyDiscoverySourceAcquirer, validate_hotspot_collection_contract
from scripts.core.business_data.run_domain_search import validate_domain_search_execution_contract
from scripts.core.business_data.domain_labels import FORMAL_DOMAIN_LABELS, get_discovery_policy
from scripts.core.external_adapters import LocalMediaCrawlerExecutor, LocalTrendRadarExecutor


APPROVED_DOMAINS = tuple(sorted(FORMAL_DOMAIN_LABELS))
EXECUTION_MODES = ("test_isolated", "validation_live", "production_daily")
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
HOTSPOT_DAILY_PROCESS_LIMIT = 3
DAILY_SOURCE_VALIDITY_HOURS = 72
SOURCE_READ_LIMIT = 6
DISCOVERY_PROMPT_VERSION = "stage1b.daily_discovery.prompt.v1"
DISCOVERY_SKILL_VERSION = "stage1b.source_to_topic.skill.v1"
ORIGINALITY_RELATIONSHIPS = frozenset({"same_topic_original_reconstruction", "problem_expansion", "independent_research"})
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
    latin_words = re.findall(r"[A-Za-z]{3,}", value)
    if han_count < 4 or latin_words:
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


def parse_candidate_judgement_output(output_text: str) -> dict[str, Any]:
    """Parse one JSON object, tolerating only a single exact JSON code fence."""
    text = output_text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return validate_candidate_judgement(json.loads(text))


def candidate_rejection(domain_label: str, judgement: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Apply deterministic post-model domain and material gates before candidate storage."""
    if judgement.get("outcome") != "candidate":
        return None
    policy = get_discovery_policy(domain_label)
    candidate_text = "\n".join(
        str(judgement.get(key) or "")
        for key in ("title", "core_question", "why_attention", "new_angle")
    ).casefold()
    exclude_terms = tuple(str(term) for term in policy.get("exclude_terms", []))
    matched_terms = sorted({term for term in exclude_terms if term.casefold() in candidate_text})
    if matched_terms:
        return "candidate_outside_domain_policy", {"matched_terms": matched_terms}
    material_readiness = str(judgement.get("material_readiness") or "")
    insufficient_markers = ("材料严重不足", "事实无法确认", "无法确认事实", "无法核实事实")
    matched_markers = [marker for marker in insufficient_markers if marker in material_readiness]
    if matched_markers:
        return "candidate_material_insufficient", {"matched_markers": matched_markers}
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
    """Build the explicitly configured candidate-judgement route."""
    if core.data_identity != "production":
        raise StateTransitionError("production daily-discovery gateway requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
        raise ModelRouterError("daily discovery requires an explicit business topic_screening route with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None or provider.provider_type != "mimo":
        raise ModelRouterError("configured candidate-judgement provider is unsupported by the current adapter")
    route = router.resolve_bound_route("business_analysis", route_name="stage1b.daily_discovery")
    if route.provider_name != "hermes":
        raise ModelRouterError("daily discovery route has an unexpected provider adapter")
    auth_ref, endpoint_ref = str(provider.settings.get("auth_ref") or ""), str(provider.settings.get("endpoint_ref") or "")
    if not auth_ref or not endpoint_ref:
        raise ModelRouterError("configured candidate-judgement provider lacks auth_ref or endpoint_ref")
    api_key, base_url = _configured_environment_value(auth_ref), _configured_environment_value(endpoint_ref)
    if not base_url.startswith(("https://", "http://")):
        raise ModelRouterError("configured candidate-judgement endpoint must be an HTTP(S) URL")
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

    def __init__(self, *, core: Stage0ContentProductionCore, gateway: ModelGateway, source_acquirer: DailyDiscoverySourceAcquirer | None = None):
        self.core = core
        self.gateway = gateway
        self.source_acquirer = source_acquirer

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
        if execution_mode == "validation_live" and len(requested_source_types) != 1:
            raise DailyDiscoveryValidationError("validation_live must validate exactly one source type")
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
        lifecycle_status, failure_reason = "completed", None
        try:
            if self.source_acquirer is not None and "hotspot" in requested_source_types:
                try:
                    hotspot = self.source_acquirer.collect_hotspots(
                        discovery_run_id=run["run_id"], now=now, deadline_monotonic=deadline
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
                    hotspot_failed = hotspot["status"] not in {"completed", "completed_with_failures"}
                    if hotspot_failed or hotspot["status"] == "completed_with_failures":
                        summary["technical_failures"] += 1
                except Exception as exc:
                    summary["acquisition"] = {
                        "execution_order": ["trendradar_hotspot"], "status": "failed", "reason": str(exc), "retry": "forbidden", "tag_search": {}
                    }
                    summary["technical_failures"] += 1
            elif "hotspot" not in requested_source_types:
                summary["acquisition"] = {"status": "not_selected", "reason": "hotspot is outside this source-specific run", "tag_search": {}}
            for domain_label in requested_domains:
                source_evidence: dict[str, dict[str, Any]] = {
                    source_type: {"status": "not_available", "reason": "no_source_available", "source_refs": []}
                    for source_type in DAILY_REPORT_SOURCE_TYPES
                    if source_type in requested_source_types
                }
                summary["source_readiness"][domain_label] = self.core.discovery_source_readiness(domain_label=domain_label, daily_since=daily_since)
                loaded_sources = [
                    source for source in self.core.load_real_discovery_sources(
                        domain_label=domain_label, daily_since=daily_since, per_source_limit=SOURCE_READ_LIMIT
                    )
                    if source["source_type"] in requested_source_types
                ]
                hotspot_seen = 0
                limited_sources: list[dict[str, Any]] = []
                for source in loaded_sources:
                    if source["source_type"] == "hotspot":
                        hotspot_seen += 1
                        if hotspot_seen > HOTSPOT_DAILY_PROCESS_LIMIT:
                            source_evidence.setdefault("hotspot", {"status": "not_available", "reason": "no_source_available", "source_refs": []})
                            source_evidence["hotspot"]["truncated_after"] = HOTSPOT_DAILY_PROCESS_LIMIT
                            continue
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
                        domain_label=domain_label, daily_since=daily_since, per_source_limit=SOURCE_READ_LIMIT
                    )
                    yield from (source for source in refreshed if source["source_type"] == "tag_discovery")

                filtered: dict[str, int] = {}
                candidate_count = 0
                sources_read = 0
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
                    assembly_payload = self._assembly_payload(domain_label=domain_label, source=source, source_version_id=source_result["source_version_id"])
                    assembly = self.core.create_discovery_input_assembly(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], payload=assembly_payload,
                        prompt_version=DISCOVERY_PROMPT_VERSION, skill_version=DISCOVERY_SKILL_VERSION,
                        idempotency_key=f"{idempotency_key}:{domain_label}:assembly:{index}",
                    )
                    model_request = self.core.prepare_discovery_model_request(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], assembly_id=assembly["assembly_id"],
                        prompt=daily_discovery_prompt(assembly_payload),
                    )
                    try:
                        model_result = self.gateway.complete(model_request)
                        judgement = parse_candidate_judgement_output(model_result.output_text)
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
                    except (json.JSONDecodeError, DailyDiscoveryValidationError) as exc:
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                            reason_code="model_output_invalid", detail={"reason": str(exc), "retry": "forbidden_after_uncertain_request"},
                            idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                        )
                        filtered["model_output_invalid"] = filtered.get("model_output_invalid", 0) + 1
                        summary["technical_failures"] += 1
                        if source["source_type"] in source_evidence:
                            source_evidence[source["source_type"]]["status"] = "no_candidate"
                            source_evidence[source["source_type"]]["reason"] = "model_output_invalid"
                        continue
                    if judgement["outcome"] == "no_candidate":
                        self.core.record_discovery_no_candidate(
                            run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                            reason_code="model_returned_no_candidate", detail={"reason": judgement["reason"]},
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
                        "normalized_title": _normalize_title(judgement["title"]),
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
                summary["source_readiness"][domain_label] = self.core.discovery_source_readiness(domain_label=domain_label, daily_since=daily_since)
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
        policy = get_discovery_policy(domain_label)
        risk_terms = tuple(str(term) for term in policy.get("risk_block_terms", []))
        if any(term in title for term in risk_terms):
            return "excluded", "risk_blocked", {"matched_terms": [term for term in risk_terms if term in title]}
        title_folded = title.casefold()
        exclude_terms = tuple(str(term) for term in policy.get("exclude_terms", []))
        matched_terms = [term for term in exclude_terms if term.casefold() in title_folded]
        if matched_terms:
            return "excluded", "outside_domain_policy", {"matched_terms": matched_terms}
        if source["source_type"] == "hotspot":
            match_terms = tuple(str(term) for term in policy.get("hotspot_match_terms", []))
            if not match_terms or not any(term.casefold() in title_folded for term in match_terms):
                return "excluded", "hotspot_domain_mismatch", {}
        normalized_title = _normalize_title(title)
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
    def _assembly_payload(*, domain_label: str, source: dict[str, Any], source_version_id: str) -> dict[str, Any]:
        payload = source["payload"]
        policy = get_discovery_policy(domain_label)
        return {
            "domain": domain_label,
            "source_version_id": source_version_id,
            "source_type": source["source_type"],
            "domain_policy": {
                "exclude_terms": list(policy.get("exclude_terms", [])),
                "hotspot_match_terms": list(policy.get("hotspot_match_terms", [])),
            },
            "source_object": {"id": source["source_object_id"], "version": source["source_object_version"], "time": source["source_time"], "title": payload["title"], "url": payload.get("url", ""), "account_name": payload.get("account_name", "")},
            "user_requirements": "daily discovery only; do not create a formal topic",
            "materials_and_facts": [{"kind": "source_clue", "reference": source_version_id}],
            "considered_experience": [], "adopted_experience": [], "rejected_experience": [], "omitted_materials": [],
            "input_completeness": "source_clue_only",
            "prompt_version": DISCOVERY_PROMPT_VERSION, "skill_version": DISCOVERY_SKILL_VERSION,
        }


def main(argv: list[str] | None = None) -> int:
    """Run one Core-owned Stage 1B batch, or classify a prior live validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=APPROVED_DOMAINS, help="one authorized formal domain for a new batch")
    parser.add_argument(
        "--source-type",
        action="append",
        choices=SOURCE_TYPES,
        help="source type to run; validation_live requires exactly one, production_daily always uses all six",
    )
    parser.add_argument("--actor", required=True, help="audited actor for the explicit user authorization")
    parser.add_argument("--idempotency-key", required=True, help="stable key for this exact authorized run")
    parser.add_argument("--mode", required=True, choices=EXECUTION_MODES, help="explicit execution identity; no mode can be promoted automatically")
    parser.add_argument("--discovery-date", default=datetime.now(timezone.utc).date().isoformat(), help="YYYY-MM-DD (defaults to UTC today)")
    parser.add_argument("--batch-timeout-seconds", type=int, default=600, help="bounded batch deadline; timed-out or interrupted batches are not retried")
    parser.add_argument("--reclassify-run", help="existing run to mark as validation_live without re-running sources or models")
    parser.add_argument("--reason", help="required audit reason when reclassifying an existing run")
    args = parser.parse_args(argv)
    if args.reclassify_run:
        if args.mode != "validation_live" or not args.reason or args.domain:
            parser.error("reclassification requires --reclassify-run, --mode validation_live, --actor, --reason, and no --domain")
    elif not args.domain:
        parser.error("a new batch requires --domain")

    selected_source_types = tuple(args.source_type or SOURCE_TYPES)
    core = Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="production")
    try:
        if args.reclassify_run:
            result = core.classify_existing_discovery_run_as_validation_live(
                run_id=args.reclassify_run,
                actor=args.actor,
                reason=args.reason,
                idempotency_key=args.idempotency_key,
            )
            print(_canonical(result))
            return 0
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
        )
        snapshot = service.view_daily_snapshot(run_id=result["run_id"], domains=(args.domain,))
        print(_canonical({**result, "snapshot": snapshot}))
        return 0 if result["status"] == "completed" else 2
    finally:
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
