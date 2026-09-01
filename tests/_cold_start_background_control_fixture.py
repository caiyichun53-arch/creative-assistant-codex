"""Long-running isolated executor with one child process and optional crash."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts.agent_platform.cold_start_background_control import (
    activate_cold_start_background,
    start_cold_start_heartbeat,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
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

    activate_cold_start_background(
        record_path=arguments.executor_record,
        worker_token=arguments.worker_token,
    )
    start_cold_start_heartbeat(
        record_path=arguments.executor_record,
        worker_token=arguments.worker_token,
    )
    _write_json(
        arguments.startup_receipt,
        {
            "status": "ready",
            "configuration_id": arguments.configuration_id,
            "cold_start_id": arguments.cold_start_id,
            "pid": os.getpid(),
        },
    )
    if arguments.actor == "crash":
        time.sleep(0.2)
        os._exit(7)

    root_marker = arguments.executor_record.parent / f"{arguments.cold_start_id}.root.txt"
    child_marker = arguments.executor_record.parent / f"{arguments.cold_start_id}.child.txt"
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests._cold_start_background_child_fixture",
            "--marker",
            str(child_marker),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    count = 0
    while True:
        count += 1
        root_marker.write_text(str(count), encoding="utf-8")
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
