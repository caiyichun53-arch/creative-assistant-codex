#!/usr/bin/env python
"""Minimal Stage control entrypoint.

This script intentionally keeps Stage control narrow:
- prepare selects the next business Stage and records the starting state.
- read-code/read-json/search bound how much context enters a Codex session.
- accept is the single formal Stage acceptance command.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
STAGE_REGISTER_PATH = ROOT / "execution" / "STAGE_REGISTER.yaml"
RUNTIME_DIR = ROOT / ".stage_runtime"
RUNTIME_PATH = RUNTIME_DIR / "current.json"
SELECTED_STAGE_PATH = RUNTIME_DIR / "current_stage.yaml"
MAX_FULL_TEXT_BYTES = 80_000
MAX_PY_FULL_LINES = 260
MAX_JSON_SUMMARY_ITEMS = 80
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_LINE_LIMIT = 240
EXCLUDED_DIRS = {
    ".git",
    ".stage_runtime",
    ".pytest_cache",
    "__pycache__",
    "archive",
    "logs",
    "outputs",
    "validation_evidence",
    "vendor",
    "vault",
}
EXCLUDED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".log",
    ".har",
    ".trace",
}
REQUIRED_TOP_KEYS = {
    "stage_id",
    "goal",
    "baseline",
    "rules",
    "scope",
    "real_workflow",
    "acceptance",
    "stop_conditions",
}


class StageControlError(Exception):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def fail(message: str, evidence: Any | None = None) -> int:
    payload: dict[str, Any] = {"status": "FAIL", "reason": message}
    if evidence is not None:
        payload["evidence"] = evidence
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1


def pass_out(payload: dict[str, Any]) -> int:
    payload = {"status": "PASS", **payload}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_contract() -> dict[str, Any]:
    if not SELECTED_STAGE_PATH.exists():
        raise StageControlError(f"missing selected Stage contract: {rel(SELECTED_STAGE_PATH)}; run prepare first")
    data = yaml.safe_load(read_text(SELECTED_STAGE_PATH))
    if not isinstance(data, dict):
        raise StageControlError(".stage_runtime/current_stage.yaml must be a mapping")
    validate_contract(data)
    return data


def load_stage_register() -> dict[str, Any]:
    if not STAGE_REGISTER_PATH.exists():
        raise StageControlError(f"missing Stage register: {rel(STAGE_REGISTER_PATH)}")
    data = yaml.safe_load(read_text(STAGE_REGISTER_PATH))
    if not isinstance(data, dict):
        raise StageControlError("STAGE_REGISTER.yaml must be a mapping")
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        raise StageControlError("STAGE_REGISTER.yaml requires a non-empty stages list")
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise StageControlError(f"stage register item {index} must be a mapping")
        for key in (
            "stage_id",
            "stage_name",
            "type",
            "status",
            "depends_on",
            "baseline_sections",
            "real_workflow_test",
            "acceptance_summary",
        ):
            if key not in stage:
                raise StageControlError(f"stage register item {index} missing {key}")
        stage_id = str(stage["stage_id"])
        if stage_id in seen:
            raise StageControlError(f"duplicate stage_id in STAGE_REGISTER.yaml: {stage_id}")
        seen.add(stage_id)
        if stage["type"] not in {"business", "maintenance"}:
            raise StageControlError(f"stage {stage_id} has invalid type: {stage['type']}")
        if not isinstance(stage["depends_on"], list):
            raise StageControlError(f"stage {stage_id} depends_on must be a list")
        if not isinstance(stage["baseline_sections"], list):
            raise StageControlError(f"stage {stage_id} baseline_sections must be a list")
        if not is_command(stage["real_workflow_test"]):
            raise StageControlError(f"stage {stage_id} real_workflow_test must be a command list")
    unknown_deps = sorted(
        {str(dep) for stage in stages for dep in stage["depends_on"] if str(dep) not in seen}
    )
    if unknown_deps:
        raise StageControlError(f"stage register references unknown dependencies: {unknown_deps}")
    return data


def select_next_business_stage(register: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    stages = register["stages"]
    status_by_id = {str(stage["stage_id"]): str(stage["status"]) for stage in stages}
    candidates: list[dict[str, Any]] = []
    blocked_by_deps: dict[str, dict[str, str]] = {}
    for stage in stages:
        if stage["type"] != "business" or stage["status"] == "passed":
            continue
        deps = [str(dep) for dep in stage["depends_on"]]
        dep_status = {dep: status_by_id.get(dep, "missing") for dep in deps}
        if all(status == "passed" for status in dep_status.values()):
            candidates.append(stage)
        else:
            blocked_by_deps[str(stage["stage_id"])] = dep_status
    if not candidates:
        raise StageControlError(f"no eligible business Stage found; blocked_by_deps={blocked_by_deps}")
    if len(candidates) > 1:
        raise StageControlError(
            "multiple eligible business Stages found: "
            + ", ".join(str(stage["stage_id"]) for stage in candidates)
        )
    selected = candidates[0]
    deps = [str(dep) for dep in selected["depends_on"]]
    dep_status = {dep: status_by_id.get(dep, "missing") for dep in deps}
    reason = {
        "rule": "first type=business stage whose status is not passed and whose dependencies are passed",
        "business_stage_order": [
            str(stage["stage_id"]) for stage in stages if stage["type"] == "business"
        ],
        "dependency_status": dep_status,
        "blocked_later_stages": blocked_by_deps,
    }
    return selected, reason


def build_contract(register: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    baseline = register.get("baseline")
    if not isinstance(baseline, dict) or not baseline.get("file") or not baseline.get("sha256"):
        raise StageControlError("STAGE_REGISTER.yaml baseline.file and baseline.sha256 are required")
    protected_files = register.get("protected_files", [])
    if not isinstance(protected_files, list):
        raise StageControlError("STAGE_REGISTER.yaml protected_files must be a list when present")
    allowed_files = stage.get("allowed_files", register.get("default_allowed_files", []))
    if not isinstance(allowed_files, list) or not allowed_files:
        raise StageControlError(f"stage {stage['stage_id']} requires allowed_files or default_allowed_files")
    forbidden_modes = register.get("forbidden_modes", [])
    if not isinstance(forbidden_modes, list):
        raise StageControlError("STAGE_REGISTER.yaml forbidden_modes must be a list when present")
    contract = {
        "stage_id": stage["stage_id"],
        "stage_name": stage["stage_name"],
        "stage_type": stage["type"],
        "stage_status": stage["status"],
        "authorization_required": bool(stage.get("authorization_required", False)),
        "authorization_status": stage.get("authorization_status", "not_required"),
        "depends_on": stage["depends_on"],
        "goal": stage["stage_name"],
        "baseline": {
            "file": baseline["file"],
            "sha256": baseline["sha256"],
            "sections": stage["baseline_sections"],
        },
        "rules": {
            "required": [
                {
                    "id": "business_stage_register_selection",
                    "description": "prepare selects only the next eligible type=business Stage from STAGE_REGISTER.yaml.",
                }
            ],
            "forbidden": register.get("forbidden_rules", []),
        },
        "scope": {
            "allowed_files": allowed_files,
            "protected_files": protected_files,
        },
        "real_workflow": {
            "entry_command": stage["real_workflow_test"],
            "evidence_commands": [],
            "forbidden_modes": forbidden_modes,
        },
        "acceptance": [
            {
                "id": "real_workflow_test",
                "evidence": "entry_command",
                "summary": stage["acceptance_summary"],
            }
        ],
        "stop_conditions": register.get("stop_conditions", []),
        "selection_source": register.get("source", {}),
    }
    validate_contract(contract)
    return contract


def validate_contract(contract: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_TOP_KEYS - set(contract))
    if missing:
        raise StageControlError(f"contract missing required keys: {missing}")
    baseline = contract.get("baseline")
    if not isinstance(baseline, dict):
        raise StageControlError("baseline must be a mapping")
    for key in ("file", "sha256", "sections"):
        if key not in baseline:
            raise StageControlError(f"baseline.{key} is required")
    if not isinstance(baseline["sections"], list):
        raise StageControlError("baseline.sections must be a list")
    rules = contract.get("rules")
    if not isinstance(rules, dict) or "required" not in rules or "forbidden" not in rules:
        raise StageControlError("rules.required and rules.forbidden are required")
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        raise StageControlError("scope must be a mapping")
    for key in ("allowed_files", "protected_files"):
        if not isinstance(scope.get(key), list):
            raise StageControlError(f"scope.{key} must be a list")
    workflow = contract.get("real_workflow")
    if not isinstance(workflow, dict):
        raise StageControlError("real_workflow must be a mapping")
    if not is_command(workflow.get("entry_command")):
        raise StageControlError("real_workflow.entry_command must be a non-empty command list")
    if not isinstance(workflow.get("evidence_commands"), list):
        raise StageControlError("real_workflow.evidence_commands must be a list")
    if not isinstance(workflow.get("forbidden_modes"), list):
        raise StageControlError("real_workflow.forbidden_modes must be a list")
    evidence_ids = {"entry_command"}
    for item in workflow["evidence_commands"]:
        if not isinstance(item, dict) or not item.get("id") or not is_command(item.get("command")):
            raise StageControlError("each evidence command needs id and command list")
        evidence_ids.add(str(item["id"]))
    if not isinstance(contract.get("acceptance"), list) or not contract["acceptance"]:
        raise StageControlError("acceptance must be a non-empty list")
    for item in contract["acceptance"]:
        if not isinstance(item, dict) or not item.get("id") or not item.get("evidence"):
            raise StageControlError("each acceptance item needs id and evidence")
        if str(item["evidence"]) not in evidence_ids:
            raise StageControlError(f"acceptance {item['id']} references unknown evidence {item['evidence']}")


def is_command(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(part, str) for part in value)


def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise StageControlError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def git_state() -> dict[str, Any]:
    branch = run_git(["branch", "--show-current"]).stdout.strip()
    head = run_git(["rev-parse", "HEAD"]).stdout.strip()
    status = parse_porcelain()
    return {"branch": branch, "head": head, "status": status}


def parse_porcelain() -> list[dict[str, str]]:
    proc = run_git(["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=all", "-z"])
    items = [item for item in proc.stdout.split("\0") if item]
    result: list[dict[str, str]] = []
    index = 0
    while index < len(items):
        item = items[index]
        code = item[:2]
        path = item[3:] if len(item) > 3 else ""
        if code[0] in {"R", "C"} and index + 1 < len(items):
            new_path = items[index + 1]
            result.append({"code": code, "path": normalize_path(new_path), "source": normalize_path(path)})
            index += 2
        else:
            result.append({"code": code, "path": normalize_path(path)})
            index += 1
    return result


def normalize_path(value: str) -> str:
    return value.replace("\\", "/")


def match_any(path: str, patterns: list[str]) -> bool:
    path = normalize_path(path)
    for pattern in patterns:
        normalized = normalize_path(str(pattern))
        if normalized.endswith("/**"):
            if path == normalized[:-3] or path.startswith(normalized[:-2]):
                return True
        elif normalized.endswith("/"):
            if path.startswith(normalized):
                return True
        elif fnmatch.fnmatch(path, normalized):
            return True
        elif path == normalized:
            return True
    return False


def extract_sections(path: Path, requested: list[str]) -> dict[str, str]:
    text = read_text(path)
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((idx, len(match.group(1)), line.strip()))
    snippets: dict[str, str] = {}
    for section in requested:
        start_info = next((item for item in headings if item[2] == section), None)
        if start_info is None:
            raise StageControlError(f"baseline section not found: {section}")
        start, level, _ = start_info
        end = len(lines)
        for idx, next_level, _ in headings:
            if idx > start and next_level <= level:
                end = idx
                break
        snippets[section] = "\n".join(lines[start:end]).strip()
    return snippets


def write_runtime(contract: dict[str, Any], baseline_hash: str, register_hash: str) -> dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    SELECTED_STAGE_PATH.write_text(yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8")
    runtime = {
        "stage_id": contract["stage_id"],
        "contract_hash": sha256_text(read_text(SELECTED_STAGE_PATH)),
        "stage_register_hash": register_hash,
        "baseline_hash": baseline_hash,
        "selected_stage_path": rel(SELECTED_STAGE_PATH),
        "git_start": git_state(),
        "last_real_test_result": None,
    }
    RUNTIME_PATH.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
    return runtime


def load_runtime() -> dict[str, Any]:
    if not RUNTIME_PATH.exists():
        raise StageControlError("missing .stage_runtime/current.json; run prepare first")
    data = json.loads(read_text(RUNTIME_PATH))
    if not isinstance(data, dict):
        raise StageControlError(".stage_runtime/current.json must be a mapping")
    return data


def command_prepare(_: argparse.Namespace) -> int:
    try:
        register = load_stage_register()
        selected_stage, selection_reason = select_next_business_stage(register)
        contract = build_contract(register, selected_stage)
        baseline_path = ROOT / contract["baseline"]["file"]
        if not baseline_path.exists():
            return fail("baseline file is missing", {"file": contract["baseline"]["file"]})
        actual_hash = sha256_file(baseline_path)
        expected_hash = str(contract["baseline"]["sha256"]).lower()
        if actual_hash != expected_hash:
            return fail(
                "baseline sha256 mismatch",
                {"file": contract["baseline"]["file"], "expected": expected_hash, "actual": actual_hash},
            )
        snippets = extract_sections(baseline_path, [str(item) for item in contract["baseline"]["sections"]])
        register_hash = sha256_text(read_text(STAGE_REGISTER_PATH))
        runtime = write_runtime(contract, actual_hash, register_hash)
        return pass_out(
            {
                "selected_business_stage_id": contract["stage_id"],
                "stage_name": contract["stage_name"],
                "selection_reason": selection_reason,
                "dependency_status": selection_reason["dependency_status"],
                "baseline_sections": contract["baseline"]["sections"],
                "real_workflow_test": contract["real_workflow"]["entry_command"],
                "temporary_stage_contract": rel(SELECTED_STAGE_PATH),
                "baseline_hash": actual_hash,
                "contract_hash": runtime["contract_hash"],
                "stage_register_hash": runtime["stage_register_hash"],
                "allowed_files": contract["scope"]["allowed_files"],
                "protected_files": contract["scope"]["protected_files"],
                "baseline_snippets": snippets,
                "git_start": runtime["git_start"],
            }
        )
    except StageControlError as exc:
        return fail(str(exc))


def command_read_code(args: argparse.Namespace) -> int:
    try:
        path = resolve_under_root(args.file)
        if path.suffix != ".py":
            raise StageControlError("read-code only supports Python files")
        lines = read_text(path).splitlines()
        if args.symbol:
            start, end = find_python_symbol(path, args.symbol)
            selected = "\n".join(lines[start - 1 : end])
            return pass_out({"file": rel(path), "symbol": args.symbol, "start": start, "end": end, "source": selected})
        if len(lines) > MAX_PY_FULL_LINES or path.stat().st_size > MAX_FULL_TEXT_BYTES:
            raise StageControlError("large Python files require --symbol")
        return pass_out({"file": rel(path), "source": "\n".join(lines)})
    except (OSError, StageControlError, SyntaxError) as exc:
        return fail(str(exc))


def find_python_symbol(path: Path, symbol: str) -> tuple[int, int]:
    tree = ast.parse(read_text(path))
    parts = symbol.split(".")
    nodes: list[ast.AST] = list(tree.body)
    target: ast.AST | None = None
    for part in parts:
        target = None
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part:
                target = node
                break
        if target is None:
            raise StageControlError(f"symbol not found: {symbol}")
        nodes = list(getattr(target, "body", []))
    start = getattr(target, "lineno", None)
    end = getattr(target, "end_lineno", None)
    if not start or not end:
        raise StageControlError(f"symbol lacks line metadata: {symbol}")
    return int(start), int(end)


def command_read_json(args: argparse.Namespace) -> int:
    try:
        path = resolve_under_root(args.file)
        if path.suffix.lower() != ".json":
            raise StageControlError("read-json only supports .json files")
        data = json.loads(read_text(path))
        if args.pointer is not None:
            value = json_pointer(data, args.pointer)
            return pass_out({"file": rel(path), "pointer": args.pointer, "value": value})
        return pass_out({"file": rel(path), "summary": summarize_json(data)})
    except (OSError, json.JSONDecodeError, StageControlError) as exc:
        return fail(str(exc))


def json_pointer(data: Any, pointer: str) -> Any:
    if pointer == "":
        return data
    if not pointer.startswith("/"):
        raise StageControlError("JSON Pointer must be empty or start with /")
    current = data
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not part.isdigit():
                raise StageControlError(f"list pointer segment must be numeric: {part}")
            current = current[int(part)]
        elif isinstance(current, dict):
            if part not in current:
                raise StageControlError(f"object pointer segment missing: {part}")
            current = current[part]
        else:
            raise StageControlError(f"cannot descend into scalar at: {part}")
    return current


def summarize_json(data: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return type(data).__name__
    if isinstance(data, dict):
        items = list(data.items())[:MAX_JSON_SUMMARY_ITEMS]
        return {
            "type": "object",
            "size": len(data),
            "keys": [{str(key): summarize_json(value, depth + 1)} for key, value in items],
        }
    if isinstance(data, list):
        sample = data[: min(len(data), 5)]
        return {"type": "array", "size": len(data), "sample": [summarize_json(item, depth + 1) for item in sample]}
    return {"type": type(data).__name__, "value": data if isinstance(data, (str, int, float, bool)) or data is None else str(data)}


def command_search(args: argparse.Namespace) -> int:
    try:
        max_results = min(args.max_results, DEFAULT_SEARCH_LIMIT)
        line_limit = min(args.max_line_length, DEFAULT_LINE_LIMIT)
        pattern = re.compile(args.pattern)
        roots = [resolve_under_root(path) for path in args.path]
        results: list[dict[str, Any]] = []
        for root in roots:
            for file_path in iter_search_files(root):
                try:
                    for line_no, line in enumerate(read_text(file_path).splitlines(), start=1):
                        if pattern.search(line):
                            text = line[:line_limit]
                            if len(line) > line_limit:
                                text += "...[truncated]"
                            results.append({"file": rel(file_path), "line": line_no, "text": text})
                            if len(results) >= max_results:
                                return pass_out({"count": len(results), "max_results": max_results, "results": results})
                except UnicodeDecodeError:
                    continue
        return pass_out({"count": len(results), "max_results": max_results, "results": results})
    except (OSError, re.error, StageControlError) as exc:
        return fail(str(exc))


def iter_search_files(root: Path):
    if root.is_file():
        if not should_exclude(root):
            yield root
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS and not name.startswith(".stage_runtime")]
        if should_exclude(current_path):
            continue
        for name in files:
            path = current_path / name
            if not should_exclude(path):
                yield path


def should_exclude(path: Path) -> bool:
    parts = set(path.relative_to(ROOT).parts) if is_under(path, ROOT) else set(path.parts)
    if parts & EXCLUDED_DIRS:
        return True
    suffix = path.suffix.lower()
    return suffix in EXCLUDED_SUFFIXES


def resolve_under_root(value: str) -> Path:
    path = (ROOT / value).resolve()
    if not is_under(path, ROOT):
        raise StageControlError(f"path is outside repository: {value}")
    if not path.exists():
        raise StageControlError(f"path does not exist: {value}")
    return path


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def command_accept(_: argparse.Namespace) -> int:
    try:
        contract = load_contract()
        runtime = load_runtime()
        current_contract_hash = sha256_text(read_text(SELECTED_STAGE_PATH))
        if runtime.get("contract_hash") != current_contract_hash:
            raise StageControlError("contract changed after prepare; rerun prepare")
        current_register_hash = sha256_text(read_text(STAGE_REGISTER_PATH))
        if runtime.get("stage_register_hash") != current_register_hash:
            raise StageControlError("STAGE_REGISTER.yaml changed after prepare; rerun prepare")
        if contract.get("authorization_required") and contract.get("authorization_status") != "authorized":
            return fail(
                "selected Stage requires explicit authorization before real workflow execution",
                {
                    "stage_id": contract["stage_id"],
                    "stage_status": contract.get("stage_status"),
                    "authorization_status": contract.get("authorization_status"),
                    "real_workflow_test": contract["real_workflow"]["entry_command"],
                },
            )
        baseline_path = ROOT / contract["baseline"]["file"]
        actual_hash = sha256_file(baseline_path)
        if actual_hash != str(contract["baseline"]["sha256"]).lower():
            return fail("baseline sha256 mismatch", {"expected": contract["baseline"]["sha256"], "actual": actual_hash})
        changed = parse_porcelain()
        scope_result = check_scope(contract, changed)
        if scope_result:
            return fail(scope_result["reason"], scope_result)
        forbidden_result = check_forbidden_changes(contract, changed)
        if forbidden_result:
            return fail(forbidden_result["reason"], forbidden_result)
        mode_result = check_forbidden_modes(contract)
        if mode_result:
            return fail(mode_result["reason"], mode_result)
        run_id = f"{contract['stage_id']}-{int(time.time())}"
        entry_result = run_workflow_command("entry_command", contract["real_workflow"]["entry_command"])
        evidence_results = []
        for item in contract["real_workflow"]["evidence_commands"]:
            evidence_results.append(run_workflow_command(str(item["id"]), item["command"]))
        all_results = [entry_result, *evidence_results]
        last_result = {
            "run_id": run_id,
            "entry_command": entry_result,
            "evidence_commands": evidence_results,
        }
        runtime["last_real_test_result"] = last_result
        RUNTIME_PATH.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
        failures = [item for item in all_results if item["exit_code"] != 0]
        if failures:
            return fail("real workflow or evidence command failed", {"run_id": run_id, "failures": failures})
        return pass_out(
            {
                "stage_id": contract["stage_id"],
                "run_id": run_id,
                "changed_files": changed,
                "entry_command_exit_code": entry_result["exit_code"],
                "evidence_exit_codes": {item["id"]: item["exit_code"] for item in evidence_results},
                "conclusion": "PASS",
            }
        )
    except StageControlError as exc:
        return fail(str(exc))


def check_scope(contract: dict[str, Any], changed: list[dict[str, str]]) -> dict[str, Any] | None:
    allowed = [str(item) for item in contract["scope"]["allowed_files"]]
    protected = [str(item) for item in contract["scope"]["protected_files"]]
    paths = sorted({item["path"] for item in changed if item.get("path")})
    outside = [path for path in paths if not match_any(path, allowed)]
    if outside:
        return {"reason": "changed files outside scope.allowed_files", "outside_files": outside, "allowed_files": allowed}
    protected_hits = [path for path in paths if match_any(path, protected)]
    if protected_hits:
        return {"reason": "protected files were modified", "protected_files": protected_hits}
    return None


def check_forbidden_changes(contract: dict[str, Any], changed: list[dict[str, str]]) -> dict[str, Any] | None:
    paths = sorted({item["path"] for item in changed if item.get("path")})
    old_path_pattern = re.compile(r"(?i)(^|/)(ROADMAP|HANDOFF|.*GOAL.*|.*DNA.*)(\..*)?$")
    old_path_hits = [path for path in paths if old_path_pattern.search(path)]
    if old_path_hits:
        return {"reason": "forbidden legacy document path changed or added", "paths": old_path_hits}
    forbidden_rules = []
    for item in contract["rules"].get("forbidden", []):
        if isinstance(item, dict) and item.get("pattern"):
            forbidden_rules.append({"id": str(item.get("id", "forbidden")), "regex": re.compile(str(item["pattern"]))})
        elif isinstance(item, str):
            forbidden_rules.append({"id": item, "regex": re.compile(re.escape(item), re.IGNORECASE)})
    for path in paths:
        if path in {
            "AGENTS.md",
            "execution/STAGE_REGISTER.yaml",
            "execution/current_stage.yaml",
            "scripts/execution/stage_control.py",
        }:
            continue
        file_path = ROOT / path
        if not file_path.exists() or file_path.is_dir() or should_exclude(file_path):
            continue
        try:
            text = read_text(file_path)
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule in forbidden_rules:
                if rule["regex"].search(line):
                    return {
                        "reason": "forbidden rule pattern found in changed file",
                        "rule": rule["id"],
                        "file": path,
                        "line": line_no,
                        "text": line[:DEFAULT_LINE_LIMIT],
                    }
    return None


def check_forbidden_modes(contract: dict[str, Any]) -> dict[str, Any] | None:
    modes = [str(item).lower() for item in contract["real_workflow"].get("forbidden_modes", [])]
    commands = [contract["real_workflow"]["entry_command"]]
    commands.extend(item["command"] for item in contract["real_workflow"].get("evidence_commands", []))
    joined = "\n".join(" ".join(command).lower() for command in commands)
    hits = [mode for mode in modes if mode.lower() in joined]
    if hits:
        return {"reason": "real workflow command uses forbidden mode", "forbidden_modes": hits}
    return None


def run_workflow_command(command_id: str, command: list[str]) -> dict[str, Any]:
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return {
        "id": command_id,
        "command": command,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout": truncate(proc.stdout),
        "stderr": truncate(proc.stderr),
    }


def truncate(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal Stage control")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.set_defaults(func=command_prepare)

    read_code = subparsers.add_parser("read-code")
    read_code.add_argument("--file", required=True)
    read_code.add_argument("--symbol")
    read_code.set_defaults(func=command_read_code)

    read_json = subparsers.add_parser("read-json")
    read_json.add_argument("--file", required=True)
    read_json.add_argument("--pointer")
    read_json.set_defaults(func=command_read_json)

    search = subparsers.add_parser("search")
    search.add_argument("--path", action="append", required=True)
    search.add_argument("--pattern", required=True)
    search.add_argument("--max-results", type=int, default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--max-line-length", type=int, default=DEFAULT_LINE_LIMIT)
    search.set_defaults(func=command_search)

    accept = subparsers.add_parser("accept")
    accept.set_defaults(func=command_accept)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
