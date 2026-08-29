"""Isolation checks for real status, stop and resume control of one background run."""

from __future__ import annotations

from functools import partial
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agent_platform import cold_start_background_control as background_control
from scripts.agent_platform.cold_start_background_control import (
    ColdStartExecutorControlError,
    activate_cold_start_background,
    finish_cold_start_background,
    get_cold_start_notification_target,
    inspect_cold_start_background,
    read_executor_record,
    reserve_cold_start_background,
    start_cold_start_heartbeat,
    stop_cold_start_background,
    write_executor_record,
)
from scripts.agent_platform.cold_start_background_entry import (
    launch_cold_start_background,
)
from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    set_domain_pack_config_dir,
)
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from tests._cold_start_test_model import test_task_model_resolver


class ColdStartBackgroundControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_dir = self.root / "domain_packs"
        self.executor_root = self.root / "executors"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        self.run_ids: list[str] = []

    def tearDown(self) -> None:
        for cold_start_id in self.run_ids:
            try:
                stop_cold_start_background(
                    cold_start_id=cold_start_id,
                    startup_root=self.executor_root,
                    wait_timeout_seconds=3.0,
                )
            except Exception:
                pass
        self.core.close()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)
        self.temp_dir.cleanup()

    @staticmethod
    def configuration(domain: str) -> dict[str, object]:
        return {
            "domain_mode": "create",
            "platform": "douyin",
            "domain_name": domain,
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

    def launcher(self, **values: object) -> dict[str, object]:
        return launch_cold_start_background(
            configuration_id=str(values["configuration_id"]),
            cold_start_id=str(values["cold_start_id"]),
            actor=str(values["actor"]),
            entry_module="tests._cold_start_background_control_fixture",
            startup_root=self.executor_root,
            startup_timeout_seconds=5.0,
        )

    def inspector(self, **values: object) -> dict[str, object]:
        return inspect_cold_start_background(
            cold_start_id=str(values["cold_start_id"]),
            startup_root=self.executor_root,
        )

    def stopper(self, **values: object) -> dict[str, object]:
        return stop_cold_start_background(
            cold_start_id=str(values["cold_start_id"]),
            startup_root=self.executor_root,
            wait_timeout_seconds=5.0,
        )

    def service(self, *, launcher: object | None = None) -> ColdStartOnboardingService:
        return ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=test_task_model_resolver,
            background_execution_launcher=launcher or self.launcher,
            background_execution_inspector=self.inspector,
            background_execution_stopper=self.stopper,
        )

    def create(self, domain: str, *, launcher: object | None = None) -> tuple[ColdStartOnboardingService, dict[str, object]]:
        service = self.service(launcher=launcher)
        payload = self.configuration(domain)
        preview = service.preview(payload)
        result = service.confirm(
            payload,
            trusted_internal_context={"task_model_name": "isolated/model-a"},
        )
        self.run_ids.append(str(result["cold_start_id"]))
        return service, result

    def wait_for_marker(self, path: Path) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.05)
        self.assertTrue(path.exists())

    def test_status_reports_lifecycle_business_progress_and_active_executor(self) -> None:
        service, created = self.create("后台状态领域")
        status = service.current_cold_start_status(
            actor="隔离用户", cold_start_id=str(created["cold_start_id"])
        )
        self.assertEqual(status["status"], "running")
        self.assertTrue(status["executor"]["active"])
        self.assertTrue(status["executor"]["process_exists"])
        self.assertEqual(status["executor"]["consistency"], "consistent")
        self.assertEqual(status["counts"]["owned_accounts"], 1)
        self.assertEqual(status["counts"]["competitor_accounts"], 20)
        self.assertEqual(status["current_stage"], "historical_collection")
        self.assertEqual(status["cold_start_id"], created["cold_start_id"])

    def test_stop_terminates_process_tree_and_repeated_stop_is_state_only(self) -> None:
        service, created = self.create("后台停止领域")
        cold_start_id = str(created["cold_start_id"])
        root_marker = self.executor_root / f"{cold_start_id}.root.txt"
        child_marker = self.executor_root / f"{cold_start_id}.child.txt"
        self.wait_for_marker(root_marker)
        self.wait_for_marker(child_marker)

        stopped = service.stop_current_cold_start(
            actor="隔离用户",
            reason="隔离停止",
            cold_start_id=cold_start_id,
        )
        root_value = root_marker.read_text(encoding="utf-8")
        child_value = child_marker.read_text(encoding="utf-8")
        time.sleep(0.3)

        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.inspector(cold_start_id=cold_start_id)["active"])
        self.assertEqual(root_marker.read_text(encoding="utf-8"), root_value)
        self.assertEqual(child_marker.read_text(encoding="utf-8"), child_value)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (cold_start_id,),
            ).fetchone()[0],
            "stopped",
        )
        repeated = service.stop_current_cold_start(
            actor="隔离用户",
            reason="重复停止",
            cold_start_id=cold_start_id,
        )
        self.assertEqual(repeated["status"], "stopped")
        self.assertEqual(repeated["message"], "当前冷启动已经停止。")

    def test_stopped_resume_uses_current_execution_model_and_new_executor(self) -> None:
        service, created = self.create("后台恢复领域")
        original_pid = int(created["execution"]["pid"])
        cold_start_id = str(created["cold_start_id"])
        service.stop_current_cold_start(
            actor="隔离用户",
            reason="恢复前停止",
            cold_start_id=cold_start_id,
        )
        resumed = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
            trusted_internal_context={"task_model_name": "isolated/model-b"},
        )
        self.assertEqual(resumed["cold_start_id"], cold_start_id)
        self.assertEqual(resumed["run_model"], "")
        self.assertNotEqual(int(resumed["execution"]["pid"]), original_pid)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )
        self.assertTrue(self.inspector(cold_start_id=cold_start_id)["active"])

    def test_running_resume_returns_current_state_without_second_executor(self) -> None:
        service, created = self.create("后台运行中重复恢复领域")
        cold_start_id = str(created["cold_start_id"])
        original_pid = int(created["execution"]["pid"])
        resumed = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
            trusted_internal_context={"task_model_name": "isolated/model-b"},
        )
        self.assertEqual(resumed["status"], "running")
        self.assertFalse(resumed["resumed"])
        self.assertFalse(resumed["created_new_run"])
        self.assertEqual(int(self.inspector(cold_start_id=cold_start_id)["pid"]), original_pid)
        self.assertTrue(self.inspector(cold_start_id=cold_start_id)["active"])

    def test_resume_refuses_second_executor_when_old_one_is_alive(self) -> None:
        service, created = self.create("后台双执行器领域")
        cold_start_id = str(created["cold_start_id"])
        with self.connection:
            self.connection.execute(
                "UPDATE stage0_cold_start SET status='stopped' WHERE cold_start_id=?",
                (cold_start_id,),
            )
            self.connection.execute(
                "UPDATE stage0_cold_start_configuration SET status='cancelled' "
                "WHERE cold_start_id=?",
                (cold_start_id,),
            )
        with self.assertRaisesRegex(StateTransitionError, "must exit before resume"):
            service.resume_current_cold_start(
                actor="隔离用户",
                cold_start_id=cold_start_id,
            )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )

    def test_failed_resume_reuses_same_run(self) -> None:
        service, created = self.create("后台失败恢复领域")
        cold_start_id = str(created["cold_start_id"])
        self.stopper(cold_start_id=cold_start_id)
        self.core.fail_configured_cold_start(
            configuration_id=str(created["configuration_id"]),
            actor="隔离用户",
            reason="隔离失败",
        )
        resumed = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
            trusted_internal_context={"task_model_name": "isolated/model-b"},
        )
        self.assertEqual(resumed["cold_start_id"], cold_start_id)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(resumed["run_model"], "")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )

    def test_repeated_resume_refreshes_only_the_next_execution_model(self) -> None:
        service, created = self.create("后台重复恢复模型领域")
        cold_start_id = str(created["cold_start_id"])
        service.stop_current_cold_start(
            actor="隔离用户",
            reason="第一次恢复前停止",
            cold_start_id=cold_start_id,
        )
        first_resume = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
            trusted_internal_context={"task_model_name": "isolated/model-b"},
        )
        self.assertEqual(first_resume["run_model"], "")
        service.stop_current_cold_start(
            actor="隔离用户",
            reason="第二次恢复前停止",
            cold_start_id=cold_start_id,
        )
        second_resume = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
            trusted_internal_context={"task_model_name": "isolated/model-c"},
        )
        self.assertEqual(second_resume["cold_start_id"], cold_start_id)
        self.assertEqual(second_resume["run_model"], "")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start WHERE cold_start_id=?",
                (cold_start_id,),
            ).fetchone()[0],
            1,
        )

    def test_crashed_executor_is_reconciled_to_failed_by_status(self) -> None:
        def crash_launcher(**values: object) -> dict[str, object]:
            return launch_cold_start_background(
                configuration_id=str(values["configuration_id"]),
                cold_start_id=str(values["cold_start_id"]),
                actor="crash",
                entry_module="tests._cold_start_background_control_fixture",
                startup_root=self.executor_root,
                startup_timeout_seconds=5.0,
            )

        service, created = self.create("后台异常退出领域", launcher=crash_launcher)
        cold_start_id = str(created["cold_start_id"])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not self.inspector(cold_start_id=cold_start_id)["active"]:
                break
            time.sleep(0.05)
        status = service.current_cold_start_status(
            actor="隔离用户", cold_start_id=cold_start_id
        )
        self.assertEqual(status["status"], "failed")
        self.assertFalse(status["executor"]["active"])
        self.assertTrue(status["executor"]["reconciled_missing_executor"])

    def test_waiting_human_resume_keeps_run_and_reports_only_pending_confirmations(self) -> None:
        service, created = self.create("后台等待人工确认领域")
        cold_start_id = str(created["cold_start_id"])
        self.stopper(cold_start_id=cold_start_id)
        with self.connection:
            self.connection.execute(
                "UPDATE stage0_cold_start SET status='waiting_human' WHERE cold_start_id=?",
                (cold_start_id,),
            )
        resumed = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
        )
        self.assertEqual(resumed["status"], "waiting_human")
        self.assertFalse(resumed["resumed"])
        self.assertEqual(resumed["cold_start_id"], cold_start_id)
        self.assertIn("review_tags", resumed["pending_actions"])
        self.assertIn("review_content_types", resumed["pending_actions"])
        self.assertIn("review_domain_boundary", resumed["pending_actions"])
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (cold_start_id,),
            ).fetchone()[0],
            "waiting_human",
        )
    def test_completed_run_cannot_resume(self) -> None:
        service, created = self.create("后台完成状态领域")
        cold_start_id = str(created["cold_start_id"])
        self.stopper(cold_start_id=cold_start_id)
        with self.connection:
            self.connection.execute(
                "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id=?",
                (cold_start_id,),
            )
            self.connection.execute(
                "UPDATE stage0_cold_start_configuration SET status='completed' "
                "WHERE cold_start_id=?",
                (cold_start_id,),
            )
        resumed = service.resume_current_cold_start(
            actor="隔离用户",
            cold_start_id=cold_start_id,
        )
        self.assertFalse(resumed["resumed"])
        self.assertEqual(resumed["status"], "completed")
        status = service.current_cold_start_status(
            actor="隔离用户", cold_start_id=cold_start_id
        )
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["executor"]["active"])


    def test_notification_target_is_bound_once_hidden_and_preserved_on_resume(self) -> None:
        cold_start_id = "notification-target-run"
        original_target = {
            "platform": "feishu",
            "chat_id": "dynamic-chat-a",
            "thread_id": "dynamic-thread-a",
        }
        record_path, worker_token = reserve_cold_start_background(
            configuration_id="notification-target-configuration",
            cold_start_id=cold_start_id,
            startup_root=self.executor_root,
            notification_target=original_target,
        )
        self.assertEqual(
            get_cold_start_notification_target(
                cold_start_id=cold_start_id,
                startup_root=self.executor_root,
            ),
            original_target,
        )
        self.assertNotIn(
            "notification_target",
            inspect_cold_start_background(
                cold_start_id=cold_start_id,
                startup_root=self.executor_root,
            ),
        )
        finish_cold_start_background(
            record_path=record_path,
            worker_token=worker_token,
            state="stopped",
            lifecycle_status="stopped",
        )

        with self.assertRaisesRegex(ColdStartExecutorControlError, "already bound"):
            reserve_cold_start_background(
                configuration_id="notification-target-configuration",
                cold_start_id=cold_start_id,
                startup_root=self.executor_root,
                notification_target={
                    "platform": "feishu",
                    "chat_id": "dynamic-chat-b",
                    "thread_id": "dynamic-thread-b",
                },
            )

        resumed_path, resumed_token = reserve_cold_start_background(
            configuration_id="notification-target-configuration",
            cold_start_id=cold_start_id,
            startup_root=self.executor_root,
        )
        self.assertEqual(resumed_path, record_path)
        self.assertEqual(
            get_cold_start_notification_target(
                cold_start_id=cold_start_id,
                startup_root=self.executor_root,
            ),
            original_target,
        )
        self.assertNotIn(
            "notification_target",
            inspect_cold_start_background(
                cold_start_id=cold_start_id,
                startup_root=self.executor_root,
            ),
        )
        finish_cold_start_background(
            record_path=resumed_path,
            worker_token=resumed_token,
            state="stopped",
            lifecycle_status="stopped",
        )

    def test_activate_refuses_stopped_reservation_without_changing_state(self) -> None:
        record_path, worker_token = reserve_cold_start_background(
            configuration_id="activation-race-configuration",
            cold_start_id="activation-race-run",
            startup_root=self.executor_root,
        )
        record = read_executor_record(record_path) or {}
        stopped = {
            **record,
            "state": "stopped",
            "lifecycle_status": "stopped",
            "reason": "stop won before activation",
        }
        write_executor_record(record_path, stopped)

        with self.assertRaisesRegex(
            ColdStartExecutorControlError,
            "no longer starting",
        ):
            activate_cold_start_background(
                record_path=record_path,
                worker_token=worker_token,
                run_model="isolated/model-a",
            )

        self.assertEqual(read_executor_record(record_path), stopped)

    def test_heartbeat_does_not_overwrite_stop_that_wins_before_update(self) -> None:
        target = {
            "platform": "feishu",
            "chat_id": "heartbeat-stop-race-chat",
            "thread_id": "heartbeat-stop-race-thread",
        }
        record_path, worker_token = reserve_cold_start_background(
            configuration_id="heartbeat-stop-race-configuration",
            cold_start_id="heartbeat-stop-race-run",
            startup_root=self.executor_root,
            notification_target=target,
        )
        activate_cold_start_background(
            record_path=record_path,
            worker_token=worker_token,
            run_model="isolated/model-a",
        )
        refresh_entered = threading.Event()
        release_refresh = threading.Event()
        callbacks: list[dict[str, object]] = []
        original_refresh = background_control._refresh_running_heartbeat

        def delayed_refresh(**values: object) -> dict[str, object] | None:
            refresh_entered.set()
            release_refresh.wait(timeout=5.0)
            return original_refresh(
                record_path=Path(values["record_path"]),
                worker_token=str(values["worker_token"]),
            )

        with patch.object(
            background_control,
            "_refresh_running_heartbeat",
            side_effect=delayed_refresh,
        ):
            heartbeat_stop, heartbeat_thread = start_cold_start_heartbeat(
                record_path=record_path,
                worker_token=worker_token,
                notification_callback=lambda payload: callbacks.append(dict(payload)),
                notification_interval_seconds=0.01,
            )
            try:
                self.assertTrue(refresh_entered.wait(timeout=3.0))
                running = read_executor_record(record_path) or {}
                stopped_heartbeat = str(running.get("heartbeat_at") or "")
                write_executor_record(
                    record_path,
                    {
                        **running,
                        "state": "stop_requested",
                        "stop_requested_at": "deterministic-race",
                    },
                )
                release_refresh.set()
                heartbeat_thread.join(timeout=3.0)

                final_record = read_executor_record(record_path) or {}
                self.assertFalse(heartbeat_thread.is_alive())
                self.assertEqual(final_record["state"], "stop_requested")
                self.assertEqual(
                    str(final_record.get("heartbeat_at") or ""),
                    stopped_heartbeat,
                )
                self.assertEqual(callbacks, [])
            finally:
                release_refresh.set()
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=2.0)

    def test_heartbeat_notification_is_throttled_async_and_stops_with_executor(self) -> None:
        cold_start_id = "notification-heartbeat-run"
        target = {
            "platform": "feishu",
            "chat_id": "dynamic-heartbeat-chat",
            "thread_id": "dynamic-heartbeat-thread",
        }
        record_path, worker_token = reserve_cold_start_background(
            configuration_id="notification-heartbeat-configuration",
            cold_start_id=cold_start_id,
            startup_root=self.executor_root,
            notification_target=target,
        )
        activate_cold_start_background(
            record_path=record_path,
            worker_token=worker_token,
            run_model="isolated/model-a",
        )
        calls: list[dict[str, object]] = []
        callback_started = threading.Event()
        release_callback = threading.Event()

        def slow_callback(payload: dict[str, object]) -> None:
            calls.append(dict(payload))
            callback_started.set()
            release_callback.wait(timeout=5.0)

        heartbeat_stop, heartbeat_thread = start_cold_start_heartbeat(
            record_path=record_path,
            worker_token=worker_token,
            notification_callback=slow_callback,
            notification_interval_seconds=0.05,
        )
        try:
            self.assertTrue(callback_started.wait(timeout=3.0))
            first_heartbeat = str(
                (read_executor_record(record_path) or {}).get("heartbeat_at") or ""
            )
            current_heartbeat = first_heartbeat
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                current_heartbeat = str(
                    (read_executor_record(record_path) or {}).get("heartbeat_at") or ""
                )
                if current_heartbeat and current_heartbeat != first_heartbeat:
                    break
                time.sleep(0.05)
            self.assertNotEqual(current_heartbeat, first_heartbeat)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event"], "cold_start_heartbeat")
            self.assertEqual(calls[0]["cold_start_id"], cold_start_id)
            self.assertEqual(calls[0]["notification_target"], target)

            record = read_executor_record(record_path) or {}
            stopped_heartbeat = str(record.get("heartbeat_at") or "")
            write_executor_record(
                record_path,
                {**record, "state": "stop_requested"},
            )
            release_callback.set()
            time.sleep(1.2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                str((read_executor_record(record_path) or {}).get("heartbeat_at") or ""),
                stopped_heartbeat,
            )
        finally:
            release_callback.set()
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
