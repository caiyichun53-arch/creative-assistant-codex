from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.core.business_data.register_competitor_accounts import install_schema, register_from_domain
from scripts.core.business_data.run_competitor_registration_full import (
    ROOT,
    classify_first_contact_category,
    ingest_daily_incremental_items,
    ingest_stock_items,
    judge_account,
    judge_domain,
    resolve_max_notes,
    run_daily_incremental,
    run_full_registration,
    run_rejudge_only,
    stable_check_id,
    stable_video_id,
    summarize,
    validate_registration_execution_contract,
)

# 2026-07-07: this whole file was rewritten from scratch for the master-doc
# realignment design (BUSINESS_RULE_CATALOG.yaml BR-HIT-001
# amendment_2026_07_07_master_doc_realignment). It replaces the prior test suite,
# which tested a design this rewrite explicitly retired: pinned-detection, the
# watching/archived/promoted status machine + retraction/graduation, the
# median*excess_threshold/P90/floor formula, and baseline_min_samples 30/10. None
# of those concepts exist anymore -- see the module itself and BUSINESS_RULE_
# CATALOG.yaml BR-HIT-001/002/003/005 for the current design.

DOMAIN_LABEL = "fan_kepu_social_life"
DOMAIN = {
    "name": "泛科普-社会与生活",
    "formal_domain_label": DOMAIN_LABEL,
    "platform": "douyin",
    "collector_policy": {
        "first_crawl": "stock_snapshot_archived",
        "comments": "reverse_prep_only_for_promoted_hits",
    },
    "competitor_seeds": [
        {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
    ],
}

HIT_CFG = {
    "observe_days": 7,
    "discovery_delay_hours_max": 36,
    "mature_history_window_days": 90,
    "mature_history_max_samples": 50,
    "unified_min_samples": 20,
    "formal_baseline_activation_min_samples": 20,
    "formal_baseline_computation_window": 50,
    "single_metric_excess_threshold": 2.0,
    "multi_indicator_excess_threshold": 1.6,
    "cold_start_d7_single_metric_threshold": 3.0,
    "cold_start_d7_multi_indicator_threshold": 2.0,
    "mature_history_absolute_like_floor": 2000,
    "small_account_p90_percentile": 0.9,
    "comment_like_ratio_threshold": 0.2,
}


def _connect(tmp: str, name: str = "test.sqlite3") -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / name)
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str = "acc1") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', ?, 'domain', 'account', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, DOMAIN_LABEL, account_id + "_sec"),
    )


def _insert_video(
    conn: sqlite3.Connection,
    video_id: str,
    account_id: str,
    platform_item_id: str,
    *,
    category: str | None,
    publish_time: datetime | None,
    like_count: int | None = None,
    comment_count: int | None = None,
    share_count: int | None = None,
    collect_count: int | None = None,
    discovery_delay_hours: float | None = None,
    tracking_completed: int = 0,
    mature_snapshot_taken_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO competitor_videos(
            video_id, account_id, platform, platform_item_id, title, url, publish_time,
            like_count, comment_count, share_count, collect_count,
            first_contact_category, discovery_delay_hours, tracking_completed,
            mature_snapshot_taken_at, registration_run_id, raw_archive_ref, raw_json
        )
        VALUES (?, ?, 'douyin', ?, 't', 'https://x', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'run1', NULL, '{}')
        """,
        (
            video_id, account_id, platform_item_id,
            publish_time.isoformat() if publish_time else None,
            like_count, comment_count, share_count, collect_count,
            category, discovery_delay_hours, tracking_completed, mature_snapshot_taken_at,
        ),
    )


def _insert_check(
    conn: sqlite3.Connection,
    video_id: str,
    run_id: str,
    *,
    discovery_batch_index: int | None,
    like_count: int | None = None,
    comment_count: int | None = None,
    share_count: int | None = None,
    collect_count: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO video_checks(check_id, video_id, discovery_batch_index, like_count, comment_count, share_count, collect_count, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_check_id(video_id, run_id) + (f":{discovery_batch_index}" if discovery_batch_index is not None else ":x"),
            video_id, discovery_batch_index, like_count, comment_count, share_count, collect_count, run_id,
        ),
    )


class ClassifyFirstContactCategoryTests(unittest.TestCase):
    def test_historical_mature_at_exactly_7_days(self) -> None:
        now = datetime.now(timezone.utc)
        published_at = now - timedelta(days=7)
        self.assertEqual(classify_first_contact_category(published_at, now, observe_days=7), "historical_mature")

    def test_transition_when_1_to_6_days(self) -> None:
        now = datetime.now(timezone.utc)
        published_at = now - timedelta(days=3)
        self.assertEqual(classify_first_contact_category(published_at, now, observe_days=7), "transition")

    def test_none_when_publish_time_missing(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertIsNone(classify_first_contact_category(None, now, observe_days=7))


class IngestStockItemsTests(unittest.TestCase):
    def test_classifies_into_historical_mature_and_transition_with_one_snapshot_each(self) -> None:
        now = datetime.now(timezone.utc)
        mature_ts = int((now - timedelta(days=30)).timestamp())
        transition_ts = int((now - timedelta(days=3)).timestamp())
        items = [
            {"aweme_id": "m1", "liked_count": 10, "create_time": mature_ts},
            {"aweme_id": "t1", "liked_count": 20, "create_time": transition_ts},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                inserted, updated = ingest_stock_items(
                    conn, account, items, hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None
                )
                self.assertEqual((inserted, updated), (2, 0))
                rows = {r["platform_item_id"]: r for r in conn.execute("SELECT * FROM competitor_videos").fetchall()}
                self.assertEqual(rows["m1"]["first_contact_category"], "historical_mature")
                self.assertEqual(rows["t1"]["first_contact_category"], "transition")
                checks = conn.execute("SELECT * FROM video_checks").fetchall()
                self.assertEqual(len(checks), 2)
                self.assertTrue(all(c["discovery_batch_index"] is None for c in checks))
            finally:
                conn.close()

    def test_reingesting_same_platform_item_id_updates_not_duplicates(self) -> None:
        # BR-COLLECT-003: video identity is platform_item_id scoped to the account --
        # re-discovering the same video must update it in place, never create a
        # second business object.
        now = datetime.now(timezone.utc)
        published_at = int((now - timedelta(days=30)).timestamp())
        item = {"aweme_id": "dup1", "title": "first", "liked_count": 10, "create_time": published_at}
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, [item], hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None)
                updated_item = dict(item, title="second")
                inserted, updated = ingest_stock_items(
                    conn, account, [updated_item], hit_cfg=HIT_CFG, run_id="run2", raw_archive_ref=None
                )
                self.assertEqual((inserted, updated), (0, 1))
                self.assertEqual(conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0], 1)
                row = conn.execute("SELECT * FROM competitor_videos").fetchone()
                self.assertEqual(row["title"], "second")
            finally:
                conn.close()

    def test_missing_publish_time_gets_no_category_and_no_snapshot(self) -> None:
        items = [{"aweme_id": "nodate", "liked_count": 5}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, items, hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None)
                row = conn.execute("SELECT * FROM competitor_videos").fetchone()
                self.assertIsNone(row["first_contact_category"])
                self.assertEqual(row["excluded_reason"], "missing_publish_time")
                self.assertEqual(conn.execute("SELECT count(*) FROM video_checks").fetchone()[0], 0)
            finally:
                conn.close()


class IngestDailyIncrementalItemsTests(unittest.TestCase):
    def test_new_discovery_is_formal_new_with_d0_regardless_of_publish_age(self) -> None:
        # BR-COLLECT-002/BR-HIT-001 section B: 'formal_new' means discovered via the
        # ongoing daily batch, not "recently published" -- even a video published 30
        # days ago that is only now first seen by this account's daily crawl is
        # formal_new, D0.
        old_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [{"aweme_id": "new1", "liked_count": 5, "create_time": old_ts}]
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = ingest_daily_incremental_items(
                    conn, account, items, hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None
                )
                self.assertEqual(result["inserted"], 1)
                row = conn.execute("SELECT * FROM competitor_videos WHERE platform_item_id='new1'").fetchone()
                self.assertEqual(row["first_contact_category"], "formal_new")
                check = conn.execute("SELECT * FROM video_checks WHERE video_id=?", (row["video_id"],)).fetchone()
                self.assertEqual(check["discovery_batch_index"], 0)
            finally:
                conn.close()

    def test_existing_formal_new_advances_to_next_d_point_and_completes_at_d7(self) -> None:
        # BR-COLLECT-004: each daily refresh of an already-known video records a new
        # video_checks snapshot (discovery-anchored D-point here, not calendar T+n).
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="formal_new",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=1), like_count=5,
                )
                _insert_check(conn, video_id, "run0", discovery_batch_index=0, like_count=5)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                item = {"aweme_id": "v1", "liked_count": 8}
                for run_idx in range(1, 8):
                    ingest_daily_incremental_items(
                        conn, account, [item], hit_cfg=HIT_CFG, run_id=f"run{run_idx}", raw_archive_ref=None
                    )
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertEqual(row["tracking_completed"], 1)
                max_index = conn.execute(
                    "SELECT MAX(discovery_batch_index) AS m FROM video_checks WHERE video_id=?", (video_id,)
                ).fetchone()["m"]
                self.assertEqual(max_index, 7)
            finally:
                conn.close()

    def test_transition_video_matures_at_day_7(self) -> None:
        # BR-BASELINE-002: a transition video's second "matured" snapshot, taken at
        # exactly day 7, is what feeds the mature history baseline -- not an ongoing
        # observation-window re-crawl.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="transition",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=8), like_count=5,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = ingest_daily_incremental_items(
                    conn, account, [{"aweme_id": "v1", "liked_count": 40}],
                    hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None,
                )
                self.assertEqual(result["transition_matured"], 1)
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertIsNotNone(row["mature_snapshot_taken_at"])
                self.assertEqual(row["like_count"], 40)
            finally:
                conn.close()

    def test_historical_mature_video_is_untouched_by_daily_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30), like_count=5,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_daily_incremental_items(
                    conn, account, [{"aweme_id": "v1", "liked_count": 999}],
                    hit_cfg=HIT_CFG, run_id="run1", raw_archive_ref=None,
                )
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertEqual(row["like_count"], 5)
                self.assertEqual(conn.execute("SELECT count(*) FROM video_checks").fetchone()[0], 0)
            finally:
                conn.close()

    def test_transition_video_day_since_publish_is_recorded_and_advances(self) -> None:
        # 2026-07-08: day_since_publish is a calendar-day label (deliberately
        # separate from discovery_batch_index, which is discovery-batch-anchored
        # and reserved for formal_new) -- it labels which day of a transition
        # video's own 0-7 lifecycle a given daily check happened on.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="transition",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=2),
                    like_count=5, comment_count=1,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_daily_incremental_items(
                    conn, account, [{"aweme_id": "v1", "liked_count": 50, "comment_count": 5}],
                    hit_cfg=HIT_CFG, run_id="run_day2", raw_archive_ref=None,
                )
                check = conn.execute(
                    "SELECT * FROM video_checks WHERE video_id=? ORDER BY checked_at DESC LIMIT 1", (video_id,)
                ).fetchone()
                self.assertEqual(check["day_since_publish"], 2)
                self.assertIsNone(check["discovery_batch_index"])
            finally:
                conn.close()

    def test_transition_video_refreshed_and_checked_every_day_before_maturing(self) -> None:
        # 2026-07-08 user decision: to avoid an age-gate's added complexity, ALL
        # videos participate in judgement every day -- a transition video's
        # numbers must be refreshed (and a video_checks row recorded) on every
        # daily incremental pass while still observing, not left frozen at its
        # first-contact snapshot until day 7 (matching formal_new's existing
        # daily cadence). mature_snapshot_taken_at must stay NULL until it
        # actually reaches day 7 -- only the refresh cadence changed.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="transition",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=3),
                    like_count=5, comment_count=1,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = ingest_daily_incremental_items(
                    conn, account, [{"aweme_id": "v1", "liked_count": 500, "comment_count": 120}],
                    hit_cfg=HIT_CFG, run_id="run_day3", raw_archive_ref=None,
                )
                self.assertEqual(result["transition_matured"], 0)
                self.assertEqual(result["checks_recorded"], 1)
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertIsNone(row["mature_snapshot_taken_at"])
                self.assertEqual(row["like_count"], 500)
                self.assertEqual(row["comment_count"], 120)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM video_checks WHERE video_id=?", (video_id,)).fetchone()[0], 1
                )
            finally:
                conn.close()


def _seed_mature_pool(
    conn: sqlite3.Connection, account_id: str, count: int, like_count: int = 100, days_ago: int = 30, prefix: str = "m"
) -> None:
    for idx in range(count):
        platform_item_id = f"{prefix}{days_ago}_{idx}"
        video_id = stable_video_id(account_id, platform_item_id)
        _insert_video(
            conn, video_id, account_id, platform_item_id, category="historical_mature",
            publish_time=datetime.now(timezone.utc) - timedelta(days=days_ago),
            like_count=like_count, comment_count=10, share_count=5, collect_count=5,
        )


class MatureHistoryBaselineTests(unittest.TestCase):
    def test_below_unified_min_samples_no_baseline_and_no_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=19)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["mature_history_sample_count"], 19)
                self.assertEqual(conn.execute("SELECT count(*) FROM baselines").fetchone()[0], 0)
            finally:
                conn.close()

    def test_at_unified_min_samples_baseline_is_computed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=20)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                baseline = conn.execute(
                    "SELECT * FROM baselines WHERE baseline_mode='mature_history' AND metric='like_count'"
                ).fetchone()
                self.assertIsNotNone(baseline)
                self.assertEqual(baseline["median_value"], 100.0)
            finally:
                conn.close()

    def test_excludes_videos_older_than_90_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=20, days_ago=30)
                _seed_mature_pool(conn, "acc1", count=5, days_ago=200, like_count=99999)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["mature_history_sample_count"], 20)
            finally:
                conn.close()

    def test_caps_at_mature_history_max_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=60)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["mature_history_sample_count"], 50)
            finally:
                conn.close()


class MatureHistoryBackfillTests(unittest.TestCase):
    # 2026-07-08 user decision: reinstates a narrower version of the master
    # document's retired legacy_supplement/backfill-outside-the-90-day-window
    # behavior (BR-BASELINE-003), scoped to accounts that are both
    # high-magnitude and have enough lifetime content -- the real case that
    # surfaced this was an account with 55 lifetime videos (six-figure like
    # counts) but only 17 within the last 90 days, which could never
    # otherwise clear unified_min_samples(20).
    def test_backfill_applies_for_high_magnitude_video_rich_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=15, like_count=100000, days_ago=30, prefix="recent")
                _seed_mature_pool(conn, "acc1", count=10, like_count=100000, days_ago=200, prefix="old")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertTrue(result["mature_history_used_backfill"])
                self.assertEqual(result["mature_history_sample_count"], 25)
                baseline = conn.execute(
                    "SELECT * FROM baselines WHERE baseline_mode='mature_history' AND metric='like_count'"
                ).fetchone()
                self.assertIsNotNone(baseline)
            finally:
                conn.close()

    def test_backfill_does_not_apply_for_small_magnitude_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=15, like_count=100, days_ago=30, prefix="recent")
                _seed_mature_pool(conn, "acc1", count=10, like_count=100, days_ago=200, prefix="old")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertFalse(result["mature_history_used_backfill"])
                self.assertEqual(result["mature_history_sample_count"], 15)
                self.assertEqual(conn.execute("SELECT count(*) FROM baselines").fetchone()[0], 0)
            finally:
                conn.close()

    def test_backfill_does_not_apply_when_lifetime_pool_also_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=15, like_count=100000, days_ago=30, prefix="recent")
                _seed_mature_pool(conn, "acc1", count=3, like_count=100000, days_ago=200, prefix="old")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertFalse(result["mature_history_used_backfill"])
                self.assertEqual(result["mature_history_sample_count"], 15)
            finally:
                conn.close()


def _seed_formal_d_predecessors(
    conn: sqlite3.Connection, account_id: str, count: int, *, discovery_delay_hours: float = 12.0, like_at_d3: int = 100
) -> None:
    for idx in range(count):
        video_id = stable_video_id(account_id, f"p{idx}")
        _insert_video(
            conn, video_id, account_id, f"p{idx}", category="formal_new",
            publish_time=datetime.now(timezone.utc) - timedelta(days=10),
            like_count=like_at_d3, tracking_completed=1, discovery_delay_hours=discovery_delay_hours,
        )
        for d in range(8):
            _insert_check(conn, video_id, f"seed{idx}:{d}", discovery_batch_index=d, like_count=like_at_d3, comment_count=10, share_count=5, collect_count=5)


class FormalDBaselineActivationTests(unittest.TestCase):
    def test_inactive_below_activation_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=19)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertFalse(result["formal_d_baseline_active"])
            finally:
                conn.close()

    def test_active_at_activation_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=20)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertTrue(result["formal_d_baseline_active"])
            finally:
                conn.close()

    def test_discovery_delay_over_36_hours_excluded_from_predecessor_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=20, discovery_delay_hours=48.0)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["formal_d_predecessor_count"], 0)
                self.assertFalse(result["formal_d_baseline_active"])
            finally:
                conn.close()


class TriggerChannelTests(unittest.TestCase):
    def test_single_metric_channel_fires_at_2x_and_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=20, like_at_d3=100)
                candidate_id = stable_video_id("acc1", "cand1")
                _insert_video(
                    conn, candidate_id, "acc1", "cand1", category="formal_new",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=3),
                    like_count=200, comment_count=1, tracking_completed=0, discovery_delay_hours=1.0,
                )
                _insert_check(conn, candidate_id, "run0", discovery_batch_index=3, like_count=200, comment_count=1, share_count=1, collect_count=1)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["promoted_count"], 1)
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (candidate_id,)).fetchone()
                self.assertIn("like_anomaly", hit["hit_channel"])
            finally:
                conn.close()

    def test_multi_indicator_channel_fires_at_1_6x_without_any_single_metric_clearing_2x(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=20, like_at_d3=100)
                candidate_id = stable_video_id("acc1", "cand1")
                # 1.7x on like and comment (clears multi 1.6x, none clears single 2.0x)
                _insert_video(
                    conn, candidate_id, "acc1", "cand1", category="formal_new",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=3),
                    like_count=170, comment_count=17, tracking_completed=0, discovery_delay_hours=1.0,
                )
                _insert_check(conn, candidate_id, "run0", discovery_batch_index=3, like_count=170, comment_count=17, share_count=5, collect_count=5)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["promoted_count"], 1)
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (candidate_id,)).fetchone()
                channels = hit["hit_channel"].split(",")
                self.assertTrue(any(c.startswith("multi_indicator:") for c in channels))
                # like_anomaly must not fire as its own standalone channel (it only
                # clears the lower multi_indicator bar) -- it's fine for its name to
                # appear INSIDE multi_indicator's own breakdown of which metrics
                # contributed.
                self.assertFalse(any(c.startswith("like_anomaly:") for c in channels))
            finally:
                conn.close()

    def test_comment_like_ratio_fires_independent_of_any_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                # No baseline seeded at all -- comment_like_ratio needs none.
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                    like_count=100, comment_count=25,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["promoted_count"], 1)
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (video_id,)).fetchone()
                self.assertEqual(hit["hit_channel"], "comment_like_ratio:0.250")
            finally:
                conn.close()

    def test_cold_start_d7_rough_creates_a_rough_confidence_hit(self) -> None:
        # BR-HIT-001 section C(2) / master doc chapter 20.2 "存量高信号": rough IS a
        # real hit (the document permits calling it "历史爆款"), just tagged a
        # different evidence caliber (judgment_confidence='rough') from formal_d_series.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                # like_count=1000 keeps median*single_threshold(3x)=3000 >= the 2000
                # absolute floor, so this account is NOT classified 'small' -- it
                # exercises the normal ratio+floor path, not the P90 fallback.
                _seed_mature_pool(conn, "acc1", count=20, like_count=1000)
                # No formal D predecessors at all -- formal_d_baseline_active must be False.
                candidate_id = stable_video_id("acc1", "cand1")
                _insert_video(
                    conn, candidate_id, "acc1", "cand1", category="formal_new",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=7),
                    like_count=3500, comment_count=1, tracking_completed=1, discovery_delay_hours=1.0,
                )
                _insert_check(conn, candidate_id, "run0", discovery_batch_index=7, like_count=3500, comment_count=1, share_count=1, collect_count=1)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertFalse(result["formal_d_baseline_active"])
                self.assertEqual(result["rough_hit_count"], 1)
                self.assertEqual(result["promoted_count"], 1)
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (candidate_id,)).fetchone()
                self.assertIsNotNone(hit)
                self.assertEqual(hit["judgment_confidence"], "rough")
            finally:
                conn.close()

    def test_historical_mature_video_gets_rough_confidence_hit_against_mature_history_baseline(self) -> None:
        # BR-HIT-001 section C(2) / master doc chapter 20.2 "存量高信号": a
        # historical_mature (or matured transition) video never gets a D-series, but
        # its own numbers vs. the mature_history baseline still produce a real
        # rough-confidence hit ("历史爆款" per the document) -- this is the ONLY
        # magnitude-based channel available at first registration (首采), since no
        # formal D baseline can exist yet without daily-tracked D0-D7 videos.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                # like_count=1000 keeps median*single_threshold(3x)=3000 >= the 2000
                # absolute floor, so this account is NOT classified 'small'.
                _seed_mature_pool(conn, "acc1", count=20, like_count=1000)
                standout_id = stable_video_id("acc1", "standout1")
                _insert_video(
                    conn, standout_id, "acc1", "standout1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                    like_count=3500, comment_count=1,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["rough_hit_count"], 1)
                self.assertEqual(result["promoted_count"], 1)
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (standout_id,)).fetchone()
                self.assertIsNotNone(hit)
                self.assertEqual(hit["judgment_confidence"], "rough")
                self.assertIn("like_anomaly", hit["hit_channel"])
            finally:
                conn.close()

    def test_small_account_uses_p90_instead_of_absolute_floor(self) -> None:
        # 2026-07-08 user decision: an account whose median*single_threshold can
        # never reach the absolute floor gets its own P90 as the bar instead --
        # a video below the fixed floor can still be a hit this way, labelled
        # p90_small_account so it is never confused with the normal path.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                # median=100 -> 100*3=300 < 2000 floor -> this account is 'small'.
                # 17 videos at like=100 (the bulk) + 3 clear standouts at like=5000
                # (top 15% of 20) -- P90 lands exactly on the standout group, so the
                # bulk is cleanly excluded and the standouts cleanly included.
                for idx in range(17):
                    video_id = stable_video_id("acc1", f"p{idx}")
                    _insert_video(
                        conn, video_id, "acc1", f"p{idx}", category="historical_mature",
                        publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                        like_count=100, comment_count=1,
                    )
                standout_ids = []
                for idx in range(3):
                    standout_id = stable_video_id("acc1", f"standout{idx}")
                    standout_ids.append(standout_id)
                    _insert_video(
                        conn, standout_id, "acc1", f"standout{idx}", category="historical_mature",
                        publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                        like_count=5000, comment_count=1,
                    )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["rough_hit_count"], 3)
                for standout_id in standout_ids:
                    hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (standout_id,)).fetchone()
                    self.assertIsNotNone(hit)
                    self.assertEqual(hit["judgment_confidence"], "rough")
                    self.assertTrue(hit["hit_channel"].startswith("p90_small_account:"))
                # The 100-like bulk must NOT have been promoted -- below this
                # account's own P90.
                self.assertIsNone(conn.execute("SELECT * FROM hits WHERE video_id=?", (stable_video_id("acc1", "p0"),)).fetchone())
            finally:
                conn.close()

    def test_absolute_like_floor_rejects_a_video_that_clears_multi_indicator_on_other_metrics(self) -> None:
        # 2026-07-08 user decision: the absolute like floor is a blanket gate on
        # the video's own like_count, regardless of WHICH metrics actually fired
        # the ratio -- a video can clear multi_indicator via comment+collect alone
        # (neither one is like_count) and still be rejected if its own like_count
        # is below the floor. Not small (median*3=3000 >= 2000), so this exercises
        # the normal path's floor gate, not the P90 fallback.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=20, like_count=1000)
                candidate_id = stable_video_id("acc1", "cand1")
                _insert_video(
                    conn, candidate_id, "acc1", "cand1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                    like_count=1500, comment_count=25, collect_count=12, share_count=5,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["rough_hit_count"], 0)
                self.assertIsNone(conn.execute("SELECT * FROM hits WHERE video_id=?", (candidate_id,)).fetchone())
            finally:
                conn.close()

    def test_video_outside_the_baseline_window_is_not_checked_against_it(self) -> None:
        # 2026-07-08 bug fix: mature_medians is computed ONLY from mature_pool
        # (trailing 90-day/50-cap window). A historical_mature video published
        # well outside that window is not a baseline member -- it must not be
        # checked against a baseline it does not belong to, even though the raw
        # SQL query for "all historical_mature/transition videos" would otherwise
        # include it.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=20, like_count=1000, days_ago=30)
                old_id = stable_video_id("acc1", "old1")
                _insert_video(
                    conn, old_id, "acc1", "old1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=200),
                    like_count=5000, comment_count=1,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["mature_history_sample_count"], 20)
                self.assertEqual(result["rough_hit_count"], 0)
                self.assertIsNone(conn.execute("SELECT * FROM hits WHERE video_id=?", (old_id,)).fetchone())
            finally:
                conn.close()


class RoughFormalCountingTests(unittest.TestCase):
    def test_video_firing_both_channels_counts_toward_both_rough_and_formal(self) -> None:
        # 2026-07-08 bug fix: rough_hit_count/formal_hit_count must be counted by
        # which channel(s) actually fired (baseline_mode), not by
        # judgment_confidence -- that field gets overwritten to "formal" whenever
        # comment_like_ratio ALSO fires on a video that already cleared the
        # mature_history channel, which was silently excluding such videos from
        # rough_hit_count even though hit_channel correctly recorded both pieces
        # of evidence.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=20, like_count=1000, days_ago=30)
                video_id = stable_video_id("acc1", "candidate")
                _insert_video(
                    conn, video_id, "acc1", "candidate", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                    like_count=5000, comment_count=1200, share_count=5, collect_count=5,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                result = judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertEqual(result["promoted_count"], 1)
                self.assertEqual(result["rough_hit_count"], 1)
                self.assertEqual(result["formal_hit_count"], 1)
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertEqual(row["judgment_confidence"], "formal")
                self.assertIn("like_anomaly", row["trigger_rules"])
                self.assertIn("comment_like_ratio", row["trigger_rules"])
            finally:
                conn.close()


class CandidatePermanenceTests(unittest.TestCase):
    # BR-HIT-005: candidate records are permanent once first triggered -- never
    # retracted, and the same source video must never produce a second hits row
    # across repeated judgement passes (BR-HIT-003 idempotency).
    def test_hit_is_kept_when_later_rejudge_would_no_longer_clear_the_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                video_id = stable_video_id("acc1", "v1")
                _insert_video(
                    conn, video_id, "acc1", "v1", category="historical_mature",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=30),
                    like_count=100, comment_count=25,
                )
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")
                self.assertIsNotNone(conn.execute("SELECT * FROM hits WHERE video_id=?", (video_id,)).fetchone())

                # Data changes so it would NOT clear comment_like_ratio anymore.
                conn.execute("UPDATE competitor_videos SET comment_count=0 WHERE video_id=?", (video_id,))
                judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run2")
                hit = conn.execute("SELECT * FROM hits WHERE video_id=?", (video_id,)).fetchone()
                self.assertIsNotNone(hit)
                self.assertEqual(conn.execute("SELECT count(*) FROM hits WHERE video_id=?", (video_id,)).fetchone()[0], 1)
                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (video_id,)).fetchone()
                self.assertIsNotNone(row["first_trigger_at"])
            finally:
                conn.close()

    def test_trigger_rules_accumulate_across_multiple_observation_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_formal_d_predecessors(conn, "acc1", count=20, like_at_d3=100)
                candidate_id = stable_video_id("acc1", "cand1")
                _insert_video(
                    conn, candidate_id, "acc1", "cand1", category="formal_new",
                    publish_time=datetime.now(timezone.utc) - timedelta(days=3),
                    like_count=250, comment_count=1, tracking_completed=0, discovery_delay_hours=1.0,
                )
                _insert_check(conn, candidate_id, "run0", discovery_batch_index=3, like_count=250, comment_count=1, share_count=1, collect_count=1)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run1")

                # Advance to D4 with an even bigger like_count -- a NEW rule firing at a
                # new observation point must be appended, not replace the D3 record.
                _insert_check(conn, candidate_id, "run1b", discovery_batch_index=4, like_count=400, comment_count=1, share_count=1, collect_count=1)
                judge_account(conn, account, hit_cfg=HIT_CFG, run_id="run2")

                row = conn.execute("SELECT * FROM competitor_videos WHERE video_id=?", (candidate_id,)).fetchone()
                trigger_rules = json.loads(row["trigger_rules"])
                self.assertTrue(any(entry.startswith("D3:") for entry in trigger_rules))
                self.assertTrue(any(entry.startswith("D4:") for entry in trigger_rules))
                self.assertEqual(conn.execute("SELECT count(*) FROM hits WHERE video_id=?", (candidate_id,)).fetchone()[0], 1)
            finally:
                conn.close()


class ExecutionContractTests(unittest.TestCase):
    def test_valid_contract_passes(self) -> None:
        validate_registration_execution_contract(DOMAIN, HIT_CFG)

    def test_rejects_wrong_unified_min_samples(self) -> None:
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(DOMAIN, dict(HIT_CFG, unified_min_samples=10))

    def test_rejects_wrong_discovery_delay_hours_max(self) -> None:
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(DOMAIN, dict(HIT_CFG, discovery_delay_hours_max=24))


class ResolveMaxNotesTests(unittest.TestCase):
    def test_uses_first_crawl_max_notes_plus_buffer(self) -> None:
        settings = {"hit_detection": dict(HIT_CFG, first_crawl_max_notes=50, first_crawl_fetch_buffer=5)}
        max_notes, source = resolve_max_notes(None, settings)
        self.assertEqual(max_notes, 55)
        self.assertIn("first_crawl_max_notes", source)

    def test_cli_override_wins(self) -> None:
        settings = {"hit_detection": dict(HIT_CFG, first_crawl_max_notes=50, first_crawl_fetch_buffer=5)}
        max_notes, source = resolve_max_notes(20, settings)
        self.assertEqual((max_notes, source), (20, "cli"))


class EntrypointSmokeTests(unittest.TestCase):
    def test_run_full_registration_wires_register_crawl_and_judge_together(self) -> None:
        now = datetime.now(timezone.utc)
        # like_count=1000 keeps median*single_threshold(3x)=3000 >= the 2000
        # absolute floor, so this account is NOT classified 'small' -- keeps this
        # smoke test isolated to exercising the comment_like_ratio channel only
        # (hit1's like_count matches the baseline exactly, so it does not also
        # fire the mature-history magnitude channel).
        items = [
            {"aweme_id": f"m{idx}", "liked_count": 1000, "comment_count": 100,
             "create_time": int((now - timedelta(days=30)).timestamp())}
            for idx in range(20)
        ] + [
            {"aweme_id": "hit1", "liked_count": 1000, "comment_count": 400,
             "create_time": int((now - timedelta(days=30)).timestamp())}
        ]

        def fake_run(args, **kwargs):
            raw_dir = Path(args[args.index("--save_data_path") + 1])
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items), encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "full_registration.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                with patch(
                    "scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run",
                    side_effect=fake_run,
                ):
                    report = run_full_registration(
                        conn, DOMAIN, domain_path=Path("config/domains/泛科普.yaml"),
                        hit_cfg=HIT_CFG, max_notes=21, max_notes_source="settings",
                        account_limit=None, timeout_seconds=60,
                    )
                self.addCleanup(shutil.rmtree, ROOT / "data" / "formal" / "competitor_registration" / report["run_id"], True)

                self.assertEqual(report["status"], "succeeded")
                self.assertEqual(report["registration"]["inserted_count"], 1)
                self.assertEqual(report["crawl"]["results"][0]["inserted_videos"], 21)
                self.assertEqual(report["judgement"]["total_promoted"], 1)
                self.assertEqual(report["summary"]["historical_mature_videos"], 21)
            finally:
                conn.close()

    def test_run_rejudge_only_rejects_contract_mismatch(self) -> None:
        bad_hit_cfg = dict(HIT_CFG, unified_min_samples=10)
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                with self.assertRaises(ValueError):
                    run_rejudge_only(conn, DOMAIN, hit_cfg=bad_hit_cfg)
            finally:
                conn.close()

    def test_run_daily_incremental_wires_crawl_ingest_and_judge_together(self) -> None:
        def fake_run(args, **kwargs):
            raw_dir = Path(args[args.index("--save_data_path") + 1])
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                json.dumps({"aweme_id": "new1", "liked_count": 5}), encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                with patch(
                    "scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run",
                    side_effect=fake_run,
                ):
                    report = run_daily_incremental(
                        conn, DOMAIN, hit_cfg=HIT_CFG, crawler_cfg={"daily_max_notes": 5},
                    )
                self.assertEqual(report["status"], "succeeded")
                self.assertEqual(report["mode"], "daily_incremental")
                self.assertEqual(report["summary"]["formal_new_videos"], 1)
            finally:
                conn.close()

    def test_run_daily_incremental_rejects_bad_daily_max_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                register_from_domain(conn, DOMAIN, source_config_ref="config/domains/x.yaml")
                with self.assertRaises(ValueError):
                    run_daily_incremental(conn, DOMAIN, hit_cfg=HIT_CFG, crawler_cfg={"daily_max_notes": 0})
            finally:
                conn.close()


class SummarizeTests(unittest.TestCase):
    def test_summarize_counts_by_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _seed_mature_pool(conn, "acc1", count=3)
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                summary = summarize(conn, DOMAIN_LABEL)
                self.assertEqual(summary["accounts"], 1)
                self.assertEqual(summary["historical_mature_videos"], 3)
                self.assertEqual(summary["videos"], 3)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
