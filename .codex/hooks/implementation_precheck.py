#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import (  # noqa: E402
    current_stage,
    is_implementation_prompt,
    load_state,
    now,
    prompt_text,
    read_input,
    record_event,
    root,
    run_stage_guard,
    save_state,
    session_key,
    workspace_fingerprint,
    write_json,
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        key = session_key(data)
        text = prompt_text(data)
        previous = load_state(project, key)
        continuation = (
            text.casefold() in {"继续", "继续做", "接着做", "继续完成", "做完"}
            or text.startswith("当前阶段完成检查失败：")
            or text.startswith("本次写入使当前阶段不一致：")
        )
        active = is_implementation_prompt(text) or bool(
            continuation and previous and previous.get("implementation_active")
        )
        stage = current_stage(project)
        state = {
            "implementation_active": active,
            "stage": stage,
            "recorded_at": now(),
            "before_fingerprint": workspace_fingerprint(project),
            "touched_paths": [],
        }
        save_state(project, key, state)
        if not active:
            record_event(project, "UserPromptSubmit", session_key=key, active=False, stage=stage, outcome="bypassed")
            write_json({"continue": True, "systemMessage": "普通讨论：实施护栏未启用。"})
            return 0
        guard = run_stage_guard(project, stage=stage, phase="pre")
        if guard.returncode:
            record_event(project, "UserPromptSubmit", session_key=key, active=True, stage=stage, outcome="blocked")
            write_json({"continue": False, "stopReason": f"当前实施阶段检查失败：{guard.stdout or guard.stderr}"})
            return 0
        record_event(project, "UserPromptSubmit", session_key=key, active=True, stage=stage, outcome="passed")
        write_json({"continue": True, "systemMessage": f"实施护栏已启用，当前阶段：{stage}。"})
        return 0
    except Exception as exc:
        record_event(project, "UserPromptSubmit", active=True, outcome="error", error=str(exc))
        write_json({"continue": False, "stopReason": f"实施前钩子失败：{exc}"})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
