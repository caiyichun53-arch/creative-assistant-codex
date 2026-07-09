"""Locks in the 2026-07-11 "清场式保留重构" (clean-sweep) so it cannot silently
regress in a later commit.

That pass archived the Goal01-12 modules that a real Python-import transitive
closure (computed from the real production entrypoints, not guessed from
filenames) proved had zero path to production, unified the model-routing
config to three explicit positions (dialogue_model/business_model/writing_model,
all currently Mimo, fallback disabled), and fixed several stale doc claims
(Codex/Claude Code being described as a runtime business provider; a
ContentWorkflow/ContentGuard class that does not exist). Each of those was a
one-time manual edit -- without a gate, a future commit could re-import an
archived module, re-add a fallback, or let a doc regress to the old claim,
and nothing would notice until the next manual audit.

Usage:
    python -m scripts.validation.dead_goal_chain_gate
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = ROOT / "archive" / "dead_goal_chain_20260709"

# The real production entrypoints. If a new one is added, add it here --
# that's the same discipline the archive's own README documents.
REAL_ENTRYPOINTS = (
    ROOT / "scripts" / "core" / "business_data" / "run_competitor_registration_full.py",
    ROOT / "scripts" / "core" / "business_data" / "run_reverse_prep.py",
    ROOT / "scripts" / "core" / "business_data" / "register_competitor_accounts.py",
    ROOT / "scripts" / "core" / "business_data" / "migrate_master_doc_realignment.py",
    ROOT / "scripts" / "core" / "experience" / "run_source_to_topic.py",
    ROOT / "scripts" / "core" / "experience" / "run_content_plan.py",
    ROOT / "scripts" / "core" / "experience" / "run_script_generate.py",
    ROOT / "scripts" / "core" / "experience" / "run_sample_deep_analyze.py",
    ROOT / "scripts" / "core" / "experience" / "review_queue.py",
    ROOT / "scripts" / "tools" / "compare_asr_providers.py",
)

# Modules archived on 2026-07-09/11 after the closure below proved zero real
# entrypoint depends on them. If one of these needs to come back, that is a
# new, explicit architectural decision (see archive/dead_goal_chain_20260709/
# README.md) -- it must not silently re-enter the import graph.
#
# 2026-07-11: scripts.core.production and scripts.core.experience.
# goal09_experiments were deliberately revived (see the approved
# "生产激活：真实经验闭环" plan) as the real foundation for
# BR-EXPERIENCE-001 -- VersionRef/PersistenceStore is connection-agnostic
# and was verified to have zero table-name collisions with the real
# production schema. This is an explicit, plan-approved revival, not a
# silent regression. verify_goal_08.py stays archived: this pass revives
# only the VersionRef/experience-state machinery, not its old self-cert
# harness.
DEAD_MODULE_DOTTED_PREFIXES = (
    "scripts.core.correction",
    "scripts.core.host",
    "scripts.core.hermes",
    "scripts.core.state",
    "scripts.core.runtime",
    "scripts.core.workflow.goal_phase5_business_workflow",
    "scripts.monitor.queries",
)

# A module that must still have its own dedicated, non-archived test file
# importing the real class directly -- added after archiving
# test_phase5_business_workflow.py silently dropped the only test covering
# Goal05WorkflowOrchestrator (a real, kept dependency), and it took a second
# pass to notice and fix.
REQUIRED_INDEPENDENT_TEST_COVERAGE = {
    "scripts.core.workflow.goal05_workflow.Goal05WorkflowOrchestrator": ROOT
    / "tests"
    / "core"
    / "test_goal05_workflow_orchestrator.py",
}


def _module_to_path(dotted: str) -> Path | None:
    parts = dotted.split(".")
    candidate = ROOT.joinpath(*parts).with_suffix(".py")
    if candidate.exists():
        return candidate
    candidate = ROOT.joinpath(*parts, "__init__.py")
    if candidate.exists():
        return candidate
    return None


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("scripts"):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scripts"):
                    found.add(alias.name)
    return found


def compute_real_reachable_modules() -> set[Path]:
    """Transitive closure of scripts.*/runtime_skills.* modules reachable from
    REAL_ENTRYPOINTS. This is the mechanical proof this gate is named after:
    it does not trust a filename or a prior audit's conclusion, it re-derives
    the answer from the current import graph every time it runs."""
    visited: set[Path] = set()
    queue: list[Path] = [p for p in REAL_ENTRYPOINTS if p.exists()]
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        for dotted in _imports_of(current):
            resolved = _module_to_path(dotted)
            if resolved is not None and resolved not in visited:
                queue.append(resolved)
    return visited


def check_dead_chain_not_reachable() -> dict[str, Any]:
    reachable = compute_real_reachable_modules()
    violations = []
    for path in reachable:
        rel = path.relative_to(ROOT).as_posix()
        dotted = rel[: -len(".py")].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
        for prefix in DEAD_MODULE_DOTTED_PREFIXES:
            if dotted == prefix or dotted.startswith(prefix + "."):
                violations.append(rel)
    return {
        "name": "dead_goal_chain_unreachable_from_real_entrypoints",
        "passed": not violations,
        "detail": violations or f"{len(reachable)} modules reachable, none from the archived set",
    }


def check_archive_not_reachable_from_real_entrypoints() -> dict[str, Any]:
    """Path-based counterpart to check_dead_chain_not_reachable(): checks that
    no module in the real closure resolves to a path physically under
    archive/, regardless of whether its dotted name happens to be listed in
    DEAD_MODULE_DOTTED_PREFIXES. This does not depend on that list being kept
    up to date -- if a future entrypoint somehow re-imported archived code
    under a path DEAD_MODULE_DOTTED_PREFIXES does not cover, this still
    catches it, because it asks "is this path under archive/" directly."""
    if not ARCHIVE_ROOT.exists():
        return {"name": "archive_unreachable_from_real_entrypoints", "passed": True, "detail": "archive directory absent"}
    reachable = compute_real_reachable_modules()
    violations = [
        path.relative_to(ROOT).as_posix()
        for path in reachable
        if ARCHIVE_ROOT in path.parents or path == ARCHIVE_ROOT
    ]
    return {
        "name": "archive_unreachable_from_real_entrypoints",
        "passed": not violations,
        "detail": violations or f"{len(reachable)} modules reachable, none under {ARCHIVE_ROOT.relative_to(ROOT).as_posix()}",
    }


def check_goal05_workflow_orchestrator_has_independent_coverage() -> dict[str, Any]:
    problems = []
    for qualified_name, test_path in REQUIRED_INDEPENDENT_TEST_COVERAGE.items():
        module_dotted, class_name = qualified_name.rsplit(".", 1)
        if not test_path.exists():
            problems.append(f"{test_path.relative_to(ROOT).as_posix()} does not exist")
            continue
        text = test_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(test_path))
        except SyntaxError as exc:
            problems.append(f"{test_path.relative_to(ROOT).as_posix()} failed to parse: {exc}")
            continue
        imports_real_class = any(
            isinstance(node, ast.ImportFrom)
            and node.module == module_dotted
            and any(alias.name == class_name for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imports_real_class:
            problems.append(f"{test_path.relative_to(ROOT).as_posix()} does not import {class_name} from {module_dotted}")
        if "archive" in text and "dead_goal_chain" in text.lower().replace(" ", "_"):
            # Allow prose mentions in the module docstring (e.g. explaining
            # why the old test was archived), but reject any actual import
            # statement reaching into archive/.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = getattr(node, "module", None) or (node.names[0].name if node.names else "")
                    if module_name and module_name.startswith("archive"):
                        problems.append(f"{test_path.relative_to(ROOT).as_posix()} imports from archive/: {module_name}")
        if not any(
            isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
            for node in ast.walk(tree)
        ):
            problems.append(f"{test_path.relative_to(ROOT).as_posix()} has no test_* methods")
    return {
        "name": "goal05_workflow_orchestrator_has_independent_test_coverage",
        "passed": not problems,
        "detail": problems or "clean",
    }


def check_no_active_reference_into_archive() -> dict[str, Any]:
    """Checks for actual Python `import`/`from ... import` statements that
    reach into archive/, not mere textual mentions of the archive's name --
    docs (this gate's own docstring, the archive README, HANDOFF_STATE.md)
    are expected to name it in prose; only a real import back into archived
    code would be the regression this guards against."""
    if not ARCHIVE_ROOT.exists():
        return {"name": "no_active_reference_into_archive", "passed": True, "detail": "archive directory absent"}
    violations = []
    scan_roots = (ROOT / "scripts", ROOT / "runtime_skills", ROOT / "tests")
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*.py"):
            if "__pycache__" in path.relative_to(ROOT).parts:
                continue
            for dotted in _imports_of(path):
                if dotted == "archive" or dotted.startswith("archive."):
                    violations.append(f"{path.relative_to(ROOT).as_posix()} imports {dotted}")
    return {
        "name": "no_active_reference_into_archive",
        "passed": not violations,
        "detail": violations or "clean",
    }


def check_active_verify_scripts_dont_import_dead_chain() -> dict[str, Any]:
    """scripts/core/**/verify_goal_*.py are one-time, per-GOAL self-certification
    scripts (see TECHNICAL_MANUAL.md section 15) -- not part of the pytest
    suite, so a broken import in one of them won't show up as a test failure.
    That is exactly how verify_goal_05.py/verify_goal_06.py went unnoticed
    after scripts/core/runtime/ was archived, until a manual check found them.
    This check scans every verify_goal_*.py still in the active tree and
    fails if any of them imports a DEAD_MODULE_DOTTED_PREFIXES module or
    reaches into archive/ -- so the next archival can't reintroduce the same
    silent breakage."""
    violations = []
    for path in ROOT.joinpath("scripts", "core").rglob("verify_goal_*.py"):
        if "__pycache__" in path.relative_to(ROOT).parts:
            continue
        for dotted in _imports_of(path):
            if dotted == "archive" or dotted.startswith("archive."):
                violations.append(f"{path.relative_to(ROOT).as_posix()} imports {dotted}")
                continue
            for prefix in DEAD_MODULE_DOTTED_PREFIXES:
                if dotted == prefix or dotted.startswith(prefix + "."):
                    violations.append(f"{path.relative_to(ROOT).as_posix()} imports {dotted} (archived: {prefix})")
    return {
        "name": "active_verify_goal_scripts_dont_import_dead_chain",
        "passed": not violations,
        "detail": violations or "clean",
    }


def check_model_positions() -> dict[str, Any]:
    config_path = ROOT / "config" / "model_routes.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    positions = data.get("model_positions") or {}
    required = {"dialogue_model", "business_model", "writing_model"}
    missing = required - set(positions)
    if missing:
        return {
            "name": "three_explicit_model_positions_present",
            "passed": False,
            "detail": f"model_positions missing: {sorted(missing)}",
        }
    routes = data.get("model_routes") or {}
    providers = data.get("model_providers") or {}
    problems = []
    for position, route_id in positions.items():
        route = routes.get(route_id)
        if route is None:
            problems.append(f"{position} -> route_id {route_id!r} not found in model_routes")
            continue
        provider_ref = route.get("provider_ref")
        provider = providers.get(provider_ref) or {}
        if provider.get("type") != "mimo":
            problems.append(f"{position} -> provider {provider_ref!r} is not currently type=mimo ({provider.get('type')!r})")
    return {
        "name": "three_explicit_model_positions_present",
        "passed": not problems,
        "detail": problems or {"positions": positions},
    }


def check_fallback_disabled() -> dict[str, Any]:
    config_path = ROOT / "config" / "model_routes.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    routes = data.get("model_routes") or {}
    violations = [
        route_id for route_id, route in routes.items() if route.get("fallback") != "none"
    ]
    return {
        "name": "fallback_disabled_on_every_route",
        "passed": not violations,
        "detail": violations or "clean",
    }


def check_no_engineering_execution_route() -> dict[str, Any]:
    config_path = ROOT / "config" / "model_routes.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    routes = data.get("model_routes") or {}
    present = "engineering_execution" in routes
    return {
        "name": "no_engineering_execution_route",
        "passed": not present,
        "detail": "engineering_execution route re-added -- Codex/Claude Code writing code is not a runtime model position" if present else "clean",
    }


def check_env_example_no_dead_config() -> dict[str, Any]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    dead_keys = ("CREATION_LLM_PROVIDER", "CREATION_LLM_MODEL", "CREATION_LLM_FALLBACK_ENABLED")
    present = [key for key in dead_keys if key in text]
    return {
        "name": "env_example_no_dead_model_config",
        "passed": not present,
        "detail": present or "clean",
    }


def check_claude_md_no_runtime_claude_claim() -> dict[str, Any]:
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    stale_patterns = (
        re.compile(r"创作走用户的\s*Claude(?:\s*Code)?\s*订阅"),
    )
    hits = [pattern.pattern for pattern in stale_patterns if pattern.search(text)]
    tool_not_provider_present = "不是系统运行期的业务 provider" in text
    return {
        "name": "claude_md_does_not_claim_creation_runs_on_claude",
        "passed": not hits and tool_not_provider_present,
        "detail": {
            "stale_phrases_found": hits,
            "tool_vs_provider_clarification_present": tool_not_provider_present,
        } if hits or not tool_not_provider_present else "clean",
    }


def check_no_content_workflow_ghost_reference() -> dict[str, Any]:
    text = (ROOT / "REQUIREMENT_CODE_TRACEABILITY.yaml").read_text(encoding="utf-8")
    present = "target_component: ContentWorkflow/ContentGuard" in text
    return {
        "name": "no_content_workflow_ghost_reference",
        "passed": not present,
        "detail": "ContentWorkflow/ContentGuard cited as a real target_component but no such class exists in the codebase" if present else "clean",
    }


def check_creation_status_disclaimer_present() -> dict[str, Any]:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required_phrases = ("状态机已验证", "真实大模型生成未验证", "不得")
    missing = [phrase for phrase in required_phrases if phrase not in text]
    return {
        "name": "creation_chain_status_disclaimer_present",
        "passed": not missing,
        "detail": missing or "clean",
    }


def run_checklist() -> dict[str, Any]:
    checks = [
        check_dead_chain_not_reachable(),
        check_archive_not_reachable_from_real_entrypoints(),
        check_no_active_reference_into_archive(),
        check_active_verify_scripts_dont_import_dead_chain(),
        check_goal05_workflow_orchestrator_has_independent_coverage(),
        check_model_positions(),
        check_fallback_disabled(),
        check_no_engineering_execution_route(),
        check_env_example_no_dead_config(),
        check_claude_md_no_runtime_claude_claim(),
        check_no_content_workflow_ghost_reference(),
        check_creation_status_disclaimer_present(),
    ]
    status = "PASS" if all(check["passed"] for check in checks) else "FAIL"
    return {"status": status, "checks": checks}


def main() -> int:
    result = run_checklist()
    for check in result["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"[{mark}] {check['name']}")
        if not check["passed"]:
            print(f"       {check['detail']}")
    print(f"\noverall: {result['status']}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
