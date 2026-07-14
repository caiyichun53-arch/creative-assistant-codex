from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.validation.dead_goal_chain_gate import (
    ARCHIVE_ROOT,
    DEAD_MODULE_DOTTED_PREFIXES,
    REQUIRED_INDEPENDENT_TEST_COVERAGE,
    ROOT,
    check_active_verify_scripts_dont_import_dead_chain,
    check_archive_not_reachable_from_real_entrypoints,
    check_agents_md_declares_codex_single_entry,
    check_effective_design_baseline_is_authoritative,
    check_dead_chain_not_reachable,
    check_env_example_no_dead_config,
    check_fallback_disabled,
    check_goal05_workflow_orchestrator_has_independent_coverage,
    check_model_positions,
    check_no_active_reference_into_archive,
    check_no_content_workflow_ghost_reference,
    check_no_engineering_execution_route,
    compute_real_reachable_modules,
    run_checklist,
)


class ArchiveOptionalTests(unittest.TestCase):
    def test_archive_is_not_required_to_prove_active_code_is_clean(self) -> None:
        # Historical materials may be moved outside the repository. The gate
        # must keep rejecting imports into the archive path when it exists,
        # without treating the archive itself as a runtime prerequisite.
        result = check_archive_not_reachable_from_real_entrypoints()
        self.assertTrue(result["passed"], result["detail"])


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

    def test_scripts_core_runtime_is_covered_by_the_dead_prefix_list(self) -> None:
        # 2026-07-11: scripts/core/runtime/ was confirmed DEAD_GOAL_CHAIN by
        # this same closure check and archived. Pin it here so a later commit
        # can't quietly remove "scripts.core.runtime" from the prefix list.
        self.assertIn("scripts.core.runtime", DEAD_MODULE_DOTTED_PREFIXES)
        reachable = compute_real_reachable_modules()
        rel = {p.relative_to(ROOT).as_posix() for p in reachable}
        self.assertFalse(any(path.startswith("scripts/core/runtime/") for path in rel))


class ArchiveNotReachableFromRealEntrypointsTests(unittest.TestCase):
    def test_no_real_reachable_module_resolves_under_archive(self) -> None:
        result = check_archive_not_reachable_from_real_entrypoints()
        self.assertTrue(result["passed"], result["detail"])

    def test_a_path_under_archive_in_the_closure_is_actually_detected(self) -> None:
        # Path-based detection must have teeth independent of
        # DEAD_MODULE_DOTTED_PREFIXES -- prove it catches an archived path
        # even without relying on that list.
        fake_reachable = {ARCHIVE_ROOT / "scripts_core_correction" / "goal10_corrections.py"}
        with patch(
            "scripts.validation.dead_goal_chain_gate.compute_real_reachable_modules",
            return_value=fake_reachable,
        ):
            result = check_archive_not_reachable_from_real_entrypoints()
        self.assertFalse(result["passed"])
        self.assertTrue(any("archive/dead_goal_chain_20260709" in item for item in result["detail"]))


class Goal05WorkflowOrchestratorCoverageTests(unittest.TestCase):
    def test_required_coverage_mapping_points_at_goal05_workflow(self) -> None:
        self.assertIn(
            "scripts.core.workflow.goal05_workflow.Goal05WorkflowOrchestrator",
            REQUIRED_INDEPENDENT_TEST_COVERAGE,
        )

    def test_the_real_test_file_exists_and_imports_the_real_class(self) -> None:
        result = check_goal05_workflow_orchestrator_has_independent_coverage()
        self.assertTrue(result["passed"], result["detail"])

    def test_a_missing_test_file_is_actually_detected(self) -> None:
        fake_mapping = {
            "scripts.core.workflow.goal05_workflow.Goal05WorkflowOrchestrator": ROOT
            / "tests"
            / "core"
            / "test_this_file_does_not_exist.py"
        }
        with patch(
            "scripts.validation.dead_goal_chain_gate.REQUIRED_INDEPENDENT_TEST_COVERAGE",
            fake_mapping,
        ):
            result = check_goal05_workflow_orchestrator_has_independent_coverage()
        self.assertFalse(result["passed"])
        self.assertIn("does not exist", str(result["detail"]))

    def test_a_test_file_that_never_imports_the_real_class_is_detected(self) -> None:
        # Must be a real path under ROOT (the checked function does
        # path.relative_to(ROOT) for its report), so write a scratch file
        # inside tests/core/ and clean it up afterward rather than using a
        # tempdir outside the repo.
        fake_test = ROOT / "tests" / "core" / "_scratch_fake_goal05_test.py"
        fake_test.write_text(
            "import unittest\n\n"
            "class FakeTests(unittest.TestCase):\n"
            "    def test_something(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.addCleanup(fake_test.unlink)
        fake_mapping = {
            "scripts.core.workflow.goal05_workflow.Goal05WorkflowOrchestrator": fake_test
        }
        with patch(
            "scripts.validation.dead_goal_chain_gate.REQUIRED_INDEPENDENT_TEST_COVERAGE",
            fake_mapping,
        ):
            result = check_goal05_workflow_orchestrator_has_independent_coverage()
        self.assertFalse(result["passed"])
        self.assertIn("does not import", str(result["detail"]))


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


class ActiveVerifyScriptsDontImportDeadChainTests(unittest.TestCase):
    def test_no_active_verify_goal_script_imports_the_dead_chain(self) -> None:
        # 2026-07-11: verify_goal_05.py and verify_goal_06.py were found
        # importing the already-archived RuntimeHost and had to be archived
        # themselves in a follow-up pass -- they weren't caught by pytest
        # since they're standalone scripts, not part of the suite. This check
        # exists so the next archival can't silently repeat that.
        result = check_active_verify_scripts_dont_import_dead_chain()
        self.assertTrue(result["passed"], result["detail"])

    def test_a_verify_script_importing_the_dead_chain_is_actually_detected(self) -> None:
        scratch_dir = ROOT / "scripts" / "core" / "_scratch_verify_test_dir"
        scratch_dir.mkdir(exist_ok=True)
        scratch_file = scratch_dir / "verify_goal_99.py"
        scratch_file.write_text(
            "from scripts.core.correction.goal10_corrections import CorrectionRegistrationCommand\n",
            encoding="utf-8",
        )
        self.addCleanup(scratch_dir.rmdir)
        self.addCleanup(scratch_file.unlink)

        result = check_active_verify_scripts_dont_import_dead_chain()

        self.assertFalse(result["passed"])
        self.assertTrue(
            any("verify_goal_99.py" in item for item in result["detail"]),
            result["detail"],
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


class CodexSingleEntryTests(unittest.TestCase):
    def test_agents_md_declares_codex_as_the_only_execution_entry(self) -> None:
        result = check_agents_md_declares_codex_single_entry()
        self.assertTrue(result["passed"], result["detail"])


class ContentWorkflowGhostReferenceTests(unittest.TestCase):
    def test_traceability_no_longer_cites_a_nonexistent_class(self) -> None:
        result = check_no_content_workflow_ghost_reference()
        self.assertTrue(result["passed"], result["detail"])


class EffectiveBaselineTests(unittest.TestCase):
    def test_agents_md_names_the_effective_design_baseline(self) -> None:
        result = check_effective_design_baseline_is_authoritative()
        self.assertTrue(result["passed"], result["detail"])


class RunChecklistTests(unittest.TestCase):
    def test_overall_status_is_pass_on_the_real_repo(self) -> None:
        result = run_checklist()
        self.assertEqual(result["status"], "PASS", result["checks"])


if __name__ == "__main__":
    unittest.main()
