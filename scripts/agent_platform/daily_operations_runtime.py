"""Transport and execution carrier for the formal daily Core capability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid
from typing import Any, Callable

from scripts.core.external_adapters.windows_process import blocking_process_mutex
from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.business_runtime_guard import (
    enforce_daily_operations_runtime_guard,
)
from scripts.core.production.stage0_content_core import (
    DAILY_PRIORITY_REPORT_LIMIT,
    Stage0ContentProductionCore,
)
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    Stage1BDailyDiscoveryService,
)
from scripts.core.scheduler.schedule_registry import (
    assert_automatic_schedule_allowed,
)


CHINA_TIME = timezone(timedelta(hours=8))
DAILY_EXECUTION_SLOT_NAME = "CreationAssistant_DailyExecutionSlot"


class DailyOperationsCoordinator:
    """Carry external execution while Core owns daily business decisions."""

    def __init__(
        self,
        *,
        db_path: Path,
        data_identity: str,
        runner: Callable[..., dict[str, Any]] | None = None,
        domain_provider: Callable[[], list[str]] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        on_formal_change: Callable[[str], None] | None = None,
        before_runner: Callable[[], None] | None = None,
        external_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.data_identity = data_identity
        self.runner = runner
        self.domain_provider = domain_provider or self._completed_domains
        self.now_provider = now_provider or (lambda: datetime.now(CHINA_TIME))
        self.on_formal_change = on_formal_change
        self.before_runner = before_runner
        self.external_executor = external_executor

    def _completed_domains(self) -> list[str]:
        core = Stage0ContentProductionCore.open(
            self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
        )
        try:
            return CreationAssistantFormalBusinessCore(core=core).list_daily_domains()
        finally:
            core.close()

    def _run_production(
        self,
        *,
        domain_label: str,
        business_date: str,
        daily_run_id: str | None,
        resume: bool,
        attempt_ref: str,
        validation_only: bool = False,
        account_id: str | None = None,
        effective_at: datetime | None = None,
        task_model_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep the legacy callable while delegating aggregation to Core."""

        core = Stage0ContentProductionCore.open(
            self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
        )
        try:
            return CreationAssistantFormalBusinessCore(core=core).execute_daily(
                domain_label=domain_label,
                business_date=business_date,
                daily_run_id=daily_run_id,
                resume=resume,
                attempt_ref=attempt_ref,
                validation_only=validation_only,
                account_id=account_id,
                effective_at=effective_at,
                task_model_binding=task_model_binding,
                external_executor=self.external_executor,
                service_factory=ProductionDailyOperationsService,
            )
        finally:
            core.close()

    def _run_one_domain(
        self,
        *,
        domain_label: str,
        business_date: str,
        trigger: str,
        resume: bool,
        attempt_ref: str,
        validation_only: bool,
        account_id: str | None,
        effective_at: datetime | None,
        task_model_binding: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with blocking_process_mutex(DAILY_EXECUTION_SLOT_NAME):
            core = Stage0ContentProductionCore.open(
                self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
            )
            try:
                business = CreationAssistantFormalBusinessCore(core=core)
                preparation = business.prepare_daily_execution(
                    domain_label=domain_label,
                    business_date=business_date,
                    trigger=trigger,
                    resume=resume,
                    attempt_ref=attempt_ref,
                    validation_only=validation_only,
                )
                if not preparation.get("execute"):
                    return {
                        key: value
                        for key, value in preparation.items()
                        if key != "execute"
                    }
                daily_run = preparation.get("daily_run")
                if self.before_runner is not None:
                    self.before_runner()
                try:
                    if self.runner is None:
                        result = self._run_production(
                            domain_label=domain_label,
                            business_date=business_date,
                            daily_run_id=(
                                str(daily_run["daily_run_id"])
                                if isinstance(daily_run, dict)
                                else None
                            ),
                            resume=resume,
                            attempt_ref=attempt_ref,
                            validation_only=validation_only,
                            account_id=account_id,
                            effective_at=effective_at,
                            task_model_binding=task_model_binding,
                        )
                    else:
                        result = business.execute_daily(
                            domain_label=domain_label,
                            business_date=business_date,
                            daily_run_id=(
                                str(daily_run["daily_run_id"])
                                if isinstance(daily_run, dict)
                                else None
                            ),
                            resume=resume,
                            attempt_ref=attempt_ref,
                            validation_only=validation_only,
                            account_id=account_id,
                            effective_at=effective_at,
                            task_model_binding=task_model_binding,
                            external_executor=self.external_executor,
                            runner=self.runner,
                        )
                except Exception as exc:
                    result = {
                        "status": "failed",
                        "domain_label": domain_label,
                        "business_date": business_date,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                if validation_only:
                    return result
                if str(result.get("status") or "") == "requires_external_intelligence":
                    return {
                        **result,
                        "daily_run_id": daily_run["daily_run_id"],
                        "daily_run_status": "running",
                        "action": "awaiting_external_intelligence",
                    }
                if not isinstance(daily_run, dict):
                    raise RuntimeError("Core did not return the daily run for execution")
                finalized = business.finalize_daily_execution(
                    daily_run=daily_run,
                    execution_result=result,
                    resume=resume,
                    resume_reconciliation=preparation.get("resume_reconciliation"),
                )
                if (
                    finalized.get("daily_run_status") == "completed"
                    and self.on_formal_change is not None
                ):
                    self.on_formal_change("daily_run_completed")
                return finalized
            finally:
                core.close()

    def _current_date(self) -> tuple[str, datetime]:
        now = self.now_provider().astimezone(CHINA_TIME)
        return now.date().isoformat(), now

    def schedule_all(
        self,
        *,
        domain_labels: tuple[str, ...] | None,
        trigger: str,
        resume: bool = False,
        attempt_ref: str | None = None,
        validation_only: bool = False,
        account_id: str | None = None,
        business_date: str | None = None,
        effective_at: datetime | None = None,
        task_model_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Reject the retired automatic entry before any formal runtime or
        # database connection is opened.  Test identities remain isolated
        # and continue to exercise the same Core path.
        assert_automatic_schedule_allowed(
            schedule_key="daily",
            trigger=trigger,
            data_identity=self.data_identity,
        )
        _, now = self._current_date()
        enforce_daily_operations_runtime_guard(
            entrypoint="daily_operations_one_shot",
            source_types=DAILY_REPORT_SOURCE_TYPES,
            daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
            data_identity=self.data_identity,
        )
        available = tuple(self.domain_provider())
        core = Stage0ContentProductionCore.open(
            self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
        )
        try:
            business = CreationAssistantFormalBusinessCore(core=core)
            selected_date, requested = business.normalize_daily_request(
                domain_labels=domain_labels,
                business_date=business_date,
                effective_at=effective_at,
                now=now,
                available_domains=available,
                resume=resume,
            )
        finally:
            core.close()
        if validation_only and not str(account_id or "").strip():
            raise ValueError("single-account validation requires an explicit account")
        execution_attempt = str(attempt_ref or uuid.uuid4().hex)
        domain_results = [
            self._run_one_domain(
                domain_label=domain_label,
                business_date=selected_date,
                trigger=trigger,
                resume=resume,
                attempt_ref=execution_attempt,
                validation_only=validation_only,
                account_id=account_id,
                effective_at=effective_at,
                task_model_binding=task_model_binding,
            )
            for domain_label in requested
        ]
        return {
            "business_date": selected_date,
            "trigger": trigger,
            "resume": resume,
            "validation_only": validation_only,
            "attempt_ref": execution_attempt,
            "domain_results": domain_results,
            "candidate_count": sum(
                int(item.get("candidate_count") or 0) for item in domain_results
            ),
            "failure_details": [
                detail
                for item in domain_results
                for detail in (item.get("failure_details") or [])
            ],
        }

    def view(self) -> list[dict[str, Any]]:
        business_date, _ = self._current_date()
        core = Stage0ContentProductionCore.open(
            self.db_path, data_identity=self.data_identity  # type: ignore[arg-type]
        )
        try:
            domains = CreationAssistantFormalBusinessCore(core=core).list_daily_domains()
            result: list[dict[str, Any]] = []
            for domain_label in domains:
                formal = core.get_daily_run_for_domain_date(
                    domain_label=domain_label, business_date=business_date
                )
                candidate_run = (
                    core.daily_candidate_discovery_run(
                        daily_run_id=str(formal["daily_run_id"])
                    )
                    if formal is not None
                    else None
                )
                candidates: list[dict[str, Any]] = []
                if candidate_run is not None:
                    candidates = Stage1BDailyDiscoveryService(
                        core=core, gateway=None,  # type: ignore[arg-type]
                    ).view_daily_snapshot(
                        run_id=str(candidate_run["run_id"]), domains=(domain_label,)
                    )[domain_label]
                result.append({
                    "domain_label": domain_label,
                    "today": business_date,
                    "next_run_at": None,
                    "daily_run": formal,
                    "candidate_run": candidate_run,
                    "candidates": candidates,
                    "tag_library_reviews": core.list_open_two_week_tag_library_reviews(
                        domain_label=domain_label
                    ),
                })
            return result
        finally:
            core.close()


from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService
