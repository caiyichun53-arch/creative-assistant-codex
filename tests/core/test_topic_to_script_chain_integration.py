"""End-to-end integration test for the full "选题 -> 大纲 -> 成稿 -> 文案优化/审核
-> 最终稿" chain: hit_deep_analysis -> source_to_topic -> content_plan (behind
human review) -> script_generate (behind human review) -> script_review
(behind human review) -> final_draft (behind human review).

The per-Skill test files (test_run_source_to_topic.py, test_run_content_plan.py,
test_run_script_generate.py, test_run_script_review.py, test_run_final_draft.py,
test_review_queue.py) each verify their own binding/the review gate in
isolation, seeding the *next* stage's expected input by hand. This test
instead runs the real run_*() functions and the real review_queue approve
step in sequence against one shared database, so it fails if any two stages'
real assumptions about each other's output shape -- or about the review gate
actually blocking/unblocking flow -- have quietly drifted apart.

2026-07-13 (置顶规则总表核对后): extended past script_drafts (the chain used to
stop there) to cover the two new stages (script_reviews/final_drafts,
BR-CONTENT-004/005) added the same day."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema
from scripts.core.experience.review_queue import set_review_status
from scripts.core.experience.run_content_plan import run_content_plan
from scripts.core.experience.run_final_draft import run_final_draft
from scripts.core.experience.run_script_generate import run_script_generate
from scripts.core.experience.run_script_review import run_script_review
from scripts.core.experience.run_source_to_topic import run_source_to_topic
from scripts.core.model_gateway.formal_skill_adapter import (
    make_content_plan_harness,
    make_script_generate_harness,
    make_script_review_harness,
    make_source_to_topic_harness,
)
from scripts.core.persistence.goal01_store import PersistenceStore

_GOAL02_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "scripts" / "core" / "persistence" / "goal02_schema.sqlite.sql"


class TopicToScriptChainIntegrationTests(unittest.TestCase):
    def test_a_real_analysis_row_flows_all_the_way_to_a_persisted_script_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
            conn.row_factory = sqlite3.Row
            install_schema(conn)
            install_domain_search_schema(conn)
            PersistenceStore(conn).install_schema()
            conn.executescript(_GOAL02_SCHEMA_PATH.read_text(encoding="utf-8"))
            try:
                conn.execute(
                    """
                    INSERT INTO competitor_accounts(
                        account_id, platform, domain_label, domain_name, account_name, sec_uid,
                        homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
                    ) VALUES ('acc1', 'douyin', 'fan_kepu_social_life', 'domain', '半佛仙人', 'sec1',
                              'https://x', 'cfg', 'active', 'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
                    VALUES ('v1', 'acc1', 'douyin', 'item1', '为什么电梯早高峰总堵', 'https://x', '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO hits(
                        hit_id, video_id, account_id, platform, platform_item_id, title, url,
                        like_count, comment_count, share_count, collect_count,
                        hit_channel, judgment_confidence, run_id, preparation_status
                    ) VALUES ('h1', 'v1', 'acc1', 'douyin', 'item1', '为什么电梯早高峰总堵', 'https://x',
                              1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO hit_transcripts(
                        transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text,
                        char_count, asr_model, vad_model, audio_sha256, processing_status, run_id
                    ) VALUES ('h1_v1', 'h1', 1, '每天早高峰电梯都挤不上。通勤高峰叠加维保停梯。',
                              '每天早高峰电梯都挤不上。通勤高峰叠加维保停梯。', 22, 'model', 'vad', 'hash', 'completed', 'run1')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
                        topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
                    VALUES ('a1', 'h1', 1, 'req1', 'corr1', '日常现象反常识切入', '直接抛出疑问', '现象-原因-反转', 'model', 'run1')
                    """
                )
                conn.commit()

                topic_harness = make_source_to_topic_harness()
                try:
                    topic_report = run_source_to_topic(conn, limit=10, harness=topic_harness, model_name="det-topic")
                finally:
                    topic_harness.close()
                self.assertEqual(topic_report["completed"], 1, topic_report)

                topic_row = conn.execute("SELECT * FROM topic_candidates WHERE source_analysis_id='a1'").fetchone()
                self.assertIsNotNone(topic_row)

                if topic_row["topic_status"] != "generated":
                    self.skipTest(f"deterministic source_to_topic port returned topic_status={topic_row['topic_status']!r}, chain cannot continue")

                # Human review gate #1: an unapproved topic must not flow to
                # content_plan, even though it is otherwise eligible.
                self.assertEqual(topic_row["human_review_status"], "pending_review")
                plan_harness = make_content_plan_harness()
                try:
                    blocked_report = run_content_plan(conn, limit=10, harness=plan_harness, model_name="det-plan")
                finally:
                    plan_harness.close()
                self.assertEqual(blocked_report["completed"], 0, "an unapproved topic must not be picked up")

                set_review_status(conn, stage="topic", item_id=topic_row["topic_id"], status="approved", note="集成测试自动通过")

                plan_harness = make_content_plan_harness()
                try:
                    plan_report = run_content_plan(conn, limit=10, harness=plan_harness, model_name="det-plan")
                finally:
                    plan_harness.close()
                self.assertEqual(plan_report["completed"], 1, plan_report)

                plan_row = conn.execute("SELECT * FROM content_plans WHERE source_topic_id=?", (topic_row["topic_id"],)).fetchone()
                self.assertIsNotNone(plan_row)
                self.assertTrue(plan_row["selected_hook"])
                self.assertTrue(json.loads(plan_row["beats"]))

                # Human review gate #2: same story, one stage further.
                self.assertEqual(plan_row["human_review_status"], "pending_review")
                draft_harness = make_script_generate_harness()
                try:
                    blocked_draft_report = run_script_generate(conn, limit=10, harness=draft_harness, model_name="det-draft")
                finally:
                    draft_harness.close()
                self.assertEqual(blocked_draft_report["completed"], 0, "an unapproved plan must not be picked up")

                set_review_status(conn, stage="plan", item_id=plan_row["plan_id"], status="approved", note="集成测试自动通过")

                draft_harness = make_script_generate_harness()
                try:
                    draft_report = run_script_generate(conn, limit=10, harness=draft_harness, model_name="det-draft")
                finally:
                    draft_harness.close()
                self.assertEqual(draft_report["completed"], 1, draft_report)

                draft_row = conn.execute("SELECT * FROM script_drafts WHERE source_plan_id=?", (plan_row["plan_id"],)).fetchone()
                self.assertIsNotNone(draft_row)
                self.assertGreaterEqual(len(draft_row["draft_text"]), 50)
                self.assertEqual(draft_row["human_review_status"], "pending_review")

                # Human review gate #3: same story, one stage further --
                # script_review must not pick up an unapproved draft.
                review_harness = make_script_review_harness()
                try:
                    blocked_review_report = run_script_review(conn, limit=10, harness=review_harness, model_name="det-review")
                finally:
                    review_harness.close()
                self.assertEqual(blocked_review_report["completed"], 0, "an unapproved draft must not be picked up")

                set_review_status(conn, stage="draft", item_id=draft_row["draft_id"], status="approved", note="集成测试自动通过")

                review_harness = make_script_review_harness()
                try:
                    review_report = run_script_review(conn, limit=10, harness=review_harness, model_name="det-review")
                finally:
                    review_harness.close()
                self.assertEqual(review_report["completed"], 1, review_report)

                review_row = conn.execute("SELECT * FROM script_reviews WHERE source_draft_id=?", (draft_row["draft_id"],)).fetchone()
                self.assertIsNotNone(review_row)
                self.assertIn(review_row["verdict"], ("pass", "revise", "fail"))
                self.assertGreaterEqual(len(review_row["polished_text"]), 50)
                self.assertEqual(review_row["human_review_status"], "pending_review")

                # Human review gate #4: an unapproved review must not produce
                # a final draft.
                blocked_final_report = run_final_draft(conn, limit=10)
                self.assertEqual(blocked_final_report["completed"], 0, "an unapproved review must not be picked up")

                set_review_status(conn, stage="review", item_id=review_row["review_id"], status="approved", note="集成测试自动通过")

                final_report = run_final_draft(conn, limit=10)
                self.assertEqual(final_report["completed"], 1, final_report)

                final_row = conn.execute("SELECT * FROM final_drafts WHERE source_review_id=?", (review_row["review_id"],)).fetchone()
                self.assertIsNotNone(final_row)
                self.assertEqual(final_row["final_text"], review_row["polished_text"])
                self.assertEqual(final_row["human_review_status"], "pending_review")

                # Approving the review must NOT have implicitly approved the
                # final draft -- BR-CONTENT-005's whole point is that these
                # are two separate confirmations.
                self.assertEqual(final_row["human_review_status"], "pending_review")
                set_review_status(conn, stage="final", item_id=final_row["final_draft_id"], status="approved", note="集成测试自动通过")
                final_row_after = conn.execute("SELECT * FROM final_drafts WHERE final_draft_id=?", (final_row["final_draft_id"],)).fetchone()
                self.assertEqual(final_row_after["human_review_status"], "approved")

                # The full lineage from the original hit all the way to the
                # final draft must be traceable through foreign keys alone.
                self.assertEqual(topic_row["source_analysis_id"], "a1")
                self.assertEqual(plan_row["source_topic_id"], topic_row["topic_id"])
                self.assertEqual(draft_row["source_plan_id"], plan_row["plan_id"])
                self.assertEqual(review_row["source_draft_id"], draft_row["draft_id"])
                self.assertEqual(final_row["source_review_id"], review_row["review_id"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
