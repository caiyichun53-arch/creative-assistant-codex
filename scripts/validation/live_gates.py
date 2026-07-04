from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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


@dataclass(frozen=True)
class HermesSubscriptionProviderSettings:
    token: str
    base_url: str
    model_name: str
    model_class: str
    token_source: str
    base_url_source: str
    model_name_source: str
    model_class_source: str


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
    if lowered.endswith("_tokens") or lowered in {"max_tokens", "prompt_tokens", "completion_tokens", "total_tokens"}:
        return False
    return any(marker in lowered for marker in ("secret", "token", "cookie", "session", "api_key", "password"))


def _required_env_value(config: "LiveGateConfig", key: str) -> str:
    value = config.env_value(key)
    if is_placeholder(value):
        raise MissingCredential(key)
    return str(value)


def _first_env_value(config: "LiveGateConfig", keys: tuple[str, ...]) -> tuple[str, str]:
    for key in keys:
        value = config.env_value(key)
        if not is_placeholder(value):
            return key, str(value)
    raise MissingCredential(" or ".join(keys))


def _infer_model_class(model_name: str) -> str:
    lowered = model_name.lower()
    if "gpt" in lowered or "openai" in lowered:
        return "gpt"
    if "mimo" in lowered or "xiaomi" in lowered:
        return "mimo"
    if "deepseek" in lowered:
        return "deepseek"
    return "unknown"


def hermes_subscription_provider_settings(config: "LiveGateConfig") -> HermesSubscriptionProviderSettings:
    token_source, token = _first_env_value(config, ("HERMES_BUSINESS_MODEL_TOKEN",))
    base_url_source, base_url = _first_env_value(config, ("HERMES_BUSINESS_MODEL_BASE_URL",))
    model_name_source, model_name = _first_env_value(config, ("HERMES_BUSINESS_MODEL_NAME",))
    explicit_model_class = config.env_value("HERMES_BUSINESS_MODEL_CLASS")
    if is_placeholder(explicit_model_class):
        model_class_source = "inferred_from_model_name"
        model_class = _infer_model_class(model_name)
    else:
        model_class_source = "HERMES_BUSINESS_MODEL_CLASS"
        model_class = str(explicit_model_class).strip().lower()
    if not _valid_base_url(base_url):
        raise MissingTestEnvironment("HERMES_BUSINESS_MODEL_BASE_URL must start with http:// or https://")
    if model_class != "mimo":
        raise MissingTestEnvironment("HERMES_BUSINESS_MODEL_CLASS must be mimo for Hermes business runtime")
    return HermesSubscriptionProviderSettings(
        token=token,
        base_url=base_url,
        model_name=model_name,
        model_class=model_class,
        token_source=token_source,
        base_url_source=base_url_source,
        model_name_source=model_name_source,
        model_class_source=model_class_source,
    )


def _valid_base_url(value: str | None) -> bool:
    if is_placeholder(value):
        return False
    return str(value).startswith(("http://", "https://"))


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
    expected_output = "HERMES_HOST_GATE_OK"
    timeout_seconds = 30

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

    def run_live(self) -> dict[str, Any]:
        host_url = _required_env_value(self.config, "HERMES_TEST_HOST_URL").rstrip("/")
        token = _required_env_value(self.config, "HERMES_TEST_TOKEN")
        actor_id = _required_env_value(self.config, "HERMES_TEST_ACTOR_ID")
        if not _valid_base_url(host_url):
            raise MissingTestEnvironment("HERMES_TEST_HOST_URL must be an http(s) URL")

        health_status, health = self._request_json("GET", host_url, "/health")
        health_detailed_status, health_detailed = self._request_json("GET", host_url, "/health/detailed")
        models_status, models = self._request_json("GET", host_url, "/v1/models", token=token, actor_id=actor_id)
        model_name = self._select_model(models)
        prompt = f"Return exactly {self.expected_output} and nothing else."
        chat_status, chat = self._request_json(
            "POST",
            host_url,
            "/v1/chat/completions",
            token=token,
            actor_id=actor_id,
            body={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 16,
                "stream": False,
            },
            timeout=120,
        )
        output_text = self._chat_output(chat)
        expected_output_match = output_text.strip() == self.expected_output
        if not expected_output_match:
            raise LiveGateError("Hermes host output did not match expected HERMES_HOST_GATE_OK marker")
        return {
            "provider": "hermes",
            "interface_type": "openai-compatible-api-server",
            "host": self._sanitized_host(host_url),
            "health_status": health_status,
            "health_detailed_status": health_detailed_status,
            "models_status": models_status,
            "chat_status": chat_status,
            "health_json": isinstance(health, dict),
            "health_detailed_json": isinstance(health_detailed, dict),
            "models_count": len(models.get("data", [])) if isinstance(models, dict) else 0,
            "model": model_name,
            "status": "succeeded",
            "expected_output_match": expected_output_match,
            "visible_output_status": "available" if output_text else "empty",
            "input_hash": stable_hash({"actor_id": actor_id, "prompt": prompt}),
            "output_hash": stable_hash(output_text),
            "usage_status": "available" if isinstance(chat, dict) and chat.get("usage") else "not_available",
            "response_id_status": "available" if isinstance(chat, dict) and chat.get("id") else "not_available",
            "actual_call_count": 1,
            "retry_count": 0,
            "dry_run_fallback": False,
            "authenticated": True,
            "correlation_id": f"{self.gate_id}.live",
            "external_side_effect": True,
        }

    def _request_json(
        self,
        method: str,
        host_url: str,
        path: str,
        *,
        token: str | None = None,
        actor_id: str | None = None,
        body: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if actor_id:
            headers["X-Hermes-Session-Key"] = actor_id
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{host_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout or self.timeout_seconds) as response:  # noqa: S310 - explicit validation host URL.
                raw = response.read().decode("utf-8", errors="replace")
                return int(response.status), json.loads(raw)
        except HTTPError as exc:
            raise LiveGateError(f"Hermes host HTTP {exc.code} for {path}") from exc
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise LiveGateError(f"Hermes host request failed for {path}: {exc}") from exc

    def _select_model(self, models: Any) -> str:
        if isinstance(models, dict):
            data = models.get("data")
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict) and first.get("id"):
                    return str(first["id"])
        return "hermes-agent"

    def _chat_output(self, chat: Any) -> str:
        if not isinstance(chat, dict):
            return ""
        try:
            return str(chat["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return ""

    def _sanitized_host(self, host_url: str) -> str:
        parsed = urlparse(host_url)
        netloc = parsed.hostname or "unknown"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return f"{parsed.scheme}://{netloc}"


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
    expected_output = "MODEL_GATE_OK"
    billing_mode = "subscription"
    live_call_limit = 1
    max_retries = 0
    timeout_ms = 30_000
    gate_max_output_tokens = 128

    def preflight(self) -> dict[str, Any]:
        response = super().preflight()
        provider_settings = hermes_subscription_provider_settings(self.config)
        return response | {
            "provider": "hermes",
            "billing_mode": self.billing_mode,
            "model_class": provider_settings.model_class,
            "model_name_source": provider_settings.model_name_source,
            "base_url_source": provider_settings.base_url_source,
            "token_source": provider_settings.token_source,
            "live_call_limit": self.live_call_limit,
            "max_retries": self.max_retries,
            "timeout_ms": self.timeout_ms,
            "gate_max_output_tokens": self.gate_max_output_tokens,
            "required_fields": (
                "HERMES_BUSINESS_MODEL_TOKEN",
                "HERMES_BUSINESS_MODEL_BASE_URL",
                "HERMES_BUSINESS_MODEL_NAME",
                "HERMES_BUSINESS_MODEL_CLASS",
            ),
            "optional_fields": (),
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

        provider_settings = hermes_subscription_provider_settings(self.config)

        store = PersistenceStore.in_memory()
        route = ModelRoute(
            route_name=self.live_route_name,
            provider_name="hermes",
            model_name=provider_settings.model_name,
            config_version="live-gates.hermes-model.v1",
            config_hash=content_hash(
                {
                    "provider": "hermes",
                    "model": provider_settings.model_name,
                    "model_class": provider_settings.model_class,
                    "billing_mode": self.billing_mode,
                    "base_url_configured": True,
                    "live_call_limit": self.live_call_limit,
                    "max_retries": self.max_retries,
                    "timeout_ms": self.timeout_ms,
                    "gate_max_output_tokens": self.gate_max_output_tokens,
                    "reasoning_effort": "low",
                },
                "live-gates.hermes-model.route.v1",
            ),
            parameters={"temperature": 0, "max_completion_tokens": self.gate_max_output_tokens, "reasoning_effort": "low"},
            timeout_ms=self.timeout_ms,
        )
        provider = HermesModelProviderAdapter(
            HermesModelProviderConfig(
                api_key=provider_settings.token,
                base_url=provider_settings.base_url,
                model=provider_settings.model_name,
                timeout_seconds=self.timeout_ms / 1000,
                max_retries=self.max_retries,
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
                    prompt=f"Reply with only: {self.expected_output}",
                    input_payload={"gate_id": self.gate_id, "purpose": "model_provider_live_validation"},
                    correlation_id=f"{self.gate_id}.live",
                    metadata={"model_class": provider_settings.model_class, "billing_mode": self.billing_mode},
                )
            )
            expected_output_match = result.output_text.strip() == self.expected_output
            if not expected_output_match:
                raise LiveGateError("Hermes model provider output did not match expected MODEL_GATE_OK marker")
            envelope = result.envelope
            metadata = envelope.metadata or {}
            billing_mode = metadata.get("billing_mode") or (envelope.cost or {}).get("billing_mode") or self.billing_mode
            if billing_mode != "subscription":
                raise LiveGateError("Hermes Mimo provider must report subscription billing")
            response = {
                "provider": envelope.provider_name,
                "model": envelope.model_name,
                "model_class": provider_settings.model_class,
                "status": envelope.status,
                "error_type": None,
                "provider_request_id_status": (envelope.metadata or {}).get("provider_request_id_status", "not_available"),
                "billing_mode": billing_mode,
                "usage_status": metadata.get("usage_status", "not_available"),
                "cost_status": metadata.get("cost_status", (envelope.cost or {}).get("status", "not_available")),
                "finish_reason": metadata.get("finish_reason", "not_available"),
                "visible_output_status": metadata.get("visible_output_status", "not_available"),
                "expected_output_match": expected_output_match,
                "latency_ms": envelope.duration_ms,
                "input_hash": envelope.input_hash,
                "output_hash": envelope.output_hash,
                "envelope_version_id": result.envelope_version_id,
                "correlation_id": envelope.correlation_id,
                "external_side_effect": True,
                "live_call_limit": self.live_call_limit,
                "actual_call_count": 1,
                "max_retries": self.max_retries,
                "retry_count": metadata.get("retry_count", self.max_retries),
                "timeout_ms": self.timeout_ms,
                "gate_max_output_tokens": self.gate_max_output_tokens,
            }
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


class DryRunExternalExecutor:
    def __init__(self, fixtures: dict[str, dict[str, Any]]):
        self.fixtures = fixtures
        self.commands: list[Any] = []

    def execute(self, command: Any) -> Any:
        from scripts.core.external_adapters import ExternalCommandResult

        self.commands.append(command)
        payload = self.fixtures.get(command.capability)
        if payload is None:
            return ExternalCommandResult(status="failed", payload={"error": "missing dry-run fixture"})
        return ExternalCommandResult(
            status="succeeded",
            payload=payload,
            raw_archive_ref=f"dry-run://{command.capability}",
            external_side_effect=False,
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
    adapter_name = "FormalResearchService external SearchProvider/Fetcher adapters"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.external_adapters import ResearchFetcherAdapter, SearchProviderAdapter
        from scripts.core.persistence.goal01_store import PersistenceStore
        from scripts.core.research.goal06_formal_research import (
            FormalResearchMaterializer,
            FormalResearchService,
            ResearchBoundaryError,
            ResearchQuery,
            SearchResult,
        )

        store = PersistenceStore.in_memory()
        executor = DryRunExternalExecutor(
            {
                "research.search": {
                    "results": [
                        {
                            "result_id": "dry-run-source-1",
                            "title": "Dry-run source",
                            "url": "https://example.invalid/dry-run-source",
                            "provider": "dry-run-search-provider",
                            "snippet": "Evidence for dry-run query",
                        }
                    ]
                },
                "research.fetch": {
                    "document": {
                        "title": "Dry-run source",
                        "text": "Dry-run fetched text.",
                        "fetched_at": "2026-07-02T00:00:00Z",
                    }
                },
            }
        )
        service = FormalResearchService(
            provider=SearchProviderAdapter(executor),
            fetcher=ResearchFetcherAdapter(executor),
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
            "adapter_capabilities": [command.capability for command in executor.commands],
        }
        store.conn.close()
        return response


class AsrHarness(BaseHarness):
    adapter_name = "ASR command boundary"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.external_adapters import AsrAdapter

        entry = ROOT / "tools" / "asr" / "transcribe.py"
        executor = DryRunExternalExecutor(
            {
                "media.transcription": {
                    "transcript_ref": "dry-run://transcripts/asr-gate.txt",
                    "transcript_hash": "sha256:dry-run-asr",
                    "duration_seconds": 12,
                    "segment_count": 3,
                    "quality_status": "passed",
                }
            }
        )
        adapter_result = AsrAdapter(executor).transcribe(
            media_ref="dry-run-hit",
            media_path="validation_evidence/media/asr-gate.mp4",
        )
        return {
            "entrypoint_exists": entry.exists(),
            "adapter_id": adapter_result.adapter_id,
            "capability": adapter_result.capability,
            "transcript_hash": adapter_result.payload["transcript_hash"],
            "planned_command": "tools/asr/transcribe.py --media <controlled-media-path>",
            "requires_isolated_workspace": True,
            "external_io": False,
        }


class ExternalCollectorHarness(BaseHarness):
    adapter_name = "External collector adapter boundary"

    def dry_run(self) -> dict[str, Any]:
        from scripts.core.external_adapters import (
            CommentCollectionAdapter,
            MediaCrawlerCollectorAdapter,
            NetEaseMusicCollectorAdapter,
        )

        entry = ROOT / "scripts" / "collect" / "crawl_competitors.py"
        common = ROOT / "scripts" / "collect" / "common.py"
        executor = DryRunExternalExecutor(
            {
                "platform.video_snapshot": {
                    "items": [
                        {
                            "aweme_id": "dry-run-aweme-1",
                            "url": "https://example.invalid/video/1",
                            "desc": "dry-run video",
                            "like_count": 1,
                            "comment_count": 1,
                        }
                    ]
                },
                "platform.comment_collection": {
                    "comments": [{"comment_id": "dry-run-comment-1", "text": "dry-run comment"}]
                },
                "music.comment_collection": {
                    "comments": [{"comment_id": "dry-run-music-comment-1", "text": "dry-run music comment"}]
                },
            }
        )
        video = MediaCrawlerCollectorAdapter(executor).collect_video_snapshot(
            platform="douyin",
            source_url="https://example.invalid/video/1",
            max_items=1,
        )
        comments = CommentCollectionAdapter(executor).collect_comments(
            platform="douyin",
            source_id="dry-run-aweme-1",
            source_url="https://example.invalid/video/1",
            max_comments=1,
        )
        music = NetEaseMusicCollectorAdapter(executor).collect_song_comments(song_id="dry-run-song", max_comments=1)
        return {
            "entrypoint_exists": entry.exists(),
            "common_adapter_source_exists": common.exists(),
            "adapter_capabilities": [video.capability, comments.capability, music.capability],
            "item_counts": {
                "video_snapshot": video.item_count,
                "comments": comments.item_count,
                "netease_comments": music.item_count,
            },
            "planned_command": "external adapters use controlled fixture/stub execution in dry-run",
            "adapter_source_only": False,
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
