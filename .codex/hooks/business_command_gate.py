#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import read_input, record_event, root, tool_text, write_json  # noqa: E402


# These are implementation-only names.  They must never appear in a terminal
# command: a business model operation has to enter through its registered
# business or test route, where its input, result handling, and retention
# boundary are fixed.
DIRECT_MODEL_MARKERS = (
    "run_test_only_competitor_breakdown_batch",
    "FormalBusinessSkillAdapter",
    "ModelGateway",
    "ModelRequest(",
    "HermesModelProviderAdapter",
    "process_prepared_breakdown",
    "build_configured_competitor_registration_executor",
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        command = tool_text(data)
        matched = [marker for marker in DIRECT_MODEL_MARKERS if marker in command]
        if not matched:
            record_event(project, "PreToolUse", gate="business_command", outcome="passed")
            return 0
        reason = (
            "Direct terminal invocation of a business model operation is blocked. "
            "Use the registered business or test entry so the input, result receipt, "
            "and review path stay fixed."
        )
        record_event(
            project,
            "PreToolUse",
            gate="business_command",
            outcome="blocked",
            matched=matched,
        )
        write_json({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        })
        return 0
    except Exception as exc:
        record_event(project, "PreToolUse", gate="business_command", outcome="error", error=str(exc))
        write_json({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Business command gate failed closed: {exc}",
            }
        })
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
