"""Focused isolation checks for detaching cold-start work from one tool call."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agent_platform.cold_start_background_entry import (
    ColdStartBackgroundLaunchError,
    _failure_notification_reason,
    _write_stdout_json_utf8,
    launch_cold_start_background,
    prepare_cold_start_background_execution,
    run_cold_start_background_execution,
)
from scripts.agent_platform.cold_start_background_control import (
    read_executor_record,
)
from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    set_domain_pack_config_dir,
)
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class ColdStartBackgroundExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_dir = self.root / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()

    def tearDown(self) -> None:
        self.core.close()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)
        self.temp_dir.cleanup()

    @staticmethod
    def configuration(domain: str) -> dict[str, object]:
        return {
            "domain_name": domain,
            "domain_mode": "create",
            "platform": "douyin",
            "actor": "隔离用户",
            "owned_account": {
                "display_name": "自营账号",
                "external_account_ref": f"douyin:{domain}:owned",
            },
            "competitor_accounts": [
                {
                    "display_name": f"对标账号{index}",
                    "external_account_ref": f"douyin:{domain}:competitor:{index}",
                }
                for index in range(20)
            ],
        }

    def create_with_launcher(self, *, domain: str, launcher: object) -> dict[str, object]:
        service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            background_execution_launcher=launcher,
        )
        payload = self.configuration(domain)
        preview = service.preview(payload)
        return service.confirm(
            payload,
        )

    def test_failure_notification_uses_formal_breakdown_reason_and_counts(self) -> None:
        class FakeCursor:
            def fetchall(self) -> list[dict[str, str]]:
                return [
                    {"status": "completed", "error_json": "{}"},
                    {"status": "failed", "error_json": '{"reason":"OpenRouter 429"}'},
                    {"status": "failed", "error_json": '{"reason":"OpenRouter 429"}'},
                ]

        class FakeConnection:
            def execute(self, *_args: object) -> FakeCursor:
                return FakeCursor()

        class FakeCore:
            data_identity = "test"
            conn = FakeConnection()

        result = _failure_notification_reason(
            FakeCore(),
            cold_start_id="run-1",
            fallback="executor failed",
        )
        self.assertIn("总计：3条", result)
        self.assertIn("已完成：1条", result)
        self.assertIn("剩余：2条", result)
        self.assertIn("主要原因：OpenRouter 429", result)
        self.assertIn("状态：可恢复", result)

    def test_failure_notification_uses_candidate_stage_when_breakdown_is_complete(self) -> None:
        class Cursor:
            def __init__(self, *, rows: list[dict[str, str]] | None = None, row: dict[str, str] | None = None) -> None:
                self.rows = rows or []
                self.row = row

            def fetchall(self) -> list[dict[str, str]]:
                return self.rows

            def fetchone(self) -> dict[str, str] | None:
                return self.row

        class Connection:
            def execute(self, sql: str, *_args: object) -> Cursor:
                if "stage0_competitor_registration_item" in sql:
                    return Cursor(rows=[])
                if "stage0_cold_start_domain_boundary_candidate" in sql:
                    return Cursor(
                        row={
                            "status": "failed",
                            "failure_json": json.dumps(
                                {"reason": "model provider failed: content_filter"}
                            ),
                            "created_at": "2026-08-26T17:37:58+00:00",
                        }
                    )
                return Cursor(row=None)

        class FakeCore:
            data_identity = "test"
            conn = Connection()

        result = _failure_notification_reason(
            FakeCore(),
            cold_start_id="run-1",
            fallback="background result output failed",
        )
        self.assertIn("【生产边界候选生成失败】", result)
        self.assertIn("content_filter", result)
        self.assertNotIn("内容拆解中断", result)

    def test_background_result_json_is_written_as_utf8(self) -> None:
        class BinaryStdout:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, _text: str) -> int:
                raise AssertionError("text path must not be used when a binary buffer exists")

            def flush(self) -> None:
                return None

        stdout = BinaryStdout()
        payload = {
            "text": '中文 💩 🎵 "quoted" \\ slash\n- Markdown',
        }
        with patch("scripts.agent_platform.cold_start_background_entry.sys.stdout", stdout):
            _write_stdout_json_utf8(payload)

        raw = stdout.buffer.getvalue()
        self.assertEqual(raw.decode("utf-8"), json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self.assertEqual(json.loads(raw.decode("utf-8")), payload)

    def test_detached_process_returns_before_business_finishes_and_keeps_running(self) -> None:
        startup_root = self.root / "background"
        notification_target = {
            "platform": "feishu",
            "chat_id": "oc_dynamic_chat",
            "thread_id": "omt_dynamic_thread",
        }
        started_at = time.monotonic()
        launch = launch_cold_start_background(
            configuration_id="configuration-isolated",
            cold_start_id="cold-start-isolated",
            actor="隔离用户",
            notification_target=notification_target,
            entry_module="tests._cold_start_background_process_fixture",
            startup_root=startup_root,
            startup_timeout_seconds=5.0,
        )
        elapsed = time.monotonic() - started_at
        completion = startup_root / "cold-start-isolated.done.json"

        self.assertTrue(launch["started"])
        self.assertEqual(launch["cold_start_id"], "cold-start-isolated")
        self.assertNotIn("notification_target", launch)
        self.assertNotIn("oc_dynamic_chat", repr(launch))
        executor_record = read_executor_record(Path(str(launch["executor_record"])))
        self.assertEqual((executor_record or {}).get("notification_target"), notification_target)
        self.assertLess(elapsed, 0.7)
        self.assertFalse(completion.exists())

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not completion.exists():
            time.sleep(0.05)
        self.assertTrue(completion.exists())

    def test_valid_failed_receipt_returns_real_reason_and_allows_child_cleanup(
        self,
    ) -> None:
        startup_root = self.root / "failed-background"
        cleanup = startup_root / "cold-start-failed.cleanup.json"
        notification_target = {
            "platform": "feishu",
            "chat_id": "oc_private_failure_chat",
            "thread_id": "omt_private_failure_thread",
        }

        started_at = time.monotonic()
        with self.assertRaises(ColdStartBackgroundLaunchError) as raised:
            launch_cold_start_background(
                configuration_id="configuration-failed",
                cold_start_id="cold-start-failed",
                actor="fail-before-ready",
                notification_target=notification_target,
                entry_module="tests._cold_start_background_process_fixture",
                startup_root=startup_root,
                startup_timeout_seconds=15.0,
            )
        elapsed = time.monotonic() - started_at

        error_text = str(raised.exception)
        self.assertEqual(
            error_text,
            "isolated child reported its real startup failure",
        )
        self.assertNotIn("timeout", error_text.lower())
        self.assertNotIn(notification_target["chat_id"], error_text)
        self.assertNotIn(notification_target["thread_id"], error_text)
        self.assertLess(elapsed, 5.0)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not cleanup.exists():
            time.sleep(0.05)
        self.assertTrue(
            cleanup.exists(),
            "worker was terminated before its short cleanup could complete",
        )

    def test_creation_returns_same_run_and_frozen_model_without_second_run(self) -> None:
        launches: list[dict[str, object]] = []

        def launcher(**values: object) -> dict[str, object]:
            launches.append(dict(values))
            return {
                "status": "background_started",
                "started": True,
                "cold_start_id": values["cold_start_id"],
                "configuration_id": values["configuration_id"],
                "pid": 12345,
            }

        created = self.create_with_launcher(
            domain="后台隔离领域",
            launcher=launcher,
        )
        cold_start_id = str(created["cold_start_id"])
        self.assertTrue(created["automatic_start"])
        self.assertEqual(launches[0]["cold_start_id"], cold_start_id)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )

        reentered = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            background_execution_launcher=launcher,
        )._automatic_start(
            configuration_id=str(created["configuration_id"]),
            actor="隔离用户",
        )
        self.assertFalse(reentered["automatic_start"])
        self.assertTrue(reentered["background_executor_reused"])
        self.assertEqual(reentered["cold_start_id"], cold_start_id)
        self.assertEqual(len(launches), 1)

        captured: list[dict[str, object]] = []

        class RegistrationService:
            def __init__(self, *, core: object, executor: object) -> None:
                self.core = core
                self.executor = executor

        service, frozen = prepare_cold_start_background_execution(
            self.core,
            configuration_id=str(created["configuration_id"]),
            cold_start_id=cold_start_id,
            executor_builder=lambda core: captured.append({}) or object(),
            registration_service_factory=RegistrationService,
        )
        self.assertEqual(frozen, {})
        self.assertEqual(captured[0], {})

        phase_events: list[tuple[str, str]] = []

        def executor_builder_with_progress(
            core: object,
            progress_callback: object,
        ) -> object:
            del core
            progress_callback("hit_filtering", "still working")  # type: ignore[operator]
            return object()

        service, frozen_with_progress = prepare_cold_start_background_execution(
            self.core,
            configuration_id=str(created["configuration_id"]),
            cold_start_id=cold_start_id,
            executor_builder=executor_builder_with_progress,
            registration_service_factory=RegistrationService,
            progress_callback=lambda phase, detail: phase_events.append((phase, detail)),
        )
        self.assertEqual(frozen_with_progress, frozen)
        self.assertEqual(phase_events, [("hit_filtering", "still working")])

        seen_runs: list[str] = []
        orchestrator_payloads: list[dict[str, object]] = []

        def existing_orchestrator_run(instance: object, **values: object) -> dict[str, object]:
            seen_runs.append(str(values["cold_start_id"]))
            instance._emit({  # type: ignore[attr-defined]
                "event": "registration_step_started",
                "account_name": "account-one",
                "registration_id": "registration-one",
                "step_name": "historical_material",
            })
            return {"status": "running", "failures": []}

        with patch(
            "scripts.core.production.cold_start_orchestrator.ColdStartExecutionOrchestrator.run",
            new=existing_orchestrator_run,
        ):
            result = run_cold_start_background_execution(
                self.core,
                configuration_id=str(created["configuration_id"]),
                cold_start_id=cold_start_id,
                actor="隔离用户",
                registration_service=service,
                progress_callback=lambda payload: orchestrator_payloads.append(dict(payload)),
            )
        self.assertEqual(result["cold_start_id"], cold_start_id)
        self.assertEqual(seen_runs, [cold_start_id])
        self.assertEqual(orchestrator_payloads, [{
            "event": "registration_step_started",
            "account_name": "account-one",
            "registration_id": "registration-one",
            "step_name": "historical_material",
        }])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )

    def test_background_launch_failure_marks_run_failed_and_never_reports_started(self) -> None:
        def failing_launcher(**values: object) -> dict[str, object]:
            self.core.fail_configured_cold_start(
                configuration_id=str(values["configuration_id"]),
                actor="隔离后台子进程",
                reason="isolated child failed before ready receipt",
            )
            raise RuntimeError("isolated background startup failure")

        result = self.create_with_launcher(
            domain="后台启动失败领域",
            launcher=failing_launcher,
        )
        status = self.connection.execute(
            "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
            (str(result["cold_start_id"]),),
        ).fetchone()[0]
        self.assertFalse(result["automatic_start"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["execution"]["status"], "background_launch_failed")
        self.assertIn("isolated background startup failure", result["execution"]["reason"])
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
