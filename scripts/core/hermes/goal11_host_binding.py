from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import content_hash
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer


class HermesHostBindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeishuBindingEvent:
    event_id: str
    chat_id: str
    sender_id: str
    command: dict[str, Any]


@dataclass(frozen=True)
class HermesInboundMessage:
    host: str
    source: str
    source_event_id: str
    reply_channel_id: str
    actor: str
    command_type: str
    object_kind: str
    payload: dict[str, Any]
    object_id: str | None = None
    expected_basis_version_id: str | None = None
    confirmed: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None

    @property
    def idempotency_key(self) -> str:
        return ".".join(
            (
                "goal11",
                self.source,
                self.source_event_id,
                self.command_type,
                self.object_kind,
                self.object_id or "new",
            )
        )

    def core_envelope(self) -> CoreCommandEnvelope:
        return CoreCommandEnvelope(
            command_type=self.command_type,
            actor="hermes",
            object_kind=self.object_kind,
            object_id=self.object_id,
            expected_basis_version_id=self.expected_basis_version_id,
            idempotency_key=self.idempotency_key,
            confirmed=self.confirmed,
            payload=self.payload,
            correlation_id=self.correlation_id or content_hash(
                {
                    "host": self.host,
                    "source": self.source,
                    "source_event_id": self.source_event_id,
                },
                "goal11.host_message_correlation.v1",
            ),
            causation_id=self.causation_id,
        )


@dataclass(frozen=True)
class HermesDispatchResult:
    status: str
    receipt_id: str
    object_id: str | None
    basis_version_id: str | None
    state: str | None
    replayed: bool
    source_event_id: str
    reply_channel_id: str
    reason: str | None = None


class FeishuThinBinding:
    source = "feishu"

    def to_hermes_message(self, event: FeishuBindingEvent) -> HermesInboundMessage:
        if not event.event_id:
            raise HermesHostBindingError("event_id is required")
        if not event.chat_id:
            raise HermesHostBindingError("chat_id is required")
        if not event.sender_id:
            raise HermesHostBindingError("sender_id is required")
        command = dict(event.command)
        command_type = _required_text(command, "command_type")
        object_kind = _required_text(command, "object_kind")
        payload = command.get("payload") or {}
        if not isinstance(payload, dict):
            raise HermesHostBindingError("payload must be an object")
        return HermesInboundMessage(
            host="hermes",
            source=self.source,
            source_event_id=event.event_id,
            reply_channel_id=event.chat_id,
            actor=event.sender_id,
            command_type=command_type,
            object_kind=object_kind,
            object_id=command.get("object_id"),
            expected_basis_version_id=command.get("expected_basis_version_id"),
            confirmed=bool(command.get("confirmed", False)),
            payload=payload,
            correlation_id=command.get("correlation_id"),
            causation_id=command.get("causation_id"),
        )


class HermesCoreBridge:
    def __init__(self, core: CoreMaterializer):
        self.core = core

    def dispatch(self, message: HermesInboundMessage) -> HermesDispatchResult:
        self._validate_message(message)
        result = self.core.execute(message.core_envelope())
        return HermesDispatchResult(
            status=result.status,
            receipt_id=result.receipt_id,
            object_id=result.object_id,
            basis_version_id=result.basis_version_id,
            state=result.state,
            replayed=result.replayed,
            source_event_id=message.source_event_id,
            reply_channel_id=message.reply_channel_id,
            reason=result.reason,
        )

    @staticmethod
    def _validate_message(message: HermesInboundMessage) -> None:
        if message.host != "hermes":
            raise HermesHostBindingError("GOAL-11 production host must be Hermes")
        if message.source != "feishu":
            raise HermesHostBindingError("unsupported host binding source")
        if message.actor == "hermes":
            raise HermesHostBindingError("external actor must not impersonate Hermes")
        if not message.reply_channel_id:
            raise HermesHostBindingError("reply_channel_id is required")


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HermesHostBindingError(f"{key} is required")
    return value
