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
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    env_path: Path | None = DEFAULT_MODEL_ENV_PATH,
) -> Any:
    """Build one adapter from the bound route without automatic fallback."""

    provider_type = provider.provider_type.casefold()
    if provider_type in {"mimo", "openai_compatible", "relay"}:
        auth_ref = str(provider.settings.get("auth_ref") or "").strip()
        auth_file_ref = str(provider.settings.get("auth_file_ref") or "").strip()
        endpoint_ref = str(provider.settings.get("endpoint_ref") or "").strip()
        if (not auth_ref and not auth_file_ref) or not endpoint_ref:
            raise ModelRouterError(
                f"configured provider {provider.provider_ref} lacks an auth reference or endpoint_ref"
            )
        if auth_ref and auth_file_ref:
            raise ModelRouterError(
                f"configured provider {provider.provider_ref} must use either auth_ref or auth_file_ref"
            )
        api_key = (
            _configured_value(auth_ref, environment=environment, env_path=env_path)
            if auth_ref
            else _configured_secret_file(
                auth_file_ref,
                environment=environment,
                env_path=env_path,
            )
        )
        base_url = _configured_value(endpoint_ref, environment=environment, env_path=env_path)
        if not base_url.startswith(("https://", "http://")):
            raise ModelRouterError("configured model endpoint must be an HTTP(S) URL")
        if provider_type in {"openai_compatible", "relay"} and not base_url.rstrip("/").endswith("/v1"):
            base_url = f"{base_url.rstrip('/')}/v1"
        default_parameters = provider.settings.get("default_parameters") or {}
        if not isinstance(default_parameters, dict):
            raise ModelRouterError(
                f"configured provider {provider.provider_ref} default_parameters must be an object"
            )
        adapter = HermesModelProviderAdapter(
            HermesModelProviderConfig(
                api_key=api_key,
                base_url=base_url,
                model=route.model_name,
                # Non-streaming formal calls expose no progress events.  Use
                # the first-activity budget as the transport deadline so a
                # provider that never answers cannot leave the Core node in
                # processing forever.
                timeout_seconds=float(
                    getattr(model_limits, "first_activity_seconds", 120)
                ),
                first_activity_timeout_seconds=float(
                    getattr(model_limits, "first_activity_seconds", 120)
                ),
                stalled_activity_timeout_seconds=float(
                    getattr(model_limits, "stalled_seconds", 300)
                ),
                max_retries=0,
                default_parameters=dict(default_parameters),
            )
        )
    elif provider_type == "codex_app_server":
        config = CodexAppServerProviderConfig.from_environment(
            cwd=(cwd or ROOT),
            environment=dict(environment) if environment is not None else None,
            env_path=env_path,
            network_access=provider.settings.get("network_access") is True,
            sandbox=str(provider.settings.get("sandbox") or "read-only"),
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


def _configured_secret_file(
    reference: str,
    *,
    environment: Mapping[str, str] | None,
    env_path: Path | None,
) -> str:
    secret_path = Path(_configured_value(reference, environment=environment, env_path=env_path))
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ModelRouterError(
            f"configured secret file cannot be read: {reference}"
        ) from exc
    if not secret:
        raise ModelRouterError(f"configured secret file is empty: {reference}")
    return secret


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
