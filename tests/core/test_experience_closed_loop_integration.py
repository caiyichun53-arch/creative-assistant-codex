"""End-to-end integration test for the "候选打法 -> 正式验证 -> 推荐状态 -> 创作复用"
closed loop (C, 2026-07-13, 置顶规则总表核对后, approved production-activation plan's
Part 2). Chains together, using each stage's REAL production entry point (not
hand-rolled test stand-ins):

  A2 (own_publications.py) -- register a self-owned account, real publications,
     real Day7 checks, a real baseline.
  tactic_registry.py -- register a real candidate tactic from a real evidence
     chain (same registration path run_tactic_extract.py would use), then
     promote it out of candidate the same way B1's binding assumes a caller
     eventually will (this promotion step itself has no automated production
     entry point yet -- see TECHNICAL_MANUAL.md's "推荐状态状态机" section --
     so the test does it directly via Goal02StateStore, matching how B1's own
     tests already do).
  B1 (run_publication_experiment.py) -- evaluate a sequence of real
     experiments against real Day7/baseline data, driving the tactic through
     the real active/watch/paused state machine (BR-EXPERIENCE-004, source
     document §7.2-7.7).
  B2 (run_content_plan.py) -- at each checkpoint, verify content_plan's real
     tactic_candidates attribution reflects the tactic's CURRENT
     recommendation status (visible when active/watch, invisible when paused).

The per-module test files (test_own_publications.py, test_tactic_registry.py,
test_goal09_experiments.py, test_run_publication_experiment.py,
test_run_content_plan.py) already cover each piece's own logic in isolation
with many more edge cases; this file's job is narrower and different: prove
the pieces actually compose into one real state machine over a real sequence
of publications, not just that each piece is individually correct.
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
from scripts.core.business_data.run_publication_experiment import evaluate_and_record_experiment
from scripts.core.experience.evidence_registry import register_hit_deep_analysis_evidence
from scripts.core.experience.run_content_plan import _load_real_tactic_candidates_for_domain
from scripts.core.experience.tactic_registry import TacticEvidenceRef, register_tactic_candidate
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.persistence.goal02_store import Goal02StateStore

_GOAL02_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "scripts" / "core" / "persistence" / "goal02_schema.sqlite.sql"
DOMAIN_LABEL = "fan_kepu_social_life"


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    PersistenceStore(conn).install_schema()
    conn.executescript(_GOAL02_SCHEMA_PATH.read_text(encoding="utf-8"))
    install_own_publications_schema(conn)
    return conn


def _insert_hit_and_analysis(conn: sqlite3.Connection, *, hit_id: str, analysis_id: str, topic: str) -> None:
    video_id = hit_id + "_vid"
    conn.execute(
        "INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json) "
        "VALUES (?, 'acc1', 'douyin', ?, 't', 'https://x', '{}')",
        (video_id, video_id + "_item"),
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, reverse_status)
        VALUES (?, ?, 'acc1', 'douyin', ?, '标题', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, hit_id + "_hititem"),
    )
    conn.execute(
        "INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id, "
        "topic_pattern, hook_pattern, structure_pattern, model_name, run_id) "
        "VALUES (?, ?, 1, 'req1', 'corr1', ?, '开头手法', '结构手法', 'model', 'run1')",
        (analysis_id, hit_id, topic),
    )


def _register_real_tactic(conn: sqlite3.Connection) -> str:
    """Real tactic registration via evidence_registry.py + tactic_registry.py
    (the same production path run_tactic_extract.py / run_evidence_registry
    calls) -- not a test-only stand-in. Returns the real tactic_id, still in
    'candidate' state (promotion to active is done by the caller, matching
    the documented gap: no automated candidate->active entry point exists
    yet)."""
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        ) VALUES ('acc1', 'douyin', ?, 'domain', 'x', 'sec1',
                  'https://x', 'cfg', 'active', 'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (DOMAIN_LABEL,),
    )
    evidence_refs = []
    for i in range(2):
        hit_id, analysis_id = f"h{i}", f"a{i}"
        _insert_hit_and_analysis(conn, hit_id=hit_id, analysis_id=analysis_id, topic=f"选题手法{i}")
        result = register_hit_deep_analysis_evidence(conn, analysis_id)
        evidence_refs.append(
            TacticEvidenceRef(
                analysis_id=analysis_id, evidence_version_id=result["version_id"],
                evidence_content_hash=result["target_content_hash"],
            )
        )
    tactic_extract_output = {
        "common_patterns": ["反常识对比开头引发好奇"],
        "example_candidates": ["示例一"],
        "schema_version": "tactic_extract.output.v1",
    }
    result = register_tactic_candidate(
        conn, request_id="req-closed-loop", analysis_batch_id="batch-1", domain_label=DOMAIN_LABEL,
        tactic_extract_output=tactic_extract_output, evidence_refs=tuple(evidence_refs),
        actor="tester", correlation_id="corr-closed-loop",
    )
    return result.tactic_id


def _promote_to_active(conn: sqlite3.Connection, store: PersistenceStore, tactic_id: str) -> None:
    goal02 = Goal02StateStore(store)
    version_id = conn.execute("SELECT current_version_id FROM trace_root WHERE root_id=?", (tactic_id,)).fetchone()["current_version_id"]
    with conn:
        goal02.transition_state(
            object_kind="tactic", object_id=tactic_id, new_state="active",
            basis_version_id=version_id, actor="tester", idempotency_key="promote-to-active",
            expected_row_revision=0,
        )


def _publish_and_evaluate(
    conn: sqlite3.Connection, store: PersistenceStore, *,
    suffix: str, tactic_id: str, core_question: str, published_at: str,
    day7_like_count: int, baseline_median: float = 100.0,
) -> dict:
    draft_id, pub_id, experiment_id = f"d_{suffix}", f"pub_{suffix}", f"exp_{suffix}"
    plan_id, topic_id, analysis_id, hit_id = f"p_{suffix}", f"t_{suffix}", f"a_own_{suffix}", f"h_own_{suffix}"
    video_id = hit_id + "_vid"
    conn.execute(
        "INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json) "
        "VALUES (?, 'acc1', 'douyin', ?, 't', 'https://x', '{}')",
        (video_id, video_id + "_item"),
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, reverse_status)
        VALUES (?, ?, 'acc1', 'douyin', ?, 't', 'https://x', 1, 1, 1, 1, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, hit_id + "_hititem"),
    )
    conn.execute(
        "INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id, "
        "topic_pattern, hook_pattern, structure_pattern, model_name, run_id) "
        "VALUES (?, ?, 1, 'req1', 'corr1', 'x', 'x', 'x', 'model', 'run1')",
        (analysis_id, hit_id),
    )
    conn.execute(
        "INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id, "
        "topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints, "
        "no_result_reason, confidence, model_name, run_id) "
        "VALUES (?, ?, 1, 'req1', 'corr1', 'generated', 'x', 'x', '[]', '[]', 'none', 'high', 'model', 'run1')",
        (topic_id, analysis_id),
    )
    conn.execute(
        "INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id, "
        "hooks, selected_hook, beats, model_name, run_id, human_review_status) "
        "VALUES (?, ?, 1, 'req1', 'corr1', '[]', 'x', '[]', 'model', 'run1', 'approved')",
        (plan_id, topic_id),
    )
    conn.execute(
        "INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id, "
        "draft_text, model_name, run_id, human_review_status) "
        "VALUES (?, ?, 1, 'req1', 'corr1', '足够长的正文足够长的正文足够长的正文足够长的正文足够长的正文', 'model', 'run1', 'approved')",
        (draft_id, plan_id),
    )
    record_own_publication(
        conn, publication_id=pub_id, account_id="acc_self_1", script_draft_id=draft_id,
        platform_url=f"https://douyin.com/video/{suffix}", published_at=published_at,
        human_confirmed_by="用户本人", planned_tactic_id=tactic_id, actually_used_tactic_id=tactic_id,
    )
    record_own_publication_check(
        conn, check_id=f"chk7_{suffix}", publication_id=pub_id, day_since_publish=7,
        like_count=day7_like_count, comment_count=10, share_count=1, collect_count=1, run_id="run1",
    )
    record_own_experiment(
        conn, experiment_id=experiment_id, publication_id=pub_id, primary_hypothesis_tactic_id=tactic_id,
        core_question=core_question, primary_metric="like_count",
        success_rule="like_count ratio >= 1.5", failure_rule="like_count ratio <= 0.8",
        evaluation_observation="P+7d like_count", run_id="run1",
    )
    conn.execute(
        "INSERT OR REPLACE INTO own_publication_baselines(baseline_id, account_id, metric, median_value, sample_count, is_formally_activated, run_id) "
        "VALUES (?, 'acc_self_1', 'like_count', ?, 20, 1, 'run1')",
        (f"baseline_{suffix}", baseline_median),
    )
    conn.commit()
    return evaluate_and_record_experiment(conn, store, experiment_id=experiment_id, actor="tester", idempotency_key=f"eval-{suffix}")


def _tactic_state(conn: sqlite3.Connection, tactic_id: str) -> str:
    return conn.execute("SELECT state FROM tactic_state WHERE tactic_id=?", (tactic_id,)).fetchone()["state"]


class ExperienceClosedLoopIntegrationTests(unittest.TestCase):
    def test_full_active_watch_paused_active_cycle_via_real_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            store = PersistenceStore(conn)
            try:
                tactic_id = _register_real_tactic(conn)
                self.assertEqual(_tactic_state(conn, tactic_id), "candidate")
                self.assertEqual(_load_real_tactic_candidates_for_domain(conn, DOMAIN_LABEL), [])

                _promote_to_active(conn, store, tactic_id)
                self.assertEqual(_tactic_state(conn, tactic_id), "active")
                self.assertEqual(
                    _load_real_tactic_candidates_for_domain(conn, DOMAIN_LABEL), ["反常识对比开头引发好奇"]
                )

                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label=DOMAIN_LABEL)

                # active -[email protected] failure]-> watch
                r1 = _publish_and_evaluate(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=50,
                )
                self.assertEqual(r1["result"], "not_supported")
                self.assertEqual(_tactic_state(conn, tactic_id), "watch")
                self.assertEqual(_load_real_tactic_candidates_for_domain(conn, DOMAIN_LABEL), ["反常识对比开头引发好奇"])

                # watch -[a later support on the same question]-> active
                r2 = _publish_and_evaluate(
                    conn, store, suffix="2", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-02T08:00:00Z", day7_like_count=200,
                )
                self.assertEqual(r2["result"], "supported")
                self.assertEqual(_tactic_state(conn, tactic_id), "active")

                # active -[3 failures across 3 distinct questions since last support]-> paused
                for i, question in zip(("3", "4", "5"), ("q2", "q3", "q4")):
                    result = _publish_and_evaluate(
                        conn, store, suffix=i, tactic_id=tactic_id, core_question=question,
                        published_at=f"2026-07-0{i}T08:00:00Z", day7_like_count=50,
                    )
                self.assertEqual(result["recommendation_status_change"]["to_state"], "paused")
                self.assertEqual(_tactic_state(conn, tactic_id), "paused")
                # Paused tactics must not be recommended to new content plans.
                self.assertEqual(_load_real_tactic_candidates_for_domain(conn, DOMAIN_LABEL), [])

                # paused: a single support is not enough to recover (needs 2 across 2 questions)
                r6 = _publish_and_evaluate(
                    conn, store, suffix="6", tactic_id=tactic_id, core_question="q2",
                    published_at="2026-07-06T08:00:00Z", day7_like_count=200,
                )
                self.assertIsNone(r6["recommendation_status_change"])
                self.assertEqual(_tactic_state(conn, tactic_id), "paused")

                # paused: a new failure resets the recovery count
                r7 = _publish_and_evaluate(
                    conn, store, suffix="7", tactic_id=tactic_id, core_question="q2",
                    published_at="2026-07-07T08:00:00Z", day7_like_count=50,
                )
                self.assertIsNone(r7["recommendation_status_change"])
                self.assertEqual(_tactic_state(conn, tactic_id), "paused")

                # paused -[2 supports across 2 distinct questions since the last failure]-> active
                _publish_and_evaluate(
                    conn, store, suffix="8", tactic_id=tactic_id, core_question="q5",
                    published_at="2026-07-08T08:00:00Z", day7_like_count=200,
                )
                r9 = _publish_and_evaluate(
                    conn, store, suffix="9", tactic_id=tactic_id, core_question="q6",
                    published_at="2026-07-09T08:00:00Z", day7_like_count=200,
                )
                self.assertEqual(r9["recommendation_status_change"]["to_state"], "active")
                self.assertEqual(_tactic_state(conn, tactic_id), "active")
                self.assertEqual(_load_real_tactic_candidates_for_domain(conn, DOMAIN_LABEL), ["反常识对比开头引发好奇"])
            finally:
                conn.close()

    def test_inconclusive_result_never_moves_the_real_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            store = PersistenceStore(conn)
            try:
                tactic_id = _register_real_tactic(conn)
                _promote_to_active(conn, store, tactic_id)
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label=DOMAIN_LABEL)

                result = _publish_and_evaluate(
                    conn, store, suffix="1", tactic_id=tactic_id, core_question="q1",
                    published_at="2026-07-01T08:00:00Z", day7_like_count=110,
                )
                self.assertEqual(result["result"], "inconclusive")
                self.assertIsNone(result["recommendation_status_change"])
                self.assertEqual(_tactic_state(conn, tactic_id), "active")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
