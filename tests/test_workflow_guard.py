from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

from scripts import workflow_guard


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _write_machine_baseline(
    root: Path,
    *,
    base_commit: str,
    design_complete: bool = False,
    effective_hash: str | None = None,
    refs: list[str] | None = None,
    test_command: list[str] | None = None,
) -> None:
    effective = root / "docs" / "EFFECTIVE_DESIGN_BASELINE.md"
    state = {
        "mode": "DESIGN_RECOVERY",
        "stage": "STAGE_1_TOPIC_DISCOVERY",
        "design_complete": design_complete,
        "baseline_sha256": effective_hash or hashlib.sha256(effective.read_bytes()).hexdigest(),
        "base_commit": base_commit,
        "requirements": [
            {
                "id": "WF-TEST-001",
                "description": "fixture requirement",
                "baseline_refs": refs if refs is not None else ["docs/IMPLEMENTATION_EXECUTION_BASELINE.md#工作流硬门禁"],
                "tests": [test_command or [sys.executable, "-c", "print('guard-test-ok')"]],
            }
        ],
        "allowed_paths": [
            "AGENTS.md",
            "docs/IMPLEMENTATION_EXECUTION_BASELINE.md",
            "scripts/workflow_guard.py",
            "tests/test_workflow_guard.py",
            ".githooks/pre-commit",
        ],
        "forbidden_actions": ["business_code_change", "external_source_call", "model_call", "workflow_guard_bypass"],
        "external_call_authorized": False,
    }
    baseline = root / "docs" / "IMPLEMENTATION_EXECUTION_BASELINE.md"
    baseline.write_text(
        "---\n"
        + yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
        + "---\n\n# Fixture execution baseline\n\n## 工作流硬门禁\n\nfixture\n",
        encoding="utf-8",
    )


def _repo(tmp_path: Path, **baseline_options: object) -> Path:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / ".githooks").mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "docs" / "EFFECTIVE_DESIGN_BASELINE.md").write_text("# Effective design fixture\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Fixture agents\n\n## 强制工作流门禁\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "guard@example.test")
    _git(root, "config", "user.name", "Workflow Guard Test")
    _git(root, "add", "docs/EFFECTIVE_DESIGN_BASELINE.md", "AGENTS.md")
    _git(root, "commit", "-q", "-m", "fixture base")
    base_commit = _git(root, "rev-parse", "HEAD")
    _write_machine_baseline(root, base_commit=base_commit, **baseline_options)
    hook = root / ".githooks" / "pre-commit"
    hook.write_text("#!/bin/sh\npython scripts/workflow_guard.py commit-check\n", encoding="utf-8")
    hook.chmod(0o755)
    (root / "scripts" / "workflow_guard.py").write_text(Path(workflow_guard.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    (root / "tests" / "test_workflow_guard.py").write_text("# fixture tracked test\n", encoding="utf-8")
    return root


def test_start_returns_baseline_gap_for_incomplete_design_and_installs_hook_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = workflow_guard.run(["start"], repo_root=root)
    assert result == workflow_guard.EXIT_CODES["BASELINE_GAP"]
    assert _git(root, "config", "--get", "core.hooksPath") == ".githooks"


def test_check_returns_scope_mismatch_for_business_code_change(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "business.py").write_text("print('forbidden')\n", encoding="utf-8")
    result = workflow_guard.run(["check"], repo_root=root)
    assert result == workflow_guard.EXIT_CODES["SCOPE_MISMATCH"]


def test_unapproved_external_call_returns_user_auth_required(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    result = workflow_guard.run(["check", "--external-call"], repo_root=root)
    assert result == workflow_guard.EXIT_CODES["USER_AUTH_REQUIRED"]


def test_requirement_without_current_baseline_reference_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path, refs=[])
    result = workflow_guard.run(["check"], repo_root=root)
    assert result == workflow_guard.EXIT_CODES["REQUIREMENT_GAP"]


def test_effective_baseline_hash_mismatch_returns_baseline_gap(tmp_path: Path) -> None:
    root = _repo(tmp_path, effective_hash="0" * 64)
    result = workflow_guard.run(["check"], repo_root=root)
    assert result == workflow_guard.EXIT_CODES["BASELINE_GAP"]


def test_finish_runs_requirement_tests_and_commit_check_binds_to_current_index(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "add", "AGENTS.md", "docs/IMPLEMENTATION_EXECUTION_BASELINE.md", "scripts/workflow_guard.py", "tests/test_workflow_guard.py", ".githooks/pre-commit")
    assert workflow_guard.run(["finish"], repo_root=root) == workflow_guard.EXIT_CODES["PASS"]
    assert workflow_guard.run(["commit-check"], repo_root=root) == workflow_guard.EXIT_CODES["PASS"]

    baseline = root / "docs" / "IMPLEMENTATION_EXECUTION_BASELINE.md"
    baseline.write_text(baseline.read_text(encoding="utf-8") + "\nchanged after finish\n", encoding="utf-8")
    _git(root, "add", "docs/IMPLEMENTATION_EXECUTION_BASELINE.md")
    assert workflow_guard.run(["commit-check"], repo_root=root) == workflow_guard.EXIT_CODES["FINISH_REQUIRED"]


def test_finish_failure_blocks_attestation(tmp_path: Path) -> None:
    root = _repo(tmp_path, test_command=[sys.executable, "-c", "raise SystemExit(7)"])
    _git(root, "add", "AGENTS.md", "docs/IMPLEMENTATION_EXECUTION_BASELINE.md", "scripts/workflow_guard.py", "tests/test_workflow_guard.py", ".githooks/pre-commit")
    assert workflow_guard.run(["finish"], repo_root=root) == workflow_guard.EXIT_CODES["TEST_FAILED"]
    assert workflow_guard.run(["commit-check"], repo_root=root) == workflow_guard.EXIT_CODES["FINISH_REQUIRED"]


def test_pre_commit_hook_rejects_commit_without_finish_and_accepts_matching_attestation(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "add", "AGENTS.md", "docs/IMPLEMENTATION_EXECUTION_BASELINE.md", "scripts/workflow_guard.py", "tests/test_workflow_guard.py", ".githooks/pre-commit")
    _git(root, "config", "core.hooksPath", ".githooks")
    base_head = _git(root, "rev-parse", "HEAD")
    blocked = subprocess.run(
        ["git", "commit", "-m", "must be blocked"], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert blocked.returncode != 0
    assert _git(root, "rev-parse", "HEAD") == base_head

    assert workflow_guard.run(["finish"], repo_root=root) == workflow_guard.EXIT_CODES["PASS"]
    accepted = subprocess.run(
        ["git", "commit", "-q", "-m", "guarded commit"], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert _git(root, "rev-parse", "HEAD") != base_head
