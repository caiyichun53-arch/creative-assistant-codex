"""Canonical formal business entrypoints for Creation Assistant.

This module is the business-facing boundary used by future transports.  It
does not know about Hermes, Codex, MCP, a web page, or a scheduler.  Existing
business services remain the implementation; this boundary is responsible for
choosing the one Core operation that owns each formal decision.

The current migration protection period is enforced outside this module by not
opening the formal database for writes.  Tests inject an isolated Core.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable

from scripts.core.production.human_decision_entry import (
    FormalHumanDecisionCommand,
    HumanDecisionCommandService,
)
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.scheduler.schedule_registry import ScheduleRegistry


def assert_business_action_allowed(*, data_identity: str) -> None:
    """Reject formal writes while the existing migration protection is active.

    This is the Core-side guard for transports that need to open a writable
    Core.  Test identities stay isolated and usable; production remains closed
    by the existing project migration declaration.
    """

    identity = str(data_identity or "").strip().lower()
    if identity == "test":
        return
    if identity != "production":
        raise StateTransitionError("business actions require an explicit data identity")
    registry = ScheduleRegistry.load()
    if registry.migration_protection:
        raise StateTransitionError(
            "formal business actions are blocked while migration protection is active"
        )


@dataclass
class CreationAssistantFormalBusinessCore:
    """The single formal business decision boundary.

    The wrapped ``Stage0ContentProductionCore`` remains the existing source of
    truth and persistence rules.  This class deliberately does not open a
    database by itself: production write opening is still frozen during the
    migration protection period, while isolated tests may inject a test Core.
    """

    core: Stage0ContentProductionCore

    @staticmethod
    def assert_action_allowed(*, data_identity: str) -> None:
        """Apply the Core-owned migration boundary before a mutable open."""

        assert_business_action_allowed(data_identity=data_identity)

    def list_daily_domains(self) -> list[str]:
        """Return the domains that Core allows the daily flow to address."""

        self.core.require_domain_activation_schema()
        query = (
            "SELECT DISTINCT account.domain_label FROM competitor_accounts account "
            "JOIN stage0_content_account formal_account "
            "ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' "
            "AND formal_account.status='active' "
            "JOIN stage0_competitor_registration registration "
            "ON account.source_config_ref='stage0_competitor_registration:' || registration.registration_id "
            "AND registration.data_identity=? AND registration.status='completed' "
            "JOIN stage0_cold_start_configuration configuration "
            "ON configuration.cold_start_id=registration.cold_start_id "
            "AND configuration.data_identity=registration.data_identity "
            "WHERE account.registration_status='active' "
            "AND configuration.status IN ('started','completed') "
        )
        params: tuple[Any, ...] = (
            self.core.data_identity,
            self.core.data_identity,
        )
        query += (
            "AND EXISTS (SELECT 1 FROM stage0_domain_activation current_activation "
            "WHERE current_activation.domain_label=account.domain_label "
            "AND current_activation.cold_start_id=registration.cold_start_id "
            "AND current_activation.data_identity=? AND current_activation.is_current=1) "
        )
        params += (self.core.data_identity,)
        query += "ORDER BY account.domain_label"
        rows = self.core.conn.execute(query, params).fetchall()
        return [str(row["domain_label"]) for row in rows]

    def normalize_daily_request(
        self,
        *,
        domain_labels: tuple[str, ...] | None,
        business_date: str | None,
        effective_at: datetime | None,
        now: datetime,
        available_domains: tuple[str, ...] | None = None,
        resume: bool = False,
    ) -> tuple[str, tuple[str, ...]]:
        """Apply the existing daily request rules before execution begins."""

        china_time = timezone(timedelta(hours=8))
        current_date = now.astimezone(china_time)
        selected_date = str(business_date or current_date.date().isoformat()).strip()
        try:
            datetime.strptime(selected_date, "%Y-%m-%d")
        except ValueError as exc:
            raise StateTransitionError(
                "daily operation business date must use YYYY-MM-DD"
            ) from exc
        if business_date is None and effective_at is not None:
            raise StateTransitionError(
                "an effective observation time requires an explicit business date"
            )
        if business_date is not None and effective_at is None:
            raise StateTransitionError(
                "a catch-up business date requires an effective observation time"
            )
        if effective_at is not None:
            if effective_at.tzinfo is None:
                raise StateTransitionError(
                    "effective observation time must include a timezone"
                )
            if effective_at.astimezone(china_time).date().isoformat() != selected_date:
                raise StateTransitionError(
                    "effective observation time must belong to the selected business date"
                )
            if effective_at > now:
                raise StateTransitionError(
                    "effective observation time cannot be in the future"
                )

        available = tuple(
            self.list_daily_domains() if available_domains is None else available_domains
        )
        requested = (
            tuple(dict.fromkeys(str(item).strip() for item in domain_labels))
            if domain_labels is not None
            else available
        )
        if not requested or any(domain not in available for domain in requested):
            raise StateTransitionError(
                "daily operations require active formally registered competitor accounts"
            )
        if resume and len(requested) != 1:
            raise StateTransitionError("daily resume requires exactly one domain")
        return selected_date, requested

    def request_daily(
        self,
        *,
        domain_label: str,
        business_date: str,
        actor: str,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Resolve one daily business run and let Core decide its lifecycle."""

        existing = self.core.get_daily_run_for_domain_date(
            domain_label=domain_label,
            business_date=business_date,
        )
        if resume and existing is None:
            raise StateTransitionError(
                "daily resume requires an existing daily run"
            )
        run = existing or self.core.get_or_create_daily_run(
            domain_label=domain_label,
            business_date=business_date,
            actor=actor,
        )
        lifecycle = str(run["lifecycle"])
        if lifecycle == "completed":
            return {"action": "skipped_completed", "daily_run": run}
        if lifecycle in {"failed", "stopped"} and not resume:
            return {"action": "awaiting_user_resume", "daily_run": run}

        started = self.core.start_daily_run(
            daily_run_id=str(run["daily_run_id"]),
            resume=resume,
            actor=actor,
        )
        return {
            "action": "resumed" if resume else "started",
            "daily_run": started,
        }

    def prepare_daily_execution(
        self,
        *,
        domain_label: str,
        business_date: str,
        trigger: str,
        resume: bool,
        attempt_ref: str,
        validation_only: bool = False,
    ) -> dict[str, Any]:
        """Resolve and start the one formal daily run before external work."""

        if validation_only:
            return {
                "execute": True,
                "daily_run": None,
                "prior_daily_lifecycle": None,
                "resume_reconciliation": None,
            }

        existing = self.core.get_daily_run_for_domain_date(
            domain_label=domain_label,
            business_date=business_date,
        )
        if resume and existing is None:
            raise StateTransitionError("daily resume requires an existing daily run")
        daily_run = existing or self.core.get_or_create_daily_run(
            domain_label=domain_label,
            business_date=business_date,
            actor=trigger,
        )
        created_here = existing is None
        lifecycle = str(daily_run["lifecycle"])
        if lifecycle == "completed":
            return {
                "execute": False,
                "domain_label": domain_label,
                "business_date": business_date,
                "daily_run_id": daily_run["daily_run_id"],
                "daily_run_status": "completed",
                "action": "skipped_completed",
            }
        prior_lifecycle = lifecycle
        if lifecycle in {"failed", "stopped"} and not resume:
            return {
                "execute": False,
                "domain_label": domain_label,
                "business_date": business_date,
                "daily_run_id": daily_run["daily_run_id"],
                "daily_run_status": lifecycle,
                "action": "awaiting_user_resume",
            }
        if lifecycle == "running" and not created_here:
            daily_run = self.core.finish_daily_run(
                daily_run_id=str(daily_run["daily_run_id"]),
                lifecycle="stopped",
                actor="daily_operations_recovery",
                reason=(
                    "daily execution slot was free while the formal run was still running"
                ),
            )
            if not resume:
                return {
                    "execute": False,
                    "domain_label": domain_label,
                    "business_date": business_date,
                    "daily_run_id": daily_run["daily_run_id"],
                    "daily_run_status": "stopped",
                    "action": "awaiting_user_resume",
                }
        daily_run = self.core.start_daily_run(
            daily_run_id=str(daily_run["daily_run_id"]),
            resume=resume,
            actor=(
                "daily_operations_user_resume"
                if resume
                else "daily_operations_automatic_worker"
            ),
        )
        if str(daily_run["lifecycle"]) != "running":
            return {
                "execute": False,
                "domain_label": domain_label,
                "business_date": business_date,
                "daily_run_id": daily_run["daily_run_id"],
                "daily_run_status": daily_run["lifecycle"],
                "action": "not_started",
            }
        reconciliation = None
        if resume and prior_lifecycle in {"failed", "stopped"}:
            reconciliation = self.core.reconcile_prior_daily_discovery_for_resume(
                daily_run_id=str(daily_run["daily_run_id"]),
                prior_daily_lifecycle=prior_lifecycle,
                resume_started_at=str(daily_run.get("started_at") or ""),
                execution_attempt_ref=attempt_ref,
                idempotency_key=(
                    f"agent-platform:daily-resume-reconcile:"
                    f"{daily_run['daily_run_id']}:{attempt_ref}"
                ),
            )
        return {
            "execute": True,
            "daily_run": daily_run,
            "prior_daily_lifecycle": prior_lifecycle,
            "resume_reconciliation": reconciliation,
        }

    def execute_daily(
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
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        runner: Callable[..., dict[str, Any]] | None = None,
        service_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Run the existing daily work while keeping its business aggregation in Core."""

        if runner is not None:
            return runner(
                domain_label=domain_label,
                business_date=business_date,
                daily_run_id=daily_run_id,
                resume=resume,
                attempt_ref=attempt_ref,
                validation_only=validation_only,
                account_id=account_id,
                effective_at=effective_at,
                task_model_binding=task_model_binding,
            )

        from scripts.core.production.stage1_daily_operations import (
            ProductionDailyOperationsService,
        )
        from scripts.core.production.stage1b_daily_discovery import (
            DAILY_REPORT_SOURCE_TYPES,
        )

        service_type = service_factory or ProductionDailyOperationsService
        if external_executor is None:
            service = service_type(
                core=self.core,
                task_model_binding=task_model_binding,
            )
        else:
            service = service_type(
                core=self.core,
                task_model_binding=task_model_binding,
                external_executor=external_executor,
            )
        collection_result = service.run(
            domain_label=domain_label,
            discovery_date=business_date,
            actor=(
                "daily_operations_user_resume"
                if resume
                else "daily_operations_automatic_worker"
            ),
            attempt_ref=attempt_ref,
            daily_run_id=daily_run_id,
            resume=resume,
            validation_only=validation_only,
            account_id=account_id,
            effective_at=effective_at,
        )
        if validation_only:
            candidate_result = {
                "run_id": None,
                "status": "completed",
                "candidate_discovery": "not_triggered_validation_only",
                "candidates": [],
                "candidate_count": 0,
            }
        elif str(collection_result.get("status") or "failed") == "stopped":
            candidate_result = {
                "run_id": None,
                "status": "stopped",
                "candidate_discovery": "not_triggered",
                "candidates": [],
                "candidate_count": 0,
                "stop_reason": collection_result.get("stop_reason"),
            }
        else:
            candidate_result = service.run_candidate_discovery(
                domain_label=domain_label,
                discovery_date=business_date,
                actor="daily_candidate_discovery_worker",
                attempt_ref=f"{attempt_ref}:candidate",
                daily_run_id=str(daily_run_id),
                resume=resume,
                validation_only=validation_only,
                upstream_failures=tuple(
                    collection_result.get("upstream_failures") or ()
                ),
                effective_at=effective_at,
            )
        collection_status = str(collection_result.get("status") or "failed")
        candidate_status = str(candidate_result.get("status") or "failed")
        failure_details: list[dict[str, Any]] = []
        if collection_status != "completed":
            failure_details.append({
                "stage": "collection",
                "status": collection_status,
                "failure_count": len(collection_result.get("upstream_failures") or []),
                "reason": (
                    collection_result.get("stop_reason")
                    if collection_status == "stopped"
                    else "daily source collection or material processing had failures"
                ),
            })
        if candidate_status != "completed":
            failure_details.append({
                "stage": "candidate_discovery",
                "status": candidate_status,
                "source_types": candidate_result.get("source_types")
                or list(DAILY_REPORT_SOURCE_TYPES),
                "reason": candidate_result.get("failure_reason")
                or candidate_result.get("stop_reason")
                or "candidate discovery did not complete",
                "technical_failures": int(candidate_result.get("technical_failures") or 0),
            })
        failure_details.extend(candidate_result.get("failure_details") or [])
        has_failure = (
            collection_status not in {"completed", "stopped"}
            or candidate_status not in {"completed", "stopped"}
        )
        stopped = (
            not has_failure
            and (collection_status == "stopped" or candidate_status == "stopped")
        )
        successful = collection_status == "completed" and candidate_status == "completed"
        return {
            "status": (
                "failed"
                if has_failure
                else ("stopped" if stopped else ("completed" if successful else "failed"))
            ),
            "domain_label": domain_label,
            "business_date": business_date,
            "daily_run_id": daily_run_id,
            "resume": resume,
            "validation_only": validation_only,
            "account_count": collection_result.get("account_count", 0),
            "collection_status": collection_status,
            "candidate_status": candidate_status,
            "collection_run_id": collection_result.get("collection_run_id"),
            "candidate_run_id": candidate_result.get("run_id"),
            "candidate_discovery": candidate_result.get("candidate_discovery") or "not_triggered",
            "collection": collection_result.get("collection") or [],
            "hit_processing": collection_result.get("hit_processing") or [],
            "candidates": candidate_result.get("candidates") or [],
            "candidate_count": int(candidate_result.get("candidate_count") or 0),
            "upstream_failures": collection_result.get("upstream_failures") or [],
            "candidate_source_types": candidate_result.get("source_types")
            or list(DAILY_REPORT_SOURCE_TYPES),
            "candidate_failure_reason": candidate_result.get("failure_reason"),
            "stop_reason": collection_result.get("stop_reason")
            or candidate_result.get("stop_reason"),
            "candidate_technical_failures": int(candidate_result.get("technical_failures") or 0),
            "candidate_summary": candidate_result.get("summary") or {},
            "failure_details": failure_details,
            "error": collection_result.get("error") or candidate_result.get("error"),
        }

    def finalize_daily_execution(
        self,
        *,
        daily_run: dict[str, Any],
        execution_result: dict[str, Any],
        resume: bool,
        resume_reconciliation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Map an execution result to the one permitted formal daily lifecycle."""

        result_status = str(execution_result.get("status") or "failed")
        final_lifecycle = (
            result_status
            if result_status in {"completed", "failed", "stopped"}
            else "failed"
        )
        finished = self.core.finish_daily_run(
            daily_run_id=str(daily_run["daily_run_id"]),
            lifecycle=final_lifecycle,
            actor="daily_operations_worker",
            reason=(
                None
                if final_lifecycle == "completed"
                else str(
                    execution_result.get("error")
                    or execution_result.get("failure_details")
                    or "daily execution failed"
                )
            ),
        )
        return {
            **execution_result,
            "daily_run_id": finished["daily_run_id"],
            "daily_run_status": finished["lifecycle"],
            "action": "resumed" if resume else "started",
            "prior_discovery_resume_reconciliation": resume_reconciliation,
        }

    def resume_daily(self, *, daily_run_id: str, actor: str) -> dict[str, Any]:
        """Resume the exact existing daily run; never create a replacement."""

        run = self.core.get_daily_run(daily_run_id=daily_run_id)
        if run is None:
            raise StateTransitionError("daily resume requires an existing daily run")
        return self.request_daily(
            domain_label=str(run["domain_label"]),
            business_date=str(run["business_date"]),
            actor=actor,
            resume=True,
        )

    def finish_daily(
        self,
        *,
        daily_run_id: str,
        lifecycle: str,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a daily lifecycle transition through the existing Core rules."""

        return self.core.finish_daily_run(
            daily_run_id=daily_run_id,
            lifecycle=lifecycle,
            actor=actor,
            reason=reason,
        )

    def start_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        task_model_binding: dict[str, Any] | None = None,
        preflight_receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """Start or continue the one Core-owned run for a configuration."""

        return self.core.start_configured_cold_start(
            configuration_id=configuration_id,
            actor=actor,
            task_model_binding=task_model_binding,
            preflight_receipt_id=preflight_receipt_id,
        )

    def reset_domain(self, *, domain_label: str, actor: str) -> dict[str, Any]:
        """Release one domain's current activation while preserving its history."""

        return self.core.reset_domain(domain_label=domain_label, actor=actor)

    def resume_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        task_model_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resume the exact configured cold-start run through Core."""

        return self.core.resume_stopped_cold_start(
            configuration_id=configuration_id,
            actor=actor,
            task_model_binding=task_model_binding,
        )

    def stop_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str | None,
        reason: str,
    ) -> dict[str, Any]:
        """Stop one existing cold-start run through the Core lifecycle rule."""

        return self.core.stop_configured_cold_start(
            configuration_id=configuration_id,
            actor=actor,
            reason=reason,
        )

    def submit_human_decision(
        self,
        *,
        command: FormalHumanDecisionCommand,
        apply_formal_decision: Callable[[FormalHumanDecisionCommand], dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate, apply, and durably finish one formal human decision.

        The callback is an internal Core business operation supplied by the
        existing service adapter.  A transport may submit a command, but it
        cannot mark the decision completed or write a business status itself.
        """

        return HumanDecisionCommandService(core=self.core).execute(
            command=command,
            handler=apply_formal_decision,
        )

    def record_discovery_decision(
        self,
        *,
        candidate_version_id: str,
        decision: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        formal_topic_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Keep candidate acceptance/rejection in the existing Core method."""

        return self.core.record_discovery_decision(
            candidate_version_id=candidate_version_id,
            decision=decision,
            actor=actor,
            reason=reason,
            formal_topic_task_id=formal_topic_task_id,
            idempotency_key=idempotency_key,
        )

    def select_discovery_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Select a candidate only through the Core-owned selection rule."""

        return self.core.select_discovery_candidate(
            candidate_version_id=candidate_version_id,
            actor=actor,
            actor_kind=actor_kind,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def execute_daily_discovery(
        self,
        *,
        discovery_date: str,
        actor: str,
        idempotency_key: str,
        execution_mode: str,
        daily_run_id: str | None,
        domains: tuple[str, ...],
        source_types: tuple[str, ...],
        gateway: Any | None = None,
        source_acquirer: Any | None = None,
        model_route: Any | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        reuse_hotspot_discovery_run_id: str | None = None,
        resume_source_object_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Run the existing discovery business service through the Core boundary."""

        # Kept in the transport signature for compatibility only.  Formal
        # discovery never accepts a Core-selected gateway or route.
        del gateway, model_route

        from scripts.core.production.stage1b_daily_discovery import (
            Stage1BDailyDiscoveryService,
        )

        service = Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=None,
            source_acquirer=source_acquirer,
            external_executor=external_executor,
        )
        return service.run_daily_discovery(
            discovery_date=discovery_date,
            actor=actor,
            idempotency_key=idempotency_key,
            execution_mode=execution_mode,
            daily_run_id=daily_run_id,
            domains=domains,
            source_types=source_types,
            reuse_hotspot_discovery_run_id=reuse_hotspot_discovery_run_id,
            resume_source_object_ids=resume_source_object_ids,
        )

    def view_daily_discovery_snapshot(
        self,
        *,
        run_id: str,
        domains: tuple[str, ...],
    ) -> dict[str, list[dict[str, Any]]]:
        from scripts.core.production.stage1b_daily_discovery import (
            Stage1BDailyDiscoveryService,
        )

        return Stage1BDailyDiscoveryService(
            core=self.core, gateway=None  # type: ignore[arg-type]
        ).view_daily_snapshot(run_id=run_id, domains=domains)

    def handoff_daily_discovery_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        gateway: Any | None = None,
    ) -> dict[str, Any]:
        from scripts.core.production.stage1b_daily_discovery import (
            Stage1BDailyDiscoveryService,
        )

        del gateway
        service = Stage1BDailyDiscoveryService(core=self.core, gateway=None)
        return service.handoff_selected_candidate(
            candidate_version_id=candidate_version_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def qualify_pending_discovery_sources(self, *, actor: str) -> dict[str, Any]:
        return self.core.qualify_pending_question_expansion_sources(actor=actor)

    def inspect_daily_discovery_run(self, *, run_id: str) -> dict[str, Any]:
        return self.core.discovery_run_diagnostics(run_id=run_id)

    def verify_stage1_production_closure(self) -> dict[str, Any]:
        return self.core.latest_stage1_production_handoff()

    def continue_formal_production(
        self,
        *,
        task_id: str,
        actor: str,
        user_requirements: str,
        idempotency_key: str,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Continue the current Core-owned production task to its next boundary."""
        from scripts.core.production.stage1c_content_pipeline import (
            Stage1CContentPipelineService,
        )

        return dict(
            Stage1CContentPipelineService(
                core=self.core,
                gateway=None,
                external_executor=external_executor,
            ).advance_formal_content(
                task_id=task_id,
                actor=actor,
                user_requirements=user_requirements,
                idempotency_key=idempotency_key,
            )
        )

    def approve_formal_production_node(
        self,
        *,
        task_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        version_id: str | None = None,
        user_requirements: str | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Approve the current human gate and continue the same production task."""
        from scripts.core.production.stage1c_content_pipeline import (
            Stage1CContentPipelineService,
        )

        task = self.core.get_task(task_id)
        current_version_id = str(version_id or task.get("current_version_id") or "").strip()
        if not current_version_id:
            raise StateTransitionError("formal production approval requires the current version")
        decision = Stage1CContentPipelineService(
            core=self.core,
            gateway=None,
            external_executor=external_executor,
        ).approve(
            task_id=task_id,
            version_id=current_version_id,
            actor=actor,
            reason=reason,
            idempotency_key=f"{idempotency_key}:approve",
        )
        if decision.get("current_node") == "user_final_confirmation":
            return {
                "decision": decision,
                "continuation": {
                    "task_id": task_id,
                    "status": "awaiting_final_confirmation",
                    "current_node": "user_final_confirmation",
                },
            }
        continuation = self.continue_formal_production(
            task_id=task_id,
            actor=actor,
            user_requirements=str(user_requirements or reason),
            idempotency_key=f"{idempotency_key}:continue",
            external_executor=external_executor,
        )
        return {"decision": decision, "continuation": continuation}

    def return_formal_production_node(
        self,
        *,
        task_id: str,
        actor: str,
        requirements: str,
        idempotency_key: str,
        version_id: str | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return the current human output and regenerate its replacement version."""
        from scripts.core.production.stage1a_research_plan import (
            Stage1AResearchPlanService,
        )
        from scripts.core.production.stage1c_content_pipeline import (
            Stage1CContentPipelineService,
        )

        if not requirements.strip():
            raise StateTransitionError("returning a formal production output requires requirements")
        task = self.core.get_task(task_id)
        current_version_id = str(version_id or task.get("current_version_id") or "").strip()
        if not current_version_id:
            raise StateTransitionError("formal production return requires the current version")
        current_version = self.core.get_node_version(current_version_id)
        if current_version["node"] == "research_plan":
            returned = Stage1AResearchPlanService(
                core=self.core,
                gateway=None,
                external_executor=external_executor,
            ).return_research_plan(
                task_id=task_id,
                research_plan_version_id=current_version_id,
                modification_requirements=requirements,
                actor=actor,
                idempotency_key=f"{idempotency_key}:return",
            )
            regenerated = Stage1AResearchPlanService(
                core=self.core,
                gateway=None,
                external_executor=external_executor,
            ).generate_research_plan(
                task_id=task_id,
                user_requirements=requirements,
                actor=actor,
                idempotency_key=f"{idempotency_key}:research-plan",
            )
            continuation = {
                **returned,
                "research_plan_version_id": regenerated["node_version_id"],
                "research_plan_status": str(regenerated.get("status") or "awaiting_human_review"),
                **({"external_task": regenerated["task"]} if isinstance(regenerated.get("task"), dict) else {}),
            }
            return {"return": returned, "continuation": continuation}
        returned = Stage1CContentPipelineService(
            core=self.core,
            gateway=None,
            external_executor=external_executor,
        ).return_for_revision(
            task_id=task_id,
            version_id=current_version_id,
            actor=actor,
            requirements=requirements,
            idempotency_key=f"{idempotency_key}:return",
        )
        continuation = self.continue_formal_production(
            task_id=task_id,
            actor=actor,
            user_requirements=requirements,
            idempotency_key=f"{idempotency_key}:continue",
            external_executor=external_executor,
        )
        return {"return": returned, "continuation": continuation}

    def submit_formal_external_result(
        self,
        *,
        task_id: str,
        node_version_id: str,
        execution_id: str,
        executor_id: str,
        model_ref: str | None,
        submitted_at: str | None,
        output: dict[str, Any],
        actor: str,
        idempotency_key: str,
        experience_usage: Mapping[str, Any] | None = None,
        validation_usage: Mapping[str, Any] | None = None,
        user_requirements: str = "continue the current formal production task",
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Accept one structured external result and continue the same task when allowed."""
        from scripts.core.production.stage1a_research_plan import (
            Stage1AResearchPlanService,
        )
        from scripts.core.production.stage1c_content_pipeline import (
            Stage1CContentPipelineService,
        )

        version = self.core.get_node_version(node_version_id)
        if str(version["task_id"]) != str(task_id):
            raise StateTransitionError("external result does not belong to the current production task")
        service = Stage1AResearchPlanService(
            core=self.core,
            gateway=None,
            external_executor=external_executor,
        ) if version["node"] == "research_plan" else Stage1CContentPipelineService(
            core=self.core,
            gateway=None,
            external_executor=external_executor,
        )
        if version["node"] == "research_plan":
            submitted = service.submit_research_plan_external_result(  # type: ignore[union-attr]
                task_id=task_id,
                node_version_id=node_version_id,
                execution_id=execution_id,
                executor_id=executor_id,
                model_ref=model_ref,
                submitted_at=submitted_at,
                output=output,
                actor=actor,
                idempotency_key=idempotency_key,
            )
            continuation = {
                "task_id": task_id,
                "status": "awaiting_human_review",
                "current_node": "research_plan",
            }
        else:
            submitted = service.submit_content_external_result(  # type: ignore[union-attr]
                task_id=task_id,
                node_version_id=node_version_id,
                execution_id=execution_id,
                executor_id=executor_id,
                model_ref=model_ref,
                submitted_at=submitted_at,
                output=output,
                experience_usage=experience_usage,
                validation_usage=validation_usage,
                actor=actor,
                idempotency_key=idempotency_key,
            )
            continuation = service.continue_after_external_result(  # type: ignore[union-attr]
                task_id=task_id,
                actor=actor,
                user_requirements=user_requirements,
                idempotency_key=f"{idempotency_key}:continue",
            )
        return {"submitted": submitted, "continuation": continuation}

    def execute_formal_research(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        research_gateway: Any | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Apply one named research operation using the existing Core services."""
        # The formal route no longer chooses or calls a model.  The parameter
        # remains accepted for transport compatibility, but an external
        # executor must submit its structured result through Core.
        del research_gateway

        from scripts.core.production.stage1a_research_plan import (
            Stage1AResearchPlanService,
        )
        from scripts.core.production.stage1c_content_pipeline import (
            Stage1CContentPipelineService,
            _screen_research_materials,
        )

        def required(*keys: str) -> list[Any]:
            values = []
            for key in keys:
                if key not in payload:
                    raise StateTransitionError(
                        f"formal research request is missing {key}"
                    )
                values.append(payload[key])
            return values

        if action == "create_plan":
            (
                domain_label,
                core_question,
                scope_or_requirement,
                original_instruction,
                actor,
                idempotency_key,
            ) = required(
                "domain_label",
                "core_question",
                "scope_or_requirement",
                "original_instruction",
                "actor",
                "idempotency_key",
            )
            account_ref = payload.get("account_ref")
            account_id = payload.get("account_id")
            if account_id and not account_ref:
                account_ref = self.core.resolve_owned_account_ref(
                    domain_label=str(domain_label),
                    content_account_id=str(account_id),
                )
            if not account_ref:
                raise StateTransitionError(
                    "formal research request requires account_id or account_ref"
                )
            service = Stage1AResearchPlanService(
                core=self.core,
                gateway=None,
                external_executor=external_executor,
            )
            return service.create_direct_formal_topic_and_generate_plan(
                domain_label=str(domain_label),
                account_ref=str(account_ref),
                core_question=str(core_question),
                scope_or_requirement=str(scope_or_requirement),
                original_instruction=original_instruction,
                actor=str(actor),
                idempotency_key=str(idempotency_key),
            )

        if action == "view_plan":
            (version_id,) = required("version_id")
            return self.core.get_artifact_payload(str(version_id))

        if action == "approve_plan":
            task_id, version_id, actor, reason, idempotency_key = required(
                "task_id",
                "research_plan_version_id",
                "actor",
                "reason",
                "idempotency_key",
            )
            return self.approve_formal_production_node(
                task_id=str(task_id),
                actor=str(actor),
                reason=str(reason),
                idempotency_key=str(idempotency_key),
                version_id=str(version_id),
                user_requirements=str(payload.get("user_requirements") or reason),
                external_executor=external_executor,
            )

        if action == "retry_plan":
            task_id, actor, reason, idempotency_key = required(
                "task_id", "actor", "reason", "idempotency_key"
            )
            task = self.core.get_task(str(task_id))
            if task["current_status"] == "failed":
                failed_version = self.core.get_node_version(
                    str(task["current_version_id"])
                )
                input_assembly = self.core.get_input_assembly_payload(
                    str(failed_version["input_assembly_id"])
                )
                user_requirements = str(input_assembly["user_requirements"])
                retry = self.core.requeue_failed_node_for_manual_retry(
                    task_id=str(task_id),
                    actor=str(actor),
                    reason=str(reason),
                    idempotency_key=str(idempotency_key),
                )
            elif (
                task["current_status"] == "not_started"
                and task["current_node"] == "research_plan"
            ):
                user_requirements = str(payload.get("user_requirements") or "")
                if not user_requirements.strip():
                    raise StateTransitionError(
                        "a resumed research-plan retry requires user_requirements"
                    )
                retry = {
                    "task_id": str(task_id),
                    "current_node": "research_plan",
                    "current_status": "not_started",
                    "task_revision": str(task["task_revision"]),
                }
            else:
                raise StateTransitionError(
                    "retry_plan requires a failed or manually requeued research_plan task"
                )
            plan = Stage1AResearchPlanService(
                core=self.core, gateway=None, external_executor=external_executor
            ).generate_research_plan(
                task_id=str(task_id),
                user_requirements=user_requirements,
                actor=str(actor),
                idempotency_key=f"{idempotency_key}:research-plan",
            )
            return {
                **retry,
                "research_plan_version_id": plan["node_version_id"],
                "research_plan_status": str(plan.get("status") or "awaiting_human_review"),
                **({"external_task": plan["task"]} if isinstance(plan.get("task"), dict) else {}),
            }

        if action == "revise_plan":
            task_id, version_id, requirements, actor, idempotency_key = required(
                "task_id",
                "research_plan_version_id",
                "modification_requirements",
                "actor",
                "idempotency_key",
            )
            service = Stage1AResearchPlanService(
                core=self.core, gateway=None, external_executor=external_executor
            )
            returned = service.return_research_plan(
                task_id=str(task_id),
                research_plan_version_id=str(version_id),
                modification_requirements=str(requirements),
                actor=str(actor),
                idempotency_key=str(idempotency_key),
            )
            plan = service.generate_research_plan(
                task_id=str(task_id),
                user_requirements=str(requirements),
                actor=str(actor),
                idempotency_key=f"{idempotency_key}:research-plan",
            )
            return {
                **returned,
                "research_plan_version_id": plan["node_version_id"],
                "research_plan_status": str(plan.get("status") or "awaiting_human_review"),
                **({"external_task": plan["task"]} if isinstance(plan.get("task"), dict) else {}),
            }

        if action == "run_research":
            task_id, actor, user_requirements, idempotency_key = required(
                "task_id", "actor", "user_requirements", "idempotency_key"
            )
            return Stage1CContentPipelineService(
                core=self.core, gateway=None, external_executor=external_executor  # type: ignore[arg-type]
            ).advance_formal_content(
                task_id=str(task_id),
                actor=str(actor),
                user_requirements=str(user_requirements),
                idempotency_key=str(idempotency_key),
            )

        if action == "approve_research_result":
            task_id, version_id, actor, reason, idempotency_key = required(
                "task_id",
                "research_result_version_id",
                "actor",
                "reason",
                "idempotency_key",
            )
            return self.approve_formal_production_node(
                task_id=str(task_id),
                actor=str(actor),
                reason=str(reason),
                idempotency_key=str(idempotency_key),
                version_id=str(version_id),
                user_requirements=str(payload.get("user_requirements") or reason),
                external_executor=external_executor,
            )

        if action == "return_research_result":
            task_id, version_id, requirements, actor, idempotency_key = required(
                "task_id",
                "research_result_version_id",
                "modification_requirements",
                "actor",
                "idempotency_key",
            )
            return self.return_formal_production_node(
                task_id=str(task_id),
                actor=str(actor),
                requirements=str(requirements),
                idempotency_key=str(idempotency_key),
                version_id=str(version_id),
                external_executor=external_executor,
            )

        if action in {"approve_content_node", "approve_content_plan", "approve_draft", "approve_review"}:
            task_id, actor, reason, idempotency_key = required(
                "task_id", "actor", "reason", "idempotency_key"
            )
            version_id = payload.get("version_id") or payload.get("content_version_id")
            return self.approve_formal_production_node(
                task_id=str(task_id),
                actor=str(actor),
                reason=str(reason),
                idempotency_key=str(idempotency_key),
                version_id=str(version_id) if version_id else None,
                user_requirements=str(payload.get("user_requirements") or reason),
                external_executor=external_executor,
            )

        if action in {"return_content_node", "return_content_plan", "return_draft", "return_review"}:
            task_id, actor, requirements, idempotency_key = required(
                "task_id", "actor", "modification_requirements", "idempotency_key"
            )
            version_id = payload.get("version_id") or payload.get("content_version_id")
            return self.return_formal_production_node(
                task_id=str(task_id),
                actor=str(actor),
                requirements=str(requirements),
                idempotency_key=str(idempotency_key),
                version_id=str(version_id) if version_id else None,
                external_executor=external_executor,
            )

        if action == "submit_external_result":
            task_id, node_version_id, execution_id, executor_id, output, actor, idempotency_key = required(
                "task_id", "node_version_id", "execution_id", "executor_id", "output", "actor", "idempotency_key"
            )
            if not isinstance(output, dict):
                raise StateTransitionError("external result output must be an object")
            return self.submit_formal_external_result(
                task_id=str(task_id),
                node_version_id=str(node_version_id),
                execution_id=str(execution_id),
                executor_id=str(executor_id),
                model_ref=(str(payload.get("model_ref") or "").strip() or None),
                submitted_at=(str(payload.get("submitted_at") or "").strip() or None),
                output=output,
                actor=str(actor),
                idempotency_key=str(idempotency_key),
                experience_usage=payload.get("experience_usage") if isinstance(payload.get("experience_usage"), Mapping) else None,
                validation_usage=payload.get("validation_usage") if isinstance(payload.get("validation_usage"), Mapping) else None,
                user_requirements=str(payload.get("user_requirements") or "continue the current formal production task"),
                external_executor=external_executor,
            )

        if action == "view_task":
            (task_id,) = required("task_id")
            return self.core.get_task(str(task_id))

        if action == "view_research_materials":
            (task_id,) = required("task_id")
            materials = self.core.list_research_materials(task_id=str(task_id))
            screened = _screen_research_materials(materials)
            tasks: dict[str, dict[str, int]] = {}
            execution_versions: dict[str, int] = {}
            plan_fingerprints: dict[str, int] = {}
            total_material_chars = 0
            total_extracted_chars = 0
            for item in materials:
                material = dict(item.get("material") or {})
                task_key = str(material.get("research_task_id") or "unassigned")
                execution_key = str(material.get("research_execution") or "unversioned")
                fingerprint_key = str(
                    material.get("research_plan_fingerprint") or "unfingerprinted"
                )
                execution_versions[execution_key] = execution_versions.get(execution_key, 0) + 1
                plan_fingerprints[fingerprint_key] = plan_fingerprints.get(fingerprint_key, 0) + 1
                stats = tasks.setdefault(
                    task_key,
                    {"source_count": 0, "material_chars": 0, "extracted_chars": 0},
                )
                material_chars = len(json.dumps(material, ensure_ascii=False))
                extracted_chars = len(str(material.get("extracted_content") or ""))
                stats["source_count"] += 1
                stats["material_chars"] += material_chars
                stats["extracted_chars"] += extracted_chars
                total_material_chars += material_chars
                total_extracted_chars += extracted_chars
            return {
                "task_id": str(task_id),
                "source_count": len(materials),
                "screened_source_count": len(screened),
                "total_material_chars": total_material_chars,
                "total_extracted_chars": total_extracted_chars,
                "screened_material_chars": len(json.dumps(screened, ensure_ascii=False)),
                "execution_versions": execution_versions,
                "plan_fingerprints": plan_fingerprints,
                "tasks": tasks,
            }

        if action == "view_research_failure":
            (task_id,) = required("task_id")
            snapshot = next(
                item
                for item in self.core.list_content_workbench()
                if str(item.get("task_id")) == str(task_id)
            )
            failure = snapshot.get("failure") or {}
            raw_output = failure.get("raw_model_output")
            return {
                "task_id": str(task_id),
                "current_status": snapshot.get("current_status"),
                "failure_stage": failure.get("failure_stage"),
                "reason": failure.get("reason"),
                "raw_model_output_status": failure.get("raw_model_output_status"),
                "raw_model_output_length": len(str(raw_output or "")),
                "raw_model_output": raw_output if isinstance(raw_output, str) else None,
            }

        if action == "view_accounts":
            return self.core.list_knowledge_account_registry()

        raise StateTransitionError(f"unsupported formal research action: {action}")

    def plan_daily_repair(self, *, as_of_business_date: str) -> dict[str, Any]:
        return self.core.daily_observation_reconciliation_plan(
            as_of_business_date=as_of_business_date
        )

    def validate_daily_repair_request(
        self,
        *,
        as_of_business_date: str,
        now: datetime | None = None,
    ) -> str:
        """Apply the existing one-missed-date repair eligibility rule."""

        china_time = timezone(timedelta(hours=8))
        current_date = (now or datetime.now(china_time)).astimezone(china_time).date()
        try:
            selected_date = datetime.strptime(
                str(as_of_business_date), "%Y-%m-%d"
            ).date()
        except ValueError as exc:
            raise StateTransitionError(
                "formal daily repair business date must use YYYY-MM-DD"
            ) from exc
        if selected_date != current_date - timedelta(days=1):
            raise StateTransitionError(
                "formal daily repair is limited to the immediately missed prior date"
            )
        return selected_date.isoformat()

    def apply_daily_repair(
        self,
        *,
        as_of_business_date: str,
        recovery_observations: tuple[dict[str, Any], ...],
        repair_run_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.core.reconcile_daily_observation_history(
            as_of_business_date=as_of_business_date,
            recovery_observations=recovery_observations,
            repair_run_id=repair_run_id,
            actor=actor,
            reason=reason,
        )

    def apply_knowledge_convergence(self, *, actor: str) -> dict[str, Any]:
        return self.core.converge_knowledge_data(actor=actor)

    def read_cold_start_lifecycle(self, *, cold_start_id: str) -> dict[str, Any]:
        run_id = str(cold_start_id or "").strip()
        if not run_id:
            raise StateTransitionError("cold-start lifecycle requires a run identity")
        row = self.core.conn.execute(
            "SELECT cold_start_id, status FROM stage0_cold_start "
            "WHERE cold_start_id=? AND data_identity=?",
            (run_id, self.core.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("cold-start run does not exist")
        return {key: row[key] for key in row.keys()}

    def validate_cold_start_background(
        self,
        *,
        configuration_id: str,
        cold_start_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        configuration = self.core.get_cold_start_configuration(
            configuration_id=configuration_id
        )
        run_id = str(cold_start_id or "").strip()
        if str(configuration.get("cold_start_id") or "") != run_id:
            raise StateTransitionError(
                "background configuration does not point to the requested run"
            )
        run = self.read_cold_start_lifecycle(cold_start_id=run_id)
        if str(run["status"]) != "running":
            raise StateTransitionError(
                f"background cold-start run is not running: {run['status']}"
            )
        return configuration, run

    def execute_cold_start_background(
        self,
        *,
        configuration_id: str,
        cold_start_id: str,
        actor: str,
        registration_service: Any,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.validate_cold_start_background(
            configuration_id=configuration_id,
            cold_start_id=cold_start_id,
        )
        from scripts.core.production.cold_start_onboarding import (
            ColdStartOnboardingService,
        )

        service_kwargs: dict[str, Any] = {
            "core": self.core,
            "execution_registration_service": registration_service,
        }
        if progress_callback is not None:
            service_kwargs["execution_progress_callback"] = progress_callback
        result = ColdStartOnboardingService(
            **service_kwargs,
        ).continue_current_cold_start(
            configuration_id=configuration_id,
            actor=actor,
        )
        if str(result.get("cold_start_id") or "") != str(cold_start_id):
            raise StateTransitionError(
                "background execution returned a different cold-start run"
            )
        return result

    def fail_cold_start_background(
        self,
        *,
        configuration_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.core.fail_configured_cold_start(
            configuration_id=configuration_id,
            actor=actor,
            reason=reason,
        )

    def fail_cold_start_background_if_running(
        self,
        *,
        configuration_id: str,
        cold_start_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        lifecycle = self.read_cold_start_lifecycle(cold_start_id=cold_start_id)
        if str(lifecycle["status"]) == "running":
            return self.fail_cold_start_background(
                configuration_id=configuration_id,
                actor=actor,
                reason=reason,
            )
        return lifecycle

    @staticmethod
    def validate_cold_start_operation(
        *,
        operation: str,
        cold_start_id: str | None,
        explicit_user_confirmation: bool,
        status_snapshot: Mapping[str, Any] | None,
        explicit_boundary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Validate operation/state rules outside the Hermes transport."""

        review_requirements = {
            "review_tags": ("tags", "awaiting_human_review"),
            "review_content_types": ("content_types", "awaiting_human_decision"),
            "review_domain_boundary": (
                "production_boundary",
                "awaiting_human_decision",
            ),
        }
        run_id = str(cold_start_id or "").strip()
        if operation in {
            "status",
            "stop",
            "resume",
            *review_requirements,
        } and operation != "status" and not run_id:
            raise StateTransitionError(
                f"cold-start {operation} requires the exact cold_start_id"
            )
        if operation in {"preview", "confirm"} and run_id:
            raise StateTransitionError(
                f"cold-start {operation} cannot target an existing cold_start_id"
            )
        if operation in {"confirm", "stop", *review_requirements} and not explicit_user_confirmation:
            raise StateTransitionError(
                f"cold-start {operation} requires explicit user confirmation"
            )
        if operation in {"preview", "confirm", "status"}:
            return None
        if not isinstance(status_snapshot, Mapping):
            raise StateTransitionError(
                "cold-start operation cannot verify the bound run state"
            )
        if str(status_snapshot.get("cold_start_id") or "") != run_id:
            raise StateTransitionError(
                "cold-start operation does not resolve to the requested run"
            )
        run_status = str(status_snapshot.get("status") or "").strip()
        if operation == "stop":
            if run_status != "running":
                raise StateTransitionError(
                    f"cold-start stop is blocked while the run is {run_status or 'unknown'}"
                )
            return dict(status_snapshot)
        if operation == "resume":
            if run_status not in {"stopped", "failed"}:
                raise StateTransitionError(
                    f"cold-start resume is blocked while the run is {run_status or 'unknown'}"
                )
            return dict(status_snapshot)
        if operation == "review_domain_boundary" and isinstance(
            explicit_boundary, Mapping
        ) and isinstance(
            status_snapshot.get("human_confirmation"), Mapping
        ):
            current_boundary = status_snapshot["human_confirmation"].get(
                "production_boundary"
            )
            if run_status == "completed" and current_boundary is None:
                return dict(status_snapshot)
            if run_status == "waiting_human" and current_boundary is None:
                pending = status_snapshot.get("pending_actions")
                if isinstance(pending, list) and operation in pending:
                    return dict(status_snapshot)
        if run_status != "waiting_human":
            raise StateTransitionError(
                f"cold-start {operation} is blocked while the run is {run_status or 'unknown'}"
            )
        pending = status_snapshot.get("pending_actions")
        if not isinstance(pending, list) or operation not in pending:
            raise StateTransitionError(
                f"cold-start {operation} is not a pending human action for this run"
            )
        section, expected_status = review_requirements[operation]
        confirmation = status_snapshot.get("human_confirmation")
        candidate = (
            confirmation.get(section)
            if isinstance(confirmation, Mapping)
            else None
        )
        if not isinstance(candidate, Mapping) or str(candidate.get("status") or "") != expected_status:
            raise StateTransitionError(
                f"cold-start {operation} is not the currently open human-confirmation item"
            )
        return dict(status_snapshot)


FORMAL_BUSINESS_CAPABILITIES = {
    "daily": "CreationAssistantFormalBusinessCore.request_daily",
    "daily_resume": "CreationAssistantFormalBusinessCore.resume_daily",
    "daily_lifecycle": "CreationAssistantFormalBusinessCore.finish_daily",
    "cold_start": "CreationAssistantFormalBusinessCore.start_cold_start",
    "cold_start_resume": "CreationAssistantFormalBusinessCore.resume_cold_start",
    "cold_start_stop": "CreationAssistantFormalBusinessCore.stop_cold_start",
    "human_decision": "CreationAssistantFormalBusinessCore.submit_human_decision",
    "candidate_decision": "CreationAssistantFormalBusinessCore.record_discovery_decision",
    "candidate_selection": "CreationAssistantFormalBusinessCore.select_discovery_candidate",
    "formal_research": "Stage1AResearchPlanService (existing Core service)",
    "daily_repair": "Stage0ContentProductionCore.reconcile_daily_observation_history",
    "knowledge_apply": "Stage0ContentProductionCore.converge_knowledge_data",
}
