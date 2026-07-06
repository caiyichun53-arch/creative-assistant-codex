from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.state.goal02_core import CoreCommandEnvelope, CoreMaterializer


class HostBindingError(RuntimeError):
    pass


PRODUCTION_HOST_ACTOR = "production_host"
GOAL11_HOST_MESSAGE_SCOPE = "goal11.host_message.receive"
GOAL11_RESPONSE_SEND_SCOPE = "goal11.feishu_response.send"
GOAL11_RESPONSE_TOPIC = "goal11.host_response.pending"
GOAL11_RESPONSE_JOB_KIND = "outbox.dispatch"
DEFAULT_ALLOWED_HOSTS = frozenset({"hermes", "codex", "claude"})
DEFAULT_ALLOWED_SOURCES = frozenset({"feishu", "codex", "claude", "cli", "web"})


@dataclass(frozen=True)
class FeishuBindingEvent:
    event_id: str
    chat_id: str
    sender_id: str
    command: dict[str, Any]


@dataclass(frozen=True)
class CodexBindingEvent:
    event_id: str
    thread_id: str
    actor_id: str
    command: dict[str, Any]


@dataclass(frozen=True)
class ClaudeBindingEvent:
    event_id: str
    thread_id: str
    actor_id: str
    command: dict[str, Any]


@dataclass(frozen=True)
class ProductionHostInboundMessage:
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
                self.host,
                self.source,
                self.source_event_id,
                self.command_type,
                self.object_kind,
                self.object_id or "new",
            )
        )

    def core_envelope(self, *, core_actor: str = PRODUCTION_HOST_ACTOR) -> CoreCommandEnvelope:
        return CoreCommandEnvelope(
            command_type=self.command_type,
            actor=core_actor,
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

    def request_payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "reply_channel_id": self.reply_channel_id,
            "actor": self.actor,
            "command_type": self.command_type,
            "object_kind": self.object_kind,
            "payload": self.payload,
            "object_id": self.object_id,
            "expected_basis_version_id": self.expected_basis_version_id,
            "confirmed": self.confirmed,
        }


@dataclass(frozen=True)
class ProductionHostDispatchResult:
    status: str
    receipt_id: str
    core_receipt_id: str
    host_message_version_id: str | None
    response_outbox_id: str | None
    object_id: str | None
    basis_version_id: str | None
    state: str | None
    replayed: bool
    source_event_id: str
    reply_channel_id: str
    reason: str | None = None


@dataclass(frozen=True)
class FeishuSendResult:
    outbox_id: str
    receipt_id: str
    reply_channel_id: str
    replayed: bool = False


class FeishuThinBinding:
    source = "feishu"

    def to_host_message(self, event: FeishuBindingEvent, *, host: str = "hermes") -> ProductionHostInboundMessage:
        if not event.event_id:
            raise HostBindingError("event_id is required")
        if not event.chat_id:
            raise HostBindingError("chat_id is required")
        if not event.sender_id:
            raise HostBindingError("sender_id is required")
        command = dict(event.command)
        command_type = _required_text(command, "command_type")
        object_kind = _required_text(command, "object_kind")
        payload = command.get("payload") or {}
        if not isinstance(payload, dict):
            raise HostBindingError("payload must be an object")
        return ProductionHostInboundMessage(
            host=host,
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

    def to_hermes_message(self, event: FeishuBindingEvent) -> ProductionHostInboundMessage:
        return self.to_host_message(event, host="hermes")


class CodexHostBinding:
    source = "codex"

    def to_host_message(self, event: CodexBindingEvent) -> ProductionHostInboundMessage:
        if not event.event_id:
            raise HostBindingError("event_id is required")
        if not event.thread_id:
            raise HostBindingError("thread_id is required")
        if not event.actor_id:
            raise HostBindingError("actor_id is required")
        command = dict(event.command)
        command_type = _required_text(command, "command_type")
        object_kind = _required_text(command, "object_kind")
        payload = command.get("payload") or {}
        if not isinstance(payload, dict):
            raise HostBindingError("payload must be an object")
        return ProductionHostInboundMessage(
            host="codex",
            source=self.source,
            source_event_id=event.event_id,
            reply_channel_id=event.thread_id,
            actor=event.actor_id,
            command_type=command_type,
            object_kind=object_kind,
            object_id=command.get("object_id"),
            expected_basis_version_id=command.get("expected_basis_version_id"),
            confirmed=bool(command.get("confirmed", False)),
            payload=payload,
            correlation_id=command.get("correlation_id"),
            causation_id=command.get("causation_id"),
        )


class ClaudeHostBinding:
    source = "claude"

    def to_host_message(self, event: ClaudeBindingEvent) -> ProductionHostInboundMessage:
        if not event.event_id:
            raise HostBindingError("event_id is required")
        if not event.thread_id:
            raise HostBindingError("thread_id is required")
        if not event.actor_id:
            raise HostBindingError("actor_id is required")
        command = dict(event.command)
        command_type = _required_text(command, "command_type")
        object_kind = _required_text(command, "object_kind")
        payload = command.get("payload") or {}
        if not isinstance(payload, dict):
            raise HostBindingError("payload must be an object")
        return ProductionHostInboundMessage(
            host="claude",
            source=self.source,
            source_event_id=event.event_id,
            reply_channel_id=event.thread_id,
            actor=event.actor_id,
            command_type=command_type,
            object_kind=object_kind,
            object_id=command.get("object_id"),
            expected_basis_version_id=command.get("expected_basis_version_id"),
            confirmed=bool(command.get("confirmed", False)),
            payload=payload,
            correlation_id=command.get("correlation_id"),
            causation_id=command.get("causation_id"),
        )


class ProductionHostBridge:
    def __init__(
        self,
        core: CoreMaterializer,
        *,
        allowed_hosts: set[str] | frozenset[str] | tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
        allowed_sources: set[str] | frozenset[str] | tuple[str, ...] = DEFAULT_ALLOWED_SOURCES,
        core_actor: str = PRODUCTION_HOST_ACTOR,
    ):
        self.core = core
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_sources = frozenset(allowed_sources)
        self.core_actor = core_actor

    def dispatch(self, message: ProductionHostInboundMessage) -> ProductionHostDispatchResult:
        self._validate_message(message)
        existing = self._existing_host_result(message)
        if existing is not None:
            return existing
        core_envelope = message.core_envelope(core_actor=self.core_actor)
        core_result = self.core.execute(core_envelope)
        correlation_id = core_envelope.correlation_id or message.idempotency_key
        with self.core.conn:
            root_id = self.core.store.create_root("goal11_host_message")
            host_payload = {
                "goal": "GOAL-11",
                "host": message.host,
                "source": message.source,
                "source_event_id": message.source_event_id,
                "reply_channel_id": message.reply_channel_id,
                "external_actor": message.actor,
                "core_actor": self.core_actor,
                "command_type": message.command_type,
                "object_kind": message.object_kind,
                "object_id": core_result.object_id,
                "basis_version_id": core_result.basis_version_id,
                "state": core_result.state,
                "status": core_result.status,
                "reason": core_result.reason,
                "core_receipt_id": core_result.receipt_id,
            }
            version_id = self.core.store.append_version(
                root_id,
                host_payload,
                projection_version="goal11.host_message.v1",
                business_payload={
                    "host": message.host,
                    "source": message.source,
                    "source_event_id": message.source_event_id,
                    "command_type": message.command_type,
                    "object_kind": message.object_kind,
                    "status": core_result.status,
                    "object_id": core_result.object_id,
                },
            )
            self.core.store.set_current_version(root_id, version_id)
            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "core_receipt_id": core_result.receipt_id,
                "status": core_result.status,
                "object_id": core_result.object_id,
                "basis_version_id": core_result.basis_version_id,
                "state": core_result.state,
                "reason": core_result.reason,
                "correlation_id": correlation_id,
            }
            receipt_id = self.core.store.record_command(
                command_scope=GOAL11_HOST_MESSAGE_SCOPE,
                idempotency_key=message.idempotency_key,
                request_payload=message.request_payload(),
                result_payload=result_payload,
                correlation_id=correlation_id,
                causation_id=message.causation_id or core_result.receipt_id,
                status=core_result.status,
            )
            audit_id = self.core.store.record_audit(
                event_type="goal11.host_message.received",
                actor=self.core_actor,
                object_kind="goal11_host_message",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "core_receipt_id": core_result.receipt_id,
                    "host": message.host,
                    "source": message.source,
                    "source_event_id": message.source_event_id,
                    "status": core_result.status,
                },
                correlation_id=correlation_id,
                causation_id=message.causation_id or core_result.receipt_id,
            )
            response_outbox_id = self.core.store.enqueue_outbox(
                topic=GOAL11_RESPONSE_TOPIC,
                payload=_response_payload(message, core_envelope, core_result.status, core_result.state, core_result.reason),
                correlation_id=correlation_id,
                causation_id=audit_id,
            )
        return ProductionHostDispatchResult(
            status=core_result.status,
            receipt_id=receipt_id,
            core_receipt_id=core_result.receipt_id,
            host_message_version_id=version_id,
            response_outbox_id=response_outbox_id,
            object_id=core_result.object_id,
            basis_version_id=core_result.basis_version_id,
            state=core_result.state,
            replayed=False,
            source_event_id=message.source_event_id,
            reply_channel_id=message.reply_channel_id,
            reason=core_result.reason,
        )

    def enqueue_response_jobs(self, scheduler: Goal03Scheduler) -> tuple[str, ...]:
        rows = self.core.conn.execute(
            """
            SELECT outbox_id
              FROM outbox_message
             WHERE topic=? AND status='pending'
             ORDER BY created_at, outbox_id
            """,
            (GOAL11_RESPONSE_TOPIC,),
        ).fetchall()
        job_ids: list[str] = []
        for row in rows:
            enqueued = scheduler.enqueue_from_outbox(
                row["outbox_id"],
                idempotency_key=f"goal11.response.outbox.{row['outbox_id']}",
                max_attempts=3,
            )
            job_ids.append(enqueued.job_id)
        return tuple(job_ids)

    def _validate_message(self, message: ProductionHostInboundMessage) -> None:
        if message.host not in self.allowed_hosts:
            raise HostBindingError(f"unsupported production host: {message.host}")
        if message.source not in self.allowed_sources:
            raise HostBindingError(f"unsupported host binding source: {message.source}")
        if message.actor == self.core_actor or message.actor in self.allowed_hosts:
            raise HostBindingError("external actor must not impersonate production host")
        if not message.reply_channel_id:
            raise HostBindingError("reply_channel_id is required")

    def _existing_host_result(self, message: ProductionHostInboundMessage) -> ProductionHostDispatchResult | None:
        row = self.core.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json, status
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (GOAL11_HOST_MESSAGE_SCOPE, message.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(message.request_payload()):
            raise IdempotencyConflict("idempotency key reused with different request")
        result = json.loads(row["result_json"])
        response_outbox_id = self._response_outbox_for(result["correlation_id"])
        return ProductionHostDispatchResult(
            status=row["status"],
            receipt_id=row["receipt_id"],
            core_receipt_id=result["core_receipt_id"],
            host_message_version_id=result["version_id"],
            response_outbox_id=response_outbox_id,
            object_id=result["object_id"],
            basis_version_id=result["basis_version_id"],
            state=result["state"],
            replayed=True,
            source_event_id=message.source_event_id,
            reply_channel_id=message.reply_channel_id,
            reason=result["reason"],
        )

    def _response_outbox_for(self, correlation_id: str) -> str | None:
        row = self.core.conn.execute(
            """
            SELECT outbox_id
              FROM outbox_message
             WHERE topic=? AND correlation_id=?
             ORDER BY created_at, outbox_id
             LIMIT 1
            """,
            (GOAL11_RESPONSE_TOPIC, correlation_id),
        ).fetchone()
        return None if row is None else str(row["outbox_id"])


class FeishuResponseDispatcher:
    def __init__(self, store: PersistenceStore, *, fail_once_outbox_ids: set[str] | None = None):
        self.store = store
        self.conn = store.conn
        self.fail_once_outbox_ids = set(fail_once_outbox_ids or set())
        self.failed_outbox_ids: set[str] = set()
        self.sent: list[FeishuSendResult] = []

    def handler(self):
        def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            result = self.dispatch(payload)
            return {
                "outbox_id": result.outbox_id,
                "reply_channel_id": result.reply_channel_id,
                "receipt_id": result.receipt_id,
                "replayed": result.replayed,
            }

        return _handler

    def dispatch(self, scheduler_payload: dict[str, Any]) -> FeishuSendResult:
        outbox_id = _required_text(scheduler_payload, "outbox_id")
        topic = _required_text(scheduler_payload, "topic")
        if topic != GOAL11_RESPONSE_TOPIC:
            raise HostBindingError("unsupported response topic")
        payload = scheduler_payload.get("payload") or {}
        if not isinstance(payload, dict):
            raise HostBindingError("response payload must be an object")
        reply_channel_id = _required_text(payload, "reply_channel_id")
        request_payload = {
            "outbox_id": outbox_id,
            "topic": topic,
            "payload": payload,
        }
        idempotency_key = f"goal11.feishu.response.{outbox_id}"
        existing = self._existing_send(idempotency_key, request_payload, outbox_id, reply_channel_id)
        if existing is not None:
            return existing
        if outbox_id in self.fail_once_outbox_ids and outbox_id not in self.failed_outbox_ids:
            self.failed_outbox_ids.add(outbox_id)
            raise HostBindingError("injected fake Feishu send failure")
        with self.conn:
            receipt_id = self.store.record_command(
                command_scope=GOAL11_RESPONSE_SEND_SCOPE,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                result_payload={
                    "outbox_id": outbox_id,
                    "reply_channel_id": reply_channel_id,
                    "status": "sent",
                },
                correlation_id=str(payload.get("correlation_id") or outbox_id),
                causation_id=outbox_id,
                status="succeeded",
            )
            self.store.record_audit(
                event_type="goal11.feishu_response.sent",
                actor="feishu_fake_binding",
                object_kind="outbox_message",
                object_id=outbox_id,
                payload={
                    "receipt_id": receipt_id,
                    "reply_channel_id": reply_channel_id,
                    "source_event_id": payload.get("source_event_id"),
                },
                correlation_id=str(payload.get("correlation_id") or outbox_id),
                causation_id=outbox_id,
            )
        result = FeishuSendResult(outbox_id=outbox_id, receipt_id=receipt_id, reply_channel_id=reply_channel_id)
        self.sent.append(result)
        return result

    def _existing_send(
        self,
        idempotency_key: str,
        request_payload: dict[str, Any],
        outbox_id: str,
        reply_channel_id: str,
    ) -> FeishuSendResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (GOAL11_RESPONSE_SEND_SCOPE, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(request_payload):
            raise IdempotencyConflict("idempotency key reused with different request")
        return FeishuSendResult(
            outbox_id=outbox_id,
            receipt_id=row["receipt_id"],
            reply_channel_id=reply_channel_id,
            replayed=True,
        )


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HostBindingError(f"{key} is required")
    return value


def _response_payload(
    message: ProductionHostInboundMessage,
    core_envelope: CoreCommandEnvelope,
    status: str,
    state: str | None,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "binding": message.source,
        "host": message.host,
        "reply_channel_id": message.reply_channel_id,
        "source_event_id": message.source_event_id,
        "status": status,
        "object_kind": message.object_kind,
        "state": state,
        "reason": reason,
        "correlation_id": core_envelope.correlation_id,
    }
