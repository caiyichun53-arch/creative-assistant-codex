from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scripts.validation.preflight_checkpoint_check import (
    check_dead_goal_chain,
    check_git_clean,
    check_ops_infra,
    check_production_data,
    check_readiness_gate,
    check_tests,
    check_unsourced_constants,
    run_all_checks,
)


def _fake_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class CheckGitCleanTests(unittest.TestCase):
    def test_passes_when_git_status_is_empty(self) -> None:
        with patch("scripts.validation.preflight_checkpoint_check.subprocess.run", return_value=_fake_completed("")):
            result = check_git_clean()
        self.assertTrue(result["passed"])

    def test_fails_and_lists_dirty_files_when_git_status_is_not_empty(self) -> None:
        dirty = " M some/file.py\n?? some/new_file.py\n"
        with patch("scripts.validation.preflight_checkpoint_check.subprocess.run", return_value=_fake_completed(dirty)):
            result = check_git_clean()
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["detail"]), 2)


class CheckTestsTests(unittest.TestCase):
    def test_skip_flag_short_circuits_without_running_pytest(self) -> None:
        with patch("scripts.validation.preflight_checkpoint_check.subprocess.run") as mock_run:
            result = check_tests(skip=True)
        mock_run.assert_not_called()
        self.assertTrue(result["passed"])

    def test_reports_failure_when_pytest_exits_nonzero(self) -> None:
        with patch("scripts.validation.preflight_checkpoint_check.subprocess.run", return_value=_fake_completed("2 failed, 5 passed", returncode=1)):
            result = check_tests(skip=False)
        self.assertFalse(result["passed"])


class CheckReadinessGateTests(unittest.TestCase):
    def test_passes_when_gate_reports_engineering_ready(self) -> None:
        with patch(
            "scripts.core.staging.verify_goal_v062_phase8_readiness.verify_phase8_readiness",
            return_value={"status": "ENGINEERING_READY", "failures": []},
        ):
            result = check_readiness_gate()
        self.assertTrue(result["passed"])

    def test_fails_and_reports_failure_names_when_gate_is_not_ready(self) -> None:
        with patch(
            "scripts.core.staging.verify_goal_v062_phase8_readiness.verify_phase8_readiness",
            return_value={"status": "ENGINEERING_NOT_READY", "failures": ["clean_room"]},
        ):
            result = check_readiness_gate()
        self.assertFalse(result["passed"])
        self.assertEqual(result["detail"], ["clean_room"])


class CheckOpsInfraTests(unittest.TestCase):
    def test_passes_when_ops_checklist_is_clean(self) -> None:
        with patch("scripts.validation.ops_infra_checklist.run_checklist", return_value={"status": "PASS", "checks": []}):
            result = check_ops_infra()
        self.assertTrue(result["passed"])

    def test_fails_when_ops_checklist_finds_something(self) -> None:
        with patch(
            "scripts.validation.ops_infra_checklist.run_checklist",
            return_value={"status": "FAIL", "checks": [{"name": "no_hardcoded_absolute_machine_paths", "passed": False, "detail": ["x"]}]},
        ):
            result = check_ops_infra()
        self.assertFalse(result["passed"])


class CheckProductionDataTests(unittest.TestCase):
    def test_passes_when_sanity_check_passes(self) -> None:
        with patch("scripts.validation.production_data_sanity_check.run_checklist", return_value={"status": "PASS", "checks": []}):
            result = check_production_data()
        self.assertTrue(result["passed"])

    def test_passes_when_no_local_database_to_check(self) -> None:
        with patch("scripts.validation.production_data_sanity_check.run_checklist", return_value={"status": "SKIPPED", "checks": []}):
            result = check_production_data()
        self.assertTrue(result["passed"])

    def test_fails_when_something_is_stuck(self) -> None:
        with patch(
            "scripts.validation.production_data_sanity_check.run_checklist",
            return_value={"status": "FAIL", "checks": [{"name": "no_stuck_reverse_prep_hits", "passed": False, "detail": ["x"]}]},
        ):
            result = check_production_data()
        self.assertFalse(result["passed"])


class CheckUnsourcedConstantsTests(unittest.TestCase):
    def test_passes_when_scanner_finds_nothing(self) -> None:
        with patch("scripts.validation.unsourced_constant_check.run_checklist", return_value={"status": "PASS", "unsourced_constants": []}):
            result = check_unsourced_constants()
        self.assertTrue(result["passed"])

    def test_fails_when_scanner_finds_an_ungrounded_constant(self) -> None:
        with patch(
            "scripts.validation.unsourced_constant_check.run_checklist",
            return_value={"status": "FAIL", "unsourced_constants": [{"path": "x.py", "line": 1, "name": "X"}]},
        ):
            result = check_unsourced_constants()
        self.assertFalse(result["passed"])


class CheckDeadGoalChainTests(unittest.TestCase):
    def test_passes_when_gate_reports_pass(self) -> None:
        with patch(
            "scripts.validation.dead_goal_chain_gate.run_checklist",
            return_value={"status": "PASS", "checks": []},
        ):
            result = check_dead_goal_chain()
        self.assertTrue(result["passed"])

    def test_fails_when_gate_reports_fail(self) -> None:
        with patch(
            "scripts.validation.dead_goal_chain_gate.run_checklist",
            return_value={"status": "FAIL", "checks": [{"name": "fallback_disabled_on_every_route", "passed": False, "detail": ["x"]}]},
        ):
            result = check_dead_goal_chain()
        self.assertFalse(result["passed"])


class RunAllChecksTests(unittest.TestCase):
    def _patched(self, **overrides):
        defaults = {
            "check_git_clean": {"name": "git", "passed": True},
            "check_tests": {"name": "tests", "passed": True},
            "check_readiness_gate": {"name": "gate", "passed": True},
            "check_ops_infra": {"name": "ops", "passed": True},
            "check_production_data": {"name": "data", "passed": True},
            "check_unsourced_constants": {"name": "constants", "passed": True},
            "check_dead_goal_chain": {"name": "dead_chain", "passed": True},
        }
        defaults.update(overrides)
        patches = [
            patch(f"scripts.validation.preflight_checkpoint_check.{name}", return_value=value)
            for name, value in defaults.items()
        ]
        return patches

    def test_overall_ready_only_when_all_checks_pass(self) -> None:
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            result = run_all_checks(skip_tests=False)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result["status"], "READY_TO_CHECKPOINT")

    def test_overall_not_ready_when_any_single_check_fails(self) -> None:
        patches = self._patched(check_git_clean={"name": "git", "passed": False, "detail": ["dirty"]})
        for p in patches:
            p.start()
        try:
            result = run_all_checks(skip_tests=False)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result["status"], "NOT_READY")

    def test_overall_not_ready_when_only_production_data_check_fails(self) -> None:
        patches = self._patched(check_production_data={"name": "data", "passed": False, "detail": ["stuck"]})
        for p in patches:
            p.start()
        try:
            result = run_all_checks(skip_tests=False)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(result["status"], "NOT_READY")


if __name__ == "__main__":
    unittest.main()
