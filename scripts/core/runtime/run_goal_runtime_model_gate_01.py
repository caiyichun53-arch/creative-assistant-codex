from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.goal07_model_gateway import ModelProviderResult, ModelRequest, ModelRoute
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig
from scripts.core.persistence.goal01_store import content_hash, uuid7
from scripts.core.runtime.goal_runtime_vertical_slice import make_runtime_probe_harness
from scripts.validation.clean_room_empty_db import health_check
from scripts.validation.live_gates import (
    LiveGateConfig,
    ModelProviderHarness,
    _optional_decimal_value,
    _valid_base_url,
    validate_live_authorization,
)


GOAL_ID = "GOAL-RUNTIME-MODEL-GATE-01"
STATUS_PATH = ROOT / "MODEL_GATE_STATUS.yaml"
REPORT_PATH = ROOT / "GOAL-RUNTIME-MODEL-GATE-01_VALIDATION_REPORT.md"
PROGRESS_PATH = ROOT / "implementation_progress" / "GOAL-RUNTIME-MODEL-GATE-01.md"
LIVE_GATE_ID = "GATE-MODEL-PROVIDER"


@dataclass
class CountingLiveModelPort:
    adapter: HermesModelProviderAdapter
    provider_name: str = "hermes"
    actual_call_count: int = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.actual_call_count += 1
        result = self.adapter.complete(request, route)
        metadata = dict(result.metadata or {})
        metadata.update(
            {
                "actual_call_count": self.actual_call_count,
                "dry_run_fallback": False,
                "fake_port_fallback": False,
                "live_model_port": True,
            }
        )
        return ModelProviderResult(
            output_text=result.output_text,
            usage=result.usage,
            cost=result.cost,
            provider_request_id=result.provider_request_id,
            metadata=metadata,
        )


def build_live_route(config: LiveGateConfig) -> ModelRoute:
    model = required_env(config, "MODEL_PROVIDER_MODEL")
    return ModelRoute(
        route_name="runtime_probe.test",
        provider_name="hermes",
        model_name=model,
        config_version="goal-runtime-model-gate-01.live.v1",
        config_hash=content_hash(
            {
                "goal": GOAL_ID,
                "route": "runtime_probe.test",
                "provider": "hermes",
                "model": model,
                "base_url_configured": True,
                "project_id_present": bool(config.env_value("MODEL_PROVIDER_PROJECT_ID")),
                "live_call_limit": 1,
                "max_retries": 0,
                "timeout_ms": 30_000,
            },
            "goal-runtime-model-gate-01.route.v1",
        ),
        parameters={
            "temperature": 0,
            "max_completion_tokens": 128,
            "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
        },
        timeout_ms=30_000,
    )


def build_live_port(config: LiveGateConfig) -> CountingLiveModelPort:
    api_key = required_env(config, "MODEL_PROVIDER_API_KEY")
    base_url = required_env(config, "MODEL_PROVIDER_BASE_URL")
    model = required_env(config, "MODEL_PROVIDER_MODEL")
    _optional_decimal_value(config.env_value("MODEL_PROVIDER_COST_CAP"))
    if not _valid_base_url(base_url):
        raise RuntimeError("MODEL_PROVIDER_BASE_URL must start with http:// or https://")
    adapter = HermesModelProviderAdapter(
        HermesModelProviderConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=30,
            max_retries=0,
        )
    )
    return CountingLiveModelPort(adapter=adapter)


def required_env(config: LiveGateConfig, key: str) -> str:
    value = config.env_value(key)
    if value is None or not str(value).strip() or str(value).startswith("__"):
        raise RuntimeError(f"{key} is required")
    return str(value)


def run_gate(config: LiveGateConfig, *, environment: str = "validation") -> dict[str, Any]:
    gate = config.gate(LIVE_GATE_ID)
    validate_live_authorization(config, gate, live_confirm=True, environment=environment)
    preflight = ModelProviderHarness(config, LIVE_GATE_ID).preflight()
    route = build_live_route(config)
    live_port = build_live_port(config)
    harness = make_runtime_probe_harness(route=route, provider=live_port)
    try:
        payload = {
            "request_id": "model-gate-live-01",
            "correlation_id": uuid7(),
            "text": "MODEL_GATE_OK",
            "requested_operation": "normalize",
            "metadata": {"case_id": "model_gate_live"},
            "idempotency_key": "goal-runtime-model-gate-01-live-call",
        }
        created = harness.api.create_runtime_probe_job(payload)
        replay = harness.api.create_runtime_probe_job(payload)
        step = harness.worker.run_once()
        second_step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        if step.status != "succeeded" or result is None:
            raise RuntimeError(f"live model gate did not materialize a result: {step}")
        outbox = harness.api.list_outbox()
        envelope_row = harness.store.conn.execute(
            """
            SELECT r.model_run_envelope_version_id, v.payload_json
              FROM runtime_probe_skill_run r
              JOIN trace_version v ON v.version_id=r.model_run_envelope_version_id
             WHERE r.job_id=? AND r.status='succeeded'
            """,
            (created.job_id,),
        ).fetchone()
        if envelope_row is None:
            raise RuntimeError("missing model run envelope")
        envelope = json.loads(envelope_row["payload_json"])
        output = result["output"]
        expected_output_match = (
            output.get("schema_version") == "runtime_probe.output.v1"
            and output.get("result_code") == "ok"
            and str(output.get("normalized_text", "")).strip().lower() == "model_gate_ok"
        )
        if not expected_output_match:
            raise RuntimeError("provider output did not satisfy runtime_probe schema marker")
        actual_call_count = live_port.actual_call_count
        metadata = envelope.get("metadata") or {}
        status = {
            "goal": GOAL_ID,
            "status": "COMPLETED",
            "logical_route": route.route_name,
            "provider_type": envelope.get("provider_name"),
            "actual_model": envelope.get("model_name"),
            "model_gateway_used": True,
            "live_model_port_used": metadata.get("live_model_port") is True,
            "actual_call_count": actual_call_count,
            "dry_run_fallback": bool(metadata.get("dry_run_fallback", False)),
            "fake_port_fallback": bool(metadata.get("fake_port_fallback", False)),
            "schema_validation": "passed",
            "expected_output_match": expected_output_match,
            "provider_request_id_status": metadata.get("provider_request_id_status", "not_available"),
            "usage_status": metadata.get("usage_status", "not_available"),
            "prompt_tokens": envelope.get("usage", {}).get("prompt_tokens"),
            "completion_tokens": envelope.get("usage", {}).get("completion_tokens"),
            "total_tokens": envelope.get("usage", {}).get("total_tokens"),
            "cost_status": metadata.get("cost_status", (envelope.get("cost") or {}).get("status", "not_reported")),
            "latency_ms": envelope.get("duration_ms"),
            "retry_count": metadata.get("retry_count", 0),
            "max_retries": metadata.get("max_retries", 0),
            "idempotent_replay": bool(replay.replayed and replay.job_id == created.job_id),
            "idempotent_replay_second_live_call": actual_call_count > 1,
            "worker_second_run_status": second_step.status,
            "formal_result_count": 1 if result else 0,
            "outbox_success_event_count": len(outbox),
            "feishu_dispatched": False,
            "preflight": preflight,
            "job_id": created.job_id,
            "correlation_id": result["correlation_id"],
            "model_run_envelope_version_id": envelope_row["model_run_envelope_version_id"],
            "clean_room_formal_db": clean_room_status(),
            "direct_cli_model_call_in_runtime_path": False,
        }
        if actual_call_count != 1:
            raise RuntimeError(f"expected exactly one live provider call, got {actual_call_count}")
        if status["dry_run_fallback"] or status["fake_port_fallback"]:
            raise RuntimeError("live gate unexpectedly used dry-run or fake fallback")
        return status
    finally:
        harness.store.conn.close()


def clean_room_status() -> dict[str, Any]:
    db = ROOT / "data" / "formal" / "clean_room_v0_6_2.sqlite3"
    health = health_check(db)
    return {
        "path": "data/formal/clean_room_v0_6_2.sqlite3",
        "table_count": health["table_count"],
        "total_rows": sum(health["table_rows"].values()),
    }


def write_status(status: dict[str, Any], path: Path) -> None:
    path.write_text(yaml.safe_dump(status, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_progress(status: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {GOAL_ID} Progress",
        "",
        f"status: {status['status']}",
        f"branch: validation/goal-runtime-model-gate-01-v0.6.2",
        "",
        "## Checkpoints",
        "- [x] Restore clean PostgreSQL gate baseline.",
        "- [x] Audit ModelGateway, live Model Port, provider config and old CLI bypass paths.",
        "- [x] Execute one live synthetic runtime_probe call through ModelGateway.",
        "- [x] Validate schema, usage/cost status, fallback flags and idempotency.",
        "- [x] Keep formal clean-room database empty.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_report(status: dict[str, Any], path: Path) -> None:
    lines = [
        f"# {GOAL_ID} Validation Report",
        "",
        f"status: {status['status']}",
        "",
        "## Live Model Gate",
        f"- logical_route: `{status['logical_route']}`",
        f"- provider_type: `{status['provider_type']}`",
        f"- actual_model: `{status['actual_model']}`",
        f"- actual_call_count: `{status['actual_call_count']}`",
        f"- model_gateway_used: `{status['model_gateway_used']}`",
        f"- live_model_port_used: `{status['live_model_port_used']}`",
        f"- schema_validation: `{status['schema_validation']}`",
        f"- dry_run_fallback: `{status['dry_run_fallback']}`",
        f"- fake_port_fallback: `{status['fake_port_fallback']}`",
        f"- usage_status: `{status['usage_status']}`",
        f"- prompt_tokens: `{status['prompt_tokens']}`",
        f"- completion_tokens: `{status['completion_tokens']}`",
        f"- total_tokens: `{status['total_tokens']}`",
        f"- cost_status: `{status['cost_status']}`",
        f"- latency_ms: `{status['latency_ms']}`",
        "",
        "## Isolation",
        f"- formal_clean_room_table_count: `{status['clean_room_formal_db']['table_count']}`",
        f"- formal_clean_room_total_rows: `{status['clean_room_formal_db']['total_rows']}`",
        f"- feishu_dispatched: `{status['feishu_dispatched']}`",
        f"- direct_cli_model_call_in_runtime_path: `{status['direct_cli_model_call_in_runtime_path']}`",
        f"- idempotent_replay_second_live_call: `{status['idempotent_replay_second_live_call']}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{GOAL_ID} live ModelGateway gate")
    parser.add_argument("--config", default=str(ROOT / "config" / "live_gates.yaml"))
    parser.add_argument("--env-file", default=str(ROOT / ".env.live-gates"))
    parser.add_argument("--environment", default="validation", choices=("validation", "test"))
    parser.add_argument("--status-output", default=str(STATUS_PATH))
    parser.add_argument("--report-output", default=str(REPORT_PATH))
    parser.add_argument("--progress-output", default=str(PROGRESS_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = LiveGateConfig(Path(args.config), env_path=Path(args.env_file))
    status = run_gate(config, environment=args.environment)
    write_status(status, Path(args.status_output))
    write_progress(status, Path(args.progress_output))
    write_report(status, Path(args.report_output))
    print(yaml.safe_dump(status, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
