from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validation.production_data_sanity_check import (
    check_no_orphaned_hits,
    check_no_stuck_reverse_prep,
    check_required_tables_present,
    run_checklist,
)


def _make_db(tmp_dir: str, sql: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp_dir) / "test.sqlite3")
    conn.executescript(sql)
    return conn


class RequiredTablesTests(unittest.TestCase):
    def test_flags_missing_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, "CREATE TABLE competitor_accounts (account_id TEXT);")
            result = check_required_tables_present(conn)
            conn.close()
        self.assertFalse(result["passed"])
        self.assertIn("hits", result["detail"])

    def test_passes_when_all_present(self) -> None:
        sql = "".join(f"CREATE TABLE {t} (id TEXT);" for t in (
            "competitor_accounts", "competitor_videos", "hits", "video_checks", "hit_transcripts", "hit_comments",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_required_tables_present(conn)
            conn.close()
        self.assertTrue(result["passed"])


class StuckReversePrepTests(unittest.TestCase):
    def test_flags_a_hit_stuck_pending_for_days(self) -> None:
        # Regression guard for the real 2026-07-08 bug: a hit that landed on
        # a stale 'none'/'pending' default and never got picked up.
        sql = """
        CREATE TABLE hits (hit_id TEXT, preparation_status TEXT, promoted_at TEXT);
        INSERT INTO hits VALUES ('h1', 'pending', datetime('now', '-10 days'));
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_no_stuck_reverse_prep(conn)
            conn.close()
        self.assertFalse(result["passed"])
        self.assertEqual(result["detail"][0]["hit_id"], "h1")

    def test_recently_promoted_pending_hit_is_not_flagged(self) -> None:
        sql = """
        CREATE TABLE hits (hit_id TEXT, preparation_status TEXT, promoted_at TEXT);
        INSERT INTO hits VALUES ('h1', 'pending', datetime('now', '-1 hours'));
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_no_stuck_reverse_prep(conn)
            conn.close()
        self.assertTrue(result["passed"])

    def test_completed_hit_is_never_flagged_regardless_of_age(self) -> None:
        sql = """
        CREATE TABLE hits (hit_id TEXT, preparation_status TEXT, promoted_at TEXT);
        INSERT INTO hits VALUES ('h1', 'completed', datetime('now', '-30 days'));
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_no_stuck_reverse_prep(conn)
            conn.close()
        self.assertTrue(result["passed"])


class OrphanedHitsTests(unittest.TestCase):
    def test_flags_a_hit_whose_account_does_not_exist(self) -> None:
        sql = """
        CREATE TABLE competitor_accounts (account_id TEXT);
        CREATE TABLE hits (hit_id TEXT, account_id TEXT);
        INSERT INTO competitor_accounts VALUES ('acc1');
        INSERT INTO hits VALUES ('h1', 'acc_missing');
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_no_orphaned_hits(conn)
            conn.close()
        self.assertFalse(result["passed"])

    def test_passes_when_every_hit_has_a_real_account(self) -> None:
        sql = """
        CREATE TABLE competitor_accounts (account_id TEXT);
        CREATE TABLE hits (hit_id TEXT, account_id TEXT);
        INSERT INTO competitor_accounts VALUES ('acc1');
        INSERT INTO hits VALUES ('h1', 'acc1');
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_db(tmp, sql)
            result = check_no_orphaned_hits(conn)
            conn.close()
        self.assertTrue(result["passed"])


class RunChecklistTests(unittest.TestCase):
    def test_skips_gracefully_when_no_local_database(self) -> None:
        with patch("scripts.validation.production_data_sanity_check.DB_PATH", Path("/nonexistent/nope.sqlite3")):
            result = run_checklist()
        self.assertEqual(result["status"], "SKIPPED")

    def test_real_repo_database_passes_if_present(self) -> None:
        from scripts.validation.production_data_sanity_check import DB_PATH
        if not DB_PATH.exists():
            self.skipTest("no local production database in this checkout")
        result = run_checklist()
        self.assertEqual(result["status"], "PASS", result["checks"])


if __name__ == "__main__":
    unittest.main()
