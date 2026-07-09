from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.scheduled.post_commit_self_check import main


class PostCommitSelfCheckTests(unittest.TestCase):
    def test_returns_zero_when_everything_is_clean(self) -> None:
        with patch(
            "scripts.core.staging.verify_goal_v062_phase8_readiness.verify_phase8_readiness",
            return_value={"status": "ENGINEERING_READY", "failures": []},
        ), patch(
            "scripts.validation.ops_infra_checklist.run_checklist",
            return_value={"status": "PASS", "checks": []},
        ), patch(
            "scripts.validation.production_data_sanity_check.run_checklist",
            return_value={"status": "SKIPPED", "checks": []},
        ):
            self.assertEqual(main(), 0)

    def test_returns_nonzero_when_readiness_gate_is_not_ready(self) -> None:
        with patch(
            "scripts.core.staging.verify_goal_v062_phase8_readiness.verify_phase8_readiness",
            return_value={"status": "ENGINEERING_NOT_READY", "failures": ["clean_room"]},
        ), patch(
            "scripts.validation.ops_infra_checklist.run_checklist",
            return_value={"status": "PASS", "checks": []},
        ), patch(
            "scripts.validation.production_data_sanity_check.run_checklist",
            return_value={"status": "SKIPPED", "checks": []},
        ):
            self.assertEqual(main(), 1)

    def test_returns_nonzero_when_production_data_check_fails(self) -> None:
        with patch(
            "scripts.core.staging.verify_goal_v062_phase8_readiness.verify_phase8_readiness",
            return_value={"status": "ENGINEERING_READY", "failures": []},
        ), patch(
            "scripts.validation.ops_infra_checklist.run_checklist",
            return_value={"status": "PASS", "checks": []},
        ), patch(
            "scripts.validation.production_data_sanity_check.run_checklist",
            return_value={"status": "FAIL", "checks": [{"name": "no_stuck_reverse_prep_hits", "passed": False}]},
        ):
            self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
