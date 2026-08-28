"""Small per-run coordinator for cold-start Hermes notifications.

The coordinator owns no business state and never polls cold-start status.  It
only accepts already-produced execution events, checks the existing executor
record, and forwards a small set of user-visible messages to an injected
sender.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
import threading
import time
from typing import Any

from scripts.agent_platform.cold_start_background_control import (
    read_executor_record,
)


USER_HEARTBEAT_INTERVAL_SECONDS = 300.0

_BLOCKED_EXECUTOR_STATES = frozenset({"stop_requested", "stopped"})
_RUNNING_EXECUTOR_STATES = frozenset({"starting", "running"})

_STEP_LABELS = {
    "historical_material": "历史材料采集",
    "high_signal_identification": "基线计算和高信号筛选",
    "transcripts_and_comments": "爆款备料、评论和转写",
    "breakdown": "爆款拆解",
    "tag_candidates": "标签候选提取",
}

_PHASE_LABELS = {
    "historical_collection": "历史材料采集",
    "baseline_calculation": "账号基线计算",
    "hit_filtering": "历史爆款筛选",
    "hit_material_preparation": "爆款备料、评论和转写",
    "hit_breakdown": "爆款拆解",
    "tag_candidate_extraction": "标签候选提取",
}

_MEANINGFUL_EVENTS = frozenset({
    "registration_step_started",
    "registration_step_completed",
    "registration_history_insufficient",
    "phase_summary",
    "breakdown_started",
    "resume_summary",
    "tag_input_ready",
    "tag_library_generated",
    "tag_library_generation_failed",
    "content_type_candidates_ready_for_review",
    "content_type_candidate_generation_failed",
    "domain_boundary_candidates_ready_for_review",
    "domain_boundary_candidate_generation_failed",
})


class ColdStartBackgroundNotifier:
    """Deduplicate and throttle notifications for one executor and one target."""

    def __init__(
        self,
        *,
        cold_start_id: str,
        record_path: Path,
        worker_token: str,
        notification_target: Mapping[str, Any] | None,
        sender: Callable[..., Any],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cold_start_id = str(cold_start_id or "").strip()
        self.record_path = Path(record_path)
        self.worker_token = str(worker_token or "").strip()
        self.sender = sender
        self.clock = clock
        self.notification_target = self._normalize_target(notification_target)
        self._seen_progress: set[tuple[str, ...]] = set()
        self._terminal_status_sent: str | None = None
        self._current_registration: tuple[str, str] | None = None
        self._last_visible_at = float(self.clock())
        self._lock = threading.Lock()
        self._delivery_lock = threading.Lock()

    @staticmethod
    def _normalize_target(
        target: Mapping[str, Any] | None,
    ) -> dict[str, str] | None:
        if not isinstance(target, Mapping):
            return None
        platform = str(target.get("platform") or "").strip().lower()
        chat_id = str(target.get("chat_id") or "").strip()
        if platform != "feishu" or not chat_id:
            return None
        normalized = {"platform": "feishu", "chat_id": chat_id}
        thread_id = str(target.get("thread_id") or "").strip()
        if thread_id:
            normalized["thread_id"] = thread_id
        return normalized

    def _executor_allows(self, *, running_only: bool) -> bool:
        if not self.cold_start_id or not self.worker_token:
            return False
        if self.notification_target is None:
            return False
        record = read_executor_record(self.record_path)
        if record is None:
            return False
        if str(record.get("cold_start_id") or "") != self.cold_start_id:
            return False
        if str(record.get("worker_token") or "") != self.worker_token:
            return False
        state = str(record.get("state") or "")
        if state in _BLOCKED_EXECUTOR_STATES:
            return False
        if running_only and state not in _RUNNING_EXECUTOR_STATES:
            return False
        return True

    def _deliver(
        self,
        *,
        message: str,
        kind: str,
        running_only: bool,
        terminal: bool = False,
    ) -> bool:
        # One local lane keeps outbound messages ordered for this executor.
        with self._delivery_lock:
            with self._lock:
                if self._terminal_status_sent is not None:
                    return False
            # Re-read immediately before delivery so a stop request observed
            # after event normalization still suppresses the outbound message.
            if not self._executor_allows(running_only=running_only):
                return False
            try:
                self.sender(
                    target=dict(self.notification_target or {}),
                    message=message,
                    kind=kind,
                    cold_start_id=self.cold_start_id,
                )
            except Exception:
                # Delivery failure must not turn successful business work into
                # a failed cold-start run. A later business event remains
                # eligible for its own notification attempt.
                return False
            if terminal:
                with self._lock:
                    self._terminal_status_sent = kind
                    self._last_visible_at = float(self.clock())
            return True

    @staticmethod
    def _registration_label(payload: Mapping[str, Any]) -> str:
        display_name = str(payload.get("account_name") or "").strip()
        registration_id = str(payload.get("registration_id") or "").strip()
        return display_name or registration_id or "当前账号"

    @staticmethod
    def _registration_key(payload: Mapping[str, Any]) -> str:
        registration_id = str(payload.get("registration_id") or "").strip()
        display_name = str(payload.get("account_name") or "").strip()
        return registration_id or display_name or "current-account"

    @staticmethod
    def _reason(payload: Mapping[str, Any]) -> str:
        return str(payload.get("reason") or "").strip()

    def _meaningful_progress(
        self, payload: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], str] | None:
        event = str(payload.get("event") or "").strip()
        if event:
            if event not in _MEANINGFUL_EVENTS:
                return None
            registration = self._registration_label(payload)
            registration_key = self._registration_key(payload)
            step_name = str(payload.get("step_name") or "").strip()
            step_label = _STEP_LABELS.get(step_name, step_name or "当前阶段")

            if event == "registration_step_started":
                if step_name == "breakdown":
                    return None
                return (event, registration_key, step_name), (
                    f"冷启动进度：{registration}开始{step_label}。"
                )
            if event == "registration_step_completed":
                if step_name == "breakdown":
                    return None
                return (event, registration_key, step_name), (
                    f"冷启动进度：{registration}已完成{step_label}。"
                )
            if event == "registration_history_insufficient":
                return (event, registration_key), (
                    f"冷启动进度：{registration}的历史材料不足，等待人工处理。"
                )
            if event == "phase_summary":
                phase = str(payload.get("phase") or "").strip()
                summary = payload.get("summary")
                if not isinstance(summary, Mapping):
                    return None
                message = self._phase_summary_message(
                    phase=phase,
                    summary=summary,
                )
                if not message:
                    return None
                return (event, phase), message
            if event == "breakdown_started":
                total = int(payload.get("total_items") or 0)
                accounts = int(payload.get("account_total") or 0)
                return (event, str(total), str(accounts)), (
                    "冷启动进度：\n【内容拆解开始】\n"
                    f"待拆解：{total}条\n"
                    f"涉及账号：{accounts}个"
                )
            if event == "resume_summary":
                phase = str(payload.get("phase") or "当前阶段").strip() or "当前阶段"
                total = int(payload.get("total_items") or 0)
                completed = int(payload.get("completed_items") or 0)
                pending = int(payload.get("pending_items") or max(total - completed, 0))
                return (event, phase, str(total), str(completed), str(pending)), (
                    "冷启动进度：\n【冷启动已恢复】\n"
                    f"当前阶段：{phase}\n"
                    f"总任务：{total}条\n"
                    f"已完成：{completed}条\n"
                    f"本次待处理：{pending}条\n"
                    "已完成项目将直接跳过"
                )
            if event == "tag_input_ready":
                completed = str(payload.get("selection_completed_count") or "").strip()
                total = str(payload.get("selection_total_count") or "").strip()
                count_text = f"（{completed}/{total}）" if completed and total else ""
                return (event, completed, total), (
                    f"冷启动进度：高信号筛选已完成{count_text}，标签材料已就绪。"
                )
            if event == "tag_library_generated":
                return (event,), "冷启动进度：标签候选已生成，等待人工确认。"
            if event == "content_type_candidates_ready_for_review":
                return (event,), "冷启动进度：内容类型候选已生成，等待人工确认。"
            if event == "domain_boundary_candidates_ready_for_review":
                return (event,), "冷启动进度：生产边界候选已生成，等待人工确认。"

            failure_labels = {
                "tag_library_generation_failed": "标签候选生成失败",
                "content_type_candidate_generation_failed": "内容类型候选生成失败",
                "domain_boundary_candidate_generation_failed": "生产边界候选生成失败",
            }
            reason = self._reason(payload)
            detail = f"：{reason}" if reason else ""
            return (event, reason), f"冷启动进度：{failure_labels[event]}{detail}。"

        phase = str(payload.get("phase") or "").strip()
        if phase not in _PHASE_LABELS:
            return None
        registration = self._registration_label(payload)
        registration_key = self._registration_key(payload)
        detail = str(payload.get("detail") or "").strip()
        message = detail or f"{registration}正在进行{_PHASE_LABELS[phase]}"
        detail_key = ""
        if phase == "hit_breakdown":
            if "总" in detail and "本次处理" in detail:
                detail_key = "breakdown-start"
            elif "本次" in detail and "累计" in detail:
                detail_key = "breakdown-end"
        return ("phase", phase, registration_key, detail_key), f"冷启动进度：{message.rstrip('。')}。"

    @staticmethod
    def _phase_summary_message(
        *, phase: str, summary: Mapping[str, Any]
    ) -> str:
        """Render one aggregate result from the current run's formal data."""

        if phase == "historical_collection":
            success = int(summary.get("success_accounts") or 0)
            total = int(summary.get("account_total") or 0)
            completed = bool(summary.get("stage_completed", success == total))
            lines = [
                "【历史采集完成】" if completed else "【历史采集未完成】",
                f"对标账号：{success}/{total}",
                f"历史作品：共 {int(summary.get('historical_items') or 0)} 条",
            ]
            failed = int(summary.get("failed_accounts") or 0)
            if failed:
                lines.append(f"失败：{failed}")
            if completed:
                lines.append("下一阶段：基线计算和高信号筛选")
            return "冷启动进度：" + "\n".join(lines)

        if phase == "baseline_high_signal":
            completed_accounts = int(summary.get("completed_accounts") or 0)
            account_total = int(summary.get("account_total") or 0)
            completed = bool(summary.get("stage_completed", completed_accounts == account_total))
            lines = [
                "【基线计算与爆款筛选完成】" if completed else "【基线计算与爆款筛选未完成】",
                f"对标账号：{int(summary.get('completed_accounts') or 0)}/{int(summary.get('account_total') or 0)}",
                f"参与基线计算：{int(summary.get('historical_items') or 0)} 条历史作品",
                f"筛出高信号作品：{int(summary.get('high_signal_items') or 0)} 条",
            ]
            if completed:
                lines.append("下一阶段：备料")
            return "冷启动进度：" + "\n".join(lines)

        if phase == "preparation":
            high_signal_items = int(summary.get("high_signal_items") or 0)
            prepared_items = int(summary.get("prepared_items") or 0)
            completed = bool(summary.get("stage_completed", prepared_items == high_signal_items))
            lines = [
                "【备料完成】" if completed else "【备料未完成】",
                f"高信号作品：{int(summary.get('high_signal_items') or 0)} 条",
                f"完成备料：{int(summary.get('prepared_items') or 0)} 条",
                f"详情：{int(summary.get('detail_items') or 0)}",
                f"评论：{int(summary.get('comment_items') or 0)}",
                f"媒体：{int(summary.get('media_items') or 0)}",
                f"转写：{int(summary.get('transcript_items') or 0)}",
            ]
            if completed:
                lines.append("下一阶段：内容拆解")
            return "冷启动进度：" + "\n".join(lines)

        if phase == "breakdown":
            success = int(summary.get("success_items") or 0)
            failed = int(summary.get("failed_items") or 0)
            pending = int(summary.get("pending_items") or 0)
            total = int(summary.get("total_items") or success + failed + pending)
            lines = [
                "【内容拆解完成】" if not failed and not pending else "【内容拆解未完成】",
                f"总计：{total} 条",
                f"完成：{success} 条",
            ]
            if failed:
                lines.append(f"失败：{failed} 条")
            if pending:
                lines.append(f"剩余：{pending} 条")
            return "冷启动进度：" + "\n".join(lines)

        return ""

    def progress(self, payload: Mapping[str, Any]) -> bool:
        """Send one meaningful, not-yet-seen progress transition."""
        if not isinstance(payload, Mapping):
            return False
        if not self._executor_allows(running_only=True):
            return False
        normalized_payload = dict(payload)
        event = str(payload.get("event") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        account_name = str(payload.get("account_name") or "").strip()
        registration_id = str(payload.get("registration_id") or "").strip()
        explicit_registration = None
        if account_name or registration_id:
            explicit_registration = (
                registration_id or account_name,
                account_name or registration_id,
            )
        with self._lock:
            if event == "registration_step_started" and explicit_registration:
                self._current_registration = explicit_registration
            phase_registration = explicit_registration or self._current_registration
        # Only executor phases inherit the active account. Global lifecycle
        # events remain global even while an account is active.
        if not event and phase in _PHASE_LABELS and phase_registration:
            registration_key, registration_label = phase_registration
            normalized_payload.setdefault("account_name", registration_label)
            normalized_payload.setdefault("registration_id", registration_key)
            detail = str(normalized_payload.get("detail") or "").strip()
            if detail:
                normalized_payload["detail"] = f"{registration_label}\uFF1A{detail}"
        normalized = self._meaningful_progress(normalized_payload)
        if normalized is None:
            return False
        key, message = normalized
        with self._lock:
            if (
                key in self._seen_progress
                or self._terminal_status_sent is not None
            ):
                return False
            self._seen_progress.add(key)
            self._last_visible_at = float(self.clock())
        return self._deliver(message=message, kind="progress", running_only=True)

    def heartbeat(self) -> bool:
        """Send a user heartbeat only after five minutes without visible progress."""
        if not self._executor_allows(running_only=True):
            return False
        now = float(self.clock())
        with self._lock:
            if self._terminal_status_sent is not None:
                return False
            if now - self._last_visible_at < USER_HEARTBEAT_INTERVAL_SECONDS:
                return False
            self._last_visible_at = now
        return self._deliver(
            message="冷启动仍在后台运行，当前执行器状态正常。",
            kind="heartbeat",
            running_only=True,
        )

    def final(self, status: str, reason: str = "") -> bool:
        """Send at most one terminal result for this notifier instance."""
        terminal = str(status or "").strip().lower()
        if terminal not in {"completed", "failed"}:
            raise ValueError("cold-start notification final status must be completed or failed")
        if not self._executor_allows(running_only=False):
            return False
        with self._lock:
            if self._terminal_status_sent is not None:
                return False
        if terminal == "completed":
            message = "冷启动后台任务已完成。"
        else:
            detail = str(reason or "").strip()
            message = f"冷启动后台任务失败：{detail or '未提供失败原因'}。"
        return self._deliver(
            message=message,
            kind=terminal,
            running_only=False,
            terminal=True,
        )
