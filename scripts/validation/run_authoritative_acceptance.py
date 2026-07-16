#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
HOOK_DIR = ROOT / ".codex" / "hooks"
sys.path.insert(0, str(HOOK_DIR))
from hook_common import current_stage_id, latest_evidence, load_turn_state, validation_dir, workspace_fingerprint


def load_current_contract() -> dict[str, Any]:
    path = ROOT / ".stage_runtime" / "current_stage.yaml"
    if not path.exists():
        raise RuntimeError("missing .stage_runtime/current_stage.yaml; run stage_control.py prepare first")
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(".stage_runtime/current_stage.yaml is not a mapping")
    return data


def authoritative_command(contract: dict[str, Any]) -> list[str]:
    workflow = contract.get("real_workflow")
    if not isinstance(workflow, dict):
        raise RuntimeError("current Stage contract lacks real_workflow")
    command = workflow.get("entry_command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise RuntimeError("current Stage contract lacks a valid real_workflow.entry_command")
    return command


def write_evidence(payload: dict[str, Any]) -> Path:
    path = validation_dir(ROOT) / f"acceptance-{int(time.time())}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and record the current authoritative real acceptance command.")
    parser.add_argument("--allow-authorized-stage", action="store_true", help="Run only when the selected Stage is already authorized.")
    args = parser.parse_args()

    started = time.time()
    before_fp = workspace_fingerprint(ROOT)
    turn = load_turn_state(ROOT) or {}
    contract = load_current_contract()
    command = authoritative_command(contract)
    stage_id = current_stage_id(ROOT)
    blocked = contract.get("authorization_required") and contract.get("authorization_status") != "authorized"
    if blocked or not args.allow_authorized_stage:
        evidence = {
            "valid": False,
            "blocked": True,
            "reason": "selected Stage is not authorized for real workflow execution" if blocked else "explicit run flag not provided",
            "turn_id": turn.get("turn_id"),
            "stage_id": stage_id,
            "command": command,
            "exit_code": 12,
            "started_at": started,
            "ended_at": time.time(),
            "before_fingerprint": before_fp,
            "after_fingerprint": workspace_fingerprint(ROOT),
            "mock_or_fallback_detected": False,
        }
        path = write_evidence(evidence)
        print(json.dumps({"status": "BLOCKED", "evidence": str(path), "reason": evidence["reason"]}, ensure_ascii=False, indent=2))
        return 12

    proc = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)
    stdout = proc.stdout
    stderr = proc.stderr
    lower = (stdout + "\n" + stderr + "\n" + " ".join(command)).lower()
    mock_or_fallback = any(token in lower for token in ("mock", "fixture-only", "fake provider", "fallback"))
    evidence = {
        "valid": proc.returncode == 0 and not mock_or_fallback,
        "turn_id": turn.get("turn_id"),
        "stage_id": stage_id,
        "command": command,
        "exit_code": proc.returncode,
        "started_at": started,
        "ended_at": time.time(),
        "before_fingerprint": before_fp,
        "after_fingerprint": workspace_fingerprint(ROOT),
        "stdout_summary": stdout[-4000:],
        "stderr_summary": stderr[-4000:],
        "mock_or_fallback_detected": mock_or_fallback,
    }
    path = write_evidence(evidence)
    print(json.dumps({"status": "PASS" if evidence["valid"] else "FAIL", "evidence": str(path), "exit_code": proc.returncode}, ensure_ascii=False, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
