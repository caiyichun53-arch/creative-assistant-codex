from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.run_content_plan import (
    ALLOWED_DOMAIN_LABELS,
    _load_real_tactic_candidates_for_domain,
    _style_examples_from_transcript,
    assemble_content_plan_input,
    generate_one_plan,
    run_content_plan,
    select_topics_pending_plan,
    validate_content_plan_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_content_plan_harness
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.persistence.goal02_store import Goal02StateStore

_GOAL02_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "scripts" / "core" / "persistence" / "goal02_schema.sqlite.sql"


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    PersistenceStore(conn).install_schema()
    conn.executescript(_GOAL02_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _make_tactic(
    conn: sqlite3.Connection, root_id: str, *, domain_label: str, common_patterns: list[str], state: str
) -> None:
    """Registers a real tactic_state row the way tactic_registry.py would --
    trace_root/trace_version payload carrying domain_label/common_patterns,
    promoted from candidate to `state` via Goal02StateStore (matching B1's
    real transition mechanism, not a hand-rolled shortcut)."""
    store = PersistenceStore(conn)
    conn.execute("INSERT INTO trace_root(root_id, object_kind) VALUES (?, 'tactic')", (root_id,))
    version_id = store.append_version(
        root_id, {"domain_label": domain_label, "common_patterns": common_patterns, "example_candidates": ["x"]}
    )
    store.set_current_version(root_id, version_id)
    conn.commit()
    goal02 = Goal02StateStore(store)
    with conn:
        goal02.create_state(
            object_kind="tactic", object_id=root_id, initial_state="candidate",
            basis_version_id=version_id, actor="tester", idempotency_key=f"create-{root_id}",
        )
    if state != "candidate":
        with conn:
            goal02.transition_state(
                object_kind="tactic", object_id=root_id, new_state=state,
                basis_version_id=version_id, actor="tester", idempotency_key=f"promote-{root_id}",
                expected_row_revision=0,
            )


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
            hit_channel, judgment_confidence, run_id, preparation_status
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
    supporting_evidence: list[str] | None = None, human_review_status: str = "approved",
) -> None:
    conn.execute(
        """
        INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints,
            no_result_reason, confidence, model_name, run_id, human_review_status)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, '候选选题', '切入角度', ?, '[]', 'none', 'high', 'model', 'run1', ?)
        """,
        (topic_id, analysis_id, version, topic_status, json.dumps(supporting_evidence or ["证据一"], ensure_ascii=False), human_review_status),
    )


def _full_chain(conn: sqlite3.Connection, *, transcript_text: str | None = "第一句话。第二句话。第三句话。") -> None:
    _insert_account(conn)
    _insert_hit(conn, "h1")
    if transcript_text is not None:
        _insert_transcript(conn, "h1", text=transcript_text)
    _insert_analysis(conn, "a1", "h1")
    _insert_topic(conn, "t1", "a1")


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_effective_baseline_content_sections(self) -> None:
        contract = validate_content_plan_execution_contract()
        self.assertTrue({"5", "10", "20"}.issubset(contract))


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


class LoadRealTacticCandidatesForDomainTests(unittest.TestCase):
    def test_no_active_or_watch_tactic_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(candidates, [])

    def test_candidate_state_tactic_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _make_tactic(conn, "tac1", domain_label="fan_kepu_social_life", common_patterns=["x"], state="candidate")
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(candidates, [])

    def test_paused_and_deprecated_tactics_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _make_tactic(conn, "tac_paused", domain_label="fan_kepu_social_life", common_patterns=["paused打法"], state="paused")
                _make_tactic(conn, "tac_deprecated", domain_label="fan_kepu_social_life", common_patterns=["deprecated打法"], state="deprecated")
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(candidates, [])

    def test_active_tactic_in_a_different_domain_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _make_tactic(conn, "tac1", domain_label="music_entertainment", common_patterns=["音乐领域打法"], state="active")
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(candidates, [])

    def test_active_tactics_are_ordered_before_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _make_tactic(conn, "tac_watch", domain_label="fan_kepu_social_life", common_patterns=["watch打法"], state="watch")
                _make_tactic(conn, "tac_active", domain_label="fan_kepu_social_life", common_patterns=["active打法"], state="active")
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(candidates, ["active打法", "watch打法"])

    def test_capped_at_twelve_total_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _make_tactic(
                    conn, "tac1", domain_label="fan_kepu_social_life",
                    common_patterns=[f"打法{i}" for i in range(20)], state="active",
                )
                candidates = _load_real_tactic_candidates_for_domain(conn, "fan_kepu_social_life")
            finally:
                conn.close()
        self.assertEqual(len(candidates), 12)


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

    def test_pending_review_topic_is_excluded(self) -> None:
        # 2026-07-10: the human review gate -- a generated topic must be
        # explicitly approved (via review_queue.py) before content_plan will
        # pick it up. Regression guard for the real gap found the same day
        # the chain was first wired: nothing previously stopped an
        # unreviewed topic from auto-flowing to a plan/draft.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1", human_review_status="pending_review")
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual(pending, [])
            finally:
                conn.close()

    def test_rejected_topic_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_transcript(conn, "h1")
                _insert_analysis(conn, "a1", "h1")
                _insert_topic(conn, "t1", "a1", human_review_status="rejected")
                pending = select_topics_pending_plan(conn, limit=10)
                self.assertEqual(pending, [])
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
                payload = assemble_content_plan_input(conn, row, run_id="run_test")
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
                payload = assemble_content_plan_input(conn, row, run_id="run_test")
            finally:
                conn.close()
        self.assertGreaterEqual(len(payload["evidence_items"]), 1)

    def test_uses_real_active_tactic_common_patterns_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                _make_tactic(
                    conn, "tac1", domain_label="fan_kepu_social_life",
                    common_patterns=["真实打法一", "真实打法二"], state="active",
                )
                row = select_topics_pending_plan(conn, limit=1)[0]
                payload = assemble_content_plan_input(conn, row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(payload["tactic_candidates"], ["真实打法一", "真实打法二"])

    def test_falls_back_to_per_hit_analysis_when_no_active_or_watch_tactic_exists(self) -> None:
        # Reverse case: a candidate (never promoted) or paused/deprecated
        # tactic must NOT be surfaced as a real recommendation.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _full_chain(conn)
                _make_tactic(
                    conn, "tac_candidate", domain_label="fan_kepu_social_life",
                    common_patterns=["不该出现的候选打法"], state="candidate",
                )
                row = select_topics_pending_plan(conn, limit=1)[0]
                payload = assemble_content_plan_input(conn, row, run_id="run_test")
            finally:
                conn.close()
        self.assertEqual(len(payload["tactic_candidates"]), 3)
        self.assertNotIn("不该出现的候选打法", payload["tactic_candidates"])

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
                payload = assemble_content_plan_input(conn, row, run_id="run_test")
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
