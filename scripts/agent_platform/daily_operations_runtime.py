"""Persistent Agent-platform scheduling for post-cold-start daily operations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import uuid
from typing import Any, Callable

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage0_content_core import DAILY_PRIORITY_REPORT_LIMIT
from scripts.core.production.business_runtime_guard import (
    enforce_daily_operations_runtime_guard,
)
from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    Stage1BDailyDiscoveryService,
)


CHINA_TIME = timezone(timedelta(hours=8))
GLOBAL_DAILY_JOB_KEY = "daily_competitor_tracking_all_domains"


class DailyOperationsCoordinator:
    def __init__(
        self,
        *,
        db_path: Path,
        data_identity: str,
        state_path: Path,
        run_hour: int = 8,
        run_minute: int = 0,
        poll_seconds: float = 30,
        max_items_per_account: int = 50,
        runner: Callable[[str, str, str], dict[str, Any]] | None = None,
        domain_provider: Callable[[], list[str]] | None = None,
        completed_run_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        on_formal_change: Callable[[str], None] | None = None,
    ) -> None:
        if not (0 <= run_hour <= 23 and 0 <= run_minute <= 59):
            raise ValueError("daily operation time is invalid")
        self.db_path = db_path
        self.data_identity = data_identity
        self.state_path = state_path
        self.run_hour = run_hour
        self.run_minute = run_minute
        self.poll_seconds = max(float(poll_seconds), 0.05)
        self.max_items_per_account = max_items_per_account
        self.runner = runner or self._run_production
        self.domain_provider = domain_provider or self._completed_domains
        self.completed_run_lookup = completed_run_lookup or self._completed_run
        self.now_provider = now_provider or (lambda: datetime.now(CHINA_TIME))
        self.on_formal_change = on_formal_change
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="daily-operations")
        self.thread: threading.Thread | None = None
        self.startup_scan_thread: threading.Thread | None = None
        _, startup_now, startup_due = self._date_and_due()
        self._skip_automatic_date_after_restart = (
            startup_now.date().isoformat() if startup_now >= startup_due else None
        )
        self.jobs = self._load_state()
        recovered_at = self.now_provider().astimezone(CHINA_TIME).isoformat()
        recovered = False
        for job in self.jobs.values():
            if job.get("status") in {"queued", "running"}:
                job["status"] = "failed"
                job["completed_at"] = recovered_at
                job["error"] = {
                    "type": "InterruptedByPlatformRestart",
                    "message": "上次运行被页面服务重启中断，请在页面确认后继续运行",
                    "automatic_retry": False,
                }
                recovered = True
        if recovered:
            self._save_state()

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".new")
        temporary.write_text(json.dumps(self.jobs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    def _completed_domains(self) -> list[str]:
        """Return every domain whose confirmed accounts are ready for shared tracking.

        An account joins this pool as soon as its own formal registration has
        finished.  It deliberately does not wait for a topic, a draft, or the
        final confirmation of every other account in the same domain.
        """
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            rows = core.conn.execute(
                "SELECT DISTINCT account.domain_label FROM competitor_accounts account "
                "JOIN stage0_competitor_registration registration "
                "ON account.source_config_ref='stage0_competitor_registration:' || registration.registration_id "
                "AND registration.data_identity=? AND registration.status='completed' "
                "JOIN stage0_cold_start_configuration configuration "
                "ON configuration.cold_start_id=registration.cold_start_id "
                "AND configuration.data_identity=registration.data_identity "
                "WHERE account.registration_status='active' "
                "AND configuration.status IN ('started','completed') "
                "ORDER BY account.domain_label",
                (self.data_identity,),
            ).fetchall()
            return [str(row["domain_label"]) for row in rows]
        finally:
            core.close()

    def _completed_run(self, domain_label: str, discovery_date: str) -> dict[str, Any] | None:
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            row = core.conn.execute(
                "SELECT run.run_id, run.status, run.completed_at, run.failure_reason, context.lifecycle_status "
                "FROM stage1b_discovery_run run "
                "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
                "JOIN stage1b_daily_snapshot snapshot ON snapshot.run_id=run.run_id "
                "WHERE run.discovery_date=? AND snapshot.domain_label=? AND run.data_identity=? "
                "AND context.execution_mode='production_daily' "
                "ORDER BY run.created_at DESC, run.run_id DESC LIMIT 1",
                (discovery_date, domain_label, self.data_identity),
            ).fetchone()
            return {key: row[key] for key in row.keys()} if row is not None else None
        finally:
            core.close()

    def _run_production(self, domain_label: str, discovery_date: str, attempt_ref: str) -> dict[str, Any]:
        core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
        try:
            result = ProductionDailyOperationsService(
                core=core, max_items_per_account=self.max_items_per_account,
            ).run(
                domain_label=domain_label,
                discovery_date=discovery_date,
                actor="daily_operations_automatic_worker",
                attempt_ref=attempt_ref,
            )
            return result
        finally:
            core.close()

    def _run_all_production(
        self, domain_labels: tuple[str, ...], discovery_date: str, attempt_ref: str
    ) -> dict[str, Any]:
        """Run one shared daily task while retaining an honest result per domain."""
        results: list[dict[str, Any]] = []
        for domain_label in domain_labels:
            try:
                results.append(self.runner(domain_label, discovery_date, attempt_ref))
            except Exception as exc:
                results.append({
                    "domain_label": domain_label,
                    "status": "failed",
                    "candidate_count": 0,
                    "collection": [],
                    "candidates": [],
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "automatic_retry": False,
                    },
                })
        has_failure = any(item.get("status") not in {"completed"} for item in results)
        return {
            "status": "completed_with_failures" if has_failure else "completed",
            "domain_results": results,
            "candidate_count": sum(int(item.get("candidate_count") or 0) for item in results),
        }

    def start(self) -> None:
        if self.thread is not None:
            return
        enforce_daily_operations_runtime_guard(
            entrypoint="daily_operations_scheduler",
            source_types=DAILY_REPORT_SOURCE_TYPES,
            daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
            run_hour=self.run_hour,
            run_minute=self.run_minute,
        )
        self.thread = threading.Thread(target=self._loop, name="daily-operations-scheduler", daemon=True)
        self.thread.start()
        # Startup reconciliation can inspect a sizeable formal corpus.  Keep it
        # asynchronous so the configuration interface becomes usable first.
        self.startup_scan_thread = threading.Thread(
            target=self._run_startup_scans,
            name="daily-operations-startup-scan",
            daemon=True,
        )
        self.startup_scan_thread.start()

    def _run_startup_scans(self) -> None:
        return

    def scan_missing_first_runs(self) -> None:
        """Registration completion dispatches the first run; restart never invents one.

        The shared tracking pool is deliberately changed only by a completed
        registration.  A service restart can restore status visibility, but it
        must not turn historical registrations into fresh collection work.
        """
        return

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=min(self.poll_seconds + 1, 3))
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _loop(self) -> None:
        while not self.stop_event.wait(self.poll_seconds):
            self.scan_due()

    def _date_and_due(self) -> tuple[str, datetime, datetime]:
        now = self.now_provider().astimezone(CHINA_TIME)
        due = now.replace(hour=self.run_hour, minute=self.run_minute, second=0, microsecond=0)
        return now.date().isoformat(), now, due

    def scan_due(self) -> None:
        discovery_date, now, due = self._date_and_due()
        if now < due:
            return
        if discovery_date == self._skip_automatic_date_after_restart:
            return
        domains = tuple(self.domain_provider())
        if domains:
            self.schedule_all(domain_labels=domains, trigger="daily_08_00", force=False)

    def activate_after_competitor_registration(self) -> dict[str, Any]:
        """A newly completed account joins the same daily task immediately."""
        domains = tuple(self.domain_provider())
        if not domains:
            return {"status": "no_tracking_accounts"}
        return self.schedule_all(
            domain_labels=domains,
            trigger="competitor_registration_completed",
            force=False,
        )

    def schedule_all(
        self,
        *,
        domain_labels: tuple[str, ...],
        trigger: str,
        force: bool,
        attempt_ref: str | None = None,
    ) -> dict[str, Any]:
        discovery_date, now, _ = self._date_and_due()
        available = tuple(self.domain_provider())
        requested = tuple(dict.fromkeys(domain_labels))
        if not requested or any(domain not in available for domain in requested):
            raise ValueError("daily operations require active formally registered competitor accounts")
        with self.lock:
            current = self.jobs.get(GLOBAL_DAILY_JOB_KEY, {})
            same_day = current.get("discovery_date") == discovery_date
            completed_domains = set(current.get("completed_domain_labels") or [])
            if same_day and set(requested).issubset(completed_domains) and not force:
                return dict(current)
            if same_day and current.get("status") in {"queued", "running"}:
                return dict(current)
            attempt = attempt_ref or uuid.uuid4().hex
            job = {
                "job_kind": "all_domains_competitor_tracking",
                "domain_labels": list(requested),
                "discovery_date": discovery_date,
                "attempt_ref": attempt,
                "trigger": trigger,
                "status": "queued",
                "queued_at": now.isoformat(),
                "error": None,
            }
            self.jobs[GLOBAL_DAILY_JOB_KEY] = job
            self._save_state()
        self.executor.submit(self._execute_all, requested, discovery_date, attempt)
        return dict(job)

    def _execute_all(self, domain_labels: tuple[str, ...], discovery_date: str, attempt_ref: str) -> None:
        with self.lock:
            job = self.jobs[GLOBAL_DAILY_JOB_KEY]
            job["status"] = "running"
            job["started_at"] = self.now_provider().astimezone(CHINA_TIME).isoformat()
            self._save_state()
        try:
            result = self._run_all_production(domain_labels, discovery_date, attempt_ref)
            with self.lock:
                job = self.jobs[GLOBAL_DAILY_JOB_KEY]
                job["status"] = "completed" if result.get("status") == "completed" else "completed_with_failures"
                job["candidate_count"] = int(result.get("candidate_count") or 0)
                job["domain_results"] = result.get("domain_results") or []
                job["completed_domain_labels"] = list(domain_labels)
                job["completed_at"] = self.now_provider().astimezone(CHINA_TIME).isoformat()
                job["error"] = None
                self._save_state()
            if self.on_formal_change is not None:
                self.on_formal_change("daily_operations_completed")
        except Exception as exc:
            with self.lock:
                job = self.jobs[GLOBAL_DAILY_JOB_KEY]
                job["status"] = "failed"
                job["completed_at"] = self.now_provider().astimezone(CHINA_TIME).isoformat()
                job["error"] = {"type": type(exc).__name__, "message": str(exc), "automatic_retry": False}
                self._save_state()

    def view(self) -> list[dict[str, Any]]:
        discovery_date, now, due = self._date_and_due()
        next_due = due if now < due else due + timedelta(days=1)
        domains = self.domain_provider()
        with self.lock:
            job = dict(self.jobs.get(GLOBAL_DAILY_JOB_KEY, {}))
        domain_results = {str(item.get("domain_label")): item for item in job.get("domain_results") or []}
        result: list[dict[str, Any]] = []
        for domain_label in domains:
            formal = self.completed_run_lookup(domain_label, discovery_date)
            domain_job = dict(job)
            domain_job["domain_result"] = domain_results.get(domain_label)
            candidates: list[dict[str, Any]] = []
            tag_library_reviews: list[dict[str, Any]] = []
            run_id = str((formal or {}).get("run_id") or job.get("run_id") or "")
            if run_id or domain_label:
                core = Stage0ContentProductionCore.open(self.db_path, data_identity=self.data_identity)  # type: ignore[arg-type]
                try:
                    if run_id:
                        candidates = Stage1BDailyDiscoveryService(core=core, gateway=None).view_daily_snapshot(  # type: ignore[arg-type]
                            run_id=run_id, domains=(domain_label,),
                        )[domain_label]
                    tag_library_reviews = core.list_open_two_week_tag_library_reviews(
                        domain_label=domain_label
                    )
                except Exception:
                    candidates = []
                finally:
                    core.close()
            result.append({
                "domain_label": domain_label,
                "today": discovery_date,
                "next_run_at": next_due.isoformat(),
                "job": domain_job,
                "formal_run": formal,
                "candidates": candidates,
                "tag_library_reviews": tag_library_reviews,
            })
        return result
