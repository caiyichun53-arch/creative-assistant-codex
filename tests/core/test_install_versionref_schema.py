"""Real tests for scripts/core/persistence/install_versionref_schema_into_business_db.py.

Every positive case here asserts an actual business result (row content
unchanged, real columns match), not just "the function returned without
raising". Every check has a paired reverse-proof case: feed it data that
should be rejected, and confirm it actually rejects rather than silently
succeeding.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.persistence.install_versionref_schema_into_business_db import (
    SCHEMA_CHAIN,
    BusinessDataMutatedError,
    SchemaCollisionError,
    _table_columns_from_schema,
    check_no_schema_collision,
    diff_snapshots,
    install,
    snapshot_business_tables,
)


ROOT = Path(__file__).resolve().parents[2]


def _scratch_db_path(testcase: unittest.TestCase, name: str) -> Path:
    """install() enforces that its target lives under the real data/formal/
    directory (see _safe_db_path) -- these three tests exercise install()'s
    own behavior (backup creation, additive apply, end-to-end collision
    refusal), so they need a real scratch path there, not an arbitrary
    tempdir. Registers cleanup of the file itself and any backup file
    install() creates alongside it."""
    db_path = ROOT / "data" / "formal" / name
    testcase.addCleanup(lambda: db_path.unlink(missing_ok=True))
    testcase.addCleanup(
        lambda: [p.unlink() for p in db_path.parent.glob(f"{db_path.stem}_pre_versionref_schema_*.sqlite3")]
    )
    return db_path


def _seed_real_business_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name,
            sec_uid, homepage_url, source_config_ref, registration_status,
            first_crawl_policy, comments_policy
        ) VALUES('acc1','douyin','fan_kepu_social_life','domain1','acc name','sec1',
                 'https://x','cfg','active','observe_days','purpose_a')
        """
    )
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES ('vid1', 'acc1', 'douyin', 'item1', 't', 'https://x', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count,
            hit_channel, judgment_confidence, run_id, preparation_status
        ) VALUES ('hit1', 'vid1', 'acc1', 'douyin', 'item1', '标题', 'https://x/1',
                  100, 10, 5, 2, 'like_anomaly', 'formal', 'run1', 'completed')
        """
    )
    conn.commit()
    return conn


class TableColumnsFromSchemaTests(unittest.TestCase):
    def test_real_schema_chain_parses_into_real_column_lists(self) -> None:
        # Regression pin for a real bug found while writing this: a naive
        # regex/comma-split parse of the CREATE TABLE body picks up fragments
        # from CHECK(...)/UNIQUE(...) clauses as if they were column names
        # (e.g. "'queued'" or "job_id)" leaking into the column list). This
        # asserts the real parser (executes the schema, reads PRAGMA
        # table_info) returns only genuine column names.
        tables = _table_columns_from_schema(SCHEMA_CHAIN[2].read_text(encoding="utf-8"))
        self.assertIn("scheduler_job", tables)
        self.assertEqual(
            tables["scheduler_job"],
            [
                "job_id", "job_kind", "status", "priority", "run_after", "max_attempts",
                "attempt_count", "current_attempt_id", "lease_owner", "lease_expires_at",
                "payload_json", "source_outbox_id", "correlation_id", "causation_id",
                "created_by_receipt_id", "created_at", "updated_at",
            ],
        )
        for columns in tables.values():
            for name in columns:
                self.assertNotIn("'", name, f"leaked CHECK(...) fragment into column list: {name!r}")
                self.assertNotIn(")", name, f"leaked constraint fragment into column list: {name!r}")


class SnapshotAndDiffTests(unittest.TestCase):
    def test_snapshot_reflects_real_row_content_not_just_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                before = snapshot_business_tables(conn)
                self.assertEqual(before["hits"]["row_count"], 1)
                conn.execute("UPDATE hits SET like_count = 999 WHERE hit_id='hit1'")
                conn.commit()
                after = snapshot_business_tables(conn)
                # Row count unchanged, but content_hash MUST differ -- proves
                # the snapshot is content-sensitive, not just a COUNT(*).
                self.assertEqual(after["hits"]["row_count"], before["hits"]["row_count"])
                self.assertNotEqual(after["hits"]["content_hash"], before["hits"]["content_hash"])
            finally:
                conn.close()

    def test_diff_snapshots_detects_a_real_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                before = snapshot_business_tables(conn)
                conn.execute("UPDATE hits SET like_count = 12345 WHERE hit_id='hit1'")
                conn.commit()
                after = snapshot_business_tables(conn)
                changed = diff_snapshots(before, after)
                self.assertIn("hits", changed)
            finally:
                conn.close()

    def test_diff_snapshots_reports_clean_when_nothing_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                before = snapshot_business_tables(conn)
                after = snapshot_business_tables(conn)
                self.assertEqual(diff_snapshots(before, after), {})
            finally:
                conn.close()


class SchemaCollisionTests(unittest.TestCase):
    def test_no_collision_on_a_real_seeded_business_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                check_no_schema_collision(conn)  # must not raise
            finally:
                conn.close()

    def test_a_real_colliding_table_with_wrong_columns_is_actually_detected(self) -> None:
        # Reverse-proof: CREATE TABLE IF NOT EXISTS silently no-ops on an
        # existing same-named table regardless of its column structure. If
        # check_no_schema_collision only checked "does the table exist" (or
        # did nothing at all), this test would still pass -- it must fail
        # unless the check genuinely compares column structure.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                conn.execute("CREATE TABLE tactic_state(only_one_wrong_column TEXT)")
                conn.commit()
                with self.assertRaises(SchemaCollisionError):
                    check_no_schema_collision(conn)
            finally:
                conn.close()

    def test_a_real_matching_pre_existing_table_is_not_flagged(self) -> None:
        # The other half of the collision check's honesty: a table that
        # already exists with the EXACT right structure (e.g. a second
        # install run) must NOT be flagged as a collision.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            conn = _seed_real_business_db(db_path)
            try:
                conn.executescript(SCHEMA_CHAIN[2].read_text(encoding="utf-8"))
                conn.commit()
                check_no_schema_collision(conn)  # must not raise
            finally:
                conn.close()


class InstallTests(unittest.TestCase):
    def test_a_real_business_table_mutation_during_install_is_actually_detected(self) -> None:
        # Reverse-proof for BusinessDataMutatedError: if the schema chain
        # ever mutated an existing business row (e.g. a future schema-file
        # bug), install() must raise loudly instead of returning a quiet
        # success. Proven by pointing SCHEMA_CHAIN at a real temp file that
        # legitimately installs a new table AND mutates hits.like_count in
        # the same executescript -- not by mocking the detection function
        # itself to claim it would fire.
        import scripts.core.persistence.install_versionref_schema_into_business_db as module

        db_path = _scratch_db_path(self, "_scratch_test_mutation_detected.sqlite3")
        _seed_real_business_db(db_path).close()

        poisoned_schema = ROOT / "data" / "formal" / "_scratch_poisoned_schema.sql"
        poisoned_schema.write_text(
            "CREATE TABLE IF NOT EXISTS harmless_new_table(id TEXT);\n"
            "UPDATE hits SET like_count = 999999 WHERE hit_id='hit1';\n",
            encoding="utf-8",
        )
        self.addCleanup(poisoned_schema.unlink)

        with mock.patch.object(module, "SCHEMA_CHAIN", (poisoned_schema,)):
            with self.assertRaises(BusinessDataMutatedError):
                install(db_path, skip_backup=True)

        # The mutation is real (executescript already committed it) -- this
        # is exactly why the guard must be loud rather than silent: the
        # caller finds out immediately instead of a silent bad install.
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT like_count FROM hits WHERE hit_id='hit1'").fetchone()
            self.assertEqual(row[0], 999999)
        finally:
            conn.close()

    def test_install_is_additive_and_leaves_real_business_rows_byte_identical(self) -> None:
        db_path = _scratch_db_path(self, "_scratch_test_additive.sqlite3")
        conn = _seed_real_business_db(db_path)
        before_snapshot = snapshot_business_tables(conn)
        conn.close()

        result = install(db_path, skip_backup=True)

        self.assertEqual(set(result["new_tables_installed"]), {
            "trace_root", "trace_version", "object_reference", "binding_manifest",
            "command_receipt", "audit_event", "outbox_message",
            "content_preference_profile", "content_preference_revision",
            "core_permission", "core_command_envelope", "production_task_state",
            "topic_state", "claim_state", "experiment_state", "tactic_state",
            "scheduler_job", "scheduler_job_attempt",
        })
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        try:
            after_snapshot = snapshot_business_tables(conn2)
            for table_name, before_state in before_snapshot.items():
                self.assertEqual(
                    after_snapshot[table_name], before_state,
                    f"{table_name} content changed across install -- must be byte-identical",
                )
            # The real row is still readable with the same values, not just
            # "the hash matches" -- an independent, direct check.
            row = conn2.execute("SELECT like_count FROM hits WHERE hit_id='hit1'").fetchone()
            self.assertEqual(row["like_count"], 100)
        finally:
            conn2.close()

    def test_install_creates_a_backup_file_by_default(self) -> None:
        db_path = _scratch_db_path(self, "_scratch_test_backup.sqlite3")
        _seed_real_business_db(db_path).close()

        result = install(db_path)

        self.assertIsNotNone(result["backup"])
        backup_path = Path(result["backup"])
        self.assertTrue(backup_path.exists())
        # Real proof the backup is a genuine copy of the pre-install state,
        # not an empty placeholder file.
        backup_conn = sqlite3.connect(backup_path)
        try:
            row = backup_conn.execute("SELECT like_count FROM hits WHERE hit_id='hit1'").fetchone()
            self.assertEqual(row[0], 100)
            # The backup must predate the schema install -- it should NOT
            # contain the new VersionRef tables.
            has_trace_root = backup_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_root'"
            ).fetchone()
            self.assertIsNone(has_trace_root)
        finally:
            backup_conn.close()

    def test_install_refuses_a_real_column_structure_collision_end_to_end(self) -> None:
        # End-to-end reverse-proof through the public install() entrypoint,
        # not just the lower-level check_no_schema_collision() unit test
        # above -- proves a caller cannot bypass the collision guard by
        # calling install() directly.
        db_path = _scratch_db_path(self, "_scratch_test_collision.sqlite3")
        conn = _seed_real_business_db(db_path)
        conn.execute("CREATE TABLE trace_root(wrong_shape TEXT)")
        conn.commit()
        conn.close()

        with self.assertRaises(SchemaCollisionError):
            install(db_path, skip_backup=True)

        # And the bad table must still be there, untouched -- a failed
        # install must not have partially applied anything.
        conn2 = sqlite3.connect(db_path)
        try:
            columns = [r[1] for r in conn2.execute('PRAGMA table_info("trace_root")').fetchall()]
            self.assertEqual(columns, ["wrong_shape"])
        finally:
            conn2.close()

    def test_install_rejects_a_path_outside_data_formal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "biz.sqlite3"
            _seed_real_business_db(db_path).close()
            with self.assertRaises(ValueError):
                install(db_path, skip_backup=True)

    def test_install_requires_an_effective_baseline_citation_and_is_gated(self) -> None:
        # The install() function itself cites the effective design baseline via
        # execution_contract.require_baseline_citations() -- this proves that
        # gate call is real by breaking it and confirming the whole install
        # fails closed, not by mocking the gate to always pass.
        db_path = _scratch_db_path(self, "_scratch_test_citation_gate.sqlite3")
        _seed_real_business_db(db_path).close()
        with mock.patch(
            "scripts.core.persistence.install_versionref_schema_into_business_db.require_baseline_citations",
            side_effect=RuntimeError("no citation"),
        ):
            with self.assertRaises(RuntimeError):
                install(db_path, skip_backup=True)


if __name__ == "__main__":
    unittest.main()
