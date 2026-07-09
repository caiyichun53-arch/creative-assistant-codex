from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.validation.dead_goal_chain_gate import (
    ARCHIVE_ROOT,
    DEAD_MODULE_DOTTED_PREFIXES,
    ROOT,
    check_claude_md_no_runtime_claude_claim,
    check_creation_status_disclaimer_present,
    check_dead_chain_not_reachable,
    check_env_example_no_dead_config,
    check_fallback_disabled,
    check_model_positions,
    check_no_active_reference_into_archive,
    check_no_content_workflow_ghost_reference,
    check_no_engineering_execution_route,
    compute_real_reachable_modules,
    run_checklist,
)


class ArchiveExistsTests(unittest.TestCase):
    def test_archive_directory_was_actually_created(self) -> None:
        # Sanity check for the whole gate: if the archive were ever deleted or
        # renamed, every other check in this file would trivially "pass" for
        # the wrong reason (nothing to detect). This pins down that the thing
        # being guarded actually exists on disk.
        self.assertTrue(ARCHIVE_ROOT.is_dir())
        self.assertTrue((ARCHIVE_ROOT / "README.md").is_file())


class DeadChainNotReachableTests(unittest.TestCase):
    def test_real_closure_never_touches_an_archived_module(self) -> None:
        result = check_dead_chain_not_reachable()
        self.assertTrue(result["passed"], result["detail"])

    def test_closure_still_includes_the_known_real_shared_modules(self) -> None:
        # Regression pin for the specific surprise the audit turned up: these
        # four modules have "goal0X" names that look like the retired chain,
        # but are real, transitively-imported dependencies of the production
        # entrypoints (through formal_skill_adapter.py / goal_phase4_external_
        # adapters.py) and must NOT be archived.
        reachable = compute_real_reachable_modules()
        rel = {p.relative_to(ROOT).as_posix() for p in reachable}
        for expected in (
            "scripts/core/persistence/goal01_store.py",
            "scripts/core/scheduler/goal03_scheduler.py",
            "scripts/core/workflow/goal05_workflow.py",
            "scripts/core/research/goal06_formal_research.py",
        ):
            self.assertIn(expected, rel)

    def test_a_reintroduced_dead_import_is_actually_detected(self) -> None:
        # Proves this check has teeth: without patching in a fake edge back
        # into the dead chain, it would trivially pass forever even if the
        # detection logic were broken.
        fake_reachable = {ROOT / "scripts" / "core" / "correction" / "goal10_corrections.py"}
        with patch(
            "scripts.validation.dead_goal_chain_gate.compute_real_reachable_modules",
            return_value=fake_reachable,
        ):
            result = check_dead_chain_not_reachable()
        self.assertFalse(result["passed"])
        self.assertIn("scripts/core/correction/goal10_corrections.py", result["detail"])


class NoActiveReferenceIntoArchiveTests(unittest.TestCase):
    def test_nothing_under_scripts_runtime_skills_or_tests_imports_archive(self) -> None:
        result = check_no_active_reference_into_archive()
        self.assertTrue(result["passed"], result["detail"])

    def test_dead_module_prefixes_all_resolve_under_the_archive(self) -> None:
        # If DEAD_MODULE_DOTTED_PREFIXES and the archive on disk ever drift
        # apart (e.g. someone archives a new module but forgets to add its
        # prefix here), this test files loudly instead of the gate silently
        # stopping covering it.
        for prefix in DEAD_MODULE_DOTTED_PREFIXES:
            parts = prefix.split(".")
            as_dir = ROOT.joinpath(*parts)
            as_file = ROOT.joinpath(*parts).with_suffix(".py")
            self.assertFalse(
                as_dir.exists() or as_file.exists(),
                f"{prefix} still exists in the active tree -- it was supposed to be archived",
            )


class ModelPositionsTests(unittest.TestCase):
    def test_three_positions_present_and_currently_mimo(self) -> None:
        result = check_model_positions()
        self.assertTrue(result["passed"], result["detail"])
        self.assertEqual(
            set(result["detail"]["positions"]),
            {"dialogue_model", "business_model", "writing_model"},
        )

    def test_missing_position_is_detected(self) -> None:
        config_path = ROOT / "config" / "model_routes.yaml"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        del data["model_positions"]["writing_model"]
        with patch("yaml.safe_load", return_value=data):
            result = check_model_positions()
        self.assertFalse(result["passed"])


class FallbackDisabledTests(unittest.TestCase):
    def test_every_real_route_has_fallback_none(self) -> None:
        result = check_fallback_disabled()
        self.assertTrue(result["passed"], result["detail"])

    def test_a_reintroduced_fallback_is_detected(self) -> None:
        fake_data = {
            "model_routes": {
                "writing_generation": {"provider_ref": "mimo_main", "fallback": "gpt_backup"},
            }
        }
        with patch("yaml.safe_load", return_value=fake_data):
            result = check_fallback_disabled()
        self.assertFalse(result["passed"])
        self.assertIn("writing_generation", result["detail"])


class NoEngineeringExecutionRouteTests(unittest.TestCase):
    def test_route_was_actually_removed(self) -> None:
        result = check_no_engineering_execution_route()
        self.assertTrue(result["passed"], result["detail"])


class EnvExampleTests(unittest.TestCase):
    def test_env_example_has_no_dead_creation_llm_keys(self) -> None:
        result = check_env_example_no_dead_config()
        self.assertTrue(result["passed"], result["detail"])


class ClaudeMdTests(unittest.TestCase):
    def test_claude_md_no_longer_claims_creation_runs_on_claude(self) -> None:
        result = check_claude_md_no_runtime_claude_claim()
        self.assertTrue(result["passed"], result["detail"])


class ContentWorkflowGhostReferenceTests(unittest.TestCase):
    def test_traceability_no_longer_cites_a_nonexistent_class(self) -> None:
        result = check_no_content_workflow_ghost_reference()
        self.assertTrue(result["passed"], result["detail"])


class CreationStatusDisclaimerTests(unittest.TestCase):
    def test_agents_md_states_creation_is_not_verified_complete(self) -> None:
        result = check_creation_status_disclaimer_present()
        self.assertTrue(result["passed"], result["detail"])


class RunChecklistTests(unittest.TestCase):
    def test_overall_status_is_pass_on_the_real_repo(self) -> None:
        result = run_checklist()
        self.assertEqual(result["status"], "PASS", result["checks"])


if __name__ == "__main__":
    unittest.main()
