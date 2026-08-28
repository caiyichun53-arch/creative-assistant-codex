"""WSL entry for one Hermes-native Feishu send or non-sending readiness check."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping
import json
import sys
from typing import Any, Awaitable


RuntimeLoader = Callable[[], tuple[Any, Callable[..., Awaitable[Any]]]]


def _request() -> dict[str, Any]:
    raw_value = getattr(sys.stdin, "buffer", sys.stdin).read()
    if isinstance(raw_value, bytes):
        raw = raw_value.decode("utf-8-sig")
    else:
        raw = str(raw_value)
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Hermes native Feishu request must be one JSON object")
    return payload


def _load_existing_feishu_runtime() -> tuple[Any, Callable[..., Awaitable[Any]]]:
    """Load only the creator profile's existing Hermes platform runtime."""
    from gateway.config import Platform, load_gateway_config
    from gateway.platform_registry import platform_registry
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli.plugins import discover_plugins
    from tools.send_message_tool import _registry_standalone_send

    # This one-shot process does not pass through Hermes CLI startup. Ask
    # Hermes itself to load the active HERMES_HOME profile before resolving
    # its existing PlatformConfig; Creation Assistant never opens that file.
    load_hermes_dotenv()
    config = load_gateway_config()
    platform = Platform("feishu")
    pconfig = config.platforms.get(platform)
    if pconfig is None or not bool(getattr(pconfig, "enabled", False)):
        raise RuntimeError(
            "Hermes creator Feishu platform is not configured and enabled"
        )

    discover_plugins()
    plugin = platform_registry.get("feishu")
    if plugin is None:
        raise RuntimeError("Hermes Feishu plugin is not registered")
    if not callable(getattr(plugin, "standalone_sender_fn", None)):
        raise RuntimeError("Hermes Feishu plugin has no standalone sender")
    if not callable(_registry_standalone_send):
        raise RuntimeError("Hermes send_message registry dispatcher is unavailable")
    return pconfig, _registry_standalone_send


def check_native_feishu_outbound(
    *, runtime_loader: RuntimeLoader | None = None
) -> dict[str, Any]:
    """Check existing config, plugin, and sender without delivering a message."""
    loader = runtime_loader or _load_existing_feishu_runtime
    loader()
    return {
        "ok": True,
        "status": "ready",
        "platform": "feishu",
        "configured": True,
        "plugin_registered": True,
        "standalone_sender": True,
        "sent": False,
    }


def send_native_feishu_outbound(
    payload: Mapping[str, Any],
    *,
    runtime_loader: RuntimeLoader | None = None,
) -> dict[str, Any]:
    """Call Hermes' existing registry dispatcher for one dynamic target."""
    if not isinstance(payload, Mapping):
        raise ValueError("Hermes native Feishu request must be one JSON object")
    chat_id = str(payload.get("chat_id") or "").strip()
    message = str(payload.get("message") or "")
    thread_id = str(payload.get("thread_id") or "").strip() or None
    if not chat_id:
        raise ValueError("Hermes native Feishu request requires chat_id")
    if not message.strip():
        raise ValueError("Hermes native Feishu request requires a non-empty message")

    loader = runtime_loader or _load_existing_feishu_runtime
    pconfig, registry_sender = loader()
    result = asyncio.run(
        registry_sender("feishu", pconfig, chat_id, message, thread_id)
    )
    if isinstance(result, Mapping) and result.get("error"):
        raise RuntimeError(str(result.get("error")))
    if isinstance(result, Mapping) and result.get("success") is False:
        raise RuntimeError("Hermes native Feishu sender reported failure")
    return {
        "ok": True,
        "status": "sent",
        "platform": "feishu",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "result": result,
    }


def _print_json(value: Mapping[str, Any]) -> None:
    serialized = json.dumps(dict(value), ensure_ascii=False, default=str) + "\n"
    stdout = getattr(sys.stdout, "buffer", None)
    if stdout is not None:
        stdout.write(serialized.encode("utf-8"))
        stdout.flush()
    else:
        sys.stdout.write(serialized)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing Hermes Feishu config/plugin/sender without sending",
    )
    args = parser.parse_args(argv)
    try:
        payload = _request()
        result = (
            check_native_feishu_outbound()
            if args.check
            else send_native_feishu_outbound(payload)
        )
    except Exception as exc:  # noqa: BLE001 - one-shot bridge returns one JSON failure.
        _print_json({"ok": False, "status": "failed", "error": str(exc)})
        return 1
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
