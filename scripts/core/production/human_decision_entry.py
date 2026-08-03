from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError


FORMAL_HUMAN_ACTIONS = frozenset({
    "confirm_formal_topic",
    "record_manual_source",
    "confirm_exploration_direction",
    "approve_research_plan",
    "return_research_plan",
    "record_research_material",
    "advance_content_step",
    "select_discovery_candidate",
    "approve_research_result",
    "return_research_result",
    "approve_content_plan",
    "return_content_plan",
    "accept_experience_candidate",
    "reject_experience_candidate",
    "approve_draft",
    "return_draft",
    "approve_final_content",
    "return_final_content",
    "confirm_cold_start_summary",
    "confirm_cold_start",
    "confirm_cold_start_configuration",
    "start_confirmed_cold_start",
    "run_competitor_breakdown",
    "promote_accepted_competitor_breakdown",
    "review_competitor_tag_library",
    "delete_cold_start_tag",
    "confirm_discovered_account",
    "confirm_search_tag",
    "confirm_voice_profile",
    "start_audio_production",
    "approve_audio",
    "return_audio",
    "change_domain_workflow_mode",
    "register_external_publication",
    "record_publication_observation",
    "prepare_p7_review",
    "confirm_p7_review",
    "review_two_week_tag_library",
})


@dataclass(frozen=True)
class FormalHumanDecisionCommand:
    command_id: str
    carrier_binding_id: str
    session_ref: str
    action: str
    target_ref: str
    payload: dict[str, Any]
    actor: str
    actor_kind: str = "user"


class HumanDecisionCommandService:
    """Carrier-neutral boundary for formal user decisions only.

    A user may express a formal decision in natural conversation or through a
    visual control. Either carrier may submit it here only after its own real
    round trip has been validated.
    """

    def __init__(self, *, core: Stage0ContentProductionCore):
        self.core = core

    def execute(
        self,
        *,
        command: FormalHumanDecisionCommand,
        handler: Callable[[FormalHumanDecisionCommand], dict[str, Any]],
    ) -> dict[str, Any]:
        if command.action not in FORMAL_HUMAN_ACTIONS:
            raise StateTransitionError("this entry accepts formal human decisions only")
        received = self.core.receive_human_decision_command(
            command_id=command.command_id, carrier_binding_id=command.carrier_binding_id,
            session_ref=command.session_ref, action=command.action, target_ref=command.target_ref,
            payload=command.payload, actor=command.actor, actor_kind=command.actor_kind,
        )
        if received.get("status") == "completed":
            return received
        if received.get("status") == "rejected":
            raise StateTransitionError("this formal human command was already rejected and will not auto-retry")
        try:
            result = handler(command)
            if not isinstance(result, dict) or not result:
                raise StateTransitionError("formal human decision handler must return its business result")
        except Exception as exc:
            self.core.finish_human_decision_command(
                command_id=command.command_id, status="rejected", result={},
                error={"error_type": type(exc).__name__, "reason": str(exc)},
            )
            raise
        return self.core.finish_human_decision_command(
            command_id=command.command_id, status="completed", result=result, error={},
        )
