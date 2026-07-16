from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IGNORED_PREFIXES = (
    ".codex/state/",
    "artifacts/validation/",
    ".stage_runtime/",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root() -> Path:
    env_root = os.environ.get("CODEX_PROJECT_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def read_stdin_json() -> dict[str, Any]:
    try:
        raw = os.sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid hook JSON input: {exc}") from exc


def hook_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def hook_system_message(message: str) -> None:
    hook_json({"continue": True, "systemMessage": message})


def hook_stop_continue(reason: str) -> None:
    hook_json({"decision": "block", "reason": reason})


def hook_stop_final(reason: str) -> None:
    hook_json({"continue": False, "stopReason": reason})


def state_dir(root: Path) -> Path:
    path = root / "artifacts" / "validation" / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def validation_dir(root: Path) -> Path:
    path = root / "artifacts" / "validation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_stage_id(root: Path) -> str:
    runtime_stage = root / ".stage_runtime" / "current_stage.yaml"
    if runtime_stage.exists():
        text = runtime_stage.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            if line.startswith("stage_id:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    legacy_stage = root / "execution" / "current_stage.yaml"
    if legacy_stage.exists():
        text = legacy_stage.read_text(encoding="utf-8-sig", errors="replace")
        for key in ("stage_id:", "stage:"):
            for line in text.splitlines():
                if line.startswith(key):
                    return line.split(":", 1)[1].strip().strip("'\"")
    return "unknown"


def modified_files(root: Path) -> list[str]:
    proc = run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git status failed")
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.replace("\\", "/")
        if any(normalized.startswith(prefix) for prefix in IGNORED_PREFIXES):
            continue
        paths.append(normalized)
    return sorted(set(paths))


def workspace_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    files = modified_files(root)
    h.update(json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    diff = run_git(root, ["diff", "--binary", "--"])
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or "git diff failed")
    h.update(diff.stdout.encode("utf-8", errors="replace"))
    for path in files:
        full = root / path
        if full.exists() and full.is_file():
            h.update(path.encode("utf-8"))
            h.update(full.read_bytes())
    return h.hexdigest()


def turn_id_from_input(data: dict[str, Any]) -> str:
    for key in ("turn_id", "turnId", "id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    seed = json.dumps(data, ensure_ascii=False, sort_keys=True) + utc_now()
    return "turn-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def load_turn_state(root: Path) -> dict[str, Any] | None:
    path = state_dir(root) / "current_turn.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def save_turn_state(root: Path, state: dict[str, Any]) -> Path:
    path = state_dir(root) / "current_turn.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_evidence(root: Path) -> dict[str, Any] | None:
    files = sorted(validation_dir(root).glob("acceptance-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data["_path"] = rel(root, path)
            return data
    return None
