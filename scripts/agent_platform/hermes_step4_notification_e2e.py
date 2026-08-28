"""Safe Step 4 notification-chain E2E fixture.

This module deliberately creates no formal cold-start row and never builds a
model, collector, or business executor.  It exercises only the existing
executor-record guard, notification coordinator, heartbeat callback, and
Hermes-native Feishu sender with the target captured by the current Hermes
request.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
import uuid

from scripts.agent_platform.cold_start_background_control import (
    start_cold_start_heartbeat,
    write_executor_record,
)
from scripts.agent_platform.hermes_cold_start_notifications import (
    ColdStartBackgroundNotifier,
    USER_HEARTBEAT_INTERVAL_SECONDS,
)
from scripts.agent_platform.hermes_native_feishu_outbound import (
    send_hermes_native_feishu_message,
)


TEST_MARKER = "[STEP4-E2E-TEST]"


class Step4NotificationE2ETestError(RuntimeError):
    """The isolated notification-chain check could not complete."""


def _normalize_target(value: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise Step4NotificationE2ETestError(
            "the current Hermes request did not provide a notification target"
        )
    platform = str(value.get("platform") or "").strip().lower()
    chat_id = str(value.get("chat_id") or "").strip()
    thread_id = str(value.get("thread_id") or "").strip()
    if platform != "feishu" or not chat_id:
        raise Step4NotificationE2ETestError(
            "the current Hermes request did not provide a dynamic Feishu chat_id"
        )
    target = {"platform": "feishu", "chat_id": chat_id}
    if thread_id:
        target["thread_id"] = thread_id
    return target


def run_step4_notification_e2e_test(
    *,
    notification_target: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Send exactly phase, heartbeat, and final test messages to one target.

    The target must come from the current Hermes request.  The temporary
    executor record is runtime-only and is removed before this function
    returns; no formal database or business entrypoint is touched.
    """

    target = _normalize_target(notification_target)
    cold_start_id = f"step4-e2e-test-{uuid.uuid4().hex}"
    worker_token = uuid.uuid4().hex
    sent_kinds: list[str] = []
    heartbeat_callback_payload: dict[str, Any] = {}
    heartbeat_done = threading.Event()
    callback_errors: list[str] = []
    clock_started = time.monotonic()

    def test_clock() -> float:
        # The real coordinator's five-minute rule is preserved; the isolated
        # fixture advances only its injected clock so the E2E takes seconds.
        return (time.monotonic() - clock_started) * USER_HEARTBEAT_INTERVAL_SECONDS

    def sender(
        *,
        target: Mapping[str, Any],
        message: str,
        kind: str,
        cold_start_id: str,
    ) -> dict[str, Any]:
        if cold_start_id != cold_start_id_value:
            raise Step4NotificationE2ETestError("notification run identity changed")
        if dict(target) != target_value:
            raise Step4NotificationE2ETestError("notification target changed mid-run")
        sent_kinds.append(kind)
        return send_hermes_native_feishu_message(
            chat_id=str(target["chat_id"]),
            thread_id=str(target.get("thread_id") or "") or None,
            message=f"{TEST_MARKER} {message}",
            timeout_seconds=8.0,
        )

    target_value = dict(target)
    cold_start_id_value = cold_start_id

    with tempfile.TemporaryDirectory(prefix="step4-e2e-") as temporary:
        record_path = Path(temporary) / "executor.json"
        write_executor_record(
            record_path,
            {
                "state": "running",
                "configuration_id": "step4-e2e-test-configuration",
                "cold_start_id": cold_start_id,
                "worker_token": worker_token,
                "pid": None,
                "heartbeat_at": "",
                "notification_target": target,
            },
        )
        notifier = ColdStartBackgroundNotifier(
            cold_start_id=cold_start_id,
            record_path=record_path,
            worker_token=worker_token,
            notification_target=target,
            sender=sender,
            clock=test_clock,
        )

        if not notifier.progress(
            {"phase": "historical_collection", "detail": "阶段通知"}
        ):
            raise Step4NotificationE2ETestError("phase notification was not delivered")

        def heartbeat_callback(payload: dict[str, Any]) -> None:
            heartbeat_callback_payload.update(payload)
            if payload.get("notification_target") != target_value:
                callback_errors.append("heartbeat target was not the captured target")
            if not notifier.heartbeat():
                callback_errors.append("heartbeat notification was not delivered")
            heartbeat_done.set()

        heartbeat_stop, heartbeat_thread = start_cold_start_heartbeat(
            record_path=record_path,
            worker_token=worker_token,
            notification_callback=heartbeat_callback,
            notification_interval_seconds=1.0,
        )
        try:
            if not heartbeat_done.wait(timeout=5.0):
                raise Step4NotificationE2ETestError(
                    "heartbeat callback did not run within the isolated E2E window"
                )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)

        if callback_errors:
            raise Step4NotificationE2ETestError("; ".join(callback_errors))
        if not notifier.final("completed"):
            raise Step4NotificationE2ETestError("final notification was not delivered")

    expected = ["progress", "heartbeat", "completed"]
    if sent_kinds != expected:
        raise Step4NotificationE2ETestError(
            f"notification order was {sent_kinds!r}, expected {expected!r}"
        )
    return {
        "status": "passed",
        "marker": TEST_MARKER,
        "captured_current_session_target": True,
        "thread_target_present": "thread_id" in target,
        "notifications_sent": expected,
        "formal_cold_start_created": False,
        "model_called": False,
        "mediacrawler_called": False,
        "formal_business_data_written": False,
    }
