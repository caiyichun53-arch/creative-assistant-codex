"""Isolation checks for Hermes tool registration and intake routing."""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import patch

from scripts.agent_platform.hermes_tool_registry import (
    HermesToolRouter,
    collect_cold_start_parameters,
    get_tool_spec,
    matches_new_cold_start_intent,
    matches_resume_cold_start_intent,
    matches_status_cold_start_intent,
    matches_stop_cold_start_intent,
    registered_tools,
)
from scripts.core.production.stage0_content_core import StateTransitionError


class RecordingColdStartAction:
    carrier_binding_id = "hermes-registration-carrier"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"ready_to_confirm": kwargs["operation"] == "preview"}


class HermesToolRegistryTest(unittest.TestCase):
    @staticmethod
    def context(command: str = "hermes-command-1") -> dict[str, str]:
        return {
            "user_identity": "Hermes用户",
            "hermes_carrier_binding_id": "hermes-registration-carrier",
            "session_identity": "hermes-session-1",
            "command_identity": command,
        }

    @staticmethod
    def complete_arguments(*, operation: str = "preview") -> dict[str, Any]:
        return {
            "operation": operation,
            "domain_name": "Hermes登记领域",
            "owned_account": {"display_name": "自营", "external_account_ref": "douyin:owned"},
            "competitor_accounts": [
                {"display_name": f"对标{i}", "external_account_ref": f"douyin:competitor-{i}"}
                for i in range(20)
            ],
        }

    def test_cold_start_action_is_registered_with_trigger_and_confirmation_contract(self) -> None:
        registry = registered_tools()
        self.assertIn("cold_start_onboarding", registry)
        spec = get_tool_spec("cold_start_onboarding")
        self.assertEqual(spec["business_system"], "Creation Assistant")
        self.assertNotIn("business_system", spec["input_schema"]["properties"])
        self.assertEqual(spec["intent"], "new_domain_cold_start")
        self.assertEqual(set(spec["operations"]), {"preview", "confirm", "status", "stop", "resume", "review_tags", "review_content_types", "review_domain_boundary"})
        self.assertTrue(spec["operations"]["confirm"]["requires_explicit_user_confirmation"])
        self.assertIn("用户要求开始、启动或配置一个新的领域冷启动", spec["trigger_conditions"])

    def test_natural_language_intent_hint_is_narrow(self) -> None:
        self.assertTrue(matches_new_cold_start_intent("请开始一个新的泛科普冷启动"))
        self.assertTrue(matches_new_cold_start_intent("create a new cold start"))
        self.assertFalse(matches_new_cold_start_intent("继续当前冷启动运行"))
        self.assertFalse(matches_new_cold_start_intent("请给我一个候选选题"))
        self.assertTrue(matches_resume_cold_start_intent("继续当前冷启动运行"))
        self.assertTrue(matches_resume_cold_start_intent("继续上次冷启动"))
        self.assertTrue(matches_resume_cold_start_intent("恢复冷启动"))
        self.assertTrue(matches_status_cold_start_intent("查看当前冷启动进度"))
        self.assertTrue(matches_status_cold_start_intent("冷启动现在怎么样"))
        self.assertTrue(matches_stop_cold_start_intent("停止当前冷启动"))
        self.assertTrue(matches_stop_cold_start_intent("请暂停冷启动"))

    def test_parameter_collection_asks_only_for_missing_inputs(self) -> None:
        result = collect_cold_start_parameters({"domain_name": "一个领域"})
        self.assertFalse(result["complete_for_preview"])
        self.assertEqual(
            [item["field"] for item in result["missing"]],
            ["owned_account", "competitor_accounts"],
        )
        self.assertEqual(result["missing"][1]["remaining"], 20)

        partial = self.complete_arguments()
        partial["competitor_accounts"] = partial["competitor_accounts"][:17]
        result = collect_cold_start_parameters(partial)
        self.assertEqual(result["missing"], [{
            "field": "competitor_accounts",
            "question": "还需要提供3个对标账号。",
            "remaining": 3,
        }])

    def test_preview_is_dispatched_and_result_is_returned(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments=self.complete_arguments(),
            context=self.context(),
        )
        self.assertEqual(response["status"], "completed")
        self.assertTrue(response["result"]["ready_to_confirm"])
        self.assertEqual(action.calls[0]["operation"], "preview")
        self.assertEqual(response["context"]["user_identity"], "Hermes用户")
        self.assertEqual(response["context"]["command_identity"], "hermes-command-1")

    def test_resume_is_dispatched_without_collecting_new_configuration(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={"operation": "resume", "cold_start_id": "cold-start-resume-1"},
            context=self.context("hermes-resume-1"),
        )
        self.assertEqual(response["status"], "completed")
        self.assertEqual(action.calls[0]["operation"], "resume")
        self.assertEqual(action.calls[0]["configuration"], {})
        self.assertEqual(action.calls[0]["command_id"], "hermes-resume-1")

    def test_missing_parameters_do_not_call_bridge(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={"operation": "preview", "domain_name": "只有领域"},
            context=self.context(),
        )
        self.assertEqual(response["status"], "needs_input")
        self.assertEqual(action.calls, [])

    def test_incomplete_intake_does_not_require_internal_context_or_call_bridge(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={"operation": "preview", "domain_name": "only-domain"},
            context={},
        )
        self.assertEqual(response["status"], "needs_input")
        self.assertEqual(response["next_action"], "ask_user")
        self.assertEqual(action.calls, [])

    def test_incomplete_intake_without_operation_is_a_question_not_a_retryable_error(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={"domain_name": "only-domain"},
            context={},
        )
        self.assertEqual(response["status"], "needs_input")
        self.assertEqual(response["next_action"], "ask_user")
        self.assertEqual(action.calls, [])

    def test_confirm_requires_explicit_user_confirmation(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        arguments = self.complete_arguments(operation="confirm")
        with self.assertRaisesRegex(StateTransitionError, "explicit user confirmation"):
            router.dispatch(
                tool_name="cold_start_onboarding",
                arguments=arguments,
                context=self.context(),
                explicit_user_confirmation=False,
            )
        self.assertEqual(action.calls, [])

    def test_confirm_uses_the_same_bridge_and_context(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        arguments = self.complete_arguments(operation="confirm")
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments=arguments,
            context=self.context("hermes-confirm-1"),
            explicit_user_confirmation=True,
        )
        self.assertEqual(response["status"], "completed")
        self.assertEqual(action.calls[0]["operation"], "confirm")
        self.assertEqual(action.calls[0]["actor"], "Hermes用户")
        self.assertEqual(action.calls[0]["session_ref"], "hermes-session-1")
        self.assertEqual(action.calls[0]["command_id"], "hermes-confirm-1")


    def test_confirm_carries_the_current_dynamic_feishu_target(self) -> None:
        for command_id, thread_id, expected in (
            (
                "hermes-confirm-chat",
                "",
                {"platform": "feishu", "chat_id": "oc_dynamic_chat"},
            ),
            (
                "hermes-confirm-thread",
                "omt_dynamic_thread",
                {
                    "platform": "feishu",
                    "chat_id": "oc_dynamic_chat",
                    "thread_id": "omt_dynamic_thread",
                },
            ),
        ):
            with self.subTest(thread_id=thread_id or None):
                action = RecordingColdStartAction()
                router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
                arguments = self.complete_arguments(operation="confirm")
                context = self.context(command_id)
                context["chat_id"] = "oc_dynamic_chat"
                if thread_id:
                    context["thread_id"] = thread_id

                response = router.dispatch(
                    tool_name="cold_start_onboarding",
                    arguments=arguments,
                    context=context,
                    explicit_user_confirmation=True,
                )

                self.assertEqual(action.calls[0]["notification_target"], expected)
                self.assertEqual(response["context"]["notification_target"], expected)

    def test_static_feishu_environment_never_substitutes_for_dynamic_target(self) -> None:
        static_values = {
            "FEISHU_CHAT_ID": "oc_static_chat",
            "FEISHU_OPEN_ID": "ou_static_open_id",
            "FEISHU_USER_OPEN_ID": "ou_static_user_open_id",
        }
        with patch.dict(os.environ, static_values, clear=True):
            action = RecordingColdStartAction()
            router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
            arguments = self.complete_arguments(operation="confirm")
            response = router.dispatch(
                tool_name="cold_start_onboarding",
                arguments=arguments,
                context=self.context("hermes-confirm-without-target"),
                explicit_user_confirmation=True,
            )

        self.assertIsNone(action.calls[0]["notification_target"])
        self.assertNotIn("notification_target", response["context"])
    def test_review_operations_use_the_same_formal_bridge_and_exact_run(self) -> None:
        for operation in ("review_tags", "review_content_types", "review_domain_boundary"):
            with self.subTest(operation=operation):
                action = RecordingColdStartAction()
                router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
                response = router.dispatch(
                    tool_name="cold_start_onboarding",
                    arguments={
                        "operation": operation,
                        "cold_start_id": "cold-start-step7-1",
                        "decisions": [{"candidate_id": "candidate-1", "decision": "accept"}],
                        "reason": "第7步隔离确认",
                    },
                    context=self.context(f"{operation}-command"),
                    explicit_user_confirmation=True,
                )
                self.assertEqual(response["status"], "completed")
                self.assertEqual(action.calls[0]["operation"], operation)
                self.assertEqual(action.calls[0]["cold_start_id"], "cold-start-step7-1")
                self.assertEqual(
                    action.calls[0]["decisions"],
                    [{"candidate_id": "candidate-1", "decision": "accept"}],
                )
                self.assertEqual(action.calls[0]["reason"], "第7步隔离确认")

    def test_direct_boundary_content_is_forwarded_through_registered_review(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        explicit_boundary = {
            "in_boundary_principles": [{
                "boundary_id": "direct-in",
                "principle": "用户明确提交的生产范围原则。",
                "rationale": "用户决定。",
            }],
            "out_boundary_principles": [{
                "boundary_id": "direct-out",
                "principle": "用户明确提交的排除范围原则。",
                "rationale": "用户决定。",
            }],
            "unknown_topic_rule": {
                "rule": "不确定内容先确认。",
                "uncertain_action": "交给用户确认。",
            },
        }
        response = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={
                "operation": "review_domain_boundary",
                "cold_start_id": "cold-start-direct-boundary-1",
                "explicit_boundary": explicit_boundary,
                "reason": "明确生产边界",
            },
            context=self.context("direct-boundary-command"),
            explicit_user_confirmation=True,
        )
        self.assertEqual(response["status"], "completed")
        self.assertEqual(action.calls[0]["operation"], "review_domain_boundary")
        self.assertIsNone(action.calls[0]["decisions"])
        self.assertEqual(action.calls[0]["explicit_boundary"], explicit_boundary)

    def test_unregistered_tool_cannot_bypass_registered_bridge(self) -> None:
        action = RecordingColdStartAction()
        router = HermesToolRouter(cold_start_action=action)  # type: ignore[arg-type]
        with self.assertRaisesRegex(StateTransitionError, "not registered"):
            router.dispatch(
                tool_name="direct_core_cold_start",
                arguments=self.complete_arguments(),
                context=self.context(),
            )
        self.assertEqual(action.calls, [])


if __name__ == "__main__":
    unittest.main()
