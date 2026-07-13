"""Real tests for scripts/core/business_data/run_publication_experiment.py
(B1, 2026-07-13, 置顶规则总表核对后, BR-EXPERIENCE-004) -- the first real wiring
of goal09_experiments.py's ExperimentMaterializer/recompute_experience_state
to real own_publications/own_experiments data.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.own_publications import (
    compute_own_publication_baseline,
    install_schema as install_own_publications_schema,
    record_own_experiment,
    record_own_publication,
    record_own_publication_check,
    register_own_account,
)
from scripts.core.business_data.register_competitor_accounts import install_schema as install_competitor_schema
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.persistence.goal02_store import Goal02StateStore
from scripts.core.business_data.run_publication_experiment import (
    PublicationExperimentError,
    evaluate_and_record_experiment,
    validate_publication_experiment_execution_contract,
)

_GOAL02_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "scripts" / "core" / "persistence" / "goal02_schema.sqlite.sql"


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    PersistenceStore(conn).install_schema()
    install_own_publications_schema(conn)
    conn.executescript(_GOAL02_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _insert_script_draft(conn: sqlite3.Connection, draft_id: str) -> None:
    suffix = draft_id
    conn.execute(
        "INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json) "
        "VALUES (?, 'acc1', 'douyin', ?, 't', 'https://x', '{}')",
        (f"v_{suffix}", f"item_{suffix}"),
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, preparation_status)
        VALUES (?, ?, 'acc1', 'douyin', ?, 't', 'https://x', 1, 1, 1, 1, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (f"h_{suffix}", f"v_{suffix}", f"hititem_{suffix}"),
    )
    conn.execute(
        "INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id, "
        "topic_pattern, hook_pattern, structure_pattern, model_name, run_id) "
        "VALUES (?, ?, 1, 'req1', 'corr1', 'x', 'x', 'x', 'model', 'run1')",
        (f"a_{suffix}", f"h_{suffix}"),
    )
    conn.execute(
        "INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id, "
        "topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints, "
        "no_result_reason, confidence, model_name, run_id) "
        "VALUES (?, ?, 1, 'req1', 'corr1', 'generated', 'x', 'x', '[]', '[]', 'none', 'high', 'model', 'run1')",
        (f"t_{suffix}", f"a_{suffix}"),
    )
    conn.execute(
        "INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id, "
        "hooks, selected_hook, beats, model_name, run_id, human_review_status) "
        "VALUES (?, ?, 1, 'req1', 'corr1', '[]', 'x', '[]', 'model', 'run1', 'approved')",
        (f"p_{suffix}", f"t_{suffix}"),
    )
    conn.execute(
        "INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id, "
        "draft_text, model_name, run_id, human_review_status) "
        "VALUES (?, ?, 1, 'req1', 'corr1', '足够长的正文足够长的正文足够长的正文足够长的正文足够长的正文', 'model', 'run1', 'approved')",
        (draft_id, f"p_{suffix}"),
    )
    conn.commit()


def _insert_competitor_account(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        ) VALUES ('acc1', 'douyin', 'fan_kepu_social_life', 'domain', 'x', 'sec1',
                  'https://x', 'cfg', 'active', 'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """
    )
    conn.commit()


def _make_tactic(store: PersistenceStore, root_id: str) -> str:
    store.conn.execute("INSERT INTO trace_root(root_id, object_kind) VALUES (?, 'tactic')", (root_id,))
    version_id = store.append_version(root_id, {"tactic_key": root_id})
    store.set_current_version(root_id, version_id)
    store.conn.commit()
    return version_id


def _make_publication_with_experiment(
    conn: sqlite3.Connection,
    store: PersistenceStore,
    *,
    suffix: str,
    tactic_id: str,
    core_question: str,
    published_at: str,
    day7_like_count: int,
    baseline_median: float = 100.0,
    actually_used_tactic_id: str | None = None,
) -> str:
    draft_id = f"d_{suffix}"
    pub_id = f"pub_{suffix}"
    experiment_id = f"exp_{suffix}"
    _insert_script_draft(conn, draft_id)
    record_own_publication(
        conn,
        publication_id=pub_id,
        account_id="acc_self_1",
        script_draft_id=draft_id,
        platform_url=f"https://douyin.com/video/{suffix}",
        published_at=published_at,
        human_confirmed_by="用户本人",
        planned_tactic_id=tactic_id,
        actually_used_tactic_id=actually_used_tactic_id if actually_used_tactic_id is not None else tactic_id,
    )
    record_own_publication_check(
        conn,
        check_id=f"chk7_{suffix}",
        publication_id=pub_id,
        day_since_publish=7,
        like_count=day7_like_count,
        comment_count=10,
        share_count=1,
        collect_count=1,
        run_id="run1",
    )
    record_own_experiment(
        conn,
        experiment_id=experiment_id,
        publication_id=pub_id,
        primary_hypothesis_tactic_id=tactic_id,
        core_question=core_question,
        primary_metric="like_count",
        success_rule="like_count ratio >= 1.5",
        failure_rule="like_count ratio <= 0.8",
        evaluation_observation="P+7d like_count",
        run_id="run1",
    )
    conn.execute(
        "INSERT OR REPLACE INTO own_publication_baselines(baseline_id, account_id, metric, median_value, sample_count, is_formally_activated, run_id) "
        "VALUES (?, 'acc_self_1', 'like_count', ?, 20, 1, 'run1')",
        (f"baseline_{suffix}", baseline_median),
    )
    conn.commit()
    return experiment_id


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_effective_baseline_experience_sections(self) -> None:
        contract = validate_publication_experiment_execution_contract()
        self.assertTrue({"9", "18"}.issubset(contract))


class EvaluateAndRecordExperimentTests(unittest.TestCase):
    def _setup(self, tmp: str) -> tuple[sqlite3.Connection, PersistenceStore, str]:
        conn = _connect(tmp)
        store = PersistenceStore(conn)
        register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
        _insert_competitor_account(conn)
        tactic_id = "tac1"
        _make_tactic(store, tactic_id)
        return conn, store, tactic_id

    def test_supported_result_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="hook_style",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertFalse(report["already_evaluated"])
                self.assertEqual(report["result"], "supported")
                row = conn.execute("SELECT * FROM own_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
                self.assertEqual(row["result"], "supported")
                self.assertIsNotNone(row["result_computed_at"])
            finally:
                conn.close()

    def test_not_supported_result_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="hook_style",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=50, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertEqual(report["result"], "not_supported")
            finally:
                conn.close()

    def test_inconclusive_result_does_not_drive_state_but_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="hook_style",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=110, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertEqual(report["result"], "inconclusive")
                self.assertIsNone(report["recommendation_status_change"])
            finally:
                conn.close()

    def test_already_evaluated_experiment_is_not_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="hook_style",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                )
                first = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                second = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-2",
                )
                self.assertFalse(first["already_evaluated"])
                self.assertTrue(second["already_evaluated"])
                self.assertEqual(second["result"], "supported")
            finally:
                conn.close()

    def test_missing_day7_check_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                draft_id = "d_nocheck"
                _insert_script_draft(conn, draft_id)
                record_own_publication(
                    conn, publication_id="pub_nocheck", account_id="acc_self_1", script_draft_id=draft_id,
                    platform_url="https://x", published_at="2026-07-01T08:00:00Z", human_confirmed_by="u",
                    planned_tactic_id=tactic_id, actually_used_tactic_id=tactic_id,
                )
                record_own_experiment(
                    conn, experiment_id="exp_nocheck", publication_id="pub_nocheck",
                    primary_hypothesis_tactic_id=tactic_id, core_question="q1", primary_metric="like_count",
                    success_rule="x", failure_rule="y", evaluation_observation="z", run_id="run1",
                )
                with self.assertRaises(PublicationExperimentError):
                    evaluate_and_record_experiment(conn, store, experiment_id="exp_nocheck", actor="t", idempotency_key="k1")
            finally:
                conn.close()

    def test_missing_baseline_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                draft_id = "d_nobaseline"
                _insert_script_draft(conn, draft_id)
                record_own_publication(
                    conn, publication_id="pub_nb", account_id="acc_self_1", script_draft_id=draft_id,
                    platform_url="https://x", published_at="2026-07-01T08:00:00Z", human_confirmed_by="u",
                    planned_tactic_id=tactic_id, actually_used_tactic_id=tactic_id,
                )
                record_own_publication_check(
                    conn, check_id="chk_nb", publication_id="pub_nb", day_since_publish=7,
                    like_count=200, comment_count=1, share_count=1, collect_count=1, run_id="run1",
                )
                record_own_experiment(
                    conn, experiment_id="exp_nb", publication_id="pub_nb",
                    primary_hypothesis_tactic_id=tactic_id, core_question="q1", primary_metric="like_count",
                    success_rule="x", failure_rule="y", evaluation_observation="z", run_id="run1",
                )
                with self.assertRaises(PublicationExperimentError):
                    evaluate_and_record_experiment(conn, store, experiment_id="exp_nb", actor="t", idempotency_key="k1")
            finally:
                conn.close()

    def test_actual_use_status_not_used_when_different_tactic_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup(tmp)
            try:
                other_tactic = "tac_other"
                _make_tactic(store, other_tactic)
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                    actually_used_tactic_id=other_tactic,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertEqual(report["metric_signal"]["signal"], "ineligible")
                self.assertIsNone(report["result"])
            finally:
                conn.close()


class RecommendationStateTransitionTests(unittest.TestCase):
    def _setup_active_tactic(self, tmp: str) -> tuple[sqlite3.Connection, PersistenceStore, str]:
        conn = _connect(tmp)
        store = PersistenceStore(conn)
        register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
        _insert_competitor_account(conn)
        tactic_id = "tac1"
        version_id = _make_tactic(store, tactic_id)
        goal02 = Goal02StateStore(store)
        with conn:
            goal02.create_state(
                object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                basis_version_id=version_id, actor="tester", idempotency_key="create-1",
            )
        with conn:
            goal02.transition_state(
                object_kind="tactic", object_id=tactic_id, new_state="active",
                basis_version_id=version_id, actor="tester", idempotency_key="promote-1",
                expected_row_revision=0,
            )
        return conn, store, tactic_id

    def test_one_formal_failure_moves_active_tactic_to_watch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup_active_tactic(tmp)
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=50, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertEqual(report["result"], "not_supported")
                self.assertIsNotNone(report["recommendation_status_change"])
                self.assertEqual(report["recommendation_status_change"]["to_state"], "watch")
                row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "watch")
            finally:
                conn.close()

    def test_watch_recovers_to_active_after_a_later_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup_active_tactic(tmp)
            try:
                exp1 = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=50, baseline_median=100.0,
                )
                evaluate_and_record_experiment(conn, store, experiment_id=exp1, actor="tester", idempotency_key="eval-1")
                self.assertEqual(
                    conn.execute("SELECT state FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["state"],
                    "watch",
                )

                exp2 = _make_publication_with_experiment(
                    conn, store, suffix="2", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-02T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                )
                report2 = evaluate_and_record_experiment(conn, store, experiment_id=exp2, actor="tester", idempotency_key="eval-2")
                self.assertEqual(report2["recommendation_status_change"]["to_state"], "active")
                self.assertEqual(
                    conn.execute("SELECT state FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["state"],
                    "active",
                )
            finally:
                conn.close()

    def test_three_failures_across_three_questions_moves_to_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup_active_tactic(tmp)
            try:
                for i, question in enumerate(["q1", "q2", "q3"], start=1):
                    experiment_id = _make_publication_with_experiment(
                        conn, store, suffix=str(i), tactic_id=tactic_id, core_question=question,
                        published_at=f"2026-07-0{i}T08:00:00Z", day7_like_count=50, baseline_median=100.0,
                    )
                    report = evaluate_and_record_experiment(
                        conn, store, experiment_id=experiment_id, actor="tester", idempotency_key=f"eval-{i}",
                    )
                self.assertEqual(report["recommendation_status_change"]["to_state"], "paused")
                self.assertEqual(
                    conn.execute("SELECT state FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["state"],
                    "paused",
                )
            finally:
                conn.close()

    def test_no_state_change_means_no_transition_recorded(self) -> None:
        # Reverse case: a supported result on an already-active tactic must
        # not produce a spurious tactic_state row_revision bump.
        with tempfile.TemporaryDirectory() as tmp:
            conn, store, tactic_id = self._setup_active_tactic(tmp)
            try:
                before = conn.execute("SELECT row_revision FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["row_revision"]
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertIsNone(report["recommendation_status_change"])
                after = conn.execute("SELECT row_revision FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["row_revision"]
                self.assertEqual(before, after)
            finally:
                conn.close()

    def test_candidate_tactic_is_skipped_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            store = PersistenceStore(conn)
            register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
            _insert_competitor_account(conn)
            tactic_id = "tac1"
            version_id = _make_tactic(store, tactic_id)
            goal02 = Goal02StateStore(store)
            with conn:
                goal02.create_state(
                    object_kind="tactic", object_id=tactic_id, initial_state="candidate",
                    basis_version_id=version_id, actor="tester", idempotency_key="create-1",
                )
            try:
                experiment_id = _make_publication_with_experiment(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=200, baseline_median=100.0,
                )
                report = evaluate_and_record_experiment(
                    conn, store, experiment_id=experiment_id, actor="tester", idempotency_key="eval-1",
                )
                self.assertIsNone(report["recommendation_status_change"])
                row = conn.execute("SELECT state FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()
                self.assertEqual(row["state"], "candidate")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
