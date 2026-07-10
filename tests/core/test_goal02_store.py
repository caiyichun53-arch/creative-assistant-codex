"""Real tests for scripts/core/persistence/goal02_store.py -- the first ever
Python implementation of the create_state/transition_state mechanism
(core_command_envelope + *_state tables). Every positive case asserts real
persisted values (FK chains, state_rank, row_revision), not just "no
exception raised"; every positive case has a paired reverse case proving the
guard actually rejects bad input.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.persistence.goal02_store import Goal02StateError, Goal02StateStore
from scripts.core.persistence.install_versionref_schema_into_business_db import SCHEMA_CHAIN


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    for schema_path in SCHEMA_CHAIN:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def _make_root(store: PersistenceStore, object_kind: str) -> tuple[str, str]:
    root_id = store.create_root(object_kind)
    version_id = store.append_version(root_id, {"seed": object_kind})
    store.set_current_version(root_id, version_id)
    return root_id, version_id


class CreateStateTests(unittest.TestCase):
    def test_produces_real_row_with_correct_fk_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                tactic_id, version_id = _make_root(store, "tactic")

                with conn:
                    result = goal02.create_state(
                        object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                        basis_version_id=version_id, actor="test", idempotency_key="k1",
                    )

                self.assertFalse(result.replayed)
                self.assertEqual(result.state, "candidate")
                self.assertEqual(result.state_rank, 0)
                self.assertEqual(result.row_revision, 0)

                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "candidate")
                self.assertEqual(row["state_rank"], 0)
                self.assertEqual(row["basis_version_id"], version_id)
                self.assertEqual(row["updated_by_command_id"], result.command_id)

                envelope = conn.execute(
                    "SELECT * FROM core_command_envelope WHERE command_id=?", (result.command_id,)
                ).fetchone()
                self.assertEqual(envelope["command_type"], "create_state")
                self.assertEqual(envelope["object_kind"], "tactic")
                self.assertEqual(envelope["object_id"], tactic_id)
                self.assertEqual(envelope["command_receipt_id"], result.receipt_id)
            finally:
                conn.close()

    def test_same_idempotency_key_twice_produces_exactly_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                tactic_id, version_id = _make_root(store, "tactic")

                with conn:
                    first = goal02.create_state(
                        object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                        basis_version_id=version_id, actor="test", idempotency_key="same-key",
                    )
                with conn:
                    second = goal02.create_state(
                        object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                        basis_version_id=version_id, actor="test", idempotency_key="same-key",
                    )

                self.assertFalse(first.replayed)
                self.assertTrue(second.replayed)
                self.assertEqual(first.command_id, second.command_id)

                rows = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchall()
                self.assertEqual(len(rows), 1)
                envelopes = conn.execute(
                    "SELECT * FROM core_command_envelope WHERE object_id=?", (tactic_id,)
                ).fetchall()
                self.assertEqual(len(envelopes), 1)
            finally:
                conn.close()

    def test_rejects_non_zero_rank_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                tactic_id, version_id = _make_root(store, "tactic")

                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.create_state(
                            object_kind="tactic", object_id=tactic_id, initial_state="active",
                            basis_version_id=version_id, actor="test", idempotency_key="k1",
                        )
                self.assertIsNone(conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone())
            finally:
                conn.close()

    def test_rejects_unknown_object_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.create_state(
                            object_kind="not_a_real_kind", object_id="whatever", initial_state="candidate",
                            basis_version_id="whatever", actor="test", idempotency_key="k1",
                        )
            finally:
                conn.close()

    def test_rejects_object_kind_without_state_rank_defined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                topic_id, version_id = _make_root(store, "topic")
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.create_state(
                            object_kind="topic", object_id=topic_id, initial_state="candidate",
                            basis_version_id=version_id, actor="test", idempotency_key="k1",
                        )
            finally:
                conn.close()

    def test_rejects_object_id_that_is_not_a_real_trace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.create_state(
                            object_kind="tactic", object_id="not_a_real_root_id", initial_state="candidate",
                            basis_version_id="whatever", actor="test", idempotency_key="k1",
                        )
            finally:
                conn.close()

    def test_rejects_object_id_with_mismatched_object_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                wrong_kind_id, version_id = _make_root(store, "hit_deep_analysis_evidence")
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.create_state(
                            object_kind="tactic", object_id=wrong_kind_id, initial_state="candidate",
                            basis_version_id=version_id, actor="test", idempotency_key="k1",
                        )
            finally:
                conn.close()


class TransitionStateTests(unittest.TestCase):
    def _create_candidate(self, conn: sqlite3.Connection) -> tuple[Goal02StateStore, str, str]:
        store = PersistenceStore(conn)
        goal02 = Goal02StateStore(store)
        tactic_id, version_id = _make_root(store, "tactic")
        with conn:
            goal02.create_state(
                object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                basis_version_id=version_id, actor="test", idempotency_key="create-k1",
            )
        return goal02, tactic_id, version_id

    def test_moves_forward_and_increments_row_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                goal02, tactic_id, version_id = self._create_candidate(conn)
                with conn:
                    result = goal02.transition_state(
                        object_kind="tactic", object_id=tactic_id, new_state="active",
                        basis_version_id=version_id, actor="test", idempotency_key="trans-k1",
                        expected_row_revision=0,
                    )
                self.assertFalse(result.replayed)
                self.assertEqual(result.state, "active")
                self.assertEqual(result.state_rank, 1)
                self.assertEqual(result.row_revision, 1)
                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "active")
                self.assertEqual(row["row_revision"], 1)
            finally:
                conn.close()

    def test_rejects_backward_move_via_one_way_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                goal02, tactic_id, version_id = self._create_candidate(conn)
                with conn:
                    goal02.transition_state(
                        object_kind="tactic", object_id=tactic_id, new_state="active",
                        basis_version_id=version_id, actor="test", idempotency_key="trans-k1",
                        expected_row_revision=0,
                    )
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.transition_state(
                            object_kind="tactic", object_id=tactic_id, new_state="candidate",
                            basis_version_id=version_id, actor="test", idempotency_key="trans-k2",
                            expected_row_revision=1,
                        )
                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "active")
                self.assertEqual(row["row_revision"], 1)
            finally:
                conn.close()

    def test_rejects_stale_row_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                goal02, tactic_id, version_id = self._create_candidate(conn)
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.transition_state(
                            object_kind="tactic", object_id=tactic_id, new_state="active",
                            basis_version_id=version_id, actor="test", idempotency_key="trans-k1",
                            expected_row_revision=99,
                        )
                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "candidate")
            finally:
                conn.close()

    def test_same_idempotency_key_twice_produces_exactly_one_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                goal02, tactic_id, version_id = self._create_candidate(conn)
                with conn:
                    first = goal02.transition_state(
                        object_kind="tactic", object_id=tactic_id, new_state="active",
                        basis_version_id=version_id, actor="test", idempotency_key="same-trans-key",
                        expected_row_revision=0,
                    )
                with conn:
                    second = goal02.transition_state(
                        object_kind="tactic", object_id=tactic_id, new_state="active",
                        basis_version_id=version_id, actor="test", idempotency_key="same-trans-key",
                        expected_row_revision=0,
                    )
                self.assertFalse(first.replayed)
                self.assertTrue(second.replayed)
                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["row_revision"], 1)
            finally:
                conn.close()

    def test_without_prior_create_state_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                goal02 = Goal02StateStore(store)
                tactic_id, version_id = _make_root(store, "tactic")
                with self.assertRaises(Goal02StateError):
                    with conn:
                        goal02.transition_state(
                            object_kind="tactic", object_id=tactic_id, new_state="active",
                            basis_version_id=version_id, actor="test", idempotency_key="k1",
                            expected_row_revision=0,
                        )
            finally:
                conn.close()


class CoreCommandEnvelopeSchemaTests(unittest.TestCase):
    def test_bad_object_kind_rejected_by_check_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                store = PersistenceStore(conn)
                # A real command_receipt row, so the failure below is isolated
                # to the object_kind CHECK constraint, not the unrelated FK
                # to command_receipt.
                receipt_id = store.record_command(
                    command_scope="test.scope", idempotency_key="k1",
                    request_payload={"a": 1}, result_payload={"b": 2},
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO core_command_envelope(
                            command_id, command_type, actor, object_kind, object_id, idempotency_key,
                            payload_json, correlation_id, command_receipt_id, status
                        ) VALUES ('c1', 'create_state', 'test', 'not_a_real_kind', 'x', 'k1', '{}', 'x', ?, 'succeeded')
                        """,
                        (receipt_id,),
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
