from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
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
    "purge_stale_experience_candidates",
    "prepare_experience_candidate",
    "prepare_experience_candidate_batch",
    "fail_experience_candidate",
    "approve_draft",
    "return_draft",
    "approve_final_content",
    "return_final_content",
    "run_competitor_breakdown",
    "reject_competitor_breakdown_quality",
    "exclude_competitor_breakdown",
    "promote_accepted_competitor_breakdown",
    "review_competitor_tag_library",
    "review_cold_start_content_types",
    "review_cold_start_domain_boundary",
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
    "run_daily_operations",
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
    # Runtime-only gateway context. It is not written to the formal command
    # table; the carrier binding and command context remain the durable audit.
    trusted_internal_context: dict[str, Any] | None = None


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
            trusted_internal_context=command.trusted_internal_context,
        )
        if received.get("status") == "completed":
            replayed = dict(received)
            raw_result = replayed.get("result_json")
            try:
                replayed["result"] = json.loads(raw_result) if isinstance(raw_result, str) else {}
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateTransitionError(
                    "stored human decision result is not valid JSON"
                ) from exc
            return replayed
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


class ColdStartStopService:
    """The single business stop service used by every caller."""

    def __init__(self, *, onboarding: ColdStartOnboardingService) -> None:
        self.onboarding = onboarding

    def execute(
        self,
        *,
        actor: str,
        reason: str,
        cold_start_id: str | None,
        trusted_internal_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self.onboarding.stop_current_cold_start(
            actor=actor,
            reason=reason,
            cold_start_id=cold_start_id,
            trusted_internal_context=trusted_internal_context,
        )

class ColdStartHumanDecisionAdapter:
    """Thin carrier adapter for the existing cold-start onboarding service.

    Preview is a read-only business request. Confirmation consumes the
    normalized preview supplied by the transport session and then delegates
    directly to the existing onboarding service.
    """

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        config_dir: Path | None = None,
        preflight_environment: dict[str, str] | None = None,
        task_model_resolver: Any | None = None,
        execution_registration_service: Any | None = None,
        background_execution_launcher: Any | None = None,
        background_execution_inspector: Any | None = None,
        background_execution_stopper: Any | None = None,
        background_notification_target_reader: Any | None = None,
    ) -> None:
        self.onboarding = ColdStartOnboardingService(
            core=core,
            config_dir=config_dir,
            preflight_environment=preflight_environment,
            execution_registration_service=execution_registration_service,
            task_model_resolver=task_model_resolver,
            background_execution_launcher=background_execution_launcher,
            background_execution_inspector=background_execution_inspector,
            background_execution_stopper=background_execution_stopper,
            background_notification_target_reader=background_notification_target_reader,
        )
        self.stop_service = ColdStartStopService(onboarding=self.onboarding)
        self.decisions = HumanDecisionCommandService(core=core)

    def preview_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the existing onboarding preview without changing formal state."""
        return self.onboarding.preview(payload)

    def confirm_configuration(
        self,
        *,
        configuration: Mapping[str, Any],
        transport_actor: str = "",
        trusted_internal_context: Mapping[str, Any] | None = None,
        notification_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a run from the normalized preview held by the transport session."""
        if not isinstance(configuration, dict):
            raise StateTransitionError(
                "cold-start confirmation requires the normalized preview configuration"
            )
        result = self.onboarding.confirm(
            configuration,
            transport_actor=transport_actor,
            trusted_internal_context=(
                dict(trusted_internal_context)
                if isinstance(trusted_internal_context, Mapping)
                else None
            ),
            notification_target=(
                dict(notification_target)
                if isinstance(notification_target, Mapping)
                else None
            ),
        )
        return dict(result)

    def resume_current_cold_start(
        self,
        *,
        actor: str,
        trusted_internal_context: dict[str, Any] | None = None,
        cold_start_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume one exact run; Feishu identity stays transport-only."""
        return self.onboarding.resume_current_cold_start(
            actor=actor,
            trusted_internal_context=trusted_internal_context,
            cold_start_id=cold_start_id,
        )

    def current_cold_start_status(
        self,
        *,
        actor: str,
        cold_start_id: str | None = None,
        trusted_internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read one exact run; Feishu identity stays transport-only."""
        return self.onboarding.current_cold_start_status(
            actor=actor,
            cold_start_id=cold_start_id,
            trusted_internal_context=trusted_internal_context,
        )

    def validate_cold_start_operation(
        self,
        *,
        operation: str,
        actor: str,
        cold_start_id: str | None,
        explicit_user_confirmation: bool,
        explicit_boundary: Mapping[str, Any] | None = None,
        trusted_internal_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Ask the Core boundary to validate state-sensitive operations."""

        snapshot = None
        if operation not in {"preview", "confirm", "status"}:
            snapshot = self.current_cold_start_status(
                actor=actor,
                cold_start_id=cold_start_id,
                trusted_internal_context=(
                    dict(trusted_internal_context)
                    if isinstance(trusted_internal_context, Mapping)
                    else None
                ),
            )
        from scripts.core.formal_business_entrypoints import (
            CreationAssistantFormalBusinessCore,
        )

        return CreationAssistantFormalBusinessCore(
            core=self.onboarding.core
        ).validate_cold_start_operation(
            operation=operation,
            cold_start_id=cold_start_id,
            explicit_user_confirmation=explicit_user_confirmation,
            status_snapshot=snapshot,
            explicit_boundary=explicit_boundary,
        )

    def stop_current_cold_start(
        self,
        *,
        actor: str,
        reason: str,
        cold_start_id: str | None = None,
        trusted_internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stop directly through the single business stop service."""
        return self.stop_service.execute(
            actor=actor,
            reason=reason,
            cold_start_id=cold_start_id,
            trusted_internal_context=trusted_internal_context,
        )

    def get_tag_library_for_review(self, *, cold_start_id: str) -> dict[str, Any] | None:
        """Read the one current-run tag library for the existing user entry."""
        return self.onboarding.core.get_cold_start_tag_library(cold_start_id=cold_start_id)

    def review_tag_library(self, *, command: FormalHumanDecisionCommand) -> dict[str, Any]:
        """Submit one whole-library user review through the existing command boundary."""
        if command.action != "review_competitor_tag_library":
            raise StateTransitionError(
                "tag-library review requires the existing review_competitor_tag_library action"
            )
        cold_start_id = str(command.payload.get("cold_start_id") or "").strip()
        decisions = command.payload.get("decisions")
        reason = str(command.payload.get("reason") or "").strip()
        if not cold_start_id or not isinstance(decisions, list) or not reason:
            raise StateTransitionError(
                "tag-library review requires the current run, all decisions and a reason"
            )
        result = self.decisions.execute(
            command=command,
            handler=lambda _: self.onboarding.core.review_cold_start_tag_library(
                cold_start_id=cold_start_id,
                decisions=tuple(
                    item for item in decisions if isinstance(item, dict)
                ),
                actor=command.actor,
                actor_kind=command.actor_kind,
                reason=reason,
            ),
        )
        return dict(result.get("result") or {})

    def get_content_type_candidates_for_review(
        self, *, cold_start_id: str
    ) -> dict[str, Any] | None:
        """Read the one current-run content-type candidate set."""
        return self.onboarding.core.get_cold_start_content_type_candidate(
            cold_start_id=cold_start_id
        )

    def review_content_types(
        self, *, command: FormalHumanDecisionCommand
    ) -> dict[str, Any]:
        """Review the whole current-run candidate set through the same entry."""
        if command.action != "review_cold_start_content_types":
            raise StateTransitionError(
                "content-type review requires the existing review_cold_start_content_types action"
            )
        cold_start_id = str(command.payload.get("cold_start_id") or "").strip()
        decisions = command.payload.get("decisions")
        reason = str(command.payload.get("reason") or "").strip()
        if not cold_start_id or not isinstance(decisions, list) or not reason:
            raise StateTransitionError(
                "content-type review requires the current run, all decisions and a reason"
            )
        result = self.decisions.execute(
            command=command,
            handler=lambda _: self.onboarding.core.review_cold_start_content_types(
                cold_start_id=cold_start_id,
                decisions=tuple(item for item in decisions if isinstance(item, dict)),
                actor=command.actor,
                actor_kind=command.actor_kind,
                reason=reason,
                decision_id=command.command_id,
            ),
        )
        return dict(result.get("result") or {})

    def get_domain_boundary_candidates_for_review(
        self, *, cold_start_id: str
    ) -> dict[str, Any] | None:
        """Read the one current-run domain-boundary candidate set."""
        return self.onboarding.core.get_cold_start_domain_boundary_candidate(
            cold_start_id=cold_start_id
        )

    def review_domain_boundary(
        self, *, command: FormalHumanDecisionCommand
    ) -> dict[str, Any]:
        """Review and freeze the whole current-run boundary through the same entry."""
        if command.action != "review_cold_start_domain_boundary":
            raise StateTransitionError(
                "domain-boundary review requires the existing review_cold_start_domain_boundary action"
            )
        cold_start_id = str(command.payload.get("cold_start_id") or "").strip()
        decisions = command.payload.get("decisions")
        reason = str(command.payload.get("reason") or "").strip()
        unknown_topic_rule = command.payload.get("unknown_topic_rule")
        explicit_boundary = command.payload.get("explicit_boundary")
        if not cold_start_id or not reason:
            raise StateTransitionError(
                "domain-boundary review requires the current run and a reason"
            )
        if explicit_boundary is not None and not isinstance(explicit_boundary, dict):
            raise StateTransitionError(
                "explicit production boundary must be an object"
            )
        if explicit_boundary is not None and decisions is not None:
            raise StateTransitionError(
                "explicit production boundary review cannot also submit candidate decisions"
            )
        if explicit_boundary is not None and unknown_topic_rule is not None:
            raise StateTransitionError(
                "explicit production boundary must contain its unknown-topic rule"
            )
        if explicit_boundary is None and not isinstance(decisions, list):
            raise StateTransitionError(
                "domain-boundary review requires all candidate decisions or explicit boundary content"
            )
        if unknown_topic_rule is not None and not isinstance(unknown_topic_rule, dict):
            raise StateTransitionError("unknown_topic_rule must be an object when supplied")
        result = self.decisions.execute(
            command=command,
            handler=lambda _: self.onboarding.core.review_cold_start_domain_boundary(
                cold_start_id=cold_start_id,
                decisions=tuple(
                    item for item in (decisions or []) if isinstance(item, dict)
                ),
                actor=command.actor,
                actor_kind=command.actor_kind,
                reason=reason,
                decision_id=command.command_id,
                unknown_topic_rule=unknown_topic_rule,
                explicit_boundary=explicit_boundary,
            ),
        )
        return dict(result.get("result") or {})
