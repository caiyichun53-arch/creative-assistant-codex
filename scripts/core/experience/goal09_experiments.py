from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash
from scripts.core.production.goal08_production_chain import VersionRef


class ExperimentError(RuntimeError):
    pass


METRIC_SIGNALS = frozenset({"supported", "not_supported", "inconclusive", "ineligible"})
ACTUAL_USE_STATUSES = frozenset({"used", "not_used", "unknown"})
EXPERIMENT_KINDS = frozenset({"formal", "observational"})
PUBLICATION_RELATIONS = frozenset({"same_as_approved", "modified_text_provided", "unknown_pending_check"})
REVIEW_PUBLICATION_RELATIONS = frozenset({"modified_text_provided", "unknown_pending_check"})
RECOMMENDATION_STATUSES = frozenset({"active", "watch", "paused", "deprecated"})
EVIDENCE_KINDS = frozenset(
    {
        "formal_p_result",
        "external_support",
        "external_counterexample",
        "structural_revision_signal",
        "human_revision_request",
    }
)


@dataclass(frozen=True)
class PPlusMetricInput:
    metric_name: str
    baseline_value: str | int | float | Decimal | None
    observed_value: str | int | float | Decimal | None
    support_ratio: str | int | float | Decimal = Decimal("1.0")
    checkpoint: str = "P+7d"

    def as_payload(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "baseline_value": _decimal_to_text(self.baseline_value),
            "observed_value": _decimal_to_text(self.observed_value),
            "support_ratio": _decimal_to_text(self.support_ratio),
            "checkpoint": self.checkpoint,
        }


@dataclass(frozen=True)
class ExperimentResultCommand:
    account_id: str
    topic_id: str
    actor: str
    idempotency_key: str
    experiment_kind: str
    primary_hypothesis_ref: VersionRef
    publication_capture_ref: VersionRef
    metric: PPlusMetricInput
    primary_hypothesis_frozen: bool
    actual_use_status: str
    major_confounder: bool = False
    publication_relation: str = "same_as_approved"
    attribution_conflict: bool = False
    notes: tuple[str, ...] = ()
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "topic_id": self.topic_id,
            "experiment_kind": self.experiment_kind,
            "primary_hypothesis_ref": self.primary_hypothesis_ref.as_payload(),
            "publication_capture_ref": self.publication_capture_ref.as_payload(),
            "metric": self.metric.as_payload(),
            "primary_hypothesis_frozen": self.primary_hypothesis_frozen,
            "actual_use_status": self.actual_use_status,
            "major_confounder": self.major_confounder,
            "publication_relation": self.publication_relation,
            "attribution_conflict": self.attribution_conflict,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MetricSignal:
    signal: str
    eligible: bool
    reason: str
    ratio: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "eligible": self.eligible,
            "reason": self.reason,
            "ratio": self.ratio,
        }


@dataclass(frozen=True)
class ExperimentReviewBoundary:
    required: bool
    reasons: tuple[str, ...]
    blocked_by_deterministic_invalidity: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "reasons": list(self.reasons),
            "blocked_by_deterministic_invalidity": self.blocked_by_deterministic_invalidity,
        }


@dataclass(frozen=True)
class ExperimentResult:
    root_id: str
    version_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    metric_signal: MetricSignal
    review_boundary: ExperimentReviewBoundary
    replayed: bool = False


@dataclass(frozen=True)
class ExperienceEvidence:
    evidence_id: str
    evidence_kind: str
    independence_key: str
    eligible: bool = True
    metric_signal: str | None = None
    primary_used: bool = False
    core_question_hash: str | None = None
    requested_action: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_kind": self.evidence_kind,
            "independence_key": self.independence_key,
            "eligible": self.eligible,
            "metric_signal": self.metric_signal,
            "primary_used": self.primary_used,
            "core_question_hash": self.core_question_hash,
            "requested_action": self.requested_action,
        }


@dataclass(frozen=True)
class ExperienceStateInput:
    tactic_key: str
    current_recommendation_status: str
    evidence: tuple[ExperienceEvidence, ...]
    manual_lock: bool = False


@dataclass(frozen=True)
class ProposalTrigger:
    trigger_kind: str
    allowed_proposal_types: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "trigger_kind": self.trigger_kind,
            "allowed_proposal_types": list(self.allowed_proposal_types),
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExperienceStateResult:
    tactic_key: str
    maturity_level: str
    recommendation_status: str
    formal_supports: int
    formal_failures: int
    independent_external_counterexamples: int
    proposal_triggers: tuple[ProposalTrigger, ...]
    audit_reasons: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "tactic_key": self.tactic_key,
            "maturity_level": self.maturity_level,
            "recommendation_status": self.recommendation_status,
            "formal_supports": self.formal_supports,
            "formal_failures": self.formal_failures,
            "independent_external_counterexamples": self.independent_external_counterexamples,
            "proposal_triggers": [trigger.as_payload() for trigger in self.proposal_triggers],
            "audit_reasons": list(self.audit_reasons),
        }


class ExperimentMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.conn = store.conn

    def record_experiment_result(self, command: ExperimentResultCommand) -> ExperimentResult:
        self._validate_command(command)
        existing = self._existing_result(command)
        if existing is not None:
            return existing
        signal = compute_metric_signal(command)
        review_boundary = determine_experiment_review_boundary(command, signal)
        with self.conn:
            root_id = self.store.create_root("goal09_experiment_result")
            payload = {
                "goal": "GOAL-09",
                "account_id": command.account_id,
                "topic_id": command.topic_id,
                "experiment_kind": command.experiment_kind,
                "metric": command.metric.as_payload(),
                "metric_signal": signal.as_payload(),
                "experiment_review_boundary": review_boundary.as_payload(),
                "primary_hypothesis_frozen": command.primary_hypothesis_frozen,
                "actual_use_status": command.actual_use_status,
                "major_confounder": command.major_confounder,
                "publication_relation": command.publication_relation,
                "attribution_conflict": command.attribution_conflict,
                "notes": list(command.notes),
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version="goal09.experiment_result.v1",
                business_payload={
                    "account_id": command.account_id,
                    "topic_id": command.topic_id,
                    "signal": signal.signal,
                    "eligible": signal.eligible,
                    "reason": signal.reason,
                    "review_required": review_boundary.required,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self._record_ref(version_id, command.primary_hypothesis_ref)
            self._record_ref(version_id, command.publication_capture_ref)
            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "metric_signal": signal.as_payload(),
                "experiment_review_boundary": review_boundary.as_payload(),
            }
            receipt_id = self.store.record_command(
                command_scope="goal09.experiment_result",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal09.experiment_result.recorded",
                actor=command.actor,
                object_kind="goal09_experiment_result",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "signal": signal.signal,
                    "eligible": signal.eligible,
                    "reason": signal.reason,
                    "review_required": review_boundary.required,
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal09.experiment_result.recorded",
                payload={
                    "account_id": command.account_id,
                    "topic_id": command.topic_id,
                    "root_id": root_id,
                    "version_id": version_id,
                    "signal": signal.signal,
                    "eligible": signal.eligible,
                    "review_required": review_boundary.required,
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=audit_id,
            )
            return ExperimentResult(root_id, version_id, receipt_id, audit_id, outbox_id, signal, review_boundary)

    def _existing_result(self, command: ExperimentResultCommand) -> ExperimentResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal09.experiment_result", command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(command.request_payload()):
            raise IdempotencyConflict("idempotency key reused with different request")
        result = json.loads(row["result_json"])
        return ExperimentResult(
            root_id=str(result["root_id"]),
            version_id=str(result["version_id"]),
            receipt_id=str(row["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            metric_signal=MetricSignal(**result["metric_signal"]),
            review_boundary=ExperimentReviewBoundary(**result["experiment_review_boundary"]),
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
    def _validate_command(command: ExperimentResultCommand) -> None:
        if not command.account_id:
            raise ExperimentError("account_id is required")
        if not command.topic_id:
            raise ExperimentError("topic_id is required")
        if not command.actor:
            raise ExperimentError("actor is required")
        if not command.idempotency_key:
            raise ExperimentError("idempotency_key is required")
        if command.experiment_kind not in EXPERIMENT_KINDS:
            raise ExperimentError(f"unsupported experiment_kind: {command.experiment_kind}")
        if command.actual_use_status not in ACTUAL_USE_STATUSES:
            raise ExperimentError(f"unsupported actual_use_status: {command.actual_use_status}")
        if command.publication_relation not in PUBLICATION_RELATIONS:
            raise ExperimentError(f"unsupported publication_relation: {command.publication_relation}")
        if not command.metric.metric_name:
            raise ExperimentError("metric_name is required")
        if not command.metric.checkpoint:
            raise ExperimentError("metric checkpoint is required")
        _validate_version_ref(command.primary_hypothesis_ref)
        _validate_version_ref(command.publication_capture_ref)


def compute_metric_signal(command: ExperimentResultCommand) -> MetricSignal:
    if command.experiment_kind != "formal":
        return MetricSignal("ineligible", False, "experiment is observational", None)
    if not command.primary_hypothesis_frozen:
        return MetricSignal("ineligible", False, "primary hypothesis was not frozen before publication", None)
    if command.actual_use_status == "unknown":
        return MetricSignal("inconclusive", False, "primary hypothesis actual use is unknown", None)
    if command.actual_use_status != "used":
        return MetricSignal("ineligible", False, "primary hypothesis was not actually used", None)
    if command.major_confounder:
        return MetricSignal("inconclusive", True, "major confounder recorded", None)

    baseline = _to_decimal(command.metric.baseline_value, "baseline_value")
    observed = _to_decimal(command.metric.observed_value, "observed_value")
    threshold = _to_decimal(command.metric.support_ratio, "support_ratio")
    if baseline is None or observed is None:
        return MetricSignal("inconclusive", True, "missing P+ metric input", None)
    if baseline <= 0:
        return MetricSignal("inconclusive", True, "baseline is zero or negative", None)
    if observed < 0:
        return MetricSignal("inconclusive", True, "observed value is negative", None)
    if threshold <= 0:
        raise ExperimentError("support_ratio must be positive")

    ratio = observed / baseline
    signal = "supported" if ratio >= threshold else "not_supported"
    return MetricSignal(signal, True, f"{command.metric.metric_name} ratio evaluated", _decimal_to_text(ratio))


def determine_experiment_review_boundary(
    command: ExperimentResultCommand,
    signal: MetricSignal | None = None,
) -> ExperimentReviewBoundary:
    signal = signal or compute_metric_signal(command)
    if _has_deterministic_metric_invalidity(signal):
        return ExperimentReviewBoundary(
            required=False,
            reasons=(signal.reason,),
            blocked_by_deterministic_invalidity=True,
        )

    reasons: list[str] = []
    if command.publication_relation in REVIEW_PUBLICATION_RELATIONS:
        reasons.append(f"publication_relation:{command.publication_relation}")
    if command.actual_use_status == "unknown":
        reasons.append("actual_use_status:unknown")
    if command.major_confounder:
        reasons.append("major_confounder")
    if command.attribution_conflict:
        reasons.append("attribution_conflict")
    return ExperimentReviewBoundary(required=bool(reasons), reasons=tuple(reasons))


def _has_deterministic_metric_invalidity(signal: MetricSignal) -> bool:
    return signal.signal == "inconclusive" and signal.reason in {
        "missing P+ metric input",
        "baseline is zero or negative",
        "observed value is negative",
    }


def recompute_experience_state(command: ExperienceStateInput) -> ExperienceStateResult:
    _validate_experience_state_input(command)
    formal_supports = _formal_evidence(command.evidence, "supported")
    formal_failures = _formal_evidence(command.evidence, "not_supported")
    external_counterexamples = _eligible_by_kind(command.evidence, "external_counterexample")
    structural_signals = _eligible_by_kind(command.evidence, "structural_revision_signal")
    human_requests = _eligible_by_kind(command.evidence, "human_revision_request")

    maturity_level = _maturity_level(formal_supports, command.evidence)
    computed_status, status_reason = _recommendation_status(
        current_status=command.current_recommendation_status,
        manual_lock=command.manual_lock,
        formal_failures=formal_failures,
        external_counterexamples=external_counterexamples,
    )
    triggers = _proposal_triggers(formal_failures, external_counterexamples, structural_signals, human_requests)
    return ExperienceStateResult(
        tactic_key=command.tactic_key,
        maturity_level=maturity_level,
        recommendation_status=computed_status,
        formal_supports=len(formal_supports),
        formal_failures=len(formal_failures),
        independent_external_counterexamples=len(_independent(external_counterexamples)),
        proposal_triggers=triggers,
        audit_reasons=(status_reason, f"maturity:{maturity_level}", f"triggers:{len(triggers)}"),
    )


def _validate_experience_state_input(command: ExperienceStateInput) -> None:
    if not command.tactic_key:
        raise ExperimentError("tactic_key is required")
    if command.current_recommendation_status not in RECOMMENDATION_STATUSES:
        raise ExperimentError(f"unsupported recommendation status: {command.current_recommendation_status}")
    for item in command.evidence:
        if not item.evidence_id:
            raise ExperimentError("evidence_id is required")
        if item.evidence_kind not in EVIDENCE_KINDS:
            raise ExperimentError(f"unsupported evidence_kind: {item.evidence_kind}")
        if not item.independence_key:
            raise ExperimentError("independence_key is required")
        if item.metric_signal and item.metric_signal not in METRIC_SIGNALS:
            raise ExperimentError(f"unsupported metric_signal: {item.metric_signal}")


def _formal_evidence(evidence: tuple[ExperienceEvidence, ...], metric_signal: str) -> tuple[ExperienceEvidence, ...]:
    return tuple(
        item
        for item in evidence
        if item.evidence_kind == "formal_p_result"
        and item.eligible
        and item.primary_used
        and item.metric_signal == metric_signal
    )


def _eligible_by_kind(evidence: tuple[ExperienceEvidence, ...], evidence_kind: str) -> tuple[ExperienceEvidence, ...]:
    return tuple(item for item in evidence if item.evidence_kind == evidence_kind and item.eligible)


def _independent(evidence: tuple[ExperienceEvidence, ...]) -> tuple[ExperienceEvidence, ...]:
    seen: set[str] = set()
    unique: list[ExperienceEvidence] = []
    for item in evidence:
        if item.independence_key in seen:
            continue
        seen.add(item.independence_key)
        unique.append(item)
    return tuple(unique)


def _maturity_level(formal_supports: tuple[ExperienceEvidence, ...], evidence: tuple[ExperienceEvidence, ...]) -> str:
    independent_supports = _independent(
        tuple(
            item
            for item in evidence
            if item.eligible
            and item.evidence_kind in {"formal_p_result", "external_support"}
            and (item.evidence_kind == "external_support" or item.metric_signal == "supported")
        )
    )
    formal_questions = {item.core_question_hash or item.independence_key for item in formal_supports}
    if len(formal_supports) >= 3 and len(formal_questions) >= 3:
        return "L5"
    if len(formal_supports) >= 2 and len(formal_questions) >= 2:
        return "L4"
    if formal_supports:
        return "L3"
    if len(independent_supports) >= 2:
        return "L2"
    if independent_supports:
        return "L1"
    return "L0"


def _recommendation_status(
    *,
    current_status: str,
    manual_lock: bool,
    formal_failures: tuple[ExperienceEvidence, ...],
    external_counterexamples: tuple[ExperienceEvidence, ...],
) -> tuple[str, str]:
    if current_status == "deprecated":
        return "deprecated", "deprecated requires formal restore proposal"
    if manual_lock:
        return current_status, "manual_lock blocks automatic recommendation transition"
    failure_questions = {item.core_question_hash or item.independence_key for item in formal_failures}
    if len(formal_failures) >= 3 and len(failure_questions) >= 3:
        return "paused", "three independent formal failures"
    if formal_failures:
        return "watch", "formal failure observed"
    if len(_independent(external_counterexamples)) >= 2:
        return "watch", "two independent external counterexamples"
    return "active", "no active risk signal"


def _proposal_triggers(
    formal_failures: tuple[ExperienceEvidence, ...],
    external_counterexamples: tuple[ExperienceEvidence, ...],
    structural_signals: tuple[ExperienceEvidence, ...],
    human_requests: tuple[ExperienceEvidence, ...],
) -> tuple[ProposalTrigger, ...]:
    triggers: list[ProposalTrigger] = []
    failure_questions = {item.core_question_hash or item.independence_key for item in formal_failures}
    if len(formal_failures) >= 3 and len(failure_questions) >= 3:
        triggers.append(
            ProposalTrigger(
                trigger_kind="repeated_formal_failures",
                allowed_proposal_types=("revise", "split", "deprecate", "no_proposal"),
                evidence_ids=tuple(item.evidence_id for item in formal_failures),
                reason="at least three formal failures across different core questions",
            )
        )
    independent_counterexamples = _independent(external_counterexamples)
    if len(independent_counterexamples) >= 2:
        triggers.append(
            ProposalTrigger(
                trigger_kind="independent_external_counterexamples",
                allowed_proposal_types=("revise", "split", "no_proposal"),
                evidence_ids=tuple(item.evidence_id for item in independent_counterexamples),
                reason="two independent eligible external counterexamples",
            )
        )
    if structural_signals:
        triggers.append(
            ProposalTrigger(
                trigger_kind="structural_revision_signal",
                allowed_proposal_types=("revise", "split", "merge", "no_proposal"),
                evidence_ids=tuple(item.evidence_id for item in structural_signals),
                reason="eligible structural revision signal",
            )
        )
    if human_requests:
        requested_restore = any(item.requested_action == "restore" for item in human_requests)
        allowed = ("revise", "split", "merge", "deprecate", "restore", "no_proposal") if requested_restore else (
            "revise",
            "split",
            "merge",
            "deprecate",
            "no_proposal",
        )
        triggers.append(
            ProposalTrigger(
                trigger_kind="human_requested_revision",
                allowed_proposal_types=allowed,
                evidence_ids=tuple(item.evidence_id for item in human_requests),
                reason="human explicitly requested experience revision",
            )
        )
    return tuple(triggers)


def _validate_version_ref(ref: VersionRef) -> None:
    if not ref.relation_role:
        raise ExperimentError("reference relation_role is required")
    if not ref.target_object_kind:
        raise ExperimentError("reference target_object_kind is required")
    if not ref.target_stable_id:
        raise ExperimentError("reference target_stable_id is required")
    if not ref.target_version_id and not ref.target_content_hash:
        raise ExperimentError("reference must point to a concrete version or content hash")
    if not ref.locator:
        raise ExperimentError("reference locator is required")


def _to_decimal(value: str | int | float | Decimal | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExperimentError(f"{field_name} must be numeric") from exc


def _decimal_to_text(value: str | int | float | Decimal | None) -> str | None:
    if value is None:
        return None
    return format(_to_decimal(value, "decimal"), "f")
