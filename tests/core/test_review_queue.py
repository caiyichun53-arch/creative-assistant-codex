from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema
from scripts.core.experience.review_queue import list_pending, set_review_status


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    install_domain_search_schema(conn)
    return conn


def _insert_account(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        ) VALUES ('acc1', 'douyin', 'fan_kepu_social_life', 'domain', '半佛仙人', 'sec1',
                  'https://x', 'cfg', 'active', 'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """
    )


def _insert_hit_and_analysis(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES ('v1', 'acc1', 'douyin', 'item1', '标题', 'https://x', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, preparation_status)
        VALUES ('h1', 'v1', 'acc1', 'douyin', 'item1', '标题', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """
    )
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES ('a1', 'h1', 1, 'req1', 'corr1', '选题手法', '开头手法', '结构手法', 'model', 'run1')
        """
    )


def _insert_topic(conn: sqlite3.Connection, topic_id: str, *, human_review_status: str = "pending_review") -> None:
    conn.execute(
        """
        INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
            no_result_reason, confidence, model_name, run_id, human_review_status)
        VALUES (?, 'a1', 1, 'req1', 'corr1', 'generated', '候选选题', '切入角度', '["证据一"]', '[]', 'none', 'high', 'model', 'run1', ?)
        """,
        (topic_id, human_review_status),
    )


class ListPendingTests(unittest.TestCase):
    def test_lists_pending_review_items_across_all_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1")
                items = list_pending(conn)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["stage"], "topic")
                self.assertEqual(items[0]["id"], "t1")
                self.assertIn("候选选题", items[0]["preview"])
            finally:
                conn.close()

    def test_approved_items_do_not_show_up_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1", human_review_status="approved")
                items = list_pending(conn)
                self.assertEqual(items, [])
            finally:
                conn.close()

    def test_filters_to_a_single_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1")
                self.assertEqual(len(list_pending(conn, stage="topic")), 1)
                self.assertEqual(len(list_pending(conn, stage="plan")), 0)
                self.assertEqual(len(list_pending(conn, stage="draft")), 0)
            finally:
                conn.close()


class SetReviewStatusTests(unittest.TestCase):
    def test_approve_marks_the_row_and_records_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1")

                result = set_review_status(conn, stage="topic", item_id="t1", status="approved", note="看着不错")

                self.assertEqual(result["status"], "approved")
                row = conn.execute("SELECT * FROM topic_candidates WHERE topic_id='t1'").fetchone()
                self.assertEqual(row["human_review_status"], "approved")
                self.assertEqual(row["reviewed_note"], "看着不错")
                self.assertTrue(row["reviewed_at"])
            finally:
                conn.close()

    def test_reject_marks_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1")

                set_review_status(conn, stage="topic", item_id="t1", status="rejected")

                row = conn.execute("SELECT * FROM topic_candidates WHERE topic_id='t1'").fetchone()
                self.assertEqual(row["human_review_status"], "rejected")
            finally:
                conn.close()

    def test_unknown_stage_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    set_review_status(conn, stage="not_a_real_stage", item_id="t1", status="approved")
            finally:
                conn.close()

    def test_invalid_status_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit_and_analysis(conn)
                _insert_topic(conn, "t1")
                with self.assertRaises(ValueError):
                    set_review_status(conn, stage="topic", item_id="t1", status="maybe")
            finally:
                conn.close()

    def test_unknown_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(ValueError):
                    set_review_status(conn, stage="topic", item_id="does_not_exist", status="approved")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
