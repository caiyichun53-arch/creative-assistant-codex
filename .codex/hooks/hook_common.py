from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def root() -> Path:
    return Path(__file__).resolve().parents[2]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # apply_patch sends the patch body directly in this Codex surface.
        # Treat it as tool input instead of misreporting a valid patch as bad JSON.
        return {"tool_input": {"patch": raw}, "_raw_patch_input": True}
    return value if isinstance(value, dict) else {}


def write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def tool_text(data: dict[str, Any]) -> str:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = data.get("toolInput")
    if isinstance(tool_input, dict):
        for key in ("command", "patch", "input"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    raw = data.get("_raw_patch_input")
    return raw if isinstance(raw, str) else ""


def _state_dir(project: Path) -> Path:
    path = project / ".codex" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_log_path(project: Path) -> Path:
    return _state_dir(project) / "independent_hook_events.jsonl"


def record_event(project: Path, event: str, **details: Any) -> None:
    payload = {"at": now(), "event": event, "pid": os.getpid(), **details}
    with event_log_path(project).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
