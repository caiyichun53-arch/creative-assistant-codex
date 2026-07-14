from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.core.business_data.daily_source_acquisition import (
    DailyDiscoverySourceAcquirer,
    collect_trendradar_hotspots,
)
from scripts.core.business_data.run_domain_search import run_daily_tag_searches, seed_active_tags_from_sources_yaml
from scripts.core.external_adapters import ExternalCommandResult
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class FakeExecutor:
    def __init__(self, results: list[ExternalCommandResult], calls: list[str] | None = None, label: str = "executor") -> None:
        self.results = list(results)
        self.calls = calls if calls is not None else []
        self.label = label

    def execute(self, command):  # type: ignore[no-untyped-def]
        self.calls.append(self.label)
        if not self.results:
            raise AssertionError("unexpected automatic retry")
        return self.results.pop(0)


class DailySourceAcquisitionTests(unittest.TestCase):
    NOW = datetime(2026, 7, 14, 8, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.core = Stage0ContentProductionCore.open(Path(self.tempdir.name) / "test.sqlite3", data_identity="test")

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    @staticmethod
    def _hotspot_config() -> dict:
        return {"provider": "trendradar", "live_enabled": True, "timeout_seconds": 10, "max_items": 100}

    def test_trendradar_saves_valid_items_and_counts_invalid_items_without_padding(self) -> None:
        executor = FakeExecutor([
            ExternalCommandResult(
                status="succeeded",
                payload={"items": [
                    {"id": "h1", "title": "养老服务新变化", "url": "https://example.test/h1", "channel": "news", "rank": 1},
                    {"id": "h2", "title": "消费规则讨论", "url": "https://example.test/h2", "channel": "social", "rank": 2},
                    {"id": "broken", "title": "缺少链接"},
                ]},
                raw_archive_ref="fixture://trendradar",
            )
        ])
        result = collect_trendradar_hotspots(
            self.core.conn, executor, discovery_run_id="discovery-1", config=self._hotspot_config(), now=self.NOW
        )
        self.assertEqual(result["status"], "completed_with_failures")
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["invalid_items"], 1)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM trendradar_hotspot_observation").fetchone()[0], 2)
        self.assertEqual(executor.calls, ["executor"])

    def test_timeout_is_recorded_once_and_never_retried(self) -> None:
        executor = FakeExecutor([ExternalCommandResult(status="failed_timeout", payload={})])
        result = collect_trendradar_hotspots(
            self.core.conn, executor, discovery_run_id="discovery-timeout", config=self._hotspot_config(), now=self.NOW
        )
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(executor.calls, ["executor"])
        row = self.core.conn.execute("SELECT status, failure_reason FROM trendradar_collection_run").fetchone()
        self.assertEqual(row["status"], "timed_out")
        self.assertIn("automatic retry is forbidden", row["failure_reason"])

    def test_disabled_live_gate_blocks_before_executor_call(self) -> None:
        executor = FakeExecutor([])
        with self.assertRaisesRegex(ValueError, "live_enabled"):
            collect_trendradar_hotspots(
                self.core.conn,
                executor,
                discovery_run_id="discovery-blocked",
                config={"provider": "trendradar", "live_enabled": False},
                now=self.NOW,
            )
        self.assertEqual(executor.calls, [])

    def test_daily_acquirer_calls_trendradar_before_each_tag_search_and_uses_available_tags_only(self) -> None:
        seed_active_tags_from_sources_yaml(
            self.core.conn,
            {"domain_label": "fan_kepu_social_life", "active_tags": ["养老", "消费"]},
        )
        calls: list[str] = []
        trendradar = FakeExecutor([
            ExternalCommandResult(status="succeeded", payload={"items": []}, raw_archive_ref="fixture://empty")
        ], calls, "trendradar")
        mediacrawler = FakeExecutor([
            ExternalCommandResult(status="succeeded", payload={"items": []}),
            ExternalCommandResult(status="succeeded", payload={"items": []}),
        ], calls, "tag")
        acquirer = DailyDiscoverySourceAcquirer(
            conn=self.core.conn,
            trendradar_executor=trendradar,
            mediacrawler_executor=mediacrawler,
            hotspot_config=self._hotspot_config(),
            domain_search_config={"live_enabled": True},
        )
        result = acquirer.acquire_daily_sources(
            discovery_run_id="daily-1", domains=("fan_kepu_social_life",), now=self.NOW
        )
        self.assertEqual(result["execution_order"], ["trendradar_hotspot", "tag_search"])
        self.assertEqual(calls, ["trendradar", "tag", "tag"])
        self.assertEqual(result["tag_search"]["fan_kepu_social_life"]["selected_tag_count"], 2)

    def test_expired_batch_deadline_sends_no_tag_request_and_does_not_move_the_cursor(self) -> None:
        seed_active_tags_from_sources_yaml(
            self.core.conn,
            {"domain_label": "fan_kepu_social_life", "active_tags": ["养老"]},
        )
        executor = FakeExecutor([])
        result = run_daily_tag_searches(
            self.core.conn,
            executor,
            domain_label="fan_kepu_social_life",
            run_id="expired-run",
            domain_search_cfg={"live_enabled": True},
            deadline_monotonic=time.monotonic() - 1,
        )
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(executor.calls, [])
        self.assertIsNone(self.core.conn.execute("SELECT last_searched_at FROM domain_search_cursor").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
