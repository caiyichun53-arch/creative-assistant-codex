from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from scripts.core.model_gateway.goal07_model_gateway import ModelRoute
from scripts.core.persistence.goal01_store import content_hash


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_ROUTES_PATH = ROOT / "config" / "model_routes.yaml"
DEFAULT_MODEL_ENV_PATH = ROOT / ".env"
WRITING_ROUTE_ID = "writing_generation"
REQUIRED_MODEL_POSITIONS = frozenset({"dialogue_model", "business_model", "writing_model"})
ACTIVE_PROVIDER_ALIAS = "active_provider"
DEFAULT_ACTIVE_PROVIDER_ENV_REF = "MODEL_ACTIVE_PROVIDER_REF"
WRITING_TASK_TYPES = frozenset(
    {
        "title_generation",
        "title_rewrite",
        "hook_generation",
        "content_structure",
        "rough_draft",
        "final_draft",
        "multi_variant_copy_generation",
        "rewrite",
        "polishing",
        "de_ai_style",
        "colloquial_rewrite",
        "emotional_strengthening",
        "reduce_preachiness",
        "reduce_plagiarism_risk_rewrite",
        "platform_adaptation",
        "cover_copy",
        "publishing_copy",
        "final_copy_review",
    }
)
BUSINESS_ANALYSIS_ROUTE_ID = "business_analysis"
HERMES_CURRENT_PROVIDER_ENV_REF = "HERMES_BUSINESS_MODEL_CLASS"
HERMES_CURRENT_MODEL_ENV_REF = "HERMES_BUSINESS_MODEL_NAME"
HERMES_CURRENT_ENDPOINT_ENV_REF = "HERMES_BUSINESS_MODEL_BASE_URL"


class ModelRouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelProviderDefinition:
    provider_ref: str
    provider_type: str
    enabled: bool
    settings: dict[str, Any]


@dataclass(frozen=True)
class ModelRouteDefinition:
    route_id: str
    provider_ref: str
    fallback: str
    allowed_task_types: tuple[str, ...]


@dataclass(frozen=True)
class HermesTaskModelBinding:
    """Credential-free model identity frozen when a Hermes task is created."""

    route_id: str
    provider_ref: str
    provider_name: str
    provider_type: str
    model_name: str
    endpoint: str
    source: str = "hermes_current_session"
    explicit_override: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider_ref": self.provider_ref,
            "provider_name": self.provider_name,
            "provider_type": self.provider_type,
            "model_name": self.model_name,
            "endpoint": self.endpoint,
            "source": self.source,
            "explicit_override": self.explicit_override,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HermesTaskModelBinding":
        if not isinstance(payload, Mapping):
            raise ModelRouterError("frozen Hermes task model binding must be an object")
        required = (
            "route_id", "provider_ref", "provider_name", "provider_type",
            "model_name", "endpoint", "source",
        )
        values = {key: str(payload.get(key) or "").strip() for key in required}
        if any(not values[key] for key in required):
            raise ModelRouterError("frozen Hermes task model binding is incomplete")
        return cls(
            **values,
            explicit_override=payload.get("explicit_override") is True,
        )


class ModelRouter:
    def __init__(
        self,
        *,
        providers: dict[str, ModelProviderDefinition],
        routes: dict[str, ModelRouteDefinition],
        model_positions: dict[str, str],
        config_hash: str,
        active_provider_env_ref: str = DEFAULT_ACTIVE_PROVIDER_ENV_REF,
        active_provider_allowed_refs: frozenset[str] = frozenset(),
    ):
        self.providers = dict(providers)
        self.routes = dict(routes)
        self.model_positions = dict(model_positions)
        self.config_hash = config_hash
        self.active_provider_env_ref = active_provider_env_ref
        self.active_provider_allowed_refs = frozenset(active_provider_allowed_refs or providers)

    @classmethod
    def from_file(cls, path: Path = DEFAULT_MODEL_ROUTES_PATH) -> "ModelRouter":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_config(data)

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> "ModelRouter":
        if data.get("schema_version") != "model_routes.v1":
            raise ModelRouterError("unexpected model_routes schema_version")
        provider_data = data.get("model_providers") or {}
        route_data = data.get("model_routes") or {}
        position_data = data.get("model_positions") or {}
        selection_data = data.get("model_selection") or {}
        if not isinstance(provider_data, dict) or not provider_data:
            raise ModelRouterError("model_providers must be a non-empty object")
        if not isinstance(route_data, dict) or not route_data:
            raise ModelRouterError("model_routes must be a non-empty object")
        if not isinstance(position_data, dict):
            raise ModelRouterError("model_positions must be an object")
        if not isinstance(selection_data, dict):
            raise ModelRouterError("model_selection must be an object")
        missing_positions = REQUIRED_MODEL_POSITIONS - set(position_data)
        if missing_positions:
            raise ModelRouterError(f"model_positions missing required bindings: {sorted(missing_positions)}")

        providers: dict[str, ModelProviderDefinition] = {}
        for provider_ref, payload in provider_data.items():
            if not isinstance(payload, dict):
                raise ModelRouterError(f"model_providers.{provider_ref} must be an object")
            provider_type = str(payload.get("type") or "")
            if not provider_type:
                raise ModelRouterError(f"model_providers.{provider_ref}.type is required")
            providers[str(provider_ref)] = ModelProviderDefinition(
                provider_ref=str(provider_ref),
                provider_type=provider_type,
                enabled=payload.get("enabled") is True,
                settings=dict(payload),
            )

        active_provider_env_ref = str(
            selection_data.get("active_provider_ref") or DEFAULT_ACTIVE_PROVIDER_ENV_REF
        ).strip()
        if not active_provider_env_ref.isupper() or not active_provider_env_ref.replace("_", "").isalnum():
            raise ModelRouterError("model_selection.active_provider_ref must be an environment-variable reference")
        configured_allowed_refs = selection_data.get("allowed_provider_refs")
        if configured_allowed_refs is None:
            active_provider_allowed_refs = frozenset(providers)
        else:
            if not isinstance(configured_allowed_refs, list) or not all(
                isinstance(item, str) and item.strip() for item in configured_allowed_refs
            ):
                raise ModelRouterError("model_selection.allowed_provider_refs must be a string list")
            active_provider_allowed_refs = frozenset(str(item).strip() for item in configured_allowed_refs)
            unknown_allowed_refs = active_provider_allowed_refs - set(providers)
            if unknown_allowed_refs:
                raise ModelRouterError(
                    "model_selection.allowed_provider_refs contains unknown providers: "
                    + ", ".join(sorted(unknown_allowed_refs))
                )
            if not active_provider_allowed_refs:
                raise ModelRouterError("model_selection.allowed_provider_refs must not be empty")

        routes: dict[str, ModelRouteDefinition] = {}
        for route_id, payload in route_data.items():
            if not isinstance(payload, dict):
                raise ModelRouterError(f"model_routes.{route_id} must be an object")
            fallback = str(payload.get("fallback") or "")
            if fallback != "none":
                raise ModelRouterError(f"model_routes.{route_id}.fallback must be none")
            provider_ref = str(payload.get("provider_ref") or "")
            if not provider_ref:
                raise ModelRouterError(f"model_routes.{route_id}.provider_ref is required")
            if provider_ref != ACTIVE_PROVIDER_ALIAS and provider_ref not in providers:
                raise ModelRouterError(f"model_routes.{route_id}.provider_ref does not exist: {provider_ref}")
            task_types = payload.get("allowed_task_types") or []
            if not isinstance(task_types, list) or not all(isinstance(item, str) and item for item in task_types):
                raise ModelRouterError(f"model_routes.{route_id}.allowed_task_types must be a string list")
            routes[str(route_id)] = ModelRouteDefinition(
                route_id=str(route_id),
                provider_ref=provider_ref,
                fallback=fallback,
                allowed_task_types=tuple(task_types),
            )

        model_positions: dict[str, str] = {}
        for position, route_id in position_data.items():
            if not isinstance(route_id, str) or not route_id:
                raise ModelRouterError(f"model_positions.{position} must bind a non-empty route_id")
            if route_id not in routes:
                raise ModelRouterError(f"model_positions.{position} binds unknown route_id: {route_id}")
            model_positions[str(position)] = route_id

        return cls(
            providers=providers,
            routes=routes,
            model_positions=model_positions,
            config_hash=content_hash(data, "model_routes.config.v1"),
            active_provider_env_ref=active_provider_env_ref,
            active_provider_allowed_refs=active_provider_allowed_refs,
        )

    def resolve(
        self,
        route_id: str,
        *,
        route_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        config_version: str = "model_routes.v1",
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
    ) -> ModelRoute:
        route = self.routes.get(route_id)
        if route is None:
            raise ModelRouterError(f"unknown route_id: {route_id}")
        provider_ref = self._resolve_provider_ref(
            route.provider_ref, environment=environment, env_path=env_path
        )
        provider = self.providers.get(provider_ref)
        if provider is None:
            raise ModelRouterError(f"missing provider_ref for route_id {route_id}: {provider_ref}")
        if not provider.enabled:
            raise ModelRouterError(f"provider is disabled for route_id {route_id}: {provider_ref}")
        if route.fallback != "none":
            raise ModelRouterError(f"route_id {route_id} has non-none fallback")
        model_name = str(provider.settings.get("model_ref") or f"provider:{provider_ref}")
        return ModelRoute(
            route_name=route_name or route_id,
            provider_name=str(provider.settings.get("provider_name") or provider_ref),
            model_name=model_name,
            config_version=config_version,
            config_hash=content_hash(
                {
                    "route_id": route.route_id,
                    "provider_ref": provider_ref,
                    "provider_type": provider.provider_type,
                    "config_hash": self.config_hash,
                },
                "model_routes.resolved_route.v1",
            ),
            parameters=parameters,
            timeout_ms=timeout_ms,
            route_id=route.route_id,
            provider_ref=provider_ref,
        )

    def resolve_bound_position(
        self,
        position: str,
        *,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
        route_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        config_version: str = "model_routes.v1",
    ) -> ModelRoute:
        route_id = self.model_positions.get(position)
        if route_id is None:
            raise ModelRouterError(f"model position is not explicitly bound: {position}")
        return self.resolve_bound_route(
            route_id,
            environment=environment,
            env_path=env_path,
            route_name=route_name or position,
            parameters=parameters,
            timeout_ms=timeout_ms,
            config_version=config_version,
        )

    def resolve_bound_route(
        self,
        route_id: str,
        *,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
        route_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        config_version: str = "model_routes.v1",
    ) -> ModelRoute:
        """Resolve a configured environment reference without contacting a provider.

        `model_ref` is an environment key, never a literal model name.  A
        conflicting process value and dotenv value is deliberately rejected:
        formal execution must not silently choose one model binding.
        """
        route = self.routes.get(route_id)
        if route is None:
            raise ModelRouterError(f"unknown route_id: {route_id}")
        provider_ref = self._resolve_provider_ref(
            route.provider_ref, environment=environment, env_path=env_path
        )
        provider = self.providers.get(provider_ref)
        if provider is None:
            raise ModelRouterError(f"missing provider_ref for route_id {route_id}: {provider_ref}")
        if not provider.enabled:
            raise ModelRouterError(f"provider is disabled for route_id {route_id}: {provider_ref}")
        if route.fallback != "none":
            raise ModelRouterError(f"route_id {route_id} has non-none fallback")

        model_ref = _required_environment_reference(provider.settings, "model_ref", route_id)
        model_name = _resolve_environment_reference(model_ref, environment=environment, env_path=env_path)
        class_ref = provider.settings.get("model_class_ref")
        if class_ref:
            model_class = _resolve_environment_reference(str(class_ref), environment=environment, env_path=env_path)
            if model_class.casefold() != provider.provider_type.casefold():
                raise ModelRouterError(
                    f"provider class mismatch for route_id {route_id}: expected {provider.provider_type}, got {model_class}"
                )
        provider_name = str(provider.settings.get("provider_name") or "")
        if not provider_name:
            raise ModelRouterError(f"model_providers.{provider_ref}.provider_name is required for a bound route")

        return ModelRoute(
            route_name=route_name or route_id,
            provider_name=provider_name,
            model_name=model_name,
            config_version=config_version,
            config_hash=content_hash(
                {
                    "route_id": route.route_id,
                    "provider_ref": provider_ref,
                    "provider_name": provider_name,
                    "provider_type": provider.provider_type,
                    "model_ref": model_ref,
                    "model_name": model_name,
                    "config_hash": self.config_hash,
                },
                "model_routes.bound_route.v1",
            ),
            parameters=parameters,
            timeout_ms=timeout_ms,
            route_id=route.route_id,
            provider_ref=provider_ref,
        )

    def resolve_hermes_task_binding(
        self,
        *,
        route_id: str,
        current_model: str,
        current_provider: str,
        current_endpoint: str,
        explicit_model_override: str | None = None,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
    ) -> HermesTaskModelBinding:
        """Bind a new formal task to the live Hermes session, never repo defaults."""
        route = self.routes.get(route_id)
        if route is None:
            raise ModelRouterError(f"unknown route_id: {route_id}")
        if route.fallback != "none":
            raise ModelRouterError(f"route_id {route_id} has non-none fallback")
        if not str(current_provider or "").strip():
            raise ModelRouterError("Hermes current session has no provider identity")
        model_name = str(explicit_model_override or current_model or "").strip()
        if not model_name:
            raise ModelRouterError("Hermes current session has no model identity")
        endpoint = _normalize_endpoint(current_endpoint)
        if not endpoint:
            raise ModelRouterError("Hermes current session has no model endpoint")

        matches: list[tuple[str, ModelProviderDefinition]] = []
        for provider_ref in sorted(self.active_provider_allowed_refs):
            provider = self.providers.get(provider_ref)
            if provider is None or not provider.enabled:
                continue
            endpoint_ref = str(provider.settings.get("endpoint_ref") or "").strip()
            if not endpoint_ref:
                continue
            configured_endpoint = _resolve_environment_reference(
                endpoint_ref, environment=environment, env_path=env_path
            )
            if _normalize_endpoint(configured_endpoint) == endpoint:
                matches.append((provider_ref, provider))
        if len(matches) != 1:
            raise ModelRouterError(
                "Hermes current session endpoint must match exactly one configured provider"
            )
        provider_ref, provider = matches[0]
        provider_name = str(provider.settings.get("provider_name") or "").strip()
        if not provider_name:
            raise ModelRouterError(f"configured provider {provider_ref} has no provider_name")
        return HermesTaskModelBinding(
            route_id=route_id,
            provider_ref=provider_ref,
            provider_name=provider_name,
            provider_type=provider.provider_type,
            model_name=model_name,
            endpoint=endpoint,
            source=(
                "user_explicit_override"
                if str(explicit_model_override or "").strip()
                else "hermes_current_session"
            ),
            explicit_override=bool(str(explicit_model_override or "").strip()),
        )

    def resolve_current_hermes_execution_binding(
        self,
        *,
        route_id: str,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
    ) -> HermesTaskModelBinding:
        """Read the current Hermes business model once for a new execution."""
        return self.resolve_hermes_task_binding(
            route_id=route_id,
            current_model=_resolve_environment_reference(
                HERMES_CURRENT_MODEL_ENV_REF,
                environment=environment,
                env_path=env_path,
            ),
            current_provider=_resolve_environment_reference(
                HERMES_CURRENT_PROVIDER_ENV_REF,
                environment=environment,
                env_path=env_path,
            ),
            current_endpoint=_resolve_environment_reference(
                HERMES_CURRENT_ENDPOINT_ENV_REF,
                environment=environment,
                env_path=env_path,
            ),
            environment=environment,
            env_path=env_path,
        )

    def resolve_frozen_task_route(
        self,
        binding: HermesTaskModelBinding | Mapping[str, Any],
        *,
        route_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
    ) -> ModelRoute:
        """Resolve a persisted task binding without reading active model selection."""
        frozen = (
            binding
            if isinstance(binding, HermesTaskModelBinding)
            else HermesTaskModelBinding.from_payload(binding)
        )
        route = self.routes.get(frozen.route_id)
        provider = self.providers.get(frozen.provider_ref)
        if route is None or provider is None or not provider.enabled:
            raise ModelRouterError("frozen Hermes task model binding is no longer configured")
        if route.fallback != "none":
            raise ModelRouterError("frozen Hermes task route must have fallback none")
        provider_name = str(provider.settings.get("provider_name") or "").strip()
        endpoint_ref = str(provider.settings.get("endpoint_ref") or "").strip()
        configured_endpoint = _resolve_environment_reference(
            endpoint_ref, environment=environment, env_path=env_path
        )
        if (
            provider.provider_type != frozen.provider_type
            or provider_name != frozen.provider_name
            or _normalize_endpoint(configured_endpoint) != _normalize_endpoint(frozen.endpoint)
        ):
            raise ModelRouterError(
                "frozen Hermes task provider no longer matches formal configuration"
            )
        return ModelRoute(
            route_name=route_name or frozen.route_id,
            provider_name=frozen.provider_name,
            model_name=frozen.model_name,
            config_version="hermes_task_binding.v1",
            config_hash=content_hash(frozen.as_payload(), "hermes_task_binding.v1"),
            parameters=parameters,
            timeout_ms=timeout_ms,
            route_id=frozen.route_id,
            provider_ref=frozen.provider_ref,
        )

    def validate_bound_provider_configuration(
        self,
        route: ModelRoute,
        *,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
    ) -> ModelProviderDefinition:
        """Validate only the selected provider's endpoint and credential binding."""
        provider = self.resolve_bound_provider(route)
        endpoint_ref = str(provider.settings.get("endpoint_ref") or "").strip()
        if not endpoint_ref:
            raise ModelRouterError(f"provider {provider.provider_ref} has no endpoint_ref")
        endpoint = _resolve_environment_reference(endpoint_ref, environment=environment, env_path=env_path)
        if not endpoint.startswith(("https://", "http://")):
            raise ModelRouterError(f"provider {provider.provider_ref} endpoint is not HTTP(S)")

        auth_ref = str(provider.settings.get("auth_ref") or "").strip()
        auth_file_ref = str(provider.settings.get("auth_file_ref") or "").strip()
        if bool(auth_ref) == bool(auth_file_ref):
            raise ModelRouterError(
                f"provider {provider.provider_ref} must configure exactly one auth_ref or auth_file_ref"
            )
        if auth_ref:
            _resolve_environment_reference(auth_ref, environment=environment, env_path=env_path)
        else:
            secret_path = Path(
                _resolve_environment_reference(auth_file_ref, environment=environment, env_path=env_path)
            )
            if not secret_path.is_file():
                raise ModelRouterError(f"provider {provider.provider_ref} secret file is missing")
        return provider

    def _resolve_provider_ref(
        self,
        provider_ref: str,
        *,
        environment: Mapping[str, str] | None,
        env_path: Path | None,
    ) -> str:
        if provider_ref != ACTIVE_PROVIDER_ALIAS:
            return provider_ref
        selected_ref = _resolve_environment_reference(
            self.active_provider_env_ref, environment=environment, env_path=env_path
        )
        if selected_ref not in self.active_provider_allowed_refs:
            raise ModelRouterError(
                f"active provider is not allowed: {selected_ref}; "
                f"allowed providers: {sorted(self.active_provider_allowed_refs)}"
            )
        return selected_ref

    def resolve_bound_provider(self, route: ModelRoute) -> ModelProviderDefinition:
        """Return the provider definition belonging to an already bound route.

        Bound routes carry the provider reference needed to build the adapter.
        Keeping this lookup in the router prevents each business gateway from
        reaching into the router's internal provider map independently.
        """
        provider_ref = str(route.provider_ref or "").strip()
        if not provider_ref and route.route_id:
            definition = self.routes.get(route.route_id)
            provider_ref = str(definition.provider_ref).strip() if definition else ""
        if not provider_ref:
            raise ModelRouterError("bound route has no provider_ref")
        provider = self.providers.get(provider_ref)
        if provider is None:
            raise ModelRouterError(
                f"missing provider_ref for bound route: {provider_ref}"
            )
        if not provider.enabled:
            raise ModelRouterError(
                f"provider is disabled for bound route: {provider_ref}"
            )
        configured_provider_name = str(provider.settings.get("provider_name") or "").strip()
        if configured_provider_name and configured_provider_name != route.provider_name:
            raise ModelRouterError(
                "bound route provider_name does not match its provider definition"
            )
        return provider

    def validate_workflow_node_bindings(self, nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node_id = str(node.get("node_id") or node.get("logical_route") or "<unknown>")
            if "route_id" not in node:
                raise ModelRouterError(f"workflow node {node_id} must bind route_id")
            self.resolve_bound_route(
                str(node["route_id"]),
                route_name=str(node.get("logical_route") or node_id),
            )
            for key in ("provider", "provider_name", "provider_ref", "model", "model_name", "api_gateway", "openai", "codex", "mimo"):
                if key in node:
                    raise ModelRouterError(f"workflow node {node_id} must not bind {key} directly")

            task_type = node.get("task_type")
            if node["route_id"] == BUSINESS_ANALYSIS_ROUTE_ID and task_type in WRITING_TASK_TYPES:
                raise ModelRouterError(f"business_analysis node {node_id} must not bind writing task_type {task_type}")
            if task_type in WRITING_TASK_TYPES and node["route_id"] != WRITING_ROUTE_ID:
                raise ModelRouterError(f"writing workflow node {node_id} must bind {WRITING_ROUTE_ID}")


def _required_environment_reference(settings: Mapping[str, Any], setting: str, route_id: str) -> str:
    reference = str(settings.get(setting) or "")
    if not reference:
        raise ModelRouterError(f"model route {route_id} has no {setting}")
    if not reference.isupper() or not reference.replace("_", "").isalnum():
        raise ModelRouterError(f"model route {route_id} {setting} must be an environment-variable reference")
    return reference


def _normalize_endpoint(value: Any) -> str:
    return str(value or "").strip().rstrip("/").casefold()


def _resolve_environment_reference(
    reference: str,
    *,
    environment: Mapping[str, str] | None,
    env_path: Path | None,
) -> str:
    process_values = environment if environment is not None else os.environ
    process_value = str(process_values.get(reference) or "").strip()
    dotenv_value = _dotenv_value(reference, env_path)
    if process_value and dotenv_value and process_value != dotenv_value:
        raise ModelRouterError(f"conflicting values for configured environment reference: {reference}")
    value = process_value or dotenv_value
    if not value:
        raise ModelRouterError(f"configured environment reference is unresolved: {reference}")
    return value


def _dotenv_value(reference: str, env_path: Path | None) -> str:
    if env_path is None or not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{reference}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
