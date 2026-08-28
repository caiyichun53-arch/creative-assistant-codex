"""Unregistered-account observation and the user-controlled registration handoff."""

from __future__ import annotations

from typing import Any
from scripts.core.production.stage1_competitor_registration import CompetitorRegistrationService

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class AccountObservationService:
    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        registration_service: CompetitorRegistrationService | None = None,
    ):
        self.core = core
        self.registration_service = registration_service

    def observe(self, **observation: Any) -> dict[str, Any]:
        return self.core.observe_unregistered_account(**observation)

    def decide(
        self,
        *,
        account_review_id: str,
        decision: str,
        owned_account_id: str | None,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:

        return self.core.decide_unregistered_account_review(
            account_review_id=account_review_id, decision=decision, owned_account_id=owned_account_id,
            actor=actor, actor_kind="user", reason=reason, idempotency_key=idempotency_key,
        )

    def accept_and_register_incremental(
        self,
        *,
        account_review_id: str,
        owned_account_id: str,
        cold_start_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Accept one observed account and run only its existing registration chain."""
        if self.registration_service is None:
            raise ValueError("incremental registration service is not configured")
        accepted = self.decide(
            account_review_id=account_review_id,
            decision="accepted",
            owned_account_id=owned_account_id,
            actor=actor,
            reason=reason,
            idempotency_key=f"{idempotency_key}:accept",
        )
        account = self.core.conn.execute(
            "SELECT content_account_id, display_name, external_account_ref "
            "FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (accepted["competitor_account_id"], self.core.data_identity),
        ).fetchone()
        if account is None:
            raise RuntimeError("accepted competitor account disappeared before registration")
        return self.registration_service.run_incremental_competitor_registrations(
            cold_start_id=cold_start_id,
            competitor_accounts=(
                {
                    "content_account_id": str(account["content_account_id"]),
                    "display_name": str(account["display_name"]),
                    "external_account_ref": str(account["external_account_ref"]),
                },
            ),
            actor=actor,
            idempotency_key=f"{idempotency_key}:registration",
        )
