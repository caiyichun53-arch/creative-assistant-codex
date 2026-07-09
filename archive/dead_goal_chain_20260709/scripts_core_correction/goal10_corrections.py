from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash
from scripts.core.production.goal08_production_chain import VersionRef

if TYPE_CHECKING:
    from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler


class CorrectionError(RuntimeError):
    pass


ACTION_KINDS = frozenset(
    {
        "no_action",
        "deterministic_recompute",
        "semantic_rerun",
        "replace_current_candidate",
        "require_user_reconfirmation",
        "historical_annotation",
        "human_attention_required",
    }
)

RECONFIRMATION_KINDS = frozenset({"production_approved_draft", "production_approval"})
HISTORICAL_ANNOTATION_KINDS = frozenset({"production_publication_capture"})
SEMANTIC_RERUN_ROLES = frozenset({"input_assembly_included_ref", "binding_local_ref", "generated_by_model_run"})
GOAL10_PROPAGATION_JOB_KIND = "goal10.correction.propagate_impact"


@dataclass(frozen=True)
class CorrectionRegistrationCommand:
    actor: str
    idempotency_key: str
    target_ref: VersionRef
    corrected_payload: dict[str, Any]
    change_scope: str
    reason: str
    evidence_refs: tuple[VersionRef, ...] = ()
    safety_limits: dict[str, int] | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "target_ref": self.target_ref.as_payload(),
            "corrected_payload_hash": content_hash(self.corrected_payload, "goal10.corrected_payload.v1"),
            "change_scope": self.change_scope,
            "reason": self.reason,
            "evidence_refs": [ref.as_payload() for ref in self.evidence_refs],
            "safety_limits": self.safety_limits or {},
        }


@dataclass(frozen=True)
class DependencyEdge:
    source_version_id: str
    source_object_kind: str
    relation_role: str
    target_ref: VersionRef
    edge_source: str
    local_ref: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_version_id": self.source_version_id,
            "source_object_kind": self.source_object_kind,
            "relation_role": self.relation_role,
            "target_ref": self.target_ref.as_payload(),
            "edge_source": self.edge_source,
            "local_ref": self.local_ref,
        }


@dataclass(frozen=True)
class CorrectionImpact:
    root_id: str
    version_id: str
    target_version_id: str
    action_kind: str
    expected_basis_hash: str
    replayed: bool = False


@dataclass(frozen=True)
class CorrectionRegistrationResult:
    root_id: str
    version_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    dependency_index_version_ids: tuple[str, ...]
    impacts: tuple[CorrectionImpact, ...]
    replayed: bool = False


@dataclass(frozen=True)
class CorrectionPropagationCommand:
    actor: str
    idempotency_key: str
    impact_root_id: str
    expected_basis_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None
    inject_fault_after_processing: bool = False

    def request_payload(self) -> dict[str, Any]:
        return {
            "impact_root_id": self.impact_root_id,
            "expected_basis_hash": self.expected_basis_hash,
        }


@dataclass(frozen=True)
class CorrectionResumeCommand:
    actor: str
    idempotency_key: str
    impact_root_id: str
    human_action: str
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "impact_root_id": self.impact_root_id,
            "human_action": self.human_action,
        }


@dataclass(frozen=True)
class CorrectionReportCommand:
    actor: str
    idempotency_key: str
    correction_version_id: str
    correlation_id: str | None = None
    causation_id: str | None = None
    inject_fault_after_version: bool = False

    def request_payload(self) -> dict[str, Any]:
        return {
            "correction_version_id": self.correction_version_id,
        }


@dataclass(frozen=True)
class CorrectionPropagationResult:
    impact_root_id: str
    impact_version_id: str
    processing_status: str
    stop_reason: str | None
    result: dict[str, Any]
    receipt_id: str | None = None
    audit_id: str | None = None
    outbox_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class CorrectionJobEnqueueResult:
    impact_root_id: str
    job_id: str
    replayed: bool = False


@dataclass(frozen=True)
class CorrectionReportResult:
    root_id: str
    version_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    replayed: bool = False


class CorrectionMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.conn = store.conn

    def register_correction(self, command: CorrectionRegistrationCommand) -> CorrectionRegistrationResult:
        self._validate_command(command)
        existing = self._existing_result(command)
        if existing is not None:
            return existing
        old_hash = self._version_content_hash(command.target_ref.target_version_id)
        corrected_hash = content_hash(command.corrected_payload, "goal10.corrected_payload.v1")
        with self.conn:
            root_id = self.store.create_root("goal10_correction_record")
            payload = {
                "goal": "GOAL-10",
                "target_ref": command.target_ref.as_payload(),
                "corrected_payload": command.corrected_payload,
                "old_content_hash": old_hash,
                "corrected_payload_hash": corrected_hash,
                "change_scope": command.change_scope,
                "reason": command.reason,
                "evidence_refs": [ref.as_payload() for ref in command.evidence_refs],
                "safety_limits": command.safety_limits or {},
                "status": "registered",
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version="goal10.correction_record.v1",
                business_payload={
                    "target_stable_id": command.target_ref.target_stable_id,
                    "target_version_id": command.target_ref.target_version_id,
                    "old_content_hash": old_hash,
                    "corrected_payload_hash": corrected_hash,
                    "change_scope": command.change_scope,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self._record_ref(version_id, command.target_ref)
            for ref in command.evidence_refs:
                self._record_ref(version_id, ref)

            dependency_versions: list[str] = []
            impacts: list[CorrectionImpact] = []
            for edge in self._direct_dependency_edges(command.target_ref):
                dependency_versions.append(self._record_dependency_index(version_id, edge))
                impacts.append(self._record_impact(version_id, edge))

            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "dependency_index_version_ids": dependency_versions,
                "impact_version_ids": [impact.version_id for impact in impacts],
            }
            receipt_id = self.store.record_command(
                command_scope="goal10.correction.register",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal10.correction.registered",
                actor=command.actor,
                object_kind="goal10_correction_record",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "target_ref": command.target_ref.as_payload(),
                    "dependency_count": len(dependency_versions),
                    "impact_count": len(impacts),
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal10.correction.registered",
                payload={
                    "root_id": root_id,
                    "version_id": version_id,
                    "target_ref": command.target_ref.as_payload(),
                    "impact_count": len(impacts),
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=audit_id,
            )
            return CorrectionRegistrationResult(
                root_id=root_id,
                version_id=version_id,
                receipt_id=receipt_id,
                audit_id=audit_id,
                outbox_id=outbox_id,
                dependency_index_version_ids=tuple(dependency_versions),
                impacts=tuple(impacts),
            )

    def process_impact(self, command: CorrectionPropagationCommand) -> CorrectionPropagationResult:
        if not command.actor:
            raise CorrectionError("actor is required")
        if not command.idempotency_key:
            raise CorrectionError("idempotency_key is required")
        existing = self._existing_propagation_result(
            command.idempotency_key,
            command.request_payload(),
            command_scope="goal10.correction.process_impact",
        )
        if existing is not None:
            return existing
        impact_version_id, impact_payload = self._current_impact(command.impact_root_id)
        if impact_payload["expected_basis_hash"] != command.expected_basis_hash:
            raise CorrectionError("impact basis hash mismatch")
        if impact_payload["processing_status"] == "completed":
            return CorrectionPropagationResult(
                impact_root_id=command.impact_root_id,
                impact_version_id=impact_version_id,
                processing_status="completed",
                stop_reason=impact_payload["stop_reason"],
                result=impact_payload["result"] or {},
                replayed=True,
            )
        if impact_payload["processing_status"] == "blocked":
            return CorrectionPropagationResult(
                impact_root_id=command.impact_root_id,
                impact_version_id=impact_version_id,
                processing_status="blocked",
                stop_reason=impact_payload["stop_reason"],
                result=impact_payload["result"] or {},
                replayed=True,
            )

        correlation_id = command.correlation_id or impact_payload["correction_version_id"]
        causation_id = command.causation_id or impact_version_id
        with self.conn:
            processing_result = {"started_from_version_id": impact_version_id}
            prior_result = impact_payload.get("result") or {}
            if prior_result.get("human_action"):
                processing_result["human_action"] = prior_result["human_action"]
            processing_version_id = self._append_impact_version(
                command.impact_root_id,
                impact_payload,
                processing_status="processing",
                result=processing_result,
                stop_reason=None,
                human_attention=None,
            )
            self.store.record_audit(
                event_type="goal10.correction.impact.processing",
                actor=command.actor,
                object_kind="goal10_correction_impact",
                object_id=command.impact_root_id,
                version_id=processing_version_id,
                payload={"expected_basis_hash": command.expected_basis_hash},
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        if command.inject_fault_after_processing:
            raise CorrectionError("injected fault after impact processing mark")

        _current_version_id, current_payload = self._current_impact(command.impact_root_id)
        final_payload, event_type, outbox_topic = self._evaluate_impact(current_payload, command.actor)
        with self.conn:
            final_version_id = self._append_impact_version(
                command.impact_root_id,
                current_payload,
                processing_status=final_payload["processing_status"],
                result=final_payload["result"],
                stop_reason=final_payload["stop_reason"],
                human_attention=final_payload["human_attention"],
            )
            result_payload = {
                "impact_root_id": command.impact_root_id,
                "impact_version_id": final_version_id,
                "processing_status": final_payload["processing_status"],
                "stop_reason": final_payload["stop_reason"],
                "result": final_payload["result"],
            }
            receipt_id = self.store.record_command(
                command_scope="goal10.correction.process_impact",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type=event_type,
                actor=command.actor,
                object_kind="goal10_correction_impact",
                object_id=command.impact_root_id,
                version_id=final_version_id,
                payload={
                    "receipt_id": receipt_id,
                    "processing_status": final_payload["processing_status"],
                    "stop_reason": final_payload["stop_reason"],
                    "result": final_payload["result"],
                },
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic=outbox_topic,
                payload={
                    "impact_root_id": command.impact_root_id,
                    "impact_version_id": final_version_id,
                    "processing_status": final_payload["processing_status"],
                    "stop_reason": final_payload["stop_reason"],
                },
                correlation_id=correlation_id,
                causation_id=audit_id,
            )
        return CorrectionPropagationResult(
            impact_root_id=command.impact_root_id,
            impact_version_id=final_version_id,
            processing_status=final_payload["processing_status"],
            stop_reason=final_payload["stop_reason"],
            result=final_payload["result"],
            receipt_id=receipt_id,
            audit_id=audit_id,
            outbox_id=outbox_id,
        )

    def enqueue_impact_jobs(
        self,
        scheduler: "Goal03Scheduler",
        correction_version_id: str,
        *,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> tuple[CorrectionJobEnqueueResult, ...]:
        results: list[CorrectionJobEnqueueResult] = []
        for root_id, payload in self._unfinished_impacts(correction_version_id):
            enqueued = scheduler.enqueue_job(
                job_kind=GOAL10_PROPAGATION_JOB_KIND,
                payload={
                    "impact_root_id": root_id,
                    "expected_basis_hash": payload["expected_basis_hash"],
                    "correlation_id": correction_version_id,
                    "causation_id": payload["correction_version_id"],
                },
                idempotency_key=f"goal10.impact.{root_id}.{payload['expected_basis_hash']}",
                priority=priority,
                max_attempts=max_attempts,
                correlation_id=correction_version_id,
                causation_id=payload["correction_version_id"],
            )
            results.append(CorrectionJobEnqueueResult(root_id, enqueued.job_id, enqueued.replayed))
        return tuple(results)

    def propagation_job_handler(self, actor: str = "goal10-propagation-worker"):
        def _handler(payload: dict[str, Any]) -> dict[str, Any]:
            result = self.process_impact(
                CorrectionPropagationCommand(
                    actor=actor,
                    idempotency_key=f"goal10.process.{payload['impact_root_id']}.{payload['expected_basis_hash']}",
                    impact_root_id=payload["impact_root_id"],
                    expected_basis_hash=payload["expected_basis_hash"],
                    correlation_id=payload.get("correlation_id"),
                    causation_id=payload.get("causation_id"),
                )
            )
            return {
                "impact_root_id": result.impact_root_id,
                "processing_status": result.processing_status,
            }

        return _handler

    def resume_blocked_impact(self, command: CorrectionResumeCommand) -> CorrectionPropagationResult:
        if not command.actor:
            raise CorrectionError("actor is required")
        if not command.idempotency_key:
            raise CorrectionError("idempotency_key is required")
        if not command.human_action:
            raise CorrectionError("human_action is required")
        existing = self._existing_propagation_result(
            command.idempotency_key,
            command.request_payload(),
            command_scope="goal10.correction.resume_impact",
        )
        if existing is not None:
            return existing
        impact_version_id, impact_payload = self._current_impact(command.impact_root_id)
        if impact_payload["processing_status"] != "blocked":
            raise CorrectionError("impact is not blocked")
        correlation_id = command.correlation_id or impact_payload["correction_version_id"]
        causation_id = command.causation_id or impact_version_id
        with self.conn:
            resumed_version_id = self._append_impact_version(
                command.impact_root_id,
                impact_payload,
                processing_status="planned",
                result={
                    "resumed_from_version_id": impact_version_id,
                    "human_action": command.human_action,
                },
                stop_reason=None,
                human_attention={"resolved_by": command.actor, "human_action": command.human_action},
            )
            result_payload = {
                "impact_root_id": command.impact_root_id,
                "impact_version_id": resumed_version_id,
                "processing_status": "planned",
                "stop_reason": None,
                "result": {"human_action": command.human_action},
            }
            receipt_id = self.store.record_command(
                command_scope="goal10.correction.resume_impact",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal10.correction.impact.resumed",
                actor=command.actor,
                object_kind="goal10_correction_impact",
                object_id=command.impact_root_id,
                version_id=resumed_version_id,
                payload={"receipt_id": receipt_id, "human_action": command.human_action},
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal10.correction.impact.resumed",
                payload={"impact_root_id": command.impact_root_id, "impact_version_id": resumed_version_id},
                correlation_id=correlation_id,
                causation_id=audit_id,
            )
        return CorrectionPropagationResult(
            impact_root_id=command.impact_root_id,
            impact_version_id=resumed_version_id,
            processing_status="planned",
            stop_reason=None,
            result={"human_action": command.human_action},
            receipt_id=receipt_id,
            audit_id=audit_id,
            outbox_id=outbox_id,
        )

    def process_unfinished_impacts(
        self,
        *,
        actor: str,
        correction_version_id: str,
        limit: int | None = None,
    ) -> tuple[CorrectionPropagationResult, ...]:
        results: list[CorrectionPropagationResult] = []
        for root_id, payload in self._unfinished_impacts(correction_version_id):
            results.append(
                self.process_impact(
                    CorrectionPropagationCommand(
                        actor=actor,
                        idempotency_key=f"goal10.resume.{root_id}.{payload['expected_basis_hash']}",
                        impact_root_id=root_id,
                        expected_basis_hash=payload["expected_basis_hash"],
                        correlation_id=correction_version_id,
                        causation_id=payload["correction_version_id"],
                    )
                )
            )
            if limit is not None and len(results) >= limit:
                break
        return tuple(results)

    def create_report(self, command: CorrectionReportCommand) -> CorrectionReportResult:
        if not command.actor:
            raise CorrectionError("actor is required")
        if not command.idempotency_key:
            raise CorrectionError("idempotency_key is required")
        if not command.correction_version_id:
            raise CorrectionError("correction_version_id is required")
        existing = self._existing_report_result(command)
        if existing is not None:
            return existing
        correction_payload = self._correction_payload(command.correction_version_id)
        summary = self._report_summary(command.correction_version_id)
        unfinished = [
            item
            for item in summary["impacts"]
            if item["processing_status"] in {"planned", "processing"}
        ]
        if unfinished:
            raise CorrectionError("cannot create correction_report with unfinished impacts")

        correlation_id = command.correlation_id or command.correction_version_id
        causation_id = command.causation_id or command.correction_version_id
        with self.conn:
            root_id = self.store.create_root("goal10_correction_report")
            payload = {
                "goal": "GOAL-10",
                "correction_version_id": command.correction_version_id,
                "target_ref": correction_payload["target_ref"],
                "old_content_hash": correction_payload["old_content_hash"],
                "corrected_payload_hash": correction_payload["corrected_payload_hash"],
                "affected_objects": summary["affected_objects"],
                "no_impact_objects": summary["no_impact_objects"],
                "stop_points": summary["stop_points"],
                "failures": summary["failures"],
                "user_confirmations": summary["user_confirmations"],
                "human_attention": summary["human_attention"],
                "impact_count": len(summary["impacts"]),
                "impact_results": summary["impacts"],
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version="goal10.correction_report.v1",
                business_payload={
                    "correction_version_id": command.correction_version_id,
                    "affected_count": len(summary["affected_objects"]),
                    "no_impact_count": len(summary["no_impact_objects"]),
                    "failure_count": len(summary["failures"]),
                    "human_attention_count": len(summary["human_attention"]),
                    "impact_count": len(summary["impacts"]),
                },
            )
            self.store.set_current_version(root_id, version_id)
            self.store.record_object_reference(
                source_version_id=version_id,
                relation_role="reports_correction_record",
                target_object_kind="goal10_correction_record",
                target_stable_id=command.correction_version_id,
                target_version_id=command.correction_version_id,
                target_content_hash=None,
                locator={"version_id": command.correction_version_id},
            )
            for impact in summary["impacts"]:
                self.store.record_object_reference(
                    source_version_id=version_id,
                    relation_role="reports_correction_impact",
                    target_object_kind="goal10_correction_impact",
                    target_stable_id=impact["impact_root_id"],
                    target_version_id=impact["impact_version_id"],
                    target_content_hash=None,
                    locator={"impact_root_id": impact["impact_root_id"]},
                )
            if command.inject_fault_after_version:
                raise CorrectionError("injected fault after report version")
            result_payload = {"root_id": root_id, "version_id": version_id}
            receipt_id = self.store.record_command(
                command_scope="goal10.correction.create_report",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal10.correction.report.created",
                actor=command.actor,
                object_kind="goal10_correction_report",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "correction_version_id": command.correction_version_id,
                    "impact_count": len(summary["impacts"]),
                },
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal10.correction.report.created",
                payload={
                    "root_id": root_id,
                    "version_id": version_id,
                    "correction_version_id": command.correction_version_id,
                    "impact_count": len(summary["impacts"]),
                },
                correlation_id=correlation_id,
                causation_id=audit_id,
            )
        return CorrectionReportResult(root_id, version_id, receipt_id, audit_id, outbox_id)

    def _record_dependency_index(self, correction_version_id: str, edge: DependencyEdge) -> str:
        root_id = self.store.create_root("goal10_dependency_index")
        payload = {
            "goal": "GOAL-10",
            "correction_version_id": correction_version_id,
            "edge": edge.as_payload(),
        }
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal10.dependency_index.v1",
            business_payload={
                "correction_version_id": correction_version_id,
                "source_version_id": edge.source_version_id,
                "target_version_id": edge.target_ref.target_version_id,
                "relation_role": edge.relation_role,
            },
        )
        self.store.set_current_version(root_id, version_id)
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="indexed_correction_record",
            target_object_kind="goal10_correction_record",
            target_stable_id=correction_version_id,
            target_version_id=correction_version_id,
            target_content_hash=None,
            locator={"version_id": correction_version_id},
        )
        self._record_ref(version_id, edge.target_ref)
        return version_id

    def _record_impact(self, correction_version_id: str, edge: DependencyEdge) -> CorrectionImpact:
        action_kind = _action_kind_for(edge)
        expected_basis_hash = _impact_basis_hash(correction_version_id, edge, action_kind)
        root_id = self.store.create_root("goal10_correction_impact")
        payload = {
            "goal": "GOAL-10",
            "correction_version_id": correction_version_id,
            "target_version_id": edge.source_version_id,
            "dependency": edge.as_payload(),
            "action_kind": action_kind,
            "expected_basis_hash": expected_basis_hash,
            "processing_status": "planned",
            "result": None,
            "stop_reason": None,
            "human_attention": None,
        }
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal10.correction_impact.v1",
            business_payload={
                "correction_version_id": correction_version_id,
                "target_version_id": edge.source_version_id,
                "action_kind": action_kind,
                "expected_basis_hash": expected_basis_hash,
                "processing_status": "planned",
            },
        )
        self.store.set_current_version(root_id, version_id)
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="impact_for_correction_record",
            target_object_kind="goal10_correction_record",
            target_stable_id=correction_version_id,
            target_version_id=correction_version_id,
            target_content_hash=None,
            locator={"version_id": correction_version_id},
        )
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="impact_target_version",
            target_object_kind=edge.source_object_kind,
            target_stable_id=edge.source_version_id,
            target_version_id=edge.source_version_id,
            target_content_hash=None,
            locator={"version_id": edge.source_version_id},
        )
        return CorrectionImpact(root_id, version_id, edge.source_version_id, action_kind, expected_basis_hash)

    def _evaluate_impact(
        self,
        impact_payload: dict[str, Any],
        actor: str,
    ) -> tuple[dict[str, Any], str, str]:
        action_kind = impact_payload["action_kind"]
        human_action = ((impact_payload.get("result") or {}).get("human_action"))
        if action_kind == "no_action":
            return (
                {
                    "processing_status": "completed",
                    "result": {"action": "no_action"},
                    "stop_reason": "no_action",
                    "human_attention": None,
                },
                "goal10.correction.impact.completed",
                "goal10.correction.impact.completed",
            )
        if action_kind in {"require_user_reconfirmation", "historical_annotation", "human_attention_required"}:
            if human_action:
                return (
                    {
                        "processing_status": "completed",
                        "result": {"action": action_kind, "human_action": human_action},
                        "stop_reason": "human_action_recorded",
                        "human_attention": {"resolved_by": actor, "human_action": human_action},
                    },
                    "goal10.correction.impact.completed",
                    "goal10.correction.impact.completed",
                )
            return (
                {
                    "processing_status": "blocked",
                    "result": {"action": action_kind},
                    "stop_reason": action_kind,
                    "human_attention": {"required": True, "action_kind": action_kind},
                },
                "goal10.correction.impact.blocked",
                "goal10.correction.impact.blocked",
            )
        return self._evaluate_materialized_replacement(impact_payload)

    def _evaluate_materialized_replacement(
        self,
        impact_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        correction_payload = self._correction_payload(impact_payload["correction_version_id"])
        target_row = self.conn.execute(
            """
            SELECT tv.version_id, tv.root_id, tv.business_hash, tv.projection_version, tv.payload_json
              FROM trace_version tv
             WHERE tv.version_id=?
            """,
            (impact_payload["target_version_id"],),
        ).fetchone()
        if target_row is None:
            return (
                {
                    "processing_status": "blocked",
                    "result": {"missing_target_version_id": impact_payload["target_version_id"]},
                    "stop_reason": "human_attention_required",
                    "human_attention": {"required": True, "reason": "missing_target_version"},
                },
                "goal10.correction.impact.blocked",
                "goal10.correction.impact.blocked",
            )
        old_payload = json.loads(target_row["payload_json"])
        new_payload = _apply_corrected_payload(old_payload, correction_payload["corrected_payload"])
        business_payload = _business_payload_from_version_payload(new_payload)
        new_business_hash = content_hash(business_payload, target_row["projection_version"])
        old_business_hash = str(target_row["business_hash"] or "")
        refs_unchanged = self._reference_signature(target_row["version_id"]) == self._reference_signature(
            target_row["version_id"]
        )
        if old_business_hash == new_business_hash and refs_unchanged:
            return (
                {
                    "processing_status": "completed",
                    "result": {
                        "business_hash_before": old_business_hash,
                        "business_hash_after": new_business_hash,
                        "replacement_version_id": None,
                    },
                    "stop_reason": "business_equivalent_refs_unchanged",
                    "human_attention": None,
                },
                "goal10.correction.impact.completed",
                "goal10.correction.impact.completed",
            )

        existing_replacement = self._existing_replacement_version(
            str(target_row["root_id"]),
            str(target_row["version_id"]),
            impact_payload["correction_version_id"],
        )
        if existing_replacement is not None:
            return (
                {
                    "processing_status": "completed",
                    "result": {
                        "business_hash_before": old_business_hash,
                        "business_hash_after": new_business_hash,
                        "replacement_version_id": existing_replacement,
                    },
                    "stop_reason": "replacement_version_created",
                    "human_attention": None,
                },
                "goal10.correction.impact.completed",
                "goal10.correction.impact.completed",
            )

        with self.conn:
            replacement_version_id = self.store.append_version(
                target_row["root_id"],
                new_payload,
                projection_version=target_row["projection_version"],
                based_on_version_id=target_row["version_id"],
                business_payload=business_payload,
            )
            self.store.set_current_version(target_row["root_id"], replacement_version_id)
            self._copy_refs(target_row["version_id"], replacement_version_id)
            self.store.record_object_reference(
                source_version_id=replacement_version_id,
                relation_role="corrected_by_goal10_correction",
                target_object_kind="goal10_correction_record",
                target_stable_id=impact_payload["correction_version_id"],
                target_version_id=impact_payload["correction_version_id"],
                target_content_hash=None,
                locator={"correction_version_id": impact_payload["correction_version_id"]},
            )
        return (
            {
                "processing_status": "completed",
                "result": {
                    "business_hash_before": old_business_hash,
                    "business_hash_after": new_business_hash,
                    "replacement_version_id": replacement_version_id,
                },
                "stop_reason": "replacement_version_created",
                "human_attention": None,
            },
            "goal10.correction.impact.completed",
            "goal10.correction.impact.completed",
        )

    def _append_impact_version(
        self,
        root_id: str,
        previous_payload: dict[str, Any],
        *,
        processing_status: str,
        result: dict[str, Any] | None,
        stop_reason: str | None,
        human_attention: dict[str, Any] | None,
    ) -> str:
        _previous_version_id, current_payload = self._current_impact(root_id)
        payload = dict(current_payload)
        payload["processing_status"] = processing_status
        payload["result"] = result
        payload["stop_reason"] = stop_reason
        payload["human_attention"] = human_attention
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal10.correction_impact.v1",
            based_on_version_id=_previous_version_id,
            business_payload={
                "correction_version_id": payload["correction_version_id"],
                "target_version_id": payload["target_version_id"],
                "action_kind": payload["action_kind"],
                "expected_basis_hash": payload["expected_basis_hash"],
                "processing_status": processing_status,
                "stop_reason": stop_reason,
            },
        )
        self.store.set_current_version(root_id, version_id)
        return version_id

    def _current_impact(self, root_id: str) -> tuple[str, dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT tv.version_id, tv.payload_json
              FROM trace_root tr
              JOIN trace_version tv ON tv.version_id=tr.current_version_id
             WHERE tr.root_id=? AND tr.object_kind='goal10_correction_impact'
            """,
            (root_id,),
        ).fetchone()
        if row is None:
            raise CorrectionError("impact root does not exist")
        return str(row["version_id"]), json.loads(row["payload_json"])

    def _unfinished_impacts(self, correction_version_id: str) -> tuple[tuple[str, dict[str, Any]], ...]:
        rows = self.conn.execute(
            """
            SELECT tr.root_id, tv.payload_json
              FROM trace_root tr
              JOIN trace_version tv ON tv.version_id=tr.current_version_id
             WHERE tr.object_kind='goal10_correction_impact'
             ORDER BY tr.created_at, tr.root_id
            """
        ).fetchall()
        results: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload["correction_version_id"] == correction_version_id and payload["processing_status"] in {
                "planned",
                "processing",
            }:
                results.append((str(row["root_id"]), payload))
        return tuple(results)

    def _correction_payload(self, correction_version_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload_json FROM trace_version WHERE version_id=?",
            (correction_version_id,),
        ).fetchone()
        if row is None:
            raise CorrectionError("correction version does not exist")
        return json.loads(row["payload_json"])

    def _existing_propagation_result(
        self,
        idempotency_key: str,
        request_payload: dict[str, Any],
        *,
        command_scope: str,
    ) -> CorrectionPropagationResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (command_scope, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(request_payload):
            raise IdempotencyConflict("idempotency key reused with different request")
        payload = json.loads(row["result_json"])
        return CorrectionPropagationResult(
            impact_root_id=payload["impact_root_id"],
            impact_version_id=payload["impact_version_id"],
            processing_status=payload["processing_status"],
            stop_reason=payload["stop_reason"],
            result=payload["result"],
            receipt_id=row["receipt_id"],
            replayed=True,
        )

    def _existing_report_result(self, command: CorrectionReportCommand) -> CorrectionReportResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal10.correction.create_report", command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(command.request_payload()):
            raise IdempotencyConflict("idempotency key reused with different request")
        payload = json.loads(row["result_json"])
        return CorrectionReportResult(
            root_id=str(payload["root_id"]),
            version_id=str(payload["version_id"]),
            receipt_id=str(row["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            replayed=True,
        )

    def _report_summary(self, correction_version_id: str) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT tr.root_id, tv.version_id, tv.payload_json
              FROM trace_root tr
              JOIN trace_version tv ON tv.version_id=tr.current_version_id
             WHERE tr.object_kind='goal10_correction_impact'
             ORDER BY tr.created_at, tr.root_id
            """
        ).fetchall()
        impacts: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload["correction_version_id"] != correction_version_id:
                continue
            result = payload.get("result") or {}
            impacts.append(
                {
                    "impact_root_id": row["root_id"],
                    "impact_version_id": row["version_id"],
                    "target_version_id": payload["target_version_id"],
                    "action_kind": payload["action_kind"],
                    "processing_status": payload["processing_status"],
                    "stop_reason": payload["stop_reason"],
                    "result": result,
                    "human_attention": payload.get("human_attention"),
                    "before_hash": result.get("business_hash_before"),
                    "after_hash": result.get("business_hash_after"),
                    "replacement_version_id": result.get("replacement_version_id"),
                }
            )
        affected = [
            item
            for item in impacts
            if item["replacement_version_id"] or item["stop_reason"] == "human_action_recorded"
        ]
        no_impact = [
            item
            for item in impacts
            if item["processing_status"] == "completed"
            and item["stop_reason"] in {"business_equivalent_refs_unchanged", "no_action"}
        ]
        failures = [
            item
            for item in impacts
            if item["processing_status"] not in {"completed", "blocked", "planned", "processing"}
        ]
        confirmations = [
            {
                "impact_root_id": item["impact_root_id"],
                "human_action": (item["result"] or {}).get("human_action"),
            }
            for item in impacts
            if (item["result"] or {}).get("human_action")
        ]
        human_attention = [
            item
            for item in impacts
            if item["processing_status"] == "blocked" or item["human_attention"]
        ]
        return {
            "impacts": impacts,
            "affected_objects": affected,
            "no_impact_objects": no_impact,
            "stop_points": [
                {
                    "impact_root_id": item["impact_root_id"],
                    "processing_status": item["processing_status"],
                    "stop_reason": item["stop_reason"],
                }
                for item in impacts
                if item["stop_reason"]
            ],
            "failures": failures,
            "user_confirmations": confirmations,
            "human_attention": human_attention,
        }

    def _copy_refs(self, source_version_id: str, replacement_version_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT relation_role, target_object_kind, target_stable_id, target_version_id,
                   target_content_hash, locator_json
              FROM object_reference
             WHERE source_version_id=?
             ORDER BY relation_role, target_stable_id, target_version_id
            """,
            (source_version_id,),
        ).fetchall()
        for row in rows:
            self.store.record_object_reference(
                source_version_id=replacement_version_id,
                relation_role=row["relation_role"],
                target_object_kind=row["target_object_kind"],
                target_stable_id=row["target_stable_id"],
                target_version_id=row["target_version_id"],
                target_content_hash=row["target_content_hash"],
                locator=json.loads(row["locator_json"]),
            )

    def _reference_signature(self, version_id: str) -> tuple[tuple[str, str, str, str | None, str | None], ...]:
        rows = self.conn.execute(
            """
            SELECT relation_role, target_object_kind, target_stable_id, target_version_id, target_content_hash
              FROM object_reference
             WHERE source_version_id=?
             ORDER BY relation_role, target_object_kind, target_stable_id, target_version_id, target_content_hash
            """,
            (version_id,),
        ).fetchall()
        return tuple(
            (
                row["relation_role"],
                row["target_object_kind"],
                row["target_stable_id"],
                row["target_version_id"],
                row["target_content_hash"],
            )
            for row in rows
        )

    def _existing_replacement_version(
        self,
        target_root_id: str,
        based_on_version_id: str,
        correction_version_id: str,
    ) -> str | None:
        row = self.conn.execute(
            """
            SELECT tv.version_id
              FROM trace_version tv
              JOIN object_reference r ON r.source_version_id=tv.version_id
             WHERE tv.root_id=?
               AND tv.based_on_version_id=?
               AND r.relation_role='corrected_by_goal10_correction'
               AND r.target_version_id=?
             ORDER BY tv.version_no
             LIMIT 1
            """,
            (target_root_id, based_on_version_id, correction_version_id),
        ).fetchone()
        return None if row is None else str(row["version_id"])

    def _direct_dependency_edges(self, target_ref: VersionRef) -> tuple[DependencyEdge, ...]:
        edges: list[DependencyEdge] = []
        rows = self.conn.execute(
            """
            SELECT r.source_version_id,
                   tr.object_kind AS source_object_kind,
                   r.relation_role,
                   r.target_object_kind,
                   r.target_stable_id,
                   r.target_version_id,
                   r.target_content_hash,
                   r.locator_json
              FROM object_reference r
              JOIN trace_version tv ON tv.version_id=r.source_version_id
              JOIN trace_root tr ON tr.root_id=tv.root_id
             WHERE (
                    r.target_version_id=?
                    OR (
                    r.target_version_id IS NULL
                    AND r.target_stable_id=?
                    AND r.target_content_hash=?
                    )
                )
               AND tr.object_kind NOT LIKE 'goal10_%'
             ORDER BY r.source_version_id, r.relation_role
            """,
            (
                target_ref.target_version_id,
                target_ref.target_stable_id,
                target_ref.target_content_hash,
            ),
        ).fetchall()
        for row in rows:
            edges.append(
                DependencyEdge(
                    source_version_id=row["source_version_id"],
                    source_object_kind=row["source_object_kind"],
                    relation_role=row["relation_role"],
                    target_ref=VersionRef(
                        relation_role=row["relation_role"],
                        target_object_kind=row["target_object_kind"],
                        target_stable_id=row["target_stable_id"],
                        target_version_id=row["target_version_id"],
                        target_content_hash=row["target_content_hash"],
                        locator=json.loads(row["locator_json"]),
                    ),
                    edge_source="object_reference",
                )
            )
        for row in self._binding_manifest_edges(target_ref):
            edges.append(row)
        return tuple(edges)

    def _binding_manifest_edges(self, target_ref: VersionRef) -> tuple[DependencyEdge, ...]:
        rows = self.conn.execute(
            """
            SELECT b.source_version_id,
                   tr.object_kind AS source_object_kind,
                   b.local_ref,
                   b.object_ref_json
              FROM binding_manifest b
              JOIN trace_version tv ON tv.version_id=b.source_version_id
              JOIN trace_root tr ON tr.root_id=tv.root_id
             ORDER BY b.source_version_id, b.local_ref
            """
        ).fetchall()
        edges: list[DependencyEdge] = []
        for row in rows:
            payload = json.loads(row["object_ref_json"])
            ref = VersionRef(
                relation_role=payload.get("relation_role") or "binding_local_ref",
                target_object_kind=payload.get("target_object_kind") or "",
                target_stable_id=payload.get("target_stable_id") or "",
                target_version_id=payload.get("target_version_id"),
                target_content_hash=payload.get("target_content_hash"),
                locator=payload.get("locator") or {"local_ref": row["local_ref"]},
            )
            if _same_target(ref, target_ref):
                edges.append(
                    DependencyEdge(
                        source_version_id=row["source_version_id"],
                        source_object_kind=row["source_object_kind"],
                        relation_role=ref.relation_role,
                        target_ref=ref,
                        edge_source="binding_manifest",
                        local_ref=row["local_ref"],
                    )
                )
        return tuple(edges)

    def _version_content_hash(self, version_id: str | None) -> str:
        if not version_id:
            raise CorrectionError("target_ref must include target_version_id")
        row = self.conn.execute("SELECT content_hash FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise CorrectionError("target version does not exist")
        return str(row["content_hash"])

    def _existing_result(self, command: CorrectionRegistrationCommand) -> CorrectionRegistrationResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal10.correction.register", command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(command.request_payload()):
            raise IdempotencyConflict("idempotency key reused with different request")
        payload = json.loads(row["result_json"])
        impact_rows = self.conn.execute(
            """
            SELECT v.root_id, v.version_id, v.payload_json
              FROM trace_version v
              JOIN trace_root r ON r.root_id=v.root_id
             WHERE r.object_kind='goal10_correction_impact'
               AND v.version_id IN ({})
             ORDER BY v.version_id
            """.format(",".join("?" for _ in payload["impact_version_ids"])),
            tuple(payload["impact_version_ids"]),
        ).fetchall() if payload["impact_version_ids"] else []
        impacts = []
        for impact_row in impact_rows:
            impact_payload = json.loads(impact_row["payload_json"])
            impacts.append(
                CorrectionImpact(
                    root_id=impact_row["root_id"],
                    version_id=impact_row["version_id"],
                    target_version_id=impact_payload["target_version_id"],
                    action_kind=impact_payload["action_kind"],
                    expected_basis_hash=impact_payload["expected_basis_hash"],
                    replayed=True,
                )
            )
        return CorrectionRegistrationResult(
            root_id=str(payload["root_id"]),
            version_id=str(payload["version_id"]),
            receipt_id=str(row["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            dependency_index_version_ids=tuple(payload["dependency_index_version_ids"]),
            impacts=tuple(impacts),
            replayed=True,
        )

    def _record_ref(self, source_version_id: str, ref: VersionRef) -> str:
        return self.store.record_object_reference(
            source_version_id=source_version_id,
            relation_role=ref.relation_role,
            target_object_kind=ref.target_object_kind,
            target_stable_id=ref.target_stable_id,
            target_version_id=ref.target_version_id,
            target_content_hash=ref.target_content_hash,
            locator=ref.locator,
        )

    @staticmethod
    def _validate_command(command: CorrectionRegistrationCommand) -> None:
        if not command.actor:
            raise CorrectionError("actor is required")
        if not command.idempotency_key:
            raise CorrectionError("idempotency_key is required")
        if not command.corrected_payload:
            raise CorrectionError("corrected_payload is required")
        if not command.change_scope:
            raise CorrectionError("change_scope is required")
        if not command.reason:
            raise CorrectionError("reason is required")
        _validate_version_ref(command.target_ref)
        for ref in command.evidence_refs:
            _validate_version_ref(ref)
        if command.safety_limits:
            for key, value in command.safety_limits.items():
                if not isinstance(key, str) or not isinstance(value, int) or value <= 0:
                    raise CorrectionError("safety_limits must map string keys to positive integers")


def _same_target(ref: VersionRef, target_ref: VersionRef) -> bool:
    if ref.target_version_id and target_ref.target_version_id:
        return ref.target_version_id == target_ref.target_version_id
    return (
        ref.target_stable_id == target_ref.target_stable_id
        and ref.target_content_hash == target_ref.target_content_hash
        and ref.target_content_hash is not None
    )


def _action_kind_for(edge: DependencyEdge) -> str:
    if edge.relation_role.startswith("discarded"):
        return "no_action"
    if edge.source_object_kind in RECONFIRMATION_KINDS:
        return "require_user_reconfirmation"
    if edge.source_object_kind in HISTORICAL_ANNOTATION_KINDS:
        return "historical_annotation"
    if edge.relation_role in SEMANTIC_RERUN_ROLES or edge.edge_source == "binding_manifest":
        return "semantic_rerun"
    return "deterministic_recompute"


def _impact_basis_hash(correction_version_id: str, edge: DependencyEdge, action_kind: str) -> str:
    return content_hash(
        {
            "correction_version_id": correction_version_id,
            "target_ref": edge.target_ref.as_payload(),
            "source_version_id": edge.source_version_id,
            "action_kind": action_kind,
        },
        "goal10.impact_basis.v1",
    )


def _apply_corrected_payload(payload: dict[str, Any], corrected_payload: dict[str, Any]) -> dict[str, Any]:
    updated = json.loads(json.dumps(payload))
    content_payload = updated.get("content_payload")
    if isinstance(content_payload, dict):
        for key, value in corrected_payload.items():
            if key in content_payload:
                content_payload[key] = value
        return updated
    for key, value in corrected_payload.items():
        if key in updated:
            updated[key] = value
    return updated


def _business_payload_from_version_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("content_payload"), dict):
        return dict(payload["content_payload"])
    return dict(payload)


def _validate_version_ref(ref: VersionRef) -> None:
    if not ref.relation_role:
        raise CorrectionError("reference relation_role is required")
    if not ref.target_object_kind:
        raise CorrectionError("reference target_object_kind is required")
    if not ref.target_stable_id:
        raise CorrectionError("reference target_stable_id is required")
    if not ref.target_version_id and not ref.target_content_hash:
        raise CorrectionError("reference must point to a concrete version or content hash")
    if not ref.locator:
        raise CorrectionError("reference locator is required")
