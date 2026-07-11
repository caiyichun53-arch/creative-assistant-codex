from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_final_draft import (
    propose_one_final_draft,
    run_final_draft,
    select_reviews_pending_final,
    validate_final_draft_execution_contract,
)


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    return conn


def _insert_full_chain_through_review(conn: sqlite3.Connection, *, review_status: str = "approved") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        ) VALUES ('acc1', 'douyin', 'fan_kepu_social_life', 'domain', 'x', 'sec1',
                  'https://x', 'cfg', 'active', 'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """
    )
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES ('v1', 'acc1', 'douyin', 'item1', 't', 'https://x', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, reverse_status)
        VALUES ('h1', 'v1', 'acc1', 'douyin', 'item1', 't', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """
    )
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES ('a1', 'h1', 1, 'req1', 'corr1', 'x', 'x', 'x', 'model', 'run1')
        """
    )
    conn.execute(
        """
        INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
            no_result_reason, confidence, model_name, run_id)
        VALUES ('t1', 'a1', 1, 'req1', 'corr1', 'generated', '选题', '角度', '[]', '[]', 'none', 'high', 'model', 'run1')
        """
    )
    conn.execute(
        """
        INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id,
            hooks, selected_hook, beats, model_name, run_id, human_review_status)
        VALUES ('p1', 't1', 1, 'req1', 'corr1', '[]', '开头', '[]', 'model', 'run1', 'approved')
        """
    )
    conn.execute(
        """
        INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id,
            draft_text, model_name, run_id, human_review_status)
        VALUES ('d1', 'p1', 1, 'req1', 'corr1',
                '足够长的初稿正文足够长的初稿正文足够长的初稿正文足够长的初稿正文足够长的初稿正文', 'model', 'run1', 'approved')
        """
    )
    conn.execute(
        """
        INSERT INTO script_reviews(review_id, source_draft_id, version, request_id, correlation_id,
            verdict, issues, polished_text, revision_focus, ai_flavor_risk, revision_targets,
            model_name, run_id, human_review_status)
        VALUES ('r1', 'd1', 1, 'req1', 'corr1', 'pass', '[]',
                '真正的润色定稿文本真正的润色定稿文本真正的润色定稿文本真正的润色定稿文本真正的润色定稿文本',
                '["开头留存"]', 'low', '["无"]', 'model', 'run1', ?)
        """,
        (review_status,),
    )
    conn.commit()


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_content_005(self) -> None:
        contract = validate_final_draft_execution_contract()
        self.assertIn("BR-CONTENT-005", contract)


class SelectReviewsPendingFinalTests(unittest.TestCase):
    def test_approved_review_with_no_final_draft_yet_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)
                pending = select_reviews_pending_final(conn, limit=10)
                self.assertEqual([row["review_id"] for row in pending], ["r1"])
            finally:
                conn.close()

    def test_pending_review_is_excluded(self) -> None:
        # The human-review gate: a review must be explicitly approved before
        # a final draft can be proposed from it -- review approval and final-
        # draft eligibility are gated on the same human_review_status.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn, review_status="pending_review")
                pending = select_reviews_pending_final(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_rejected_review_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn, review_status="rejected")
                pending = select_reviews_pending_final(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_review_already_promoted_to_final_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)
                conn.execute(
                    "INSERT INTO final_drafts(final_draft_id, source_review_id, version, final_text, run_id) "
                    "VALUES ('f1', 'r1', 1, '定稿文本', 'run1')"
                )
                pending = select_reviews_pending_final(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()


class ProposeOneFinalDraftAndRunTests(unittest.TestCase):
    def test_propose_one_final_draft_copies_the_reviews_polished_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)
                row = select_reviews_pending_final(conn, limit=1)[0]

                result = propose_one_final_draft(conn, row, run_id="run_test")

                self.assertEqual(result["status"], "completed")
                final_row = conn.execute("SELECT * FROM final_drafts WHERE source_review_id='r1'").fetchone()
                self.assertEqual(final_row["version"], 1)
                self.assertEqual(final_row["final_text"], row["polished_text"])
                self.assertEqual(final_row["human_review_status"], "pending_review")
            finally:
                conn.close()

    def test_final_draft_approval_is_separate_from_review_approval(self) -> None:
        # This is the actual point of BR-CONTENT-005: approving script_reviews
        # does NOT automatically approve final_drafts -- they are independent
        # human_review_status columns on independent rows.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)
                row = select_reviews_pending_final(conn, limit=1)[0]
                propose_one_final_draft(conn, row, run_id="run_test")

                final_row = conn.execute("SELECT * FROM final_drafts WHERE source_review_id='r1'").fetchone()
                review_row = conn.execute("SELECT * FROM script_reviews WHERE review_id='r1'").fetchone()
                self.assertEqual(review_row["human_review_status"], "approved")
                self.assertEqual(final_row["human_review_status"], "pending_review")
            finally:
                conn.close()

    def test_retry_after_a_prior_final_draft_appends_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)
                row = select_reviews_pending_final(conn, limit=1)[0]
                propose_one_final_draft(conn, row, run_id="run_a")
                propose_one_final_draft(conn, row, run_id="run_b")

                versions = [r["version"] for r in conn.execute("SELECT * FROM final_drafts WHERE source_review_id='r1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_final_draft_processes_only_pending_reviews_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_full_chain_through_review(conn)

                report = run_final_draft(conn, limit=10)

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
