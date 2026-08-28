"""Minimal stdio MCP server for the Stage 2B-1 external task boundary.

The server is deliberately an adapter: it exposes fixed Core operations and
does not expose SQL, a task queue, a second lifecycle, or model selection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1b_daily_discovery import (
    Stage1BDailyDiscoveryService,
)


SERVER_NAME = "creation-assistant"
SERVER_VERSION = "stage2b-1"
MCP_PROTOCOL_VERSION = "2024-11-05"


class CreationAssistantMcpError(RuntimeError):
    """An expected MCP operation error returned as a tool error."""


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CreationAssistantMcpError(f"{name} is required")
    return value.strip()


def _required_object(arguments: dict[str, Any], name: str) -> dict[str, Any]:
    value = arguments.get(name)
    if not isinstance(value, dict):
        raise CreationAssistantMcpError(f"{name} must be an object")
    return value


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CreationAssistantMcpApplication:
    """Map the small MCP surface to the existing Core/service boundary."""

    def __init__(self, core: Stage0ContentProductionCore) -> None:
        self.core = core
        self.discovery = Stage1BDailyDiscoveryService(core=core, gateway=None)

    def close(self) -> None:
        self.core.close()

    def status(self) -> dict[str, Any]:
        """Return stable facts about this Core connection and boundary."""
        return {
            "project": "Creation Assistant",
            "data_identity": self.core.data_identity,
            "business_state_owner": "Creation Assistant Core",
            "database_path": str(self.core.db_path),
            "mcp_state": "none",
            "mcp_database": None,
            "external_task_boundary": {
                "task_types": ["source_to_topic"],
                "model_selection": "external_client",
                "provider_selection": "external_client",
                "formal_skill_source": "Creation Assistant runtime skill",
            },
        }

    def get_external_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = self.discovery.prepare_source_to_topic_external_task(
            run_id=_required_text(arguments, "run_id"),
            source_version_id=_required_text(arguments, "source_version_id"),
            assembly_id=_required_text(arguments, "assembly_id"),
        )
        return {"status": "ready", "task": task}

    def submit_external_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        run_id = _required_text(arguments, "run_id")
        source_version_id = _required_text(arguments, "source_version_id")
        assembly_id = _required_text(arguments, "assembly_id")
        receipt, output_payload = self.discovery.submit_source_to_topic_external_result(
            run_id=run_id,
            source_version_id=source_version_id,
            assembly_id=assembly_id,
            execution_id=_required_text(arguments, "execution_id"),
            executor_id=_required_text(arguments, "executor_id"),
            model_ref=(
                str(arguments.get("model_ref") or "").strip() or None
            ),
            submitted_at=(
                str(arguments.get("submitted_at") or "").strip() or None
            ),
            output=_required_object(arguments, "output"),
        )
        return {
            "status": "accepted",
            "task_type": "source_to_topic",
            "model_run_id": receipt.model_run_id,
            "validated_output": output_payload,
            "business_state_changed_by": "Creation Assistant Core",
        }

    def get_external_result(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.core.get_discovery_external_execution_result(
            run_id=_required_text(arguments, "run_id"),
            source_version_id=_required_text(arguments, "source_version_id"),
            assembly_id=_required_text(arguments, "assembly_id"),
        )

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        arguments = arguments or {}
        if name == "creation_assistant_status":
            return self.status()
        if name == "creation_assistant_get_external_task":
            return self.get_external_task(arguments)
        if name == "creation_assistant_submit_external_result":
            return self.submit_external_result(arguments)
        if name == "creation_assistant_get_external_result":
            return self.get_external_result(arguments)
        raise CreationAssistantMcpError(f"unknown tool: {name}")


def _identifier_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "source_version_id": {"type": "string"},
            "assembly_id": {"type": "string"},
        },
        "required": ["run_id", "source_version_id", "assembly_id"],
        "additionalProperties": False,
    }


def tool_definitions() -> list[dict[str, Any]]:
    identifiers = _identifier_schema()
    return [
        {
            "name": "creation_assistant_status",
            "description": "Read basic facts from the connected Creation Assistant Core.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "creation_assistant_get_external_task",
            "description": "Get one Core-prepared source_to_topic task, including its formal Skill and minimal material.",
            "inputSchema": identifiers,
        },
        {
            "name": "creation_assistant_submit_external_result",
            "description": "Submit structured source_to_topic fields for Core validation and acceptance.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **identifiers["properties"],
                    "execution_id": {"type": "string"},
                    "executor_id": {"type": "string"},
                    "model_ref": {"type": "string"},
                    "submitted_at": {"type": "string"},
                    "output": {"type": "object"},
                },
                "required": [
                    "run_id", "source_version_id", "assembly_id",
                    "execution_id", "executor_id", "output",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "creation_assistant_get_external_result",
            "description": "Read the persisted Core processing result for one external execution.",
            "inputSchema": identifiers,
        },
    ]


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_message(app: CreationAssistantMcpApplication, message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message without writing anything to stdout except JSON-RPC."""
    method = message.get("method")
    request_id = message.get("id")
    if not isinstance(method, str):
        return _error(request_id, -32600, "invalid JSON-RPC request")
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        protocol_version = requested if isinstance(requested, str) else MCP_PROTOCOL_VERSION
        return _success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _success(request_id, {})
    if method == "tools/list":
        return _success(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        try:
            result = app.call_tool(params["name"], params.get("arguments"))
        except Exception as exc:
            return _success(
                request_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text({"error": str(exc)})}],
                },
            )
        return _success(
            request_id,
            {
                "isError": False,
                "content": [{"type": "text", "text": _json_text(result)}],
                "structuredContent": result,
            },
        )
    return _error(request_id, -32601, f"method not found: {method}")


def serve_stdio(app: CreationAssistantMcpApplication) -> int:
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = handle_message(app, message)
        except Exception as exc:
            response = _error(None, -32700, str(exc))
        if response is not None:
            sys.stdout.write(_json_text(response) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Creation Assistant MCP server")
    parser.add_argument("--db-path", required=True, type=Path)
    parser.add_argument("--data-identity", required=True, choices=("test", "production"))
    args = parser.parse_args(argv)
    core = Stage0ContentProductionCore.open(args.db_path, data_identity=args.data_identity)
    app = CreationAssistantMcpApplication(core)
    try:
        return serve_stdio(app)
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
