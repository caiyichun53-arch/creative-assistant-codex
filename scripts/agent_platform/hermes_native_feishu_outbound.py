"""One-shot Windows bridge to Hermes' native Feishu outbound sender.

The Creation Assistant process owns only the dynamic destination and message.
Hermes continues to own Feishu configuration, credentials, plugin discovery,
and delivery.  Each call starts one short-lived process in the current Ubuntu
Hermes environment and exchanges exactly one UTF-8 JSON request over stdin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import subprocess
from typing import Any


HERMES_WSL_DISTRIBUTION = "Ubuntu"
HERMES_AGENT_ROOT = "/home/caibin/.hermes/hermes-agent"
HERMES_PYTHON = f"{HERMES_AGENT_ROOT}/venv/bin/python"
HERMES_HOME = "/home/caibin/.hermes/profiles/creator"


class HermesNativeFeishuOutboundError(RuntimeError):
    """The one-shot Hermes sender could not be started or did not send."""


def _entry_wsl_path() -> str:
    path = str(
        Path(__file__).with_name("hermes_native_feishu_outbound_entry.py").resolve()
    )
    if path.startswith("\\\\?\\"):
        path = path[4:]
    if len(path) >= 3 and path[1] == ":" and path[2] in {"\\", "/"}:
        drive = path[0].lower()
        suffix = path[2:].replace("\\", "/")
        return f"/mnt/{drive}{suffix}"
    if path.startswith("/"):
        return path
    raise HermesNativeFeishuOutboundError(
        "cannot translate the Hermes outbound entry path for WSL"
    )


def default_hermes_native_feishu_runner_command() -> tuple[str, ...]:
    """Return the fixed one-shot command for the current Hermes installation."""
    return (
        "wsl.exe",
        "-d",
        HERMES_WSL_DISTRIBUTION,
        "--",
        "env",
        f"HERMES_HOME={HERMES_HOME}",
        f"PYTHONPATH={HERMES_AGENT_ROOT}",
        HERMES_PYTHON,
        "-u",
        _entry_wsl_path(),
    )


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8-sig", errors="replace")
    return str(value)


def _last_json_result(stdout: bytes | str | None) -> dict[str, Any]:
    text = _text(stdout)
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise HermesNativeFeishuOutboundError(
        "Hermes native Feishu sender did not return a final JSON object"
    )


def _failure_detail(
    result: Mapping[str, Any] | None,
    stderr: bytes | str | None,
) -> str:
    if isinstance(result, Mapping):
        for key in ("error", "message", "detail"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
    stderr_text = _text(stderr).strip()
    if stderr_text:
        return stderr_text.splitlines()[-1].strip()
    return "the one-shot Hermes process failed without an error detail"


def run_hermes_native_feishu_outbound(
    payload: Mapping[str, Any],
    *,
    runner_command: Sequence[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run one isolated Hermes outbound request and return its final JSON result.

    ``runner_command`` exists only so isolation tests can replace ``wsl.exe``
    with a harmless local process.  Production callers should leave it unset.
    A payload containing ``check: true`` invokes the entry's non-sending
    ``--check`` mode.
    """
    if not isinstance(payload, Mapping):
        raise HermesNativeFeishuOutboundError(
            "Hermes native Feishu outbound payload must be one JSON object"
        )
    if timeout_seconds <= 0:
        raise HermesNativeFeishuOutboundError(
            "Hermes native Feishu outbound timeout must be positive"
        )
    command = list(
        runner_command
        if runner_command is not None
        else default_hermes_native_feishu_runner_command()
    )
    if not command:
        raise HermesNativeFeishuOutboundError(
            "Hermes native Feishu outbound runner command is empty"
        )
    if payload.get("check") is True and "--check" not in command:
        command.append("--check")
    try:
        request_bytes = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HermesNativeFeishuOutboundError(
            f"Hermes native Feishu outbound payload is not JSON serializable: {exc}"
        ) from exc

    run_kwargs: dict[str, Any] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            command,
            input=request_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=float(timeout_seconds),
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise HermesNativeFeishuOutboundError(
            f"Hermes native Feishu outbound timed out after {timeout_seconds:g} seconds"
        ) from exc
    except OSError as exc:
        raise HermesNativeFeishuOutboundError(
            f"could not start the one-shot WSL Hermes sender: {exc}"
        ) from exc

    try:
        result = _last_json_result(completed.stdout)
    except HermesNativeFeishuOutboundError:
        if completed.returncode != 0:
            detail = _failure_detail(None, completed.stderr)
            raise HermesNativeFeishuOutboundError(
                f"Hermes native Feishu outbound failed: {detail}"
            ) from None
        raise
    if completed.returncode != 0:
        detail = _failure_detail(result, completed.stderr)
        raise HermesNativeFeishuOutboundError(
            f"Hermes native Feishu outbound failed: {detail}"
        )
    if result.get("ok") is False or result.get("error"):
        detail = _failure_detail(result, completed.stderr)
        raise HermesNativeFeishuOutboundError(
            f"Hermes native Feishu outbound failed: {detail}"
        )
    return result


def send_hermes_native_feishu_message(
    *,
    chat_id: str,
    message: str,
    thread_id: str | None = None,
    runner_command: Sequence[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Send one message to the supplied dynamic Feishu conversation target."""
    target = str(chat_id or "").strip()
    body = str(message or "")
    if not target:
        raise HermesNativeFeishuOutboundError(
            "Hermes native Feishu outbound requires a dynamic chat_id"
        )
    if not body.strip():
        raise HermesNativeFeishuOutboundError(
            "Hermes native Feishu outbound requires a non-empty message"
        )
    thread = str(thread_id or "").strip() or None
    return run_hermes_native_feishu_outbound(
        {
            "chat_id": target,
            "message": body,
            "thread_id": thread,
        },
        runner_command=runner_command,
        timeout_seconds=timeout_seconds,
    )


def check_hermes_native_feishu_outbound(
    *,
    runner_command: Sequence[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Verify native Hermes Feishu config/plugin/sender without sending."""
    return run_hermes_native_feishu_outbound(
        {"check": True},
        runner_command=runner_command,
        timeout_seconds=timeout_seconds,
    )
