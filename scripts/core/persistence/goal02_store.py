"""Generic implementation of goal02_schema.sqlite.sql's create_state/
transition_state mechanism: `core_command_envelope` + five parallel
`{object_kind}_state` tables (production_task/topic/claim/experiment/tactic),
each with an identical shape (state/state_rank/basis_version_id/row_revision/
updated_by_command_id) and an identical one-way `*_state_one_way` trigger
that aborts any UPDATE moving state_rank backward.

Why this exists: this schema was installed into the real business database
(install_versionref_schema_into_business_db.py) but, as of 2026-07-11, had
never had a single line of Python written against it anywhere in this
codebase -- confirmed by a full-repo grep for "core_command_envelope" that
matched only a schema-collision test. The current design baseline
traceability entry already says "target formalization pending" for the step
that needs this (turning a batch of sample analyses into a persisted
candidate tactic) -- this module is that formalization's foundation.

Built generically across all 5 object kinds (not tactic-only) because the
schema itself already committed to that generality -- five identical tables,
one identical trigger shape. Refusing to mirror that in Python would just
mean re-deriving the same command-envelope-plus-state-row logic by hand
later, once, for each of the other 4 kinds. STATE_RANK is deliberately only
populated for "tactic" right now -- the other 4 kinds' real state orderings
are not specified in the effective design baseline, and guessing them
here would be exactly the kind of unreviewed business logic this project's
constitution forbids inventing silently. Calling create_state/transition_state
for an object_kind without a STATE_RANK entry raises a clear, documented
error rather than a guessed ranking.

Methods here deliberately do not open their own transaction (no `with
self.conn:`) -- callers (e.g. scripts/core/experience/tactic_registry.py)
need to compose a create_state call with trace_root/trace_version/
object_reference writes inside one atomic transaction, matching the
transaction-agnostic style PersistenceStore's own methods already use.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import PersistenceStore, canonical_json


class Goal02StateError(RuntimeError):
    """Raised for any goal02 state-machine misuse: unsupported object_kind,
    an object_id that isn't a real trace_root (or whose object_kind doesn't
    match), a state without a defined rank, a stale row_revision, or a
    backward state_rank move (surfaces the *_state_one_way trigger's abort
    as a typed error instead of a raw sqlite3.IntegrityError)."""


OBJECT_KIND_STATE_TABLE: dict[str, tuple[str, str]] = {
    "production_task": ("production_task_state", "production_task_id"),
    "topic": ("topic_state", "topic_id"),
    "claim": ("claim_state", "claim_id"),
    "experiment": ("experiment_state", "experiment_id"),
    "tactic": ("tactic_state", "tactic_id"),
}

# Only "tactic" is populated -- see module docstring. The other 4 object
# kinds are wired into OBJECT_KIND_STATE_TABLE (so the table/id-column
# lookup already works for them) but calling create_state/transition_state
# for them raises Goal02StateError until a real caller needs one and its
# state ordering gets designed and added here, cited against a real
# effective design baseline entry the way "tactic" now is,
# 原文档"7 推荐状态状态机", 2026-07-13).
#
# This is NOT a strictly-increasing "further along = higher number" ordering
# the way the other 4 kinds' rank columns are meant to be read (their shared
# *_state_one_way trigger literally rejects any UPDATE where state_rank
# decreases). tactic_state deliberately does NOT use that trigger -- see
# goal02_schema.sqlite.sql's tactic_state_terminal_and_no_candidate_reentry --
# because the real design has active/watch/paused freely bidirectional; only
# leaving "candidate" and entering "deprecated" are one-way. The numbers below
# exist only to express the doc's tie-break priority when multiple automatic
# conditions match at once (deprecated > paused > watch > active); callers
# must not assume a transition from a higher rank to a lower one is invalid.
STATE_RANK: dict[str, dict[str, int]] = {
    "tactic": {"candidate": 0, "active": 1, "watch": 2, "paused": 3, "deprecated": 4},
}


@dataclass(frozen=True)
class Goal02StateResult:
    command_id: str
    receipt_id: str
    object_kind: str
    object_id: str
    state: str
    state_rank: int
    row_revision: int
    replayed: bool = False


def _resolve_table(object_kind: str) -> tuple[str, str]:
    if object_kind not in OBJECT_KIND_STATE_TABLE:
        raise Goal02StateError(
            f"unsupported object_kind: {object_kind!r} (must be one of {sorted(OBJECT_KIND_STATE_TABLE)})"
        )
    return OBJECT_KIND_STATE_TABLE[object_kind]


def _resolve_rank(object_kind: str, state: str) -> int:
    ranks = STATE_RANK.get(object_kind)
    if ranks is None:
        raise Goal02StateError(
            f"state_rank ordering for object_kind={object_kind!r} is not yet defined -- "
            "this is a deliberate, documented gap (see goal02_store.py module docstring), "
            "not a bug; add it to STATE_RANK once a real caller needs this object_kind"
        )
    if state not in ranks:
        raise Goal02StateError(f"unsupported state {state!r} for object_kind={object_kind!r} (must be one of {sorted(ranks)})")
    return ranks[state]


class Goal02StateStore:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.conn = store.conn

    def _validate_object_id(self, object_kind: str, object_id: str) -> None:
        row = self.conn.execute("SELECT object_kind FROM trace_root WHERE root_id=?", (object_id,)).fetchone()
        if row is None:
            raise Goal02StateError(f"object_id {object_id!r} is not a real trace_root.root_id")
        if row["object_kind"] != object_kind:
            raise Goal02StateError(
                f"trace_root {object_id!r} has object_kind={row['object_kind']!r}, expected {object_kind!r}"
            )

    def create_state(
        self,
        *,
        object_kind: str,
        object_id: str,
        initial_state: str,
        basis_version_id: str,
        actor: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> Goal02StateResult:
        table, id_col = _resolve_table(object_kind)
        rank = _resolve_rank(object_kind, initial_state)
        if rank != 0:
            raise Goal02StateError(
                f"create_state requires object_kind={object_kind!r}'s lowest-rank state; "
                f"{initial_state!r} has rank {rank}, not 0"
            )
        self._validate_object_id(object_kind, object_id)

        existing = self.conn.execute(f'SELECT * FROM "{table}" WHERE "{id_col}"=?', (object_id,)).fetchone()
        if existing is not None:
            envelope = self.conn.execute(
                "SELECT * FROM core_command_envelope WHERE command_id=?", (existing["updated_by_command_id"],)
            ).fetchone()
            if envelope is None or envelope["idempotency_key"] != idempotency_key:
                raise Goal02StateError(
                    f"{object_kind} {object_id!r} already has a state created under a different idempotency_key"
                )
            return Goal02StateResult(
                command_id=envelope["command_id"],
                receipt_id=envelope["command_receipt_id"],
                object_kind=object_kind,
                object_id=object_id,
                state=existing["state"],
                state_rank=int(existing["state_rank"]),
                row_revision=int(existing["row_revision"]),
                replayed=True,
            )

        command_id = self.store.id_factory()
        request_payload: dict[str, Any] = {
            "command_type": "create_state",
            "object_kind": object_kind,
            "object_id": object_id,
            "initial_state": initial_state,
            "basis_version_id": basis_version_id,
        }
        receipt_id = self.store.record_command(
            command_scope=f"goal02.create_state.{object_kind}",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            result_payload={"command_id": command_id, "object_id": object_id, "state": initial_state},
            correlation_id=correlation_id or object_id,
            causation_id=causation_id,
            status="succeeded",
        )
        self.conn.execute(
            """
            INSERT INTO core_command_envelope(
                command_id, command_type, actor, object_kind, object_id, idempotency_key,
                expected_basis_version_id, confirmed, payload_json, correlation_id, causation_id,
                command_receipt_id, status
            ) VALUES (?, 'create_state', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'succeeded')
            """,
            (
                command_id, actor, object_kind, object_id, idempotency_key, basis_version_id,
                canonical_json(request_payload), correlation_id or object_id, causation_id, receipt_id,
            ),
        )
        self.conn.execute(
            f'INSERT INTO "{table}"("{id_col}", state, state_rank, basis_version_id, updated_by_command_id) '
            "VALUES (?, ?, ?, ?, ?)",
            (object_id, initial_state, rank, basis_version_id, command_id),
        )
        return Goal02StateResult(
            command_id=command_id,
            receipt_id=receipt_id,
            object_kind=object_kind,
            object_id=object_id,
            state=initial_state,
            state_rank=rank,
            row_revision=0,
            replayed=False,
        )

    def transition_state(
        self,
        *,
        object_kind: str,
        object_id: str,
        new_state: str,
        basis_version_id: str,
        actor: str,
        idempotency_key: str,
        expected_row_revision: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> Goal02StateResult:
        table, id_col = _resolve_table(object_kind)
        new_rank = _resolve_rank(object_kind, new_state)
        self._validate_object_id(object_kind, object_id)

        existing = self.conn.execute(f'SELECT * FROM "{table}" WHERE "{id_col}"=?', (object_id,)).fetchone()
        if existing is None:
            raise Goal02StateError(f"{object_kind} {object_id!r} has no existing state to transition -- call create_state first")

        existing_envelope = self.conn.execute(
            """
            SELECT * FROM core_command_envelope
             WHERE object_kind=? AND object_id=? AND idempotency_key=? AND command_type='transition_state'
            """,
            (object_kind, object_id, idempotency_key),
        ).fetchone()
        if existing_envelope is not None:
            current = self.conn.execute(f'SELECT * FROM "{table}" WHERE "{id_col}"=?', (object_id,)).fetchone()
            return Goal02StateResult(
                command_id=existing_envelope["command_id"],
                receipt_id=existing_envelope["command_receipt_id"],
                object_kind=object_kind,
                object_id=object_id,
                state=current["state"],
                state_rank=int(current["state_rank"]),
                row_revision=int(current["row_revision"]),
                replayed=True,
            )

        command_id = self.store.id_factory()
        request_payload = {
            "command_type": "transition_state",
            "object_kind": object_kind,
            "object_id": object_id,
            "new_state": new_state,
            "basis_version_id": basis_version_id,
            "expected_row_revision": expected_row_revision,
        }
        receipt_id = self.store.record_command(
            command_scope=f"goal02.transition_state.{object_kind}",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            result_payload={"command_id": command_id, "object_id": object_id, "state": new_state},
            correlation_id=correlation_id or object_id,
            causation_id=causation_id,
            status="succeeded",
        )
        self.conn.execute(
            """
            INSERT INTO core_command_envelope(
                command_id, command_type, actor, object_kind, object_id, idempotency_key,
                expected_basis_version_id, confirmed, payload_json, correlation_id, causation_id,
                command_receipt_id, status
            ) VALUES (?, 'transition_state', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'succeeded')
            """,
            (
                command_id, actor, object_kind, object_id, idempotency_key, basis_version_id,
                canonical_json(request_payload), correlation_id or object_id, causation_id, receipt_id,
            ),
        )
        try:
            cur = self.conn.execute(
                f'UPDATE "{table}" SET state=?, state_rank=?, basis_version_id=?, row_revision=row_revision+1, '
                f'updated_by_command_id=?, updated_at=CURRENT_TIMESTAMP WHERE "{id_col}"=? AND row_revision=?',
                (new_state, new_rank, basis_version_id, command_id, object_id, expected_row_revision),
            )
        except sqlite3.IntegrityError as exc:
            raise Goal02StateError(
                f"transition rejected for {object_kind} {object_id!r}: {exc} "
                "(most likely a state-guard trigger: backward state_rank move for the "
                "4 strictly-ordered kinds, or deprecated/candidate re-entry for tactic)"
            ) from exc
        if cur.rowcount != 1:
            raise Goal02StateError(
                f"stale row_revision for {object_kind} {object_id!r}: expected {expected_row_revision}"
            )
        return Goal02StateResult(
            command_id=command_id,
            receipt_id=receipt_id,
            object_kind=object_kind,
            object_id=object_id,
            state=new_state,
            state_rank=new_rank,
            row_revision=expected_row_revision + 1,
            replayed=False,
        )
