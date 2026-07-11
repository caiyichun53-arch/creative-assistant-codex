from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.own_publications import (
    compute_own_publication_baseline,
    install_schema as install_own_publications_schema,
    record_own_publication,
    record_own_publication_check,
    register_own_account,
)
from scripts.core.business_data.register_competitor_accounts import install_schema as install_competitor_schema
from scripts.core.persistence.goal01_store import PersistenceStore


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_competitor_schema(conn)
    PersistenceStore(conn).install_schema()
    install_own_publications_schema(conn)
    return conn


def _insert_script_draft(conn: sqlite3.Connection, draft_id: str = "d1") -> None:
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
        "INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json) "
        "VALUES ('v1', 'acc1', 'douyin', 'item1', 't', 'https://x', '{}')"
    )
    conn.execute(
        """
        INSERT INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count, hit_channel, judgment_confidence, run_id, reverse_status)
        VALUES ('h1', 'v1', 'acc1', 'douyin', 'item1', 't', 'https://x', 1, 1, 1, 1, 'like_anomaly', 'formal', 'run1', 'completed')
        """
    )
    conn.execute(
        "INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id, "
        "topic_pattern, hook_pattern, structure_pattern, model_name, run_id) "
        "VALUES ('a1', 'h1', 1, 'req1', 'corr1', 'x', 'x', 'x', 'model', 'run1')"
    )
    conn.execute(
        "INSERT INTO topic_candidates(topic_id, source_analysis_id, version, request_id, correlation_id, "
        "topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints, "
        "no_result_reason, confidence, model_name, run_id) "
        "VALUES ('t1', 'a1', 1, 'req1', 'corr1', 'generated', 'x', 'x', '[]', '[]', 'none', 'high', 'model', 'run1')"
    )
    conn.execute(
        "INSERT INTO content_plans(plan_id, source_topic_id, version, request_id, correlation_id, "
        "hooks, selected_hook, beats, model_name, run_id, human_review_status) "
        "VALUES ('p1', 't1', 1, 'req1', 'corr1', '[]', 'x', '[]', 'model', 'run1', 'approved')"
    )
    conn.execute(
        "INSERT INTO script_drafts(draft_id, source_plan_id, version, request_id, correlation_id, "
        "draft_text, model_name, run_id, human_review_status) "
        "VALUES (?, 'p1', 1, 'req1', 'corr1', '足够长的正文足够长的正文足够长的正文足够长的正文足够长的正文', 'model', 'run1', 'approved')",
        (draft_id,),
    )
    conn.commit()


def _insert_tactic_root(conn: sqlite3.Connection, root_id: str = "tac1") -> None:
    conn.execute("INSERT INTO trace_root(root_id, object_kind) VALUES (?, 'tactic')", (root_id,))
    conn.commit()


class InstallSchemaTests(unittest.TestCase):
    def test_creates_all_five_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                for table in ("own_accounts", "own_publications", "own_publication_checks", "own_publication_baselines", "own_experiments"):
                    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    self.assertTrue(columns, f"{table} was not created")
            finally:
                conn.close()


class RegisterOwnAccountTests(unittest.TestCase):
    def test_registers_a_real_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="张芝士", domain_label="fan_kepu_social_life")
                row = conn.execute("SELECT * FROM own_accounts WHERE account_id='acc_self_1'").fetchone()
                self.assertEqual(row["handle"], "张芝士")
                self.assertEqual(row["status"], "active")
            finally:
                conn.close()

    def test_re_registering_the_same_handle_updates_not_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="张芝士", domain_label="fan_kepu_social_life")
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="张芝士", domain_label="music_entertainment")
                rows = conn.execute("SELECT * FROM own_accounts WHERE handle='张芝士'").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["domain_label"], "music_entertainment")
            finally:
                conn.close()


class RecordOwnPublicationTests(unittest.TestCase):
    def test_records_a_real_confirmed_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
                _insert_script_draft(conn)
                _insert_tactic_root(conn, "tac1")

                pub_id = record_own_publication(
                    conn,
                    publication_id="pub1",
                    account_id="acc_self_1",
                    script_draft_id="d1",
                    platform_url="https://douyin.com/video/real123",
                    published_at="2026-07-13T08:00:00Z",
                    human_confirmed_by="用户本人",
                    planned_tactic_id="tac1",
                    actually_used_tactic_id="tac1",
                    target_age_hours=6.0,
                    actual_age_hours=6.5,
                )

                row = conn.execute("SELECT * FROM own_publications WHERE publication_id=?", (pub_id,)).fetchone()
                self.assertEqual(row["human_confirmed_by"], "用户本人")
                self.assertEqual(row["planned_tactic_id"], "tac1")
                self.assertEqual(row["actual_age_hours"], 6.5)
            finally:
                conn.close()

    def test_missing_human_confirmation_raises(self) -> None:
        # Reverse case: a publication is a real-world fact the system cannot
        # infer -- no confirming person means no record, not a silent default.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
                _insert_script_draft(conn)
                with self.assertRaises(ValueError):
                    record_own_publication(
                        conn,
                        publication_id="pub1",
                        account_id="acc_self_1",
                        script_draft_id="d1",
                        platform_url="https://douyin.com/video/real123",
                        published_at="2026-07-13T08:00:00Z",
                        human_confirmed_by="   ",
                    )
                self.assertIsNone(conn.execute("SELECT * FROM own_publications WHERE publication_id='pub1'").fetchone())
            finally:
                conn.close()

    def test_tactic_attribution_is_optional(self) -> None:
        # planned_tactic_id/actually_used_tactic_id are nullable -- a
        # publication with no real tactic attribution yet must still record
        # successfully, not force a fake value.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
                _insert_script_draft(conn)
                record_own_publication(
                    conn,
                    publication_id="pub1",
                    account_id="acc_self_1",
                    script_draft_id="d1",
                    platform_url="https://douyin.com/video/real123",
                    published_at="2026-07-13T08:00:00Z",
                    human_confirmed_by="用户本人",
                )
                row = conn.execute("SELECT * FROM own_publications WHERE publication_id='pub1'").fetchone()
                self.assertIsNone(row["planned_tactic_id"])
                self.assertIsNone(row["actually_used_tactic_id"])
            finally:
                conn.close()


class ComputeOwnPublicationBaselineTests(unittest.TestCase):
    def _seed_checks(self, conn: sqlite3.Connection, *, count: int) -> None:
        register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
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
        for i in range(count):
            draft_id = f"d{i}"
            _insert_script_draft_variant(conn, draft_id, plan_id=f"p{i}", topic_id=f"t{i}", analysis_id=f"a{i}", hit_id=f"h{i}", video_id=f"v{i}")
            record_own_publication(
                conn,
                publication_id=f"pub{i}",
                account_id="acc_self_1",
                script_draft_id=draft_id,
                platform_url=f"https://douyin.com/video/{i}",
                published_at=f"2026-07-{i + 1:02d}T08:00:00Z",
                human_confirmed_by="用户本人",
            )
            record_own_publication_check(
                conn,
                check_id=f"chk{i}",
                publication_id=f"pub{i}",
                day_since_publish=0,
                like_count=100 + i,
                comment_count=10,
                share_count=1,
                collect_count=1,
                run_id="run1",
            )

    def test_below_20_samples_is_not_formally_activated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                self._seed_checks(conn, count=5)
                result = compute_own_publication_baseline(conn, account_id="acc_self_1", metric="like_count", run_id="run_test")
                self.assertEqual(result["sample_count"], 5)
                self.assertFalse(result["is_formally_activated"])
            finally:
                conn.close()

    def test_20_or_more_samples_is_formally_activated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                self._seed_checks(conn, count=20)
                result = compute_own_publication_baseline(conn, account_id="acc_self_1", metric="like_count", run_id="run_test")
                self.assertEqual(result["sample_count"], 20)
                self.assertTrue(result["is_formally_activated"])
            finally:
                conn.close()

    def test_zero_samples_produces_zero_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
                result = compute_own_publication_baseline(conn, account_id="acc_self_1", metric="like_count", run_id="run_test")
                self.assertEqual(result["sample_count"], 0)
                self.assertFalse(result["is_formally_activated"])
                self.assertEqual(result["median_value"], 0.0)
            finally:
                conn.close()

    def test_unsupported_metric_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_own_account(conn, account_id="acc_self_1", platform="douyin", handle="x", domain_label="fan_kepu_social_life")
                with self.assertRaises(ValueError):
                    compute_own_publication_baseline(conn, account_id="acc_self_1", metric="not_a_real_metric", run_id="run_test")
            finally:
                conn.close()


def _insert_script_draft_variant(
    conn: sqlite3.Connection, draft_id: str, *, plan_id: str, topic_id: str, analysis_id: str, hit_id: str, video_id: str
) -> None:
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
        (hit_id, video_id, video_id + "_hititem"),
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
    conn.commit()


if __name__ == "__main__":
    unittest.main()
