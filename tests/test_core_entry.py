from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.core.core_entry import CoreReadOnlySession, _open_read_only_database


class IndependentCoreEntryTests(unittest.TestCase):
    def test_read_only_connection_rejects_writes_in_test_database(self) -> None:
        with TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "test.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            connection.commit()
            connection.close()

            read_only = _open_read_only_database(database_path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute("INSERT INTO sample(value) VALUES ('must-not-write')")
            finally:
                read_only.close()

    def test_basic_status_reads_without_business_writes(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE stage0_daily_run (lifecycle TEXT NOT NULL);
            CREATE TABLE stage0_cold_start (status TEXT NOT NULL);
            CREATE TABLE stage1b_discovery_run (status TEXT NOT NULL);
            CREATE TABLE stage0_content_task (current_status TEXT NOT NULL);
            CREATE TABLE stage0_content_node_version (status TEXT NOT NULL);
            CREATE TABLE stage0_experience_candidate_run (status TEXT NOT NULL);
            CREATE TABLE stage1b_candidate_version (status TEXT NOT NULL);
            INSERT INTO stage0_daily_run VALUES ('failed');
            INSERT INTO stage0_cold_start VALUES ('completed');
            INSERT INTO stage1b_discovery_run VALUES ('processing');
            INSERT INTO stage0_content_task VALUES ('awaiting_human_review');
            INSERT INTO stage0_content_node_version VALUES ('processing');
            INSERT INTO stage0_experience_candidate_run VALUES ('running');
            INSERT INTO stage1b_candidate_version VALUES ('awaiting_user_decision');
            """
        )
        before = connection.execute(
            "SELECT COUNT(*) AS count FROM stage0_daily_run"
        ).fetchone()["count"]
        session = CoreReadOnlySession(
            connection=connection,
            runtime_root=Path("test-runtime"),
            database_path=Path("test.sqlite3"),
            identity_receipt={},
        )
        try:
            status = session.basic_status()
        finally:
            session.close()

        self.assertEqual(status["schema"]["table_count"], 7)
        self.assertEqual(status["business_runs"]["daily_runs"]["total"], 1)
        self.assertTrue(status["unfinished_business_detected"])
        self.assertEqual(status["unfinished_status_counts"]["stage1b_discovery_processing"], 1)
        self.assertEqual(before, 1)

    def test_entry_source_has_no_transport_or_model_imports(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("scripts", "core", "core_entry.py")
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("scripts.agent_platform", text)
        self.assertNotIn("scripts.core.model_gateway", text)
        self.assertNotIn("HERMES_", text)
