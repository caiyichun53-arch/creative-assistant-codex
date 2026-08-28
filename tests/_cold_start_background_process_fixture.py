"""Credential-free child process used only by background-launch isolation tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from scripts.agent_platform.cold_start_background_control import (
    activate_cold_start_background,
    finish_cold_start_background,
    start_cold_start_heartbeat,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration-id", required=True)
    parser.add_argument("--cold-start-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--startup-receipt", type=Path, required=True)
    parser.add_argument("--executor-record", type=Path, required=True)
    parser.add_argument("--worker-token", required=True)
    arguments = parser.parse_args()

    if arguments.actor == "fail-before-ready":
        _write_json(
            arguments.startup_receipt,
            {
                "status": "failed",
                "configuration_id": arguments.configuration_id,
                "cold_start_id": arguments.cold_start_id,
                "reason": "isolated child reported its real startup failure",
                "error_type": "RuntimeError",
            },
        )
        time.sleep(0.4)
        _write_json(
            arguments.startup_receipt.parent
            / f"{arguments.cold_start_id}.cleanup.json",
            {
                "status": "cleanup_completed",
                "cold_start_id": arguments.cold_start_id,
            },
        )
        return 7

    activate_cold_start_background(
        record_path=arguments.executor_record,
        worker_token=arguments.worker_token,
        run_model="isolated/model-a",
    )
    heartbeat_stop, heartbeat_thread = start_cold_start_heartbeat(
        record_path=arguments.executor_record,
        worker_token=arguments.worker_token,
    )
    try:
        _write_json(
            arguments.startup_receipt,
            {
                "status": "ready",
                "configuration_id": arguments.configuration_id,
                "cold_start_id": arguments.cold_start_id,
                "run_model": "isolated/model-a",
            },
        )
        time.sleep(0.8)
        _write_json(
            arguments.startup_receipt.parent / f"{arguments.cold_start_id}.done.json",
            {
                "configuration_id": arguments.configuration_id,
                "cold_start_id": arguments.cold_start_id,
                "actor": arguments.actor,
            },
        )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        finish_cold_start_background(
            record_path=arguments.executor_record,
            worker_token=arguments.worker_token,
            state="completed",
            lifecycle_status="completed",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
