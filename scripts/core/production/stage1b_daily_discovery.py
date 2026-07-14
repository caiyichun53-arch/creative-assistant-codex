"""Stage 1B controlled daily candidate discovery.

Only already-recorded, real source facts may enter this path.  It creates
candidate snapshots and user decisions, never a production task by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig
from scripts.core.model_gateway.model_router import DEFAULT_MODEL_ENV_PATH, ModelRouter, ModelRouterError
from scripts.core.production.stage0_content_core import (
    CoreDiscoveryModelRunMaterializer,
    Stage0ContentProductionCore,
    StateTransitionError,
)


APPROVED_DOMAINS = ("fan_kepu_social_life", "music_entertainment")
DAILY_SOURCE_VALIDITY_HOURS = 72
SOURCE_READ_LIMIT = 6
DISCOVERY_PROMPT_VERSION = "stage1b.daily_discovery.prompt.v1"
DISCOVERY_SKILL_VERSION = "stage1b.source_to_topic.skill.v1"
ORIGINALITY_RELATIONSHIPS = frozenset({"same_topic_original_reconstruction", "problem_expansion", "independent_research"})
RISK_BLOCK_TERMS = {
    "fan_kepu_social_life": ("处方", "诊断", "偏方", "急救"),
    "music_entertainment": ("八卦", "绯闻", "恋情", "私生活", "粉圈", "撕"),
}


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
        key: _required_text(payload, key)
        for key in ("title", "core_question", "why_attention", "new_angle", "material_readiness", "risk_limits")
    }
    relation = _required_text(payload, "originality_relation")
    if relation not in ORIGINALITY_RELATIONSHIPS:
        raise DailyDiscoveryValidationError("candidate originality_relation is unsupported")
    normalized["originality_relation"] = relation
    normalized["outcome"] = outcome
    return normalized


def daily_discovery_prompt(input_payload: dict[str, Any]) -> str:
    return (
        "You make a limited candidate judgement from one already-qualified source only. "
        "Do not search, fetch, download, transcribe, analyze a video, or claim facts absent from controlled input. "
        "The source is only a discovery clue, never research evidence. Do not choose a formal topic. "
        "Return exactly one JSON object. If no candidate is justified, return only outcome=no_candidate and a non-empty reason. "
        "Otherwise return outcome=candidate plus title, core_question, why_attention, new_angle, material_readiness, "
        "risk_limits, originality_relation. originality_relation must be one of same_topic_original_reconstruction, "
        "problem_expansion, independent_research. Never return score, rank, weight, recommendation_score, or quality_rank. "
        "Explicitly state uncertainty and material gaps.\n\n"
        f"Controlled input: {_canonical(input_payload)}"
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
    """Build the explicit Mimo path; provider calls remain inside ModelGateway."""
    if core.data_identity != "production":
        raise StateTransitionError("production daily-discovery gateway requires production data identity")
    router = ModelRouter.from_file()
    definition = router.routes.get("business_analysis")
    if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
        raise ModelRouterError("daily discovery requires an explicit business topic_screening route with fallback none")
    provider = router.providers.get(definition.provider_ref)
    if provider is None or provider.provider_type != "mimo":
        raise ModelRouterError("daily discovery requires the configured Mimo provider")
    route = router.resolve_bound_route("business_analysis", route_name="stage1b.daily_discovery")
    if route.provider_name != "hermes":
        raise ModelRouterError("daily discovery route has an unexpected provider adapter")
    auth_ref, endpoint_ref = str(provider.settings.get("auth_ref") or ""), str(provider.settings.get("endpoint_ref") or "")
    if not auth_ref or not endpoint_ref:
        raise ModelRouterError("configured Mimo provider lacks auth_ref or endpoint_ref")
    api_key, base_url = _configured_environment_value(auth_ref), _configured_environment_value(endpoint_ref)
    if not base_url.startswith(("https://", "http://")):
        raise ModelRouterError("configured Mimo endpoint must be an HTTP(S) URL")
    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=api_key, base_url=base_url, model=route.model_name, timeout_seconds=90, max_retries=0))
    return ModelGateway(routes={route.route_name: route}, providers={adapter.provider_name: adapter}, materializer=CoreDiscoveryModelRunMaterializer(core))


class Stage1BDailyDiscoveryService:
    """The controlled real-source -> candidate snapshot -> user decision path."""

    def __init__(self, *, core: Stage0ContentProductionCore, gateway: ModelGateway):
        self.core = core
        self.gateway = gateway

    def run_daily_discovery(
        self,
        *,
        discovery_date: str,
        actor: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        request = {"discovery_date": discovery_date, "actor": actor, "domains": list(APPROVED_DOMAINS)}
        replay = self.core.find_command_replay("stage1b_execute_daily_discovery", idempotency_key, request)
        if replay:
            return {**replay, "replayed": True}
        run = self.core.create_discovery_run(discovery_date=discovery_date, actor=actor, idempotency_key=f"{idempotency_key}:run")
        summary: dict[str, Any] = {"run_id": run["run_id"], "discovery_date": discovery_date, "domains": {}, "filtered": {}, "source_readiness": {}}
        daily_since = (now - timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()
        for domain_label in APPROVED_DOMAINS:
            summary["source_readiness"][domain_label] = self.core.discovery_source_readiness(domain_label=domain_label, daily_since=daily_since)
            sources = self.core.load_real_discovery_sources(domain_label=domain_label, daily_since=daily_since, per_source_limit=SOURCE_READ_LIMIT)
            filtered: dict[str, int] = {}
            candidate_count = 0
            for index, source in enumerate(sources):
                outcome, reason_code, detail = self._deterministic_filter(
                    domain_label=domain_label, source=source, now=now
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
                if outcome != "eligible":
                    filtered[reason_code] = filtered.get(reason_code, 0) + 1
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
                    judgement = validate_candidate_judgement(json.loads(model_result.output_text))
                except ModelGatewayError as exc:
                    self.core.record_discovery_no_candidate(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=None,
                        reason_code="model_gateway_failed", detail={"reason": str(exc)},
                        idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                    )
                    filtered["model_gateway_failed"] = filtered.get("model_gateway_failed", 0) + 1
                    continue
                except (json.JSONDecodeError, DailyDiscoveryValidationError) as exc:
                    self.core.record_discovery_no_candidate(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                        reason_code="model_output_invalid", detail={"reason": str(exc)},
                        idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                    )
                    filtered["model_output_invalid"] = filtered.get("model_output_invalid", 0) + 1
                    continue
                if judgement["outcome"] == "no_candidate":
                    self.core.record_discovery_no_candidate(
                        run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                        reason_code="model_returned_no_candidate", detail={"reason": judgement["reason"]},
                        idempotency_key=f"{idempotency_key}:{domain_label}:absence:{index}",
                    )
                    filtered["model_returned_no_candidate"] = filtered.get("model_returned_no_candidate", 0) + 1
                    continue
                candidate_payload = {
                    **judgement,
                    "normalized_title": _normalize_title(judgement["title"]),
                    "normalized_source_title": _normalize_title(str(source["payload"]["title"])),
                    "domain": domain_label,
                    "source_reference": {"source_version_id": source_result["source_version_id"], "source_type": source["source_type"], "source_object_id": source["source_object_id"], "source_object_version": source["source_object_version"], "source_time": source["source_time"], "url": source["payload"]["url"]},
                }
                self.core.create_discovery_candidate(
                    run_id=run["run_id"], source_version_id=source_result["source_version_id"], model_run_id=model_result.envelope_version_id,
                    candidate_id=f"candidate_{_stable_id({domain_label: source_result['source_version_id'], 'title': candidate_payload['normalized_title']})}",
                    payload=candidate_payload, idempotency_key=f"{idempotency_key}:{domain_label}:candidate:{index}",
                )
                candidate_count += 1
            summary["domains"][domain_label] = {"sources_read": len(sources), "candidates": candidate_count}
            summary["filtered"][domain_label] = filtered
        self.core.complete_discovery_run(run_id=run["run_id"], domains=APPROVED_DOMAINS, idempotency_key=f"{idempotency_key}:snapshot")
        result = {"run_id": run["run_id"], "discovery_date": discovery_date, "status": "completed", "summary": summary}
        self.core.record_completed_command(command="stage1b_execute_daily_discovery", idempotency_key=idempotency_key, request=request, task_id=run["run_id"], event="stage1b_daily_discovery_completed", result={"run_id": run["run_id"], "status": "completed"})
        return result

    def view_daily_snapshot(self, *, run_id: str) -> dict[str, list[dict[str, Any]]]:
        return {domain_label: self.core.get_discovery_snapshot(run_id=run_id, domain_label=domain_label) for domain_label in APPROVED_DOMAINS}

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
        if source["source_type"] not in {"daily_competitor_content", "historical_high_signal"}:
            return "excluded", "source_not_qualified", {}
        if domain_label not in APPROVED_DOMAINS:
            return "excluded", "domain_mismatch", {}
        title, url = str(source["payload"].get("title") or ""), str(source["payload"].get("url") or "")
        if len(title.strip()) < 6 or not url:
            return "excluded", "material_obviously_insufficient", {}
        if source["source_type"] == "daily_competitor_content" and _parse_time(source["source_time"]) + timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS) < now:
            return "excluded", "freshness_expired", {}
        if any(term in title for term in RISK_BLOCK_TERMS[domain_label]):
            return "excluded", "risk_blocked", {"matched_terms": [term for term in RISK_BLOCK_TERMS[domain_label] if term in title]}
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
        if source["source_type"] != "daily_competitor_content":
            return None
        return (_parse_time(source["source_time"]) + timedelta(hours=DAILY_SOURCE_VALIDITY_HOURS)).isoformat()

    @staticmethod
    def _assembly_payload(*, domain_label: str, source: dict[str, Any], source_version_id: str) -> dict[str, Any]:
        payload = source["payload"]
        return {
            "domain": domain_label,
            "source_version_id": source_version_id,
            "source_type": source["source_type"],
            "source_object": {"id": source["source_object_id"], "version": source["source_object_version"], "time": source["source_time"], "title": payload["title"], "url": payload["url"], "account_name": payload["account_name"]},
            "user_requirements": "daily discovery only; do not create a formal topic",
            "materials_and_facts": [{"kind": "source_clue", "reference": source_version_id}],
            "considered_experience": [], "adopted_experience": [], "rejected_experience": [], "omitted_materials": [],
            "input_completeness": "source_clue_only",
            "prompt_version": DISCOVERY_PROMPT_VERSION, "skill_version": DISCOVERY_SKILL_VERSION,
        }
