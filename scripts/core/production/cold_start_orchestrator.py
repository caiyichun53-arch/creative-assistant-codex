"""Automatic execution for one confirmed cold-start run.

This module coordinates the existing per-account registration service and
starts the deterministic whole-library tag branch as soon as all twenty
accounts finish high-signal selection.  After the current run's breakdown
branch is complete, it also creates the one run-scoped content-type candidate
and domain-boundary candidate reviews.  It does not submit a human review.
Preparation and breakdown follow the strict stage order.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1_competitor_registration import (
    CompetitorRegistrationService,
)


_CONTENT_BRANCH_STEPS = frozenset({
    "historical_material",
    "high_signal_identification",
    "transcripts_and_comments",
    "breakdown",
})


class ColdStartExecutionOrchestrator:
    """Run the current cold-start content branch and expose tag readiness."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        registration_service: CompetitorRegistrationService,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.core = core
        self.registration_service = registration_service
        self.progress_callback = progress_callback
        self.external_executor = external_executor

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback(dict(payload))

    def _phase_summary(
        self,
        *,
        phase: str,
        registration_ids: list[str],
        failed_accounts: int = 0,
    ) -> dict[str, Any]:
        """Derive one user-facing summary from this run's formal artifacts."""

        ids = tuple(str(value) for value in registration_ids if str(value).strip())
        if not ids:
            return {"event": "phase_summary", "phase": phase, "summary": {}}
        placeholders = ",".join("?" for _ in ids)
        rows = self.core.conn.execute(
            "SELECT registration_id, step_name, artifact_refs_json "
            "FROM stage0_competitor_registration_step "
            f"WHERE data_identity=? AND registration_id IN ({placeholders})",
            (self.core.data_identity, *ids),
        ).fetchall()
        step_rows: dict[str, list[Any]] = {}
        for row in rows:
            step_rows.setdefault(str(row["step_name"]), []).append(row)

        def artifacts(step_name: str) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for row in step_rows.get(step_name, []):
                try:
                    payload = json.loads(str(row["artifact_refs_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                values = payload.get("artifact_refs") if isinstance(payload, dict) else None
                if isinstance(values, list):
                    result.extend(item for item in values if isinstance(item, dict))
            return result

        historical = artifacts("historical_material")
        high_signal = artifacts("high_signal_identification")
        historical_items = sum(
            int(item.get("item_count") or len(item.get("items") or []))
            for item in historical
        )
        selected_items = sum(
            len(item.get("selected_items") or [])
            for item in high_signal
        )
        completed_accounts = len(step_rows.get("high_signal_identification", []))
        if phase == "historical_collection":
            success_accounts = len(step_rows.get("historical_material", []))
            return {
                "event": "phase_summary",
                "phase": phase,
                "summary": {
                    "account_total": len(ids),
                    "success_accounts": success_accounts,
                    "failed_accounts": int(failed_accounts),
                    "historical_items": historical_items,
                    "stage_completed": success_accounts == len(ids) and not failed_accounts,
                },
            }
        if phase == "baseline_high_signal":
            return {
                "event": "phase_summary",
                "phase": phase,
                "summary": {
                    "account_total": len(ids),
                    "completed_accounts": completed_accounts,
                    "historical_items": historical_items,
                    "high_signal_items": selected_items,
                    "failed_accounts": int(failed_accounts),
                    "stage_completed": completed_accounts == len(ids) and not failed_accounts,
                },
            }

        item_step = "transcripts_and_comments" if phase == "preparation" else "breakdown"
        item_rows = self.core.conn.execute(
            "SELECT status FROM stage0_competitor_registration_item "
            f"WHERE data_identity=? AND registration_id IN ({placeholders}) AND step_name=?",
            (self.core.data_identity, *ids, item_step),
        ).fetchall()
        item_statuses = [str(row["status"] or "") for row in item_rows]
        completed_items = sum(status == "completed" for status in item_statuses)
        failed_items = sum(status == "failed" for status in item_statuses)
        excluded_items = sum(status == "excluded" for status in item_statuses)
        if phase == "preparation":
            return {
                "event": "phase_summary",
                "phase": phase,
                "summary": {
                    "high_signal_items": selected_items,
                    "prepared_items": completed_items,
                    "detail_items": completed_items,
                    "comment_items": completed_items,
                    "media_items": completed_items,
                    "transcript_items": completed_items,
                    "stage_completed": (
                        completed_items == selected_items
                        and not failed_items
                        and not (selected_items - completed_items - failed_items - excluded_items)
                    ),
                },
            }

        terminal_items = completed_items + failed_items + excluded_items
        return {
            "event": "phase_summary",
            "phase": phase,
            "summary": {
                "total_items": selected_items,
                "pending_items": max(selected_items - terminal_items, 0),
                "success_items": completed_items,
                "failed_items": failed_items,
                "stage_completed": (
                    selected_items == terminal_items
                    and not failed_items
                    and selected_items >= completed_items
                ),
            },
        }

    def run(
        self,
        *,
        cold_start_id: str,
        actor: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Resume every unfinished content-branch step for the current run.

        The old per-account tag step is not a prerequisite for preparation or
        breakdown.  Once all twenty accounts finish high-signal selection,
        the existing deterministic whole-library builder is started once for
        the current run and left awaiting the existing human review entry.
        """
        if not str(cold_start_id or "").strip() or not str(idempotency_key or "").strip():
            raise StateTransitionError(
                "cold-start orchestration requires a run identity and idempotency key"
            )
        actor_value = str(actor or "").strip() or "system"
        status = self.core.get_cold_start_orchestration_status(cold_start_id=cold_start_id)
        def require_active_run() -> None:
            run = self.core.conn.execute(
                "SELECT status FROM stage0_cold_start "
                "WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.core.data_identity),
            ).fetchone()
            if run is None:
                raise StateTransitionError("cold-start run does not exist")
            if str(run["status"]) != "running":
                raise StateTransitionError("cold-start execution is not running")

        require_active_run()

        registration_rows = self.core.conn.execute(
            "SELECT registration.registration_id, account.display_name "
            "FROM stage0_competitor_registration registration "
            "JOIN stage0_content_account account "
            "ON account.content_account_id=registration.competitor_account_id "
            "AND account.data_identity=registration.data_identity "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "ORDER BY registration.created_at, registration.registration_id",
            (cold_start_id, self.core.data_identity),
        ).fetchall()
        registration_ids = [str(row["registration_id"]) for row in registration_rows]
        expected = int(status["expected_registration_total"])
        if len(registration_ids) != expected:
            raise StateTransitionError(
                f"cold-start orchestration requires exactly {expected} current account registrations"
            )

        # A resumed executor reports the work that is already present in the
        # formal step/item records.  This is computed for the notification
        # only; it is not persisted as another progress state.
        prior_step_count = int(self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_step step "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=step.registration_id "
            "AND registration.data_identity=step.data_identity "
            "WHERE registration.cold_start_id=? AND registration.data_identity=?",
            (cold_start_id, self.core.data_identity),
        ).fetchone()[0])
        prior_item_count = int(self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=item.registration_id "
            "AND registration.data_identity=item.data_identity "
            "WHERE registration.cold_start_id=? AND registration.data_identity=?",
            (cold_start_id, self.core.data_identity),
        ).fetchone()[0])
        resumed = bool(prior_step_count or prior_item_count)
        if resumed:
            current_step_rows = self.core.conn.execute(
                "SELECT current_step FROM stage0_competitor_registration "
                "WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.core.data_identity),
            ).fetchall()
            phase_labels = {
                "historical_material": "历史采集",
                "high_signal_identification": "基线计算与高信号筛选",
                "transcripts_and_comments": "备料",
                "breakdown": "内容拆解",
                "tag_candidates": "标签候选",
                "completed": "内容拆解",
            }
            phase_order = (
                "historical_material",
                "high_signal_identification",
                "transcripts_and_comments",
                "breakdown",
                "tag_candidates",
                "completed",
            )
            active_step = "completed"
            current_steps = {str(row["current_step"] or "") for row in current_step_rows}
            for candidate in phase_order:
                if candidate in current_steps and candidate != "completed":
                    active_step = candidate
                    break
            breakdown_summary = self._phase_summary(
                phase="breakdown",
                registration_ids=registration_ids,
            ).get("summary") or {}
            total_items = int(breakdown_summary.get("total_items") or 0)
            completed_items = int(breakdown_summary.get("success_items") or 0)
            self._emit({
                "event": "resume_summary",
                "cold_start_id": cold_start_id,
                "phase": phase_labels.get(active_step, active_step),
                "total_items": total_items,
                "completed_items": completed_items,
                "pending_items": max(total_items - completed_items, 0),
            })

        account_results_by_id: dict[str, dict[str, Any]] = {}
        tag_ready_emitted = bool(status["tag_input_ready"])
        tag_library_result: dict[str, Any] | None = None
        tag_library_error: dict[str, str] | None = None
        content_type_candidate_result: dict[str, Any] | None = None
        content_type_candidate_error: dict[str, str] | None = None
        domain_boundary_candidate_result: dict[str, Any] | None = None
        domain_boundary_candidate_error: dict[str, str] | None = None

        def ensure_tag_library(current_status: dict[str, Any]) -> None:
            nonlocal tag_library_result, tag_library_error
            if not current_status["tag_input_ready"] or tag_library_result is not None or tag_library_error is not None:
                return
            try:
                tag_library_result = self.core.build_cold_start_tag_library(
                    cold_start_id=cold_start_id,
                    actor=actor_value,
                )
            except Exception as exc:
                tag_library_error = {
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
                self._emit({
                    "event": "tag_library_generation_failed",
                    "cold_start_id": cold_start_id,
                    **tag_library_error,
                })
                return
            self._emit({
                "event": "tag_library_generated",
                "cold_start_id": cold_start_id,
                "tag_library_id": tag_library_result.get("tag_library_id"),
                "status": tag_library_result.get("status"),
                "model_calls": 0,
            })

        def ensure_content_type_candidates(current_status: dict[str, Any]) -> None:
            nonlocal content_type_candidate_result, content_type_candidate_error
            if (
                not current_status["breakdown_complete"]
                or content_type_candidate_result is not None
                or content_type_candidate_error is not None
            ):
                return
            try:
                content_type_candidate_result = self.core.build_cold_start_content_type_candidates(
                    cold_start_id=cold_start_id,
                    actor=actor_value,
                )
            except Exception as exc:
                content_type_candidate_error = {
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
                self._emit({
                    "event": "content_type_candidate_generation_failed",
                    "cold_start_id": cold_start_id,
                    **content_type_candidate_error,
                })
                return
            self._emit({
                "event": "content_type_candidates_ready_for_review",
                "cold_start_id": cold_start_id,
                "content_type_candidate_id": content_type_candidate_result.get(
                    "content_type_candidate_id"
                ),
                "candidate_version": content_type_candidate_result.get("candidate_version"),
                "status": content_type_candidate_result.get("status"),
                "model_calls": 0,
            })

        def ensure_domain_boundary_candidates(current_status: dict[str, Any]) -> None:
            nonlocal domain_boundary_candidate_result, domain_boundary_candidate_error
            if (
                not current_status["breakdown_complete"]
                or domain_boundary_candidate_result is not None
                or domain_boundary_candidate_error is not None
            ):
                return
            from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired
            try:
                domain_boundary_candidate_result = self.core.build_cold_start_domain_boundary_candidates(
                    cold_start_id=cold_start_id,
                    actor=actor_value,
                    external_executor=self.external_executor,
                )
            except ExternalIntelligenceRequired:
                raise
            except Exception as exc:
                domain_boundary_candidate_error = {
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
                self._emit({
                    "event": "domain_boundary_candidate_generation_failed",
                    "cold_start_id": cold_start_id,
                    **domain_boundary_candidate_error,
                })
                return
            self._emit({
                "event": "domain_boundary_candidates_ready_for_review",
                "cold_start_id": cold_start_id,
                "boundary_candidate_id": domain_boundary_candidate_result.get(
                    "boundary_candidate_id"
                ),
                "candidate_version": domain_boundary_candidate_result.get("candidate_version"),
                "status": domain_boundary_candidate_result.get("status"),
                "model_calls": int(bool(domain_boundary_candidate_result.get("model_run"))),
                "source_count": int(
                    (domain_boundary_candidate_result.get("source_snapshot") or {}).get("valid_count", 0)
                ),
            })

        def refresh_branch_state() -> dict[str, Any]:
            nonlocal status, tag_ready_emitted
            status = self.core.get_cold_start_orchestration_status(
                cold_start_id=cold_start_id
            )
            ensure_tag_library(status)
            ensure_content_type_candidates(status)
            ensure_domain_boundary_candidates(status)
            if status["tag_input_ready"] and not tag_ready_emitted:
                tag_ready_emitted = True
                self._emit({
                    "event": "tag_input_ready",
                    "cold_start_id": cold_start_id,
                    "selection_completed_count": status["selection_completed_count"],
                    "selection_total_count": status["registration_total"],
                    "depends_on_breakdown": False,
                })
            return status

        refresh_branch_state()
        for registration_row in registration_rows:
            registration_id = str(registration_row["registration_id"])
            account_results_by_id[registration_id] = {
                "registration_id": registration_id,
                "status": "pending",
                "completed_steps": [],
            }
        phase_plan = (

            ("collection", frozenset({"historical_material"})),
            ("screening", frozenset({"high_signal_identification"})),
            ("preparation", frozenset({"transcripts_and_comments"})),
            ("breakdown", frozenset({"breakdown"})),
        )
        step_order = {
            "historical_material": 0,
            "high_signal_identification": 1,
            "transcripts_and_comments": 2,
            "breakdown": 3,
        }

        phase_summary_names = {
            "collection": "historical_collection",
            "screening": "baseline_high_signal",
            "preparation": "preparation",
            "breakdown": "breakdown",
        }
        phase_failure_counts: dict[str, int] = {}
        for phase_name, phase_steps in phase_plan:
            expected_step = next(iter(phase_steps))
            phase_executed = False
            if phase_name == "breakdown" and not resumed:
                breakdown_summary = self._phase_summary(
                    phase="breakdown",
                    registration_ids=registration_ids,
                ).get("summary") or {}
                total_items = int(breakdown_summary.get("total_items") or 0)
                if total_items:
                    self._emit({
                        "event": "breakdown_started",
                        "cold_start_id": cold_start_id,
                        "total_items": total_items,
                        "account_total": len(registration_ids),
                    })
            for registration_position, registration_row in enumerate(registration_rows, start=1):
                registration_id = str(registration_row["registration_id"])
                account_name = str(registration_row["display_name"] or "").strip()
                event_context = {
                    "account_name": account_name,
                    "registration_position": registration_position,
                    "registration_total": len(registration_rows),
                    "phase": phase_name,
                }
                registration_result = account_results_by_id[registration_id]
                if (
                    str(registration_result.get("status") or "").startswith("failed")
                    or registration_result.get("history_insufficient")
                    or registration_result.get("status") in {"blocked", "awaiting_human_review"}
                ):
                    continue
                while True:
                    require_active_run()
                    registration = self.core.get_competitor_registration(
                        registration_id=registration_id
                    )
                    current_status = str(registration["status"])
                    current_step = str(registration["current_step"])
                    if current_status == "completed":
                        registration_result["status"] = "completed"
                        break
                    if current_status == "failed":
                        registration_result["status"] = "failed"
                        registration_result["step"] = current_step
                        registration_result["reason"] = "registration is already marked failed"
                        break
                    if current_status == "awaiting_human_review":
                        completion_key = f"{idempotency_key}:{registration_id}:complete"
                        phase_executed = True
                        try:
                            transition = self.registration_service.run_competitor_registration_step(
                                registration_id=registration_id,
                                actor=actor_value,
                                idempotency_key=completion_key,
                            )
                        except Exception as exc:
                            phase_failure_counts[phase_name] = phase_failure_counts.get(phase_name, 0) + 1
                            self.core.record_cold_start_orchestration_failure(
                                cold_start_id=cold_start_id,
                                registration_id=registration_id,
                                step_name=current_step,
                                actor=actor_value,
                                error_type=type(exc).__name__,
                                reason=str(exc),
                            )
                            registration_result.update({
                                "status": "failed_resumable",
                                "step": current_step,
                                "error_type": type(exc).__name__,
                                "reason": str(exc),
                            })
                            break
                        if transition.get("status") == "completed":
                            registration_result["status"] = "completed"
                            registration_result["completion"] = transition
                        else:
                            registration_result["status"] = "awaiting_human_review"
                            registration_result["step"] = current_step
                            registration_result["transition"] = transition
                        break
                    if current_step not in phase_steps:
                        current_index = step_order.get(current_step)
                        expected_index = step_order[expected_step]
                        if current_index is not None and current_index > expected_index:
                            break
                        registration_result["status"] = "waiting_for_previous"
                        registration_result["step"] = current_step
                        registration_result["reason"] = "phase dependency is not ready"
                        break
                    if current_step == "tag_candidates":
                        registration_result["status"] = "awaiting_tag_branch"
                        registration_result["step"] = current_step
                        break
                    if current_step not in _CONTENT_BRANCH_STEPS:
                        registration_result["status"] = "blocked"
                        registration_result["step"] = current_step
                        registration_result["reason"] = "unsupported content-branch step"
                        break

                    step_key = f"{idempotency_key}:{registration_id}:{current_step}"
                    phase_executed = True
                    self._emit({
                        "event": "registration_step_started",
                        "cold_start_id": cold_start_id,
                        "registration_id": registration_id,
                        **event_context,
                        "step_name": current_step,
                    })
                    try:
                        transition = self.registration_service.run_competitor_registration_step(
                            registration_id=registration_id,
                            actor=actor_value,
                            idempotency_key=step_key,
                        )
                    except Exception as exc:
                        phase_failure_counts[phase_name] = phase_failure_counts.get(phase_name, 0) + 1
                        self.core.record_cold_start_orchestration_failure(
                            cold_start_id=cold_start_id,
                            registration_id=registration_id,
                            step_name=current_step,
                            actor=actor_value,
                            error_type=type(exc).__name__,
                            reason=str(exc),
                        )
                        registration_result.update({
                            "status": "failed_resumable",
                            "step": current_step,
                            "error_type": type(exc).__name__,
                            "reason": str(exc),
                        })
                        break

                    if transition.get("history_insufficient"):
                        registration_result.update({
                            "status": "awaiting_human_review",
                            "step": "historical_material",
                            "history_insufficient": True,
                            "reason": "历史已正常耗尽，但成熟样本不足20条；未进入基线和爆款筛选",
                        })
                        self._emit({
                            "event": "registration_history_insufficient",
                            "cold_start_id": cold_start_id,
                            "registration_id": registration_id,
                            **event_context,
                            "transition": transition,
                        })
                        break

                    registration_result["completed_steps"].append(current_step)
                    self._emit({
                        "event": "registration_step_completed",
                        "cold_start_id": cold_start_id,
                        "registration_id": registration_id,
                        **event_context,
                        "step_name": current_step,
                        "transition": transition,
                    })
                    refresh_branch_state()

            if phase_executed:
                self._emit(self._phase_summary(
                    phase=phase_summary_names[phase_name],
                    registration_ids=registration_ids,
                    failed_accounts=phase_failure_counts.get(phase_name, 0),
                ))

        account_results = [
            account_results_by_id[str(row["registration_id"])]
            for row in registration_rows
        ]

        status = self.core.get_cold_start_orchestration_status(cold_start_id=cold_start_id)
        ensure_tag_library(status)
        ensure_content_type_candidates(status)
        ensure_domain_boundary_candidates(status)
        failed = [
            item for item in account_results
            if str(item.get("status") or "").startswith("failed")
            or item.get("status") == "blocked"
        ]
        waiting_for_tag = [
            item for item in account_results
            if item.get("status") == "awaiting_tag_branch"
        ]
        waiting_for_history = [
            item for item in account_results
            if item.get("history_insufficient")
        ]
        if status["tag_input_ready"]:
            overall_status = "tag_input_ready"
        elif waiting_for_history:
            overall_status = "awaiting_human_review"
        elif failed:
            overall_status = "running_with_resumable_failures"
        else:
            overall_status = "running"
        return {
            "cold_start_id": cold_start_id,
            "status": overall_status,
            "tag_input_ready": bool(status["tag_input_ready"]),
            "selection_progress": {
                "completed": int(status["selection_completed_count"]),
                "total": int(status["registration_total"]),
            },
            "breakdown_progress": {
                "completed": int(status["breakdown_completed_count"]),
                "total": int(status["registration_total"]),
            },
            "tag_branch": {
                "ready": bool(status["tag_input_ready"]),
                "generation_started": tag_library_result is not None,
                "human_review_started": False,
                "waits_for_breakdown": False,
                "accounts_waiting_at_existing_tag_step": len(waiting_for_tag),
                "library": tag_library_result,
                "generation_error": tag_library_error,
            },
            "content_type_branch": {
                "ready": bool(
                    content_type_candidate_result is not None
                    and content_type_candidate_result.get("status") in {
                        "awaiting_human_decision", "frozen"
                    }
                ),
                "candidate_generation_started": content_type_candidate_result is not None,
                "human_review_started": False,
                "waits_for_domain_boundary": False,
                "candidate": content_type_candidate_result,
                "generation_error": content_type_candidate_error,
            },
            "domain_boundary_branch": {
                "ready": bool(
                    domain_boundary_candidate_result is not None
                    and domain_boundary_candidate_result.get("status") in {
                        "awaiting_human_decision", "frozen"
                    }
                ),
                "candidate_generation_started": domain_boundary_candidate_result is not None,
                "human_review_started": False,
                "waits_for_content_type": False,
                "candidate": domain_boundary_candidate_result,
                "generation_error": domain_boundary_candidate_error,
            },
            "account_results": account_results,
            "failures": failed,
            "history_insufficient": waiting_for_history,
        }
