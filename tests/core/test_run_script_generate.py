from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_script_generate import (
    ALLOWED_DOMAIN_LABELS,
    assemble_script_generate_input,
    generate_one_draft,
    run_script_generate,
    select_plans_pending_script,
    validate_script_generate_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_script_generate_harness


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


def _insert_hit(conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1") -> None:
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
        ) VALUES (?, ?, ?, 'douyin', ?, '为什么电梯早高峰总堵', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item"),
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


def _insert_plan(
    conn: sqlite3.Connection, plan_id: str, topic_id: str, *, version: int = 1, beats: list[str] | None = None,
    human_review_status: str = "approved",
) -> None:
    conn.execute(
        """
        INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id,
            hooks, selected_hook, beats, model_name, run_id, human_review_status)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, '选中的开头', ?, 'model', 'run1', ?)
        """,
        (plan_id, topic_id, version, json.dumps(["开头一"], ensure_ascii=False), json.dumps(beats or ["第一拍", "第二拍"], ensure_ascii=False), human_review_status),
    )


def _full_chain(conn: sqlite3.Connection) -> None:
    _insert_account(conn)
    _insert_hit(conn, "h1")
    _insert_analysis(conn, "a1", "h1")
    _insert_topic(conn, "t1", "a1")
    _insert_plan(conn, "p1", "t1")


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_dna_001(self) -> None:
        contract = validate_script_generate_execution_contract()
        self.assertIn("BR-DNA-001", contract)


class SelectPlansPendingScriptTests(unittest.TestCase):
    def test_plan_with_no_draft_yet_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                pending = select_plans_pending_script(conn, limit=10)
                self.assertEqual([row["plan_id"] for row in pending], ["p1"])
            finally:
                conn.close()

    def test_plan_already_drafted_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                conn.execute(
                    """
                    INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id,
                        draft_text, model_name, run_id)
                    VALUES ('d1', 'p1', 1, 'req1', 'corr1', '正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文', 'model', 'run1')
                    """
                )
                pending = select_plans_pending_script(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_pending_review_plan_is_excluded(self) -> None:
        # 2026-07-10: the human review gate -- a plan must be explicitly
        # approved (via review_queue.py) before script_generate will pick it
        # up. Same mechanism/regression rationale as content_plan's own gate
        # test (see test_run_content_plan.py).
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1", human_review_status="pending_review")
                pending = select_plans_pending_script(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_rejected_plan_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1", human_review_status="rejected")
                pending = select_plans_pending_script(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_uses_latest_plan_version_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1", version=1, beats=["第一版大纲"])
                _insert_plan(conn, "p2", "t1", version=2, beats=["第二版大纲"])
                pending = select_plans_pending_script(conn, limit=10)
                self.assertEqual([row["plan_id"] for row in pending], ["p2"])
            finally:
                conn.close()


class AssembleScriptGenerateInputTests(unittest.TestCase):
    def test_maps_real_fields_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_plans_pending_script(conn, limit=1)[0]
                payload = assemble_script_generate_input(row, run_id="run_test")
            finally:
                conn.close()

        self.assertEqual(payload["selected_hook"], "选中的开头")
        self.assertEqual(payload["beats"], ["第一拍", "第二拍"])
        self.assertIn("候选选题", payload["brief"])
        self.assertEqual(payload["request_id"], "script_generate_p1")
        self.assertEqual(payload["correlation_id"], "run_test")
        self.assertEqual(payload["schema_version"], "script_generate.input.v1")
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)
        self.assertTrue(any(item["text"] == "证据一" for item in payload["evidence_items"]))
        self.assertTrue(payload["research_summary"])
        self.assertLessEqual(len(payload["research_summary"]), 1000)
        self.assertIn("证据一", payload["research_summary"])
        self.assertIn(payload["domain_label"], ALLOWED_DOMAIN_LABELS)

    def test_empty_supporting_evidence_falls_back_to_topic_angle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1", supporting_evidence=[])
                _insert_plan(conn, "p1", "t1")
                row = select_plans_pending_script(conn, limit=1)[0]
                payload = assemble_script_generate_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)

    def test_unrecognized_domain_label_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn, domain_label="some_future_domain")
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1")
                _insert_plan(conn, "p1", "t1")
                row = select_plans_pending_script(conn, limit=1)[0]
                payload = assemble_script_generate_input(row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")


class GenerateOneDraftAndRunTests(unittest.TestCase):
    """Uses the real make_script_generate_harness() with its default
    deterministic model port -- no real network call, no real credentials."""

    def test_generate_one_draft_persists_a_real_output_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_script_generate_harness()
            try:
                _full_chain(conn)
                row = select_plans_pending_script(conn, limit=1)[0]

                result = generate_one_draft(conn, harness, row, run_id="run_test", model_name="deterministic-test")

                self.assertEqual(result["status"], "completed")
                draft_row = conn.execute("SELECT * FROM script_drafts WHERE source_plan_id='p1'").fetchone()
                self.assertEqual(draft_row["version"], 1)
                self.assertEqual(draft_row["request_id"], "script_generate_p1")
                self.assertTrue(draft_row["draft_text"])
                self.assertGreaterEqual(len(draft_row["draft_text"]), 50)
                self.assertEqual(draft_row["model_name"], "deterministic-test")
            finally:
                conn.close()
                harness.close()

    def test_retry_after_a_prior_draft_appends_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                row = select_plans_pending_script(conn, limit=1)[0]
                harness_a = make_script_generate_harness()
                try:
                    generate_one_draft(conn, harness_a, row, run_id="run_a", model_name="m")
                finally:
                    harness_a.close()
                harness_b = make_script_generate_harness()
                try:
                    generate_one_draft(conn, harness_b, row, run_id="run_b", model_name="m")
                finally:
                    harness_b.close()

                versions = [r["version"] for r in conn.execute("SELECT * FROM script_drafts WHERE source_plan_id='p1' ORDER BY version").fetchall()]
                self.assertEqual(versions, [1, 2])
            finally:
                conn.close()

    def test_run_script_generate_processes_only_pending_plans_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_script_generate_harness()
            try:
                _full_chain(conn)

                report = run_script_generate(conn, limit=10, harness=harness, model_name="m")

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
