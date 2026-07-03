from __future__ import annotations

import unittest

from scripts.core.staging.verify_goal_v062_phase8_readiness import verify_phase8_readiness


class Phase8EngineeringReadinessTests(unittest.TestCase):
    def test_phase8_engineering_readiness_does_not_require_gpt_or_live_activation(self) -> None:
        result = verify_phase8_readiness()

        self.assertEqual(result["status"], "ENGINEERING_READY")
        self.assertEqual(result["engineering_goal_status"], "completed")
        self.assertEqual(result["production_activation_status"], "not_started")
        self.assertEqual(result["model_binding"]["model_class"], "mimo")
        self.assertFalse(result["model_binding"]["fallback_enabled"])
        self.assertFalse(result["safety_summary"]["gpt_called"])
        self.assertFalse(result["safety_summary"]["real_platform_collection_started"])
        self.assertFalse(result["safety_summary"]["real_feishu_message_sent"])

