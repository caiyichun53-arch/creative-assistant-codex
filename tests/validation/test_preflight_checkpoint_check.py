from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from scripts.validation.preflight_checkpoint_check import (
    check_git_clean,
    check_readiness_gate,
    check_tests,
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


class RunAllChecksTests(unittest.TestCase):
    def test_overall_ready_only_when_all_three_checks_pass(self) -> None:
        with patch("scripts.validation.preflight_checkpoint_check.check_git_clean", return_value={"name": "git", "passed": True}), \
             patch("scripts.validation.preflight_checkpoint_check.check_tests", return_value={"name": "tests", "passed": True}), \
             patch("scripts.validation.preflight_checkpoint_check.check_readiness_gate", return_value={"name": "gate", "passed": True}):
            result = run_all_checks(skip_tests=False)
        self.assertEqual(result["status"], "READY_TO_CHECKPOINT")

    def test_overall_not_ready_when_any_single_check_fails(self) -> None:
        with patch("scripts.validation.preflight_checkpoint_check.check_git_clean", return_value={"name": "git", "passed": False, "detail": ["dirty"]}), \
             patch("scripts.validation.preflight_checkpoint_check.check_tests", return_value={"name": "tests", "passed": True}), \
             patch("scripts.validation.preflight_checkpoint_check.check_readiness_gate", return_value={"name": "gate", "passed": True}):
            result = run_all_checks(skip_tests=False)
        self.assertEqual(result["status"], "NOT_READY")


if __name__ == "__main__":
    unittest.main()
