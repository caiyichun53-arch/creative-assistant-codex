"""Small, deterministic control-command boundary for cold-start stop."""

from __future__ import annotations

import re
from typing import Any


_SLASH_STOP = re.compile(r"^/cold-start\s+stop$", re.IGNORECASE)


def parse_control_command(text: str) -> str | None:
    """Return ``stop`` only for an exact supported control command."""

    value = str(text or "").strip()
    if value == "停止冷启动" or _SLASH_STOP.fullmatch(value):
        return "stop"
    return None


def resolve_cold_start_id(
    core: Any,
    *,
    operation: str,
    requested_id: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve one exact run without falling back to a latest historical run."""

    requested = str(requested_id or "").strip()
    if requested:
        return requested, None

    operation_name = str(operation or "").strip().lower()
    allowed = {"running"} if operation_name == "stop" else {
        "running", "stopped", "failed", "waiting_human"
    }
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for config in core.list_cold_start_configurations():
        run_id = str(config.get("cold_start_id") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        row = core.conn.execute(
            "SELECT status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (run_id, core.data_identity),
        ).fetchone()
        status = str(row["status"] or "").strip() if row is not None else ""
        if status in allowed:
            candidates.append({"cold_start_id": run_id, "status": status})

    if len(candidates) == 1:
        return candidates[0]["cold_start_id"], None
    if not candidates:
        return None, {
            "status": "no_active_cold_start",
            "reason": "no_current_active_cold_start",
            "message": "当前没有可停止的冷启动。",
        }
    return None, {
        "status": "needs_clarification",
        "reason": "multiple_active_cold_starts",
        "message": "当前有多个正在运行的冷启动，请明确指定 cold_start_id。",
        "candidates": candidates,
    }


def build_internal_stop_context(*, session_ref: str, command_id: str) -> dict[str, str]:
    """Build the fixed gateway context, not a run owner or operator."""

    return {
        "marker": "hermes_gateway_internal_v1",
        "platform": "feishu",
        "profile": "creator",
        "carrier_binding_id": "hermes-creator-feishu-gateway",
        "entry_ref": "hermes://creator/feishu-gateway",
        "tool_action": "cold_start_onboarding",
        "session_identity": str(session_ref or "").strip(),
        "command_identity": str(command_id or "").strip(),
        "user_identity": "creation_assistant_plugin",
    }

