from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMPLEMENTATION_ACTIONS = (
    "修复", "修改", "实施", "落地", "接通", "补齐", "写完", "执行",
    "构建", "升级", "开始做", "继续做", "改代码", "启动superpower",
)
IMPLEMENTATION_OBJECTS = (
    "代码", "系统", "页面", "功能", "hook", "钩子", "护栏", "链路",
    "实现", "项目", "服务", "数据库", "接口", "运行",
)
STRONG_IMPLEMENTATION_PHRASES = (
    "修复",
    "允许继续修改",
    "继续修改",
    "继续修复",
    "完成修复",
    "开始打通",
    "继续补",
    "继续做完",
    "执行实施",
)


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


def prompt_text(data: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "message", "input"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def is_implementation_prompt(text: str) -> bool:
    folded = text.casefold()
    return (
        any(value.casefold() in folded for value in STRONG_IMPLEMENTATION_PHRASES)
        or (
            any(value.casefold() in folded for value in IMPLEMENTATION_ACTIONS)
            and any(value.casefold() in folded for value in IMPLEMENTATION_OBJECTS)
        )
    )


def registry(project: Path) -> dict[str, Any]:
    path = project / "config" / "business_guardrails" / "stage_registry.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("implementation stage registry must be one object")
    return value


def current_pointer(project: Path, values: dict[str, Any]) -> dict[str, Any]:
    path = project / str(values["pointer"])
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value.get("stage"):
        raise RuntimeError("current implementation stage pointer is invalid")
    return value


def current_stage(project: Path) -> str:
    values = registry(project)
    return str(current_pointer(project, values)["stage"])


def stages_for_path(project: Path, relative_path: str) -> set[str]:
    raw_path = relative_path.strip().strip('"')
    candidate = Path(raw_path)
    if candidate.is_absolute():
        try:
            path = candidate.resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            return {"__outside_project__"}
    else:
        path = raw_path.replace("\\", "/").lstrip("./")
    values = registry(project)
    matches: set[str] = set()
    for stage, definition in values.get("stages", {}).items():
        for prefix in definition.get("path_prefixes", []):
            normalized = str(prefix).replace("\\", "/")
            if path == normalized or path.startswith(normalized.rstrip("/") + "/"):
                matches.add(str(stage))
    return matches


def affected_paths(data: dict[str, Any]) -> list[str]:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = data.get("toolInput")
    if not isinstance(tool_input, dict):
        tool_input = {}
    result: set[str] = set()
    for key in ("file_path", "filePath", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            result.add(value.strip())
    patch = tool_text(data)
    if isinstance(patch, str):
        for value in re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
            patch,
            flags=re.MULTILINE,
        ):
            result.add(value.strip())
        # In this Codex surface a hook can receive the JavaScript wrapper
        # around apply_patch instead of the patch body.  Its line breaks are
        # escaped, so accept that representation as well.
        for value in re.findall(
            r"\*\*\* (?:Add|Update|Delete) File: (.+?)(?=\\n|\r?\n|\*\*\* End Patch)",
            patch,
        ):
            result.add(value.replace("\\\\", "\\").strip().strip('"'))
    return sorted(result)


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


def run_stage_guard(
    project: Path,
    *,
    stage: str,
    phase: str,
    paths: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(project / "scripts" / "stage_contract_guard.py"),
        "--stage",
        stage,
        "--phase",
        phase,
    ]
    for path in paths or []:
        command.extend(("--path", path))
    return subprocess.run(
        command,
        cwd=project,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def git(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def workspace_fingerprint(project: Path) -> str:
    status = git(project, ["status", "--porcelain=v1", "--untracked-files=all"])
    diff = git(project, ["diff", "--binary", "--"])
    if status.returncode or diff.returncode:
        raise RuntimeError("cannot fingerprint the implementation workspace")
    digest = hashlib.sha256()
    digest.update(status.stdout.encode("utf-8", errors="replace"))
    digest.update(diff.stdout.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _state_dir(project: Path) -> Path:
    path = project / ".codex" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_key(data: dict[str, Any]) -> str:
    value = str(data.get("session_id") or "").strip()
    if not value:
        return "unknown"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def state_path(project: Path, key: str) -> Path:
    return _state_dir(project) / f"current_implementation_turn_{key}.json"


def event_log_path(project: Path) -> Path:
    return _state_dir(project) / "independent_hook_events.jsonl"


def save_state(project: Path, key: str, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    state_path(project, key).write_text(serialized, encoding="utf-8")
    # Tool hooks do not receive the conversation session id, so they need the
    # latest precheck state as a stable fallback for the same turn.
    (_state_dir(project) / "current_implementation_turn.json").write_text(
        serialized, encoding="utf-8"
    )


def load_state(project: Path, key: str) -> dict[str, Any] | None:
    path = state_path(project, key)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def load_current_state(project: Path) -> dict[str, Any] | None:
    path = _state_dir(project) / "current_implementation_turn.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def record_event(project: Path, event: str, **details: Any) -> None:
    payload = {"at": now(), "event": event, "pid": os.getpid(), **details}
    with event_log_path(project).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
