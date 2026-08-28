"""Hermes transport bridge for the existing cold-start business entry.

This module deliberately contains no cold-start business rules.  It only keeps
the normalized preview temporarily for the current Hermes session so a later
confirm consumes exactly what the user saw instead of rebuilding it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import StateTransitionError


def collect_cold_start_parameters(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Collect only the formal intake fields before touching onboarding."""
    if not isinstance(arguments, Mapping):
        raise StateTransitionError("Hermes cold-start arguments must be an object")

    missing: list[dict[str, Any]] = []
    domain = str(arguments.get("domain_name") or "").strip()
    if not domain:
        missing.append({"field": "domain_name", "question": "请提供新领域名称。"})

    owned = arguments.get("owned_account")
    if not isinstance(owned, Mapping) or not owned:
        missing.append({"field": "owned_account", "question": "请提供一个自营账号。"})

    competitors = arguments.get("competitor_accounts")
    if not isinstance(competitors, list):
        missing.append({
            "field": "competitor_accounts",
            "question": "请提供20个对标账号。",
            "remaining": 20,
        })
    elif len(competitors) < 20:
        remaining = 20 - len(competitors)
        missing.append({
            "field": "competitor_accounts",
            "question": f"还需要提供{remaining}个对标账号。",
            "remaining": remaining,
        })
    elif len(competitors) > 20:
        missing.append({
            "field": "competitor_accounts",
            "question": "对标账号必须恰好20个，请删减后再继续。",
            "provided_count": len(competitors),
        })

    questions = "；".join(item["question"] for item in missing)
    return {
        "complete_for_preview": not missing,
        "missing": missing,
        "next_action": "preview" if not missing else "ask_user",
        "message": "输入已齐全，可以生成preview。" if not missing else questions,
        "provided": {
            "domain_name": bool(domain),
            "owned_account": isinstance(owned, Mapping) and bool(owned),
            "competitor_account_count": len(competitors) if isinstance(competitors, list) else 0,
        },
    }


class HermesColdStartAction:
    """Hermes action backed by the existing cold-start adapter.

    ``preview`` is read-only.  ``confirm`` consumes the normalized preview kept
    for the current transport session.  The bridge never opens a database and
    never calls Core directly.
    """

    ACTION_NAME = "cold_start_onboarding"
    OPERATIONS = frozenset({
        "preview", "confirm", "status", "stop", "resume",
        "review_tags", "review_content_types", "review_domain_boundary",
    })

    def __init__(
        self,
        *,
        adapter: ColdStartHumanDecisionAdapter,
        carrier_binding_id: str,
    ) -> None:
        binding = str(carrier_binding_id or "").strip()
        if not binding:
            raise StateTransitionError("Hermes cold-start action requires a carrier binding")
        self.adapter = adapter
        self.carrier_binding_id = binding
        self._pending_previews: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise StateTransitionError(f"Hermes cold-start action requires {name}")
        return text

    @classmethod
    def _configuration(
        cls,
        *,
        configuration: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if not isinstance(configuration, Mapping):
            raise StateTransitionError("Hermes cold-start configuration must be an object")
        cls._required_text(actor, "the Hermes transport context")
        payload = dict(configuration)
        # Transport identity is validated here but never enters the business payload.
        payload.pop("actor", None)
        payload.setdefault("domain_mode", "create")
        payload.setdefault("platform", "douyin")
        return payload

    @classmethod
    def _missing_intake_result(cls, configuration: Mapping[str, Any]) -> dict[str, Any] | None:
        collection = collect_cold_start_parameters(configuration)
        if collection["complete_for_preview"]:
            return None
        return {
            "status": "needs_input",
            "next_action": "ask_user",
            "message": collection["message"],
            "missing": collection["missing"],
            "provided": collection["provided"],
        }

    def _assert_interaction_allowed(
        self,
        *,
        operation: str,
        actor: str,
        cold_start_id: str | None,
        explicit_user_confirmation: bool,
        explicit_boundary: Mapping[str, Any] | None = None,
        trusted_internal_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Delegate all state-sensitive operation checks to Core."""

        validator = getattr(self.adapter, "validate_cold_start_operation", None)
        if callable(validator):
            return validator(
                operation=operation,
                actor=actor,
                cold_start_id=cold_start_id,
                explicit_user_confirmation=explicit_user_confirmation,
                explicit_boundary=explicit_boundary,
                trusted_internal_context=trusted_internal_context,
            )
        if operation in {"preview", "confirm", "status"}:
            return None
        status_reader = getattr(self.adapter, "current_cold_start_status", None)
        if not callable(status_reader):
            raise StateTransitionError(
                "cold-start transport has no Core operation validator"
            )
        status_kwargs = {
            "actor": actor,
            "cold_start_id": str(cold_start_id or "").strip(),
        }
        if isinstance(trusted_internal_context, Mapping):
            status_kwargs["trusted_internal_context"] = dict(trusted_internal_context)
        snapshot = status_reader(**status_kwargs)
        from scripts.core.formal_business_entrypoints import (
            CreationAssistantFormalBusinessCore,
        )

        return CreationAssistantFormalBusinessCore.validate_cold_start_operation(
            operation=operation,
            cold_start_id=cold_start_id,
            explicit_user_confirmation=explicit_user_confirmation,
            status_snapshot=snapshot,
            explicit_boundary=explicit_boundary,
        )

    def invoke(
        self,
        *,
        operation: str,
        configuration: Mapping[str, Any],
        actor: str,
        session_ref: str,
        command_id: str | None = None,
        reason: str | None = None,
        trusted_internal_context: Mapping[str, Any] | None = None,
        notification_target: Mapping[str, Any] | None = None,
        cold_start_id: str | None = None,
        decisions: list[Mapping[str, Any]] | None = None,
        unknown_topic_rule: Mapping[str, Any] | None = None,
        explicit_boundary: Mapping[str, Any] | None = None,
        explicit_user_confirmation: bool = False,
    ) -> dict[str, Any]:
        """Handle one Hermes action operation without creating parallel state."""
        operation_name = self._required_text(operation, "an operation")
        if operation_name not in self.OPERATIONS:
            raise StateTransitionError(
                "unsupported Hermes cold-start operation; "
                "use preview, confirm, status, stop, resume or a review operation"
            )
        if operation_name == "preview":
            missing_input = self._missing_intake_result(configuration)
            if missing_input is not None:
                return missing_input
        session = self._required_text(session_ref, "the session identity")
        payload = self._configuration(configuration=configuration, actor=actor)
        # The identity supplied by Hermes remains transport context only.
        business_actor = str(actor or "").strip()
        self._assert_interaction_allowed(
            operation=operation_name,
            actor=business_actor,
            cold_start_id=cold_start_id,
            explicit_user_confirmation=explicit_user_confirmation,
            explicit_boundary=explicit_boundary,
            trusted_internal_context=trusted_internal_context,
        )

        if operation_name == "status":
            status_kwargs = {
                "actor": business_actor,
                "cold_start_id": str(cold_start_id or "").strip() or None,
            }
            if isinstance(trusted_internal_context, Mapping):
                status_kwargs["trusted_internal_context"] = dict(trusted_internal_context)
            return self.adapter.current_cold_start_status(**status_kwargs)

        if operation_name == "resume":
            return self.adapter.resume_current_cold_start(
                actor=business_actor,
                trusted_internal_context=(
                    dict(trusted_internal_context)
                    if isinstance(trusted_internal_context, Mapping)
                    else None
                ),
                cold_start_id=str(cold_start_id or "").strip() or None,
            )

        review_actions = {
            "review_tags": ("review_competitor_tag_library", "review_tag_library"),
            "review_content_types": ("review_cold_start_content_types", "review_content_types"),
            "review_domain_boundary": ("review_cold_start_domain_boundary", "review_domain_boundary"),
        }
        if operation_name in review_actions:
            command = self._required_text(command_id, "the command identity")
            run_id = self._required_text(cold_start_id, "the cold-start run identity")
            if operation_name == "review_domain_boundary" and explicit_boundary is not None:
                if not isinstance(explicit_boundary, Mapping):
                    raise StateTransitionError(
                        "explicit production boundary must be an object"
                    )
                if decisions is not None:
                    raise StateTransitionError(
                        "explicit production boundary review cannot also submit candidate decisions"
                    )
                if unknown_topic_rule is not None:
                    raise StateTransitionError(
                        "explicit production boundary must contain its unknown-topic rule"
                    )
            elif not isinstance(decisions, list):
                raise StateTransitionError(
                    "cold-start review requires all decisions"
                )
            review_reason = self._required_text(reason, "the review reason")
            action_name, handler_name = review_actions[operation_name]
            handler = getattr(self.adapter, handler_name)
            review_payload: dict[str, Any] = {
                "cold_start_id": run_id,
                "reason": review_reason,
            }
            if decisions is not None:
                review_payload["decisions"] = [
                    dict(item) for item in decisions if isinstance(item, Mapping)
                ]
            if operation_name == "review_domain_boundary" and explicit_boundary is not None:
                review_payload["explicit_boundary"] = dict(explicit_boundary)
            if operation_name == "review_domain_boundary" and unknown_topic_rule is not None:
                if not isinstance(unknown_topic_rule, Mapping):
                    raise StateTransitionError(
                        "unknown_topic_rule must be an object when supplied"
                    )
                review_payload["unknown_topic_rule"] = dict(unknown_topic_rule)
            decision = FormalHumanDecisionCommand(
                command_id=command,
                carrier_binding_id=self.carrier_binding_id,
                session_ref=session,
                action=action_name,
                target_ref=f"cold_start:{run_id}",
                payload=review_payload,
                actor=business_actor,
                actor_kind="user",
                trusted_internal_context=(
                    dict(trusted_internal_context)
                    if isinstance(trusted_internal_context, Mapping)
                    else None
                ),
            )
            return handler(command=decision)
        if operation_name == "preview":
            result = self.adapter.preview_configuration(payload)
            if bool(result.get("ready_to_confirm")) and isinstance(result.get("normalized"), Mapping):
                self._pending_previews[session] = dict(result["normalized"])
            else:
                self._pending_previews.pop(session, None)
            return result

        if operation_name == "confirm":
            pending = self._pending_previews.get(session)
            if pending is None:
                return {
                    "status": "rejected",
                    "error_type": "no_pending_preview",
                    "message": "当前没有待确认的冷启动预览。",
                }
            result = self.adapter.confirm_configuration(
                configuration=dict(pending),
                transport_actor=business_actor,
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
            self._pending_previews.pop(session, None)
            return result

        if operation_name == "stop":
            stop_reason = self._required_text(reason, "the stop reason")
            return self.adapter.stop_current_cold_start(
                actor=business_actor,
                reason=stop_reason,
                cold_start_id=str(cold_start_id or "").strip() or None,
                trusted_internal_context=(
                    dict(trusted_internal_context)
                    if isinstance(trusted_internal_context, Mapping)
                    else None
                ),
            )

        raise StateTransitionError(f"unsupported cold-start operation: {operation_name}")
