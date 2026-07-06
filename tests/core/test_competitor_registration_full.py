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
    first_crawl_excluded_reason,
    ingest_stock_items,
    judge_domain,
    judge_account,
    mark_pinned_items,
    resolve_max_notes,
    run_full_registration,
    run_rejudge_only,
    select_baseline_sample,
    settled_sample_count,
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
        # BR-BASELINE-003: the 90-day window already meets the 30-sample target, so no
        # legacy_supplement backfill into older videos should happen.
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


if __name__ == "__main__":
    unittest.main()
