"""Repository workflow hard gate driven by execution/current_stage.yaml."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable

import yaml


CURRENT_STAGE = Path("execution/current_stage.yaml")
IMPLEMENTATION_BASELINE = Path("docs/IMPLEMENTATION_EXECUTION_BASELINE.md")
EFFECTIVE_BASELINE = Path("docs/EFFECTIVE_DESIGN_BASELINE.md")
ATTESTATION_RELATIVE = Path("workflow_guard/finish.json")
REQUIRED_FIELDS = (
    "mode",
    "stage",
    "design_complete",
    "implementation_authorized",
    "external_calls_authorized",
    "environment_changes_authorized",
    "formal_data_writes_authorized",
    "production_authorized",
    "completion_status",
    "stage_status",
    "next_action",
    "baseline_sha256",
    "base_commit",
    "requirements",
    "allowed_paths",
    "forbidden_actions",
)
EXIT_CODES = {
    "PASS": 0,
    "BASELINE_GAP": 10,
    "SCOPE_MISMATCH": 11,
    "USER_AUTH_REQUIRED": 12,
    "REQUIREMENT_GAP": 13,
    "TEST_FAILED": 14,
    "FINISH_REQUIRED": 15,
    "WORKTREE_NOT_READY": 16,
}
BUSINESS_CODE_PATTERNS = (
    "scripts/core/**",
    "runtime_skills/**",
    "BUSINESS_MODEL_ROUTE_REGISTRY.yaml",
    "*_BUSINESS_CONTRACT.yaml",
    "data/formal/**",
    "config/model_routes.yaml",
)
SUCCESSFUL_SOURCE_LEVELS = {"LIVE_VALIDATED", "PRODUCTION_READY"}


class GuardFailure(RuntimeError):
    def __init__(self, status: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git(root: Path, args: Iterable[str], *, check: bool = True, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        raise GuardFailure("BASELINE_GAP", "git state cannot be verified", git_error=str(stderr).strip())
    return result


def _load_state(root: Path) -> dict[str, Any]:
    path = root / CURRENT_STAGE
    if not path.is_file():
        raise GuardFailure("BASELINE_GAP", "current stage machine state is missing", path=str(CURRENT_STAGE))
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GuardFailure("BASELINE_GAP", "current stage machine state must be a mapping")
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise GuardFailure("BASELINE_GAP", "current stage machine state is incomplete", missing_fields=missing)
    return payload


def _anchor_exists(content: str, anchor: str) -> bool:
    normalized = anchor.strip().replace("-", " ").casefold()
    headings = [line.lstrip("#").strip().casefold() for line in content.splitlines() if line.startswith("#")]
    return any(heading == normalized for heading in headings)


def _validate_requirements(root: Path, state: dict[str, Any]) -> list[list[str]]:
    requirements = state.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise GuardFailure("REQUIREMENT_GAP", "requirements must be a non-empty list")
    test_commands: list[list[str]] = []
    seen_ids: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise GuardFailure("REQUIREMENT_GAP", "each requirement must be a mapping")
        requirement_id = str(requirement.get("id") or "").strip()
        refs = requirement.get("baseline_refs")
        tests = requirement.get("tests")
        if not requirement_id or requirement_id in seen_ids:
            raise GuardFailure("REQUIREMENT_GAP", "requirement id is missing or duplicated", requirement_id=requirement_id)
        seen_ids.add(requirement_id)
        if not isinstance(refs, list) or not refs:
            raise GuardFailure("REQUIREMENT_GAP", "requirement lacks a current baseline reference", requirement_id=requirement_id)
        if not isinstance(tests, list) or not tests:
            raise GuardFailure("REQUIREMENT_GAP", "requirement lacks a direct test", requirement_id=requirement_id)
        for reference in refs:
            path_text, separator, anchor = str(reference).partition("#")
            reference_path = (root / path_text).resolve()
            try:
                reference_path.relative_to(root.resolve())
            except ValueError as exc:
                raise GuardFailure("REQUIREMENT_GAP", "baseline reference escapes the repository", requirement_id=requirement_id) from exc
            if not separator or not anchor or not reference_path.is_file():
                raise GuardFailure("REQUIREMENT_GAP", "baseline reference is missing", requirement_id=requirement_id, reference=reference)
            if not _anchor_exists(reference_path.read_text(encoding="utf-8"), anchor):
                raise GuardFailure("REQUIREMENT_GAP", "baseline reference anchor is not current", requirement_id=requirement_id, reference=reference)
        for command in tests:
            if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
                raise GuardFailure("REQUIREMENT_GAP", "test command must be a non-empty argument list", requirement_id=requirement_id)
            if command not in test_commands:
                test_commands.append(command)
    return test_commands


def _validate_state(root: Path) -> tuple[dict[str, Any], list[list[str]]]:
    state = _load_state(root)
    effective_path = root / EFFECTIVE_BASELINE
    implementation_path = root / IMPLEMENTATION_BASELINE
    if not effective_path.is_file() or not implementation_path.is_file():
        raise GuardFailure("BASELINE_GAP", "required baseline file is missing")
    expected_hash = str(state["baseline_sha256"]).strip().lower()
    actual_hash = _sha256_file(effective_path)
    if expected_hash != actual_hash:
        raise GuardFailure("BASELINE_GAP", "effective design baseline hash does not match", expected=expected_hash, actual=actual_hash)
    base_commit = str(state["base_commit"]).strip()
    _git(root, ["cat-file", "-e", f"{base_commit}^{{commit}}"])
    for field in (
        "design_complete",
        "implementation_authorized",
        "external_calls_authorized",
        "environment_changes_authorized",
        "formal_data_writes_authorized",
        "production_authorized",
    ):
        if not isinstance(state[field], bool):
            raise GuardFailure("BASELINE_GAP", f"{field} must be a boolean")
    if str(state["mode"]).casefold() == "design_recovery" and state["implementation_authorized"]:
        raise GuardFailure("BASELINE_GAP", "design_recovery must not authorize business implementation")
    if str(state["stage_status"]).strip() == "BASELINE_GAP" and state["design_complete"]:
        raise GuardFailure("BASELINE_GAP", "stage_status and design_complete disagree")
    if state["production_authorized"] and (not state["implementation_authorized"] or str(state["stage_status"]).strip() != "PASS"):
        raise GuardFailure("BASELINE_GAP", "production cannot be authorized before implementation stage is passable")
    if not isinstance(state["allowed_paths"], list) or not state["allowed_paths"]:
        raise GuardFailure("BASELINE_GAP", "allowed_paths must be a non-empty list")
    if not isinstance(state["forbidden_actions"], list) or not state["forbidden_actions"]:
        raise GuardFailure("BASELINE_GAP", "forbidden_actions must be a non-empty list")
    return state, _validate_requirements(root, state)


def _untracked_paths(root: Path) -> list[str]:
    output = _git(root, ["ls-files", "--others", "--exclude-standard"]).stdout
    return [line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()]


def _changed_paths(root: Path, base_commit: str, *, cached: bool = False) -> list[str]:
    args = ["diff", "--name-only"]
    if cached:
        args.append("--cached")
    args.extend([base_commit, "--"])
    paths = {line.strip().replace("\\", "/") for line in _git(root, args).stdout.splitlines() if line.strip()}
    if not cached:
        paths.update(_untracked_paths(root))
    return sorted(paths)


def _path_matches(path: str, patterns: Iterable[Any]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, str(pattern).replace("\\", "/")) for pattern in patterns)


def _validate_scope(root: Path, state: dict[str, Any], *, cached: bool = False) -> list[str]:
    paths = _changed_paths(root, str(state["base_commit"]), cached=cached)
    outside = [path for path in paths if not _path_matches(path, state["allowed_paths"])]
    frozen_tests = state.get("frozen_acceptance_tests") or []
    frozen_contracts = state.get("frozen_contracts") or []
    changed_frozen = [path for path in paths if _path_matches(path, frozen_tests)]
    changed_contracts = [path for path in paths if _path_matches(path, frozen_contracts)]
    business_changes = [] if state["implementation_authorized"] else [path for path in paths if _path_matches(path, BUSINESS_CODE_PATTERNS)]
    if outside or changed_frozen or changed_contracts or business_changes:
        raise GuardFailure(
            "SCOPE_MISMATCH",
            "changed paths exceed current stage contract",
            changed_paths=paths,
            outside_paths=outside,
            frozen_acceptance_tests_changed=changed_frozen,
            frozen_contracts_changed=changed_contracts,
            business_code_changed_without_authorization=business_changes,
        )
    return paths


def _external_requested(flag: bool) -> bool:
    env_value = str(os.environ.get("WORKFLOW_GUARD_EXTERNAL_CALL") or "").strip().lower()
    return flag or env_value in {"1", "true", "yes", "on"}


def _environment_change_requested(flag: bool) -> bool:
    env_value = str(os.environ.get("WORKFLOW_GUARD_ENVIRONMENT_CHANGE") or "").strip().lower()
    return flag or env_value in {"1", "true", "yes", "on"}


def _formal_data_write_requested(flag: bool) -> bool:
    env_value = str(os.environ.get("WORKFLOW_GUARD_FORMAL_DATA_WRITE") or "").strip().lower()
    return flag or env_value in {"1", "true", "yes", "on"}


def _validate_authorization(
    state: dict[str, Any],
    *,
    external_requested: bool,
    environment_change_requested: bool,
    formal_data_write_requested: bool,
) -> None:
    if external_requested and not state["external_calls_authorized"]:
        raise GuardFailure("USER_AUTH_REQUIRED", "external call is not authorized by the current stage")
    if environment_change_requested and not state["environment_changes_authorized"]:
        raise GuardFailure("USER_AUTH_REQUIRED", "environment change or installation is not authorized by the current stage")
    if formal_data_write_requested and not state["formal_data_writes_authorized"]:
        raise GuardFailure("USER_AUTH_REQUIRED", "formal data write is not authorized by the current stage")


def _validate_source_level_claims(state: dict[str, Any]) -> None:
    source_evidence_level = str(state.get("source_evidence_level") or "").strip().upper()
    claimed_source_level = str(state.get("claimed_source_level") or "").strip().upper()
    if source_evidence_level == "TECH_TESTED" and claimed_source_level in SUCCESSFUL_SOURCE_LEVELS:
        raise GuardFailure(
            "BASELINE_GAP",
            "TECH_TESTED evidence cannot be claimed as LIVE_VALIDATED or PRODUCTION_READY",
            source_evidence_level=source_evidence_level,
            claimed_source_level=claimed_source_level,
        )


def _ensure_hook(root: Path) -> None:
    hook = root / ".githooks" / "pre-commit"
    if not hook.is_file():
        raise GuardFailure("BASELINE_GAP", "tracked pre-commit hook is missing")
    _git(root, ["config", "core.hooksPath", ".githooks"])


def _ensure_staged_only(root: Path) -> None:
    unstaged = _git(root, ["diff", "--name-only"]).stdout.splitlines()
    untracked = _untracked_paths(root)
    if unstaged or untracked:
        raise GuardFailure(
            "WORKTREE_NOT_READY",
            "finish requires all changes to be staged and no untracked files",
            unstaged_paths=[path.strip() for path in unstaged if path.strip()],
            untracked_paths=untracked,
        )


def _run_tests(root: Path, commands: list[list[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(command, cwd=root, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        result = {"command": command, "returncode": completed.returncode}
        results.append(result)
        if completed.returncode != 0:
            raise GuardFailure(
                "TEST_FAILED",
                "requirement test failed",
                command=command,
                returncode=completed.returncode,
                stdout=(completed.stdout or "")[-2000:],
                stderr=(completed.stderr or "")[-2000:],
            )
    return results


def _index_fingerprint(root: Path, base_commit: str) -> str:
    diff = _git(root, ["diff", "--cached", "--binary", base_commit, "--"], binary=True).stdout
    return _sha256_bytes(diff)


def _attestation_path(root: Path) -> Path:
    git_dir_text = _git(root, ["rev-parse", "--git-dir"]).stdout.strip()
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    return git_dir / ATTESTATION_RELATIVE


def _write_attestation(root: Path, state: dict[str, Any], changed_paths: list[str], tests: list[dict[str, Any]]) -> Path:
    path = _attestation_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_commit": state["base_commit"],
        "mode": state["mode"],
        "stage": state["stage"],
        "completion_status": state["completion_status"],
        "stage_status": state["stage_status"],
        "implementation_authorized": state["implementation_authorized"],
        "production_authorized": state["production_authorized"],
        "next_action": state["next_action"],
        "index_sha256": _index_fingerprint(root, str(state["base_commit"])),
        "current_stage_sha256": _sha256_file(root / CURRENT_STAGE),
        "implementation_baseline_sha256": _sha256_file(root / IMPLEMENTATION_BASELINE),
        "effective_baseline_sha256": state["baseline_sha256"],
        "changed_paths": changed_paths,
        "tests": tests,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return path


def _verify_attestation(root: Path, state: dict[str, Any]) -> None:
    _ensure_staged_only(root)
    _validate_scope(root, state, cached=True)
    path = _attestation_path(root)
    if not path.is_file():
        raise GuardFailure("FINISH_REQUIRED", "finish attestation is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardFailure("FINISH_REQUIRED", "finish attestation is unreadable") from exc
    expected = {
        "base_commit": state["base_commit"],
        "completion_status": state["completion_status"],
        "stage_status": state["stage_status"],
        "implementation_authorized": state["implementation_authorized"],
        "production_authorized": state["production_authorized"],
        "next_action": state["next_action"],
        "index_sha256": _index_fingerprint(root, str(state["base_commit"])),
        "current_stage_sha256": _sha256_file(root / CURRENT_STAGE),
        "implementation_baseline_sha256": _sha256_file(root / IMPLEMENTATION_BASELINE),
        "effective_baseline_sha256": state["baseline_sha256"],
    }
    mismatches = {key: {"expected": value, "actual": payload.get(key)} for key, value in expected.items() if payload.get(key) != value}
    if mismatches:
        raise GuardFailure("FINISH_REQUIRED", "finish attestation does not match the current index", mismatches=mismatches)


def _emit(status: str, message: str, **details: Any) -> None:
    print(json.dumps({"status": status, "message": message, **details}, ensure_ascii=False, sort_keys=True))


def _finish_status_payload(state: dict[str, Any], *, task_finish_status: str) -> dict[str, Any]:
    return {
        "task_finish_status": task_finish_status,
        "stage_status": state["stage_status"],
        "implementation_authorized": state["implementation_authorized"],
        "production_authorized": state["production_authorized"],
        "next_action": state["next_action"],
    }


def run(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "check", "finish", "commit-check"))
    parser.add_argument("--external-call", action="store_true", help="declare that the requested task would make a real external call")
    parser.add_argument("--environment-change", action="store_true", help="declare that the requested task would install software or change the environment")
    parser.add_argument("--formal-data-write", action="store_true", help="declare that the requested task would write formal business data")
    args = parser.parse_args(argv)
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        state, test_commands = _validate_state(root)
        _validate_authorization(
            state,
            external_requested=_external_requested(args.external_call),
            environment_change_requested=_environment_change_requested(args.environment_change),
            formal_data_write_requested=_formal_data_write_requested(args.formal_data_write),
        )
        _validate_source_level_claims(state)
        if args.command == "commit-check":
            _verify_attestation(root, state)
            _emit(
                "PASS",
                "finish attestation matches the current index; only governance/design-recovery commit is allowed, this does not mean Stage 1 has passed",
                **_finish_status_payload(state, task_finish_status="PASS"),
            )
            return EXIT_CODES["PASS"]

        _ensure_hook(root)
        if args.command in {"start", "check"}:
            changed_paths = _validate_scope(root, state)
            if not state["design_complete"]:
                raise GuardFailure(
                    "BASELINE_GAP",
                    "design is incomplete; business code and real execution are blocked",
                    mode=state["mode"],
                    stage=state["stage"],
                    completion_status=state["completion_status"],
                    changed_paths=changed_paths,
                    allowed_paths=state["allowed_paths"],
                )
            _emit("PASS", f"workflow {args.command} passed", mode=state["mode"], stage=state["stage"], changed_paths=changed_paths)
            return EXIT_CODES["PASS"]

        _ensure_staged_only(root)
        changed_paths = _validate_scope(root, state, cached=True)
        if not state["design_complete"] and str(state["mode"]).casefold() != "design_recovery":
            raise GuardFailure("BASELINE_GAP", "incomplete design may finish only in design_recovery")
        if str(state["mode"]).casefold() != "design_recovery" and str(state["stage_status"]).strip() == "BASELINE_GAP":
            raise GuardFailure("BASELINE_GAP", "implementation mode cannot finish while the stage has BASELINE_GAP")
        tests = _run_tests(root, test_commands)
        attestation = _write_attestation(root, state, changed_paths, tests)
        _emit(
            "PASS",
            "workflow task finish passed; only governance/design-recovery commit is allowed, this does not mean Stage 1 has passed",
            **_finish_status_payload(state, task_finish_status="PASS"),
            mode=state["mode"],
            stage=state["stage"],
            completion_status=state["completion_status"],
            changed_paths=changed_paths,
            attestation=str(attestation),
        )
        return EXIT_CODES["PASS"]
    except GuardFailure as exc:
        _emit(exc.status, exc.message, **exc.details)
        return EXIT_CODES.get(exc.status, 1)


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
