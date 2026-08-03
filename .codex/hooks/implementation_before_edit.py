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
    session_key,
    stages_for_path,
    tool_text,
    write_json,
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        key = session_key(data)
        state = load_current_state(project) or load_state(project, key)
        if not state or not state.get("implementation_active"):
            record_event(project, "PreToolUse", session_key=key, active=False, outcome="bypassed")
            return 0
        stage = str(state["stage"])
        paths = affected_paths(data)
        patch_text = tool_text(data)
        if stage == "system_governance" and "ModelRequest(" in patch_text:
            forbidden_additions = [
                line for line in patch_text.splitlines()
                if line.startswith("+") and not line.startswith("+++") and "ModelRequest(" in line
            ]
            if forbidden_additions and any(
                "scripts/core/production/" in path.replace("\\", "/")
                for path in paths
            ):
                record_event(project, "PreToolUse", session_key=key, active=True, stage=stage, outcome="blocked", paths=paths, reason="direct_business_model_request")
                write_json({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "业务模型判断必须登记为原子 Skill；不得在业务流程中新增直连模型调用。",
                    }
                })
                return 0
        ownership = {path: stages_for_path(project, path) for path in paths}
        foreign = {
            path: sorted(mapped)
            for path, mapped in ownership.items()
            if stage not in mapped
        }
        if foreign:
            record_event(project, "PreToolUse", session_key=key, active=True, stage=stage, outcome="blocked", paths=paths, foreign=foreign)
            write_json({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"本次只能修改 {stage} 阶段，发现其他阶段文件：{foreign}",
                }
            })
            return 0
        guard = run_stage_guard(project, stage=stage, phase="before_write", paths=paths)
        if guard.returncode:
            record_event(project, "PreToolUse", session_key=key, active=True, stage=stage, outcome="blocked", paths=paths)
            write_json({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": guard.stdout or guard.stderr,
                }
            })
            return 0
        record_event(project, "PreToolUse", session_key=key, active=True, stage=stage, outcome="passed", paths=paths)
        return 0
    except Exception as exc:
        record_event(project, "PreToolUse", active=True, outcome="error", error=str(exc))
        write_json({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"写入前钩子失败：{exc}",
            }
        })
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
