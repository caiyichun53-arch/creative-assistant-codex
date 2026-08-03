"""Local visual business entry for guided cold-start configuration."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import socket
import subprocess
import sys
import threading
import time
import tempfile
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.agent_platform.daily_operations_runtime import DailyOperationsCoordinator
from scripts.core.external_adapters.windows_process import (
    SingleInstanceAlreadyRunning,
    single_instance_guard,
)
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.business_runtime_guard import (
    enforce_content_production_runtime_guard,
    enforce_daily_operations_runtime_guard,
    enforce_manual_exploration_runtime_guard,
    enforce_runtime_startup_guard,
)
from scripts.core.production.human_decision_entry import (
    FormalHumanDecisionCommand,
    HumanDecisionCommandService,
)
from scripts.core.production.stage0_content_core import (
    DAILY_PRIORITY_REPORT_LIMIT,
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1_competitor_registration import (
    CompetitorRegistrationService,
    build_configured_competitor_registration_executor,
    configured_first_registration_item_limit,
    run_test_only_competitor_breakdown_batch,
)
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    Stage1BDailyDiscoveryService,
    build_production_daily_discovery_gateway,
)
from scripts.core.production.stage1a_research_plan import (
    Stage1AResearchPlanService,
    build_research_plan_gateway,
)
from scripts.core.production.stage1c_content_pipeline import (
    Stage1CContentPipelineService,
    build_production_content_pipeline_gateway,
)
from scripts.core.production.stage1d_audio_production import (
    build_production_audio_service,
)
from scripts.core.production.publication_feedback import (
    decide_p7_review,
    prepare_p7_review,
    record_observation,
    register_publication,
)
from scripts.core.production.stage1_music_person_exploration import (
    build_music_audience_material_collector,
)
from scripts.core.production.stage4_knowledge_mirror import (
    ObsidianKnowledgeMirrorService,
)
from scripts.core.runtime.runtime_storage import runtime_path, runtime_storage_overview
from scripts.core.runtime.migrate_runtime_storage import (
    plan_relocation,
    relocate_active_runtime,
)
from scripts.agent_platform.workbench_assistant import (
    WorkbenchAssistant,
    restrict_workbench_state,
    summarize_workbench_state,
)
from scripts.core.model_gateway.codex_app_server_provider import (
    CodexAppServerProviderError,
)


ROOT = Path(__file__).resolve().parents[2]
UI_PATH = Path(__file__).with_name("cold_start_config_ui.html")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _console(message: str) -> None:
    if sys.stdout is None:
        return
    try:
        print(message, flush=True)
    except (OSError, ValueError):
        return


def _friendly_retry_reason(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    match = re.search(r"competitor material preparation failed for (\d+) item", text)
    if match:
        return f"爆款备料还有 {match.group(1)} 条未完成"
    match = re.search(r"competitor breakdown failed for (\d+) item", text)
    if match:
        return f"爆款拆解还有 {match.group(1)} 条未完成"
    if "timed out" in text.lower():
        return "模型或外部服务本次响应超时"
    if "account blocked" in text.lower():
        return "抖音当前暂时拒绝采集请求；系统已停止，等待用户明确要求继续"
    if "JSON" in type(exc).__name__ or "Expecting value" in text or "model JSON" in text:
        return "模型本次返回的结构无法使用"
    return text


def _competitor_breakdown_test_job_view(
    job: dict[str, Any],
    *,
    include_raw: bool,
) -> dict[str, Any]:
    """Expose one test receipt without accidentally returning every raw answer."""
    visible = {
        key: value
        for key, value in job.items()
        if key not in {"result"}
    }
    result = job.get("result")
    if not isinstance(result, dict):
        return visible
    outcomes: list[dict[str, Any]] = []
    for item in result.get("outcomes") or []:
        if not isinstance(item, dict):
            continue
        outcome = {
            key: value
            for key, value in item.items()
            if key not in {"raw_model_output"}
        }
        raw_answer = item.get("raw_model_output")
        if raw_answer is not None:
            outcome["raw_model_output_status"] = "available"
            if include_raw:
                outcome["raw_model_output"] = raw_answer
        correction = outcome.get("test_correction")
        if isinstance(correction, dict):
            correction_view = dict(correction)
            initial_failure = correction_view.get("initial_failure")
            if isinstance(initial_failure, dict):
                initial_view = {
                    key: value
                    for key, value in initial_failure.items()
                    if key != "raw_model_output"
                }
                if initial_failure.get("raw_model_output") is not None:
                    initial_view["raw_model_output_status"] = "available"
                    if include_raw:
                        initial_view["raw_model_output"] = initial_failure["raw_model_output"]
                correction_view["initial_failure"] = initial_view
            outcome["test_correction"] = correction_view
        outcomes.append(outcome)
    visible["result"] = {
        "kind": result.get("kind"),
        "test_id": result.get("test_id"),
        "formal_business_data_written": result.get("formal_business_data_written"),
        "automatic_retry": result.get("automatic_retry"),
        "test_single_correction_enabled": result.get("test_single_correction_enabled"),
        "model_delivery": result.get("model_delivery"),
        "outcomes": outcomes,
    }
    return visible


_REGISTRATION_STEP_PHASE = {
    "historical_material": "historical_collection",
    "high_signal_identification": "baseline_calculation",
    "transcripts_and_comments": "hit_material_preparation",
    "breakdown": "hit_breakdown",
    "tag_candidates": "tag_candidate_extraction",
    "awaiting_human_review": "registration_finalizing",
    "completed": "registration_completed",
}

_REGISTRATION_QUEUE_DETAIL = {
    "historical_material": "等待采集账号历史内容",
    "high_signal_identification": "等待计算基线与筛选爆款",
    "transcripts_and_comments": "等待爆款备料",
    "breakdown": "等待爆款拆解",
    "tag_candidates": "等待提取领域标签",
    "awaiting_human_review": "等待自动完成资料建库",
}

_DAILY_FORMAL_ACTIONS = {
    "run_daily_operations",
    "select_discovery_candidate",
    "review_two_week_tag_library",
    "record_manual_source",
    "collect_person_materials",
    "record_material",
    "complete_collection",
    "confirm_direction",
    "confirm_exploration_direction",
    "confirm_formal_topic",
}

_CONTENT_ENTRY_ACTIONS = {
    "select_discovery_candidate",
    "confirm_exploration_direction",
    "confirm_formal_topic",
}

class ColdStartConfigServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        db_path: Path,
        data_identity: str,
        config_dir: Path,
        daily_state_path: Path,
        daily_hour: int = 8,
        daily_minute: int = 0,
        daily_poll_seconds: float = 30,
        daily_max_items_per_account: int = 50,
        collection_block_cooldowns: tuple[float, ...] = (300.0, 1800.0, 7200.0, 21600.0),
        collection_risk_state_path: Path | None = None,
        start_daily_operations: bool = True,
    ):
        super().__init__(address, ColdStartConfigHandler)
        self.db_path = db_path
        self.data_identity = data_identity
        self.config_dir = config_dir
        self.challenges: dict[str, dict[str, Any]] = {}
        self.validated_sessions: dict[str, str] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.atomic_breakdown_jobs: dict[str, dict[str, Any]] = {}
        self.breakdown_test_jobs: dict[str, dict[str, Any]] = {}
        self.breakdown_backlog_jobs: dict[str, dict[str, Any]] = {}
        self.challenge_lock = threading.Lock()
        self.finalization_watchdog_stop = threading.Event()
        self.finalization_watchdog: threading.Thread | None = None
        self.recovery_thread: threading.Thread | None = None
        self.tag_library_lock = threading.Lock()
        self.collection_cooldown_until: datetime | None = None
        self.collection_block_cooldowns = collection_block_cooldowns
        self.collection_block_streak = 0
        self.collection_last_block_reason: str | None = None
        self.collection_risk_state_path = (
            collection_risk_state_path
            or daily_state_path.with_name("collection_protection_state.json")
        )
        self._load_collection_protection_state()
        self.pause_state_path = daily_state_path.with_name(
            "cold_start_pause_state.json"
        )
        self.pause_lock = threading.Lock()
        self.paused_jobs = self._load_pause_state()
        # Creator history and hit preparation must not block each other's
        # non-platform work.  MediaCrawler itself still serializes every
        # Douyin request against one retained browser profile and gives
        # creator-history requests priority between detail requests.
        self.history_collection_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cold-start-history",
        )
        self.material_preparation_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cold-start-material",
        )
        self.transition_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cold-start-transition")
        # Final registration copies already-retained material into the daily
        # tracking tables.  SQLite has one writer, so this must stay serial:
        # parallel finalization otherwise leaves several jobs looking active
        # while they are merely waiting on one another's write transaction.
        self.finalization_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="cold-start-finalization",
        )
        self.analysis_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cold-start-analysis")
        self.atomic_breakdown_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="competitor-breakdown",
        )
        self.breakdown_test_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="competitor-breakdown-test",
        )
        self.breakdown_backlog_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="competitor-breakdown-backlog",
        )
        self.manual_exploration_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="manual-exploration",
        )
        self.manual_exploration_jobs: dict[str, dict[str, Any]] = {}
        self.knowledge_mirror_pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="knowledge-mirror",
        )
        self.knowledge_mirror_lock = threading.Lock()
        self.knowledge_mirror_pending = False
        self.knowledge_mirror_running = False
        self.knowledge_mirror_reason = ""
        self.knowledge_mirror_status: dict[str, Any] = {
            "status": "idle",
            "reason": None,
            "updated_at": None,
            "error": None,
        }
        self.daily_operations = DailyOperationsCoordinator(
            db_path=db_path,
            data_identity=data_identity,
            state_path=daily_state_path,
            run_hour=daily_hour,
            run_minute=daily_minute,
            poll_seconds=daily_poll_seconds,
            max_items_per_account=daily_max_items_per_account,
            on_formal_change=self.schedule_knowledge_mirror,
        )
        if data_identity == "production" and start_daily_operations:
            self.daily_operations.start()
        self.storage_restart_requested = False

    def start_finalization_watchdog(self) -> None:
        """Keep the database-only registration finish step from displaying stale work forever.

        This is deliberately limited to registrations that have already completed
        every collection and analysis step.  Recovering such a task cannot invoke
        MediaCrawler or a model and therefore cannot create duplicate external work.
        """
        if self.finalization_watchdog is not None:
            return
        self.finalization_watchdog = threading.Thread(
            target=self._finalization_watchdog_loop,
            name="cold-start-finalization-watchdog",
            daemon=True,
        )
        self.finalization_watchdog.start()

    def start_recovery(self) -> None:
        """Resume existing formal work without delaying the local interface startup.

        Startup recovery may need to inspect a large retained corpus.  The web
        interface must remain available while that read-only scheduling pass is
        in progress; individual registration work is still dispatched through
        its normal guarded pools.
        """
        if self.recovery_thread is not None:
            return

        def recover() -> None:
            # The watchdog starts first so a delayed startup scan cannot leave
            # a database-only finalization without a visible job state.
            self.start_finalization_watchdog()
            resumed = self.resume_started_cold_starts()
            resumed_backlogs = self.resume_pending_breakdown_backlog_tasks()
            _console(
                "automatic cold-start resume: "
                f"{resumed['registration_count']} registrations and "
                f"{resumed_backlogs} formal breakdown backlog task(s)"
            )

        self.recovery_thread = threading.Thread(
            target=recover,
            name="cold-start-startup-recovery",
            daemon=True,
        )
        self.recovery_thread.start()

    def _finalization_watchdog_loop(self) -> None:
        while not self.finalization_watchdog_stop.is_set():
            try:
                self.recover_stalled_finalizations()
            except BaseException:
                # The next minute performs a fresh database check.  This monitor
                # must never turn an internal display recovery into an external retry.
                pass
            if self.finalization_watchdog_stop.wait(60):
                return

    def recover_stalled_finalizations(self) -> int:
        """Requeue only stale database-only registration finalizations."""
        core = Stage0ContentProductionCore.open(
            self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
        )
        try:
            rows = core.conn.execute(
                "SELECT registration.registration_id FROM stage0_competitor_registration registration "
                "JOIN stage0_cold_start_configuration configuration "
                "ON configuration.cold_start_id=registration.cold_start_id "
                "AND configuration.data_identity=registration.data_identity "
                "WHERE configuration.status='started' AND registration.data_identity=? "
                "AND registration.current_step='awaiting_human_review' "
                "AND registration.status='awaiting_human_review' "
                "ORDER BY registration.registration_id",
                (self.data_identity,),
            ).fetchall()
        finally:
            core.close()
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
        recovered = 0
        for row in rows:
            registration_id = str(row["registration_id"])
            with self.challenge_lock:
                current = dict(self.jobs.get(registration_id) or {})
                updated_text = str(current.get("updated_at") or "")
                try:
                    updated_at = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
                except ValueError:
                    updated_at = datetime.min.replace(tzinfo=timezone.utc)
                if current.get("status") in {"queued", "running"} and updated_at > stale_before:
                    continue
                self.jobs.pop(registration_id, None)
            self.schedule_registration(registration_id)
            recovered += 1
        return recovered

    def _load_collection_protection_state(self) -> None:
        path = self.collection_risk_state_path
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            streak = int(payload.get("block_streak") or 0)
            cooldown_text = str(payload.get("cooldown_until") or "").strip()
            cooldown_until = datetime.fromisoformat(cooldown_text) if cooldown_text else None
            if cooldown_until is not None and cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
            self.collection_block_streak = max(0, streak)
            self.collection_cooldown_until = cooldown_until
            self.collection_last_block_reason = str(payload.get("last_reason") or "").strip() or None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.collection_block_streak = 0
            self.collection_cooldown_until = None
            self.collection_last_block_reason = None

    def _load_pause_state(self) -> dict[str, dict[str, Any]]:
        if not self.pause_state_path.is_file():
            return {}
        try:
            value = json.loads(self.pause_state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return {
            str(job_id): dict(state)
            for job_id, state in value.items()
            if isinstance(job_id, str) and isinstance(state, dict)
        } if isinstance(value, dict) else {}

    def _save_pause_state(self) -> None:
        temporary = self.pause_state_path.with_suffix(
            self.pause_state_path.suffix + ".new"
        )
        self.pause_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(
                self.paused_jobs,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.pause_state_path)

    def _persist_paused_job(self, job_id: str, state: dict[str, Any]) -> None:
        with self.pause_lock:
            self.paused_jobs[job_id] = dict(state)
            self._save_pause_state()

    def _clear_paused_job(self, job_id: str) -> None:
        with self.pause_lock:
            if self.paused_jobs.pop(job_id, None) is not None:
                self._save_pause_state()

    def _save_collection_protection_state(self) -> None:
        path = self.collection_risk_state_path
        with self.challenge_lock:
            payload = {
                "block_streak": self.collection_block_streak,
                "cooldown_until": (
                    self.collection_cooldown_until.isoformat()
                    if self.collection_cooldown_until is not None
                    else None
                ),
                "last_reason": self.collection_last_block_reason,
                "updated_at": _utc_now(),
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _register_collection_block(self, reason: str) -> datetime:
        if not self.collection_block_cooldowns:
            raise RuntimeError("collection protection requires at least one cooldown interval")
        with self.challenge_lock:
            self.collection_block_streak += 1
            index = min(self.collection_block_streak - 1, len(self.collection_block_cooldowns) - 1)
            delay = max(1.0, float(self.collection_block_cooldowns[index]))
            self.collection_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
            self.collection_last_block_reason = reason
            cooldown_until = self.collection_cooldown_until
        self._save_collection_protection_state()
        return cooldown_until

    def _clear_collection_block(self) -> None:
        with self.challenge_lock:
            if self.collection_block_streak == 0 and self.collection_cooldown_until is None:
                return
            self.collection_block_streak = 0
            self.collection_cooldown_until = None
            self.collection_last_block_reason = None
        self._save_collection_protection_state()

    def collection_protection_view(self) -> dict[str, Any]:
        with self.challenge_lock:
            cooldown_until = self.collection_cooldown_until
            streak = self.collection_block_streak
            reason = self.collection_last_block_reason
        now = datetime.now(timezone.utc)
        return {
            "active": cooldown_until is not None and cooldown_until > now,
            "block_streak": streak,
            "next_eligible_at": cooldown_until.isoformat() if cooldown_until is not None else None,
            "reason": reason,
            "strategy": "manual_reentry_only",
        }

    def server_close(self) -> None:
        self.daily_operations.close()
        self.finalization_watchdog_stop.set()
        if self.finalization_watchdog is not None:
            self.finalization_watchdog.join(timeout=2)
        for pool in (
            self.history_collection_pool,
            self.material_preparation_pool,
            self.transition_pool,
            self.finalization_pool,
            self.analysis_pool,
            self.atomic_breakdown_pool,
            self.breakdown_test_pool,
            self.breakdown_backlog_pool,
            self.manual_exploration_pool,
            self.knowledge_mirror_pool,
        ):
            pool.shutdown(wait=False, cancel_futures=False)
        super().server_close()

    def storage_relocation_preflight(self) -> list[str]:
        """Storage is copied only while no formal worker can change the source."""
        with self.challenge_lock:
            running_cold_start = [
                job_id for job_id, job in self.jobs.items()
                if str(job.get("status") or "") in {"queued", "running"}
            ]
        running_daily = [
            item for item in self.daily_operations.view()
            if str((item.get("job") or {}).get("status") or "") == "running"
        ] if self.data_identity == "production" else []
        reasons: list[str] = []
        if running_cold_start:
            reasons.append("冷启动仍有正在执行或排队的正式任务，不能在写入过程中复制数据")
        if running_daily:
            reasons.append("日常运行仍在执行，不能在写入过程中复制数据")
        return reasons

    def restart_after_storage_relocation(self) -> None:
        """Start one hidden replacement only after this process has released the local port."""
        if self.storage_restart_requested:
            return
        self.storage_restart_requested = True
        command = [
            sys.executable,
            "-m",
            "scripts.agent_platform.cold_start_config_server",
            "--host", str(self.server_address[0]),
            "--port", str(self.server_address[1]),
            "--data-identity", str(self.data_identity),
            "--config-dir", str(self.config_dir),
            "--daily-hour", str(self.daily_operations.run_hour),
            "--daily-minute", str(self.daily_operations.run_minute),
            "--daily-poll-seconds", str(self.daily_operations.poll_seconds),
            "--daily-max-items-per-account", str(self.daily_operations.max_items_per_account),
            "--start-delay-seconds", "2",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(command, cwd=str(ROOT), creationflags=creationflags)
        threading.Timer(0.5, self.shutdown).start()

    def schedule_knowledge_mirror(self, reason: str) -> None:
        if self.data_identity != "production":
            return
        with self.knowledge_mirror_lock:
            self.knowledge_mirror_pending = True
            self.knowledge_mirror_reason = reason
            if self.knowledge_mirror_running:
                return
            self.knowledge_mirror_running = True
            self.knowledge_mirror_status = {
                "status": "queued",
                "reason": reason,
                "updated_at": _utc_now(),
                "error": None,
            }
        self.knowledge_mirror_pool.submit(self._drain_knowledge_mirror)

    def schedule_person_exploration(
        self,
        *,
        exploration_id: str,
        person_name: str,
        works: tuple[dict[str, Any], ...],
        actor: str,
    ) -> dict[str, Any]:
        with self.challenge_lock:
            current = self.manual_exploration_jobs.get(exploration_id)
            if current and current.get("status") in {"queued", "running"}:
                return dict(current)
            job = {
                "exploration_id": exploration_id,
                "status": "queued",
                "detail": "人物听众材料采集已经排队",
                "updated_at": _utc_now(),
                "error": None,
            }
            self.manual_exploration_jobs[exploration_id] = job
        self.manual_exploration_pool.submit(
            self._run_person_exploration,
            exploration_id=exploration_id,
            person_name=person_name,
            works=works,
            actor=actor,
        )
        return dict(job)

    def _run_person_exploration(
        self,
        *,
        exploration_id: str,
        person_name: str,
        works: tuple[dict[str, Any], ...],
        actor: str,
    ) -> None:
        with self.challenge_lock:
            self.manual_exploration_jobs[exploration_id] = {
                "exploration_id": exploration_id,
                "status": "running",
                "detail": "正在复用网易云和豆瓣各自的一个后台浏览器会话采集听众材料",
                "updated_at": _utc_now(),
                "error": None,
            }
        core = Stage0ContentProductionCore.open(
            self.db_path,
            data_identity=self.data_identity,  # type: ignore[arg-type]
        )
        try:
            result = build_music_audience_material_collector(core).collect_initial_scan(
                exploration_id=exploration_id,
                person_name=person_name,
                works=works,
                actor=actor,
            )
            with self.challenge_lock:
                self.manual_exploration_jobs[exploration_id] = {
                    "exploration_id": exploration_id,
                    "status": "completed",
                    "detail": (
                        f"已查看 {int(result.get('viewed_count') or 0)} 条，"
                        f"正式留存 {int(result.get('retained_count') or 0)} 条"
                    ),
                    "updated_at": _utc_now(),
                    "result": result,
                    "error": None,
                }
            self.schedule_knowledge_mirror(
                f"person_exploration_completed:{exploration_id}"
            )
        except Exception as exc:
            with self.challenge_lock:
                self.manual_exploration_jobs[exploration_id] = {
                    "exploration_id": exploration_id,
                    "status": "failed",
                    "detail": "本次人物听众材料采集已停止，不会自动反复重试",
                    "updated_at": _utc_now(),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc).strip() or type(exc).__name__,
                    },
                }
        finally:
            core.close()

    def _drain_knowledge_mirror(self) -> None:
        while True:
            with self.knowledge_mirror_lock:
                reason = self.knowledge_mirror_reason
                self.knowledge_mirror_pending = False
                self.knowledge_mirror_status = {
                    "status": "running",
                    "reason": reason,
                    "updated_at": _utc_now(),
                    "error": None,
                }
            core = Stage0ContentProductionCore.open(
                self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
            )
            try:
                enforce_runtime_startup_guard(
                    entrypoint="knowledge_mirror_formal_receipt"
                )
                result = ObsidianKnowledgeMirrorService(core=core).export(
                    vault_directory=ROOT / "vault",
                    actor="knowledge_mirror_automatic_worker",
                )
                status = {
                    "status": "completed",
                    "reason": reason,
                    "updated_at": _utc_now(),
                    "record_count": result["record_count"],
                    "relationship_count": result["relationship_count"],
                    "error": None,
                }
            except Exception as exc:
                status = {
                    "status": "failed",
                    "reason": reason,
                    "updated_at": _utc_now(),
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "automatic_retry": False,
                    },
                }
            finally:
                core.close()
            with self.knowledge_mirror_lock:
                self.knowledge_mirror_status = status
                if not self.knowledge_mirror_pending:
                    self.knowledge_mirror_running = False
                    return

    def _job_state(
        self,
        *,
        status: str,
        detail: str,
        attempt: int = 1,
        phase: str | None = None,
        queued_at: str | None = None,
        failure_stage: str | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "status": status,
            "detail": detail,
            "attempt": attempt,
            "max_attempts": 1,
            "updated_at": _utc_now(),
        }
        if phase:
            state["phase"] = phase
        if queued_at:
            state["queued_at"] = queued_at
        if failure_stage:
            state["failure_stage"] = failure_stage
        return state

    def resume_started_cold_starts(self) -> dict[str, int]:
        """Resume formal cold-start work after the local platform restarts."""
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            unfinished_rows = core.conn.execute(
                "SELECT registration.registration_id, registration.current_step, registration.status "
                "FROM stage0_cold_start_configuration configuration "
                "JOIN stage0_competitor_registration registration "
                "ON registration.cold_start_id=configuration.cold_start_id "
                "AND registration.data_identity=configuration.data_identity "
                "WHERE configuration.data_identity=? AND configuration.status='started' "
                "AND registration.status!='completed' "
                "ORDER BY CASE registration.current_step "
                "WHEN 'historical_material' THEN 0 "
                "WHEN 'high_signal_identification' THEN 1 "
                "WHEN 'transcripts_and_comments' THEN 2 "
                "WHEN 'breakdown' THEN 3 "
                "WHEN 'tag_candidates' THEN 4 "
                "ELSE 5 END, registration.created_at, registration.registration_id",
                (self.data_identity,),
            ).fetchall()
            tag_library_ready_count = 0
        finally:
            core.close()
        resumed_registration_count = 0
        paused_registration_count = 0
        for row in unfinished_rows:
            registration_id = str(row["registration_id"])
            paused = self.paused_jobs.get(registration_id)
            if paused is not None:
                expected_phase = _REGISTRATION_STEP_PHASE.get(
                    str(row["current_step"]),
                    "starting",
                )
                paused_failure_stage = str(
                    paused.get("failure_stage") or paused.get("phase") or ""
                )
                pause_detail = str(paused.get("detail") or "")
                contract_reload_pause = (
                    "cold-start contract changed after this process loaded it"
                    in pause_detail
                )
                if paused_failure_stage == expected_phase and not contract_reload_pause:
                    with self.challenge_lock:
                        self.jobs[registration_id] = dict(paused)
                    paused_registration_count += 1
                    continue
                self._clear_paused_job(registration_id)
            self.schedule_registration(registration_id)
            resumed_registration_count += 1
        return {
            "registration_count": resumed_registration_count,
            "paused_registration_count": paused_registration_count,
            "tag_library_review_count": 0,
            "tag_library_ready_count": tag_library_ready_count,
        }

    def _ensure_tag_library_if_ready(
        self,
        core: Stage0ContentProductionCore,
        *,
        cold_start_id: str,
    ) -> dict[str, Any] | None:
        with self.tag_library_lock:
            existing = core.get_cold_start_tag_library(cold_start_id=cold_start_id)
            if existing is not None:
                return existing
            missing_high_signal = core.conn.execute(
                "SELECT 1 FROM stage0_competitor_registration registration "
                "WHERE registration.cold_start_id=? AND registration.data_identity=? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM stage0_competitor_registration_step step "
                "WHERE step.registration_id=registration.registration_id "
                "AND step.data_identity=registration.data_identity "
                "AND step.step_name='high_signal_identification'"
                ") LIMIT 1",
                (cold_start_id, core.data_identity),
            ).fetchone()
            if missing_high_signal is not None:
                return None
            return core.build_cold_start_tag_library(
                cold_start_id=cold_start_id,
                actor="cold_start_automatic_worker",
            )

    def _registration_step(self, registration_id: str) -> tuple[str, str]:
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            enforce_runtime_startup_guard(
                entrypoint="automatic_competitor_registration"
            )
            registration = core.get_competitor_registration(registration_id=registration_id)
            return str(registration["current_step"]), str(registration["status"])
        finally:
            core.close()

    def _pool_for_registration_step(self, step_name: str) -> ThreadPoolExecutor:
        if step_name == "historical_material":
            return self.history_collection_pool
        if step_name == "transcripts_and_comments":
            return self.material_preparation_pool
        if step_name == "awaiting_human_review":
            return self.finalization_pool
        if step_name in {"breakdown", "tag_candidates"}:
            return self.analysis_pool
        return self.transition_pool

    def schedule_registration(self, registration_id: str, *, attempt: int = 1) -> None:
        step_name, registration_status = self._registration_step(registration_id)
        if registration_status == "completed":
            return
        phase = _REGISTRATION_STEP_PHASE.get(step_name, "starting")
        detail = _REGISTRATION_QUEUE_DETAIL.get(step_name, "等待读取未完成步骤")
        with self.challenge_lock:
            current = self.jobs.get(registration_id)
            if current and current.get("status") in {"queued", "running"}:
                return
            now = _utc_now()
            self.jobs[registration_id] = self._job_state(
                status="queued",
                phase=phase,
                detail=detail,
                attempt=attempt,
                queued_at=now,
            )
        self._pool_for_registration_step(step_name).submit(
            self._run_registration,
            registration_id,
            attempt,
            step_name,
        )

    def _run_registration(self, registration_id: str, attempt: int, scheduled_step: str) -> None:
        collection_steps = {"historical_material", "transcripts_and_comments"}
        if scheduled_step in collection_steps:
            with self.challenge_lock:
                cooldown_until = self.collection_cooldown_until
            if cooldown_until is not None and cooldown_until > datetime.now(timezone.utc):
                self._record_registration_failure(
                    registration_id,
                    attempt=attempt,
                    reason=(
                        "平台拦截后的冷却尚未结束；系统已停止，不会自行再次请求。"
                        "如需继续，须由用户明确发起。"
                    ),
                )
                return
        phase = _REGISTRATION_STEP_PHASE.get(scheduled_step, "starting")
        with self.challenge_lock:
            self.jobs[registration_id] = self._job_state(
                status="running",
                phase=phase,
                detail="正在执行该账号当前资料建库步骤",
                attempt=attempt,
            )

        def report_progress(progress_phase: str, detail: str) -> None:
            with self.challenge_lock:
                self.jobs[registration_id] = self._job_state(
                    status="running",
                    phase=progress_phase,
                    detail=detail,
                    attempt=attempt,
                )

        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            enforce_runtime_startup_guard(
                entrypoint="automatic_competitor_registration"
            )
            registration = core.get_competitor_registration(registration_id=registration_id)
            current_step = str(registration["current_step"])
            if current_step != scheduled_step:
                with self.challenge_lock:
                    self.jobs.pop(registration_id, None)
                self.schedule_registration(registration_id)
                return
            result = CompetitorRegistrationService(
                core=core,
                executor=build_configured_competitor_registration_executor(
                    core,
                    progress_callback=report_progress,
                ),
            ).run_competitor_registration_step(
                registration_id=registration_id,
                actor="cold_start_automatic_worker",
                idempotency_key=f"automatic:v4:{registration_id}:{scheduled_step}",
            )
            self._clear_paused_job(registration_id)
            if scheduled_step in collection_steps:
                self._clear_collection_block()
            registration = core.get_competitor_registration(registration_id=registration_id)
            cold_start_id = str(registration["cold_start_id"])
            self._ensure_tag_library_if_ready(core, cold_start_id=cold_start_id)
            if registration["status"] == "completed":
                with self.challenge_lock:
                    self.jobs[registration_id] = self._job_state(
                        status="completed",
                        phase="registration_completed",
                        detail="该账号的对标资料建库已经完成并进入正式日常追踪",
                        attempt=attempt,
                    )
                remaining = core.conn.execute(
                    "SELECT 1 FROM stage0_competitor_registration WHERE cold_start_id=? AND data_identity=? AND status!='completed' LIMIT 1",
                    (cold_start_id, core.data_identity),
                ).fetchone()
                if remaining is None:
                    core.build_cold_start_tag_library(
                        cold_start_id=cold_start_id,
                        actor="cold_start_automatic_worker",
                    )
                # Each completed competitor is eligible for the one shared
                # daily tracking task immediately; it never waits for a topic
                # or for unrelated domains to finish their own setup.
                self.daily_operations.activate_after_competitor_registration()
                return
            del result
            with self.challenge_lock:
                self.jobs.pop(registration_id, None)
            self.schedule_registration(registration_id)
        except Exception as exc:
            if scheduled_step in collection_steps and "account blocked" in str(exc).lower():
                self._register_collection_block(_friendly_retry_reason(exc))
                self._record_registration_failure(
                    registration_id,
                    attempt=attempt,
                    reason=(
                        "平台拒绝采集，系统已停止，不会等待冷却后自行重试；"
                        "如需继续，须由用户明确发起。"
                    ),
                )
            else:
                self._record_registration_failure(
                    registration_id, attempt=attempt, reason=_friendly_retry_reason(exc)
                )
        except BaseException as exc:
            # A worker must never vanish from the visible queue.  This branch
            # is still database-only for the finalization step and records an
            # explicit attention state instead of retrying external work.
            self._record_registration_failure(
                registration_id, attempt=attempt, reason=_friendly_retry_reason(exc)
            )
        finally:
            core.close()

    def _record_registration_failure(self, registration_id: str, *, attempt: int, reason: str) -> None:
        with self.challenge_lock:
            current = dict(self.jobs.get(registration_id) or {})
        failure_stage = str(current.get("phase") or "starting")
        with self.challenge_lock:
            paused_state = self._job_state(
                status="needs_attention",
                phase=failure_stage,
                failure_stage=failure_stage,
                attempt=attempt,
                detail=(
                    f"部分资料本次未完成：{reason}；系统不会自动重复请求，"
                    "用户明确要求继续后才会处理未完成内容"
                ),
            )
            self.jobs[registration_id] = paused_state
        self._persist_paused_job(registration_id, paused_state)

    def schedule_user_authorized_atomic_breakdown(
        self,
        *,
        command_id: str,
        registration_id: str,
        source_id: str,
    ) -> dict[str, Any]:
        """Queue one already-authorized breakdown inside this server process.

        This is deliberately serial and never retries. The formal command is
        the only permission to place work here; the task itself records either
        one completed breakdown or one saved failure.
        """
        if not command_id or not registration_id or not source_id:
            raise StateTransitionError("atomic competitor breakdown needs a formal command and one source")
        with self.challenge_lock:
            existing = self.atomic_breakdown_jobs.get(command_id)
            if existing is not None:
                return dict(existing)
            job = self._job_state(
                status="queued",
                phase="hit_breakdown",
                detail="已收到用户确认，等待执行这一条拆解",
            )
            job.update({
                "command_id": command_id,
                "registration_id": registration_id,
                "source_id": source_id,
                "automatic_retry": False,
            })
            self.atomic_breakdown_jobs[command_id] = job
        self.atomic_breakdown_pool.submit(
            self._run_user_authorized_atomic_breakdown,
            command_id,
            registration_id,
            source_id,
        )
        return dict(job)

    def atomic_breakdown_progress(self, *, command_id: str) -> dict[str, Any]:
        with self.challenge_lock:
            job = self.atomic_breakdown_jobs.get(command_id)
            if job is None:
                raise StateTransitionError("the requested competitor breakdown job does not exist in this server session")
            return dict(job)

    def resume_pending_breakdown_backlog_tasks(self) -> int:
        """Resume only a user-authorized frozen backlog after a server restart."""
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            rows = core.conn.execute(
                "SELECT backlog_task_id FROM stage0_competitor_breakdown_backlog_task "
                "WHERE data_identity=? AND status IN ('queued', 'running') ORDER BY created_at",
                (self.data_identity,),
            ).fetchall()
        finally:
            core.close()
        for row in rows:
            self.schedule_formal_breakdown_backlog_task(backlog_task_id=str(row["backlog_task_id"]))
        return len(rows)

    def schedule_formal_breakdown_backlog_task(self, *, backlog_task_id: str) -> dict[str, Any]:
        if not backlog_task_id:
            raise StateTransitionError("formal breakdown backlog needs its task identity")
        with self.challenge_lock:
            existing = self.breakdown_backlog_jobs.get(backlog_task_id)
            if existing is not None:
                return dict(existing)
            job = self._job_state(
                status="queued",
                phase="hit_breakdown_backlog",
                detail="正式补拆任务已锁定材料清单，等待逐条运行",
            )
            job["backlog_task_id"] = backlog_task_id
            self.breakdown_backlog_jobs[backlog_task_id] = job
        self.breakdown_backlog_pool.submit(self._run_formal_breakdown_backlog_task, backlog_task_id)
        return dict(job)

    def formal_breakdown_backlog_progress(self, *, backlog_task_id: str) -> dict[str, Any]:
        with self.challenge_lock:
            visible = dict(self.breakdown_backlog_jobs.get(backlog_task_id) or {})
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            task = core.competitor_breakdown_backlog_task_progress(backlog_task_id=backlog_task_id)
        finally:
            core.close()
        return {**visible, "task": task}

    def _run_formal_breakdown_backlog_task(self, backlog_task_id: str) -> None:
        with self.challenge_lock:
            current = dict(self.breakdown_backlog_jobs.get(backlog_task_id) or {})
            if current.get("status") != "queued":
                return
            current.update(self._job_state(
                status="running",
                phase="hit_breakdown_backlog",
                detail="正式补拆任务正在逐条等待模型返回",
            ))
            current["backlog_task_id"] = backlog_task_id
            self.breakdown_backlog_jobs[backlog_task_id] = current

        def report(progress: dict[str, Any]) -> None:
            summary = progress.get("summary") if isinstance(progress.get("summary"), dict) else {}
            phase = str(progress.get("phase") or "initial")
            detail = (
                f"正式补拆：已完成 {int(summary.get('completed') or 0)}，"
                f"失败留档 {int(summary.get('failed') or 0)}，"
                f"待处理 {int(summary.get('pending') or 0)}"
            )
            if phase == "post_batch_delivery_retry":
                detail = "正式补拆的常规处理已结束，正在补跑服务端中断项；" + detail
            with self.challenge_lock:
                job = self._job_state(status="running", phase="hit_breakdown_backlog", detail=detail)
                job["backlog_task_id"] = backlog_task_id
                job["summary"] = summary
                self.breakdown_backlog_jobs[backlog_task_id] = job

        core: Stage0ContentProductionCore | None = None
        try:
            core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
            enforce_runtime_startup_guard(entrypoint="user_authorized_formal_competitor_breakdown_backlog")
            result = CompetitorRegistrationService(
                core=core,
                executor=build_configured_competitor_registration_executor(core, progress_callback=lambda _phase, _detail: None),
            ).run_formal_breakdown_backlog_task(
                backlog_task_id=backlog_task_id,
                progress_callback=report,
            )
            terminal = self._job_state(
                status="completed" if result["status"] == "completed" else "completed_with_failures",
                phase="hit_breakdown_backlog",
                detail="正式补拆任务已处理完全部锁定材料，可查看最终汇总",
            )
            terminal["result"] = result
        except Exception as exc:
            terminal = self._job_state(
                status="failed",
                phase="hit_breakdown_backlog",
                failure_stage="hit_breakdown_backlog",
                detail=f"正式补拆任务异常中止：{_friendly_retry_reason(exc)}",
            )
            terminal["error"] = {"type": type(exc).__name__, "reason": str(exc)}
        finally:
            if core is not None:
                core.close()
        terminal["backlog_task_id"] = backlog_task_id
        with self.challenge_lock:
            self.breakdown_backlog_jobs[backlog_task_id] = terminal

    def schedule_competitor_breakdown_test(
        self,
        *,
        test_id: str,
        materials: list[dict[str, Any]],
        approval: dict[str, str],
    ) -> dict[str, Any]:
        """Run an explicitly selected, non-writing quality test in this server.

        The caller supplies already-prepared formal materials.  This is the only
        supported test path: it uses the registered atomic Skill but retains its
        receipt in server memory rather than writing business data.
        """
        if self.data_identity != "production":
            raise StateTransitionError("competitor breakdown tests require the formal production runtime")
        if not test_id or not materials:
            raise StateTransitionError("competitor breakdown test needs a test id and selected materials")
        with self.challenge_lock:
            if test_id in self.breakdown_test_jobs:
                return _competitor_breakdown_test_job_view(
                    self.breakdown_test_jobs[test_id], include_raw=False
                )
            job = self._job_state(
                status="queued",
                phase="hit_breakdown_test",
                detail="已收到人工选定的测试材料，等待按固定测试路径运行",
            )
            job.update({
                "test_id": test_id,
                "source_ids": [str(item["source_id"]) for item in materials],
                "approval": dict(approval),
                "formal_business_data_written": False,
            })
            self.breakdown_test_jobs[test_id] = job
        self.breakdown_test_pool.submit(
            self._run_competitor_breakdown_test,
            test_id,
            [dict(item) for item in materials],
        )
        return _competitor_breakdown_test_job_view(job, include_raw=False)

    def competitor_breakdown_test_progress(
        self,
        *,
        test_id: str,
        include_raw: bool,
    ) -> dict[str, Any]:
        with self.challenge_lock:
            job = self.breakdown_test_jobs.get(test_id)
            if job is None:
                raise StateTransitionError("the requested competitor breakdown test does not exist in this server session")
            return _competitor_breakdown_test_job_view(job, include_raw=include_raw)

    def _run_competitor_breakdown_test(
        self,
        test_id: str,
        materials: list[dict[str, Any]],
    ) -> None:
        with self.challenge_lock:
            current = dict(self.breakdown_test_jobs.get(test_id) or {})
            if current.get("status") != "queued":
                return
            current.update(self._job_state(
                status="running",
                phase="hit_breakdown_test",
                detail="正在按固定测试路径逐条等待模型返回",
            ))
            self.breakdown_test_jobs[test_id] = current
        try:
            enforce_runtime_startup_guard(entrypoint="user_authorized_competitor_breakdown_test")
            result = run_test_only_competitor_breakdown_batch(
                test_id=test_id,
                materials=materials,
            )
            terminal = self._job_state(
                status="completed",
                phase="hit_breakdown_test",
                detail="本次测试材料已全部处理完，可查看拉通结果",
            )
            terminal["result"] = result
        except Exception as exc:
            terminal = self._job_state(
                status="failed",
                phase="hit_breakdown_test",
                detail=f"固定测试入口未能完成：{_friendly_retry_reason(exc)}",
            )
            terminal["error"] = {"type": type(exc).__name__, "reason": str(exc)}
        terminal.update({
            "test_id": test_id,
            "source_ids": [str(item["source_id"]) for item in materials],
            "approval": current.get("approval") or {},
            "formal_business_data_written": False,
        })
        with self.challenge_lock:
            self.breakdown_test_jobs[test_id] = terminal

    def _run_user_authorized_atomic_breakdown(
        self,
        command_id: str,
        registration_id: str,
        source_id: str,
    ) -> None:
        with self.challenge_lock:
            current = dict(self.atomic_breakdown_jobs.get(command_id) or {})
            if current.get("status") != "queued":
                return
            current.update(self._job_state(
                status="running",
                phase="hit_breakdown",
                detail="正在等待模型最终回答",
            ))
            current.update({
                "command_id": command_id,
                "registration_id": registration_id,
                "source_id": source_id,
                "automatic_retry": False,
            })
            self.atomic_breakdown_jobs[command_id] = current
        core: Stage0ContentProductionCore | None = None
        try:
            core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)
            enforce_runtime_startup_guard(entrypoint="user_authorized_atomic_competitor_breakdown")
            result = CompetitorRegistrationService(
                core=core,
                executor=build_configured_competitor_registration_executor(core),
            ).run_user_authorized_atomic_breakdown(
                registration_id=registration_id,
                source_id=source_id,
            )
            terminal = self._job_state(
                status="completed",
                phase="hit_breakdown",
                detail="这一条拆解已经完成",
            )
            terminal["result"] = result
        except Exception as exc:
            terminal = self._job_state(
                status="failed",
                phase="hit_breakdown",
                failure_stage="hit_breakdown",
                detail=f"这一条拆解失败：{_friendly_retry_reason(exc)}",
            )
            terminal["error"] = {
                "type": type(exc).__name__,
                "reason": str(exc),
                "automatic_retry": False,
            }
        finally:
            if core is not None:
                core.close()
        terminal.update({
            "command_id": command_id,
            "registration_id": registration_id,
            "source_id": source_id,
            "automatic_retry": False,
        })
        with self.challenge_lock:
            self.atomic_breakdown_jobs[command_id] = terminal

class ColdStartConfigHandler(BaseHTTPRequestHandler):
    server: ColdStartConfigServer

    def log_message(self, format: str, *args: Any) -> None:
        _console(f"{self.address_string()} - {format % args}")

    def _send_json(self, value: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise StateTransitionError("invalid request length") from exc
        if length <= 0 or length > 1_000_000:
            raise StateTransitionError("request body is missing or too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateTransitionError("request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise StateTransitionError("request body must be an object")
        return value

    def _open_core(self) -> Stage0ContentProductionCore:
        return Stage0ContentProductionCore.open(
            self.server.db_path, data_identity=self.server.data_identity,  # type: ignore[arg-type]
        )

    def _service(self, core: Stage0ContentProductionCore) -> ColdStartOnboardingService:
        return ColdStartOnboardingService(core=core, config_dir=self.server.config_dir)

    def _workbench_state_snapshot(self) -> dict[str, Any]:
        """Return only status facts that are safe to place in a model prompt."""
        core = self._open_core()
        try:
            service = self._service(core)
            daily_operations = (
                self.server.daily_operations.view()
                if self.server.data_identity == "production"
                else []
            )
            return summarize_workbench_state(
                data_identity=core.data_identity,
                domains=service.list_domains(),
                daily_operations=daily_operations,
                content_tasks=core.list_content_workbench(),
                runtime_storage=runtime_storage_overview(),
            )
        finally:
            core.close()

    def _assistant_legacy_disabled(self, body: dict[str, Any]) -> dict[str, Any]:
        message = str(body.get("message") or "").strip()
        domain_label = str(body.get("domain_label") or "").strip()
        if not message:
            raise StateTransitionError("请输入你想询问的内容")
        snapshot = self._workbench_state_snapshot()
        valid_domains = {
            str(item.get("domain_label") or "")
            for item in snapshot.get("domains") or []
        }
        if domain_label and domain_label not in valid_domains:
            raise StateTransitionError("指定的领域不存在，系统没有替你切换领域")
        if not domain_label and len(valid_domains) > 1:
            raise StateTransitionError("当前有多个领域，请先明确选择一个领域")
        snapshot = restrict_workbench_state(snapshot, domain_label=domain_label)
        if body.get("allow_formal_action") is True or body.get("explicit_confirmation") is True:
            raise StateTransitionError("GPT 对话助手现在只读；请使用领域卡片上的普通按钮切换运行模式")
        allow_formal_action = body.get("allow_formal_action") is True
        explicit_confirmation = body.get("explicit_confirmation") is True
        formal_action_handler = None
        if allow_formal_action or explicit_confirmation:
            if not (allow_formal_action and explicit_confirmation):
                raise StateTransitionError("正式动作必须同时打开动作许可和明确确认")
            actor = str(body.get("actor") or "").strip()
            action_reason = str(body.get("action_reason") or "").strip()
            carrier_binding_id = str(body.get("carrier_binding_id") or "").strip()
            session_ref = str(body.get("session_ref") or "").strip()
            command_id = str(body.get("command_id") or "").strip()
            if not domain_label or not actor or not action_reason:
                raise StateTransitionError("启用正式动作时必须明确领域、操作人和本次理由")
            if not carrier_binding_id or not session_ref or not command_id:
                raise StateTransitionError("正式动作必须使用已经验证的工作台会话")
            with self.server.challenge_lock:
                session_actor = self.server.validated_sessions.get(session_ref)
            if session_actor != actor:
                raise StateTransitionError("这个工作台会话没有完成当前操作人的验证")
            action_used = False

            def formal_action_handler(
                arguments: dict[str, Any],
                protocol_params: dict[str, Any],
            ) -> dict[str, Any]:
                nonlocal action_used
                if action_used:
                    raise StateTransitionError("一次对话最多执行一个领域模式切换")
                requested_domain = str(arguments.get("domain_label") or "").strip()
                requested_mode = str(arguments.get("workflow_mode") or "").strip()
                requested_reason = str(arguments.get("reason") or "").strip()
                if requested_domain != domain_label:
                    raise StateTransitionError("工具请求的领域不是当前已选领域，已阻止执行")
                if requested_mode not in {"manual_guard", "mature_automatic"}:
                    raise StateTransitionError("工具请求的模式不受支持")
                if requested_reason != action_reason:
                    raise StateTransitionError("模式切换理由必须来自用户明确填写的确认内容")
                call_id = str(protocol_params.get("callId") or uuid.uuid4().hex)
                self._change_workflow_mode(
                    {
                        "domain_label": requested_domain,
                        "workflow_mode": requested_mode,
                        "actor": actor,
                        "reason": action_reason,
                        "carrier_binding_id": carrier_binding_id,
                        "session_ref": session_ref,
                        "command_id": f"assistant:{command_id}:{call_id}",
                    }
                )
                action_used = True
                return {
                    "success": True,
                    "action": "change_domain_workflow_mode",
                    "domain_label": requested_domain,
                    "workflow_mode": requested_mode,
                    "formal_data_written": True,
                    "result": "领域模式已经按用户确认切换",
                }

        assistant = WorkbenchAssistant.from_environment(cwd=ROOT)
        return assistant.answer(
            message=message,
            domain_label=domain_label,
            state_snapshot=snapshot,
        )

    def _assistant(self, body: dict[str, Any]) -> dict[str, Any]:
        message = str(body.get("message") or "").strip()
        domain_label = str(body.get("domain_label") or "").strip()
        if not message:
            raise StateTransitionError("请输入你想询问的内容")
        snapshot = self._workbench_state_snapshot()
        valid_domains = {
            str(item.get("domain_label") or "")
            for item in snapshot.get("domains") or []
        }
        if domain_label and domain_label not in valid_domains:
            raise StateTransitionError("指定的领域不存在，系统没有替你切换领域")
        if not domain_label and len(valid_domains) > 1:
            raise StateTransitionError("当前有多个领域，请先明确选择一个领域")
        if body.get("allow_formal_action") is True or body.get("explicit_confirmation") is True:
            raise StateTransitionError("GPT 对话助手现在只读；请使用领域卡片上的普通按钮切换运行模式")
        snapshot = restrict_workbench_state(snapshot, domain_label=domain_label)
        assistant = WorkbenchAssistant.from_environment(cwd=ROOT)
        return assistant.answer(
            message=message,
            domain_label=domain_label,
            state_snapshot=snapshot,
        )

    def _require_test_approval(self, body: dict[str, Any]) -> dict[str, str]:
        actor = str(body.get("actor") or "").strip()
        approval_kind = str(body.get("approval_kind") or "").strip()
        if not actor:
            raise StateTransitionError("competitor breakdown test needs its confirming user")
        if approval_kind == "natural_dialogue":
            conversation_ref = str(body.get("conversation_ref") or "").strip()
            if body.get("explicit_confirmation") is not True or not conversation_ref:
                raise StateTransitionError("natural-dialogue testing needs the user's explicit confirmation and conversation reference")
            return {
                "kind": approval_kind,
                "actor": actor,
                "conversation_ref": conversation_ref,
            }
        if approval_kind == "visual_session":
            session_ref = str(body.get("session_ref") or "").strip()
            if not session_ref:
                raise StateTransitionError("visual testing needs its confirming user and validated session")
            with self.server.challenge_lock:
                if self.server.validated_sessions.get(session_ref) != actor:
                    raise StateTransitionError("this session has not completed its own user round-trip validation")
            return {"kind": approval_kind, "actor": actor, "session_ref": session_ref}
        raise StateTransitionError("competitor breakdown test needs either natural-dialogue or visual-session approval")

    def _selected_breakdown_test_materials(
        self,
        *,
        core: Stage0ContentProductionCore,
        source_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not source_ids or len(source_ids) != len(set(source_ids)):
            raise StateTransitionError("competitor breakdown test needs a non-empty list of distinct selected materials")
        materials: list[dict[str, Any]] = []
        for source_id in source_ids:
            row = core.conn.execute(
                "SELECT item.item_ref, item.artifact_json, account.display_name "
                "FROM stage0_competitor_registration_item item "
                "JOIN stage0_competitor_registration registration "
                "ON registration.registration_id=item.registration_id "
                "AND registration.data_identity=item.data_identity "
                "JOIN stage0_content_account account "
                "ON account.content_account_id=registration.competitor_account_id "
                "AND account.data_identity=registration.data_identity "
                "WHERE item.data_identity=? AND item.step_name='transcripts_and_comments' "
                "AND item.status='completed' AND item.item_ref=? LIMIT 1",
                (core.data_identity, source_id),
            ).fetchone()
            if row is None:
                raise StateTransitionError("selected competitor breakdown test material is not a completed formal material")
            try:
                artifact = json.loads(str(row["artifact_json"]))
                transcript_path = Path(str(artifact["transcript_ref"])).resolve()
                transcript = transcript_path.read_text(encoding="utf-8")
            except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StateTransitionError("selected competitor breakdown test material cannot be read") from exc
            if not transcript.strip():
                raise StateTransitionError("selected competitor breakdown test material has an empty transcript")
            materials.append({
                "source_id": str(row["item_ref"]),
                "hit_id": str(row["item_ref"]),
                "title": str(row["display_name"]),
                "transcript": transcript,
                "metrics": dict(artifact.get("metrics") or {}),
                "comments": list(artifact.get("comments") or []),
            })
        return materials

    def _available_breakdown_test_materials(self, body: dict[str, Any]) -> dict[str, Any]:
        """List only existing, ready-to-test materials; this never selects or sends one."""
        approval = self._require_test_approval(body)
        core = self._open_core()
        try:
            rows = core.conn.execute(
                "SELECT item.item_ref, account.display_name, item.artifact_json "
                "FROM stage0_competitor_registration_item item "
                "JOIN stage0_competitor_registration registration "
                "ON registration.registration_id=item.registration_id "
                "AND registration.data_identity=item.data_identity "
                "JOIN stage0_content_account account "
                "ON account.content_account_id=registration.competitor_account_id "
                "AND account.data_identity=registration.data_identity "
                "WHERE item.data_identity=? AND item.step_name='transcripts_and_comments' "
                "AND item.status='completed' ORDER BY item.item_ref",
                (core.data_identity,),
            ).fetchall()
        finally:
            core.close()
        materials: list[dict[str, Any]] = []
        for row in rows:
            try:
                artifact = json.loads(str(row["artifact_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                artifact = {}
            materials.append({
                "source_id": str(row["item_ref"]),
                "account_name": str(row["display_name"]),
                "title": str(artifact.get("title") or ""),
            })
        return {
            "approval": approval,
            "materials": materials,
            "formal_business_data_written": False,
        }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = UI_PATH.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            core = self._open_core()
            try:
                service = self._service(core)
                carriers = core.list_validated_human_decision_carriers()
                visual = next((item for item in carriers if item["carrier_kind"] == "local_visual_business_ui"), None)
                knowledge_mirror = dict(self.server.knowledge_mirror_status)
                if knowledge_mirror.get("status") == "idle":
                    latest_mirror = core.conn.execute(
                        "SELECT mirror_root, record_count, relationship_count, completed_at "
                        "FROM stage0_knowledge_mirror_run "
                        "WHERE data_identity=? AND status='completed' "
                        "ORDER BY completed_at DESC LIMIT 1",
                        (core.data_identity,),
                    ).fetchone()
                    if latest_mirror is not None:
                        knowledge_mirror = {
                            "status": "completed",
                            "reason": "最近一次正式知识镜像",
                            "updated_at": str(latest_mirror["completed_at"]),
                            "record_count": int(latest_mirror["record_count"]),
                            "relationship_count": int(
                                latest_mirror["relationship_count"]
                            ),
                            "mirror_root": str(latest_mirror["mirror_root"]),
                            "error": None,
                        }
                self._send_json({
                    "data_identity": core.data_identity,
                    "domains": service.list_domains(),
                    "configurations": core.list_cold_start_configurations(),
                    "validated_carrier": visual,
                    "daily_operations": self.server.daily_operations.view() if self.server.data_identity == "production" else [],
                    "manual_sources": core.list_manual_source_workbench(),
                    "manual_exploration_jobs": list(
                        self.server.manual_exploration_jobs.values()
                    ),
                    "content_tasks": core.list_content_workbench(),
                    "voice_profiles": core.list_voice_profiles(),
                    "knowledge_mirror": knowledge_mirror,
                    "runtime_storage": runtime_storage_overview(),
                })
            finally:
                core.close()
            return
        if parsed.path == "/api/assistant/status":
            self._send_json(WorkbenchAssistant.configuration_status(cwd=ROOT))
            return
        if parsed.path == "/api/audio":
            production_id = str(
                (parse_qs(parsed.query).get("production_id") or [""])[0]
            ).strip()
            core = self._open_core()
            try:
                production = core.get_audio_production(
                    audio_production_id=production_id
                )
                audio_ref = Path(str(production.get("audio_ref") or "")).resolve()
                if (
                    production["status"]
                    not in {"awaiting_human_review", "approved"}
                    or not audio_ref.is_file()
                    or audio_ref.suffix.lower() != ".wav"
                ):
                    raise StateTransitionError(
                        "the requested formal audio is not available for review"
                    )
                body = audio_ref.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except StateTransitionError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            finally:
                core.close()
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            routes = {
                "/api/carrier/challenge": self._carrier_challenge,
                "/api/carrier/validate": self._carrier_validate,
                "/api/preview": self._preview,
                "/api/confirm": self._confirm,
                "/api/preflight": self._preflight,
                "/api/start": self._start,
                "/api/progress": self._progress,
                "/api/competitor-breakdown/run": self._run_competitor_breakdown,
                "/api/competitor-breakdown/progress": self._competitor_breakdown_progress,
                "/api/competitor-breakdown/backlog/run": self._run_formal_breakdown_backlog,
                "/api/competitor-breakdown/backlog/progress": self._formal_breakdown_backlog_progress,
                "/api/competitor-breakdown/test/run": self._run_competitor_breakdown_test,
                "/api/competitor-breakdown/test/materials": self._available_breakdown_test_materials,
                "/api/competitor-breakdown/test/progress": self._competitor_breakdown_test_progress,
                "/api/tag-library/review": self._review_tag_library,
                "/api/daily/run": self._run_daily,
                "/api/daily/select": self._select_daily_candidate,
                "/api/two-week/tags/review": self._review_two_week_tags,
                "/api/source/manual": self._register_manual_source,
                "/api/exploration/person/collect": self._collect_person_exploration,
                "/api/exploration/material": self._record_exploration_material,
                "/api/exploration/complete": self._complete_exploration_collection,
                "/api/exploration/direction": self._confirm_exploration_direction,
                "/api/topic/direct": self._create_direct_formal_topic,
                "/api/research-plan/decide": self._decide_research_plan,
                "/api/research/material": self._record_research_material,
                "/api/content/generate": self._generate_content,
                "/api/experience-candidate/decide": self._decide_experience_candidate,
                "/api/content/decide": self._decide_content,
                "/api/voice-profile/configure": self._configure_voice_profile,
                "/api/audio/produce": self._produce_audio,
                "/api/audio/review": self._review_audio,
                "/api/domain/workflow-mode": self._change_workflow_mode,
                "/api/publication/register": self._register_publication,
                "/api/publication/observation": self._record_publication_observation,
                "/api/publication/p7-review": self._prepare_p7_review,
                "/api/publication/p7-review/decide": self._decide_p7_review,
                "/api/runtime-storage/plan": self._plan_runtime_storage_relocation,
                "/api/runtime-storage/relocate": self._relocate_runtime_storage,
                "/api/assistant": self._assistant,
            }
            handler = routes.get(self.path)
            if handler is None:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(handler(body))
        except CodexAppServerProviderError as exc:
            self._send_json(
                {"error": str(exc), "formal_data_written": False},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (StateTransitionError, ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": f"unexpected server failure: {type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _carrier_challenge(self, body: dict[str, Any]) -> dict[str, Any]:
        actor = str(body.get("actor") or "").strip()
        if not actor:
            raise StateTransitionError("carrier validation requires the user's name")
        core = self._open_core()
        try:
            binding_id = "carrier_local_visual_business_ui"
            row = core.conn.execute(
                "SELECT * FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
                (binding_id, core.data_identity),
            ).fetchone()
            if row is None:
                core.propose_human_decision_carrier(
                    carrier_binding_id=binding_id,
                    carrier_kind="local_visual_business_ui",
                    entry_ref=f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/",
                    context_strategy="browser session keeps formal command context and identity",
                    actor=actor,
                )
            session_ref = f"visual_session_{uuid.uuid4().hex}"
            challenge = uuid.uuid4().hex
            with self.server.challenge_lock:
                self.server.challenges[challenge] = {
                    "session_ref": session_ref, "actor": actor, "created_at": time.time(),
                }
            return {
                "carrier_binding_id": binding_id,
                "session_ref": session_ref,
                "challenge": challenge,
                "outbound_received": True,
            }
        finally:
            core.close()

    def _carrier_validate(self, body: dict[str, Any]) -> dict[str, Any]:
        challenge = str(body.get("challenge") or "")
        session_ref = str(body.get("session_ref") or "")
        actor = str(body.get("actor") or "").strip()
        if body.get("explicit_confirmation") is not True:
            raise StateTransitionError("the user must explicitly confirm this visual entry")
        with self.server.challenge_lock:
            record = self.server.challenges.pop(challenge, None)
        if (
            record is None or time.time() - float(record["created_at"]) > 300
            or record["session_ref"] != session_ref or record["actor"] != actor
            or body.get("outbound_received") is not True
        ):
            raise StateTransitionError("visual entry round-trip challenge is missing, expired or from another context")
        core = self._open_core()
        try:
            binding_id = str(body.get("carrier_binding_id") or "")
            row = core.conn.execute(
                "SELECT status FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
                (binding_id, core.data_identity),
            ).fetchone()
            if row is None:
                raise StateTransitionError("visual entry proposal does not exist")
            if row["status"] != "validated":
                core.validate_human_decision_carrier(
                    carrier_binding_id=binding_id,
                    validation_evidence={
                        "inbound_round_trip": True,
                        "outbound_round_trip": True,
                        "same_context_verified": True,
                        "decision_identity_verified": True,
                        "evidence_ref": f"local_visual_round_trip:{challenge}",
                    },
                    actor=actor,
                    actor_kind="user",
                )
            with self.server.challenge_lock:
                self.server.validated_sessions[session_ref] = actor
            return {"carrier_binding_id": binding_id, "status": "validated"}
        finally:
            core.close()

    def _preview(self, body: dict[str, Any]) -> dict[str, Any]:
        core = self._open_core()
        try:
            return self._service(core).preview(body)
        finally:
            core.close()

    def _formal_command(
        self,
        *,
        core: Stage0ContentProductionCore,
        body: dict[str, Any],
        action: str,
        target_ref: str,
        payload: dict[str, Any],
        handler: Any,
    ) -> dict[str, Any]:
        enforce_runtime_startup_guard(
            entrypoint=f"formal_human_action:{action}"
        )
        if action in _DAILY_FORMAL_ACTIONS:
            enforce_daily_operations_runtime_guard(
                entrypoint=f"formal_human_action:{action}",
                source_types=DAILY_REPORT_SOURCE_TYPES,
                daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
            )
        if action in _CONTENT_ENTRY_ACTIONS:
            enforce_content_production_runtime_guard(
                entrypoint=f"formal_human_action:{action}",
                current_node="research_plan",
                current_status="not_started",
            )
        command = FormalHumanDecisionCommand(
            command_id=str(body.get("command_id") or "").strip(),
            carrier_binding_id=str(body.get("carrier_binding_id") or "").strip(),
            session_ref=str(body.get("session_ref") or "").strip(),
            action=action,
            target_ref=target_ref,
            payload=payload,
            actor=str(payload.get("actor") or body.get("actor") or "").strip(),
        )
        with self.server.challenge_lock:
            session_actor = self.server.validated_sessions.get(command.session_ref)
        if session_actor != command.actor:
            raise StateTransitionError("this browser session has not completed its own user round-trip validation")
        try:
            return HumanDecisionCommandService(core=core).execute(
                command=command, handler=handler
            )
        finally:
            if action not in {"confirm_cold_start_summary", "run_competitor_breakdown"}:
                self.server.schedule_knowledge_mirror(
                    f"formal_human_action:{action}"
                )

    def _confirm(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        preview_token = str(body.get("preview_token") or "").strip()
        core = self._open_core()
        try:
            service = self._service(core)
            return self._formal_command(
                core=core, body=body, action="confirm_cold_start_configuration",
                target_ref=preview_token, payload=payload,
                handler=lambda command: service.confirm(command.payload, preview_token=preview_token),
            )
        finally:
            core.close()

    def _preflight(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        core = self._open_core()
        try:
            return self._service(core).inspect_configuration(configuration_id)
        finally:
            core.close()

    def _start(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        core = self._open_core()
        try:
            service = self._service(core)
            command_result = self._formal_command(
                core=core, body=body, action="start_confirmed_cold_start",
                target_ref=configuration_id, payload={"configuration_id": configuration_id, "actor": actor},
                handler=lambda command: service.start(
                    configuration_id=configuration_id, actor=command.actor,
                ),
            )
            result = command_result.get("result") if isinstance(command_result.get("result"), dict) else {}
            registration_ids = result.get("registration_ids", [])
            if not isinstance(registration_ids, list) or not all(isinstance(item, str) for item in registration_ids):
                raise StateTransitionError("cold-start resume did not return a valid registration queue")
            progress = service.progress(configuration_id)
            resumable_ids = {
                str(item["registration_id"])
                for item in progress["registrations"]
                if item["status"] != "completed"
            }
            queued_registration_ids = [
                registration_id for registration_id in registration_ids
                if registration_id in resumable_ids
            ]
            self.server._clear_collection_block()
            for registration_id in queued_registration_ids:
                self.server._clear_paused_job(registration_id)
                self.server.schedule_registration(str(registration_id))
            command_result["scheduled_registration_count"] = len(queued_registration_ids)
            return command_result
        finally:
            core.close()

    def _run_competitor_breakdown(self, body: dict[str, Any]) -> dict[str, Any]:
        registration_id = str(body.get("registration_id") or "").strip()
        source_id = str(body.get("source_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not registration_id or not source_id or not actor:
            raise StateTransitionError("competitor breakdown needs one registration, one source and the confirming user")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="run_competitor_breakdown",
                target_ref=f"{registration_id}:{source_id}",
                payload={
                    "registration_id": registration_id,
                    "source_id": source_id,
                    "actor": actor,
                },
                handler=lambda command: self.server.schedule_user_authorized_atomic_breakdown(
                    command_id=command.command_id,
                    registration_id=registration_id,
                    source_id=source_id,
                ),
            )
        finally:
            core.close()

    def _competitor_breakdown_progress(self, body: dict[str, Any]) -> dict[str, Any]:
        command_id = str(body.get("command_id") or "").strip()
        session_ref = str(body.get("session_ref") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not command_id or not session_ref or not actor:
            raise StateTransitionError("competitor breakdown progress needs its command, session and user")
        with self.server.challenge_lock:
            if self.server.validated_sessions.get(session_ref) != actor:
                raise StateTransitionError("this session has not completed its own user round-trip validation")
        return self.server.atomic_breakdown_progress(command_id=command_id)

    def _run_formal_breakdown_backlog(self, body: dict[str, Any]) -> dict[str, Any]:
        approval = self._require_test_approval(body)
        if approval.get("kind") != "natural_dialogue":
            raise StateTransitionError("formal breakdown backlog needs the user's natural-dialogue confirmation")
        cold_start_id = str(body.get("cold_start_id") or "").strip()
        expected_pending_count = body.get("expected_pending_count")
        if not cold_start_id or not isinstance(expected_pending_count, int):
            raise StateTransitionError("formal breakdown backlog needs its cold-start identity and confirmed pending count")
        core = self._open_core()
        try:
            task = core.create_competitor_breakdown_backlog_task(
                cold_start_id=cold_start_id,
                approval=approval,
                expected_pending_count=expected_pending_count,
            )
        finally:
            core.close()
        job = self.server.schedule_formal_breakdown_backlog_task(
            backlog_task_id=str(task["backlog_task_id"]),
        )
        return {"job": job, "task": task}

    def _formal_breakdown_backlog_progress(self, body: dict[str, Any]) -> dict[str, Any]:
        approval = self._require_test_approval(body)
        backlog_task_id = str(body.get("backlog_task_id") or "").strip()
        if not backlog_task_id:
            raise StateTransitionError("formal breakdown backlog progress needs its task identity")
        core = self._open_core()
        try:
            task = core.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        finally:
            core.close()
        if task.get("approval") != approval:
            raise StateTransitionError("this formal breakdown backlog belongs to another confirmed conversation")
        return self.server.formal_breakdown_backlog_progress(backlog_task_id=backlog_task_id)

    def _run_competitor_breakdown_test(self, body: dict[str, Any]) -> dict[str, Any]:
        approval = self._require_test_approval(body)
        raw_source_ids = body.get("source_ids")
        if not isinstance(raw_source_ids, list):
            raise StateTransitionError("competitor breakdown test needs the materials selected by the user")
        source_ids = [str(value).strip() for value in raw_source_ids if str(value).strip()]
        if len(source_ids) != len(raw_source_ids):
            raise StateTransitionError("competitor breakdown test material identifiers are invalid")
        core = self._open_core()
        try:
            materials = self._selected_breakdown_test_materials(core=core, source_ids=source_ids)
        finally:
            core.close()
        test_id = f"competitor_breakdown_test_{uuid.uuid4().hex}"
        return self.server.schedule_competitor_breakdown_test(
            test_id=test_id,
            materials=materials,
            approval=approval,
        )

    def _competitor_breakdown_test_progress(self, body: dict[str, Any]) -> dict[str, Any]:
        approval = self._require_test_approval(body)
        test_id = str(body.get("test_id") or "").strip()
        if not test_id:
            raise StateTransitionError("competitor breakdown test progress needs its test id")
        result = self.server.competitor_breakdown_test_progress(
            test_id=test_id,
            include_raw=body.get("include_raw") is True,
        )
        if result.get("approval") != approval:
            raise StateTransitionError("this test belongs to another confirmed conversation")
        return result

    def _progress(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        core = self._open_core()
        try:
            result = self._service(core).progress(configuration_id)
            job_ids = [item["registration_id"] for item in result["registrations"]]
            result["tag_candidates"] = {
                item["registration_id"]: core.list_competitor_registration_tag_candidates(
                    registration_id=item["registration_id"]
                )
                for item in result["registrations"]
            }
            result["tag_library"] = (
                core.get_cold_start_tag_library(cold_start_id=str(result["cold_start_id"]))
                if result["cold_start_id"]
                else None
            )
            with self.server.challenge_lock:
                result["automatic_jobs"] = {
                    job_id: dict(self.server.jobs[job_id])
                    for job_id in job_ids if job_id in self.server.jobs
                }
            result["collection_protection"] = self.server.collection_protection_view()
            return result
        finally:
            core.close()

    def _review_tag_library(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        decisions = body.get("decisions")
        actor = str(body.get("actor") or "").strip()
        if not configuration_id or not isinstance(decisions, list):
            raise StateTransitionError("tag-library review requires the complete proposed library")
        core = self._open_core()
        try:
            progress = self._service(core).progress(configuration_id)
            if not progress["registrations"] or any(
                "high_signal_identification" not in item.get("completed_steps", {})
                for item in progress["registrations"]
            ):
                raise StateTransitionError(
                    "the tag library can be reviewed only after every account completes baseline and hit filtering"
                )

            def review_library(command: FormalHumanDecisionCommand) -> dict[str, Any]:
                return core.review_cold_start_tag_library(
                    cold_start_id=str(progress["cold_start_id"]),
                    decisions=tuple(decisions),
                    actor=command.actor,
                    actor_kind="user",
                    reason="用户已一次审核整版领域话题标签库",
                )

            command_result = self._formal_command(
                core=core,
                body=body,
                action="review_competitor_tag_library",
                target_ref=configuration_id,
                payload={"configuration_id": configuration_id, "decisions": decisions, "actor": actor},
                handler=review_library,
            )
            return command_result
        finally:
            core.close()

    def _delete_tag_library_item(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        tag_id = str(body.get("tag_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not configuration_id or not tag_id:
            raise StateTransitionError("tag deletion requires a configuration and tag")
        core = self._open_core()
        try:
            progress = self._service(core).progress(configuration_id)
            cold_start_id = str(progress.get("cold_start_id") or "")
            if not cold_start_id:
                raise StateTransitionError("the cold start does not exist")
            return self._formal_command(
                core=core,
                body=body,
                action="delete_cold_start_tag",
                target_ref=tag_id,
                payload={
                    "configuration_id": configuration_id,
                    "tag_id": tag_id,
                    "actor": actor,
                },
                handler=lambda command: core.delete_pending_cold_start_tag(
                    cold_start_id=cold_start_id,
                    tag_id=tag_id,
                    actor=command.actor,
                    actor_kind="user",
                    reason="用户在整版领域标签库中明确点击删除",
                ),
            )
        finally:
            core.close()

    def _revise_tag_library(self, body: dict[str, Any]) -> dict[str, Any]:
        configuration_id = str(body.get("configuration_id") or "").strip()
        deleted_tag_ids = body.get("deleted_tag_ids")
        actor = str(body.get("actor") or "").strip()
        if not configuration_id or not isinstance(deleted_tag_ids, list):
            raise StateTransitionError("accepted tag-library revision requires confirmed deletions")
        core = self._open_core()
        try:
            progress = self._service(core).progress(configuration_id)
            cold_start_id = str(progress.get("cold_start_id") or "")
            if not cold_start_id:
                raise StateTransitionError("the cold start does not exist")
            return self._formal_command(
                core=core,
                body=body,
                action="review_competitor_tag_library",
                target_ref=configuration_id,
                payload={
                    "configuration_id": configuration_id,
                    "deleted_tag_ids": deleted_tag_ids,
                    "actor": actor,
                },
                handler=lambda command: core.revise_accepted_cold_start_tag_library(
                    cold_start_id=cold_start_id,
                    deleted_tag_ids=tuple(str(item) for item in deleted_tag_ids),
                    actor=command.actor,
                    actor_kind="user",
                    reason="用户确认删除已审核领域话题标签",
                ),
            )
        finally:
            core.close()

    def _run_daily(self, body: dict[str, Any]) -> dict[str, Any]:
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        scope = str(body.get("scope") or "").strip()
        if not actor or not reason or scope != "single_account_smoke":
            raise StateTransitionError(
                "manual daily validation requires a user, reason and scope=single_account_smoke"
            )
        core = self._open_core()
        try:
            command_result = self._formal_command(
                core=core,
                body=body,
                action="run_daily_operations",
                target_ref="single_music_account_validation",
                payload={"actor": actor, "reason": reason, "scope": scope},
                handler=lambda command: {
                    "scope": scope,
                    "status": "authorized",
                    "actor": command.actor,
                    "reason": reason,
                },
            )
            command_result["daily_operations"] = self.server.daily_operations.schedule_all(
                domain_labels=tuple(self.server.daily_operations.domain_provider()),
                trigger="user_authorized_run",
                force=True,
                attempt_ref=str(body.get("command_id") or "").strip(),
                validation_only=True,
            )
            return command_result
        finally:
            core.close()

    def _select_daily_candidate(self, body: dict[str, Any]) -> dict[str, Any]:
        candidate_version_id = str(body.get("candidate_version_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if not candidate_version_id or not actor or not reason:
            raise StateTransitionError("daily candidate selection requires a candidate, user and reason")
        core = self._open_core()
        try:
            service = Stage1BDailyDiscoveryService(
                core=core,
                gateway=build_production_daily_discovery_gateway(core),
            )
            return self._formal_command(
                core=core,
                body=body,
                action="select_discovery_candidate",
                target_ref=candidate_version_id,
                payload={
                    "candidate_version_id": candidate_version_id,
                    "actor": actor,
                    "reason": reason,
                },
                handler=lambda command: service.handoff_selected_candidate(
                    candidate_version_id=candidate_version_id,
                    actor=command.actor,
                    reason=reason,
                    idempotency_key=f"ui-select:{candidate_version_id}",
                ),
            )
        finally:
            core.close()

    def _review_two_week_tags(self, body: dict[str, Any]) -> dict[str, Any]:
        review_id = str(body.get("tag_review_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        decisions = body.get("decisions")
        if (
            not review_id
            or not actor
            or not reason
            or not isinstance(decisions, list)
        ):
            raise StateTransitionError(
                "two-week tag-library review requires the whole library and a user reason"
            )
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="review_two_week_tag_library",
                target_ref=review_id,
                payload={
                    "tag_review_id": review_id,
                    "decisions": decisions,
                    "actor": actor,
                    "reason": reason,
                },
                handler=lambda command: core.review_two_week_tag_library(
                    tag_review_id=review_id,
                    decisions=tuple(
                        item for item in decisions if isinstance(item, dict)
                    ),
                    actor=command.actor,
                    actor_kind="user",
                    reason=reason,
                ),
            )
        finally:
            core.close()

    def _register_manual_source(self, body: dict[str, Any]) -> dict[str, Any]:
        domain_label = str(body.get("domain_label") or "").strip()
        account_ref = str(body.get("account_ref") or "").strip()
        source_kind = str(body.get("source_kind") or "").strip()
        original_content = body.get("original_content")
        actor = str(body.get("actor") or "").strip()
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        if (
            not domain_label
            or source_kind not in {"direction", "link", "person", "work", "playlist"}
            or original_content is None
            or not str(original_content).strip()
            or not actor
            or (
                source_kind in {"person", "work", "playlist"}
                and not account_ref
            )
        ):
            raise StateTransitionError(
                "manual source requires a domain, source type, original content and user; person, work and playlist exploration also require the service account"
            )
        if source_kind == "playlist":
            work_names = [
                item.strip()
                for item in re.split(r"[\r\n,，;；、]+", str(original_content))
                if item.strip()
            ]
            if len(work_names) < 10:
                raise StateTransitionError(
                    "playlist exploration requires at least ten works and one shared question or angle"
                )
            if not str(scope.get("requested_scope") or "").strip():
                raise StateTransitionError(
                    "playlist exploration requires one shared question or angle"
                )
        exploration_kind = {
            "person": "person_exploration",
            "work": "work_exploration",
            "playlist": "playlist_exploration",
        }.get(source_kind)
        if exploration_kind:
            enforce_manual_exploration_runtime_guard(
                entrypoint="local_visual_business_ui",
                exploration_kind=exploration_kind,
                action="route",
                work_count=(
                    len(work_names) if source_kind == "playlist" else None
                ),
            )
        core = self._open_core()
        try:
            command_id = str(body.get("command_id") or "").strip()

            def register(command: FormalHumanDecisionCommand) -> dict[str, Any]:
                if source_kind == "direction":
                    return core.register_saved_user_direction_source(
                        direction_id=f"direction_{command_id}",
                        domain_label=domain_label,
                        core_question=str(original_content),
                        submitted_by=command.actor,
                        account_ref=account_ref or None,
                    )
                manual = core.register_manual_source(
                    manual_source_id=f"manual_source_{command_id}",
                    domain_label=domain_label,
                    account_ref=account_ref or None,
                    source_kind=source_kind,
                    original_content=original_content,
                    submitted_by=command.actor,
                )
                if source_kind == "link":
                    return {**manual, "status": "retained_for_clarification"}
                exploration = core.route_manual_source_to_exploration(
                    manual_source_id=manual["manual_source_id"],
                    actor=command.actor,
                    actor_kind="user",
                    scope={
                        **scope,
                        "original_content": original_content,
                        "account_ref": account_ref or None,
                    },
                )
                return {
                    **manual,
                    **exploration,
                    "next_step": "agent_material_collection",
                    "human_confirmation_required_now": False,
                }

            return self._formal_command(
                core=core,
                body=body,
                action="record_manual_source",
                target_ref=f"{source_kind}:{command_id}",
                payload={
                    "domain_label": domain_label,
                    "account_ref": account_ref,
                    "source_kind": source_kind,
                    "original_content": original_content,
                    "scope": scope,
                    "actor": actor,
                },
                handler=register,
            )
        finally:
            core.close()

    def _collect_person_exploration(self, body: dict[str, Any]) -> dict[str, Any]:
        exploration_id = str(body.get("exploration_id") or "").strip()
        person_name = str(body.get("person_name") or "").strip()
        actor = str(body.get("actor") or "").strip()
        raw_works = body.get("works")
        if (
            not exploration_id
            or not person_name
            or not actor
            or not isinstance(raw_works, list)
            or not 10 <= len(raw_works) <= 12
            or any(
                not isinstance(item, dict)
                or not str(item.get("title") or "").strip()
                for item in raw_works
            )
        ):
            raise StateTransitionError(
                "person exploration requires the person, actor and one complete 10-12 work scan list"
            )
        core = self._open_core()
        try:
            packet = core.get_manual_exploration_packet(
                exploration_id=exploration_id
            )
            if packet["exploration_kind"] != "person_exploration":
                raise StateTransitionError(
                    "only a person exploration may start the music audience collector"
                )
            enforce_manual_exploration_runtime_guard(
                entrypoint="manual_exploration_worker",
                exploration_kind=str(packet["exploration_kind"]),
                action="collect_person_materials",
                work_count=len(raw_works),
            )
        finally:
            core.close()
        return self.server.schedule_person_exploration(
            exploration_id=exploration_id,
            person_name=person_name,
            works=tuple(dict(item) for item in raw_works),
            actor=actor,
        )

    def _record_exploration_material(self, body: dict[str, Any]) -> dict[str, Any]:
        exploration_id = str(body.get("exploration_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        source_ref = str(body.get("source_ref") or "").strip()
        title = str(body.get("title") or "").strip()
        retained_text = str(body.get("retained_text") or "").strip()
        evidence_role = str(body.get("evidence_role") or "").strip()
        if not all((exploration_id, actor, source_ref, title, retained_text)):
            raise StateTransitionError(
                "exploration material requires the exploration, actor, traceable source, title and retained content"
            )
        core = self._open_core()
        try:
            packet = core.get_manual_exploration_packet(
                exploration_id=exploration_id
            )
            enforce_manual_exploration_runtime_guard(
                entrypoint="agent_exploration_material_entry",
                exploration_kind=str(packet["exploration_kind"]),
                action="record_material",
            )
            if packet["status"] == "awaiting_material_collection":
                core.begin_manual_exploration_collection(
                    exploration_id=exploration_id,
                    actor=actor,
                    reason="the assigned agent started collecting traceable exploration materials",
                )
            elif packet["status"] != "collecting":
                raise StateTransitionError(
                    "this exploration is not accepting more materials"
                )
            return core.record_manual_exploration_material(
                exploration_id=exploration_id,
                material_ref={
                    "source_ref": source_ref,
                    "title": title,
                    "retained_text": retained_text,
                    "source_url": str(body.get("source_url") or source_ref).strip(),
                },
                evidence_role=evidence_role,
                collected_at=str(body.get("collected_at") or _utc_now()).strip(),
            )
        finally:
            core.close()

    def _complete_exploration_collection(
        self, body: dict[str, Any]
    ) -> dict[str, Any]:
        exploration_id = str(body.get("exploration_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if not all((exploration_id, actor, reason)):
            raise StateTransitionError(
                "exploration completion requires the exploration, actor and a material-based conclusion"
            )
        core = self._open_core()
        try:
            packet = core.get_manual_exploration_packet(
                exploration_id=exploration_id
            )
            enforce_manual_exploration_runtime_guard(
                entrypoint="agent_exploration_completion_entry",
                exploration_kind=str(packet["exploration_kind"]),
                action="complete_collection",
            )
            result = core.complete_manual_exploration_collection(
                exploration_id=exploration_id,
                actor=actor,
                reason=reason,
            )
        finally:
            core.close()
        self.server.schedule_knowledge_mirror(
            f"manual_exploration_ready:{exploration_id}"
        )
        return result

    def _confirm_exploration_direction(
        self, body: dict[str, Any]
    ) -> dict[str, Any]:
        exploration_id = str(body.get("exploration_id") or "").strip()
        core_question = str(body.get("core_question") or "").strip()
        scope_or_requirement = str(
            body.get("scope_or_requirement") or ""
        ).strip()
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if not all(
            (
                exploration_id,
                core_question,
                scope_or_requirement,
                actor,
                reason,
            )
        ):
            raise StateTransitionError(
                "the final exploration direction requires a question, scope and user reason"
            )
        core = self._open_core()
        try:
            packet = core.get_manual_exploration_packet(
                exploration_id=exploration_id
            )
            if packet["status"] != "awaiting_human_direction":
                raise StateTransitionError(
                    "the exploration has not completed its material scan"
                )
            if not str(packet.get("account_ref") or "").strip():
                raise StateTransitionError(
                    "the exploration has no service account and cannot create a formal topic"
                )
            enforce_manual_exploration_runtime_guard(
                entrypoint="formal_human_direction_entry",
                exploration_kind=str(packet["exploration_kind"]),
                action="confirm_direction",
            )
            materials = [
                {
                    "source_ref": item["material"],
                    "evidence_role": item["evidence_role"],
                    "collected_at": item["collected_at"],
                }
                for item in packet["materials"]
            ]

            def confirm(command: FormalHumanDecisionCommand) -> dict[str, Any]:
                service = Stage1AResearchPlanService(
                    core=core,
                    gateway=build_research_plan_gateway(core),
                )
                created = service.create_direct_formal_topic_and_generate_plan(
                    domain_label=str(packet["domain_label"]),
                    account_ref=str(packet["account_ref"]),
                    core_question=core_question,
                    scope_or_requirement=scope_or_requirement,
                    original_instruction={
                        "exploration_id": exploration_id,
                        "manual_origin": packet["manual_origin"],
                        "selected_direction": core_question,
                        "confirmation_reason": reason,
                    },
                    actor=command.actor,
                    existing_manual_source_ids=(
                        str(packet["manual_source_id"]),
                    ),
                    known_materials=materials,
                    material_gaps=[
                        "formal research must independently verify facts and fill any gaps identified by the exploration"
                    ],
                    idempotency_key=f"exploration-direction:{exploration_id}",
                )
                closed = core.complete_manual_exploration_direction(
                    exploration_id=exploration_id,
                    formal_topic_id=created["task_id"],
                    actor=command.actor,
                    reason=reason,
                )
                return {**created, "exploration": closed}

            return self._formal_command(
                core=core,
                body=body,
                action="confirm_exploration_direction",
                target_ref=exploration_id,
                payload={
                    "exploration_id": exploration_id,
                    "core_question": core_question,
                    "scope_or_requirement": scope_or_requirement,
                    "reason": reason,
                    "actor": actor,
                },
                handler=confirm,
            )
        finally:
            core.close()

    def _create_direct_formal_topic(self, body: dict[str, Any]) -> dict[str, Any]:
        domain_label = str(body.get("domain_label") or "").strip()
        account_ref = str(body.get("account_ref") or "").strip()
        core_question = str(body.get("core_question") or "").strip()
        scope = str(body.get("scope_or_requirement") or "").strip()
        original_instruction = body.get("original_instruction")
        actor = str(body.get("actor") or "").strip()
        if not all(
            (
                domain_label,
                account_ref,
                core_question,
                scope,
                str(original_instruction or "").strip(),
                actor,
            )
        ):
            raise StateTransitionError(
                "direct formal topic requires account, domain, core question, scope and original instruction"
            )
        core = self._open_core()
        try:
            command_id = str(body.get("command_id") or "").strip()
            service = Stage1AResearchPlanService(
                core=core,
                gateway=build_research_plan_gateway(core),
            )
            return self._formal_command(
                core=core,
                body=body,
                action="confirm_formal_topic",
                target_ref=f"direct-topic:{command_id}",
                payload={
                    "domain_label": domain_label,
                    "account_ref": account_ref,
                    "core_question": core_question,
                    "scope_or_requirement": scope,
                    "original_instruction": original_instruction,
                    "actor": actor,
                },
                handler=lambda command: service.create_direct_formal_topic_and_generate_plan(
                    domain_label=domain_label,
                    account_ref=account_ref,
                    core_question=core_question,
                    scope_or_requirement=scope,
                    original_instruction=original_instruction,
                    actor=command.actor,
                    idempotency_key=f"ui-direct-topic:{command_id}",
                ),
            )
        finally:
            core.close()

    def _decide_research_plan(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("task_id") or "").strip()
        version_id = str(body.get("research_plan_version_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if (
            not task_id
            or not version_id
            or decision not in {"approved", "returned"}
            or not reason
            or not actor
        ):
            raise StateTransitionError(
                "research-plan decision requires the task, plan, decision, user and reason"
            )
        core = self._open_core()
        try:
            service = Stage1AResearchPlanService(
                core=core,
                gateway=build_research_plan_gateway(core),
            )

            def decide(command: FormalHumanDecisionCommand) -> dict[str, Any]:
                if decision == "approved":
                    return service.approve_research_plan(
                        task_id=task_id,
                        research_plan_version_id=version_id,
                        actor=command.actor,
                        reason=reason,
                        idempotency_key=f"ui-research-plan:{body['command_id']}:approve",
                    )
                return service.return_research_plan(
                    task_id=task_id,
                    research_plan_version_id=version_id,
                    modification_requirements=reason,
                    actor=command.actor,
                    idempotency_key=f"ui-research-plan:{body['command_id']}:return",
                )

            current_task = core.get_task(task_id)
            enforce_content_production_runtime_guard(
                entrypoint="research_plan_decision",
                current_node=str(current_task["current_node"]),
                current_status=str(current_task["current_status"]),
            )
            receipt = self._formal_command(
                core=core,
                body=body,
                action=(
                    "approve_research_plan"
                    if decision == "approved"
                    else "return_research_plan"
                ),
                target_ref=version_id,
                payload={
                    "task_id": task_id,
                    "research_plan_version_id": version_id,
                    "decision": decision,
                    "reason": reason,
                    "actor": actor,
                },
                handler=decide,
            )
            if decision == "returned":
                try:
                    receipt["automatic_advance"] = service.generate_research_plan(
                        task_id=task_id,
                        user_requirements=None,
                        actor=actor,
                        idempotency_key=f"ui-research-plan:{body['command_id']}:regenerate",
                    )
                except Exception as exc:
                    receipt["automatic_advance"] = {
                        "status": "failed",
                        "reason": str(exc),
                    }
            else:
                task = core.get_task(task_id)
                enforce_content_production_runtime_guard(
                    entrypoint="research_plan_approved",
                    current_node=str(task["current_node"]),
                    current_status=str(task["current_status"]),
                )
                materials = core.list_research_materials(task_id=task_id)
                if materials:
                    pipeline = Stage1CContentPipelineService(
                        core=core,
                        gateway=build_production_content_pipeline_gateway(core),
                    )
                    try:
                        receipt["automatic_advance"] = (
                            pipeline.advance_to_next_human_gate(
                                task_id=task_id,
                                actor=actor,
                                user_requirements=reason,
                                idempotency_key=f"ui-research-plan:{body['command_id']}:start-research",
                            )
                        )
                    except Exception as exc:
                        receipt["automatic_advance"] = {
                            "status": "failed",
                            "reason": str(exc),
                        }
                else:
                    receipt["automatic_advance"] = {
                        "status": "waiting_for_research_materials",
                        "reason": "formal research requires retained traceable web or file evidence",
                    }
            return receipt
        finally:
            core.close()

    def _record_research_material(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("task_id") or "").strip()
        source_ref = str(body.get("source_ref") or "").strip()
        title = str(body.get("title") or "").strip()
        evidence_role = str(body.get("evidence_role") or "").strip()
        material_text = str(body.get("material_text") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not all((task_id, source_ref, title, evidence_role, material_text, actor)):
            raise StateTransitionError(
                "research material requires task, source, title, role, retained text and user"
            )
        core = self._open_core()
        try:
            task = core.get_task(task_id)
            enforce_content_production_runtime_guard(
                entrypoint="record_research_material",
                current_node=str(task["current_node"]),
                current_status=str(task["current_status"]),
            )
            return self._formal_command(
                core=core,
                body=body,
                action="record_research_material",
                target_ref=task_id,
                payload={
                    "task_id": task_id,
                    "source_ref": source_ref,
                    "title": title,
                    "evidence_role": evidence_role,
                    "material_text": material_text,
                    "actor": actor,
                },
                handler=lambda command: core.record_research_material(
                    task_id=task_id,
                    source_ref=source_ref,
                    title=title,
                    evidence_role=evidence_role,
                    material={"retained_text": material_text},
                    collected_at=_utc_now(),
                ),
            )
        finally:
            core.close()

    def _generate_content(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("task_id") or "").strip()
        requirements = str(body.get("requirements") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not all((task_id, requirements, actor)):
            raise StateTransitionError(
                "content generation requires task, explicit requirements and user"
            )
        core = self._open_core()
        try:
            task = core.get_task(task_id)
            enforce_content_production_runtime_guard(
                entrypoint="content_generation",
                current_node=str(task["current_node"]),
                current_status=str(task["current_status"]),
            )
            pipeline = Stage1CContentPipelineService(
                core=core,
                gateway=build_production_content_pipeline_gateway(core),
            )
            return self._formal_command(
                core=core,
                body=body,
                action="advance_content_step",
                target_ref=task_id,
                payload={
                    "task_id": task_id,
                    "requirements": requirements,
                    "actor": actor,
                },
                handler=lambda command: pipeline.advance_formal_content(
                    task_id=task_id,
                    actor=command.actor,
                    user_requirements=requirements,
                    idempotency_key=f"ui-content-generate:{body['command_id']}",
                ),
            )
        finally:
            core.close()

    def _decide_experience_candidate(self, body: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(body.get("experience_candidate_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not candidate_id or decision not in {"accepted", "rejected"} or not reason or not actor:
            raise StateTransitionError("experience candidate decision requires candidate, accept or reject, user and reason")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="accept_experience_candidate" if decision == "accepted" else "reject_experience_candidate",
                target_ref=candidate_id,
                payload={"experience_candidate_id": candidate_id, "decision": decision, "reason": reason, "actor": actor},
                handler=lambda command: core.decide_experience_candidate(
                    experience_candidate_id=candidate_id, decision=decision, actor=command.actor,
                    actor_kind="user", reason=reason,
                ),
            )
        finally:
            core.close()

    def _decide_content(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("task_id") or "").strip()
        version_id = str(body.get("version_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if (
            not task_id
            or not version_id
            or decision not in {"approved", "returned"}
            or not reason
            or not actor
        ):
            raise StateTransitionError(
                "content decision requires task, version, decision, reason and user"
            )
        core = self._open_core()
        try:
            task = core.get_task(task_id)
            node = str(task["current_node"])
            action_by_node = {
                "deep_research": (
                    "approve_research_result",
                    "return_research_result",
                ),
                "content_plan": ("approve_content_plan", "return_content_plan"),
                "formal_draft": ("approve_draft", "return_draft"),
                "review": ("approve_final_content", "return_final_content"),
            }
            if node not in action_by_node:
                raise StateTransitionError(
                    "the current content step is not a formal human confirmation point"
                )
            enforce_content_production_runtime_guard(
                entrypoint="content_decision",
                current_node=node,
                current_status=str(task["current_status"]),
            )
            pipeline = Stage1CContentPipelineService(
                core=core,
                gateway=build_production_content_pipeline_gateway(core),
            )

            def decide(command: FormalHumanDecisionCommand) -> dict[str, Any]:
                if decision == "approved":
                    return pipeline.approve(
                        task_id=task_id,
                        version_id=version_id,
                        actor=command.actor,
                        reason=reason,
                        idempotency_key=f"ui-content-decision:{body['command_id']}:approve",
                    )
                return pipeline.return_for_revision(
                    task_id=task_id,
                    version_id=version_id,
                    actor=command.actor,
                    requirements=reason,
                    idempotency_key=f"ui-content-decision:{body['command_id']}:return",
                )

            receipt = self._formal_command(
                core=core,
                body=body,
                action=action_by_node[node][0 if decision == "approved" else 1],
                target_ref=version_id,
                payload={
                    "task_id": task_id,
                    "version_id": version_id,
                    "decision": decision,
                    "reason": reason,
                    "actor": actor,
                },
                handler=decide,
            )
            refreshed = core.get_task(task_id)
            if (
                refreshed["current_node"] != "user_final_confirmation"
                or refreshed["current_status"] != "approved"
            ):
                try:
                    receipt["automatic_advance"] = (
                        pipeline.advance_formal_content(
                            task_id=task_id,
                            actor=actor,
                            user_requirements=reason,
                            idempotency_key=f"ui-content-decision:{body['command_id']}:advance",
                        )
                    )
                except Exception as exc:
                    receipt["automatic_advance"] = {
                        "status": "failed",
                        "reason": str(exc),
                    }
            return receipt
        finally:
            core.close()

    def _configure_voice_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        label = str(body.get("profile_label") or "").strip()
        reference = str(body.get("reference_audio_ref") or "").strip()
        prompt_text = str(body.get("prompt_text") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not all((label, reference, actor)):
            raise StateTransitionError(
                "voice configuration requires a label, reference WAV and user"
            )
        reference_path = Path(reference).expanduser().resolve()
        if not reference_path.is_file() or reference_path.suffix.lower() != ".wav":
            raise StateTransitionError(
                "voice reference must be an existing local WAV file"
            )
        profile_id = f"voice_{uuid.uuid4().hex}"
        settings = {
            "style_prompt": str(body.get("style_prompt") or "").strip(),
            "emotion_preset": str(body.get("emotion_preset") or "neutral").strip(),
            "emotion_strength": float(body.get("emotion_strength") or 1.0),
            "segment_strategy": "auto",
            "max_chars": int(body.get("max_chars") or 220),
            "silence_ms": int(body.get("silence_ms") or 350),
            "cfg_value": float(body.get("cfg_value") or 2.0),
            "inference_timesteps": int(body.get("inference_timesteps") or 10),
        }
        core = self._open_core()
        try:
            enforce_content_production_runtime_guard(
                entrypoint="voice_profile_configuration",
                current_node="audio_production",
                current_status="not_started",
            )
            return self._formal_command(
                core=core,
                body=body,
                action="confirm_voice_profile",
                target_ref=profile_id,
                payload={
                    "voice_profile_id": profile_id,
                    "profile_label": label,
                    "reference_audio_ref": str(reference_path),
                    "actor": actor,
                },
                handler=lambda command: {
                    "submitted": core.submit_voice_profile(
                        voice_profile_id=profile_id,
                        profile_label=label,
                        reference_audio_ref=str(reference_path),
                        emotion_reference_audio_ref=None,
                        mode="basic",
                        prompt_text=prompt_text,
                        emotion_prompt_text="",
                        settings=settings,
                        actor=command.actor,
                        actor_kind="user",
                        idempotency_key=f"ui-voice:{body['command_id']}:submit",
                    ),
                    "confirmed": core.confirm_voice_profile(
                        voice_profile_id=profile_id,
                        actor=command.actor,
                        actor_kind="user",
                        idempotency_key=f"ui-voice:{body['command_id']}:confirm",
                    ),
                },
            )
        finally:
            core.close()

    def _produce_audio(self, body: dict[str, Any]) -> dict[str, Any]:
        task_id = str(body.get("task_id") or "").strip()
        version_id = str(body.get("approved_content_version_id") or "").strip()
        voice_profile_id = str(body.get("voice_profile_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not all((task_id, version_id, voice_profile_id, actor)):
            raise StateTransitionError(
                "audio production requires task, approved content, voice profile and user"
            )
        core = self._open_core()
        try:
            task = core.get_task(task_id)
            enforce_content_production_runtime_guard(
                entrypoint="audio_production",
                current_node=str(task["current_node"]),
                current_status=str(task["current_status"]),
            )
            service = build_production_audio_service(core)
            return self._formal_command(
                core=core,
                body=body,
                action="start_audio_production",
                target_ref=version_id,
                payload={
                    "task_id": task_id,
                    "approved_content_version_id": version_id,
                    "voice_profile_id": voice_profile_id,
                    "actor": actor,
                },
                handler=lambda command: service.produce(
                    task_id=task_id,
                    approved_content_version_id=version_id,
                    voice_profile_id=voice_profile_id,
                    actor=command.actor,
                    idempotency_key=f"ui-audio:{body['command_id']}:produce",
                ),
            )
        finally:
            core.close()

    def _change_workflow_mode(self, body: dict[str, Any]) -> dict[str, Any]:
        domain_label = str(body.get("domain_label") or "").strip()
        workflow_mode = str(body.get("workflow_mode") or "").strip()
        actor = str(body.get("actor") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if not domain_label or workflow_mode not in {"manual_guard", "mature_automatic"}:
            raise StateTransitionError(
                "workflow mode change requires a domain and a supported mode"
            )
        if not actor or not reason:
            raise StateTransitionError(
                "workflow mode change requires the user and a reason"
            )
        core = self._open_core()
        try:
            service = self._service(core)
            return self._formal_command(
                core=core,
                body=body,
                action="change_domain_workflow_mode",
                target_ref=domain_label,
                payload={
                    "domain_label": domain_label,
                    "workflow_mode": workflow_mode,
                    "actor": actor,
                    "reason": reason,
                },
                handler=lambda command: service.change_workflow_mode(
                    domain_label=domain_label,
                    workflow_mode=workflow_mode,
                    actor=command.actor,
                    reason=reason,
                ),
            )
        finally:
            core.close()

    def _register_publication(self, body: dict[str, Any]) -> dict[str, Any]:
        publication_id = str(body.get("publication_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not publication_id or not actor:
            raise StateTransitionError("publication registration requires publication and user")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="register_external_publication",
                target_ref=publication_id,
                payload={
                    "publication_id": publication_id,
                    "domain_label": str(body.get("domain_label") or "").strip(),
                    "content_account_id": str(body.get("content_account_id") or "").strip(),
                    "external_video_url": str(body.get("external_video_url") or "").strip(),
                    "published_at": str(body.get("published_at") or "").strip(),
                    "actual_content_status": str(body.get("actual_content_status") or "").strip(),
                    "actor": actor,
                },
                handler=lambda command: register_publication(
                    core.conn,
                    publication_id=publication_id,
                    content_account_id=str(body.get("content_account_id") or ""),
                    domain_label=str(body.get("domain_label") or ""),
                    task_id=str(body.get("task_id") or ""),
                    audio_delivery_id=str(body.get("audio_delivery_id") or ""),
                    approved_content_version_id=str(body.get("approved_content_version_id") or ""),
                    platform=str(body.get("platform") or ""),
                    external_video_url=str(body.get("external_video_url") or ""),
                    published_at=str(body.get("published_at") or ""),
                    actual_content_status=str(body.get("actual_content_status") or ""),
                    actual_content_note=str(body.get("actual_content_note") or ""),
                    confirmed_by=command.actor,
                    data_identity=core.data_identity,
                    created_by=command.actor,
                ),
            )
        finally:
            core.close()

    def _record_publication_observation(self, body: dict[str, Any]) -> dict[str, Any]:
        observation_id = str(body.get("observation_id") or "").strip()
        publication_id = str(body.get("publication_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not observation_id or not publication_id or not actor:
            raise StateTransitionError("publication observation requires observation, publication and user")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="record_publication_observation",
                target_ref=publication_id,
                payload={
                    "observation_id": observation_id,
                    "publication_id": publication_id,
                    "point_code": str(body.get("point_code") or "").strip(),
                    "observation_status": str(body.get("observation_status") or "").strip(),
                    "actor": actor,
                },
                handler=lambda command: record_observation(
                    core.conn,
                    observation_id=observation_id,
                    publication_id=publication_id,
                    point_code=str(body.get("point_code") or ""),
                    observation_status=str(body.get("observation_status") or ""),
                    metrics=body.get("metrics") if isinstance(body.get("metrics"), dict) else {},
                    missing_reason=str(body.get("missing_reason") or ""),
                    source_ref=str(body.get("source_ref") or ""),
                    observed_at=str(body.get("observed_at") or ""),
                    recorded_by=command.actor,
                    data_identity=core.data_identity,
                ),
            )
        finally:
            core.close()

    def _prepare_p7_review(self, body: dict[str, Any]) -> dict[str, Any]:
        review_id = str(body.get("review_id") or "").strip()
        publication_id = str(body.get("publication_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not review_id or not publication_id or not actor:
            raise StateTransitionError("P7 review preparation requires review, publication and user")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="prepare_p7_review",
                target_ref=publication_id,
                payload={"review_id": review_id, "publication_id": publication_id, "actor": actor},
                handler=lambda command: prepare_p7_review(
                    core.conn,
                    review_id=review_id,
                    publication_id=publication_id,
                    selection_assessment=str(body.get("selection_assessment") or ""),
                    narrative_assessment=str(body.get("narrative_assessment") or ""),
                    material_assessment=str(body.get("material_assessment") or ""),
                    external_conditions_assessment=str(body.get("external_conditions_assessment") or ""),
                    feedback_candidate=body.get("feedback_candidate") if isinstance(body.get("feedback_candidate"), dict) else {},
                    created_by=command.actor,
                    data_identity=core.data_identity,
                ),
            )
        finally:
            core.close()

    def _decide_p7_review(self, body: dict[str, Any]) -> dict[str, Any]:
        review_id = str(body.get("review_id") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if not review_id or not actor:
            raise StateTransitionError("P7 review decision requires review and user")
        core = self._open_core()
        try:
            return self._formal_command(
                core=core,
                body=body,
                action="confirm_p7_review",
                target_ref=review_id,
                payload={"review_id": review_id, "decision": str(body.get("decision") or "").strip(), "actor": actor},
                handler=lambda command: decide_p7_review(
                    core.conn,
                    review_id=review_id,
                    decision=str(body.get("decision") or ""),
                    actor=command.actor,
                    reason=str(body.get("reason") or ""),
                    data_identity=core.data_identity,
                ),
            )
        finally:
            core.close()

    def _plan_runtime_storage_relocation(self, body: dict[str, Any]) -> dict[str, Any]:
        target_root = str(body.get("target_root") or "").strip()
        if not target_root:
            raise StateTransitionError("请填写新的数据保存位置")
        running = self.server.storage_relocation_preflight()
        if running:
            return {
                "ready": False,
                "blockers": running,
                "current": runtime_storage_overview(),
            }
        plan_result = plan_relocation(target_root)
        return {
            "ready": True,
            "current": runtime_storage_overview(),
            "plan": plan_result,
            "notice": "确认后系统会复制、核验并切换到新位置，然后自动重启页面。旧位置保留，不会被删除。",
        }

    def _relocate_runtime_storage(self, body: dict[str, Any]) -> dict[str, Any]:
        target_root = str(body.get("target_root") or "").strip()
        confirmation = str(body.get("confirmation") or "").strip()
        if confirmation != "确认迁移正式数据":
            raise StateTransitionError("请明确确认迁移正式数据后再执行")
        running = self.server.storage_relocation_preflight()
        if running:
            raise StateTransitionError("；".join(running))
        receipt = relocate_active_runtime(target_root)
        self.server.restart_after_storage_relocation()
        return {
            "completed": True,
            "new_root": receipt["formal_runtime_root"],
            "verified_database": receipt["formal_database"],
            "message": "数据已完成复制与核验，页面正在自动重启并切换到新保存位置。旧位置仍完整保留。",
        }

    def _review_audio(self, body: dict[str, Any]) -> dict[str, Any]:
        production_id = str(body.get("audio_production_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        issue_scope = str(body.get("issue_scope") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "").strip()
        if (
            not production_id
            or decision not in {"approved", "returned"}
            or not issue_scope
            or not reason
            or not actor
        ):
            raise StateTransitionError(
                "audio review requires production, decision, scope, reason and user"
            )
        core = self._open_core()
        try:
            production = core.get_audio_production(
                audio_production_id=production_id
            )
            task = core.get_task(str(production["task_id"]))
            enforce_content_production_runtime_guard(
                entrypoint="audio_review",
                current_node=str(task["current_node"]),
                current_status=str(task["current_status"]),
            )
            service = build_production_audio_service(core)
            receipt = self._formal_command(
                core=core,
                body=body,
                action="approve_audio" if decision == "approved" else "return_audio",
                target_ref=production_id,
                payload={
                    "audio_production_id": production_id,
                    "decision": decision,
                    "issue_scope": issue_scope,
                    "reason": reason,
                    "actor": actor,
                },
                handler=lambda command: service.review(
                    audio_production_id=production_id,
                    decision=decision,
                    issue_scope=issue_scope,
                    reason=reason,
                    actor=command.actor,
                ),
            )
            if decision == "returned":
                try:
                    if issue_scope == "audio_regeneration":
                        receipt["automatic_advance"] = service.produce(
                            task_id=str(production["task_id"]),
                            approved_content_version_id=str(
                                production["approved_content_version_id"]
                            ),
                            voice_profile_id=str(production["voice_profile_id"]),
                            actor=actor,
                            idempotency_key=f"ui-audio:{body['command_id']}:regenerate",
                        )
                    else:
                        pipeline = Stage1CContentPipelineService(
                            core=core,
                            gateway=build_production_content_pipeline_gateway(core),
                        )
                        receipt["automatic_advance"] = (
                            pipeline.advance_to_next_human_gate(
                                task_id=str(production["task_id"]),
                                actor=actor,
                                user_requirements=reason,
                                idempotency_key=f"ui-audio:{body['command_id']}:content-revision",
                            )
                        )
                except Exception as exc:
                    receipt["automatic_advance"] = {
                        "status": "failed",
                        "reason": str(exc),
                    }
            return receipt
        finally:
            core.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the local cold-start configuration interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-identity", choices=("production", "test"), default="production")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config" / "domain_packs")
    parser.add_argument("--daily-hour", type=int, default=8)
    parser.add_argument("--daily-minute", type=int, default=0)
    parser.add_argument("--daily-poll-seconds", type=float, default=30)
    parser.add_argument("--daily-max-items-per-account", type=int, default=50)
    parser.add_argument("--daily-state", type=Path, default=runtime_path("agent_platform", "daily_operations_state.json"))
    parser.add_argument("--start-delay-seconds", type=float, default=0.0)
    parser.add_argument(
        "--safe-ui-only",
        action="store_true",
        help="serve the local page without automatically resuming business work or starting daily work",
    )
    args = parser.parse_args()
    if args.start_delay_seconds > 0:
        time.sleep(min(args.start_delay_seconds, 10.0))
    test_runtime_root = (
        args.db.resolve().parent
        if args.data_identity == "test" and args.db is not None
        else Path(tempfile.gettempdir()) / "creation_assistant_validation_live"
    ).resolve()
    startup_guard_log = (
        test_runtime_root / "cold_start_ui_test_guard_events.jsonl"
        if args.data_identity == "test"
        else None
    )
    enforce_runtime_startup_guard(
        entrypoint="visual_business_system",
        event_log_path=startup_guard_log,
    )
    configured_first_registration_item_limit()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the local business interface may only bind to this computer")
    if args.data_identity == "production":
        if args.db is not None and args.db.resolve() != FORMAL_DB_PATH.resolve():
            parser.error("production mode may only use the formal production database")
        db_path = FORMAL_DB_PATH
    else:
        db_path = (args.db or test_runtime_root / "validation.sqlite3").resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with socket.create_connection((args.host, args.port), timeout=0.25):
            _console("cold-start configuration UI is already running")
            return 0
    except OSError:
        pass

    instance_name = f"CreationAssistant_ColdStart_{args.data_identity}_{args.port}"
    try:
        with single_instance_guard(instance_name):
            server = ColdStartConfigServer(
                (args.host, args.port), db_path=db_path, data_identity=args.data_identity,
                config_dir=args.config_dir.resolve(),
                daily_state_path=args.daily_state.resolve(),
                daily_hour=args.daily_hour,
                daily_minute=args.daily_minute,
                daily_poll_seconds=args.daily_poll_seconds,
                daily_max_items_per_account=args.daily_max_items_per_account,
                start_daily_operations=not args.safe_ui_only,
            )
            _console(f"cold-start configuration UI ready at http://{args.host}:{server.server_address[1]}/")
            if args.safe_ui_only:
                _console("cold-start configuration UI started in safe UI-only mode")
            else:
                server.start_recovery()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            except BaseException:
                fatal_path = runtime_path("agent_platform", "cold_start_server_fatal.log")
                fatal_path.parent.mkdir(parents=True, exist_ok=True)
                fatal_path.write_text(traceback.format_exc(), encoding="utf-8")
                raise
            finally:
                server.server_close()
    except SingleInstanceAlreadyRunning:
        _console("cold-start configuration UI is already starting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
