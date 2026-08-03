#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import (  # noqa: E402
    load_current_state,
    load_state,
    read_input,
    record_event,
    root,
    run_stage_guard,
    session_key,
    workspace_fingerprint,
    write_json,
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        key = session_key(data)
        state = load_current_state(project) or load_state(project, key)
        if not state or not state.get("implementation_active"):
            record_event(project, "Stop", session_key=key, active=False, outcome="bypassed")
            write_json({"continue": True})
            return 0
        stage = str(state["stage"])
        changed = workspace_fingerprint(project) != state.get("before_fingerprint")
        if not changed:
            record_event(project, "Stop", session_key=key, active=True, stage=stage, outcome="passed_no_change")
            write_json({"continue": True})
            return 0
        guard = run_stage_guard(
            project,
            stage=stage,
            phase="post",
            paths=list(state.get("touched_paths", [])),
        )
        if guard.returncode:
            record_event(project, "Stop", session_key=key, active=True, stage=stage, outcome="blocked")
            reason = f"当前阶段完成检查失败：{guard.stdout or guard.stderr}"
            if data.get("stop_hook_active"):
                write_json({"continue": False, "stopReason": reason})
            else:
                write_json({"decision": "block", "reason": reason})
            return 0
        record_event(project, "Stop", session_key=key, active=True, stage=stage, outcome="passed")
        write_json({"continue": True, "systemMessage": f"{stage} 阶段完成检查通过。"})
        return 0
    except Exception as exc:
        record_event(project, "Stop", active=True, outcome="error", error=str(exc))
        write_json({"decision": "block", "reason": f"实施后钩子失败：{exc}"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
