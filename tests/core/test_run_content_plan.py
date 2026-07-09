from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_content_plan import (
    ALLOWED_DOMAIN_LABELS,
    _style_examples_from_transcript,
    assemble_content_plan_input,
    generate_one_plan,
    run_content_plan,
    select_topics_pending_plan,
    validate_content_plan_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_content_plan_harness


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


def _insert_hit(conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1", title: str = "为什么电梯早高峰总堵") -> None:
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
            hit_channel, judgment_confidence, run_id, reverse_status
        ) VALUES (?, ?, ?, 'douyin', ?, ?, 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item", title),
    )


def _insert_transcript(conn: sqlite3.Connection, hit_id: str, *, text: str = "第一句话。第二句话。第三句话。") -> None:
    conn.execute(
        """
        INSERT INTO hit_transcripts(
            transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text,
            char_count, asr_model, vad_model, audio_sha256, processing_status, run_id
        ) VALUES (?, ?, 1, ?, ?, ?, 'model', 'vad', 'hash', 'completed', 'run1')
        """,
        (f"{hit_id}_v1", hit_id, text, text, len(text)),
    )


def _insert_analysis(conn: sqlite3.Connection, analysis_id: str, hit_id: str) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, 1, 'req1', 'corr1', '选题手法示例', '开头手法示例', '结构手法示例', 'model', 'run1')
        """,
        (analysis_id, hit_id),
    )


def _insert_topic(
    conn: sqlite3.Connection, topic_id: str, analysis_id: str, *, version: int = 1, topic_status: str = "generated",
    supporting_evidence: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
            no_result_reason, confidence, model_name, run_id)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, '候选选题', '切入角度', ?, '[]', 'none', 'high', 'model', 'run1')
        """,
        (topic_id, analysis_id, version, topic_status, json.dumps(supporting_evidence or ["证据一"], ensure_ascii=False)),
    )


def _full_chain(conn: sqlite3.Connection, *, transcript_text: str | None = "第一句话。第二句话。第三句话。") -> None:
    _insert_account(conn)
    _insert_hit(conn, "h1")
    if transcript_text is not None:
        _insert_transcript(conn, "h1", text=transcript_text)
    _insert_analysis(conn, "a1", "h1")
    _insert_topic(conn, "t1", "a1")


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_dna_001(self) -> None:
        contract = validate_content_plan_execution_contract()
        self.assertIn("BR-DNA-001", contract)


class StyleExamplesFromTranscriptTests(unittest.TestCase):
    def test_splits_into_sentence_sized_examples(self) -> None:
        examples = _style_examples_from_transcript("第一句话。第二句话！第三句话？")
        self.assertEqual(examples, ["第一句话", "第二句话", "第三句话"])

    def test_empty_transcript_returns_a_placeholder_not_an_empty_list(self) -> None:
        examples = _style_examples_from_transcript("")
        self.assertEqual(len(examples), 1)
        self.assertTrue(examples[0])

    def test_caps_at_six_examples(self) -> None:
        text = "。".join(f"第{i}句" for i in range(20))
        examples = _style_examples_from_transcript(text)
        self.assertLessEqual(len(examples), 6)


class SelectTopicsPendingPlanTests(unittest.TestCase):
    def test_generated_topic_with_no_plan_yet_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual([row["topic_id"] for row in pending], ["t1"])
            finally:
                conn.close()

    def test_needs_review_and_no_result_topics_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1", topic_status="needs_review")
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_topic_already_planned_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                conn.execute(
                    """
                    INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id,
                        hooks, selected_hook, beats, model_name, run_id)
                    VALUES ('p1', 't1', 1, 'req1', 'corr1', '[]', 'hook', '[]', 'model', 'run1')
                    """
                )
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_missing_transcript_does_not_exclude_the_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn, transcript_text=None)
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual([row["topic_id"] for row in pending], ["t1"])
                self.assertIsNone(pending[0]["transcript_text"])
            finally:
                conn.close()


class AssembleContentPlanInputTests(unittest.TestCase):
    def test_maps_real_fields_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_topics_pending_plan(conn, limit=1)[0]
                payload = assemble_content_plan_input(row, run_id="run_test")
            finally:
                conn.close()

        self.assertEqual(payload["candidate_topic"], "候选选题")
        self.assertIn("候选选题", payload["brief"])
        self.assertIn("切入角度", payload["brief"])
        self.assertEqual(payload["request_id"], "content_plan_t1")
        self.assertEqual(payload["correlation_id"], "run_test")
        self.assertEqual(payload["schema_version"], "content_plan.input.v1")
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)
        self.assertTrue(any(item["text"] == "证据一" for item in payload["evidence_items"]))
        self.assertEqual(len(payload["tactic_candidates"]), 3)
        self.assertTrue(any("选题手法示例" in t for t in payload["tactic_candidates"]))
        self.assertGreaterEqual(len(payload["style_examples"]), 1)
        self.assertIn(payload["domain_label"], ALLOWED_DOMAIN_LABELS)

    def test_empty_supporting_evidence_falls_back_to_topic_angle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1", supporting_evidence=[])
                row = select_topics_pending_plan(conn, limit=1)[0]
                payload = assemble_content_plan_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)

    def test_unrecognized_domain_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="some_future_domain")
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                row = select_topics_pending_plan(conn, limit=1)[0]
                payload = assemble_content_plan_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")


class GenerateOnePlanAndRunTests(unittest.TestCase):
    """Uses the real make_content_plan_harness() with its default
    deterministic model port -- no real network call, no real credentials."""

    def test_generate_one_plan_persists_a_real_output_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_content_plan_harness()
            try:
                _full_chain(conn)
                row = select_topics_pending_plan(conn, limit=1)[0]

                result = generate_one_plan(conn, harness, row, run_id="run_test", model_name="deterministic-test")

                self.assertEqual(result["status"], "completed")
                plan_row = conn.execute("SELECT * FROM content_plans WHERE source_topic_id='t1'").fetchone()
                self.assertEqual(plan_row["version"], 1)
                self.assertEqual(plan_row["request_id"], "content_plan_t1")
                self.assertTrue(plan_row["selected_hook"])
                self.assertIsInstance(json.loads(plan_row["hooks"]), list)
                self.assertIsInstance(json.loads(plan_row["beats"]), list)
                self.assertEqual(plan_row["model_name"], "deterministic-test")
            finally:
                conn.close()
                harness.close()

    def test_retry_after_a_prior_plan_appends_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_topics_pending_plan(conn, limit=1)[0]
                harness_a = make_content_plan_harness()
                try:
                    generate_one_plan(conn, harness_a, row, run_id="run_a", model_name="m")
                finally:
                    harness_a.close()
                harness_b = make_content_plan_harness()
                try:
                    generate_one_plan(conn, harness_b, row, run_id="run_b", model_name="m")
                finally:
                    harness_b.close()

                versions = [r["version"] for r in conn.execute("SELECT * FROM content_plans WHERE source_topic_id='t1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_content_plan_processes_only_pending_topics_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_content_plan_harness()
            try:
                _full_chain(conn)

                report = run_content_plan(conn, limit=10, harness=harness, model_name="m")

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
