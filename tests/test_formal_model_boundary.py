"""Static guard for the formal Core-to-executor model boundary."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN_IMPORT_MARKERS = (
    "scripts.core.model_gateway.configured_provider",
    "scripts.core.model_gateway.goal07_model_gateway",
    "scripts.core.model_gateway.model_router",
)
_FORBIDDEN_CALLS = {
    "build_configured_model_provider",
    "resolve_bound_provider",
    "resolve_bound_route",
    "resolve_frozen_task_route",
    "resolve_hermes_task_binding",
    "resolve_current_hermes_execution_binding",
    "_resolve_discovery_model_route",
    "complete",
    "chat",
    "create_completion",
    "responses_create",
    "generate_content",
}


def _tree(relative_path: str) -> tuple[str, ast.Module]:
    path = ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        target = call.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


class FormalModelBoundaryTests(unittest.TestCase):
    def test_formal_boundary_modules_do_not_import_execution_implementations(self) -> None:
        paths = (
            "scripts/core/formal_business_entrypoints.py",
            "scripts/core/production/domain_boundary_lifecycle.py",
            "scripts/core/production/stage1_daily_operations.py",
            "scripts/core/production/cold_start_onboarding.py",
            "scripts/core/production/cold_start_orchestrator.py",
            "scripts/core/production/live_music_cold_start_preflight.py",
        )
        for relative_path in paths:
            _source, tree = _tree(relative_path)
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imported.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            self.assertFalse(
                any(
                    any(marker in module for marker in _FORBIDDEN_IMPORT_MARKERS)
                    for module in imported
                ),
                relative_path,
            )

    def test_formal_reachable_intelligent_steps_only_call_the_external_boundary(self) -> None:
        functions = (
            (
                "scripts/core/production/domain_boundary_lifecycle.py",
                "build_candidates",
            ),
            (
                "scripts/core/production/stage1b_daily_discovery.py",
                "run_daily_discovery",
            ),
            (
                "scripts/core/production/stage1b_daily_discovery.py",
                "_run_source_to_topic_skill",
            ),
            (
                "scripts/core/production/stage1a_research_plan.py",
                "generate_research_plan",
            ),
            (
                "scripts/core/production/stage1c_content_pipeline.py",
                "generate",
            ),
        )
        for relative_path, function_name in functions:
            _source, tree = _tree(relative_path)
            called = _called_names(_function(tree, function_name))
            self.assertFalse(
                called & _FORBIDDEN_CALLS,
                f"{relative_path}:{function_name} calls {sorted(called & _FORBIDDEN_CALLS)}",
            )


    def test_direct_model_paths_are_removed_for_tests_and_production(self) -> None:
        import importlib.util
        import inspect
        from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
        from scripts.core.model_gateway import formal_skill_adapter
        for name in ("model_router", "configured_provider", "hermes_model_provider", "codex_app_server_provider", "goal07_model_gateway"):
            self.assertIsNone(importlib.util.find_spec("scripts.core.model_gateway." + name))
        self.assertFalse((ROOT / "config" / "model_routes.yaml").exists())
        self.assertFalse(hasattr(formal_skill_adapter, "FormalBusinessSkillAdapter"))
        for name in ("prepare_discovery_model_request", "persist_discovery_model_envelope", "_resolve_discovery_model_route", "prepare_atomic_skill_binding", "complete_node_from_model"):
            self.assertFalse(hasattr(Stage0ContentProductionCore, name), name)
        parameters = inspect.signature(Stage0ContentProductionCore.create_discovery_input_assembly).parameters
        self.assertNotIn("model_route", parameters)
        self.assertNotIn("external_execution", parameters)


if __name__ == "__main__":
    unittest.main()
