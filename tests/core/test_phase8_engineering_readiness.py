from __future__ import annotations

import unittest

from scripts.core.staging.verify_goal_v062_phase8_readiness import verify_phase8_readiness


class Phase8EngineeringReadinessTests(unittest.TestCase):
    def test_phase8_readiness_requires_mimo_only_runtime_configuration(self) -> None:
        result = verify_phase8_readiness()

        self.assertEqual(result["status"], "ENGINEERING_READY")
        self.assertEqual(result["engineering_goal_status"], "completed")
        self.assertEqual(result["production_activation_status"], "controlled_pilot_started")
        self.assertNotIn("model_binding", result["failures"])
        self.assertEqual(result["phase8_gate_status"]["matched_profile"], "post_controlled_real_data_pilot")
        self.assertEqual(result["model_binding"]["model_ref_source"], "HERMES_BUSINESS_MODEL_NAME")
        self.assertEqual(result["model_binding"]["model_class_source"], "HERMES_BUSINESS_MODEL_CLASS")
        self.assertEqual(result["model_binding"]["billing_mode"], "subscription")
        self.assertTrue(result["model_binding"]["mimo_configured"])
        self.assertFalse(result["model_binding"]["gpt_configured"])
        self.assertFalse(result["model_binding"]["fallback_enabled"])
        self.assertIn("model_provider_validation", result)
        self.assertFalse(result["safety_summary"]["gpt_called"])
        self.assertTrue(result["safety_summary"]["real_platform_collection_started"])
        self.assertTrue(result["safety_summary"]["approved_real_data_pilot"])
        self.assertFalse(result["safety_summary"]["real_feishu_message_sent"])
