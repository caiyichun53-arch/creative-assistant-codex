from __future__ import annotations

import unittest

from scripts.core.staging.verify_goal_v062_phase8_readiness import production_task_status, verify_phase8_readiness


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

    def test_daily_schedule_passes_only_because_status_yaml_declares_it_authorized(self) -> None:
        # 2026-07-08: CreationAssistant_Daily is now a real, enabled Windows
        # Scheduled Task (user-authorized, daily 08:00) -- this gate must not
        # regress to requiring every production task be Disabled/missing
        # unconditionally. It must instead check that PHASE_8_AUTHORIZATION_
        # GATE_STATUS.yaml explicitly declares that specific task "enabled_...".
        result = production_task_status()
        self.assertTrue(result["passed"])
        self.assertEqual(result["tasks"]["CreationAssistant_Daily"], "Ready")
        self.assertTrue(result["declared_authorization"]["CreationAssistant_Daily"].startswith("enabled_"))
        # The Listener task remains un-authorized -- still held to the strict
        # Disabled/missing bar, not swept along by the Daily task's exemption.
        self.assertEqual(result["declared_authorization"]["CreationAssistant_Listener"], "disabled")
        self.assertIn(result["tasks"]["CreationAssistant_Listener"], {"Disabled", "missing"})
