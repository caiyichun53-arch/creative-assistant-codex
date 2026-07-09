from __future__ import annotations

import unittest

from scripts.core.staging.verify_goal_v062_phase7 import verify_phase7_acceptance


class Phase7SyntheticAcceptanceTests(unittest.TestCase):
    def test_phase7_acceptance_matrix_passes_without_live_side_effects(self) -> None:
        result = verify_phase7_acceptance(run_postgres_smoke=False)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["failed_checks"], [])
        for name, passed in result["checks"].items():
            self.assertTrue(passed, name)
        safety = result["details"]["safety"]
        self.assertFalse(safety["old_data_read"])
        self.assertFalse(safety["real_platform_collection"])
        self.assertFalse(safety["real_feishu_send"])
        self.assertFalse(safety["real_provider_called"])
        self.assertFalse(safety["gpt_called"])
        self.assertFalse(safety["deepseek_called"])
        self.assertFalse(safety["fallback_enabled"])

