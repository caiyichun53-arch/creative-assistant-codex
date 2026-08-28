from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from scripts.core.persistence.goal01_store import PersistenceStore, content_hash


class ModelGatewayError(RuntimeError):
    """A failed atomic model call, with its retained run record when available."""

    def __init__(
        self, message: str, *, model_run_envelope_version_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.model_run_envelope_version_id = model_run_envelope_version_id


@dataclass(frozen=True)
class ModelRoute:
    route_name: str
    provider_name: str
    model_name: str
    config_version: str
    config_hash: str
    parameters: dict[str, Any] | None = None
    timeout_ms: int | None = None
    route_id: str | None = None
    provider_ref: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    route_name: str
    prompt: str
    input_payload: dict[str, Any]
    correlation_id: str | None = None
    skill_name: str | None = None
    skill_version: str | None = None
    skill_hash: str | None = None
    binding_name: str | None = None
    binding_version: str | None = None
    binding_hash: str | None = None
    response_format: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ModelProviderResult:
    output_text: str
    usage: ModelUsage
    cost: dict[str, Any]
    provider_request_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelRunEnvelope:
    status: str
    correlation_id: str | None
    route_name: str
    route_id: str | None
    provider_name: str
    provider_ref: str | None
    model_name: str
    prompt_hash: str
    skill_name: str | None
    skill_version: str | None
    skill_hash: str | None
    binding_name: str | None
    binding_version: str | None
    binding_hash: str | None
    config_version: str
    config_hash: str
    input_hash: str
    output_hash: str | None
    duration_ms: int
    cost: dict[str, Any]
    usage: ModelUsage
    provider_request_id: str | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "correlation_id": self.correlation_id,
            "route_name": self.route_name,
            "route_id": self.route_id,
            "provider_name": self.provider_name,
            "provider_ref": self.provider_ref,
            "model_name": self.model_name,
            "prompt_hash": self.prompt_hash,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "skill_hash": self.skill_hash,
            "binding_name": self.binding_name,
            "binding_version": self.binding_version,
            "binding_hash": self.binding_hash,
            "config_version": self.config_version,
            "config_hash": self.config_hash,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "duration_ms": self.duration_ms,
            "cost": self.cost,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "provider_request_id": self.provider_request_id,
            "error": self.error,
            "metadata": self.metadata or {},
        }


@dataclass(frozen=True)
class ModelRunResult:
    output_text: str
    envelope_version_id: str
    envelope: ModelRunEnvelope


class ModelProvider(Protocol):
    provider_name: str

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        ...


class ModelRunMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        payload = envelope.as_payload()
        root_id = self.store.create_root("model_run_envelope")
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal07.model_run_envelope.v1",
            business_payload={
            "status": envelope.status,
            "route_name": envelope.route_name,
            "route_id": envelope.route_id,
            "provider_name": envelope.provider_name,
            "provider_ref": envelope.provider_ref,
            "model_name": envelope.model_name,
                "input_hash": envelope.input_hash,
                "output_hash": envelope.output_hash,
            },
        )
        self.store.set_current_version(root_id, version_id)
        self.store.record_audit(
            event_type="goal07.model_gateway.run_envelope_recorded",
            actor="model_gateway",
            object_kind="model_run_envelope",
            object_id=root_id,
            version_id=version_id,
            payload={
                "status": envelope.status,
                "route_name": envelope.route_name,
                "route_id": envelope.route_id,
                "provider_name": envelope.provider_name,
                "provider_ref": envelope.provider_ref,
                "model_name": envelope.model_name,
                "duration_ms": envelope.duration_ms,
                "cost": envelope.cost,
            },
            correlation_id=root_id,
        )
        return version_id


class ModelGateway:
    def __init__(
        self,
        *,
        routes: dict[str, ModelRoute],
        providers: dict[str, ModelProvider],
        materializer: ModelRunMaterializer,
        monotonic_ms: Callable[[], int] | None = None,
    ):
        self.routes = dict(routes)
        self.providers = dict(providers)
        self.materializer = materializer
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))

    def complete(self, request: ModelRequest) -> ModelRunResult:
        self._validate_request(request)
        route = self.routes.get(request.route_name)
        if route is None:
            raise ModelGatewayError(f"unknown model route: {request.route_name}")
        provider = self.providers.get(route.provider_name)
        if provider is None:
            raise ModelGatewayError(f"missing provider for route: {route.provider_name}")
        if provider.provider_name != route.provider_name:
            raise ModelGatewayError("provider_name mismatch")

        start_ms = self.monotonic_ms()
        try:
            provider_result = provider.complete(request, route)
        except Exception as exc:  # noqa: BLE001 - provider boundary records failure envelope.
            duration_ms = max(0, self.monotonic_ms() - start_ms)
            provider_diagnostics = getattr(exc, "diagnostics", None)
            if not isinstance(provider_diagnostics, dict):
                provider_diagnostics = {}
            envelope = self._build_envelope(
                request=request,
                route=route,
                status="failed",
                duration_ms=duration_ms,
                output_text=None,
                usage=ModelUsage(),
                cost={},
                provider_request_id=None,
                error={"code": "provider_error", "message": str(exc)},
                metadata={"provider_diagnostics": provider_diagnostics} if provider_diagnostics else None,
            )
            envelope_version_id = self.materializer.persist_envelope(envelope)
            gateway_error = ModelGatewayError(
                f"model provider failed: {exc}",
                model_run_envelope_version_id=envelope_version_id,
            )
            # Preserve provider-side finish reason and length diagnostics on the
            # test-visible gateway error.  The formal boundary still fails
            # closed; this only makes the cause inspectable.
            gateway_error.diagnostics = provider_diagnostics
            raise gateway_error from exc

        duration_ms = max(0, self.monotonic_ms() - start_ms)
        if route.timeout_ms is not None and duration_ms > route.timeout_ms:
            envelope = self._build_envelope(
                request=request,
                route=route,
                status="failed",
                duration_ms=duration_ms,
                output_text=None,
                usage=provider_result.usage,
                cost=provider_result.cost,
                provider_request_id=provider_result.provider_request_id,
                error={"code": "timeout", "timeout_ms": route.timeout_ms, "duration_ms": duration_ms},
                metadata=provider_result.metadata,
            )
            envelope_version_id = self.materializer.persist_envelope(envelope)
            raise ModelGatewayError(
                f"model provider timed out after {duration_ms}ms",
                model_run_envelope_version_id=envelope_version_id,
            )

        envelope = self._build_envelope(
            request=request,
            route=route,
            status="succeeded",
            duration_ms=duration_ms,
            output_text=provider_result.output_text,
            usage=provider_result.usage,
            cost=provider_result.cost,
            provider_request_id=provider_result.provider_request_id,
            metadata=provider_result.metadata,
        )
        envelope_version_id = self.materializer.persist_envelope(envelope)
        return ModelRunResult(
            output_text=provider_result.output_text,
            envelope_version_id=envelope_version_id,
            envelope=envelope,
        )

    @staticmethod
    def _validate_request(request: ModelRequest) -> None:
        if not request.route_name:
            raise ModelGatewayError("route_name is required")
        if not request.prompt:
            raise ModelGatewayError("prompt is required")

    @staticmethod
    def _build_envelope(
        *,
        request: ModelRequest,
        route: ModelRoute,
        status: str,
        duration_ms: int,
        output_text: str | None,
        usage: ModelUsage,
        cost: dict[str, Any],
        provider_request_id: str | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelRunEnvelope:
        return ModelRunEnvelope(
            status=status,
            correlation_id=request.correlation_id,
            route_name=route.route_name,
            route_id=route.route_id,
            provider_name=route.provider_name,
            provider_ref=route.provider_ref,
            model_name=route.model_name,
            prompt_hash=content_hash({"prompt": request.prompt}, "goal07.prompt.v1"),
            skill_name=request.skill_name,
            skill_version=request.skill_version,
            skill_hash=request.skill_hash,
            binding_name=request.binding_name,
            binding_version=request.binding_version,
            binding_hash=request.binding_hash,
            config_version=route.config_version,
            config_hash=route.config_hash,
            input_hash=content_hash(request.input_payload, "goal07.model_input.v1"),
            output_hash=content_hash({"output_text": output_text}, "goal07.model_output.v1")
            if output_text is not None
            else None,
            duration_ms=duration_ms,
            cost=dict(cost),
            usage=usage,
            provider_request_id=provider_request_id,
            error=error,
            metadata={**(request.metadata or {}), **(metadata or {})},
        )
