"""Hermes transport for the formal research Core capability."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.stage0_content_core import FORMAL_DB_PATH, Stage0ContentProductionCore
from scripts.core.production.stage1a_research_plan import build_research_plan_gateway


def _request(value: str | None, encoded_value: str | None = None) -> dict[str, Any]:
    if encoded_value is not None:
        try:
            raw = base64.b64decode(encoded_value, validate=True).decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("formal research base64 request must contain UTF-8 JSON") from exc
    elif value is not None:
        raw = value
    else:
        raw_bytes = getattr(sys.stdin, "buffer", sys.stdin).read()
        raw = raw_bytes.decode("utf-8-sig") if isinstance(raw_bytes, bytes) else raw_bytes
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("formal research request must be one JSON object")
    return payload


def run(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    core = Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="production")
    try:
        gateway = None
        if action in {"create_plan", "retry_plan", "revise_plan"}:
            gateway = build_research_plan_gateway(core)
        return CreationAssistantFormalBusinessCore(core=core).execute_formal_research(
            action=action,
            payload=payload,
            research_gateway=gateway,
        )
    finally:
        core.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "create_plan",
            "view_plan",
            "approve_plan",
            "retry_plan",
            "revise_plan",
            "run_research",
            "approve_research_result",
            "return_research_result",
            "view_task",
            "view_research_materials",
            "view_research_failure",
            "view_accounts",
        ),
    )
    parser.add_argument("--request-json")
    parser.add_argument("--request-base64")
    args = parser.parse_args(argv)
    try:
        if args.request_json is not None and args.request_base64 is not None:
            raise ValueError("provide only one formal research request transport")
        result = run(args.action, _request(args.request_json, args.request_base64))
    except Exception as exc:  # noqa: BLE001 - formal carrier reports one safe failure.
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    stdout = getattr(sys.stdout, "buffer", None)
    if stdout is not None:
        stdout.write(serialized.encode("utf-8"))
        stdout.flush()
    else:
        sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
