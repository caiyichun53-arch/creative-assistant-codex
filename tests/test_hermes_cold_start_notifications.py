from __future__ import annotations

import json
import threading
from pathlib import Path
import tempfile
import unittest

from scripts.agent_platform.hermes_cold_start_notifications import (
    ColdStartBackgroundNotifier,
    USER_HEARTBEAT_INTERVAL_SECONDS,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ColdStartBackgroundNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sent: list[dict[str, object]] = []

    def record(
        self,
        *,
        name: str = "executor.json",
        cold_start_id: str = "cold-start-one",
        worker_token: str = "worker-one",
        state: str = "running",
    ) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps({
                "cold_start_id": cold_start_id,
                "worker_token": worker_token,
                "state": state,
            }),
            encoding="utf-8",
        )
        return path

    def notifier(
        self,
        *,
        record_path: Path,
        cold_start_id: str = "cold-start-one",
        worker_token: str = "worker-one",
        target: dict[str, str] | None = None,
        clock: FakeClock | None = None,
    ) -> ColdStartBackgroundNotifier:
        return ColdStartBackgroundNotifier(
            cold_start_id=cold_start_id,
            record_path=record_path,
            worker_token=worker_token,
            notification_target=target or {
                "platform": "feishu",
                "chat_id": "oc_chat_one",
                "thread_id": "omt_thread_one",
            },
            sender=lambda **payload: self.sent.append(payload),
            clock=clock or FakeClock(),
        )

    def test_progress_accepts_meaningful_transition_and_deduplicates_it(self) -> None:
        notifier = self.notifier(record_path=self.record())
        payload = {
            "event": "registration_step_started",
            "registration_id": "registration-1",
            "account_name": "账号甲",
            "step_name": "historical_material",
        }

        self.assertTrue(notifier.progress(payload))
        self.assertFalse(notifier.progress(payload))
        self.assertFalse(notifier.progress({"event": "database_row_written"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["kind"], "progress")
        self.assertEqual(self.sent[0]["cold_start_id"], "cold-start-one")
        self.assertEqual(
            self.sent[0]["target"],
            {
                "platform": "feishu",
                "chat_id": "oc_chat_one",
                "thread_id": "omt_thread_one",
            },
        )
        self.assertIn("账号甲开始历史材料采集", str(self.sent[0]["message"]))

    def test_phase_progress_sends_only_first_visible_change_per_account_phase(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.progress({
            "phase": "hit_material_preparation",
            "registration_id": "registration-1",
            "detail": "正在为第1条爆款准备详情、评论、音频和转写",
        }))
        self.assertFalse(notifier.progress({
            "phase": "hit_material_preparation",
            "registration_id": "registration-1",
            "detail": "正在为第2条爆款准备详情、评论、音频和转写",
        }))
        self.assertTrue(notifier.progress({
            "phase": "hit_material_preparation",
            "registration_id": "registration-2",
            "detail": "正在为第1条爆款准备详情、评论、音频和转写",
        }))
        self.assertFalse(notifier.progress({
            "phase": "external_runtime",
            "detail": "内部子进程仍有输出",
        }))
        self.assertEqual(len(self.sent), 2)

    def test_accountless_phase_inherits_unique_latest_registration(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.progress({
            "event": "registration_step_started",
            "registration_id": "registration-A",
            "account_name": "same-name",
            "step_name": "historical_material",
        }))
        self.assertTrue(notifier.progress({
            "phase": "hit_filtering",
            "detail": "phase-A",
        }))
        self.assertFalse(notifier.progress({
            "phase": "hit_filtering",
            "detail": "phase-A-duplicate",
        }))
        self.assertTrue(notifier.progress({
            "event": "registration_step_started",
            "registration_id": "registration-B",
            "account_name": "same-name",
            "step_name": "historical_material",
        }))
        self.assertTrue(notifier.progress({
            "phase": "hit_filtering",
            "detail": "phase-B",
        }))
        self.assertTrue(notifier.progress({
            "event": "tag_input_ready",
            "selection_completed_count": 20,
            "selection_total_count": 20,
        }))

        self.assertEqual(len(self.sent), 5)
        self.assertIn("same-name", str(self.sent[1]["message"]))
        self.assertIn("phase-A", str(self.sent[1]["message"]))
        self.assertIn("same-name", str(self.sent[3]["message"]))
        self.assertIn("phase-B", str(self.sent[3]["message"]))
        self.assertNotIn("same-name", str(self.sent[4]["message"]))

    def test_phase_summary_messages_use_formal_counts_and_deduplicate(self) -> None:
        notifier = self.notifier(record_path=self.record())
        summaries = [
            (
                "historical_collection",
                {
                    "account_total": 20,
                    "success_accounts": 20,
                    "historical_items": 317,
                    "failed_accounts": 0,
                },
            ),
            (
                "baseline_high_signal",
                {
                    "account_total": 20,
                    "completed_accounts": 20,
                    "historical_items": 317,
                    "high_signal_items": 12,
                },
            ),
            (
                "preparation",
                {
                    "high_signal_items": 12,
                    "prepared_items": 12,
                    "detail_items": 12,
                    "comment_items": 12,
                    "media_items": 12,
                    "transcript_items": 12,
                },
            ),
            (
                "breakdown",
                {"pending_items": 0, "success_items": 12, "failed_items": 0},
            ),
        ]
        for phase, summary in summaries:
            self.assertTrue(notifier.progress({
                "event": "phase_summary",
                "phase": phase,
                "summary": summary,
            }))
        self.assertFalse(notifier.progress({
            "event": "phase_summary",
            "phase": "baseline_high_signal",
            "summary": summaries[1][1],
        }))
        self.assertEqual([item["kind"] for item in self.sent], ["progress"] * 4)
        messages = [str(item["message"]) for item in self.sent]
        self.assertIn("历史作品：共 317 条", messages[0])
        self.assertIn("筛出高信号作品：12 条", messages[1])
        self.assertIn("转写：12", messages[2])
        self.assertIn("总计：12 条", messages[3])
        self.assertIn("完成：12 条", messages[3])
        self.assertNotIn("失败：0", messages[0])
        self.assertNotIn("失败", messages[3])

    def test_resume_summary_reports_only_current_work(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.progress({
            "event": "resume_summary",
            "phase": "内容拆解",
            "total_items": 127,
            "completed_items": 60,
            "pending_items": 67,
        }))
        message = str(self.sent[0]["message"])
        self.assertIn("总任务：127条", message)
        self.assertIn("已完成：60条", message)
        self.assertIn("本次待处理：67条", message)
        self.assertIn("已完成项目将直接跳过", message)

    def test_breakdown_account_messages_use_pending_work_and_allow_end_summary(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.progress({
            "phase": "hit_breakdown",
            "registration_id": "registration-1",
            "account_name": "百科全吃",
            "detail": "总12条，已完成8条，本次处理4条",
        }))
        self.assertTrue(notifier.progress({
            "phase": "hit_breakdown",
            "registration_id": "registration-1",
            "account_name": "百科全吃",
            "detail": "本次完成4条，累计12/12",
        }))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("总12条，已完成8条，本次处理4条", str(self.sent[0]["message"]))
        self.assertIn("本次完成4条，累计12/12", str(self.sent[1]["message"]))

    def test_fresh_breakdown_has_one_global_start_and_no_generic_duplicate(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.progress({
            "event": "breakdown_started",
            "total_items": 127,
            "account_total": 20,
        }))
        self.assertFalse(notifier.progress({
            "event": "registration_step_started",
            "registration_id": "registration-1",
            "account_name": "百科全吃",
            "step_name": "breakdown",
        }))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("待拆解：127条", str(self.sent[0]["message"]))
        self.assertIn("涉及账号：20个", str(self.sent[0]["message"]))

    def test_heartbeat_waits_five_minutes_and_progress_resets_the_window(self) -> None:
        clock = FakeClock()
        notifier = self.notifier(record_path=self.record(), clock=clock)

        clock.advance(USER_HEARTBEAT_INTERVAL_SECONDS - 1)
        self.assertFalse(notifier.heartbeat())
        clock.advance(1)
        self.assertTrue(notifier.heartbeat())
        self.assertFalse(notifier.heartbeat())

        clock.advance(10)
        self.assertTrue(notifier.progress({
            "event": "tag_input_ready",
            "selection_completed_count": 20,
            "selection_total_count": 20,
        }))
        clock.advance(USER_HEARTBEAT_INTERVAL_SECONDS - 1)
        self.assertFalse(notifier.heartbeat())
        clock.advance(1)
        self.assertTrue(notifier.heartbeat())
        self.assertEqual(
            [item["kind"] for item in self.sent],
            ["heartbeat", "progress", "heartbeat"],
        )

    def test_in_flight_heartbeat_finishes_before_terminal_delivery(self) -> None:
        clock = FakeClock()
        heartbeat_sender_entered = threading.Event()
        release_heartbeat = threading.Event()
        terminal_sender_entered = threading.Event()
        arrivals: list[str] = []
        results: dict[str, bool] = {}

        def blocking_sender(**payload: object) -> None:
            kind = str(payload["kind"])
            if kind == "heartbeat":
                heartbeat_sender_entered.set()
                release_heartbeat.wait(timeout=2.0)
            if kind == "completed":
                terminal_sender_entered.set()
            arrivals.append(kind)

        notifier = ColdStartBackgroundNotifier(
            cold_start_id="cold-start-one",
            record_path=self.record(),
            worker_token="worker-one",
            notification_target={"platform": "feishu", "chat_id": "oc_one"},
            sender=blocking_sender,
            clock=clock,
        )
        clock.advance(USER_HEARTBEAT_INTERVAL_SECONDS)

        def send_heartbeat() -> None:
            results["heartbeat"] = notifier.heartbeat()

        def send_final() -> None:
            results["final"] = notifier.final("completed")

        heartbeat_thread = threading.Thread(target=send_heartbeat)
        heartbeat_thread.start()
        self.assertTrue(heartbeat_sender_entered.wait(timeout=2.0))
        final_thread = threading.Thread(target=send_final)
        final_thread.start()

        with notifier._lock:
            self.assertIsNone(notifier._terminal_status_sent)
        self.assertFalse(terminal_sender_entered.wait(timeout=0.05))
        release_heartbeat.set()
        heartbeat_thread.join(timeout=2.0)
        final_thread.join(timeout=2.0)

        self.assertFalse(heartbeat_thread.is_alive())
        self.assertFalse(final_thread.is_alive())
        self.assertEqual(results, {"heartbeat": True, "final": True})
        self.assertEqual(arrivals, ["heartbeat", "completed"])

    def test_final_is_one_terminal_message_even_if_called_again(self) -> None:
        notifier = self.notifier(record_path=self.record())

        self.assertTrue(notifier.final("completed"))
        self.assertFalse(notifier.final("completed"))
        self.assertFalse(notifier.final("failed", "late contradictory failure"))
        self.assertFalse(notifier.progress({
            "event": "tag_library_generated",
        }))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["kind"], "completed")

    def test_failed_final_is_allowed_after_executor_record_is_failed(self) -> None:
        notifier = self.notifier(record_path=self.record(state="failed"))

        self.assertTrue(notifier.final("failed", "模型调用失败"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["kind"], "failed")
        self.assertIn("模型调用失败", str(self.sent[0]["message"]))

    def test_stop_states_and_changed_worker_token_suppress_all_notifications(self) -> None:
        path = self.record()
        notifier = self.notifier(record_path=path, worker_token="wrong-token")
        self.assertFalse(notifier.progress({"event": "tag_library_generated"}))
        self.assertFalse(notifier.heartbeat())
        self.assertFalse(notifier.final("failed", "ignored"))

        for state in ("stop_requested", "stopped"):
            path = self.record(name=f"{state}.json", state=state)
            notifier = self.notifier(record_path=path)
            self.assertFalse(notifier.progress({"event": "tag_library_generated"}))
            self.assertFalse(notifier.heartbeat())
            self.assertFalse(notifier.final("completed"))
        self.assertEqual(self.sent, [])

    def test_instances_keep_run_and_target_isolated(self) -> None:
        target_one = {"platform": "feishu", "chat_id": "oc_one"}
        target_two = {
            "platform": "feishu",
            "chat_id": "oc_two",
            "thread_id": "omt_two",
        }
        first = self.notifier(
            record_path=self.record(name="one.json"),
            target=target_one,
        )
        second = self.notifier(
            record_path=self.record(
                name="two.json",
                cold_start_id="cold-start-two",
                worker_token="worker-two",
            ),
            cold_start_id="cold-start-two",
            worker_token="worker-two",
            target=target_two,
        )
        target_one["chat_id"] = "mutated-after-construction"

        self.assertTrue(first.progress({"event": "tag_library_generated"}))
        self.assertTrue(second.progress({"event": "tag_library_generated"}))
        self.assertEqual(self.sent[0]["target"], {"platform": "feishu", "chat_id": "oc_one"})
        self.assertEqual(self.sent[0]["cold_start_id"], "cold-start-one")
        self.assertEqual(self.sent[1]["target"], target_two)
        self.assertEqual(self.sent[1]["cold_start_id"], "cold-start-two")

    def test_sender_failure_does_not_escape_or_suppress_a_later_event(self) -> None:
        attempts: list[dict[str, object]] = []

        def failing_sender(**payload: object) -> None:
            attempts.append(payload)
            raise RuntimeError("isolated outbound failure")

        notifier = ColdStartBackgroundNotifier(
            cold_start_id="cold-start-one",
            record_path=self.record(),
            worker_token="worker-one",
            notification_target={"platform": "feishu", "chat_id": "oc_one"},
            sender=failing_sender,
            clock=FakeClock(),
        )
        payload = {"event": "tag_library_generated"}

        self.assertFalse(notifier.progress(payload))
        self.assertFalse(notifier.progress(payload))
        self.assertFalse(notifier.progress({"event": "content_type_candidates_ready_for_review"}))
        self.assertEqual(len(attempts), 2)

    def test_failed_terminal_delivery_can_be_attempted_by_a_later_terminal_event(self) -> None:
        attempts: list[dict[str, object]] = []

        def fail_once(**payload: object) -> None:
            attempts.append(payload)
            if len(attempts) == 1:
                raise RuntimeError("isolated terminal outbound failure")

        notifier = ColdStartBackgroundNotifier(
            cold_start_id="cold-start-one",
            record_path=self.record(),
            worker_token="worker-one",
            notification_target={"platform": "feishu", "chat_id": "oc_one"},
            sender=fail_once,
            clock=FakeClock(),
        )

        self.assertFalse(notifier.final("failed", "首次发送失败"))
        self.assertTrue(notifier.final("failed", "第二次发送成功"))
        self.assertEqual(len(attempts), 2)

    def test_invalid_target_and_invalid_final_status_fail_closed(self) -> None:
        notifier = self.notifier(
            record_path=self.record(),
            target={"platform": "feishu", "chat_id": ""},
        )
        self.assertFalse(notifier.progress({"event": "tag_library_generated"}))
        with self.assertRaises(ValueError):
            notifier.final("waiting_human")


if __name__ == "__main__":
    unittest.main()
