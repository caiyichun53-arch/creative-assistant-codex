from __future__ import annotations

import argparse
import contextlib
import decimal
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig

ALLOWED_STATUSES = {
    "NOT_READY",
    "PREFLIGHT_PASSED",
    "DRY_RUN_PASSED",
    "LIVE_PASSED",
    "LIVE_FAILED",
    "BLOCKED_MISSING_CREDENTIAL",
    "BLOCKED_MISSING_TEST_ENV",
    "BLOCKED_MISSING_AUTHORIZATION",
}

GATE_IDS = (
    "GATE-HERMES-REAL-HOST",
    "GATE-FEISHU-THIN-BINDING",
    "GATE-MODEL-PROVIDER",
    "GATE-ASR",
    "GATE-SEARCH-PROVIDER",
    "GATE-EXTERNAL-COLLECTOR-ADAPTER",
    "GATE-SHADOW-E2E",
    "GATE-CONTINUOUS-FAULT-RECOVERY",
)

DEFAULT_CONFIG = ROOT / "config" / "live_gates.example.yaml"
DEFAULT_STATUS = ROOT / "EXTERNAL_LIVE_GATE_STATUS.yaml"
DEFAULT_REPORT = ROOT / "EXTERNAL_LIVE_GATE_VALIDATION_REPORT.md"


class LiveGateError(RuntimeError):
    pass


class MissingCredential(LiveGateError):
    pass


class MissingTestEnvironment(LiveGateError):
    pass


class MissingAuthorization(LiveGateError):
    pass


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    started_at: str
    finished_at: str
    environment: str
    adapter: str
    request_hash: str
    response_hash: str
    correlation_id: str
    exit_code: int
    status: str
    external_side_effect: bool
    failure_reason: str
    evidence_location: str
    mode: str

    def as_dict(self) -> dict[str, Any]:
        data = {
            "gate_id": self.gate_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "environment": self.environment,
            "adapter": self.adapter,
            "request_hash": self.request_hash,
            "response_hash": self.response_hash,
            "correlation_id": self.correlation_id,
            "exit_code": self.exit_code,
            "status": self.status,
            "external_side_effect": self.external_side_effect,
            "failure_reason": self.failure_reason,
            "evidence_location": self.evidence_location,
            "mode": self.mode,
        }
        assert data["status"] in ALLOWED_STATUSES
        return data


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return lowered in {"placeholder", "changeme", "change_me", "todo"} or stripped.startswith("__")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if _looks_secret_key(key) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 20 and any(marker in value.lower() for marker in ("secret", "token", "key")):
        return "<redacted>"
    return value


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("secret", "token", "cookie", "session", "api_key", "password"))


def _required_env_value(config: "LiveGateConfig", key: str) -> str:
    value = config.env_value(key)
    if is_placeholder(value):
        raise MissingCredential(key)
    return str(value)


def _valid_base_url(value: str | None) -> bool:
    if is_placeholder(value):
        return False
    return str(value).startswith(("http://", "https://"))


def _valid_decimal(value: str | None) -> bool:
    if is_placeholder(value):
        return False
    try:
        parsed = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return False
    return parsed >= 0


def _decimal_value(value: str | None) -> decimal.Decimal:
    if not _valid_decimal(value):
        raise MissingTestEnvironment("MODEL_PROVIDER_COST_CAP must be a decimal USD amount")
    return decimal.Decimal(str(value))


def _cost_cap_result(cost: dict[str, Any], cap: decimal.Decimal) -> dict[str, Any]:
    currency = str(cost.get("currency", "")).upper()
    amount = cost.get("amount")
    if currency != "USD" or amount is None:
        return {
            "cost_cap_status": "not_available",
            "cost_cap_unit": "USD",
            "cost_cap": str(cap),
            "cost_amount": "not_available",
            "cost_currency": currency or "not_available",
            "cost_cap_comparison": "not_available",
        }
    try:
        actual = decimal.Decimal(str(amount))
    except decimal.InvalidOperation:
        return {
            "cost_cap_status": "not_available",
            "cost_cap_unit": "USD",
            "cost_cap": str(cap),
            "cost_amount": "not_available",
            "cost_currency": currency,
            "cost_cap_comparison": "not_available",
        }
    return {
        "cost_cap_status": "within_cap" if actual <= cap else "exceeded",
        "cost_cap_unit": "USD",
        "cost_cap": str(cap),
        "cost_amount": str(actual),
        "cost_currency": currency,
        "cost_cap_comparison": "actual_usd_amount_lte_configured_usd_cap",
    }


class LiveGateConfig:
    def __init__(self, path: Path = DEFAULT_CONFIG, *, env_path: Path | None = None):
        self.path = path
        self.raw = load_yaml(path)
        self.env_path = env_path or ROOT / str(self.raw.get("env_file", ".env.live-gates"))
        self.env_file = load_env_file(self.env_path)
        self.evidence_root = ROOT / str(self.raw.get("evidence_root", "validation_evidence"))
        self.status_file = ROOT / str(self.raw.get("status_file", "EXTERNAL_LIVE_GATE_STATUS.yaml"))
        self.environment = str(self.raw.get("environment", "validation"))
        self.allow_live_calls = bool(self.raw.get("allow_live_calls", False))
        self.shadow_only = bool(self.raw.get("shadow_only", True))
        self.allowed_database_roots = tuple(str(item) for item in self.raw.get("allowed_database_roots", []))
        self.gates = {str(gate["gate_id"]): gate for gate in self.raw.get("gates", [])}
        missing = set(GATE_IDS) - set(self.gates)
        if missing:
            raise LiveGateError(f"missing gate config: {', '.join(sorted(missing))}")

    def gate(self, gate_id: str) -> dict[str, Any]:
        if gate_id not in self.gates:
            raise LiveGateError(f"unknown gate_id: {gate_id}")
        return self.gates[gate_id]

    def env_value(self, key: str) -> str | None:
        return os.environ.get(key) or self.env_file.get(key)

    def credential_missing(self, gate: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for key in gate.get("required_env", []):
            if is_placeholder(self.env_value(str(key))):
                missing.append(str(key))
        return missing

    def test_env_missing(self, gate: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for item in gate.get("required_test_env", []):
            kind = str(item.get("kind", "path"))
            name = str(item.get("name", "unnamed"))
            value = str(item.get("value", ""))
            if kind == "path_exists" and not (ROOT / value).exists():
                missing.append(name)
            elif kind == "configured_value" and is_placeholder(value):
                missing.append(name)
            elif kind == "isolated_database" and not self._is_isolated_database(value):
                missing.append(name)
        return missing

    def _is_isolated_database(self, value: str) -> bool:
        normalized = value.replace("\\", "/").strip()
        if not normalized or normalized in {"data/creation.db", "./data/creation.db"}:
            return False
        return any(normalized.startswith(root.replace("\\", "/").rstrip("/") + "/") for root in self.allowed_database_roots)


class GateStatusStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "status": "NOT_READY",
                "updated_at": None,
                "gates": {gate_id: {"status": "NOT_READY", "latest_result": None} for gate_id in GATE_IDS},
            }
        return load_yaml(self.path)

    def write_result(self, result: GateResult) -> None:
        data = self.read()
        data["updated_at"] = result.finished_at
        data.setdefault("gates", {})
        data["gates"][result.gate_id] = {
            "status": result.status,
            "latest_result": result.as_dict(),
        }
        data["status"] = summarize_status(data)
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def summarize_status(status_data: dict[str, Any]) -> str:
    statuses = [str(item.get("status", "NOT_READY")) for item in status_data.get("gates", {}).values()]
    if any(status == "LIVE_FAILED" for status in statuses):
        return "LIVE_FAILED"
    if statuses and all(status == "LIVE_PASSED" for status in statuses):
        return "LIVE_PASSED"
    if statuses and all(status in {"DRY_RUN_PASSED", "LIVE_PASSED"} for status in statuses):
        return "DRY_RUN_PASSED"
    if any(status.startswith("BLOCKED_") for status in statuses):
        return "NOT_READY"
    if statuses and all(status in {"PREFLIGHT_PASSED", "DRY_RUN_PASSED", "LIVE_PASSED"} for status in statuses):
        return "PREFLIGHT_PASSED"
    return "NOT_READY"


class BaseHarness:
    adapter_name = "base"

    def __init__(self, config: LiveGateConfig, gate_id: str):
        self.config = config
        self.gate_id = gate_id
        self.gate = config.gate(gate_id)

    def preflight(self) -> dict[str, Any]:
        missing_credentials = self.config.credential_missing(self.gate)
        missing_test_env = self.config.test_env_missing(self.gate)
        if missing_credentials:
            raise MissingCredential(", ".join(missing_credentials))
        if missing_test_env:
            raise MissingTestEnvironment(", ".join(missing_test_env))
        return {"preflight": "passed", "gate_id": self.gate_id}

    def dry_run(self) -> dict[str, Any]:
        raise NotImplementedError

    def run_live(self) -> dict[str, Any]:
        raise MissingTestEnvironment("live adapter is not enabled in validation config")


class HermesHostHarness(BaseHarness):
    adapter_name = "HermesCoreBridge"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.hermes.goal11_host_binding import FeishuBindingEvent, FeishuResponseDispatcher, FeishuThinBinding, HermesCoreBridge
        from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost
        from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
        from scripts.core.state.goal02_core import CoreMaterializer

        core = CoreMaterializer.in_memory()
        core.grant_permission("hermes", "create_state")
        bridge = HermesCoreBridge(core)
        dispatch = bridge.dispatch(
            FeishuThinBinding().to_hermes_message(
                FeishuBindingEvent(
                    event_id="dry-run-hermes-create",
                    chat_id="dry-run-shadow-chat",
                    sender_id="dry-run-user",
                    command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
                )
            )
        )
        scheduler = Goal03Scheduler(core.store)
        job_ids = bridge.enqueue_response_jobs(scheduler)
        runtime = RuntimeHost(scheduler, worker_id="dry-run-hermes-worker")
        runtime.register_handler(
            "outbox.dispatch",
            FeishuResponseDispatcher(core.store).handler(),
            contract=RuntimeHandlerContract(
                job_kind="outbox.dispatch",
                required_payload_keys=("outbox_id", "topic", "payload"),
                required_result_keys=("outbox_id", "reply_channel_id", "receipt_id"),
            ),
        )
        runtime_results = runtime.run_batch(max_jobs=4)
        response = {
            "core_status": dispatch.status,
            "object_id_present": bool(dispatch.object_id),
            "response_jobs": len(job_ids),
            "runtime_statuses": [item.status for item in runtime_results],
        }
        core.store.conn.close()
        return response


class FeishuBindingHarness(BaseHarness):
    adapter_name = "FeishuThinBinding"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.hermes.goal11_host_binding import FeishuBindingEvent, FeishuThinBinding, HermesCoreBridge
        from scripts.core.state.goal02_core import CoreMaterializer

        core = CoreMaterializer.in_memory()
        core.grant_permission("hermes", "create_state")
        bridge = HermesCoreBridge(core)
        event = FeishuBindingEvent(
            event_id="dry-run-feishu-event",
            chat_id="dry-run-shadow-chat",
            sender_id="dry-run-user",
            command={"command_type": "create_state", "object_kind": "topic", "payload": {}},
        )
        message = FeishuThinBinding().to_hermes_message(event)
        first = bridge.dispatch(message)
        replay = bridge.dispatch(message)
        response = {
            "message_source": message.source,
            "first_status": first.status,
            "replay_status": replay.status,
            "replayed": replay.replayed,
            "reply_channel_id": replay.reply_channel_id,
        }
        core.store.conn.close()
        return response


class DryRunModelProvider:
    provider_name = "dry-run-model-provider"

    def complete(self, request: Any, route: Any) -> Any:
        from scripts.core.model_gateway.goal07_model_gateway import ModelProviderResult, ModelUsage

        return ModelProviderResult(
            output_text=f"dry-run response for {request.input_payload.get('topic_id', 'topic')}",
            usage=ModelUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
            cost={"currency": "USD", "amount": "0"},
            provider_request_id="dry-run-provider-request",
            metadata={"external_io": False},
        )


class ModelProviderHarness(BaseHarness):
    adapter_name = "ModelGateway"
    live_route_name = "hermes_live_validation"

    def preflight(self) -> dict[str, Any]:
        response = super().preflight()
        cost_cap = self.config.env_value("MODEL_PROVIDER_COST_CAP")
        base_url = self.config.env_value("MODEL_PROVIDER_BASE_URL")
        _decimal_value(cost_cap)
        if not _valid_base_url(base_url):
            raise MissingTestEnvironment("MODEL_PROVIDER_BASE_URL must start with http:// or https://")
        return response | {
            "provider": "hermes",
            "cost_cap_unit": "USD",
            "cost_cap_format": "decimal amount",
            "cost_cap_comparison": "provider cost.amount is compared when provider cost.currency is USD; otherwise cost is not_available",
            "required_fields": (
                "MODEL_PROVIDER_API_KEY",
                "MODEL_PROVIDER_BASE_URL",
                "MODEL_PROVIDER_MODEL",
                "MODEL_PROVIDER_COST_CAP",
            ),
            "optional_fields": ("MODEL_PROVIDER_PROJECT_ID",),
        }

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRequest, ModelRoute, ModelRunMaterializer
        from scripts.core.persistence.goal01_store import PersistenceStore, content_hash

        store = PersistenceStore.in_memory()
        route = ModelRoute(
            route_name="dry_run_route",
            provider_name="dry-run-model-provider",
            model_name="dry-run-model",
            config_version="live-gates.dry-run.v1",
            config_hash=content_hash({"route": "dry_run_route"}, "live-gates.route.v1"),
            timeout_ms=10_000,
        )
        gateway = ModelGateway(
            routes={route.route_name: route},
            providers={route.provider_name: DryRunModelProvider()},
            materializer=ModelRunMaterializer(store),
        )
        result = gateway.complete(
            ModelRequest(
                route_name=route.route_name,
                prompt="Dry-run prompt with no private data.",
                input_payload={"topic_id": "dry-run-topic"},
                correlation_id="dry-run-model",
            )
        )
        response = {
            "status": result.envelope.status,
            "provider": result.envelope.provider_name,
            "usage_total_tokens": result.envelope.usage.total_tokens,
            "envelope_version_id": result.envelope_version_id,
        }
        store.conn.close()
        return response

    def run_live(self) -> dict[str, Any]:
        from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRequest, ModelRoute, ModelRunMaterializer
        from scripts.core.persistence.goal01_store import PersistenceStore, content_hash

        api_key = _required_env_value(self.config, "MODEL_PROVIDER_API_KEY")
        base_url = _required_env_value(self.config, "MODEL_PROVIDER_BASE_URL")
        model = _required_env_value(self.config, "MODEL_PROVIDER_MODEL")
        cost_cap = _required_env_value(self.config, "MODEL_PROVIDER_COST_CAP")
        cost_cap_amount = _decimal_value(cost_cap)
        if not _valid_base_url(base_url):
            raise MissingTestEnvironment("MODEL_PROVIDER_BASE_URL must start with http:// or https://")

        store = PersistenceStore.in_memory()
        route = ModelRoute(
            route_name=self.live_route_name,
            provider_name="hermes",
            model_name=model,
            config_version="live-gates.hermes-model.v1",
            config_hash=content_hash(
                {
                    "provider": "hermes",
                    "model": model,
                    "base_url_configured": True,
                    "project_id_present": not is_placeholder(self.config.env_value("MODEL_PROVIDER_PROJECT_ID")),
                    "cost_cap": cost_cap,
                },
                "live-gates.hermes-model.route.v1",
            ),
            parameters={"temperature": 0},
            timeout_ms=30_000,
        )
        provider = HermesModelProviderAdapter(
            HermesModelProviderConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_seconds=30.0,
            )
        )
        gateway = ModelGateway(
            routes={route.route_name: route},
            providers={provider.provider_name: provider},
            materializer=ModelRunMaterializer(store),
        )
        try:
            result = gateway.complete(
                ModelRequest(
                    route_name=route.route_name,
                    prompt="Return the exact text: live-gate-hermes-model-ok",
                    input_payload={"gate_id": self.gate_id, "purpose": "model_provider_live_validation"},
                    correlation_id=f"{self.gate_id}.live",
                    metadata={"project_id_status": "available" if self.config.env_value("MODEL_PROVIDER_PROJECT_ID") else "not_available"},
                )
            )
            if not result.output_text.strip():
                raise LiveGateError("Hermes model provider returned empty output")
            envelope = result.envelope
            cost_cap_check = _cost_cap_result(envelope.cost or {}, cost_cap_amount)
            if cost_cap_check["cost_cap_status"] == "exceeded":
                raise LiveGateError(
                    "MODEL_PROVIDER_COST_CAP exceeded: "
                    f"{cost_cap_check['cost_amount']} {cost_cap_check['cost_currency']} > "
                    f"{cost_cap_check['cost_cap']} {cost_cap_check['cost_cap_unit']}"
                )
            response = {
                "provider": envelope.provider_name,
                "model": envelope.model_name,
                "status": envelope.status,
                "error_type": None,
                "provider_request_id_status": (envelope.metadata or {}).get("provider_request_id_status", "not_available"),
                "usage_status": (envelope.metadata or {}).get("usage_status", "not_available"),
                "cost_status": (envelope.cost or {}).get("status", "not_available"),
                "finish_reason": (envelope.metadata or {}).get("finish_reason", "not_available"),
                "latency_ms": envelope.duration_ms,
                "input_hash": envelope.input_hash,
                "output_hash": envelope.output_hash,
                "envelope_version_id": result.envelope_version_id,
                "correlation_id": envelope.correlation_id,
                "external_side_effect": True,
            } | cost_cap_check
            return response
        except Exception as exc:
            raise LiveGateError(f"Hermes model provider live call failed: {type(exc).__name__}: {exc}") from exc
        finally:
            store.conn.close()


class DryRunSearchProvider:
    provider_name = "dry-run-search-provider"

    def search(self, query: Any) -> list[Any]:
        from scripts.core.research.goal06_formal_research import SearchResult

        return [
            SearchResult(
                result_id="dry-run-source-1",
                title="Dry-run source",
                url="https://example.invalid/dry-run-source",
                provider=self.provider_name,
                snippet=f"Evidence for {query.query}",
            )
        ]


class DryRunFetcher:
    fetcher_name = "dry-run-fetcher"

    def fetch(self, result: Any) -> Any:
        from scripts.core.research.goal06_formal_research import FetchedDocument

        return FetchedDocument(
            result_id=result.result_id,
            url=result.url,
            title=result.title,
            text="Dry-run fetched text.",
            fetched_at="2026-07-02T00:00:00Z",
            fetcher=self.fetcher_name,
        )


class DryRunExtractor:
    extractor_name = "dry-run-extractor"

    def extract(self, document: Any) -> list[Any]:
        from scripts.core.research.goal06_formal_research import ExtractedEvidence

        return [
            ExtractedEvidence(
                evidence_id="dry-run-evidence-1",
                result_id=document.result_id,
                claim="Dry-run claim",
                quote="Dry-run fetched text.",
                locator={"url": document.url},
                extractor=self.extractor_name,
            )
        ]


class SearchProviderHarness(BaseHarness):
    adapter_name = "FormalResearchService"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.persistence.goal01_store import PersistenceStore
        from scripts.core.research.goal06_formal_research import (
            FormalResearchMaterializer,
            FormalResearchService,
            ResearchBoundaryError,
            ResearchQuery,
            SearchResult,
        )

        store = PersistenceStore.in_memory()
        service = FormalResearchService(
            provider=DryRunSearchProvider(),
            fetcher=DryRunFetcher(),
            extractor=DryRunExtractor(),
            materializer=FormalResearchMaterializer(store),
        )
        run = service.run(ResearchQuery(topic_id="dry-run-topic", query="dry-run query"))
        blocked_rejected = False
        try:
            FormalResearchMaterializer(store).persist_run(
                query=ResearchQuery(topic_id="dry-run-topic", query="blocked"),
                provider_name="dry-run-search-provider",
                fetcher_name="dry-run-fetcher",
                extractor_name="dry-run-extractor",
                search_results=[
                    SearchResult(
                        result_id="blocked",
                        title="blocked",
                        url="https://www.douyin.com/video/blocked",
                        provider="dry-run",
                        platform="douyin",
                    )
                ],
                documents=[],
                evidence=[],
            )
        except ResearchBoundaryError:
            blocked_rejected = True
        response = {
            "artifact_version_id": run.artifact_version_id,
            "source_count": len(run.source_version_ids),
            "evidence_count": len(run.evidence_version_ids),
            "blocked_platform_rejected": blocked_rejected,
        }
        store.conn.close()
        return response


class AsrHarness(BaseHarness):
    adapter_name = "ASR command boundary"

    def dry_run(self) -> dict[str, Any]:
        entry = ROOT / "tools" / "asr" / "transcribe.py"
        return {
            "entrypoint_exists": entry.exists(),
            "planned_command": "tools/asr/.venv/Scripts/python.exe tools/asr/transcribe.py --hit <test-hit-id>",
            "requires_isolated_workspace": True,
            "external_io": False,
        }


class ExternalCollectorHarness(BaseHarness):
    adapter_name = "External collector adapter boundary"

    def dry_run(self) -> dict[str, Any]:
        entry = ROOT / "scripts" / "collect" / "crawl_competitors.py"
        common = ROOT / "scripts" / "collect" / "common.py"
        return {
            "entrypoint_exists": entry.exists(),
            "common_adapter_source_exists": common.exists(),
            "planned_command": "python scripts/collect/crawl_competitors.py --mode daily",
            "adapter_source_only": True,
            "external_io": False,
        }


class ShadowE2EHarness(BaseHarness):
    adapter_name = "GOAL-12 staging shadow boundary"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.staging.verify_goal_12 import run_end_to_end_staging

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            harness, artifacts = run_end_to_end_staging()
        response = {
            "topic_id_present": bool(artifacts.topic_id),
            "script_version_id_present": bool(artifacts.script_version_id),
            "publication_capture_version_id_present": bool(artifacts.publication_version_id),
            "trace_versions": int(harness.store.conn.execute("SELECT count(*) FROM trace_version").fetchone()[0]),
            "shadow_only": True,
        }
        harness.store.conn.close()
        return response


class Clock:
    def __init__(self) -> None:
        self.value = 1_000

    def now_ms(self) -> int:
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value


class ContinuousFaultRecoveryHarness(BaseHarness):
    adapter_name = "Scheduler and RuntimeHost"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.hermes.goal11_host_binding import GOAL11_RESPONSE_SEND_SCOPE, GOAL11_RESPONSE_TOPIC, FeishuResponseDispatcher
        from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost
        from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
        from scripts.core.state.goal02_core import CoreMaterializer

        clock = Clock()
        core = CoreMaterializer.in_memory()
        scheduler = Goal03Scheduler(core.store, now_ms=clock.now_ms)
        scheduler.enqueue_job(
            job_kind="outbox.dispatch",
            payload={
                "outbox_id": "dry-run-outbox-leased",
                "topic": GOAL11_RESPONSE_TOPIC,
                "payload": {"reply_channel_id": "dry-run-chat", "source_event_id": "leased"},
            },
            idempotency_key="dry-run-continuous-leased",
            max_attempts=2,
        )
        claim = scheduler.claim_next(worker_id="interrupted-worker", lease_seconds=1)
        clock.advance_ms(2_000)
        recovered = scheduler.recover_expired_leases(actor="dry-run-recovery")
        for idx in range(2):
            scheduler.enqueue_job(
                job_kind="outbox.dispatch",
                payload={
                    "outbox_id": f"dry-run-outbox-{idx}",
                    "topic": GOAL11_RESPONSE_TOPIC,
                    "payload": {
                        "reply_channel_id": "dry-run-chat",
                        "source_event_id": f"dry-run-{idx}",
                    },
                },
                idempotency_key=f"dry-run-continuous-{idx}",
                max_attempts=2,
            )
        runtime = RuntimeHost(scheduler, worker_id="dry-run-continuous-worker")
        runtime.register_handler(
            "outbox.dispatch",
            FeishuResponseDispatcher(core.store).handler(),
            contract=RuntimeHandlerContract(
                job_kind="outbox.dispatch",
                required_payload_keys=("outbox_id", "topic", "payload"),
                required_result_keys=("outbox_id", "reply_channel_id", "receipt_id"),
            ),
        )
        results = runtime.run_batch(max_jobs=5)
        receipts = int(
            core.store.conn.execute(
                "SELECT count(*) FROM command_receipt WHERE command_scope=?",
                (GOAL11_RESPONSE_SEND_SCOPE,),
            ).fetchone()[0]
        )
        response = {
            "interrupted_attempt_id_present": bool(claim.attempt_id),
            "recovered_leases": recovered,
            "runtime_statuses": [item.status for item in results],
            "send_receipts": receipts,
        }
        core.store.conn.close()
        return response


HARNESS_TYPES = {
    "GATE-HERMES-REAL-HOST": HermesHostHarness,
    "GATE-FEISHU-THIN-BINDING": FeishuBindingHarness,
    "GATE-MODEL-PROVIDER": ModelProviderHarness,
    "GATE-ASR": AsrHarness,
    "GATE-SEARCH-PROVIDER": SearchProviderHarness,
    "GATE-EXTERNAL-COLLECTOR-ADAPTER": ExternalCollectorHarness,
    "GATE-SHADOW-E2E": ShadowE2EHarness,
    "GATE-CONTINUOUS-FAULT-RECOVERY": ContinuousFaultRecoveryHarness,
}


def harness_for(config: LiveGateConfig, gate_id: str) -> BaseHarness:
    return HARNESS_TYPES[gate_id](config, gate_id)


def run_gate_mode(
    *,
    config: LiveGateConfig,
    gate_id: str,
    mode: str,
    live_confirm: bool = False,
    environment: str | None = None,
    write_status: bool = True,
    status_path: Path | None = None,
) -> GateResult:
    if gate_id not in GATE_IDS:
        raise LiveGateError(f"unknown gate_id: {gate_id}")
    harness = harness_for(config, gate_id)
    gate = config.gate(gate_id)
    started = utc_now()
    correlation_id = f"{gate_id}.{mode}.{started}"
    evidence_location = make_evidence_location(config, gate_id, started)
    request = {
        "gate_id": gate_id,
        "mode": mode,
        "environment": environment or config.environment,
        "adapter": harness.adapter_name,
        "live_confirm": live_confirm,
    }
    status = "NOT_READY"
    exit_code = 1
    failure_reason = ""
    response: dict[str, Any] = {}
    try:
        if mode == "preflight":
            response = harness.preflight()
            status = "PREFLIGHT_PASSED"
            exit_code = 0
        elif mode == "dry-run":
            response = harness.dry_run()
            status = "DRY_RUN_PASSED"
            exit_code = 0
        elif mode == "run":
            validate_live_authorization(config, gate, live_confirm=live_confirm, environment=environment)
            response = harness.run_live()
            status = "LIVE_PASSED"
            exit_code = 0
        else:
            raise LiveGateError(f"unsupported mode: {mode}")
    except MissingCredential as exc:
        status = "BLOCKED_MISSING_CREDENTIAL"
        failure_reason = str(exc)
        response = {"blocked": status, "missing": str(exc)}
    except MissingTestEnvironment as exc:
        status = "BLOCKED_MISSING_TEST_ENV"
        failure_reason = str(exc)
        response = {"blocked": status, "missing": str(exc)}
    except MissingAuthorization as exc:
        status = "BLOCKED_MISSING_AUTHORIZATION"
        failure_reason = str(exc)
        response = {"blocked": status, "missing": str(exc)}
    except Exception as exc:  # noqa: BLE001 - validation harness records local failures.
        status = "LIVE_FAILED" if mode == "run" and live_confirm else "NOT_READY"
        failure_reason = f"{type(exc).__name__}: {exc}"
        response = {"error": failure_reason}
    finished = utc_now()
    result = GateResult(
        gate_id=gate_id,
        started_at=started,
        finished_at=finished,
        environment=environment or config.environment,
        adapter=harness.adapter_name,
        request_hash=stable_hash(redact(request)),
        response_hash=stable_hash(redact(response)),
        correlation_id=correlation_id,
        exit_code=exit_code,
        status=status,
        external_side_effect=False if mode != "run" or status.startswith("BLOCKED_") else bool(response.get("external_side_effect", False)),
        failure_reason=failure_reason,
        evidence_location=display_path(evidence_location),
        mode=mode,
    )
    write_evidence(evidence_location, result, request, response)
    if write_status:
        GateStatusStore(status_path or config.status_file).write_result(result)
    return result


def validate_live_authorization(
    config: LiveGateConfig,
    gate: dict[str, Any],
    *,
    live_confirm: bool,
    environment: str | None,
) -> None:
    effective_env = environment or config.environment
    if not live_confirm:
        raise MissingAuthorization("run requires --live-confirm")
    if effective_env not in {"validation", "test"}:
        raise MissingAuthorization("environment must be validation or test")
    if not config.allow_live_calls:
        raise MissingAuthorization("config allow_live_calls must be true")
    if not config.shadow_only:
        raise MissingAuthorization("config shadow_only must be true")
    missing_credentials = config.credential_missing(gate)
    if missing_credentials:
        raise MissingCredential(", ".join(missing_credentials))
    missing_test_env = config.test_env_missing(gate)
    if missing_test_env:
        raise MissingTestEnvironment(", ".join(missing_test_env))
    if not bool(gate.get("live_enabled", False)):
        raise MissingTestEnvironment("gate live_enabled is false")


def make_evidence_location(config: LiveGateConfig, gate_id: str, started_at: str) -> Path:
    stamp = started_at.replace(":", "").replace("-", "").replace("Z", "")
    return config.evidence_root / gate_id / stamp


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_evidence(path: Path, result: GateResult, request: dict[str, Any], response: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    manifest = result.as_dict() | {
        "request": redact(request),
        "response": redact(response),
    }
    (path / "run_manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")


def iter_gate_ids(selected: str | None = None) -> tuple[str, ...]:
    if selected:
        if selected not in GATE_IDS:
            raise LiveGateError(f"unknown gate_id: {selected}")
        return (selected,)
    return GATE_IDS


def command_list(config: LiveGateConfig, status_path: Path | None = None) -> int:
    status = GateStatusStore(status_path or config.status_file).read()
    for gate_id in GATE_IDS:
        gate_status = status.get("gates", {}).get(gate_id, {}).get("status", "NOT_READY")
        print(f"{gate_id}\t{gate_status}")
    return 0


def command_preflight(config: LiveGateConfig, gate_id: str | None, status_path: Path | None = None) -> int:
    exit_code = 0
    for item in iter_gate_ids(gate_id):
        result = run_gate_mode(config=config, gate_id=item, mode="preflight", status_path=status_path)
        print(f"{item}\t{result.status}\t{result.failure_reason}")
        if result.exit_code != 0 and result.status not in {"BLOCKED_MISSING_CREDENTIAL", "BLOCKED_MISSING_TEST_ENV"}:
            exit_code = result.exit_code
    return exit_code


def command_dry_run(config: LiveGateConfig, gate_id: str | None, status_path: Path | None = None) -> int:
    exit_code = 0
    for item in iter_gate_ids(gate_id):
        result = run_gate_mode(config=config, gate_id=item, mode="dry-run", status_path=status_path)
        print(f"{item}\t{result.status}\t{result.failure_reason}")
        if result.exit_code != 0:
            exit_code = result.exit_code
    return exit_code


def command_run(
    config: LiveGateConfig,
    *,
    gate_id: str | None,
    live_confirm: bool,
    environment: str | None,
    status_path: Path | None = None,
) -> int:
    if not gate_id:
        raise MissingAuthorization("run requires --gate; run-all live mode is not provided")
    result = run_gate_mode(
        config=config,
        gate_id=gate_id,
        mode="run",
        live_confirm=live_confirm,
        environment=environment,
        status_path=status_path,
    )
    print(f"{gate_id}\t{result.status}\t{result.failure_reason}")
    return result.exit_code


def command_status(config: LiveGateConfig, status_path: Path | None = None) -> int:
    data = GateStatusStore(status_path or config.status_file).read()
    print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return 0


def command_report(config: LiveGateConfig, report_path: Path = DEFAULT_REPORT, status_path: Path | None = None) -> int:
    status = GateStatusStore(status_path or config.status_file).read()
    lines = [
        "# External Live Gate Validation Report",
        "",
        f"status: `{status.get('status', 'NOT_READY')}`",
        "",
        f"updated_at: `{status.get('updated_at')}`",
        "",
        "## Gate Summary",
        "",
    ]
    for gate_id in GATE_IDS:
        gate_status = status.get("gates", {}).get(gate_id, {}).get("status", "NOT_READY")
        latest = status.get("gates", {}).get(gate_id, {}).get("latest_result") or {}
        lines.extend(
            [
                f"### {gate_id}",
                "",
                f"- status: `{gate_status}`",
                f"- mode: `{latest.get('mode', 'none')}`",
                f"- adapter: `{latest.get('adapter', config.gate(gate_id).get('adapter', 'unknown'))}`",
                f"- evidence: `{latest.get('evidence_location', '')}`",
                f"- external_side_effect: `{latest.get('external_side_effect', False)}`",
                f"- failure_reason: `{latest.get('failure_reason', '')}`",
                f"- missing_credentials: `{', '.join(config.credential_missing(config.gate(gate_id)))}`",
                f"- missing_test_environment: `{', '.join(config.test_env_missing(config.gate(gate_id)))}`",
                f"- live_enabled: `{bool(config.gate(gate_id).get('live_enabled', False))}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Safety Statement",
            "",
            "This report is generated from local preflight, dry-run or explicitly guarded single-gate runs.",
            "No run-all live mode exists in the harness.",
            "Dry-run mode does not initiate Hermes, Feishu, model, ASR, search or platform network calls.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(str(report_path.relative_to(ROOT)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V0.6.2 external live gate harness")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to live gate config YAML")
    parser.add_argument("--env-file", default=None, help="Optional env file with local validation placeholders/secrets")
    parser.add_argument("--status-file", default=None, help="Override status YAML path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--gate", choices=GATE_IDS)
    dry = sub.add_parser("dry-run")
    dry.add_argument("--gate", choices=GATE_IDS)
    run = sub.add_parser("run")
    run.add_argument("--gate", choices=GATE_IDS, required=True)
    run.add_argument("--live-confirm", action="store_true")
    run.add_argument("--environment", choices=("validation", "test", "production"), default=None)
    sub.add_parser("status")
    report = sub.add_parser("report")
    report.add_argument("--output", default=str(DEFAULT_REPORT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = LiveGateConfig(Path(args.config), env_path=Path(args.env_file) if args.env_file else None)
    status_path = Path(args.status_file) if args.status_file else None
    if args.command == "list":
        return command_list(config, status_path)
    if args.command == "preflight":
        return command_preflight(config, args.gate, status_path)
    if args.command == "dry-run":
        return command_dry_run(config, args.gate, status_path)
    if args.command == "run":
        return command_run(
            config,
            gate_id=args.gate,
            live_confirm=args.live_confirm,
            environment=args.environment,
            status_path=status_path,
        )
    if args.command == "status":
        return command_status(config, status_path)
    if args.command == "report":
        return command_report(config, Path(args.output), status_path)
    raise LiveGateError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
