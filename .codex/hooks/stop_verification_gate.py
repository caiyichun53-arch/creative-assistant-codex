#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import current_stage_id, hook_json, hook_stop_continue, hook_stop_final, hook_system_message, latest_evidence, load_turn_state, modified_files, project_root, read_stdin_json, workspace_fingerprint


def block(message: str, missing: list[str], stop_hook_active: bool) -> int:
    reason = (
        f"{message}; required skill: $project-verification-before-completion; "
        f"missing: {', '.join(missing)}"
    )
    if stop_hook_active:
        hook_stop_final(reason)
    else:
        hook_stop_continue(reason)
    return 0


def main() -> int:
    try:
        data = read_stdin_json()
        root = project_root()
        files = modified_files(root)
        if not files:
            hook_json({"continue": True, "systemMessage": "no controlled workspace changes; completion gate not required"})
            return 0

        state = load_turn_state(root)
        if not state:
            return block("controlled changes exist but no turn baseline was recorded", ["same-turn baseline"], bool(data.get("stop_hook_active")))

        current_fp = workspace_fingerprint(root)
        start_fp = state.get("workspace_fingerprint")
        if current_fp == start_fp:
            hook_json({"continue": True, "systemMessage": "workspace fingerprint matches turn baseline; no new controlled changes"})
            return 0

        stage_id = current_stage_id(root)
        evidence = latest_evidence(root)
        missing: list[str] = []
        if not evidence:
            missing.append("authoritative acceptance evidence")
        else:
            if evidence.get("turn_id") != state.get("turn_id"):
                missing.append("same turn_id evidence")
            if evidence.get("stage_id") != stage_id:
                missing.append("same Stage evidence")
            if evidence.get("after_fingerprint") != current_fp:
                missing.append("evidence matching final workspace fingerprint")
            if evidence.get("exit_code") != 0:
                missing.append("exit_code 0")
            if not evidence.get("valid"):
                missing.append("valid evidence flag")
            if evidence.get("mock_or_fallback_detected"):
                missing.append("no Mock or fallback")
            if not evidence.get("command"):
                missing.append("authoritative real acceptance command")

        if missing:
            command = evidence.get("command") if evidence else "run the current authoritative acceptance command"
            return block(
                "controlled changes lack fresh matching verification evidence; use $project-verification-before-completion and do not use old output or weak tests",
                missing + [f"required command: {command}"],
                bool(data.get("stop_hook_active")),
            )

        hook_json({"continue": True, "systemMessage": f"fresh verification evidence matches final workspace fingerprint: {evidence.get('_path')}"})
        return 0
    except Exception as exc:
        hook_stop_final(f"stop verification hook failed closed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
