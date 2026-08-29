from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from scripts.agent_platform.daily_operations_runtime import DailyOperationsCoordinator
from scripts.core.scheduler.executor_launch import (
    ExternalExecutorConfiguration,
    ExternalExecutorUnavailable,
    ExecutorLauncher,
)
from scripts.core.scheduler.schedule_registry import (
    ScheduleDisabledError,
    ScheduleRegistry,
    ScheduleRegistryError,
)
from scripts.core.scheduler.runtime import CreationAssistantScheduler


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
BOOTSTRAP_PATH = PROJECT_ROOT / "scripts" / "core" / "scheduler" / "bootstrap.py"


class Stage3SchedulerAuthorityTests(unittest.TestCase):
    def test_registry_has_one_core_managed_daily_schedule_and_is_disabled_in_migration(self) -> None:
        registry = ScheduleRegistry.load()
        self.assertEqual(registry.schedule_keys(), ("daily",))
        self.assertTrue(registry.migration_protection)
        daily = registry.get("daily")
        self.assertFalse(daily.enabled)
        self.assertEqual(daily.time, "08:00")
        self.assertEqual(daily.domain_scope, "core_managed")
        self.assertEqual(registry.due_keys(datetime.fromisoformat("2026-08-29T08:00:00+08:00")), ())

    def test_duplicate_schedule_key_is_rejected(self) -> None:
        duplicate_raw = (
            '{"registry_version":"creation_assistant_schedule_registry_v1",'
            '"migration_protection":true,'
            '"business_event_triggers_are_not_schedules":true,'
            '"schedules":{"daily":{"enabled":false,"time":"08:00",'
            '"timezone":"Asia/Shanghai","trigger_type":"absolute_time",'
            '"domain_scope":"core_managed"},"daily":{}}}'
        )
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "schedule.json"
            path.write_text(duplicate_raw, encoding="utf-8")
            with self.assertRaises(ScheduleRegistryError):
                ScheduleRegistry.load(path)

    def test_run_now_uses_test_world_without_changing_schedule(self) -> None:
        registry_path = PROJECT_ROOT / "config" / "schedule_registry.json"
        before = registry_path.read_bytes()
        registry = ScheduleRegistry.load(registry_path)
        observed: list[str] = []
        result = registry.run_now(
            key="daily",
            data_identity="test",
            runner=lambda schedule: observed.append(schedule.key) or "test-result",
        )
        self.assertEqual(result, "test-result")
        self.assertEqual(observed, ["daily"])
        self.assertEqual(registry_path.read_bytes(), before)

    def test_formal_automatic_daily_is_rejected_before_database_open(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "must-not-open.sqlite3"
            coordinator = DailyOperationsCoordinator(
                db_path=db_path,
                data_identity="production",
                domain_provider=lambda: (_ for _ in ()).throw(
                    AssertionError("domain lookup must not run")
                ),
            )
            with self.assertRaises(ScheduleDisabledError):
                coordinator.schedule_all(
                    domain_labels=None,
                    trigger="daily_scheduled",
                )
            self.assertFalse(db_path.exists())

    def test_executor_config_has_no_model_or_provider_policy(self) -> None:
        config = ExternalExecutorConfiguration.load()
        self.assertEqual(config.default_executor, "hermes")
        self.assertEqual(config.executor_ids(), ("hermes",))
        payload = json.loads(config.path.read_text(encoding="utf-8"))
        self.assertNotIn("model", payload)
        self.assertNotIn("provider", payload)
        self.assertNotIn("model", payload["executors"]["hermes"])
        self.assertNotIn("provider", payload["executors"]["hermes"])

    def test_running_executor_is_not_started_again(self) -> None:
        launcher = ExecutorLauncher()
        calls: list[tuple[tuple, dict]] = []
        result = launcher.ensure_started(
            is_running=lambda executor: executor == "hermes",
            process_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        self.assertEqual(result["status"], "already_running")
        self.assertEqual(calls, [])

    def test_missing_executor_is_started_once_from_its_config(self) -> None:
        launcher = ExecutorLauncher()
        calls: list[tuple[tuple, dict]] = []

        class Process:
            pid = 1234

        def process_factory(*args, **kwargs):
            calls.append((args, kwargs))
            return Process()

        result = launcher.ensure_started(
            is_running=lambda executor: False,
            process_factory=process_factory,
        )
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["pid"], 1234)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", "exec hermes"])

    def test_launch_failure_is_unavailable_and_never_falls_back(self) -> None:
        launcher = ExecutorLauncher()
        with self.assertRaises(ExternalExecutorUnavailable) as context:
            launcher.ensure_started(
                is_running=lambda executor: False,
                process_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
                    FileNotFoundError("hermes launcher missing")
                ),
            )
        self.assertIn("hermes", str(context.exception))

    def test_executor_selection_does_not_change_schedule_registry(self) -> None:
        runtime = CreationAssistantScheduler()
        before = runtime.registry.get("daily")
        self.assertEqual(runtime.executor_launcher.configuration.default_executor, "hermes")
        self.assertEqual(runtime.registry.get("daily"), before)

    def test_bootstrap_is_not_a_business_scheduler_or_executor_selector(self) -> None:
        source = BOOTSTRAP_PATH.read_text(encoding="utf-8").lower()
        for forbidden in ("08:00", "daily", "hermes", "codex", "provider", "mimo", "gpt"):
            self.assertNotIn(forbidden, source)

    def test_non_bootstrap_code_has_no_os_scheduler_creation(self) -> None:
        forbidden = (
            "schtasks",
            "register-scheduledtask",
            "new-scheduledtask",
            "systemd timer",
            "crontab",
        )
        violations: list[str] = []
        for path in SCRIPTS_ROOT.rglob("*.py"):
            if "__pycache__" in path.parts or path == BOOTSTRAP_PATH:
                continue
            source = path.read_text(encoding="utf-8", errors="ignore").lower()
            for marker in forbidden:
                if marker in source:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: {marker}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
