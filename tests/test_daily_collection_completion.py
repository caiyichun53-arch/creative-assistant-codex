from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService


class _Cursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class _ResumeConnection:
    def __init__(self, accounts: list[dict]) -> None:
        self.accounts = accounts

    def execute(self, sql: str, _params: tuple | list = ()) -> _Cursor:
        if "SELECT account.account_id" in sql:
            return _Cursor(self.accounts)
        if "SELECT hit.hit_id FROM hits" in sql:
            return _Cursor([])
        raise AssertionError(f"unexpected test query: {sql}")


class _ResumeCore:
    data_identity = "production"

    def __init__(self, accounts: list[dict], *, audit_account_ids: set[str] | None = None) -> None:
        self.conn = _ResumeConnection(accounts)
        self.completion_account_ids: set[str] = set()
        self.audit_account_ids = set(audit_account_ids or ())
        self.snapshot_calls: list[str] = []

    def daily_collection_account_ids(self, *, daily_run_id: str) -> set[str]:
        self.seen_daily_run_ids = getattr(self, "seen_daily_run_ids", [])
        self.seen_daily_run_ids.append(daily_run_id)
        return set(self.completion_account_ids)

    def record_daily_competitor_snapshot(self, **kwargs: object) -> dict:
        account_id = str(kwargs["account_id"])
        self.snapshot_calls.append(account_id)
        self.completion_account_ids.add(account_id)
        return {"source_refs": []}

    def judge_daily_competitor_hits(self, **_kwargs: object) -> dict:
        return {"new_hit_ids": []}


class _ResumeCollector:
    def __init__(self, *, failures: set[str] | None = None) -> None:
        self.failures = set(failures or ())
        self.calls: list[str] = []

    def collect_video_snapshot(self, **kwargs: object) -> SimpleNamespace:
        account_id = str(kwargs["source_url"]).rsplit("/", 1)[-1]
        self.calls.append(account_id)
        if account_id in self.failures:
            raise RuntimeError(f"collector failed for {account_id}")
        return SimpleNamespace(payload={"items": []}, raw_archive_ref=f"archive://{account_id}")


class DailyCollectionCompletionFactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "daily.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _core(self) -> Stage0ContentProductionCore:
        return Stage0ContentProductionCore.open(self.db_path, data_identity="test")

    def _account(self, core: Stage0ContentProductionCore, account_id: str) -> None:
        core.conn.execute(
            "INSERT INTO competitor_accounts("
            "account_id, platform, domain_label, domain_name, account_name, sec_uid, homepage_url, "
            "source_config_ref, registration_status, first_crawl_policy, comments_policy"
            ") VALUES (?, 'douyin', 'music_entertainment', '音乐娱乐', ?, ?, ?, ?, 'active', 'daily', 'none')",
            (
                account_id,
                account_id,
                f"sec-{account_id}",
                f"https://example.test/{account_id}",
                f"registration-{account_id}",
            ),
        )
        core.conn.commit()

    def _daily(self, core: Stage0ContentProductionCore, business_date: str) -> str:
        daily = core.get_or_create_daily_run(
            domain_label="music_entertainment",
            business_date=business_date,
            actor="test",
        )
        return str(daily["daily_run_id"])

    def _record(
        self,
        core: Stage0ContentProductionCore,
        *,
        account_id: str,
        daily_run_id: str,
        business_date: str,
        items: tuple[dict, ...],
    ) -> None:
        core.record_daily_competitor_snapshot(
            account_id=account_id,
            items=items,
            raw_archive_ref=f"archive://{account_id}",
            collection_run_id=f"daily_competitor:music_entertainment:{business_date}:{daily_run_id}:attempt-1",
            daily_run_id=daily_run_id,
            observed_at=datetime.fromisoformat(f"{business_date}T09:00:00+00:00"),
            business_date=business_date,
        )

    def test_successful_snapshot_with_items_records_completion(self) -> None:
        core = self._core()
        try:
            self._account(core, "account-1")
            daily_run_id = self._daily(core, "2026-08-28")
            self._record(
                core,
                account_id="account-1",
                daily_run_id=daily_run_id,
                business_date="2026-08-28",
                items=(
                    {
                        "source_id": "video-1",
                        "title": "测试视频",
                        "url": "https://example.test/video-1",
                        "published_at": "2026-08-28T01:00:00+00:00",
                        "duration_seconds": 10,
                        "metrics": {},
                    },
                ),
            )
            row = core.conn.execute(
                "SELECT daily_run_id, account_id, completed_at "
                "FROM daily_collection_account_completion"
            ).fetchone()
            self.assertEqual(row["daily_run_id"], daily_run_id)
            self.assertEqual(row["account_id"], "account-1")
            self.assertTrue(row["completed_at"])
        finally:
            core.close()

    def test_successful_empty_snapshot_records_completion(self) -> None:
        core = self._core()
        try:
            self._account(core, "account-empty")
            daily_run_id = self._daily(core, "2026-08-29")
            self._record(
                core,
                account_id="account-empty",
                daily_run_id=daily_run_id,
                business_date="2026-08-29",
                items=(),
            )
            self.assertEqual(
                core.daily_collection_account_ids(daily_run_id=daily_run_id),
                {"account-empty"},
            )
        finally:
            core.close()

    def test_failed_formal_persistence_does_not_record_completion(self) -> None:
        core = self._core()
        try:
            self._account(core, "account-1")
            daily_run_id = self._daily(core, "2026-08-30")
            with self.assertRaisesRegex(StateTransitionError, "lacks its platform identity or URL"):
                self._record(
                    core,
                    account_id="account-1",
                    daily_run_id=daily_run_id,
                    business_date="2026-08-30",
                    items=({"source_id": "video-1", "url": ""},),
                )
            self.assertEqual(
                core.daily_collection_account_ids(daily_run_id=daily_run_id),
                set(),
            )
        finally:
            core.close()

    def test_completion_query_ignores_audit_and_uses_exact_daily_run(self) -> None:
        core = self._core()
        try:
            self._account(core, "audit-only")
            self._account(core, "completion-only")
            daily_run_id = self._daily(core, "2026-08-31")
            core.conn.execute(
                "INSERT INTO stage0_audit_event VALUES (?, NULL, ?, ?, ?, ?)",
                (
                    "audit-1",
                    "daily_competitor_snapshot_recorded",
                    json.dumps({
                        "account_id": "audit-only",
                        "collection_run_id": f"daily_competitor:music_entertainment:2026-08-31:{daily_run_id}:old-attempt",
                    }),
                    "test",
                    "2026-08-31T01:00:00+00:00",
                ),
            )
            core.conn.execute(
                "INSERT INTO daily_collection_account_completion VALUES (?, ?, ?)",
                (daily_run_id, "completion-only", "2026-08-31T01:01:00+00:00"),
            )
            core.conn.commit()
            self.assertEqual(
                core.daily_collection_account_ids(daily_run_id=daily_run_id),
                {"completion-only"},
            )
        finally:
            core.close()

    def test_failed_collector_does_not_create_completion_and_resume_retries_only_it(self) -> None:
        accounts = [
            {
                "account_id": account_id,
                "account_name": account_id,
                "platform": "douyin",
                "homepage_url": f"https://example.test/{account_id}",
            }
            for account_id in ("account-a", "account-b", "account-c")
        ]
        core = _ResumeCore(accounts, audit_account_ids={"account-a", "account-b"})
        first_collector = _ResumeCollector(failures={"account-c"})
        second_collector = _ResumeCollector()
        service = ProductionDailyOperationsService(core=core, collector=first_collector)
        with (
            patch(
                "scripts.core.production.stage1_daily_operations.enforce_runtime_startup_guard",
                return_value={"valid": True},
            ),
            patch(
                "scripts.core.production.stage1_daily_operations.enforce_daily_operations_runtime_guard",
                return_value=None,
            ),
        ):
            first = service.run(
                domain_label="music_entertainment",
                discovery_date="2026-08-28",
                actor="test",
                attempt_ref="attempt-1",
                daily_run_id="daily-run-1",
                effective_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(core.completion_account_ids, {"account-a", "account-b"})
            service.collector = second_collector
            resumed = service.run(
                domain_label="music_entertainment",
                discovery_date="2026-08-28",
                actor="test",
                attempt_ref="attempt-2",
                daily_run_id="daily-run-1",
                resume=True,
                effective_at=datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(first["status"], "completed_with_failures")
        self.assertEqual(first_collector.calls, ["account-a", "account-b", "account-c"])
        self.assertEqual(core.completion_account_ids, {"account-a", "account-b", "account-c"})
        self.assertEqual(second_collector.calls, ["account-c"])
        self.assertEqual(
            [item["status"] for item in resumed["collection"]],
            ["reused_completed_snapshot", "reused_completed_snapshot", "completed"],
        )
        self.assertEqual(resumed["run_id"], "daily-run-1")
        self.assertEqual(core.seen_daily_run_ids, ["daily-run-1"])


if __name__ == "__main__":
    unittest.main()
