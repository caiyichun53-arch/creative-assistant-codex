"""Unregistered-account observation and the user-controlled registration handoff."""

from __future__ import annotations

from typing import Any

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class AccountObservationService:
    def __init__(self, *, core: Stage0ContentProductionCore):
        self.core = core

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
