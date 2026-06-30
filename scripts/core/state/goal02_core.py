from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from pathlib import Path
from typing import Any, Callable

from scripts.core.persistence.goal01_store import (
    IdempotencyConflict,
    PersistenceStore,
    canonical_json,
    content_hash,
    uuid7,
)


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "persistence" / "goal02_schema.sqlite.sql"


class PermissionDenied(RuntimeError):
    pass


class InvalidTransition(RuntimeError):
    pass


class StaleBasis(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    pass


STATE_TABLES = {
    "production_task": ("production_task_state", "production_task_id"),
    "topic": ("topic_state", "topic_id"),
    "claim": ("claim_state", "claim_id"),
    "experiment": ("experiment_state", "experiment_id"),
    "tactic": ("tactic_state", "tactic_id"),
}

STATE_RANKS = {
    "production_task": {"draft": 0, "queued": 10, "running": 20, "completed": 30, "cancelled": 90},
    "topic": {"candidate": 0, "selected": 10, "researching": 20, "planned": 30, "approved": 40, "cancelled": 90},
    "claim": {"proposed": 0, "checking": 10, "verified": 20, "rejected": 90},
    "experiment": {"planned": 0, "active": 10, "completed": 20, "cancelled": 90},
    "tactic": {"candidate": 0, "active": 10, "paused": 20, "deprecated": 90},
}

INITIAL_STATES = {
    "production_task": "draft",
    "topic": "candidate",
    "claim": "proposed",
    "experiment": "planned",
    "tactic": "candidate",
}

ALLOWED_TRANSITIONS = {
    "production_task": {
        "draft": {"queued", "cancelled"},
        "queued": {"running", "cancelled"},
        "running": {"completed", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    },
    "topic": {
        "candidate": {"selected", "cancelled"},
        "selected": {"researching", "cancelled"},
        "researching": {"planned", "cancelled"},
        "planned": {"approved", "cancelled"},
        "approved": set(),
        "cancelled": set(),
    },
    "claim": {
        "proposed": {"checking", "rejected"},
        "checking": {"verified", "rejected"},
        "verified": set(),
        "rejected": set(),
    },
    "experiment": {
        "planned": {"active", "cancelled"},
        "active": {"completed", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    },
    "tactic": {
        "candidate": {"active", "deprecated"},
        "active": {"paused", "deprecated"},
        "paused": {"deprecated"},
        "deprecated": set(),
    },
}

CONFIRMATION_REQUIRED = {
    ("topic", "planned", "approved"),
    ("experiment", "planned", "active"),
    ("tactic", "candidate", "active"),
}


@dataclass(frozen=True)
class CoreCommandEnvelope:
    command_type: str
    actor: str
    object_kind: str
    idempotency_key: str
    payload: dict[str, Any]
    object_id: str | None = None
    expected_basis_version_id: str | None = None
    confirmed: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "command_type": self.command_type,
            "actor": self.actor,
            "object_kind": self.object_kind,
            "object_id": self.object_id,
            "expected_basis_version_id": self.expected_basis_version_id,
            "confirmed": self.confirmed,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CommandResult:
    command_id: str | None
    receipt_id: str
    status: str
    object_id: str | None = None
    basis_version_id: str | None = None
    state: str | None = None
    replayed: bool = False
    reason: str | None = None


class CoreMaterializer:
    def __init__(self, store: PersistenceStore, *, id_factory: Callable[[], str] = uuid7):
        self.store = store
        self.conn = store.conn
        self.id_factory = id_factory
        self.install_schema()

    @classmethod
    def in_memory(cls, *, id_factory: Callable[[], str] = uuid7) -> "CoreMaterializer":
        store = PersistenceStore.in_memory(id_factory=id_factory)
        return cls(store, id_factory=id_factory)

    def install_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def grant_permission(self, actor: str, command_type: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO core_permission(actor, command_type) VALUES(?, ?)",
            (actor, command_type),
        )

    def execute(self, envelope: CoreCommandEnvelope) -> CommandResult:
        self._validate_envelope_shape(envelope)
        existing = self._existing_receipt(envelope)
        if existing is not None:
            return existing
        with self.conn:
            if not self._has_permission(envelope.actor, envelope.command_type):
                return self._reject(envelope, "permission_denied")
            if envelope.command_type == "create_state":
                return self._create_state(envelope)
            if envelope.command_type == "transition_state":
                return self._transition_state(envelope)
        raise InvalidTransition(f"unsupported command type: {envelope.command_type}")

    def get_state(self, object_kind: str, object_id: str) -> sqlite3.Row:
        table, id_column = self._state_table(object_kind)
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?",
            (object_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"missing {object_kind} state: {object_id}")
        return row

    def _validate_envelope_shape(self, envelope: CoreCommandEnvelope) -> None:
        if envelope.command_type not in {"create_state", "transition_state"}:
            raise InvalidTransition(f"unsupported command type: {envelope.command_type}")
        if envelope.object_kind not in STATE_TABLES:
            raise InvalidTransition(f"unsupported object kind: {envelope.object_kind}")
        if envelope.command_type == "transition_state" and not envelope.object_id:
            raise InvalidTransition("transition_state requires object_id")

    def _existing_receipt(self, envelope: CoreCommandEnvelope) -> CommandResult | None:
        request_hash = content_hash(envelope.request_payload())
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json, status
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal02.core", envelope.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key reused with different request")
        return CommandResult(
            command_id=None,
            receipt_id=row["receipt_id"],
            status=row["status"],
            replayed=True,
        )

    def _create_state(self, envelope: CoreCommandEnvelope) -> CommandResult:
        state = envelope.payload.get("state") or INITIAL_STATES[envelope.object_kind]
        expected = INITIAL_STATES[envelope.object_kind]
        if state != expected:
            return self._reject(envelope, "invalid_initial_state")
        command_id, receipt_id = self._record_command(envelope, "succeeded", {"state": state})
        root_id = self.store.create_root(envelope.object_kind)
        version_id = self.store.append_version(
            root_id,
            self._version_payload(envelope, state=state, previous_state=None),
        )
        self.store.set_current_version(root_id, version_id)
        table, id_column = self._state_table(envelope.object_kind)
        rank = STATE_RANKS[envelope.object_kind][state]
        self.conn.execute(
            f"""
            INSERT INTO {table}(
                {id_column}, state, state_rank, basis_version_id, updated_by_command_id
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (root_id, state, rank, version_id, command_id),
        )
        self._audit_and_outbox(envelope, command_id, root_id, version_id, state)
        return CommandResult(command_id, receipt_id, "succeeded", root_id, version_id, state)

    def _transition_state(self, envelope: CoreCommandEnvelope) -> CommandResult:
        assert envelope.object_id is not None
        row = self.get_state(envelope.object_kind, envelope.object_id)
        current_state = row["state"]
        current_basis = row["basis_version_id"]
        target_state = str(envelope.payload.get("to_state", ""))
        if envelope.expected_basis_version_id != current_basis:
            return self._reject(envelope, "stale_basis")
        if target_state not in ALLOWED_TRANSITIONS[envelope.object_kind].get(current_state, set()):
            return self._reject(envelope, "invalid_transition")
        if (envelope.object_kind, current_state, target_state) in CONFIRMATION_REQUIRED and not envelope.confirmed:
            return self._reject(envelope, "confirmation_required")
        if envelope.payload.get("inject_fault_after_version"):
            return self._transition_with_injected_fault(envelope, row, target_state)
        return self._transition(envelope, row, target_state)

    def _transition_with_injected_fault(
        self,
        envelope: CoreCommandEnvelope,
        row: sqlite3.Row,
        target_state: str,
    ) -> CommandResult:
        command_id, _receipt_id = self._record_command(envelope, "succeeded", {"state": target_state})
        version_id = self.store.append_version(
            envelope.object_id or "",
            self._version_payload(envelope, state=target_state, previous_state=row["state"]),
            based_on_version_id=row["basis_version_id"],
        )
        raise RuntimeError(f"injected materializer fault after version {version_id} for command {command_id}")

    def _transition(self, envelope: CoreCommandEnvelope, row: sqlite3.Row, target_state: str) -> CommandResult:
        assert envelope.object_id is not None
        command_id, receipt_id = self._record_command(envelope, "succeeded", {"state": target_state})
        version_id = self.store.append_version(
            envelope.object_id,
            self._version_payload(envelope, state=target_state, previous_state=row["state"]),
            based_on_version_id=row["basis_version_id"],
        )
        self.store.set_current_version(envelope.object_id, version_id)
        table, id_column = self._state_table(envelope.object_kind)
        rank = STATE_RANKS[envelope.object_kind][target_state]
        self.conn.execute(
            f"""
            UPDATE {table}
               SET state=?,
                   state_rank=?,
                   basis_version_id=?,
                   row_revision=row_revision+1,
                   updated_by_command_id=?,
                   updated_at=CURRENT_TIMESTAMP
             WHERE {id_column}=?
            """,
            (target_state, rank, version_id, command_id, envelope.object_id),
        )
        self._audit_and_outbox(envelope, command_id, envelope.object_id, version_id, target_state)
        return CommandResult(command_id, receipt_id, "succeeded", envelope.object_id, version_id, target_state)

    def _reject(self, envelope: CoreCommandEnvelope, reason: str) -> CommandResult:
        command_id, receipt_id = self._record_command(envelope, "rejected", {"reason": reason})
        self.store.record_audit(
            event_type="core_command_rejected",
            actor=envelope.actor,
            object_kind=envelope.object_kind,
            object_id=envelope.object_id or command_id,
            payload={"command_id": command_id, "reason": reason},
            correlation_id=envelope.correlation_id or command_id,
            causation_id=envelope.causation_id,
        )
        return CommandResult(command_id, receipt_id, "rejected", envelope.object_id, reason=reason)

    def _record_command(
        self,
        envelope: CoreCommandEnvelope,
        status: str,
        result_payload: dict[str, Any],
    ) -> tuple[str, str]:
        command_id = self.id_factory()
        correlation_id = envelope.correlation_id or command_id
        receipt_id = self.store.record_command(
            command_scope="goal02.core",
            idempotency_key=envelope.idempotency_key,
            request_payload=envelope.request_payload(),
            result_payload=result_payload,
            correlation_id=correlation_id,
            causation_id=envelope.causation_id,
            status=status,
        )
        self.conn.execute(
            """
            INSERT INTO core_command_envelope(
                command_id, command_type, actor, object_kind, object_id,
                idempotency_key, expected_basis_version_id, confirmed, payload_json,
                correlation_id, causation_id, command_receipt_id, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                envelope.command_type,
                envelope.actor,
                envelope.object_kind,
                envelope.object_id,
                envelope.idempotency_key,
                envelope.expected_basis_version_id,
                1 if envelope.confirmed else 0,
                canonical_json(envelope.payload),
                correlation_id,
                envelope.causation_id,
                receipt_id,
                status,
            ),
        )
        return command_id, receipt_id

    def _audit_and_outbox(
        self,
        envelope: CoreCommandEnvelope,
        command_id: str,
        object_id: str,
        version_id: str,
        state: str,
    ) -> None:
        correlation_id = envelope.correlation_id or command_id
        audit_id = self.store.record_audit(
            event_type="core_state_materialized",
            actor=envelope.actor,
            object_kind=envelope.object_kind,
            object_id=object_id,
            version_id=version_id,
            payload={"command_id": command_id, "state": state},
            correlation_id=correlation_id,
            causation_id=envelope.causation_id,
        )
        self.store.enqueue_outbox(
            topic="core.state.changed",
            payload={"object_kind": envelope.object_kind, "object_id": object_id, "state": state, "version_id": version_id},
            correlation_id=correlation_id,
            causation_id=audit_id,
        )

    def _version_payload(
        self,
        envelope: CoreCommandEnvelope,
        *,
        state: str,
        previous_state: str | None,
    ) -> dict[str, Any]:
        return {
            "command_type": envelope.command_type,
            "object_kind": envelope.object_kind,
            "previous_state": previous_state,
            "state": state,
            "payload": envelope.payload,
            "confirmed": envelope.confirmed,
        }

    def _has_permission(self, actor: str, command_type: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM core_permission WHERE actor=? AND command_type=?",
            (actor, command_type),
        ).fetchone()
        return row is not None

    def _state_table(self, object_kind: str) -> tuple[str, str]:
        try:
            return STATE_TABLES[object_kind]
        except KeyError as exc:
            raise InvalidTransition(f"unsupported object kind: {object_kind}") from exc
