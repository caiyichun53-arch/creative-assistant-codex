from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_sample_deep_analyze import (
    ALLOWED_DOMAIN_LABELS,
    analyze_one_hit,
    assemble_sample_deep_analyze_input,
    deviation_value_from_hit_channel,
    run_sample_deep_analyze,
    select_hits_pending_analysis,
    select_hits_pending_analysis_by_deviation,
    validate_sample_deep_analyze_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_sample_deep_analyze_harness


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
        VALUES (?, 'douyin', ?, 'domain', 'account', ?, 'https://x', 'cfg', 'active',
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


def _insert_hit(
    conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1", title: str = "为什么电梯早高峰总堵",
    like_count: int = 1000, comment_count: int = 200, share_count: int = 10, collect_count: int = 5,
    reverse_status: str = "completed", hit_channel: str = "like_anomaly",
) -> None:
    video_id = hit_id + "_vid"
    _insert_video(conn, video_id, account_id)
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count,
            hit_channel, judgment_confidence, run_id, reverse_status
        ) VALUES (?, ?, ?, 'douyin', ?, ?, 'https://x', ?, ?, ?, ?, ?, 'formal', 'run1', ?)
        """,
        (hit_id, video_id, account_id, hit_id + "_item", title, like_count, comment_count, share_count, collect_count, hit_channel, reverse_status),
    )


def _insert_transcript(conn: sqlite3.Connection, hit_id: str, *, text: str = "正文", version: int = 1, processing_status: str = "completed") -> None:
    conn.execute(
        """
        INSERT INTO hit_transcripts(
            transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text,
            char_count, asr_model, vad_model, audio_sha256, processing_status, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'model', 'vad', 'hash', ?, 'run1')
        """,
        (f"{hit_id}_v{version}", hit_id, version, text, text, len(text), processing_status),
    )


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_dna_001(self) -> None:
        contract = validate_sample_deep_analyze_execution_contract()
        self.assertIn("BR-DNA-001", contract)


class SelectHitsPendingAnalysisTests(unittest.TestCase):
    def test_only_completed_reverse_status_with_completed_transcript_and_no_prior_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1", reverse_status="completed")
                _insert_transcript(conn, "h1")
                _insert_hit(conn, "h2", reverse_status="pending")  # no transcript, not eligible
                _insert_hit(conn, "h3", reverse_status="completed")
                _insert_transcript(conn, "h3", processing_status="failed")  # failed transcript, not eligible

                pending = select_hits_pending_analysis(conn, limit=10)
                self.assertEqual([row["hit_id"] for row in pending], ["h1"])
            finally:
                conn.close()

    def test_already_analyzed_hit_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                conn.execute(
                    """
                    INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
                        topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
                    VALUES ('a1', 'h1', 1, 'req1', 'corr1', 'topic', 'hook', 'structure', 'model', 'run1')
                    """
                )
                pending = select_hits_pending_analysis(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_uses_latest_completed_transcript_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1", text="第一版", version=1)
                _insert_transcript(conn, "h1", text="第二版更长更完整", version=2)
                pending = select_hits_pending_analysis(conn, limit=10)
                self.assertEqual(pending[0]["transcript_text"], "第二版更长更完整")
            finally:
                conn.close()


class DeviationValueFromHitChannelTests(unittest.TestCase):
    def test_single_anomaly_multiplier(self) -> None:
        self.assertEqual(deviation_value_from_hit_channel("comment_anomaly:4.09x"), 4.09)

    def test_takes_the_highest_of_several_multipliers(self) -> None:
        channel = (
            "like_anomaly:3.86x,comment_anomaly:5.99x,collect_anomaly:6.76x,share_anomaly:10.04x,"
            "multi_indicator:like_anomaly=3.86x;comment_anomaly=5.99x;collect_anomaly=6.76x;share_anomaly=10.04x"
        )
        self.assertEqual(deviation_value_from_hit_channel(channel), 10.04)

    def test_comment_like_ratio_only_has_no_deviation_value(self) -> None:
        self.assertIsNone(deviation_value_from_hit_channel("comment_like_ratio:0.446"))

    def test_p90_small_account_only_has_no_deviation_value(self) -> None:
        self.assertIsNone(deviation_value_from_hit_channel("p90_small_account:like=8836>=p90:3458,comment_like_ratio:0.256"))

    def test_none_input_has_no_deviation_value(self) -> None:
        self.assertIsNone(deviation_value_from_hit_channel(None))

    def test_empty_string_has_no_deviation_value(self) -> None:
        self.assertIsNone(deviation_value_from_hit_channel(""))


class SelectHitsPendingAnalysisByDeviationTests(unittest.TestCase):
    def test_orders_by_highest_deviation_value_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "low", hit_channel="comment_anomaly:2.10x")
                _insert_transcript(conn, "low")
                _insert_hit(conn, "high", hit_channel="share_anomaly:422.75x")
                _insert_transcript(conn, "high")
                _insert_hit(conn, "mid", hit_channel="like_anomaly:6.14x,comment_anomaly:4.72x")
                _insert_transcript(conn, "mid")

                selected = select_hits_pending_analysis_by_deviation(conn, limit=10)

                self.assertEqual([row["hit_id"] for row in selected], ["high", "mid", "low"])
            finally:
                conn.close()

    def test_limit_takes_only_the_top_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "a", hit_channel="like_anomaly:2.0x")
                _insert_transcript(conn, "a")
                _insert_hit(conn, "b", hit_channel="like_anomaly:9.0x")
                _insert_transcript(conn, "b")
                _insert_hit(conn, "c", hit_channel="like_anomaly:5.0x")
                _insert_transcript(conn, "c")

                selected = select_hits_pending_analysis_by_deviation(conn, limit=2)

                self.assertEqual([row["hit_id"] for row in selected], ["b", "c"])
            finally:
                conn.close()

    def test_hits_with_no_deviation_value_are_excluded_not_ranked_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "ratio_only", hit_channel="comment_like_ratio:0.446")
                _insert_transcript(conn, "ratio_only")
                _insert_hit(conn, "has_multiplier", hit_channel="like_anomaly:2.0x")
                _insert_transcript(conn, "has_multiplier")

                selected = select_hits_pending_analysis_by_deviation(conn, limit=10)

                self.assertEqual([row["hit_id"] for row in selected], ["has_multiplier"])
            finally:
                conn.close()

    def test_already_analyzed_hit_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1", hit_channel="like_anomaly:9.0x")
                _insert_transcript(conn, "h1")
                conn.execute(
                    """
                    INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
                        topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
                    VALUES ('a1', 'h1', 1, 'req1', 'corr1', 'topic', 'hook', 'structure', 'model', 'run1')
                    """
                )
                selected = select_hits_pending_analysis_by_deviation(conn, limit=10)
                self.assertEqual(selected, [])
            finally:
                conn.close()

    def test_no_transcript_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1", hit_channel="like_anomaly:9.0x", reverse_status="pending")
                selected = select_hits_pending_analysis_by_deviation(conn, limit=10)
                self.assertEqual(selected, [])
            finally:
                conn.close()


class AssembleSampleDeepAnalyzeInputTests(unittest.TestCase):
    def test_maps_real_fields_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="fan_kepu_social_life")
                _insert_hit(conn, "h1", title="为什么电梯早高峰总堵", like_count=1000, comment_count=200, share_count=10, collect_count=5)
                _insert_transcript(conn, "h1", text="每天早高峰电梯都挤不上")
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                payload = assemble_sample_deep_analyze_input(hit_row, run_id="run_test")
            finally:
                conn.close()

        self.assertEqual(payload["sample_id"], "h1")
        self.assertEqual(payload["candidate_topic"], "为什么电梯早高峰总堵")
        self.assertEqual(payload["transcript_excerpt"], "每天早高峰电梯都挤不上")
        self.assertEqual(payload["metrics"], {"like_count": 1000, "comment_count": 200, "share_count": 10, "collect_count": 5})
        self.assertEqual(payload["domain_label"], "fan_kepu_social_life")
        self.assertEqual(payload["correlation_id"], "run_test")
        self.assertEqual(payload["request_id"], "sample_deep_analyze_h1")
        self.assertEqual(payload["schema_version"], "sample_deep_analyze.input.v1")

    def test_unrecognized_domain_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="some_future_domain_not_in_the_enum")
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                payload = assemble_sample_deep_analyze_input(hit_row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")
        self.assertIn(payload["domain_label"], ALLOWED_DOMAIN_LABELS)

    def test_transcript_excerpt_truncated_to_7000_chars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1", text="字" * 8000)
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                payload = assemble_sample_deep_analyze_input(hit_row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["transcript_excerpt"]), 7000)

    def test_a_real_length_transcript_passes_through_untruncated(self) -> None:
        # Regression for the real bug found 2026-07-09: a 4409-char real
        # transcript got cut to its first half under the old 2200 cap, and the
        # model fabricated an ending for content it never saw. This length is
        # representative of real observed data (max seen so far: 4528 chars),
        # well under the new 7000 cap, and must survive whole.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1", text="字" * 4409)
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                payload = assemble_sample_deep_analyze_input(hit_row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["transcript_excerpt"]), 4409)

    def test_missing_title_falls_back_to_placeholder_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1", title="")
                _insert_transcript(conn, "h1")
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                payload = assemble_sample_deep_analyze_input(hit_row, run_id="run_test")
            finally:
                conn.close()
        self.assertTrue(payload["candidate_topic"])


class AnalyzeOneHitAndRunTests(unittest.TestCase):
    """Uses the real make_sample_deep_analyze_harness() with its default
    deterministic model port -- exercises the real job/worker/materializer
    path this Skill already has full test coverage for, with no real network
    call and no real credentials needed."""

    def test_analyze_one_hit_persists_a_real_output_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_sample_deep_analyze_harness()
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1", title="为什么电梯早高峰总堵")
                _insert_transcript(conn, "h1", text="每天早高峰电梯都挤不上，通勤高峰叠加维保停梯")
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]

                result = analyze_one_hit(conn, harness, hit_row, run_id="run_test", model_name="deterministic-test")

                self.assertEqual(result["status"], "completed")
                row = conn.execute("SELECT * FROM hit_deep_analysis WHERE hit_id='h1'").fetchone()
                self.assertEqual(row["version"], 1)
                self.assertEqual(row["request_id"], "sample_deep_analyze_h1")
                self.assertTrue(row["topic_pattern"])
                self.assertTrue(row["hook_pattern"])
                self.assertTrue(row["structure_pattern"])
                self.assertEqual(row["model_name"], "deterministic-test")
            finally:
                conn.close()
                harness.close()

    def test_retry_after_a_prior_analysis_appends_a_new_version(self) -> None:
        # A hand-crafted second attempt for the same hit (bypassing the
        # "already analyzed" exclusion which run_sample_deep_analyze would
        # normally apply -- e.g. deliberately re-analyzing after a prompt
        # update) must still version up, never overwrite. Uses a fresh
        # harness for the second call, matching real usage: every CLI
        # invocation gets its own fresh in-memory job store (see
        # make_sample_deep_analyze_harness), so a deterministic
        # request_id=sample_deep_analyze_{hit_id} across two *separate*
        # processes is not an idempotency-key collision -- only reusing the
        # same in-process harness for two calls with the same hit would be,
        # which is what this test intentionally avoids.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                hit_row = select_hits_pending_analysis(conn, limit=1)[0]
                harness_a = make_sample_deep_analyze_harness()
                try:
                    analyze_one_hit(conn, harness_a, hit_row, run_id="run_a", model_name="m")
                finally:
                    harness_a.close()
                harness_b = make_sample_deep_analyze_harness()
                try:
                    analyze_one_hit(conn, harness_b, hit_row, run_id="run_b", model_name="m")
                finally:
                    harness_b.close()

                versions = [row["version"] for row in conn.execute("SELECT * FROM hit_deep_analysis WHERE hit_id='h1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_sample_deep_analyze_processes_only_pending_hits_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_sample_deep_analyze_harness()
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_hit(conn, "h2", reverse_status="pending")  # no transcript, excluded

                report = run_sample_deep_analyze(conn, limit=10, harness=harness, model_name="m")

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
