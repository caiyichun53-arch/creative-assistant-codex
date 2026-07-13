from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.domain_labels import ALLOWED_DOMAIN_LABELS
from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_script_review import (
    assemble_script_review_input,
    review_one_draft,
    run_script_review,
    select_drafts_pending_review,
    validate_script_review_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_script_review_harness


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str = "acc1", domain_label: str = "fan_kepu_social_life") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', ?, 'domain', '半佛仙人', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, domain_label, account_id + "_sec"),
    )


def _insert_hit(conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1", with_transcript: bool = True) -> None:
    video_id = hit_id + "_vid"
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES (?, ?, 'douyin', ?, 't', 'https://x', '{}')
        """,
        (video_id, account_id, video_id + "_item"),
    )
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count,
            hit_channel, judgment_confidence, run_id, preparation_status
        ) VALUES (?, ?, ?, 'douyin', ?, '为什么电梯早高峰总堵', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item"),
    )
    if with_transcript:
        conn.execute(
            """
            INSERT INTO hit_transcripts(
                transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text,
                char_count, asr_model, vad_model, audio_sha256, processing_status, run_id
            ) VALUES (?, ?, 1, ?, ?, 30, 'model', 'vad', 'hash', 'completed', 'run1')
            """,
            (
                hit_id + "_t1", hit_id,
                "每天早高峰电梯都挤不上人。通勤高峰叠加维保停梯让整栋楼排起长队。",
                "每天早高峰电梯都挤不上人。通勤高峰叠加维保停梯让整栋楼排起长队。",
            ),
        )


def _insert_analysis(conn: sqlite3.Connection, analysis_id: str, hit_id: str) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, 1, 'req1', 'corr1', '选题手法', '开头手法', '结构手法', 'model', 'run1')
        """,
        (analysis_id, hit_id),
    )


def _insert_topic(conn: sqlite3.Connection, topic_id: str, analysis_id: str, *, supporting_evidence: list[str] | None = None) -> None:
    conn.execute(
        """
        INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
            no_result_reason, confidence, model_name, run_id)
        VALUES (?, ?, 1, 'req1', 'corr1', 'generated', '候选选题', '切入角度', ?, '[]', 'none', 'high', 'model', 'run1')
        """,
        (topic_id, analysis_id, json.dumps(supporting_evidence or ["证据一"], ensure_ascii=False)),
    )


def _insert_plan(conn: sqlite3.Connection, plan_id: str, topic_id: str) -> None:
    conn.execute(
        """
        INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id,
            hooks, selected_hook, beats, model_name, run_id, human_review_status)
        VALUES (?, ?, 1, 'req1', 'corr1', ?, '选中的开头', ?, 'model', 'run1', 'approved')
        """,
        (plan_id, topic_id, json.dumps(["开头一"], ensure_ascii=False), json.dumps(["第一拍", "第二拍"], ensure_ascii=False)),
    )


def _insert_draft(conn: sqlite3.Connection, draft_id: str, plan_id: str, *, version: int = 1, human_review_status: str = "approved") -> None:
    conn.execute(
        """
        INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id,
            draft_text, model_name, run_id, human_review_status)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, 'model', 'run1', ?)
        """,
        (
            draft_id, plan_id, version,
            "每天早高峰电梯都挤不上人。这不是运气差,是通勤时段和维保时间叠在一起了。" * 2,
            human_review_status,
        ),
    )


def _full_chain(conn: sqlite3.Connection, *, with_transcript: bool = True) -> None:
    _insert_account(conn)
    _insert_hit(conn, "h1", with_transcript=with_transcript)
    _insert_analysis(conn, "a1", "h1")
    _insert_topic(conn, "t1", "a1")
    _insert_plan(conn, "p1", "t1")
    _insert_draft(conn, "d1", "p1")


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_effective_baseline_content_sections(self) -> None:
        contract = validate_script_review_execution_contract()
        self.assertTrue({"5", "10", "20"}.issubset(contract))


class SelectDraftsPendingReviewTests(unittest.TestCase):
    def test_approved_draft_with_no_review_yet_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                pending = select_drafts_pending_review(conn, limit=10)
                self.assertEqual([row["draft_id"] for row in pending], ["d1"])
            finally:
                conn.close()

    def test_draft_already_reviewed_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                conn.execute(
                    """
                    INSERT INTO script_reviews(review_id, source_draft_id, version, request_id, correlation_id,
                        verdict, issues, polished_text, revision_focus, ai_flavor_risk, revision_targets,
                        model_name, run_id)
                    VALUES ('r1', 'd1', 1, 'req1', 'corr1', 'pass', '[]',
                        '足够长的润色文本足够长的润色文本足够长的润色文本足够长的润色文本足够长的润色文本',
                        '["开头留存"]', 'low', '["无"]', 'model', 'run1')
                    """
                )
                pending = select_drafts_pending_review(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_pending_review_draft_is_excluded(self) -> None:
        # Same human-review-gate mechanism as content_plan/script_generate:
        # a draft must be explicitly approved before script_review picks it up.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1")
                _insert_draft(conn, "d1", "p1", human_review_status="pending_review")
                pending = select_drafts_pending_review(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_rejected_draft_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1")
                _insert_draft(conn, "d1", "p1", human_review_status="rejected")
                pending = select_drafts_pending_review(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()


class AssembleScriptReviewInputTests(unittest.TestCase):
    def test_maps_real_fields_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_drafts_pending_review(conn, limit=1)[0]
                payload = assemble_script_review_input(row, run_id="run_test")
            finally:
                conn.close()

        self.assertEqual(payload["request_id"], "script_review_d1")
        self.assertEqual(payload["correlation_id"], "run_test")
        self.assertEqual(payload["schema_version"], "script_review.input.v1")
        self.assertIn("候选选题", payload["brief"])
        self.assertTrue(payload["draft_text"])
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)
        self.assertTrue(any(item["text"] == "证据一" for item in payload["evidence_items"]))
        self.assertGreaterEqual(len(payload["human_reference_refs"]), 1)
        self.assertIn(payload["domain_label"], ALLOWED_DOMAIN_LABELS)

    def test_missing_transcript_raises_instead_of_sending_empty_refs(self) -> None:
        # Reverse case: human_reference_refs is required (minItems: 1) by
        # script_review's own input_schema -- fail loud here rather than
        # silently send an empty list the Skill would reject anyway with a
        # less useful error message.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn, with_transcript=False)
                row = select_drafts_pending_review(conn, limit=1)[0]
                with self.assertRaises(ValueError):
                    assemble_script_review_input(row, run_id="run_test")
            finally:
                conn.close()

    def test_unrecognized_domain_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="some_future_domain")
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1")
                _insert_draft(conn, "d1", "p1")
                row = select_drafts_pending_review(conn, limit=1)[0]
                payload = assemble_script_review_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")


class ReviewOneDraftAndRunTests(unittest.TestCase):
    """Uses the real make_script_review_harness() with its default
    deterministic model port -- no real network call, no real credentials."""

    def test_review_one_draft_persists_a_real_output_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_script_review_harness()
            try:
                _full_chain(conn)
                row = select_drafts_pending_review(conn, limit=1)[0]

                result = review_one_draft(conn, harness, row, run_id="run_test", model_name="deterministic-test")

                self.assertEqual(result["status"], "completed")
                review_row = conn.execute("SELECT * FROM script_reviews WHERE source_draft_id='d1'").fetchone()
                self.assertEqual(review_row["version"], 1)
                self.assertEqual(review_row["request_id"], "script_review_d1")
                self.assertIn(review_row["verdict"], ("pass", "revise", "fail"))
                self.assertTrue(review_row["polished_text"])
                self.assertTrue(json.loads(review_row["revision_focus"]))
                self.assertIn(review_row["ai_flavor_risk"], ("low", "medium", "high"))
                self.assertEqual(review_row["human_review_status"], "pending_review")
            finally:
                conn.close()
                harness.close()

    def test_retry_after_a_prior_review_appends_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_drafts_pending_review(conn, limit=1)[0]
                harness_a = make_script_review_harness()
                try:
                    review_one_draft(conn, harness_a, row, run_id="run_a", model_name="m")
                finally:
                    harness_a.close()
                # select_drafts_pending_review excludes already-reviewed drafts,
                # so re-review directly with the same row to test the version bump.
                harness_b = make_script_review_harness()
                try:
                    review_one_draft(conn, harness_b, row, run_id="run_b", model_name="m")
                finally:
                    harness_b.close()

                versions = [r["version"] for r in conn.execute("SELECT * FROM script_reviews WHERE source_draft_id='d1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_script_review_processes_only_pending_drafts_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_script_review_harness()
            try:
                _full_chain(conn)

                report = run_script_review(conn, limit=10, harness=harness, model_name="m")

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
