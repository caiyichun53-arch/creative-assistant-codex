#!/usr/bin/env python
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / ".codex" / "hooks"))
from hook_common import current_stage_id, load_turn_state, validation_dir, workspace_fingerprint  # noqa: E402


def run(cmd: list[str], cwd: Path, *, input_data: dict | str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if isinstance(input_data, dict):
        stdin = json.dumps(input_data, ensure_ascii=False)
    else:
        stdin = input_data
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=merged_env,
    )


def init_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="codex-hook-test-"))
    (root / ".codex" / "hooks").mkdir(parents=True)
    (root / ".stage_runtime").mkdir(parents=True)
    shutil.copy2(ROOT / ".codex" / "hooks" / "hook_common.py", root / ".codex" / "hooks" / "hook_common.py")
    shutil.copy2(ROOT / ".codex" / "hooks" / "turn_change_baseline.py", root / ".codex" / "hooks" / "turn_change_baseline.py")
    shutil.copy2(ROOT / ".codex" / "hooks" / "stop_verification_gate.py", root / ".codex" / "hooks" / "stop_verification_gate.py")
    (root / "AGENTS.md").write_text("baseline\n", encoding="utf-8")
    (root / ".stage_runtime" / "current_stage.yaml").write_text("stage_id: stage_test\n", encoding="utf-8")
    run(["git", "init"], root)
    run(["git", "config", "user.email", "test@example.invalid"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "init"], root)
    return root


def hook_env(root: Path) -> dict[str, str]:
    return {"CODEX_PROJECT_ROOT": str(root)}


def hook_json(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


def record_turn(root: Path, turn_id: str = "turn-a") -> subprocess.CompletedProcess[str]:
    return run([PYTHON, str(root / ".codex" / "hooks" / "turn_change_baseline.py")], root, input_data={"turn_id": turn_id}, env=hook_env(root))


def stop(root: Path, *, active: bool = False, input_data: dict | str | None = None) -> subprocess.CompletedProcess[str]:
    data = {"stop_hook_active": active} if input_data is None else input_data
    return run([PYTHON, str(root / ".codex" / "hooks" / "stop_verification_gate.py")], root, input_data=data, env=hook_env(root))


def fingerprint(root: Path) -> str:
    code = "import sys; from pathlib import Path; sys.path.insert(0, str(Path('.codex/hooks').resolve())); import hook_common; print(hook_common.workspace_fingerprint(Path('.').resolve()))"
    proc = run([PYTHON, "-c", code], root, env=hook_env(root))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


def write_evidence(root: Path, *, turn_id: str = "turn-a", stage_id: str = "stage_test", exit_code: int = 0, valid: bool = True, after_fp: str | None = None) -> None:
    (root / "artifacts" / "validation").mkdir(parents=True, exist_ok=True)
    payload = {
        "valid": valid,
        "turn_id": turn_id,
        "stage_id": stage_id,
        "command": ["python", "-c", "print('authoritative')"],
        "exit_code": exit_code,
        "before_fingerprint": "before",
        "after_fingerprint": after_fp or fingerprint(root),
        "mock_or_fallback_detected": False,
        "ended_at": time.time(),
    }
    (root / "artifacts" / "validation" / f"acceptance-{time.time_ns()}.json").write_text(json.dumps(payload), encoding="utf-8")


def assert_case(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed: {detail}")
    print(f"PASS {name}")


def record_acceptance(started_at: float) -> Path:
    state = load_turn_state(ROOT) or {}
    fingerprint = workspace_fingerprint(ROOT)
    payload = {
        "valid": True,
        "turn_id": state.get("turn_id", "manual-record"),
        "stage_id": current_stage_id(ROOT),
        "command": ["python", "scripts/validation/test_superpowers_selective.py", "--record-evidence"],
        "exit_code": 0,
        "started_at": started_at,
        "ended_at": time.time(),
        "before_fingerprint": fingerprint,
        "after_fingerprint": fingerprint,
        "stdout_summary": "test_superpowers_selective passed",
        "stderr_summary": "",
        "mock_or_fallback_detected": False,
    }
    path = validation_dir(ROOT) / f"acceptance-{time.time_ns()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    roots: list[Path] = []
    try:
        # 1. no changes allows completion
        root = init_repo(); roots.append(root)
        proc = record_turn(root)
        assert_case(
            "record baseline creates state",
            proc.returncode == 0 and (root / "artifacts" / "validation" / "state" / "current_turn.json").exists(),
            proc.stdout + proc.stderr,
        )
        proc = stop(root)
        assert_case("no file changes allowed", proc.returncode == 0 and hook_json(proc)["continue"] is True, proc.stdout)

        # 2. controlled change without evidence blocks
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        proc = stop(root)
        assert_case("change without evidence blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block", proc.stdout)

        # 3. read-only discussion over pre-existing changes is not a completion claim.
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        proc = stop(root, input_data={"assistant_response": "read-only research comparison; no code change claim"})
        assert_case("read-only discussion bypasses verification debt", proc.returncode == 0 and hook_json(proc)["continue"] is True, proc.stdout)

        # 4. Tool and transcript noise must not turn a read-only response into a completion claim.
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        proc = stop(
            root,
            input_data={
                "assistant_response": "read-only investigation; no code change claim",
                "tool_result": {"content": "Script completed"},
                "transcript": "previous task completed",
            },
        )
        assert_case("tool or transcript completion noise bypasses verification debt", proc.returncode == 0 and hook_json(proc)["continue"] is True, proc.stdout)

        # 5. completion/fix claims still require evidence.
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        proc = stop(root, input_data={"assistant_response": "fixed hook and updated controlled files"})
        assert_case("completion claim without evidence blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block", proc.stdout)

        # 5. failed evidence blocks
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        write_evidence(root, exit_code=1, valid=False)
        proc = stop(root)
        assert_case("failed evidence blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block" and "exit_code 0" in proc.stdout, proc.stdout)

        # 6. successful matching evidence allows
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        write_evidence(root)
        proc = stop(root)
        assert_case("matching evidence allows", proc.returncode == 0 and hook_json(proc)["continue"] is True, proc.stdout)

        # 7. evidence invalidates after another edit
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        write_evidence(root)
        (root / "AGENTS.md").write_text("changed again\n", encoding="utf-8")
        proc = stop(root)
        assert_case("post-evidence edit blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block" and "fingerprint" in proc.stdout, proc.stdout)

        # 8. old turn evidence blocks
        root = init_repo(); roots.append(root)
        record_turn(root, "turn-current")
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        write_evidence(root, turn_id="turn-old")
        proc = stop(root)
        assert_case("old turn evidence blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block" and "same turn_id" in proc.stdout, proc.stdout)

        # 9. other Stage evidence blocks
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        write_evidence(root, stage_id="stage_other")
        proc = stop(root)
        assert_case("other stage evidence blocks", proc.returncode == 0 and hook_json(proc)["decision"] == "block" and "same Stage" in proc.stdout, proc.stdout)

        # 10. stop_hook_active avoids loop and fails explicitly
        root = init_repo(); roots.append(root)
        record_turn(root)
        (root / "AGENTS.md").write_text("changed\n", encoding="utf-8")
        proc = stop(root, active=True)
        assert_case("stop_hook_active fails closed", proc.returncode == 0 and hook_json(proc)["continue"] is False, proc.stdout)

        # 11. damaged hook input fails closed
        root = init_repo(); roots.append(root)
        proc = stop(root, input_data="{not-json")
        assert_case("damaged hook input fails closed", proc.returncode == 1 and hook_json(proc)["continue"] is False, proc.stdout)

        # 12. state directory is created safely
        root = init_repo(); roots.append(root)
        shutil.rmtree(root / "artifacts" / "validation" / "state", ignore_errors=True)
        proc = record_turn(root, "turn-state")
        assert_case("missing state dir created", proc.returncode == 0 and (root / "artifacts" / "validation" / "state").is_dir(), proc.stdout)

        return 0
    finally:
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    started = time.time()
    exit_code = main()
    if exit_code == 0 and "--record-evidence" in sys.argv:
        path = record_acceptance(started)
        print(f"PASS record evidence {path}")
    raise SystemExit(exit_code)
