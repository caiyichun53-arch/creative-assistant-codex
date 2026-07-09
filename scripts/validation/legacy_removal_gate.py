from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
GOAL_ID = "GOAL-V0.6.2-LEGACY-REMOVAL-01"
INVENTORY_PATH = ROOT / "LEGACY_REMOVAL_INVENTORY.yaml"

LEGACY_PATH_PREFIXES = (
    "scripts/analyze/",
    "scripts/collect/",
    "scripts/content/",
    "scripts/db/",
    "scripts/feishu/",
    "scripts/humanize/",
    "scripts/language_fuel/",
    "scripts/llm/",
    "scripts/music/",
    "scripts/research/",
    "scripts/reverse/",
    "scripts/setup/",
    "scripts/topics/",
    "tools/asr/",
)
LEGACY_EXACT_PATHS = (
    "scripts/project_check.py",
    "scripts/run_daily.py",
    "tools/start_listener.vbs",
    "启动真人写作提炼.bat",
    "启动逆向拆DNA.bat",
)
LEGACY_REFERENCE_TOKENS = LEGACY_EXACT_PATHS + LEGACY_PATH_PREFIXES + (
    "creation.db",
    "scripts/llm/call.py",
    "codex.exe",
    "claude.exe",
    "fallback_model",
    "secondary_provider",
    "backup_route",
    "SQLite生产回退",
)
ACTIVE_CODE_PREFIXES = (
    "scripts/core/",
    "scripts/validation/",
    "runtime_skills/",
    "tests/",
)
ACTIVE_CONFIG_PREFIXES = (
    "config/",
)
ALLOWED_GUARD_REFERENCE_FILES = {
    "scripts/validation/clean_room_empty_db.py",
    "scripts/validation/clean_room_readiness.py",
    "scripts/validation/legacy_removal_gate.py",
    "scripts/validation/live_gates.py",
    "scripts/validation/production_startup_smoke.py",
    "scripts/core/business_data/register_competitor_accounts.py",
    "scripts/core/business_data/run_competitor_registration_full.py",
    "scripts/core/business_data/run_reverse_prep.py",
    "scripts/core/experience/run_sample_deep_analyze.py",
    "scripts/core/persistence/install_versionref_schema_into_business_db.py",
    "scripts/core/business_data/README.md",
    "tests/core/test_phase5_business_workflow.py",
    "tests/validation/test_clean_room_readiness.py",
    "tests/validation/test_authoritative_docs_not_legacy.py",
    "tests/validation/test_legacy_removal_gate.py",
}
ALLOWED_EXCLUSION_REFERENCE_FILES = {
    "BUSINESS_MODEL_ROUTE_REGISTRY.yaml",
}
# Root .bat/.vbs launchers are treated as legacy-style by default because the old
# business pipeline shipped several (启动逆向拆DNA.bat etc). These two are a new,
# read-only dev-tool convenience launcher for scripts/monitor/ (double-click start/stop
# for a user who isn't comfortable with a CLI) -- not a revived legacy execution path.
ALLOWED_NEW_EXECUTABLE_PATHS = {
    "启动监控面板.bat",
    "停止监控面板.bat",
    "scripts/scheduled/run_daily_incremental.bat",
    "scripts/scheduled/run_daily_incremental_hidden.vbs",
}
HISTORICAL_DOC_PREFIXES = (
    "implementation_progress/",
    "goals/",
)
HISTORICAL_DOC_SUFFIXES = (
    "_REPORT.md",
    "_STATUS.yaml",
    "_MATRIX.yaml",
    "_AUDIT.md",
    "_BASELINE.md",
    "_INVENTORY.yaml",
    "_PLAN.md",
    "_PROOF.md",
    "_CHECKLIST.yaml",
)
HISTORICAL_DOC_EXACT = {
    "AGENTS.md",
    "BUILD_PLAN.md",
    "CLAUDE.md",
    "CURRENT_REPOSITORY_BASELINE.md",
    "DATA_PURGE_PLAN.md",
    "LEGACY_CODE_AUDIT.md",
    "LEGACY_DATA_INVENTORY.yaml",
    "LEGACY_REMOVAL_INVENTORY.yaml",
    "LEGACY_RETIREMENT_MATRIX.yaml",
    "LEGACY_RETIREMENT_REPORT.md",
    "LEGACY_TO_TARGET_DATA_MAPPING.yaml",
    "MIGRATION_EXECUTION_PLAN.md",
    "MODULE_REUSE_MATRIX.yaml",
    "README.md",
    "REQUIREMENT_CODE_TRACEABILITY.yaml",
    "UNDOCUMENTED_LEGACY_BEHAVIOR.md",
}
REPLACEMENT_BY_PREFIX = {
    "scripts/analyze/": "formal Core workflow plus future deterministic scoring adapter",
    "scripts/collect/": "scripts/core/external_adapters CollectorAdapter boundary",
    "scripts/content/": "runtime_skills/script_review plus formal Materializer/Outbox",
    "scripts/db/": "scripts/core/persistence formal schema chain",
    "scripts/feishu/": "scripts/core/hermes whitelist Tool and Outbox",
    "scripts/humanize/": "runtime_skills/script_review and experience_revision_propose",
    "scripts/language_fuel/": "formal experience/evidence pipeline",
    "scripts/llm/": "scripts/core/model_gateway",
    "scripts/music/": "scripts/core/external_adapters NetEase adapter boundary",
    "scripts/research/": "scripts/core/research",
    "scripts/reverse/": "runtime_skills/sample_deep_analyze and tactic_extract",
    "scripts/topics/": "runtime_skills/source_to_topic and scripts/core/production",
    "tools/asr/": "scripts/core/external_adapters ASR adapter boundary",
}


def git_ls_files() -> list[str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]


def staged_deleted_files() -> list[str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "--diff-filter=D"],
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()]


def existing_inventory_paths() -> list[str]:
    if not INVENTORY_PATH.exists():
        return []
    try:
        data = yaml.safe_load(INVENTORY_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    return [
        str(item["path"]).replace("\\", "/")
        for item in data.get("items", [])
        if isinstance(item, dict) and item.get("path")
    ]


def is_legacy_executable_path(path: str) -> bool:
    if path in ALLOWED_NEW_EXECUTABLE_PATHS:
        return False
    return path in LEGACY_EXACT_PATHS or path.endswith((".bat", ".vbs")) or any(path.startswith(prefix) for prefix in LEGACY_PATH_PREFIXES)


def is_historical_doc(path: str) -> bool:
    if path in HISTORICAL_DOC_EXACT:
        return True
    if any(path.startswith(prefix) for prefix in HISTORICAL_DOC_PREFIXES):
        return True
    return path.endswith(HISTORICAL_DOC_SUFFIXES)


def is_active_surface(path: str) -> bool:
    return path == "README.md" or path == "BUSINESS_MODEL_ROUTE_REGISTRY.yaml" or path.startswith(ACTIVE_CODE_PREFIXES) or path.startswith(ACTIVE_CONFIG_PREFIXES)


def replacement_for(path: str) -> str:
    for prefix, replacement in REPLACEMENT_BY_PREFIX.items():
        if path.startswith(prefix):
            return replacement
    if path == "scripts/run_daily.py":
        return "scripts/core/scheduler and Hermes whitelist Tool controlled task creation"
    if path.endswith(".bat") or path.endswith(".vbs"):
        return "no direct launcher; use future user-approved Hermes/Core activation path"
    return "V0.6.2 formal runtime"


def reference_hits(files: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for path in files:
        full = ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits = sorted({token for token in LEGACY_REFERENCE_TOKENS if token in text})
        if not hits:
            continue
        if path in ALLOWED_GUARD_REFERENCE_FILES or path in ALLOWED_EXCLUSION_REFERENCE_FILES:
            historical.append({"path": path, "tokens": hits[:20], "token_count": len(hits), "reference_role": "guard_or_explicit_exclusion"})
            continue
        entry = {"path": path, "tokens": hits[:20], "token_count": len(hits)}
        if is_active_surface(path) and not is_historical_doc(path):
            active.append(entry)
        else:
            historical.append(entry)
    return active, historical


def build_inventory(files: list[str], active_refs: list[dict[str, Any]], historical_refs: list[dict[str, Any]], deleted_files: list[str]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    current = set(files)
    for path in sorted(path for path in set(files + deleted_files) if is_legacy_executable_path(path)):
        referenced_by = sorted(
            entry["path"]
            for entry in active_refs + historical_refs
            if any(token in path or path.startswith(token) or token in path for token in entry["tokens"])
        )
        production_reachable = any(entry["path"].startswith(ACTIVE_CODE_PREFIXES + ACTIVE_CONFIG_PREFIXES) for entry in active_refs if path in entry["tokens"])
        test_reachable = any(entry["path"].startswith("tests/") for entry in active_refs if path in entry["tokens"])
        documentation_reachable = any(entry["path"] for entry in historical_refs if path in entry["tokens"])
        items.append(
            {
                "path": path,
                "type": "legacy_executable_or_business_runtime_asset",
                "original_purpose": "Legacy MVP business/runtime path before V0.6.2 formal runtime",
                "current_reference_count": len(referenced_by),
                "referenced_by": referenced_by[:50],
                "production_reachable": production_reachable,
                "test_reachable": test_reachable,
                "documentation_reachable": documentation_reachable,
                "replacement": replacement_for(path),
                "removal_decision": "remove_from_active_repository",
                "removal_reason": "Not part of the V0.6.2 formal production chain and superseded by Core/Adapter/Formal Skill boundaries",
                "preservation_reason": "",
                "removed_from_worktree": path not in current,
                "risk": "medium" if referenced_by else "low",
                "verification_method": "git ls-files plus legacy_removal_gate active/historical reference scan",
            }
        )
    return {
        "schema_version": "legacy_removal_inventory.v1",
        "goal": GOAL_ID,
        "baseline_tag": "v0.6.2-engineering-ready",
        "items": items,
        "summary": {
            "legacy_executable_path_count": len(items),
            "production_legacy_reference_count": len(active_refs),
            "historical_documentation_reference_count": len(historical_refs),
        },
    }


def run_gate(*, write_inventory: bool = False) -> dict[str, Any]:
    files = git_ls_files()
    deleted_files = staged_deleted_files()
    legacy_paths = sorted(path for path in files if is_legacy_executable_path(path))
    active_refs, historical_refs = reference_hits(files)
    inventory_removed_paths = sorted(set(deleted_files + existing_inventory_paths()))
    inventory = build_inventory(files, active_refs, historical_refs, inventory_removed_paths)
    if write_inventory:
        INVENTORY_PATH.write_text(yaml.safe_dump(inventory, allow_unicode=True, sort_keys=False), encoding="utf-8")
    result = {
        "schema_version": "legacy_removal_gate.v1",
        "goal": GOAL_ID,
        "production_legacy_executable_paths": legacy_paths,
        "production_legacy_executable_path_count": len(legacy_paths),
        "production_legacy_references": active_refs,
        "production_legacy_reference_count": len(active_refs),
        "historical_documentation_references": historical_refs,
        "historical_documentation_reference_count": len(historical_refs),
        "inventory": {
            "path": INVENTORY_PATH.relative_to(ROOT).as_posix(),
            "item_count": len(inventory["items"]),
            "written": write_inventory,
        },
    }
    result["status"] = "PASS" if not legacy_paths and not active_refs else "FAIL"
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Verify V0.6.2 legacy removal boundaries.")
    parser.add_argument("--write-inventory", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_gate(write_inventory=args.write_inventory)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
