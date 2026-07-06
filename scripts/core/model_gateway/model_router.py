from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts.core.model_gateway.goal07_model_gateway import ModelRoute
from scripts.core.persistence.goal01_store import content_hash


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_ROUTES_PATH = ROOT / "config" / "model_routes.yaml"
WRITING_ROUTE_ID = "writing_generation"
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


class ModelRouter:
    def __init__(self, *, providers: dict[str, ModelProviderDefinition], routes: dict[str, ModelRouteDefinition], config_hash: str):
        self.providers = dict(providers)
        self.routes = dict(routes)
        self.config_hash = config_hash

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
        if not isinstance(provider_data, dict) or not provider_data:
            raise ModelRouterError("model_providers must be a non-empty object")
        if not isinstance(route_data, dict) or not route_data:
            raise ModelRouterError("model_routes must be a non-empty object")

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
            if provider_ref not in providers:
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

        return cls(
            providers=providers,
            routes=routes,
            config_hash=content_hash(data, "model_routes.config.v1"),
        )

    def resolve(
        self,
        route_id: str,
        *,
        route_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        config_version: str = "model_routes.v1",
    ) -> ModelRoute:
        route = self.routes.get(route_id)
        if route is None:
            raise ModelRouterError(f"unknown route_id: {route_id}")
        provider = self.providers.get(route.provider_ref)
        if provider is None:
            raise ModelRouterError(f"missing provider_ref for route_id {route_id}: {route.provider_ref}")
        if not provider.enabled:
            raise ModelRouterError(f"provider is disabled for route_id {route_id}: {route.provider_ref}")
        if route.fallback != "none":
            raise ModelRouterError(f"route_id {route_id} has non-none fallback")
        model_name = str(provider.settings.get("model_ref") or f"provider:{route.provider_ref}")
        return ModelRoute(
            route_name=route_name or route_id,
            provider_name=route.provider_ref,
            model_name=model_name,
            config_version=config_version,
            config_hash=content_hash(
                {
                    "route_id": route.route_id,
                    "provider_ref": route.provider_ref,
                    "provider_type": provider.provider_type,
                    "config_hash": self.config_hash,
                },
                "model_routes.resolved_route.v1",
            ),
            parameters=parameters,
            timeout_ms=timeout_ms,
            route_id=route.route_id,
            provider_ref=route.provider_ref,
        )

    def validate_workflow_node_bindings(self, nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node_id = str(node.get("node_id") or node.get("logical_route") or "<unknown>")
            if "route_id" not in node:
                raise ModelRouterError(f"workflow node {node_id} must bind route_id")
            self.resolve(str(node["route_id"]), route_name=str(node.get("logical_route") or node_id))
            for key in ("provider", "provider_name", "provider_ref", "model", "model_name", "api_gateway", "openai", "codex", "mimo"):
                if key in node:
                    raise ModelRouterError(f"workflow node {node_id} must not bind {key} directly")

            task_type = node.get("task_type")
            if node["route_id"] == BUSINESS_ANALYSIS_ROUTE_ID and task_type in WRITING_TASK_TYPES:
                raise ModelRouterError(f"business_analysis node {node_id} must not bind writing task_type {task_type}")
            if task_type in WRITING_TASK_TYPES and node["route_id"] != WRITING_ROUTE_ID:
                raise ModelRouterError(f"writing workflow node {node_id} must bind {WRITING_ROUTE_ID}")
