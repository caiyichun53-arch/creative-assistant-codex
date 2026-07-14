"""Repository workflow hard gate driven by IMPLEMENTATION_EXECUTION_BASELINE.md."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

import yaml


IMPLEMENTATION_BASELINE = Path("docs/IMPLEMENTATION_EXECUTION_BASELINE.md")
EFFECTIVE_BASELINE = Path("docs/EFFECTIVE_DESIGN_BASELINE.md")
ATTESTATION_RELATIVE = Path("workflow_guard/finish.json")
REQUIRED_FIELDS = (
    "mode",
    "stage",
    "design_complete",
    "baseline_sha256",
    "base_commit",
    "requirements",
    "allowed_paths",
    "forbidden_actions",
    "external_call_authorized",
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


def _front_matter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise GuardFailure("BASELINE_GAP", "implementation baseline lacks YAML front matter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise GuardFailure("BASELINE_GAP", "implementation baseline front matter is not closed") from exc
    payload = yaml.safe_load("\n".join(lines[1:closing]))
    if not isinstance(payload, dict):
        raise GuardFailure("BASELINE_GAP", "implementation baseline front matter must be a mapping")
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise GuardFailure("BASELINE_GAP", "machine workflow state is incomplete", missing_fields=missing)
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
    baseline_path = root / IMPLEMENTATION_BASELINE
    effective_path = root / EFFECTIVE_BASELINE
    if not baseline_path.is_file() or not effective_path.is_file():
        raise GuardFailure("BASELINE_GAP", "required baseline file is missing")
    state = _front_matter(baseline_path)
    expected_hash = str(state["baseline_sha256"]).strip().lower()
    actual_hash = _sha256_file(effective_path)
    if expected_hash != actual_hash:
        raise GuardFailure("BASELINE_GAP", "effective design baseline hash does not match", expected=expected_hash, actual=actual_hash)
    base_commit = str(state["base_commit"]).strip()
    _git(root, ["cat-file", "-e", f"{base_commit}^{{commit}}"])
    if not isinstance(state["allowed_paths"], list) or not state["allowed_paths"]:
        raise GuardFailure("BASELINE_GAP", "allowed_paths must be a non-empty list")
    if not isinstance(state["forbidden_actions"], list) or not state["forbidden_actions"]:
        raise GuardFailure("BASELINE_GAP", "forbidden_actions must be a non-empty list")
    if not isinstance(state["design_complete"], bool) or not isinstance(state["external_call_authorized"], bool):
        raise GuardFailure("BASELINE_GAP", "design_complete and external_call_authorized must be booleans")
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


def _path_allowed(path: str, allowed_patterns: list[Any]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, str(pattern).replace("\\", "/")) for pattern in allowed_patterns)


def _validate_scope(root: Path, state: dict[str, Any], *, cached: bool = False) -> list[str]:
    paths = _changed_paths(root, str(state["base_commit"]), cached=cached)
    outside = [path for path in paths if not _path_allowed(path, state["allowed_paths"])]
    if outside:
        raise GuardFailure("SCOPE_MISMATCH", "changed paths exceed allowed_paths", changed_paths=paths, outside_paths=outside)
    return paths


def _external_requested(flag: bool) -> bool:
    env_value = str(os.environ.get("WORKFLOW_GUARD_EXTERNAL_CALL") or "").strip().lower()
    return flag or env_value in {"1", "true", "yes", "on"}


def _validate_external_authorization(state: dict[str, Any], requested: bool) -> None:
    if requested and not state["external_call_authorized"]:
        raise GuardFailure("USER_AUTH_REQUIRED", "external call is not authorized by the machine baseline")


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
        "index_sha256": _index_fingerprint(root, str(state["base_commit"])),
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
        "index_sha256": _index_fingerprint(root, str(state["base_commit"])),
        "implementation_baseline_sha256": _sha256_file(root / IMPLEMENTATION_BASELINE),
        "effective_baseline_sha256": state["baseline_sha256"],
    }
    mismatches = {key: {"expected": value, "actual": payload.get(key)} for key, value in expected.items() if payload.get(key) != value}
    if mismatches:
        raise GuardFailure("FINISH_REQUIRED", "finish attestation does not match the current index", mismatches=mismatches)


def _emit(status: str, message: str, **details: Any) -> None:
    print(json.dumps({"status": status, "message": message, **details}, ensure_ascii=False, sort_keys=True))


def run(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "check", "finish", "commit-check"))
    parser.add_argument("--external-call", action="store_true", help="declare that the requested task would make a real external call")
    args = parser.parse_args(argv)
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        state, test_commands = _validate_state(root)
        _validate_external_authorization(state, _external_requested(args.external_call))
        if args.command == "commit-check":
            _verify_attestation(root, state)
            _emit("PASS", "finish attestation matches the current index")
            return EXIT_CODES["PASS"]

        _ensure_hook(root)
        if args.command in {"start", "check"}:
            changed_paths = _validate_scope(root, state)
            if not state["design_complete"]:
                raise GuardFailure(
                    "BASELINE_GAP",
                    "design is incomplete; business code is blocked",
                    mode=state["mode"],
                    stage=state["stage"],
                    changed_paths=changed_paths,
                    allowed_paths=state["allowed_paths"],
                )
            _emit("PASS", f"workflow {args.command} passed", mode=state["mode"], stage=state["stage"], changed_paths=changed_paths)
            return EXIT_CODES["PASS"]

        _ensure_staged_only(root)
        changed_paths = _validate_scope(root, state, cached=True)
        if not state["design_complete"] and state["mode"] != "DESIGN_RECOVERY":
            raise GuardFailure("BASELINE_GAP", "incomplete design may finish only in DESIGN_RECOVERY")
        tests = _run_tests(root, test_commands)
        attestation = _write_attestation(root, state, changed_paths, tests)
        _emit(
            "PASS",
            "workflow finish passed",
            mode=state["mode"],
            stage=state["stage"],
            baseline_status="PASS" if state["design_complete"] else "BASELINE_GAP",
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
