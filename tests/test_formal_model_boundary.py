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
                "scripts/core/production/stage1b_daily_discovery.py",
                "_run_hotspot_to_opportunity_skill",
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

    def test_production_discovery_model_request_is_closed_before_route_resolution(self) -> None:
        source, tree = _tree("scripts/core/production/stage0_content_core.py")
        assembly_function = _function(tree, "create_discovery_input_assembly")
        request_function = _function(tree, "prepare_discovery_model_request")
        assembly_text = ast.get_source_segment(source, assembly_function) or ""
        request_text = ast.get_source_segment(source, request_function) or ""
        self.assertIn('self.data_identity == "production" and not external_execution', assembly_text)
        self.assertIn('self.data_identity == "production":', request_text)
        self.assertIn("formal discovery model requests must be submitted by an external executor", request_text)


if __name__ == "__main__":
    unittest.main()
