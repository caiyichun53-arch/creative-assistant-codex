#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import (  # noqa: E402
    affected_paths,
    load_current_state,
    load_state,
    read_input,
    record_event,
    root,
    run_stage_guard,
    save_state,
    session_key,
    write_json,
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        key = session_key(data)
        state = load_current_state(project) or load_state(project, key)
        if not state or not state.get("implementation_active"):
            record_event(project, "PostToolUse", session_key=key, active=False, outcome="bypassed")
            return 0
        stage = str(state["stage"])
        paths = affected_paths(data)
        guard = run_stage_guard(project, stage=stage, phase="after_write", paths=paths)
        touched = sorted(set(state.get("touched_paths", [])) | set(paths))
        state["touched_paths"] = touched
        save_state(project, key, state)
        if guard.returncode:
            record_event(project, "PostToolUse", session_key=key, active=True, stage=stage, outcome="blocked", paths=paths)
            write_json({"continue": False, "stopReason": f"本次写入使当前阶段不一致：{guard.stdout or guard.stderr}"})
            return 0
        record_event(project, "PostToolUse", session_key=key, active=True, stage=stage, outcome="passed", paths=paths)
        return 0
    except Exception as exc:
        record_event(project, "PostToolUse", active=True, outcome="error", error=str(exc))
        write_json({"continue": False, "stopReason": f"写入后钩子失败：{exc}"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
