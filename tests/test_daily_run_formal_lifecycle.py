from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.agent_platform.daily_operations_runtime import DailyOperationsCoordinator
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class DailyRunFormalLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "daily.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _core(self) -> Stage0ContentProductionCore:
        return Stage0ContentProductionCore.open(self.db_path, data_identity="test")

    def test_domain_and_business_date_have_one_formal_run(self) -> None:
        core = self._core()
        try:
            first = core.get_or_create_daily_run(
                domain_label="music_entertainment",
                business_date="2026-08-27",
                actor="test",
            )
            second = core.get_or_create_daily_run(
                domain_label="music_entertainment",
                business_date="2026-08-27",
                actor="test",
            )
            other_domain = core.get_or_create_daily_run(
                domain_label="another_domain",
                business_date="2026-08-27",
                actor="test",
            )
            self.assertEqual(first["daily_run_id"], second["daily_run_id"])
            self.assertNotEqual(first["daily_run_id"], other_domain["daily_run_id"])
            count = core.conn.execute(
                "SELECT COUNT(*) FROM stage0_daily_run WHERE business_date=?",
                ("2026-08-27",),
            ).fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            core.close()

    def test_automatic_failure_waits_for_explicit_resume_of_same_run(self) -> None:
        now = lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
        first_calls: list[dict] = []

        def fail_runner(**kwargs):
            first_calls.append(kwargs)
            return {"status": "failed", "failure_details": [{"stage": "test"}]}

        guard = patch(
            "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
            lambda **kwargs: None,
        )
        with guard:
            first = DailyOperationsCoordinator(
                db_path=self.db_path,
                data_identity="test",
                runner=fail_runner,
                domain_provider=lambda: ["music_entertainment"],
                now_provider=now,
            )
            first_result = first.schedule_all(
                domain_labels=("music_entertainment",),
                trigger="automatic",
                attempt_ref="execution-1",
            )
            self.assertEqual(first_result["domain_results"][0]["daily_run_status"], "failed")

            blocked_calls: list[dict] = []

            def unexpected_runner(**kwargs):
                blocked_calls.append(kwargs)
                return {"status": "completed"}

            automatic_again = DailyOperationsCoordinator(
                db_path=self.db_path,
                data_identity="test",
                runner=unexpected_runner,
                domain_provider=lambda: ["music_entertainment"],
                now_provider=now,
            )
            automatic_result = automatic_again.schedule_all(
                domain_labels=("music_entertainment",),
                trigger="automatic",
                attempt_ref="execution-2",
            )
            self.assertEqual(
                automatic_result["domain_results"][0]["action"],
                "awaiting_user_resume",
            )
            self.assertEqual(blocked_calls, [])

            resumed_calls: list[dict] = []

            def resume_runner(**kwargs):
                resumed_calls.append(kwargs)
                return {"status": "completed"}

            resumed = DailyOperationsCoordinator(
                db_path=self.db_path,
                data_identity="test",
                runner=resume_runner,
                domain_provider=lambda: ["music_entertainment"],
                now_provider=now,
            )
            resumed_result = resumed.schedule_all(
                domain_labels=("music_entertainment",),
                trigger="user_resume",
                resume=True,
                attempt_ref="execution-3",
            )
            self.assertEqual(resumed_result["domain_results"][0]["daily_run_status"], "completed")
            self.assertEqual(len(first_calls), 1)
            self.assertEqual(len(resumed_calls), 1)
            self.assertEqual(
                first_calls[0]["daily_run_id"], resumed_calls[0]["daily_run_id"]
            )

            completed_again = DailyOperationsCoordinator(
                db_path=self.db_path,
                data_identity="test",
                runner=unexpected_runner,
                domain_provider=lambda: ["music_entertainment"],
                now_provider=now,
            )
            completed_result = completed_again.schedule_all(
                domain_labels=("music_entertainment",),
                trigger="automatic",
                attempt_ref="execution-4",
            )
            self.assertEqual(
                completed_result["domain_results"][0]["action"],
                "skipped_completed",
            )
            self.assertEqual(blocked_calls, [])

    def test_failed_domain_does_not_stop_the_next_domain(self) -> None:
        calls: list[str] = []

        def runner(**kwargs):
            calls.append(kwargs["domain_label"])
            return {"status": "failed" if kwargs["domain_label"] == "alpha" else "completed"}

        guard = patch(
            "scripts.agent_platform.daily_operations_runtime.enforce_daily_operations_runtime_guard",
            lambda **kwargs: None,
        )
        with guard:
            coordinator = DailyOperationsCoordinator(
                db_path=self.db_path,
                data_identity="test",
                runner=runner,
                domain_provider=lambda: ["alpha", "beta"],
                now_provider=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
            )
            result = coordinator.schedule_all(
                domain_labels=("alpha", "beta"),
                trigger="automatic",
                attempt_ref="execution-1",
            )
        self.assertEqual(calls, ["alpha", "beta"])
        self.assertEqual(
            [item["daily_run_status"] for item in result["domain_results"]],
            ["failed", "completed"],
        )

    def test_candidate_stage_keeps_daily_run_as_its_parent_business_fact(self) -> None:
        core = self._core()
        try:
            daily = core.get_or_create_daily_run(
                domain_label="music_entertainment",
                business_date="2026-08-27",
                actor="test",
            )
            discovery = core.create_discovery_run(
                discovery_date="2026-08-27",
                actor="test",
                execution_mode="test_isolated",
                domains=("music_entertainment",),
                idempotency_key="test-discovery-1",
                daily_run_id=str(daily["daily_run_id"]),
            )
            row = core.conn.execute(
                "SELECT daily_run_id FROM stage1b_run_execution_context WHERE run_id=?",
                (discovery["run_id"],),
            ).fetchone()
            self.assertEqual(row["daily_run_id"], daily["daily_run_id"])
        finally:
            core.close()


if __name__ == "__main__":
    unittest.main()
