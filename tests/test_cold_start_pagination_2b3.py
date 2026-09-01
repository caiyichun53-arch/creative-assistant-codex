"""Isolation checks for the 2B-3 cold-start history paging contract."""

from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterError,
    ExternalAdapterRunResult,
)
from scripts.core.production.high_signal_policy import (
    MIN_RELIABLE_HISTORY_ITEMS,
    build_historical_collection_artifact,
    validate_historical_collection_artifact,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
)


class FakePageCore:
    def __init__(self) -> None:
        self.progress: dict[str, dict] = {}
        self.progress_history: list[dict] = []

    def get_competitor_historical_page_progress(self, *, registration_id: str):
        return self.progress.get(registration_id)

    def record_competitor_historical_page_progress(self, **payload):
        value = dict(payload)
        self.progress[str(payload["registration_id"])] = value
        self.progress_history.append(value)
        return value


class FakePagedCollector:
    def __init__(self) -> None:
        self.pages: dict[tuple[bool, str], dict] = {}
        self.archives: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, bool]] = []
        self.fail_once: set[tuple[bool, str]] = set()

    def add_page(
        self,
        *,
        cursor: str,
        cutoff: bool,
        items: list[dict],
        next_cursor: str,
        has_more: bool,
        reached_time_boundary: bool = False,
        stop_reason: str = "page_complete",
    ) -> None:
        archive = f"isolated://page/{len(self.archives) + 1}"
        self.archives[archive] = list(items)
        self.pages[(cutoff, cursor)] = {
            "items": list(items),
            "pagination": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "reached_time_boundary": reached_time_boundary,
                "stop_reason": stop_reason,
            },
            "archive": archive,
        }

    def collect_video_snapshot_page(self, *, platform, source_url, continuation_cursor, published_after):
        del platform, source_url
        key = (published_after is not None, str(continuation_cursor or ""))
        self.calls.append(key)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise ExternalAdapterError("isolated collector interruption")
        page = self.pages[key]
        return ExternalAdapterRunResult(
            adapter_id="collector.mediacrawler",
            capability="platform.video_snapshot",
            item_count=len(page["items"]),
            payload={"items": list(page["items"]), "pagination": dict(page["pagination"])},
            command_hash=f"command-{len(self.calls)}",
            output_hash=f"output-{len(self.calls)}",
            raw_archive_ref=page["archive"],
            external_side_effect=False,
        )

    def read_video_snapshot_archive(self, *, raw_archive_ref, platform):
        del platform
        return list(self.archives[raw_archive_ref])


def _item(source_id: str, age_days: int) -> dict:
    now = int(time.time())
    return {
        "source_id": source_id,
        "platform": "douyin",
        "url": f"https://example.invalid/{source_id}",
        "title": source_id,
        "author": "isolated-account",
        "published_at": now - age_days * 86400,
        "duration_seconds": 30,
        "metrics": {
            "like_count": 100,
            "comment_count": 10,
            "share_count": 3,
            "collect_count": 2,
        },
    }


def _registration() -> dict:
    return {
        "registration_id": "registration-2b3",
        "cold_start_id": "cold-start-2b3",
        "external_account_ref": "douyin:isolated-account",
    }


def _executor(collector: FakePagedCollector, core: FakePageCore | None = None):
    return ConfiguredCompetitorRegistrationExecutor(
        core=core or FakePageCore(),
        collector=collector,
        transcriber=object(),
        media_materializer=object(),
    )


class ColdStartPagination2B3Test(unittest.TestCase):
    def test_recent_window_keeps_reading_after_mature_target(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(10)]
            + [_item(f"recent-{i}", 3) for i in range(8)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(10, 20)],
            next_cursor="c2", has_more=True,
        )
        collector.add_page(
            cursor="c2", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(8, 25)],
            next_cursor="c3", has_more=True,
        )
        collector.add_page(
            cursor="c3", cutoff=True,
            items=[], next_cursor="c4", has_more=True,
            reached_time_boundary=True, stop_reason="time_boundary",
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(collector.calls, [(True, ""), (True, "c1"), (True, "c2"), (True, "c3")])
        self.assertEqual(artifact["item_count"], 45)
        self.assertEqual(artifact["collection_status"], "target_reached")
        self.assertEqual(artifact["page_request_count"], 4)

    def test_nineteen_plus_one_stops_on_second_page(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(19)],
            next_cursor="c1", has_more=True, reached_time_boundary=True,
        )
        collector.add_page(
            cursor="c1", cutoff=False,
            items=[_item("mature-19", 120)], next_cursor="c2", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(collector.calls, [(True, ""), (False, "c1")])
        self.assertEqual(artifact["recent_mature_item_count"], 19)
        self.assertTrue(artifact["used_older_history_backfill"])

    def test_multiple_pages_stop_at_mature_target(self) -> None:
        collector = FakePagedCollector()
        for cursor, next_cursor, start, count in (("", "c1", 0, 10), ("c1", "c2", 10, 5), ("c2", "c3", 15, 5)):
            collector.add_page(
                cursor=cursor, cutoff=True,
                items=[_item(f"mature-{i}", 10) for i in range(start, start + count)],
                next_cursor=next_cursor, has_more=True,
                reached_time_boundary=(cursor == "c2"),
            )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(len(collector.calls), 3)
        self.assertEqual(artifact["recent_mature_item_count"], 20)

    def test_collection_stops_when_second_page_reaches_fifty(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(20)]
            + [_item(f"recent-{i}", 3) for i in range(10)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(10, 30)],
            next_cursor="c2", has_more=True,
        )
        collector.add_page(
            cursor="c2", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(30, 50)],
            next_cursor="c3", has_more=True,
        )
        collector.add_page(
            cursor="c3", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(50, 80)],
            next_cursor="c4", has_more=True,
            reached_time_boundary=True, stop_reason="time_boundary",
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 50)
        self.assertEqual(artifact["recent_mature_item_count"], 20)
        self.assertEqual(artifact["collection_stop_reason"], "maximum_item_count_reached")
        self.assertEqual(len(collector.calls), 2)

    def test_high_producer_stops_after_third_page_reaches_fifty(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(18)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(18)],
            next_cursor="c2", has_more=True,
        )
        collector.add_page(
            cursor="c2", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(18, 32)],
            next_cursor="c3", has_more=True,
        )
        collector.add_page(
            cursor="c3", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(32, 60)],
            next_cursor="c4", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 50)
        self.assertEqual(artifact["collection_stop_reason"], "maximum_item_count_reached")
        self.assertEqual(len(collector.calls), 3)

    def test_thirty_five_recent_with_eighteen_mature_backfills_two(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(18)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(17)],
            next_cursor="c2", has_more=True,
            reached_time_boundary=True, stop_reason="time_boundary",
        )
        collector.add_page(
            cursor="c2", cutoff=False,
            items=[_item("older-0", 120), _item("older-1", 121)],
            next_cursor="c3", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 37)
        self.assertEqual(artifact["recent_mature_item_count"], 18)
        self.assertEqual(collector.calls, [(True, ""), (True, "c1"), (False, "c2")])

    def test_collection_cap_stops_before_older_backfill(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(18)]
            + [_item(f"recent-{i}", 3) for i in range(12)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(12, 32)],
            next_cursor="c2", has_more=True,
        )
        collector.add_page(
            cursor="c2", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(32, 42)],
            next_cursor="c3", has_more=True,
            reached_time_boundary=True, stop_reason="time_boundary",
        )
        collector.add_page(
            cursor="c3", cutoff=False,
            items=[_item("older-0", 120), _item("older-1", 121)],
            next_cursor="c4", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        recent_ids = {
            item["source_id"] for item in artifact["items"]
            if item["published_at"] >= int(time.time()) - 90 * 86400
        }
        self.assertEqual(len(recent_ids), 50)
        self.assertEqual(artifact["item_count"], 50)
        self.assertEqual(artifact["collector_returned_item_count"], 50)
        self.assertEqual(artifact["collection_stop_reason"], "maximum_item_count_reached")
        self.assertEqual(collector.calls, [(True, ""), (True, "c1")])
        self.assertNotIn("recent-41", recent_ids)

    def test_fifty_items_with_fewer_than_twenty_mature_still_stop(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(10)]
            + [_item(f"recent-{i}", 3) for i in range(20)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(20, 40)],
            next_cursor="c2", has_more=True,
        )
        collector.add_page(
            cursor="c2", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(40, 60)],
            next_cursor="c3", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 50)
        self.assertEqual(artifact["recent_mature_item_count"], 10)
        self.assertEqual(artifact["collection_stop_reason"], "maximum_item_count_reached")
        self.assertEqual(len(collector.calls), 2)

    def test_twenty_two_items_end_when_no_more(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(22)],
            next_cursor="", has_more=False,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 22)
        self.assertEqual(artifact["collection_stop_reason"], "mature_target_reached")
        self.assertEqual(len(collector.calls), 1)

    def test_forty_five_recent_with_twenty_five_mature_waits_for_boundary(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(25)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"recent-{i}", 3) for i in range(20)],
            next_cursor="c2", has_more=True,
            reached_time_boundary=True, stop_reason="time_boundary",
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 45)
        self.assertEqual(artifact["recent_mature_item_count"], 25)
        self.assertEqual(artifact["used_older_history_backfill"], False)
        self.assertEqual(len(collector.calls), 2)

    def test_duplicate_items_do_not_count_twice(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(10)],
            next_cursor="c1", has_more=True,
        )
        collector.add_page(
            cursor="c1", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(5)]
            + [_item(f"mature-{i}", 10) for i in range(10, 20)],
            next_cursor="c2", has_more=True, reached_time_boundary=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 20)
        self.assertEqual(len({item["source_id"] for item in artifact["items"]}), 20)

    def test_last_page_may_over_return_after_target(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(60)],
            next_cursor="c1", has_more=False,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["item_count"], 50)
        self.assertEqual(collector.calls, [(True, "")])

    def test_normal_exhaustion_is_explicit_and_valid(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(6)],
            next_cursor="", has_more=False,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        validate_historical_collection_artifact(artifact)
        self.assertEqual(artifact["collection_status"], "history_exhausted_insufficient")
        self.assertTrue(artifact["history_exhausted"])

    def test_collector_exception_is_not_history_exhaustion(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True, items=[_item("mature-0", 10)],
            next_cursor="c1", has_more=True, reached_time_boundary=True,
        )
        collector.fail_once.add((False, "c1"))
        with self.assertRaises(ExternalAdapterError):
            _executor(collector)._historical_material(_registration(), ())

    def test_risk_failure_is_not_history_exhaustion(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True, items=[_item("mature-0", 10)],
            next_cursor="c1", has_more=True, reached_time_boundary=True,
        )
        collector.fail_once.add((False, "c1"))
        with self.assertRaises(ExternalAdapterError):
            _executor(collector)._historical_material(_registration(), ())
        self.assertEqual(collector.calls, [(True, ""), (False, "c1")])

    def test_older_backfill_uses_current_cursor_without_restart(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"recent-{i}", 10) for i in range(10)],
            next_cursor="older-1", has_more=True, reached_time_boundary=True,
        )
        collector.add_page(
            cursor="older-1", cutoff=False,
            items=[_item(f"older-{i}", 120) for i in range(10)],
            next_cursor="older-2", has_more=True,
        )
        artifact = _executor(collector)._historical_material(_registration(), ()) [0]
        self.assertEqual(collector.calls, [(True, ""), (False, "older-1")])
        self.assertEqual(artifact["item_count"], 20)

    def test_interrupted_run_resumes_from_saved_cursor(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True, items=[_item("recent-0", 10)],
            next_cursor="c1", has_more=True, reached_time_boundary=True,
        )
        collector.add_page(
            cursor="c1", cutoff=False,
            items=[_item(f"older-{i}", 120) for i in range(20)],
            next_cursor="c2", has_more=True,
        )
        collector.fail_once.add((False, "c1"))
        core = FakePageCore()
        executor = _executor(collector, core)
        with self.assertRaises(ExternalAdapterError):
            executor._historical_material(_registration(), ())
        self.assertEqual(core.progress["registration-2b3"]["next_cursor"], "c1")
        artifact = executor._historical_material(_registration(), ()) [0]
        self.assertEqual(collector.calls, [(True, ""), (False, "c1"), (False, "c1")])
        self.assertEqual(artifact["item_count"], 20)

    def test_boundary_then_backfill_keeps_page_evidence(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"recent-{i}", 10) for i in range(3)],
            next_cursor="old-1", has_more=True, reached_time_boundary=True,
        )
        collector.add_page(
            cursor="old-1", cutoff=False,
            items=[_item(f"old-{i}", 120) for i in range(17)],
            next_cursor="old-2", has_more=True,
        )
        core = FakePageCore()
        artifact = _executor(collector, core)._historical_material(_registration(), ()) [0]
        self.assertEqual(artifact["page_request_count"], 2)
        self.assertEqual(len(artifact["raw_archive_refs"]), 2)
        self.assertEqual(core.progress["registration-2b3"]["status"], "completed")

    def test_paged_path_has_no_legacy_five_hundred_request(self) -> None:
        collector = FakePagedCollector()
        collector.add_page(
            cursor="", cutoff=True,
            items=[_item(f"mature-{i}", 10) for i in range(20)],
            next_cursor="c1", has_more=False,
        )
        executor = _executor(collector)
        self.assertFalse(hasattr(collector, "collect_video_snapshot"))
        executor._historical_material(_registration(), ())
        self.assertEqual(len(collector.calls), 1)

    def test_history_shortfall_stops_registration_before_baseline(self) -> None:
        connection = sqlite3.connect(":memory:")
        core = Stage0ContentProductionCore(
            connection, db_path=Path(":memory:"), data_identity="isolated-2b3",
        )
        core.install_schema()
        now = "2026-08-21T00:00:00+00:00"
        with connection:
            connection.execute(
                "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("owned-2b3", "owned", "自营", "domain-2b3", "douyin:owned", "active", "isolated-2b3", "isolated", now),
            )
            connection.execute(
                "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("competitor-2b3", "competitor", "对标", "domain-2b3", "douyin:competitor", "active", "isolated-2b3", "isolated", now),
            )
            connection.execute(
                "INSERT INTO stage0_cold_start VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("cold-2b3", "owned-2b3", "domain-2b3", "running", "isolated-2b3", "isolated", now, None),
            )
            connection.execute(
                "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("registration-2b3", "cold-2b3", "competitor-2b3", "historical_material", "processing", "isolated-2b3", now, None),
            )
        evaluated_at = int(time.time())
        artifact = build_historical_collection_artifact(
            platform="douyin",
            account_source_ref="douyin:competitor",
            items=[_item(f"short-{i}", 10) for i in range(6)],
            raw_archive_ref="isolated://shortfall/archive",
            command_hash="shortfall-command",
            output_hash="shortfall-output",
            evaluated_at=evaluated_at,
            collection_status="history_exhausted_insufficient",
            history_exhausted=True,
            collection_stop_reason="history_exhausted",
            page_request_count=1,
            raw_archive_refs=["isolated://shortfall/archive"],
        )
        with patch("scripts.core.production.stage0_content_core.record_runtime_guard_event"):
            result = core.record_competitor_registration_step(
                registration_id="registration-2b3",
                step_name="historical_material",
                artifact_refs=(artifact,),
                actor="隔离测试",
                idempotency_key="shortfall-record",
            )
        self.assertTrue(result["history_insufficient"])
        row = core.get_competitor_registration(registration_id="registration-2b3")
        self.assertEqual(row["status"], "awaiting_human_review")
        self.assertEqual(core.list_competitor_registration_steps(registration_id="registration-2b3"), [])
        self.assertIsNotNone(core.get_competitor_registration_history_shortfall(registration_id="registration-2b3"))
        core.close()


if __name__ == "__main__":
    unittest.main()
