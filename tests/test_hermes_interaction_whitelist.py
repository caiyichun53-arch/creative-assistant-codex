"""Isolation checks for the Step 9 Hermes interaction whitelist."""

from __future__ import annotations

import unittest
from typing import Any

from scripts.agent_platform.hermes_cold_start_action import HermesColdStartAction
from scripts.agent_platform.hermes_tool_registry import HermesToolRouter
from scripts.core.production.stage0_content_core import StateTransitionError


RUN_ID = "cold-start-whitelist-1"
CARRIER = "hermes-whitelist-carrier"


def snapshot(
    status: str,
    *,
    pending: list[str] | None = None,
    tag_status: str = "awaiting_human_review",
    content_type_status: str = "awaiting_human_decision",
    boundary_status: str = "awaiting_human_review",
    run_id: str = RUN_ID,
) -> dict[str, Any]:
    return {
        "status": status,
        "cold_start_id": run_id,
        "pending_actions": list(pending or []),
        "human_confirmation": {
            "tags": {"status": tag_status},
            "content_types": {"status": content_type_status},
            "production_boundary": {"status": boundary_status},
        },
    }


class GateAdapter:
    carrier_binding_id = CARRIER

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.calls: list[str] = []

    def current_cold_start_status(self, *, actor: str, cold_start_id: str) -> dict[str, Any]:
        del actor
        self.calls.append("status")
        return dict(self.state)

    def preview_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("preview")
        return {"ready_to_confirm": True, "payload": payload}

    def confirm_configuration(self, *, command: Any) -> dict[str, Any]:
        del command
        self.calls.append("confirm")
        return {"status": "confirmed"}

    def stop_current_cold_start(
        self,
        *,
        actor: str,
        reason: str,
        cold_start_id: str | None = None,
        trusted_internal_context: Any = None,
    ) -> dict[str, Any]:
        del actor, reason, cold_start_id, trusted_internal_context
        self.calls.append("stop")
        return {"status": "stopped", "cold_start_id": RUN_ID}

    def resume_current_cold_start(
        self,
        *,
        actor: str,
        trusted_internal_context: Any = None,
        cold_start_id: str | None = None,
    ) -> dict[str, Any]:
        # Keep this signature intentionally strict: command-level dedup keys
        # were removed and must not reappear at the adapter boundary.
        del actor, trusted_internal_context, cold_start_id
        self.calls.append("resume")
        return {"status": "running", "resumed": True, "cold_start_id": RUN_ID}

    def review_tag_library(self, *, command: Any) -> dict[str, Any]:
        del command
        self.calls.append("review_tags")
        return {"status": "accepted", "cold_start_id": RUN_ID}

    def review_content_types(self, *, command: Any) -> dict[str, Any]:
        del command
        self.calls.append("review_content_types")
        return {"status": "frozen", "cold_start_id": RUN_ID}

    def review_domain_boundary(self, *, command: Any) -> dict[str, Any]:
        del command
        self.calls.append("review_domain_boundary")
        return {"status": "frozen", "cold_start_id": RUN_ID}


class HermesInteractionWhitelistTest(unittest.TestCase):
    @staticmethod
    def action(adapter: GateAdapter) -> HermesColdStartAction:
        return HermesColdStartAction(
            adapter=adapter, carrier_binding_id=CARRIER  # type: ignore[arg-type]
        )

    @staticmethod
    def invoke(action: HermesColdStartAction, operation: str, **kwargs: Any) -> dict[str, Any]:
        return action.invoke(
            operation=operation,
            configuration={},
            actor="白名单测试用户",
            session_ref="whitelist-session",
            command_id=f"whitelist-{operation}",
            cold_start_id=RUN_ID,
            explicit_user_confirmation=operation in {
                "stop", "review_tags", "review_content_types", "review_domain_boundary"
            },
            decisions=[{"candidate_id": "candidate-1", "decision": "accepted"}],
            reason="白名单隔离确认",
            **kwargs,
        )

    def test_running_allows_status_and_stop_but_blocks_resume(self) -> None:
        adapter = GateAdapter(snapshot("running"))
        action = self.action(adapter)
        self.invoke(action, "status")
        self.invoke(action, "stop")
        with self.assertRaisesRegex(StateTransitionError, "resume is blocked"):
            self.invoke(action, "resume")

    def test_stopped_and_failed_allow_resume_only(self) -> None:
        for status in ("stopped", "failed"):
            with self.subTest(status=status):
                adapter = GateAdapter(snapshot(status))
                action = self.action(adapter)
                self.invoke(action, "resume")
                with self.assertRaisesRegex(StateTransitionError, "stop is blocked"):
                    self.invoke(action, "stop")

    def test_completed_blocks_resume_and_reviews(self) -> None:
        adapter = GateAdapter(snapshot("completed", pending=["review_tags"]))
        action = self.action(adapter)
        with self.assertRaisesRegex(StateTransitionError, "resume is blocked"):
            self.invoke(action, "resume")
        with self.assertRaisesRegex(StateTransitionError, "review_tags is blocked"):
            self.invoke(action, "review_tags")

    def test_completed_without_boundary_candidate_allows_explicit_boundary_review(self) -> None:
        state = snapshot("completed")
        state["human_confirmation"]["production_boundary"] = None
        adapter = GateAdapter(state)
        action = self.action(adapter)
        result = action.invoke(
            operation="review_domain_boundary",
            configuration={},
            actor="白名单测试用户",
            session_ref="whitelist-session",
            command_id="whitelist-direct-boundary",
            cold_start_id=RUN_ID,
            reason="明确提交生产边界",
            explicit_boundary={
                "in_boundary_principles": [{"boundary_id": "in", "principle": "实际范围", "rationale": "用户决定"}],
                "out_boundary_principles": [{"boundary_id": "out", "principle": "实际排除范围", "rationale": "用户决定"}],
                "unknown_topic_rule": {"rule": "不确定时确认", "uncertain_action": "交给用户"},
            },
            explicit_user_confirmation=True,
        )
        self.assertEqual(result["status"], "frozen")
        self.assertEqual(adapter.calls, ["status", "review_domain_boundary"])

    def test_completed_without_boundary_candidate_still_requires_explicit_content(self) -> None:
        state = snapshot("completed")
        state["human_confirmation"]["production_boundary"] = None
        action = self.action(GateAdapter(state))
        with self.assertRaisesRegex(StateTransitionError, "review_domain_boundary is blocked"):
            self.invoke(action, "review_domain_boundary")

    def test_waiting_human_allows_only_the_current_open_confirmation(self) -> None:
        adapter = GateAdapter(snapshot("waiting_human", pending=["review_tags"]))
        action = self.action(adapter)
        self.invoke(action, "review_tags")
        with self.assertRaisesRegex(StateTransitionError, "review_content_types is not a pending"):
            self.invoke(action, "review_content_types")
        with self.assertRaisesRegex(StateTransitionError, "review_domain_boundary is not a pending"):
            self.invoke(action, "review_domain_boundary")

    def test_confirmed_item_cannot_be_overwritten_by_review(self) -> None:
        adapter = GateAdapter(
            snapshot(
                "waiting_human",
                pending=["review_content_types"],
                content_type_status="frozen",
            )
        )
        with self.assertRaisesRegex(StateTransitionError, "currently open"):
            self.invoke(self.action(adapter), "review_content_types")

    def test_cross_run_or_missing_run_identity_is_blocked(self) -> None:
        adapter = GateAdapter(snapshot("stopped", run_id="other-run"))
        action = self.action(adapter)
        with self.assertRaisesRegex(StateTransitionError, "exact cold_start_id"):
            action.invoke(
                operation="resume",
                configuration={},
                actor="白名单测试用户",
                session_ref="whitelist-session",
                command_id="missing-run",
            )
        with self.assertRaisesRegex(StateTransitionError, "does not resolve"):
            self.invoke(action, "resume")

    def test_ambiguous_operation_is_rejected_without_substitution(self) -> None:
        adapter = GateAdapter(snapshot("stopped"))
        with self.assertRaisesRegex(StateTransitionError, "unsupported"):
            self.action(adapter).invoke(
                operation="continue_or_confirm",
                configuration={},
                actor="白名单测试用户",
                session_ref="whitelist-session",
                command_id="ambiguous-operation",
                cold_start_id=RUN_ID,
            )
        self.assertEqual(adapter.calls, [])

    def test_router_requires_exact_run_and_confirmation_before_bridge(self) -> None:
        adapter = GateAdapter(snapshot("stopped"))
        action = self.action(adapter)
        router = HermesToolRouter(cold_start_action=action)
        context = {
            "user_identity": "白名单测试用户",
            "hermes_carrier_binding_id": CARRIER,
            "session_identity": "whitelist-session",
            "command_identity": "router-test",
        }
        with self.assertRaisesRegex(StateTransitionError, "exact cold_start_id"):
            router.dispatch(
                tool_name="cold_start_onboarding",
                arguments={"operation": "resume"},
                context=context,
            )
        with self.assertRaisesRegex(StateTransitionError, "explicit user confirmation"):
            router.dispatch(
                tool_name="cold_start_onboarding",
                arguments={
                    "operation": "stop",
                    "cold_start_id": RUN_ID,
                    "reason": "需要停止",
                },
                context=context,
            )
        with self.assertRaisesRegex(StateTransitionError, "cannot target"):
            router.dispatch(
                tool_name="cold_start_onboarding",
                arguments={"operation": "preview", "cold_start_id": RUN_ID},
                context=context,
            )

    def test_router_preserves_exact_run_and_confirmation_flags(self) -> None:
        adapter = GateAdapter(snapshot("stopped"))
        action = self.action(adapter)
        router = HermesToolRouter(cold_start_action=action)
        context = {
            "user_identity": "白名单测试用户",
            "hermes_carrier_binding_id": CARRIER,
            "session_identity": "whitelist-session",
            "command_identity": "router-resume",
        }
        result = router.dispatch(
            tool_name="cold_start_onboarding",
            arguments={"operation": "resume", "cold_start_id": RUN_ID},
            context=context,
        )
        self.assertEqual(result["result"]["cold_start_id"], RUN_ID)
        self.assertEqual(adapter.calls, ["status", "resume"])


if __name__ == "__main__":
    unittest.main()
