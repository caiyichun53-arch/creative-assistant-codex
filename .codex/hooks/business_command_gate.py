#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_common import read_input, record_event, root, tool_text, write_json  # noqa: E402


# These are implementation-only names.  They must never appear in a terminal
# command: model execution belongs to the connected Agent. The project
# provides tasks and receives results through MCP, including during tests.
DIRECT_MODEL_MARKERS = (
    "FormalBusinessSkillAdapter",
    "ModelGateway",
    "ModelRequest(",
    "HermesModelProviderAdapter",
    "process_prepared_breakdown",
    "build_configured_competitor_registration_executor",
)

# Directly launching the underlying business service remains blocked.
# The connected Agent uses the registered Core/MCP entry.
DIRECT_SERVICE_RUN_PATTERNS = (
    "python scripts/agent_platform/cold_start_config_server.py",
    "python scripts\\agent_platform\\cold_start_config_server.py",
    "python -m scripts.agent_platform.cold_start_config_server",
    "python -m scripts\\agent_platform\\cold_start_config_server",
)

# A formal database write is valid only when it is performed by the registered
# Core business route. Block raw SQLite writes and known one-off script
# shapes before they can reach the database.
FORMAL_DATABASE_REFERENCES = (
    "production_activation.sqlite3",
    "creation_assistant-runtime\\formal",
    "creation_assistant-runtime/formal",
    "/mnt/i/creation_assistant-runtime/formal",
    "scripts/tools/create_xuhuaiyu_task.py",
    "scripts\\tools\\create_xuhuaiyu_task.py",
)
FORMAL_DATABASE_WRITE_MARKERS = (
    "sqlite3.connect",
    "insert into stage0_",
    "update stage0_",
    "delete from stage0_",
    "insert into stage1",
    "update stage1",
    "delete from stage1",
)


def main() -> int:
    project = root()
    try:
        data = read_input()
        command = tool_text(data)
        matched = [marker for marker in DIRECT_MODEL_MARKERS if marker in command]
        matched.extend(
            marker for marker in DIRECT_SERVICE_RUN_PATTERNS
            if marker in command.lower()
        )
        lowered = command.lower()
        if any(reference in lowered for reference in FORMAL_DATABASE_REFERENCES) and (
            any(marker in lowered for marker in FORMAL_DATABASE_WRITE_MARKERS)
            or "sqlite3.connect" in lowered
        ):
            matched.append("formal_database_direct_write")
        if not matched:
            record_event(project, "PreToolUse", gate="business_command", outcome="passed")
            return 0
        reason = (
            "Direct model execution or bypassing the Core business entry is blocked. "
            "The connected Agent executes model work and uses the registered "
            "Core/MCP entry for business input and formal results; tests are not an exception."
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
