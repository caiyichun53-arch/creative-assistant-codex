from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_source_to_topic import (
    ALLOWED_DOMAIN_LABELS,
    REVERSE_PREP_MAX_COMMENTS_PER_HIT,
    SOURCE_EVIDENCE_ITEMS_MAX,
    _comment_evidence_items,
    assemble_source_to_topic_input,
    generate_one_topic,
    run_source_to_topic,
    select_analyses_pending_topic,
    validate_source_to_topic_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_source_to_topic_harness


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


def _insert_video(conn: sqlite3.Connection, video_id: str, account_id: str) -> None:
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES (?, ?, 'douyin', ?, 't', 'https://x', '{}')
        """,
        (video_id, account_id, video_id + "_item"),
    )


def _insert_hit(conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1", title: str = "为什么电梯早高峰总堵") -> None:
    video_id = hit_id + "_vid"
    _insert_video(conn, video_id, account_id)
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count,
            hit_channel, judgment_confidence, run_id, reverse_status
        ) VALUES (?, ?, ?, 'douyin', ?, ?, 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item", title),
    )


def _insert_analysis(
    conn: sqlite3.Connection, analysis_id: str, hit_id: str, *, version: int = 1,
    topic_pattern: str = "选题手法示例", hook_pattern: str = "开头手法示例", structure_pattern: str = "结构手法示例",
) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, ?, ?, 'model', 'run1')
        """,
        (analysis_id, hit_id, version, topic_pattern, hook_pattern, structure_pattern),
    )


def _insert_comment(conn: sqlite3.Connection, hit_id: str, comment_id: str, *, text: str, sample_rank: int, like_count: int = 10) -> None:
    conn.execute(
        """
        INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
        VALUES (?, ?, ?, ?, ?, 'mature_analysis', 'run1')
        """,
        (hit_id, comment_id, text, like_count, sample_rank),
    )


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_dna_001(self) -> None:
        contract = validate_source_to_topic_execution_contract()
        self.assertIn("BR-DNA-001", contract)


class SelectAnalysesPendingTopicTests(unittest.TestCase):
    def test_analysis_with_no_topic_yet_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                pending = select_analyses_pending_topic(conn, limit=10)
                self.assertEqual([row["analysis_id"] for row in pending], ["a1"])
            finally:
                conn.close()

    def test_analysis_already_turned_into_a_topic_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                conn.execute(
                    """
                    INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
                        topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
                        no_result_reason, confidence, model_name, run_id)
                    VALUES ('t1', 'a1', 1, 'req1', 'corr1', 'generated', '选题', '角度', '[]', '[]', 'none', 'high', 'model', 'run1')
                    """
                )
                pending = select_analyses_pending_topic(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_uses_latest_analysis_version_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1", version=1, topic_pattern="第一版选题手法")
                _insert_analysis(conn, "a2", "h1", version=2, topic_pattern="第二版选题手法")
                pending = select_analyses_pending_topic(conn, limit=10)
                self.assertEqual([row["analysis_id"] for row in pending], ["a2"])
            finally:
                conn.close()

    def test_pulls_all_comments_ordered_by_sample_rank_no_cap(self) -> None:
        # 2026-07-11: the old "3" per-hit cap was removed (explicit user
        # decision) -- reverse-prep's own real BR-COLLECT-005/006 limit
        # (top_comments=60) is the only real cap, so a 4th comment (and
        # beyond) must come through here, not get silently dropped.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_comment(conn, "h1", "c3", text="第四热评论,现在应该出现", sample_rank=3)
                _insert_comment(conn, "h1", "c1", text="最热评论", sample_rank=0)
                _insert_comment(conn, "h1", "c2", text="第二热评论", sample_rank=1)
                _insert_comment(conn, "h1", "c0", text="第三热评论", sample_rank=2)
                row = select_analyses_pending_topic(conn, limit=1)[0]
                comments = row["top_comments_text"].split("\x1e")
                self.assertEqual(comments, ["最热评论", "第二热评论", "第三热评论", "第四热评论,现在应该出现"])
            finally:
                conn.close()

    def test_hit_with_no_comments_has_null_top_comments_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                row = select_analyses_pending_topic(conn, limit=1)[0]
                self.assertIsNone(row["top_comments_text"])
            finally:
                conn.close()


class CommentEvidenceItemsTests(unittest.TestCase):
    def test_splits_delimited_comments_into_labeled_items(self) -> None:
        items = _comment_evidence_items("评论一\x1e评论二\x1e评论三")
        self.assertEqual(items, ["热门评论:评论一", "热门评论:评论二", "热门评论:评论三"])

    def test_none_returns_empty_list(self) -> None:
        self.assertEqual(_comment_evidence_items(None), [])

    def test_empty_string_returns_empty_list(self) -> None:
        self.assertEqual(_comment_evidence_items(""), [])

    def test_truncates_to_240_chars_per_item(self) -> None:
        items = _comment_evidence_items("字" * 500)
        self.assertEqual(len(items[0]), 240)


class AssembleSourceToTopicInputTests(unittest.TestCase):
    def test_maps_real_fields_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="fan_kepu_social_life")
                _insert_hit(conn, "h1", title="为什么电梯早高峰总堵")
                _insert_analysis(conn, "a1", "h1", topic_pattern="日常现象反常识切入", hook_pattern="直接抛出疑问", structure_pattern="现象-原因-反转")
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()

        self.assertEqual(payload["source_id"], "a1")
        self.assertEqual(payload["domain_label"], "fan_kepu_social_life")
        self.assertEqual(payload["correlation_id"], "run_test")
        self.assertEqual(payload["request_id"], "source_to_topic_a1")
        self.assertEqual(payload["schema_version"], "source_to_topic.input.v1")
        self.assertIn("半佛仙人", payload["source_content"])
        self.assertIn("为什么电梯早高峰总堵", payload["source_content"])
        self.assertEqual(len(payload["source_evidence_items"]), 3)
        self.assertTrue(any("日常现象反常识切入" in item for item in payload["source_evidence_items"]))
        self.assertTrue(any("直接抛出疑问" in item for item in payload["source_evidence_items"]))
        self.assertTrue(any("现象-原因-反转" in item for item in payload["source_evidence_items"]))
        self.assertTrue(payload["relation_summary"])
        self.assertLessEqual(len(payload["relation_summary"]), 600)

    def test_top_comments_are_mixed_into_evidence_items_alongside_the_analysis_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_comment(conn, "h1", "c1", text="求你讲讲这个话题", sample_rank=0)
                _insert_comment(conn, "h1", "c2", text="太真实了", sample_rank=1)
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["source_evidence_items"]), 5)
        self.assertTrue(any("求你讲讲这个话题" in item for item in payload["source_evidence_items"]))
        self.assertTrue(any("太真实了" in item for item in payload["source_evidence_items"]))
        self.assertTrue(any(item.startswith("热门评论:") for item in payload["source_evidence_items"]))

    def test_no_comments_still_produces_the_three_pattern_items_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["source_evidence_items"]), 3)

    def test_unrecognized_domain_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="some_future_domain_not_in_the_enum")
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")
        self.assertIn(payload["domain_label"], ALLOWED_DOMAIN_LABELS)

    def test_evidence_items_respect_the_240_char_schema_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1", topic_pattern="字" * 500, hook_pattern="字" * 500, structure_pattern="字" * 500)
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()
        for item in payload["source_evidence_items"]:
            self.assertLessEqual(len(item), 240)

    def test_real_max_comment_count_fits_without_the_defensive_error(self) -> None:
        # 2026-07-11 regression: SOURCE_EVIDENCE_ITEMS_MAX(63) must actually
        # cover the real upstream ceiling (3 pattern items +
        # REVERSE_PREP_MAX_COMMENTS_PER_HIT comments) -- proves the derived
        # number is correct, not just that it compiles.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                for i in range(REVERSE_PREP_MAX_COMMENTS_PER_HIT):
                    _insert_comment(conn, "h1", f"c{i}", text=f"评论{i}", sample_rank=i)
                row = select_analyses_pending_topic(conn, limit=1)[0]
                payload = assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["source_evidence_items"]), 3 + REVERSE_PREP_MAX_COMMENTS_PER_HIT)
        self.assertEqual(len(payload["source_evidence_items"]), SOURCE_EVIDENCE_ITEMS_MAX)

    def test_exceeding_the_real_ceiling_raises_instead_of_silently_truncating(self) -> None:
        # Simulates data that should be impossible given reverse-prep's real
        # cap, proving the fail-loud safety net fires rather than silently
        # cutting evidence the way the old hardcoded "3" cap did.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                for i in range(REVERSE_PREP_MAX_COMMENTS_PER_HIT + 1):
                    _insert_comment(conn, "h1", f"c{i}", text=f"评论{i}", sample_rank=i)
                row = select_analyses_pending_topic(conn, limit=1)[0]
                with self.assertRaises(ValueError):
                    assemble_source_to_topic_input(row, run_id="run_test")
            finally:
                conn.close()


class GenerateOneTopicAndRunTests(unittest.TestCase):
    """Uses the real make_source_to_topic_harness() with its default
    deterministic model port -- no real network call, no real credentials."""

    def test_generate_one_topic_persists_a_real_output_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_source_to_topic_harness()
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                row = select_analyses_pending_topic(conn, limit=1)[0]

                result = generate_one_topic(conn, harness, row, run_id="run_test", model_name="deterministic-test")

                self.assertEqual(result["status"], "completed")
                topic_row = conn.execute("SELECT * FROM topic_candidates WHERE source_analysis_id='a1'").fetchone()
                self.assertEqual(topic_row["version"], 1)
                self.assertEqual(topic_row["request_id"], "source_to_topic_a1")
                self.assertTrue(topic_row["candidate_topic"])
                self.assertEqual(topic_row["model_name"], "deterministic-test")
                self.assertIsInstance(json.loads(topic_row["supporting_evidence"]), list)
                self.assertIsInstance(json.loads(topic_row["source_constraints"]), list)
            finally:
                conn.close()
                harness.close()

    def test_retry_after_a_prior_topic_appends_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                row = select_analyses_pending_topic(conn, limit=1)[0]
                harness_a = make_source_to_topic_harness()
                try:
                    generate_one_topic(conn, harness_a, row, run_id="run_a", model_name="m")
                finally:
                    harness_a.close()
                harness_b = make_source_to_topic_harness()
                try:
                    generate_one_topic(conn, harness_b, row, run_id="run_b", model_name="m")
                finally:
                    harness_b.close()

                versions = [r["version"] for r in conn.execute("SELECT * FROM topic_candidates WHERE source_analysis_id='a1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_source_to_topic_processes_only_pending_analyses_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_source_to_topic_harness()
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")

                report = run_source_to_topic(conn, limit=10, harness=harness, model_name="m")

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
