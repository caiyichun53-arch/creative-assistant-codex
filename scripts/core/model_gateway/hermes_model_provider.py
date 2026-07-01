from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelUsage,
)


class HermesModelProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesModelProviderConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 0


class HermesModelProviderAdapter:
    provider_name = "hermes"
    billing_mode = "subscription"

    def __init__(
        self,
        config: HermesModelProviderConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        if route.provider_name != self.provider_name:
            raise HermesModelProviderError("route provider must be hermes")
        model_name = route.model_name or self.config.model
        if not model_name:
            raise HermesModelProviderError("model is required")

        client = self._make_client()
        create_kwargs = {
            "model": model_name,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if route.parameters:
            create_kwargs.update(route.parameters)

        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:  # noqa: BLE001 - provider boundary must normalize failures.
            raise HermesModelProviderError(_scrub_secret(str(exc), self.config.api_key)) from exc

        output_text = _extract_output_text(response)
        usage, usage_status = _extract_usage(response)
        provider_request_id = _text_or_none(getattr(response, "id", None))
        finish_reason = _extract_finish_reason(response)
        metadata = {
            "external_io": True,
            "api_mode": "chat_completions",
            "billing_mode": self.billing_mode,
            "usage_status": usage_status,
            "cost_status": "not_applicable",
            "provider_request_id_status": "available" if provider_request_id else "not_available",
            "finish_reason": finish_reason or "not_available",
            "visible_output_status": "available" if output_text else "empty",
            "max_retries": self.config.max_retries,
            "retry_count": 0,
            "timeout_seconds": self.config.timeout_seconds,
        }
        return ModelProviderResult(
            output_text=output_text,
            usage=usage,
            cost={"status": "not_applicable", "billing_mode": self.billing_mode},
            provider_request_id=provider_request_id,
            metadata=metadata,
        )

    def _make_client(self) -> Any:
        factory = self._client_factory
        if factory is None:
            try:
                from openai import OpenAI  # type: ignore
            except Exception as exc:  # noqa: BLE001 - dependency failure belongs at adapter boundary.
                raise HermesModelProviderError("OpenAI SDK is required for Hermes model provider") from exc
            factory = OpenAI
        try:
            return factory(
                api_key=self.config.api_key,
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
            )
        except TypeError:
            try:
                return factory(api_key=self.config.api_key, base_url=self.config.base_url.rstrip("/"))
            except Exception as exc:  # noqa: BLE001 - normalize provider-client construction failures.
                raise HermesModelProviderError(_scrub_secret(str(exc), self.config.api_key)) from exc
        except Exception as exc:  # noqa: BLE001 - normalize provider-client construction failures.
            raise HermesModelProviderError(_scrub_secret(str(exc), self.config.api_key)) from exc


def _extract_output_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _extract_usage(response: Any) -> tuple[ModelUsage, str]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return ModelUsage(), "not_available"
    prompt_tokens = _int_or_none(getattr(usage, "prompt_tokens", None))
    completion_tokens = _int_or_none(getattr(usage, "completion_tokens", None))
    total_tokens = _int_or_none(getattr(usage, "total_tokens", None))
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return ModelUsage(), "not_available"
    return (
        ModelUsage(
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            total_tokens=total_tokens or 0,
        ),
        "available",
    )


def _extract_finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return _text_or_none(getattr(choices[0], "finish_reason", None))


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _scrub_secret(message: str, api_key: str) -> str:
    scrubbed = message.replace(api_key, "<redacted>") if api_key else message
    scrubbed = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", scrubbed)
    scrubbed = re.sub(r"(api[-_ ]?key[=: ]+)[^\s,;]+", r"\1<redacted>", scrubbed, flags=re.IGNORECASE)
    return scrubbed
