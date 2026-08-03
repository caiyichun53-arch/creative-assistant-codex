from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelUsage,
)


class HermesModelProviderError(RuntimeError):
    """A provider failure with safe, non-content diagnostic facts."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class HermesModelProviderConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float | None = None
    first_activity_timeout_seconds: float | None = None
    stalled_activity_timeout_seconds: float | None = None
    activity_callback: Callable[[dict[str, Any]], None] | None = None
    activity_heartbeat_seconds: float = 15.0
    max_retries: int = 0


class HermesModelProviderAdapter:
    provider_name = "hermes"
    billing_mode = "subscription"
    disallowed_route_parameter_keys = frozenset(
        {
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "previous_response_id",
            "conversation",
            "memory",
            "store",
            "metadata",
            "response_sink",
            "webhook_url",
            "feishu",
            "messaging",
            "nested_jobs",
            "job_orchestration",
            "file_output",
            "terminal",
            "shell",
        }
    )

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
        self._validate_isolated_route(route)
        model_name = route.model_name or self.config.model
        if not model_name:
            raise HermesModelProviderError("model is required")

        stream_requested = bool((route.parameters or {}).get("stream"))
        client = self._make_client(streaming=stream_requested)
        create_kwargs = {
            "model": model_name,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if route.parameters:
            create_kwargs.update(route.parameters)
        if request.response_format:
            create_kwargs["response_format"] = dict(request.response_format)

        if stream_requested:
            return self._complete_stream(client, create_kwargs)

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
            "cost_status": "not_reported",
            "provider_request_id_status": "available" if provider_request_id else "not_available",
            "finish_reason": finish_reason or "not_available",
            "visible_output_status": "available" if output_text else "empty",
            "max_retries": self.config.max_retries,
            "retry_count": 0,
            "timeout_seconds": self.config.timeout_seconds,
            "tools_enabled": False,
            "memory_enabled": False,
            "messaging_enabled": False,
            "nested_job_orchestration_enabled": False,
            "file_or_terminal_side_effects_enabled": False,
        }
        if not output_text.strip():
            # An empty assistant message is not a usable completion.  Treating it as
            # successful loses the actual provider condition and makes downstream
            # JSON validation report a misleading error instead.
            raise HermesModelProviderError(
                "provider returned empty visible content",
                diagnostics={
                    "provider_response_kind": "empty_visible_content",
                    "finish_reason": metadata["finish_reason"],
                    "choice_count": len(getattr(response, "choices", None) or []),
                    "reasoning_content_status": _message_field_status(response, "reasoning_content"),
                    "refusal_status": _message_field_status(response, "refusal"),
                    "usage_status": usage_status,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
        return ModelProviderResult(
            output_text=output_text,
            usage=usage,
            cost={"status": "not_reported", "billing_mode": self.billing_mode},
            provider_request_id=provider_request_id,
            metadata=metadata,
        )

    def _complete_stream(self, client: Any, create_kwargs: dict[str, Any]) -> ModelProviderResult:
        """Collect only final visible content from a streamed provider response.

        Reasoning chunks are deliberately never retained as business output.
        A healthy stream remains alive regardless of elapsed wall-clock time;
        only no first activity, stalled activity, an explicit provider error,
        or a completed invalid result can stop it.
        """
        start = time.monotonic()
        first_activity_timeout = self.config.first_activity_timeout_seconds or self.config.timeout_seconds
        stalled_activity_timeout = self.config.stalled_activity_timeout_seconds or self.config.timeout_seconds
        visible_parts: list[str] = []
        first_chunk_ms: int | None = None
        chunk_count = 0
        reasoning_chunk_count = 0
        complete_visible_json_at_ms: int | None = None
        finish_reason: str | None = None
        provider_request_id: str | None = None
        usage = ModelUsage()
        usage_status = "not_available"
        last_activity_callback_at = start
        stream_box: dict[str, Any] = {}
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def consume() -> None:
            try:
                stream = client.chat.completions.create(**create_kwargs)
                stream_box["stream"] = stream
                for chunk in stream:
                    events.put(("chunk", chunk))
                events.put(("complete", None))
            except Exception as exc:  # noqa: BLE001 - normalized at this provider boundary.
                events.put(("error", exc))

        threading.Thread(target=consume, name="hermes-stream-reader", daemon=True).start()
        try:
            while True:
                wait_seconds = first_activity_timeout if first_chunk_ms is None else stalled_activity_timeout
                try:
                    event, value = events.get(timeout=wait_seconds)
                except queue.Empty:
                    close = getattr(stream_box.get("stream"), "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            # A streaming SDK may still be advancing its iterator in
                            # the reader thread.  The liveness decision must not be
                            # replaced by that best-effort close error.
                            pass
                    response_kind = (
                        "stream_first_activity_timeout"
                        if first_chunk_ms is None
                        else "stream_stalled_activity_timeout"
                    )
                    raise HermesModelProviderError(
                        "streamed model response produced no activity within its liveness limit",
                        diagnostics={
                            "provider_response_kind": response_kind,
                            "elapsed_ms": int((time.monotonic() - start) * 1000),
                            "chunk_count": chunk_count,
                            "reasoning_chunk_count": reasoning_chunk_count,
                        },
                    )
                if event == "complete":
                    break
                if event == "error":
                    raise value
                chunk = value
                elapsed_ms = int((time.monotonic() - start) * 1000)
                chunk_count += 1
                if first_chunk_ms is None:
                    first_chunk_ms = elapsed_ms
                now = time.monotonic()
                if self.config.activity_callback is not None and (
                    chunk_count == 1
                    or now - last_activity_callback_at >= self.config.activity_heartbeat_seconds
                ):
                    try:
                        self.config.activity_callback({
                            "elapsed_ms": elapsed_ms,
                            "chunk_count": chunk_count,
                            "first_activity_received": first_chunk_ms is not None,
                        })
                    except Exception:
                        # Observability must never replace a healthy model response.
                        pass
                    last_activity_callback_at = now
                provider_request_id = _text_or_none(getattr(chunk, "id", None)) or provider_request_id
                chunk_usage, chunk_usage_status = _extract_usage(chunk)
                if chunk_usage_status == "available":
                    usage, usage_status = chunk_usage, chunk_usage_status
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = _text_or_none(getattr(choice, "finish_reason", None)) or finish_reason
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None)
                if isinstance(content, str):
                    visible_parts.append(content)
                elif isinstance(content, list):
                    visible_parts.extend(_content_parts(content))
                reasoning = getattr(delta, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning.strip():
                    reasoning_chunk_count += 1
                if _is_complete_json_object("".join(visible_parts)):
                    complete_visible_json_at_ms = elapsed_ms
                    close = getattr(stream_box.get("stream"), "close", None)
                    if callable(close):
                        close()
                    finish_reason = finish_reason or "complete_visible_json"
                    break
        except HermesModelProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary must normalize failures.
            raise HermesModelProviderError(_scrub_secret(str(exc), self.config.api_key)) from exc

        output_text = "".join(visible_parts)
        metadata = {
            "external_io": True,
            "api_mode": "chat_completions",
            "response_delivery": "stream",
            "billing_mode": self.billing_mode,
            "usage_status": usage_status,
            "cost_status": "not_reported",
            "provider_request_id_status": "available" if provider_request_id else "not_available",
            "finish_reason": finish_reason or "not_available",
            "visible_output_status": "available" if output_text.strip() else "empty",
            "first_chunk_ms": first_chunk_ms,
            "chunk_count": chunk_count,
            "reasoning_chunk_count": reasoning_chunk_count,
            "complete_visible_json_at_ms": complete_visible_json_at_ms,
            "max_retries": self.config.max_retries,
            "retry_count": 0,
            "total_duration_limit_seconds": None,
            "first_activity_timeout_seconds": first_activity_timeout,
            "stalled_activity_timeout_seconds": stalled_activity_timeout,
            "tools_enabled": False,
            "memory_enabled": False,
            "messaging_enabled": False,
            "nested_job_orchestration_enabled": False,
            "file_or_terminal_side_effects_enabled": False,
        }
        if not output_text.strip():
            raise HermesModelProviderError(
                "provider returned empty visible content",
                diagnostics={
                    "provider_response_kind": "empty_visible_content",
                    "response_delivery": "stream",
                    "finish_reason": metadata["finish_reason"],
                    "chunk_count": chunk_count,
                    "reasoning_chunk_count": reasoning_chunk_count,
                    "usage_status": usage_status,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
        return ModelProviderResult(
            output_text=output_text,
            usage=usage,
            cost={"status": "not_reported", "billing_mode": self.billing_mode},
            provider_request_id=provider_request_id,
            metadata=metadata,
        )

    def _validate_isolated_route(self, route: ModelRoute) -> None:
        parameters = route.parameters or {}
        disallowed = sorted(set(parameters) & self.disallowed_route_parameter_keys)
        if disallowed:
            raise HermesModelProviderError(f"Hermes inference route enables forbidden parameters: {disallowed}")

    def _make_client(self, *, streaming: bool) -> Any:
        factory = self._client_factory
        if factory is None:
            try:
                from openai import OpenAI  # type: ignore
            except Exception as exc:  # noqa: BLE001 - dependency failure belongs at adapter boundary.
                raise HermesModelProviderError("OpenAI SDK is required for Hermes model provider") from exc
            factory = OpenAI
        transport_timeout: float | None
        if streaming:
            transport_timeout = max(
                float(self.config.first_activity_timeout_seconds or 0),
                float(self.config.stalled_activity_timeout_seconds or 0),
                min(float(self.config.timeout_seconds or 0), 120.0),
            )
        else:
            # A non-streaming atomic skill has no provider progress signal.
            # Do not turn silence into a synthetic failure deadline; its state
            # remains awaiting the final provider response until a real terminal
            # event (response, error, worker exit, or explicit cancellation).
            transport_timeout = None
        try:
            return factory(
                api_key=self.config.api_key,
                base_url=self.config.base_url.rstrip("/"),
                timeout=transport_timeout,
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
        return "".join(_content_parts(content))
    return ""


def _content_parts(content: list[Any]) -> list[str]:
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, str):
            parts.append(item)
    return parts


def _is_complete_json_object(value: str) -> bool:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip() if len(lines) >= 2 else ""
    if not text:
        return False
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


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


def _message_field_status(response: Any, field: str) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "not_available"
    value = getattr(getattr(choices[0], "message", None), field, None)
    if value is None:
        return "absent"
    if isinstance(value, str):
        return "available" if value.strip() else "empty"
    return "available"


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
