from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock
from unittest.mock import patch

from scripts.core.business_data.register_competitor_accounts import install_schema, register_from_domain
from scripts.core.business_data.run_competitor_registration_full import (
    ROOT,
    first_crawl_excluded_reason,
    ingest_daily_incremental_items,
    ingest_stock_items,
    judge_domain,
    judge_account,
    mark_pinned_items,
    resolve_max_notes,
    run_daily_incremental,
    run_full_registration,
    run_rejudge_only,
    select_baseline_sample,
    settled_sample_count,
    stable_video_id,
    summarize,
)
from scripts.core.external_adapters import ExternalAdapterCommand
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor


class FullCompetitorRegistrationTests(unittest.TestCase):
    def test_first_crawl_items_are_archived_and_judged_without_comments(self) -> None:
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {
                "aweme_id": f"v{idx}",
                "title": f"baseline-{idx}",
                "aweme_url": f"https://www.douyin.com/video/v{idx}",
                "liked_count": 10,
                "create_time": published_at,
            }
            for idx in range(1, 30)
        ]
        items.append(
            {
                "aweme_id": "v30",
                "title": "high",
                "aweme_url": "https://www.douyin.com/video/v30",
                "liked_count": 1000,
                "comment_count": 10,
                "share_count": 30,
                "create_time": published_at,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "registration.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                inserted, updated = ingest_stock_items(
                    conn, account, items,
                    hit_cfg={"observe_days": 7, "baseline_window_days": 90},
                    run_id="run-1", raw_archive_ref="raw://fixture",
                )
                judgement = judge_domain(
                    conn,
                    "fan_kepu_social_life",
                    hit_cfg={
                        "baseline_window_days": 90,
                        "baseline_min_samples": 30,
                        "excess_threshold": 3.0,
                        "hit_floor_absolute_like_count": 2000,
                        "comment_like_ratio_threshold": 0.2,
                    },
                    run_id="run-1",
                )
                summary = summarize(conn, "fan_kepu_social_life")

                self.assertEqual((inserted, updated), (30, 0))
                self.assertEqual(summary["watching_videos"], 0)
                self.assertEqual(summary["videos"], 30)
                self.assertEqual(judgement["total_promoted"], 1)
                self.assertEqual(judgement["accounts"][0]["evidence_status"], "sufficient")
                self.assertEqual(summary["hits"], 1)
                comments_table = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='hit_comments'"
                ).fetchone()[0]
                self.assertEqual(comments_table, 0)
            finally:
                conn.close()

    def test_registration_defaults_use_configured_limit_not_unbounded_max(self) -> None:
        value, source = resolve_max_notes(
            None,
            {"hit_detection": {"baseline_min_samples": 30}, "crawler": {"daily_max_notes": 20}},
        )

        # No first_crawl_fetch_buffer configured -> falls back to the bare target.
        self.assertEqual(value, 30)
        self.assertEqual(source, "settings.hit_detection.baseline_min_samples+first_crawl_fetch_buffer")

    def test_first_crawl_fetch_buffer_adds_to_the_target_not_replaces_it(self) -> None:
        # Pinned/young videos are permanently ineligible, so fetching exactly the
        # 30-sample target structurally falls short after exclusions -- the buffer
        # exists so first crawl alone can actually reach 30 eligible videos.
        value, source = resolve_max_notes(
            None,
            {"hit_detection": {"baseline_min_samples": 30, "first_crawl_fetch_buffer": 15}},
        )

        self.assertEqual(value, 45)
        self.assertEqual(source, "settings.hit_detection.baseline_min_samples+first_crawl_fetch_buffer")

    def test_explicit_cli_max_notes_overrides_the_buffered_default(self) -> None:
        value, source = resolve_max_notes(
            50,
            {"hit_detection": {"baseline_min_samples": 30, "first_crawl_fetch_buffer": 15}},
        )

        self.assertEqual(value, 50)
        self.assertEqual(source, "cli")

    def test_local_mediacrawler_executor_defaults_to_headless(self) -> None:
        executor = LocalMediaCrawlerExecutor()
        command = ExternalAdapterCommand(
            adapter_id="collector.mediacrawler",
            capability="platform.video_snapshot",
            executable="vendor/MediaCrawler/main.py",
            args=("douyin", "creator"),
            input_payload={
                "platform": "douyin",
                "source_url": "https://www.douyin.com/user/MS4wLjABAAAAabc",
                "source_kind": "creator",
            },
            max_items=30,
        )

        args = executor._build_args(command, Path("raw"))  # noqa: SLF001 - command construction is the behavior under test.

        self.assertIn("--headless", args)
        self.assertEqual(args[args.index("--headless") + 1], "yes")
        self.assertEqual(args[args.index("--crawler_max_notes_count") + 1], "30")

    def test_baseline_sample_uses_recent_window_without_old_supplement(self) -> None:
        # BR-BASELINE-002: videos that have left the observation window (published more
        # than observe_days ago) become baseline material -- this is what select_baseline_sample
        # is filtering for. BR-BASELINE-003: the 90-day window already meets the 30-sample
        # target here, so no legacy_supplement backfill into older videos should happen.
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "sample.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("CREATE TABLE sample(video_id TEXT, publish_time TEXT, like_count INTEGER)")
                now = datetime.now(timezone.utc)
                for idx in range(35):
                    conn.execute(
                        "INSERT INTO sample VALUES(?, ?, ?)",
                        (f"recent-{idx}", (now - timedelta(days=30)).isoformat(), 10),
                    )
                for idx in range(20):
                    conn.execute(
                        "INSERT INTO sample VALUES(?, ?, ?)",
                        (f"old-{idx}", (now - timedelta(days=120 + idx)).isoformat(), 10),
                    )
                rows = conn.execute("SELECT * FROM sample ORDER BY publish_time DESC").fetchall()
                sample, window_days = select_baseline_sample(
                    rows,
                    {
                        "observe_days": 7,
                        "baseline_window_days": 90,
                        "baseline_min_samples": 30,
                    },
                )

                self.assertEqual(len(sample), 30)
                self.assertTrue(all(row["video_id"].startswith("recent-") for row in sample))
                self.assertEqual(window_days, 90)
            finally:
                conn.close()

    def test_baseline_sample_expands_window_only_when_recent_sample_below_target(self) -> None:
        # BR-BASELINE-002: videos past the observation window are baseline material even
        # when the in-window sample falls short and the legacy_supplement backfill has to
        # reach further back for them.
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "sample.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("CREATE TABLE sample(video_id TEXT, publish_time TEXT, like_count INTEGER)")
                now = datetime.now(timezone.utc)
                for idx in range(9):
                    conn.execute(
                        "INSERT INTO sample VALUES(?, ?, ?)",
                        (f"recent-{idx}", (now - timedelta(days=30)).isoformat(), 10),
                    )
                for idx in range(20):
                    conn.execute(
                        "INSERT INTO sample VALUES(?, ?, ?)",
                        (f"old-{idx}", (now - timedelta(days=120 + idx)).isoformat(), 10),
                    )
                rows = conn.execute("SELECT * FROM sample ORDER BY publish_time DESC").fetchall()
                sample, window_days = select_baseline_sample(
                    rows,
                    {
                        "observe_days": 7,
                        "baseline_window_days": 90,
                        "baseline_min_samples": 30,
                    },
                )

                self.assertEqual(len(sample), 29)
                self.assertEqual(window_days, 0)
            finally:
                conn.close()

    def test_hit_floor_caps_p90_instead_of_p90_setting_an_unbounded_bar(self) -> None:
        # BR-HIT-001 (2026-07-06 revision): threshold = max(median * excess_threshold,
        # min(P90, hit_floor_absolute_like_count)). This account's own P90 (3000) is
        # above the configured floor (2000) and well above median*3 (300) -- without
        # capping, P90 alone would set the bar at 3000; with capping, the floor (2000,
        # the smaller of the two) is what actually binds.
        # BR-HIT-002: the fixture below deliberately mixes a "low" tier (20 videos), a
        # "mid" tier (9 videos), and one true outlier -- normal/low-performer contrast
        # cases must survive alongside the hit so the threshold math is proven to
        # discriminate, not just proven to promote something.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = (
            [{"aweme_id": f"low-{idx}", "liked_count": 100, "create_time": published_at} for idx in range(20)]
            + [{"aweme_id": f"mid-{idx}", "liked_count": 3000, "create_time": published_at} for idx in range(9)]
            + [{"aweme_id": "outlier", "liked_count": 500000, "create_time": published_at}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "floor_cap.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["median"], 100.0)
                self.assertEqual(result["p90"], 3000.0)
                self.assertEqual(result["threshold"], 2000.0)
                self.assertEqual(result["promoted_count"], 10)  # 9 mid + 1 outlier, all >= 2000
            finally:
                conn.close()

    def test_judgement_runs_below_target_sample_and_flags_insufficient_evidence(self) -> None:
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"low-{idx}", "liked_count": 10, "create_time": published_at}
            for idx in range(9)
        ] + [
            {"aweme_id": "high", "liked_count": 1000, "comment_count": 10, "share_count": 30, "create_time": published_at}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "judgement.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/娉涚鏅?yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(
                    conn, account, items,
                    hit_cfg={"observe_days": 7, "baseline_window_days": 90},
                    run_id="run-1", raw_archive_ref="raw://fixture",
                )
                result = judge_account(
                    conn,
                    account,
                    hit_cfg={
                        "observe_days": 7,
                        "baseline_window_days": 90,
                        "baseline_min_samples": 30,
                        "excess_threshold": 3.0,
                        "hit_floor_absolute_like_count": 2000,
                        "comment_like_ratio_threshold": 0.2,
                    },
                    run_id="run-1",
                )

                self.assertEqual(result["status"], "judged")
                self.assertEqual(result["sample_count"], 10)
                self.assertEqual(result["promoted_count"], 1)
                self.assertEqual(result["evidence_status"], "insufficient_sample")
            finally:
                conn.close()

    def test_rejudgement_retracts_hit_that_no_longer_clears_recomputed_threshold(self) -> None:
        # BR-HIT-003: re-judging the same video across two runs (via the ON CONFLICT
        # upsert in the hits insert) must stay idempotent -- update the one logical hit
        # record, never duplicate it -- whether the outcome is promote or retract.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "retraction.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                # Round 1: sparse sample, the one clear outlier gets promoted.
                round_1_items = [
                    {"aweme_id": f"low-{idx}", "liked_count": 10, "create_time": published_at}
                    for idx in range(9)
                ] + [{"aweme_id": "outlier", "liked_count": 1000, "create_time": published_at}]
                ingest_stock_items(conn, account, round_1_items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")
                first_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")
                self.assertEqual(first_result["promoted_count"], 1)
                self.assertEqual(
                    conn.execute("SELECT status FROM competitor_videos WHERE platform_item_id='outlier'").fetchone()[0],
                    "promoted",
                )

                # Round 2: many more mid-range videos raise the median well past the
                # old outlier's like_count -- it should no longer qualify as a hit.
                round_2_items = [
                    {"aweme_id": f"mid-{idx}", "liked_count": 800, "create_time": published_at}
                    for idx in range(20)
                ]
                ingest_stock_items(conn, account, round_2_items, hit_cfg=hit_cfg, run_id="run-2", raw_archive_ref="raw://fixture")
                second_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-2")

                self.assertEqual(second_result["promoted_count"], 0)
                self.assertEqual(second_result["retracted_count"], 1)
                self.assertEqual(
                    conn.execute("SELECT status FROM competitor_videos WHERE platform_item_id='outlier'").fetchone()[0],
                    "archived",
                )
                self.assertIsNone(
                    conn.execute("SELECT hit_id FROM hits WHERE platform_item_id='outlier'").fetchone()
                )
            finally:
                conn.close()

    def test_comment_like_ratio_channel_promotes_video_below_like_threshold(self) -> None:
        # BR-HIT-001 (2026-07-07 revision): a video with deep comment engagement is a
        # hit even when its own like_count never clears the account's scale threshold.
        # median=100 -> like_threshold = max(100*3, min(p90, 2000)) = 300, well above
        # the 150-like "deep_comment" video below -- only the ratio channel (45/150=0.3
        # >= 0.2) should promote it.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"low-{idx}", "liked_count": 100, "comment_count": 5, "create_time": published_at}
            for idx in range(9)
        ] + [
            {
                "aweme_id": "deep_comment",
                "liked_count": 150,
                "comment_count": 45,
                "create_time": published_at,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ratio_channel.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["threshold"], 300.0)
                self.assertEqual(result["promoted_count"], 1)
                self.assertEqual(result["promoted_via_ratio_channel"], 1)
                hit_row = conn.execute(
                    "SELECT hit_channel FROM hits WHERE platform_item_id='deep_comment'"
                ).fetchone()
                self.assertEqual(hit_row["hit_channel"], "comment_like_ratio")
            finally:
                conn.close()

    def test_hit_channel_recorded_as_both_when_like_and_ratio_channels_both_clear(self) -> None:
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"low-{idx}", "liked_count": 100, "comment_count": 5, "create_time": published_at}
            for idx in range(9)
        ] + [
            # Clears the like channel (100*3=300) AND the ratio channel (200/500=0.4 >= 0.2).
            {"aweme_id": "double_hit", "liked_count": 500, "comment_count": 200, "create_time": published_at}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "both_channel.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["promoted_count"], 1)
                hit_row = conn.execute(
                    "SELECT hit_channel FROM hits WHERE platform_item_id='double_hit'"
                ).fetchone()
                self.assertEqual(hit_row["hit_channel"], "both")
            finally:
                conn.close()

    def test_video_failing_both_channels_is_retracted_not_kept_as_a_stale_hit(self) -> None:
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "retract_ratio.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                # Round 1: promoted purely through the ratio channel.
                round_1_items = [
                    {"aweme_id": f"low-{idx}", "liked_count": 100, "comment_count": 5, "create_time": published_at}
                    for idx in range(9)
                ] + [
                    {"aweme_id": "was_deep", "liked_count": 150, "comment_count": 45, "create_time": published_at}
                ]
                ingest_stock_items(conn, account, round_1_items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")
                first_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")
                self.assertEqual(first_result["promoted_count"], 1)

                # Round 2: re-crawled with a lower comment_count -- ratio drops below
                # the threshold and like_count still never clears the like channel.
                conn.execute(
                    "UPDATE competitor_videos SET comment_count=1 WHERE platform_item_id='was_deep'"
                )
                second_result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-2")

                self.assertEqual(second_result["promoted_count"], 0)
                self.assertEqual(second_result["retracted_count"], 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM competitor_videos WHERE platform_item_id='was_deep'"
                    ).fetchone()[0],
                    "archived",
                )
            finally:
                conn.close()

    def test_first_four_items_mark_old_pinned_items_when_no_explicit_flag(self) -> None:
        now = datetime.now(timezone.utc)
        items = [
            {"aweme_id": "old-pinned-1", "create_time": int((now - timedelta(days=120)).timestamp())},
            {"aweme_id": "old-pinned-2", "create_time": int((now - timedelta(days=100)).timestamp())},
            {"aweme_id": "normal-newest", "create_time": int((now - timedelta(days=10)).timestamp())},
            {"aweme_id": "normal-anchor", "create_time": int((now - timedelta(days=11)).timestamp())},
        ]

        marked = mark_pinned_items(items)

        self.assertEqual([item["_is_pinned"] for item in marked], [True, True, False, False])
        self.assertEqual(
            first_crawl_excluded_reason(marked[0], {"observe_days": 7, "baseline_window_days": 90}), "pinned"
        )

    def test_settled_sample_count_excludes_young_first_crawl_items(self) -> None:
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        young_time = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        settled_time = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"young-{idx}", "liked_count": 10, "create_time": young_time}
            for idx in range(5)
        ] + [
            {"aweme_id": f"settled-{idx}", "liked_count": 10, "create_time": settled_time}
            for idx in range(30)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "settled.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(
                    conn, account, items,
                    hit_cfg={"observe_days": 7, "baseline_window_days": 90},
                    run_id="run-1", raw_archive_ref="raw://fixture",
                )

                count = settled_sample_count(
                    conn,
                    account["account_id"],
                    {"observe_days": 7, "baseline_window_days": 90, "baseline_min_samples": 30},
                )

                self.assertEqual(count, 30)
            finally:
                conn.close()

    def test_younger_than_7_days_flag_is_not_a_permanent_exclusion(self) -> None:
        # BR-BASELINE-002 / BUILD_PLAN.md 阶段1 二次修正 (2026-06-13): "其计数照样每日
        # 刷新+每轮重判,不吃亏" -- a video flagged younger_than_7_days at ingest time must
        # become eligible for judgement once it has genuinely aged past observe_days,
        # exactly like any other archived video. This row is inserted directly with an
        # aged publish_time to simulate "flagged young back then, settled by now" without
        # needing to travel through real time.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        settled_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "unstick_young_flag.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                # 9 ordinary settled videos to give the baseline a real median, plus one
                # row that carries the stale younger_than_7_days flag but is, by its
                # publish_time, thoroughly settled now.
                for idx in range(9):
                    video_id = stable_video_id(account["account_id"], f"ordinary-{idx}")
                    conn.execute(
                        """
                        INSERT INTO competitor_videos(
                            video_id, account_id, platform, platform_item_id, publish_time,
                            like_count, comment_count, excluded_reason, status,
                            registration_run_id, raw_archive_ref, raw_json
                        ) VALUES (?, ?, 'douyin', ?, ?, 100, 5, NULL, 'archived', 'run-0', 'raw://fixture', '{}')
                        """,
                        (video_id, account["account_id"], f"ordinary-{idx}", settled_time),
                    )
                stale_flag_video_id = stable_video_id(account["account_id"], "stale-young-flag")
                conn.execute(
                    """
                    INSERT INTO competitor_videos(
                        video_id, account_id, platform, platform_item_id, publish_time,
                        like_count, comment_count, excluded_reason, status,
                        registration_run_id, raw_archive_ref, raw_json
                    ) VALUES (?, ?, 'douyin', ?, ?, 5000, 10, 'younger_than_7_days', 'archived', 'run-0', 'raw://fixture', '{}')
                    """,
                    (stale_flag_video_id, account["account_id"], "stale-young-flag", settled_time),
                )
                conn.commit()

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["sample_count"], 10)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM competitor_videos WHERE platform_item_id='stale-young-flag'"
                    ).fetchone()["status"],
                    "promoted",
                )
            finally:
                conn.close()

    def test_watching_video_within_observe_window_is_promoted_early_when_it_clears_the_bar(self) -> None:
        # 2026-07-07 user decision: the whole point of capturing a video's own 0-7 day
        # video_checks curve is to actually USE it, not just store it. A video still
        # inside the observation window that already clears the account's existing
        # hit threshold (computed from settled samples) must be promoted immediately --
        # not held back until day 7. This reverses an earlier, incorrect "no early
        # promotion" design point that had been pulled from BUILD_PLAN.md (the retired
        # legacy system's build log) without independent confirmation; there is no
        # separate growth-curve/steepness *model* here, just the same already-validated
        # hit criteria (like_threshold / comment_like_ratio_threshold) applied to a
        # still-watching video's current numbers instead of only at exit.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        settled_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        young_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "no_early_judgement.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                for idx in range(9):
                    video_id = stable_video_id(account["account_id"], f"ordinary-{idx}")
                    conn.execute(
                        """
                        INSERT INTO competitor_videos(
                            video_id, account_id, platform, platform_item_id, publish_time,
                            like_count, comment_count, excluded_reason, status,
                            registration_run_id, raw_archive_ref, raw_json
                        ) VALUES (?, ?, 'douyin', ?, ?, 100, 5, NULL, 'archived', 'run-0', 'raw://fixture', '{}')
                        """,
                        (video_id, account["account_id"], f"ordinary-{idx}", settled_time),
                    )
                # Only 2 days old, but clears the like threshold (median*3=300) many
                # times over -- must be promoted right now, not held until day 7.
                early_video_id = stable_video_id(account["account_id"], "early-riser")
                conn.execute(
                    """
                    INSERT INTO competitor_videos(
                        video_id, account_id, platform, platform_item_id, publish_time,
                        like_count, comment_count, excluded_reason, status,
                        registration_run_id, raw_archive_ref, raw_json
                    ) VALUES (?, ?, 'douyin', ?, ?, 50000, 100, NULL, 'watching', 'run-0', 'raw://fixture', '{}')
                    """,
                    (early_video_id, account["account_id"], "early-riser", young_time),
                )
                conn.commit()

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["sample_count"], 9)  # baseline itself still only uses settled samples
                self.assertEqual(result["promoted_early_count"], 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM competitor_videos WHERE platform_item_id='early-riser'"
                    ).fetchone()["status"],
                    "promoted",
                )
                self.assertIsNotNone(
                    conn.execute("SELECT hit_id FROM hits WHERE platform_item_id='early-riser'").fetchone()
                )
            finally:
                conn.close()

    def test_watching_video_within_observe_window_stays_watching_when_it_does_not_clear_the_bar(self) -> None:
        # The counterpart: a still-young video that has NOT cleared either channel yet
        # must stay 'watching' -- early promotion only fires when the bar is actually
        # cleared, it does not shorten the observation window itself.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        settled_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        young_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "no_premature_promotion.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                for idx in range(9):
                    video_id = stable_video_id(account["account_id"], f"ordinary-{idx}")
                    conn.execute(
                        """
                        INSERT INTO competitor_videos(
                            video_id, account_id, platform, platform_item_id, publish_time,
                            like_count, comment_count, excluded_reason, status,
                            registration_run_id, raw_archive_ref, raw_json
                        ) VALUES (?, ?, 'douyin', ?, ?, 100, 5, NULL, 'archived', 'run-0', 'raw://fixture', '{}')
                        """,
                        (video_id, account["account_id"], f"ordinary-{idx}", settled_time),
                    )
                slow_video_id = stable_video_id(account["account_id"], "slow-starter")
                conn.execute(
                    """
                    INSERT INTO competitor_videos(
                        video_id, account_id, platform, platform_item_id, publish_time,
                        like_count, comment_count, excluded_reason, status,
                        registration_run_id, raw_archive_ref, raw_json
                    ) VALUES (?, ?, 'douyin', ?, ?, 50, 2, NULL, 'watching', 'run-0', 'raw://fixture', '{}')
                    """,
                    (slow_video_id, account["account_id"], "slow-starter", young_time),
                )
                conn.commit()

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["promoted_early_count"], 0)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM competitor_videos WHERE platform_item_id='slow-starter'"
                    ).fetchone()["status"],
                    "watching",
                )
            finally:
                conn.close()

    def test_watching_video_graduates_to_archived_when_it_never_cleared_the_bar(self) -> None:
        # BUILD_PLAN.md 阶段2: "watching 到期(>7天)-> archived 转基线材料". A video that
        # aged out of observation without ever clearing the hit threshold settles as
        # ordinary archived material -- this is its first judgement (graduation), not a
        # retraction, so it must not touch the hits table.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        settled_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "graduation.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                for idx in range(9):
                    video_id = stable_video_id(account["account_id"], f"ordinary-{idx}")
                    conn.execute(
                        """
                        INSERT INTO competitor_videos(
                            video_id, account_id, platform, platform_item_id, publish_time,
                            like_count, comment_count, excluded_reason, status,
                            registration_run_id, raw_archive_ref, raw_json
                        ) VALUES (?, ?, 'douyin', ?, ?, 100, 5, NULL, 'archived', 'run-0', 'raw://fixture', '{}')
                        """,
                        (video_id, account["account_id"], f"ordinary-{idx}", settled_time),
                    )
                # Aged past the 7-day window, still 'watching', and never cleared either
                # channel -- must graduate to 'archived', not stay stuck in 'watching'.
                graduate_video_id = stable_video_id(account["account_id"], "never-hit")
                conn.execute(
                    """
                    INSERT INTO competitor_videos(
                        video_id, account_id, platform, platform_item_id, publish_time,
                        like_count, comment_count, excluded_reason, status,
                        registration_run_id, raw_archive_ref, raw_json
                    ) VALUES (?, ?, 'douyin', ?, ?, 50, 1, NULL, 'watching', 'run-0', 'raw://fixture', '{}')
                    """,
                    (graduate_video_id, account["account_id"], "never-hit", settled_time),
                )
                conn.commit()

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["promoted_count"], 0)
                self.assertEqual(result["graduated_count"], 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM competitor_videos WHERE platform_item_id='never-hit'"
                    ).fetchone()["status"],
                    "archived",
                )
                self.assertIsNone(
                    conn.execute("SELECT hit_id FROM hits WHERE platform_item_id='never-hit'").fetchone()
                )
            finally:
                conn.close()

    def test_rejudge_only_recomputes_without_registering_or_crawling(self) -> None:
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"v{idx}", "liked_count": 10, "create_time": published_at}
            for idx in range(1, 30)
        ] + [
            {
                "aweme_id": "v30",
                "liked_count": 1000,
                "comment_count": 10,
                "share_count": 30,
                "create_time": published_at,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "rejudge.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                # Simulate data that already went through a (buggy) first judgement pass.
                ingest_stock_items(
                    conn, account, items,
                    hit_cfg={"observe_days": 7, "baseline_window_days": 90},
                    run_id="run-1", raw_archive_ref="raw://fixture",
                )
                conn.execute("DELETE FROM baselines")
                conn.execute("DELETE FROM hits")
                conn.execute("UPDATE competitor_videos SET status='archived'")
                conn.commit()

                videos_before = conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0]
                accounts_before = conn.execute("SELECT count(*) FROM competitor_accounts").fetchone()[0]

                report = run_rejudge_only(conn, domain, hit_cfg=hit_cfg)

                self.assertEqual(report["status"], "succeeded")
                self.assertEqual(report["mode"], "rejudge_only")
                self.assertTrue(report["run_id"].startswith("competitor_registration_rejudge_"))
                self.assertIsNone(report["registration"])
                self.assertEqual(report["crawl"]["results"], [])
                self.assertEqual(report["judgement"]["total_promoted"], 1)

                videos_after = conn.execute("SELECT count(*) FROM competitor_videos").fetchone()[0]
                accounts_after = conn.execute("SELECT count(*) FROM competitor_accounts").fetchone()[0]
                self.assertEqual(videos_after, videos_before)
                self.assertEqual(accounts_after, accounts_before)

                baseline_run_ids = {
                    row["run_id"] for row in conn.execute("SELECT run_id FROM baselines").fetchall()
                }
                self.assertEqual(baseline_run_ids, {report["run_id"]})
            finally:
                conn.close()

    def test_run_full_registration_wires_register_crawl_and_judge_together(self) -> None:
        # Only the sub-steps (register/ingest/judge) were previously tested in isolation.
        # This exercises run_full_registration() itself -- the actual entrypoint that ran
        # against the real 28-account pilot -- end to end, with the crawl subprocess faked.
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"v{idx}", "liked_count": 10, "create_time": published_at}
            for idx in range(1, 30)
        ] + [
            {
                "aweme_id": "v30",
                "liked_count": 1000,
                "comment_count": 10,
                "share_count": 30,
                "create_time": published_at,
            }
        ]

        def fake_run(args, **kwargs):
            raw_dir = Path(args[args.index("--save_data_path") + 1])
            jsonl_dir = raw_dir / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items),
                encoding="utf-8",
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
                        conn,
                        domain,
                        domain_path=Path("config/domains/泛科普.yaml"),
                        hit_cfg=hit_cfg,
                        max_notes=30,
                        max_notes_source="settings.hit_detection.baseline_min_samples",
                        account_limit=None,
                        timeout_seconds=60,
                    )
                self.addCleanup(shutil.rmtree, ROOT / "data" / "formal" / "competitor_registration" / report["run_id"], True)

                self.assertEqual(report["status"], "succeeded")
                self.assertEqual(report["mode"], "full")
                self.assertTrue(report["run_id"].startswith("competitor_registration_full_"))
                self.assertEqual(report["registration"]["inserted_count"], 1)
                self.assertEqual(report["crawl"]["results"][0]["inserted_videos"], 30)
                self.assertEqual(report["judgement"]["total_promoted"], 1)
                self.assertEqual(report["summary"]["accounts"], 1)
                self.assertEqual(report["summary"]["archived_videos"], 29)
                self.assertEqual(report["summary"]["promoted_videos"], 1)
            finally:
                conn.close()

    def test_rejudge_only_still_rejects_contract_mismatch(self) -> None:
        domain = {
            "name": "泛科普-社会与生活",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "张见识", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        bad_hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 10,  # violates the fixed 30-sample target contract
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "rejudge_reject.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")

                with self.assertRaises(ValueError):
                    run_rejudge_only(conn, domain, hit_cfg=bad_hit_cfg)
            finally:
                conn.close()

    def test_reingesting_same_platform_item_id_updates_not_duplicates(self) -> None:
        # BR-COLLECT-003: video identity is platform_item_id scoped to the competitor --
        # re-discovering the same video must update its metrics in place, never create a
        # second business object for the same source video.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "dedupe.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                first_pass = [{"aweme_id": "dupe-check", "liked_count": 100, "create_time": published_at}]
                inserted, updated = ingest_stock_items(
                    conn, account, first_pass, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture"
                )
                self.assertEqual((inserted, updated), (1, 0))

                second_pass = [{"aweme_id": "dupe-check", "liked_count": 250, "create_time": published_at}]
                inserted, updated = ingest_stock_items(
                    conn, account, second_pass, hit_cfg=hit_cfg, run_id="run-2", raw_archive_ref="raw://fixture"
                )
                self.assertEqual((inserted, updated), (0, 1))

                rows = conn.execute(
                    "SELECT like_count FROM competitor_videos WHERE platform_item_id='dupe-check'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["like_count"], 250)
            finally:
                conn.close()

    def test_share_comment_ratio_is_derived_evidence_not_a_promotion_channel(self) -> None:
        # BR-HIT-004: share_comment_ratio is stored as derived evidence for ranking/
        # analysis only. A video with a huge share_comment_ratio but low like_count and
        # low comment_count (clearing neither the like channel nor the comment/like
        # ratio channel) must not be promoted by ratio alone.
        domain = {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        published_at = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
        items = [
            {"aweme_id": f"low-{idx}", "liked_count": 100, "comment_count": 5, "share_count": 2, "create_time": published_at}
            for idx in range(9)
        ] + [
            # share_comment_ratio = 500/1 = 500 (huge), but like_count=100 clears neither
            # the like channel (median*3=300) nor the ratio channel (1/100=0.01 < 0.2).
            {
                "aweme_id": "high_share_ratio_only",
                "liked_count": 100,
                "comment_count": 1,
                "share_count": 500,
                "create_time": published_at,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "share_ratio.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_stock_items(conn, account, items, hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture")

                result = judge_account(conn, account, hit_cfg=hit_cfg, run_id="run-1")

                self.assertEqual(result["promoted_count"], 0)
                self.assertIsNone(
                    conn.execute(
                        "SELECT hit_id FROM hits WHERE platform_item_id='high_share_ratio_only'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def _fixture_domain(self) -> dict[str, Any]:
        return {
            "name": "fixture-domain",
            "formal_domain_label": "fan_kepu_social_life",
            "platform": "douyin",
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            },
            "competitor_seeds": [
                {"name": "fixture-account", "url": "https://www.douyin.com/user/MS4wLjABAAAAabc"},
            ],
        }

    def test_ingest_daily_incremental_new_video_within_window_enters_watching(self) -> None:
        # BR-COLLECT-002: a genuinely new video, published within the observation
        # window, must enter 'watching' -- not 'archived' the way stock items do.
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily_new.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, self._fixture_domain(), source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                result = ingest_daily_incremental_items(
                    conn, account,
                    [{"aweme_id": "brand-new", "liked_count": 10, "comment_count": 1, "create_time": published_at}],
                    hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture",
                )

                self.assertEqual(result["inserted"], 1)
                row = conn.execute(
                    "SELECT status, excluded_reason FROM competitor_videos WHERE platform_item_id='brand-new'"
                ).fetchone()
                self.assertEqual(row["status"], "watching")
                self.assertIsNone(row["excluded_reason"])
            finally:
                conn.close()

    def test_ingest_daily_incremental_updates_existing_and_appends_video_check(self) -> None:
        # BR-COLLECT-004: a video already known (in 'watching') must have its metrics
        # updated in place AND get a new video_checks snapshot row appended -- not
        # overwritten/lost, and not treated as a fresh discovery.
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily_reconcile.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, self._fixture_domain(), source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_daily_incremental_items(
                    conn, account,
                    [{"aweme_id": "growing", "liked_count": 10, "comment_count": 1, "create_time": published_at}],
                    hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture",
                )

                result = ingest_daily_incremental_items(
                    conn, account,
                    [{"aweme_id": "growing", "liked_count": 40, "comment_count": 5, "create_time": published_at}],
                    hit_cfg=hit_cfg, run_id="run-2", raw_archive_ref="raw://fixture",
                )

                self.assertEqual(result["inserted"], 0)
                self.assertEqual(result["updated"], 1)
                row = conn.execute(
                    "SELECT like_count, check_count FROM competitor_videos WHERE platform_item_id='growing'"
                ).fetchone()
                self.assertEqual(row["like_count"], 40)
                self.assertEqual(row["check_count"], 1)
                checks = conn.execute(
                    "SELECT vc.like_count FROM video_checks vc JOIN competitor_videos cv ON cv.video_id=vc.video_id"
                    " WHERE cv.platform_item_id='growing' ORDER BY vc.checked_at"
                ).fetchall()
                self.assertEqual([c["like_count"] for c in checks], [10, 40])
            finally:
                conn.close()

    def test_ingest_daily_incremental_reconciles_existing_before_new_discoveries(self) -> None:
        # Agreed processing order: reconcile against the observation pool first, then
        # treat leftover items as newly discovered -- not the other way round.
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily_order.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, self._fixture_domain(), source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()
                ingest_daily_incremental_items(
                    conn, account,
                    [{"aweme_id": "already-known", "liked_count": 10, "create_time": published_at}],
                    hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture",
                )

                result = ingest_daily_incremental_items(
                    conn, account,
                    [
                        {"aweme_id": "already-known", "liked_count": 20, "create_time": published_at},
                        {"aweme_id": "just-discovered", "liked_count": 5, "create_time": published_at},
                    ],
                    hit_cfg=hit_cfg, run_id="run-2", raw_archive_ref="raw://fixture",
                )

                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["inserted"], 1)
            finally:
                conn.close()

    def test_ingest_daily_incremental_only_honors_explicit_pinned_flag(self) -> None:
        # No positional guessing for a small daily batch -- only an explicit pinned
        # flag from the platform routes a new discovery straight to archived+excluded.
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily_pinned.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, self._fixture_domain(), source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                ingest_daily_incremental_items(
                    conn, account,
                    [
                        {"aweme_id": "explicit-pin", "liked_count": 10, "is_top": True, "create_time": published_at},
                        {"aweme_id": "no-flag", "liked_count": 10, "create_time": published_at},
                    ],
                    hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture",
                )

                pinned_row = conn.execute(
                    "SELECT status, excluded_reason FROM competitor_videos WHERE platform_item_id='explicit-pin'"
                ).fetchone()
                self.assertEqual(pinned_row["status"], "archived")
                self.assertEqual(pinned_row["excluded_reason"], "pinned")

                unflagged_row = conn.execute(
                    "SELECT status, excluded_reason FROM competitor_videos WHERE platform_item_id='no-flag'"
                ).fetchone()
                self.assertEqual(unflagged_row["status"], "watching")
                self.assertIsNone(unflagged_row["excluded_reason"])
            finally:
                conn.close()

    def test_ingest_daily_incremental_late_discovery_older_than_window_goes_straight_to_archived(self) -> None:
        hit_cfg = {"observe_days": 7, "baseline_window_days": 90}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=20)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "daily_late.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, self._fixture_domain(), source_config_ref="config/domains/泛科普.yaml")
                account = conn.execute("SELECT * FROM competitor_accounts").fetchone()

                ingest_daily_incremental_items(
                    conn, account,
                    [{"aweme_id": "late-find", "liked_count": 10, "create_time": published_at}],
                    hit_cfg=hit_cfg, run_id="run-1", raw_archive_ref="raw://fixture",
                )

                row = conn.execute(
                    "SELECT status FROM competitor_videos WHERE platform_item_id='late-find'"
                ).fetchone()
                self.assertEqual(row["status"], "archived")
            finally:
                conn.close()

    def test_run_daily_incremental_wires_crawl_ingest_and_judge_together(self) -> None:
        domain = self._fixture_domain()
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        crawler_cfg = {"daily_max_notes": 20}
        published_at = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "run_daily.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")

                fake_executor = mock.Mock()
                fake_executor.execute.return_value = mock.Mock(
                    status="succeeded",
                    raw_archive_ref="raw://fixture",
                    payload={"items": [{"aweme_id": "daily-find", "liked_count": 10, "create_time": published_at}]},
                )

                report = run_daily_incremental(
                    conn, domain, hit_cfg=hit_cfg, crawler_cfg=crawler_cfg, executor=fake_executor,
                )

                self.assertEqual(report["status"], "succeeded")
                self.assertIn("judgement", report)
                row = conn.execute(
                    "SELECT status FROM competitor_videos WHERE platform_item_id='daily-find'"
                ).fetchone()
                self.assertEqual(row["status"], "watching")
            finally:
                conn.close()

    def test_run_daily_incremental_rejects_bad_daily_max_notes(self) -> None:
        domain = self._fixture_domain()
        hit_cfg = {
            "observe_days": 7,
            "baseline_window_days": 90,
            "baseline_min_samples": 30,
            "excess_threshold": 3.0,
            "hit_floor_absolute_like_count": 2000,
            "comment_like_ratio_threshold": 0.2,
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "run_daily_reject.sqlite3")
            conn.row_factory = sqlite3.Row
            try:
                install_schema(conn)
                register_from_domain(conn, domain, source_config_ref="config/domains/泛科普.yaml")

                with self.assertRaises(ValueError):
                    run_daily_incremental(
                        conn, domain, hit_cfg=hit_cfg, crawler_cfg={"daily_max_notes": 0},
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
