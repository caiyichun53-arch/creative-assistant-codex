from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).with_name("goal01_schema.sqlite.sql")
_LAST_UUID7_MS = -1
_LAST_UUID7_RAND_A = 0


def uuid7() -> str:
    """Generate an application-side UUIDv7 string without external dependencies."""
    global _LAST_UUID7_MS, _LAST_UUID7_RAND_A
    unix_ms = int(time.time() * 1000)
    if unix_ms <= _LAST_UUID7_MS:
        unix_ms = _LAST_UUID7_MS
        _LAST_UUID7_RAND_A = (_LAST_UUID7_RAND_A + 1) & 0xFFF
    else:
        _LAST_UUID7_MS = unix_ms
        _LAST_UUID7_RAND_A = secrets.randbits(12)
    rand_a = _LAST_UUID7_RAND_A
    rand_b = secrets.randbits(62)
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return str(uuid.UUID(int=value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any, projection_version: str = "goal01.canonical_json.v1") -> str:
    payload = f"{projection_version}\n{canonical_json(value)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class IdempotencyConflict(RuntimeError):
    pass


class PersistenceStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def in_memory(cls) -> "PersistenceStore":
        conn = sqlite3.connect(":memory:")
        store = cls(conn)
        store.install_schema()
        return store

    def install_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def create_root(self, object_kind: str) -> str:
        root_id = uuid7()
        self.conn.execute(
            "INSERT INTO trace_root(root_id, object_kind) VALUES(?, ?)",
            (root_id, object_kind),
        )
        return root_id

    def append_version(
        self,
        root_id: str,
        payload: dict[str, Any],
        *,
        projection_version: str = "goal01.canonical_json.v1",
        based_on_version_id: str | None = None,
        business_payload: dict[str, Any] | None = None,
    ) -> str:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM trace_version WHERE root_id=?",
            (root_id,),
        ).fetchone()
        version_no = int(row[0])
        version_id = uuid7()
        c_hash = content_hash(payload, projection_version)
        b_hash = content_hash(business_payload, projection_version) if business_payload is not None else None
        self.conn.execute(
            """
            INSERT INTO trace_version(
                version_id, root_id, version_no, based_on_version_id,
                content_hash, business_hash, projection_version, payload_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                root_id,
                version_no,
                based_on_version_id,
                c_hash,
                b_hash,
                projection_version,
                canonical_json(payload),
            ),
        )
        return version_id

    def set_current_version(self, root_id: str, version_id: str, *, expected_row_revision: int | None = None) -> None:
        params: list[Any] = [version_id]
        where = "root_id=?"
        params.append(root_id)
        if expected_row_revision is not None:
            where += " AND row_revision=?"
            params.append(expected_row_revision)
        cur = self.conn.execute(
            f"""
            UPDATE trace_root
               SET current_version_id=?,
                   row_revision=row_revision+1,
                   updated_at=CURRENT_TIMESTAMP
             WHERE {where}
            """,
            params,
        )
        if cur.rowcount != 1:
            raise RuntimeError("stale root revision or missing root")

    def record_command(
        self,
        *,
        command_scope: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        status: str = "succeeded",
    ) -> str:
        request_hash = content_hash(request_payload)
        existing = self.conn.execute(
            """
            SELECT receipt_id, request_hash
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (command_scope, idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict("idempotency key reused with different request")
            return str(existing["receipt_id"])

        receipt_id = uuid7()
        self.conn.execute(
            """
            INSERT INTO command_receipt(
                receipt_id, command_scope, idempotency_key, request_hash,
                result_json, correlation_id, causation_id, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                command_scope,
                idempotency_key,
                request_hash,
                canonical_json(result_payload),
                correlation_id or uuid7(),
                causation_id,
                status,
            ),
        )
        return receipt_id

    def record_audit(
        self,
        *,
        event_type: str,
        actor: str,
        object_kind: str,
        object_id: str,
        payload: dict[str, Any],
        correlation_id: str,
        version_id: str | None = None,
        causation_id: str | None = None,
    ) -> str:
        audit_id = uuid7()
        self.conn.execute(
            """
            INSERT INTO audit_event(
                audit_id, event_type, actor, object_kind, object_id, version_id,
                payload_json, correlation_id, causation_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                event_type,
                actor,
                object_kind,
                object_id,
                version_id,
                canonical_json(payload),
                correlation_id,
                causation_id,
            ),
        )
        return audit_id

    def enqueue_outbox(
        self,
        *,
        topic: str,
        payload: dict[str, Any],
        correlation_id: str,
        causation_id: str | None = None,
    ) -> str:
        outbox_id = uuid7()
        self.conn.execute(
            """
            INSERT INTO outbox_message(outbox_id, topic, payload_json, correlation_id, causation_id)
            VALUES(?, ?, ?, ?, ?)
            """,
            (outbox_id, topic, canonical_json(payload), correlation_id, causation_id),
        )
        return outbox_id

    def create_preference_profile(self, scope_type: str, scope_id: str | None = None) -> str:
        profile_id = uuid7()
        self.conn.execute(
            """
            INSERT INTO content_preference_profile(profile_id, scope_type, scope_id)
            VALUES(?, ?, ?)
            """,
            (profile_id, scope_type, scope_id),
        )
        return profile_id

    def append_preference_revision(
        self,
        profile_id: str,
        *,
        status: str,
        preference_payload: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        origin: str,
        base_revision_id: str | None = None,
    ) -> str:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM content_preference_revision WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        revision_no = int(row[0])
        revision_id = uuid7()
        self.conn.execute(
            """
            INSERT INTO content_preference_revision(
                revision_id, profile_id, revision_no, base_revision_id, status,
                preference_payload, evidence_refs, origin, content_hash, decided_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ?='published' THEN CURRENT_TIMESTAMP ELSE NULL END)
            """,
            (
                revision_id,
                profile_id,
                revision_no,
                base_revision_id,
                status,
                canonical_json(preference_payload),
                canonical_json(evidence_refs),
                origin,
                content_hash(preference_payload),
                status,
            ),
        )
        return revision_id

    def set_current_preference(self, profile_id: str, revision_id: str) -> None:
        cur = self.conn.execute(
            """
            UPDATE content_preference_profile
               SET current_revision_id=?,
                   row_revision=row_revision+1,
                   updated_at=CURRENT_TIMESTAMP
             WHERE profile_id=?
            """,
            (revision_id, profile_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("missing content preference profile")
