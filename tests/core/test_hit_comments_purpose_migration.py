from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema

# 2026-07-13 (BR-HIT-007, 置顶规则总表核对后): hit_comments gained a NOT NULL
# purpose column and its PRIMARY KEY changed from (hit_id, comment_id) to
# (hit_id, comment_id, purpose). The real production DB already held 23,893
# comment rows when this change was made, so this cannot be a drop-and-
# recreate the way earlier hit_comments reshapes were (see install_schema's
# 2026-07-08 comment) -- it must be a copy-migrate that preserves every row.
# This file is the test backing for that migration specifically, separate
# from tests/core/test_run_reverse_prep.py's behavioral tests for the
# resulting schema.


def _connect_without_schema(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def _insert_minimal_hit(conn: sqlite3.Connection, hit_id: str) -> None:
    # hit_comments.hit_id is a real foreign key (enforced by this sqlite3
    # build) -- a comment row needs an actual hits row (and its own parents)
    # to reference, not just a matching string.
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', 'domain', 'domain', 'account', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (hit_id + "_acc", hit_id + "_sec"),
    )
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES (?, ?, 'douyin', ?, 't', 'https://x', '{}')
        """,
        (hit_id + "_vid", hit_id + "_acc", hit_id + "_item"),
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url, hit_channel, judgment_confidence, run_id)
        VALUES (?, ?, ?, 'douyin', ?, 't', 'https://x', 'like_anomaly', 'formal', 'run1')
        """,
        (hit_id, hit_id + "_vid", hit_id + "_acc", hit_id + "_hititem"),
    )


class HitCommentsPurposeMigrationTests(unittest.TestCase):
    def test_fresh_database_gets_the_new_shape_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect_without_schema(tmp)
            try:
                install_schema(conn)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(hit_comments)").fetchall()}
                self.assertIn("purpose", columns)
                self.assertIn("observation_point", columns)
                self.assertIn("sampling_strategy", columns)
            finally:
                conn.close()

    def test_existing_old_shape_rows_are_preserved_and_backfilled_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect_without_schema(tmp)
            try:
                install_schema(conn)
                _insert_minimal_hit(conn, "h1")
                # Simulate a database that still has the OLD hit_comments shape
                # (no purpose column, old primary key) by dropping and
                # recreating it exactly as it looked before this change, then
                # writing real-looking rows into it before migrating.
                conn.execute("DROP TABLE hit_comments")
                conn.execute(
                    """
                    CREATE TABLE hit_comments (
                        hit_id TEXT NOT NULL,
                        comment_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        like_count INTEGER NOT NULL DEFAULT 0,
                        parent_comment_id TEXT,
                        sample_rank INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (hit_id, comment_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, run_id)
                    VALUES ('h1', 'c1', '真实历史评论', 42, 0, 'old_run')
                    """
                )
                conn.commit()

                install_schema(conn)  # must detect the old shape and migrate, not skip or drop

                rows = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchall()
                self.assertEqual(len(rows), 1, "migration must not lose the pre-existing row")
                self.assertEqual(rows[0]["text"], "真实历史评论")
                self.assertEqual(rows[0]["like_count"], 42)
                self.assertEqual(rows[0]["run_id"], "old_run")
                self.assertEqual(
                    rows[0]["purpose"], "mature_analysis",
                    "pre-existing rows must be backfilled to the one honestly defensible "
                    "default (mature_analysis), not left NULL or guessed at random",
                )
            finally:
                conn.close()

    def test_migration_is_idempotent_running_install_schema_twice_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect_without_schema(tmp)
            try:
                install_schema(conn)
                _insert_minimal_hit(conn, "h1")
                conn.execute(
                    """
                    INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
                    VALUES ('h1', 'c1', '评论', 1, 0, 'mature_analysis', 'run1')
                    """
                )
                conn.commit()

                install_schema(conn)  # already migrated -- must be a no-op for hit_comments

                rows = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()

    def test_new_shape_rejects_a_row_with_no_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect_without_schema(tmp)
            try:
                install_schema(conn)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, run_id)
                        VALUES ('h1', 'c1', '评论', 1, 0, 'run1')
                        """
                    )
            finally:
                conn.close()

    def test_new_shape_rejects_an_unknown_purpose_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect_without_schema(tmp)
            try:
                install_schema(conn)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
                        VALUES ('h1', 'c1', '评论', 1, 0, 'not_a_real_purpose', 'run1')
                        """
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
