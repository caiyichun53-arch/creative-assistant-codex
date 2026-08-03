"""Build the one explicitly configured model adapter for formal gateways."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from scripts.core.model_gateway.codex_app_server_provider import (
    CodexAppServerProviderAdapter,
    CodexAppServerProviderConfig,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelRoute
from scripts.core.model_gateway.hermes_model_provider import (
    HermesModelProviderAdapter,
    HermesModelProviderConfig,
)
from scripts.core.model_gateway.model_router import (
    DEFAULT_MODEL_ENV_PATH,
    ModelProviderDefinition,
    ModelRouterError,
)


ROOT = Path(__file__).resolve().parents[3]


def build_configured_model_provider(
    provider: ModelProviderDefinition,
    route: ModelRoute,
    *,
    model_limits: Any,
    environment: Mapping[str, str] | None = None,
    env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
) -> Any:
    """Build one adapter from the bound route without automatic fallback."""

    provider_type = provider.provider_type.casefold()
    if provider_type == "mimo":
        auth_ref = str(provider.settings.get("auth_ref") or "").strip()
        endpoint_ref = str(provider.settings.get("endpoint_ref") or "").strip()
        if not auth_ref or not endpoint_ref:
            raise ModelRouterError(
                f"configured provider {provider.provider_ref} lacks auth_ref or endpoint_ref"
            )
        api_key = _configured_value(auth_ref, environment=environment, env_path=env_path)
        base_url = _configured_value(endpoint_ref, environment=environment, env_path=env_path)
        if not base_url.startswith(("https://", "http://")):
            raise ModelRouterError("configured model endpoint must be an HTTP(S) URL")
        adapter = HermesModelProviderAdapter(
            HermesModelProviderConfig(
                api_key=api_key,
                base_url=base_url,
                model=route.model_name,
                first_activity_timeout_seconds=float(
                    getattr(model_limits, "first_activity_seconds", 120)
                ),
                stalled_activity_timeout_seconds=float(
                    getattr(model_limits, "stalled_seconds", 300)
                ),
                max_retries=0,
            )
        )
    elif provider_type == "codex_app_server":
        config = CodexAppServerProviderConfig.from_environment(
            cwd=ROOT,
            environment=dict(environment) if environment is not None else None,
            env_path=env_path,
        )
        if config.model != route.model_name:
            raise ModelRouterError(
                "configured Codex model does not match the bound formal route"
            )
        adapter = CodexAppServerProviderAdapter(config)
    else:
        raise ModelRouterError(
            f"unsupported configured model provider type: {provider.provider_type}"
        )

    if adapter.provider_name != route.provider_name:
        raise ModelRouterError(
            "configured provider adapter does not match the bound route provider"
        )
    return adapter


def _configured_value(
    reference: str,
    *,
    environment: Mapping[str, str] | None,
    env_path: Path | None,
) -> str:
    values = environment if environment is not None else os.environ
    process_value = str(values.get(reference) or "").strip()
    dotenv_value = _dotenv_value(reference, env_path)
    if process_value and dotenv_value and process_value != dotenv_value:
        raise ModelRouterError(
            f"conflicting values for configured environment reference: {reference}"
        )
    value = process_value or dotenv_value
    if not value:
        raise ModelRouterError(
            f"configured environment reference is unresolved: {reference}"
        )
    return value


def _dotenv_value(reference: str, path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != reference:
            continue
        return value.strip().strip('"').strip("'")
    return ""
