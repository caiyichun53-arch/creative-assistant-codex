from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.scheduled.post_commit_self_check import main


class PostCommitSelfCheckTests(unittest.TestCase):
    def test_returns_zero_when_everything_is_clean(self) -> None:
        with patch(
            "scripts.core.model_gateway.model_router.ModelRouter.from_file",
            return_value=type("Router", (), {"routes": {"daily_chat": object(), "business_analysis": object(), "writing_generation": object()}})(),
        ), patch(
            "scripts.validation.ops_infra_checklist.run_checklist",
            return_value={"status": "PASS", "checks": []},
        ), patch(
            "scripts.validation.production_data_sanity_check.run_checklist",
            return_value={"status": "SKIPPED", "checks": []},
        ):
            self.assertEqual(main(), 0)

    def test_returns_nonzero_when_routing_is_unexpected(self) -> None:
        with patch(
            "scripts.core.model_gateway.model_router.ModelRouter.from_file",
            return_value=type("Router", (), {"routes": {"daily_chat": object()}})(),
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
            "scripts.core.model_gateway.model_router.ModelRouter.from_file",
            return_value=type("Router", (), {"routes": {"daily_chat": object(), "business_analysis": object(), "writing_generation": object()}})(),
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
