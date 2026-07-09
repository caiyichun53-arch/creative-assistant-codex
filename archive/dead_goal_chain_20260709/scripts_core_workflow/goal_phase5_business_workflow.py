from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import yaml

from scripts.core.model_gateway.formal_skill_adapter import (
    FORMAL_MAPPING_PATH,
    FORMAL_SKILL_JOB_KIND,
    FormalBusinessSkillAdapter,
    FormalBusinessSkillMaterializer,
    FormalSkillAdapterError,
    FormalSkillContract,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelGatewayError
from scripts.core.model_gateway.goal07_skill_runner import SkillContractError
from scripts.core.persistence.goal01_store import PersistenceStore, content_hash, uuid7
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator, WorkflowStepSpec


class BusinessWorkflowError(RuntimeError):
    pass


FORMAL_BUSINESS_WORKFLOW_SKILLS: tuple[str, ...] = (
    "content_classify",
    "content_relation_judge",
    "source_to_topic",
    "sample_deep_analyze",
    "tactic_extract",
    "research_evidence_extract",
    "production_research_plan",
    "content_plan",
    "script_generate",
    "script_review",
    "experiment_review",
    "experience_revision_propose",
)

EXPERIENCE_REQUIRED_SKILLS = frozenset(
    {
        "content_plan",
        "script_generate",
        "script_review",
        "experiment_review",
        "experience_revision_propose",
    }
)

ALLOWED_EXPERIENCE_STATUSES = frozenset({"published"})
REJECTED_EXPERIENCE_STATUSES = frozenset({"candidate", "draft", "revoked", "deprecated"})
ALLOWED_USAGE_STATUSES = frozenset(
    {
        "applied",
        "considered_not_applied",
        "rejected_due_to_conflict",
        "not_applicable",
    }
)
FORBIDDEN_INPUT_KEY_PARTS = (
    "database",
    "db_connection",
    "sqlite",
    "creation_db",
    "vault",
    "obsidian",
    "hermes_memory",
    "cold_backup",
    "legacy",
    "local_path",
    "file_path",
)


@dataclass(frozen=True)
class ExperienceVersion:
    experience_ref: str
    experience_version: str
    experience_type: str
    status: str
    domain_scope: tuple[str, ...]
    applicable_conditions: tuple[str, ...]
    content: dict[str, Any]
    evidence_refs: tuple[str, ...]
    confidence: str
    source_type: str
    content_hash: str
    selection_reason: str = ""
    conflict_status: str = "none"
    supersedes: tuple[str, ...] = ()
    priority: int = 0

    def as_context_item(self, reason: str) -> dict[str, Any]:
        return {
            "experience_ref": self.experience_ref,
            "experience_version": self.experience_version,
            "experience_type": self.experience_type,
            "status": self.status,
            "domain_scope": list(self.domain_scope),
            "applicable_conditions": list(self.applicable_conditions),
            "content": self.content,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "source_type": self.source_type,
            "content_hash": self.content_hash,
            "selection_reason": reason,
        }


@dataclass(frozen=True)
class ExperienceContext:
    schema_version: str
    skill_id: str
    domain: str
    content_form: str
    token_budget: int
    items: tuple[dict[str, Any], ...]
    context_hash: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "domain": self.domain,
            "content_form": self.content_form,
            "token_budget": self.token_budget,
            "items": list(self.items),
            "context_hash": self.context_hash,
        }


@dataclass(frozen=True)
class FrozenSkillInput:
    schema_version: str
    workflow_id: str
    step_key: str
    formal_skill_id: str
    formal_skill_version: str
    input_payload: dict[str, Any]
    upstream_refs: tuple[dict[str, Any], ...]
    experience_context: ExperienceContext
    input_hash: str
    assembly_hash: str

    def scheduler_payload(self) -> dict[str, Any]:
        return {
            "formal_skill_id": self.formal_skill_id,
            "input": self.input_payload,
            "idempotency_key": f"{self.workflow_id}:{self.step_key}:{self.input_hash}",
            "input_assembly": {
                "schema_version": self.schema_version,
                "workflow_id": self.workflow_id,
                "step_key": self.step_key,
                "formal_skill_id": self.formal_skill_id,
                "formal_skill_version": self.formal_skill_version,
                "input_hash": self.input_hash,
                "assembly_hash": self.assembly_hash,
                "upstream_refs": list(self.upstream_refs),
                "experience_context": self.experience_context.as_payload(),
            },
        }

    @classmethod
    def from_scheduler_payload(cls, payload: dict[str, Any]) -> "FrozenSkillInput":
        assembly = payload.get("input_assembly")
        if not isinstance(assembly, dict):
            raise BusinessWorkflowError("scheduler payload missing input_assembly")
        context_payload = assembly.get("experience_context")
        if not isinstance(context_payload, dict):
            raise BusinessWorkflowError("input_assembly missing experience_context")
        context = ExperienceContext(
            schema_version=str(context_payload["schema_version"]),
            skill_id=str(context_payload["skill_id"]),
            domain=str(context_payload["domain"]),
            content_form=str(context_payload["content_form"]),
            token_budget=int(context_payload["token_budget"]),
            items=tuple(dict(item) for item in context_payload.get("items", [])),
            context_hash=str(context_payload["context_hash"]),
        )
        return cls(
            schema_version=str(assembly["schema_version"]),
            workflow_id=str(assembly["workflow_id"]),
            step_key=str(assembly["step_key"]),
            formal_skill_id=str(assembly["formal_skill_id"]),
            formal_skill_version=str(assembly["formal_skill_version"]),
            input_payload=dict(payload.get("input") or {}),
            upstream_refs=tuple(dict(item) for item in assembly.get("upstream_refs", [])),
            experience_context=context,
            input_hash=str(assembly["input_hash"]),
            assembly_hash=str(assembly["assembly_hash"]),
        )


@dataclass(frozen=True)
class WorkflowArtifactResult:
    root_id: str
    version_id: str
    receipt_id: str
    outbox_id: str
    replayed: bool = False


@dataclass(frozen=True)
class SkillExecutionOutput:
    result_version_id: str
    output_payload: dict[str, Any]


@dataclass(frozen=True)
class BusinessWorkflowStepResult:
    status: str
    job_id: str | None = None
    attempt_id: str | None = None
    step_key: str | None = None
    formal_skill_id: str | None = None
    result_version_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class FormalWorkflowStepDefinition:
    step_key: str
    formal_skill_id: str
    depends_on: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    optional_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class FormalWorkflowDefinition:
    workflow_id: str
    workflow_version: str
    entry_contract: str
    step_graph: tuple[FormalWorkflowStepDefinition, ...]
    required_artifacts: tuple[str, ...]
    optional_artifacts: tuple[str, ...]
    success_definition: str
    failure_definition: str
    cancellation_policy: str
    retry_policy: str
    materialization_contract: str
    outbox_contract: str


@dataclass(frozen=True)
class BusinessWorkflowRunResult:
    workflow_id: str
    workflow_version: str
    workflow_instance_id: str
    completed_steps: tuple[BusinessWorkflowStepResult, ...]
    upstream_refs: tuple[dict[str, Any], ...]


FORMAL_WORKFLOW_DEFINITIONS: dict[str, FormalWorkflowDefinition] = {
    "business.content_learning_analysis": FormalWorkflowDefinition(
        workflow_id="business.content_learning_analysis",
        workflow_version="1.0.0",
        entry_contract="content_learning_analysis.entry.v1",
        step_graph=(
            FormalWorkflowStepDefinition("content_classify", "content_classify"),
            FormalWorkflowStepDefinition("sample_deep_analyze", "sample_deep_analyze", depends_on=("content_classify",)),
            FormalWorkflowStepDefinition("tactic_extract", "tactic_extract", depends_on=("sample_deep_analyze",)),
        ),
        required_artifacts=("public_content_sample", "sample_metrics"),
        optional_artifacts=("prior_relation_summary",),
        success_definition="classified sample, single-sample analysis and tactic candidates are materialized",
        failure_definition="any required step failure stops downstream scheduling",
        cancellation_policy="queued not-yet-started downstream steps may be cancelled by Core only",
        retry_policy="retry same frozen input only; no provider, route, Skill, prompt or experience changes",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.source_to_topic": FormalWorkflowDefinition(
        workflow_id="business.source_to_topic",
        workflow_version="1.0.0",
        entry_contract="source_to_topic.entry.v1",
        step_graph=(
            FormalWorkflowStepDefinition("content_relation_judge", "content_relation_judge"),
            FormalWorkflowStepDefinition("source_to_topic", "source_to_topic", depends_on=("content_relation_judge",)),
        ),
        required_artifacts=("source_content", "relation_inputs"),
        optional_artifacts=("prior_topic_context",),
        success_definition="source relation judgement and candidate topic are materialized",
        failure_definition="topic generation is not scheduled unless relation judgement succeeds",
        cancellation_policy="Core may cancel queued downstream topic generation before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.research": FormalWorkflowDefinition(
        workflow_id="business.research",
        workflow_version="1.0.0",
        entry_contract="research.entry.v1",
        step_graph=(
            FormalWorkflowStepDefinition("research_evidence_extract", "research_evidence_extract"),
            FormalWorkflowStepDefinition(
                "production_research_plan",
                "production_research_plan",
                depends_on=("research_evidence_extract",),
            ),
        ),
        required_artifacts=("research_packet", "research_question"),
        optional_artifacts=("topic_constraints",),
        success_definition="research evidence and production research plan are materialized",
        failure_definition="research planning is not scheduled unless evidence extraction succeeds",
        cancellation_policy="Core may cancel queued downstream planning before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.creation": FormalWorkflowDefinition(
        workflow_id="business.creation",
        workflow_version="1.0.0",
        entry_contract="creation.entry.v1",
        step_graph=(
            FormalWorkflowStepDefinition("content_plan", "content_plan"),
            FormalWorkflowStepDefinition("script_generate", "script_generate", depends_on=("content_plan",)),
        ),
        required_artifacts=("brief", "research_summary", "style_examples"),
        optional_artifacts=("human_reference_refs",),
        success_definition="content plan and generated draft are materialized",
        failure_definition="draft generation is not scheduled unless content planning succeeds",
        cancellation_policy="Core may cancel queued downstream draft generation before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.review": FormalWorkflowDefinition(
        workflow_id="business.review",
        workflow_version="1.0.0",
        entry_contract="review.entry.v1",
        step_graph=(FormalWorkflowStepDefinition("script_review", "script_review"),),
        required_artifacts=("draft_text", "brief", "human_reference_refs"),
        optional_artifacts=("review_preferences",),
        success_definition="script review result is materialized",
        failure_definition="review failure is terminal for this review workflow run",
        cancellation_policy="Core may cancel queued review before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.experiment_review": FormalWorkflowDefinition(
        workflow_id="business.experiment_review",
        workflow_version="1.0.0",
        entry_contract="experiment_review.entry.v1",
        step_graph=(FormalWorkflowStepDefinition("experiment_review", "experiment_review"),),
        required_artifacts=("experiment_design", "execution_summary", "result_metrics"),
        optional_artifacts=("tested_experience_refs",),
        success_definition="experiment review result is materialized",
        failure_definition="experiment review failure is terminal for this workflow run",
        cancellation_policy="Core may cancel queued experiment review before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
    "business.experience_revision_candidate": FormalWorkflowDefinition(
        workflow_id="business.experience_revision_candidate",
        workflow_version="1.0.0",
        entry_contract="experience_revision_candidate.entry.v1",
        step_graph=(
            FormalWorkflowStepDefinition("experiment_review", "experiment_review"),
            FormalWorkflowStepDefinition(
                "experience_revision_propose",
                "experience_revision_propose",
                depends_on=("experiment_review",),
            ),
        ),
        required_artifacts=("frozen_experience_versions", "new_evidence_refs", "experiment_packet"),
        optional_artifacts=("tactic_candidates",),
        success_definition="experiment review and candidate-only experience revision proposal are materialized",
        failure_definition="revision proposal is not scheduled unless experiment review succeeds",
        cancellation_policy="Core may cancel queued downstream revision proposal before lease",
        retry_policy="retry same frozen input only",
        materialization_contract="formal_business_skill_result.v1 plus business_workflow_input_assembly",
        outbox_contract="formal_business_skill.result.materialized and phase5 input/usage events",
    ),
}


class SkillExecutionPort(Protocol):
    def __call__(self, frozen: FrozenSkillInput, *, job_id: str, attempt_id: str) -> SkillExecutionOutput:
        ...


class ExperienceSelector:
    def select(
        self,
        *,
        skill_id: str,
        domain: str,
        content_form: str,
        conditions: Iterable[str],
        token_budget: int,
        candidates: Iterable[ExperienceVersion],
    ) -> ExperienceContext:
        if skill_id not in FORMAL_BUSINESS_WORKFLOW_SKILLS:
            raise BusinessWorkflowError(f"unknown formal skill: {skill_id}")
        if token_budget < 0:
            raise BusinessWorkflowError("token_budget must not be negative")
        selected: list[dict[str, Any]] = []
        condition_set = {item for item in conditions if item}
        for item in sorted(candidates, key=lambda exp: (-exp.priority, exp.experience_ref, exp.experience_version)):
            reason = self._selection_reason(item, domain=domain, content_form=content_form, condition_set=condition_set)
            if reason:
                selected.append(item.as_context_item(reason))
        payload = {
            "schema_version": "experience_context.v1",
            "skill_id": skill_id,
            "domain": domain,
            "content_form": content_form,
            "token_budget": token_budget,
            "items": selected,
        }
        return ExperienceContext(
            schema_version="experience_context.v1",
            skill_id=skill_id,
            domain=domain,
            content_form=content_form,
            token_budget=token_budget,
            items=tuple(selected),
            context_hash=content_hash(payload, "experience_context.v1"),
        )

    @staticmethod
    def _selection_reason(
        item: ExperienceVersion,
        *,
        domain: str,
        content_form: str,
        condition_set: set[str],
    ) -> str | None:
        if item.status not in ALLOWED_EXPERIENCE_STATUSES:
            if item.status in REJECTED_EXPERIENCE_STATUSES:
                return None
            return None
        if item.conflict_status not in {"none", "resolved"}:
            return None
        if domain not in item.domain_scope and "all" not in item.domain_scope:
            return None
        required = set(item.applicable_conditions)
        if content_form and content_form not in required and "any_form" not in required:
            return None
        if not required.issubset(condition_set | {content_form, "any_form"}):
            return None
        return item.selection_reason or "published experience matched skill, domain and conditions"


class InputAssembly:
    schema_version = "business_workflow.input_assembly.v1"

    def __init__(self, *, experience_selector: ExperienceSelector | None = None):
        self.experience_selector = experience_selector or ExperienceSelector()

    def freeze_skill_input(
        self,
        *,
        workflow_id: str,
        step_key: str,
        formal_skill_id: str,
        formal_skill_version: str | None = None,
        input_payload: dict[str, Any],
        upstream_refs: Iterable[dict[str, Any]],
        domain: str,
        content_form: str,
        conditions: Iterable[str],
        experience_candidates: Iterable[ExperienceVersion],
        token_budget: int = 1200,
    ) -> FrozenSkillInput:
        if formal_skill_id not in FORMAL_BUSINESS_WORKFLOW_SKILLS:
            raise BusinessWorkflowError(f"unknown formal skill: {formal_skill_id}")
        resolved_version = formal_skill_version or FormalSkillRegistry().resolve(formal_skill_id).version
        self._validate_public_input(input_payload)
        upstream = tuple(dict(item) for item in upstream_refs)
        self._validate_upstream_refs(upstream)
        context = self.experience_selector.select(
            skill_id=formal_skill_id,
            domain=domain,
            content_form=content_form,
            conditions=conditions,
            token_budget=token_budget,
            candidates=experience_candidates,
        )
        input_hash = content_hash(input_payload, f"{formal_skill_id}.input.v1")
        assembly_payload = {
            "workflow_id": workflow_id,
            "step_key": step_key,
            "formal_skill_id": formal_skill_id,
            "formal_skill_version": resolved_version,
            "input_hash": input_hash,
            "upstream_refs": upstream,
            "experience_context": context.as_payload(),
        }
        return FrozenSkillInput(
            schema_version=self.schema_version,
            workflow_id=workflow_id,
            step_key=step_key,
            formal_skill_id=formal_skill_id,
            formal_skill_version=resolved_version,
            input_payload=dict(input_payload),
            upstream_refs=upstream,
            experience_context=context,
            input_hash=input_hash,
            assembly_hash=content_hash(assembly_payload, self.schema_version),
        )

    @staticmethod
    def assert_retry_uses_same_input(first: FrozenSkillInput, retry: FrozenSkillInput) -> None:
        if first.input_hash != retry.input_hash or first.assembly_hash != retry.assembly_hash:
            raise BusinessWorkflowError("retry attempted to change frozen input assembly")

    @staticmethod
    def _validate_public_input(payload: Any, path: str = "input") -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized = key.lower()
                if any(part in normalized for part in FORBIDDEN_INPUT_KEY_PARTS):
                    raise BusinessWorkflowError(f"forbidden private input key at {path}.{key}")
                InputAssembly._validate_public_input(value, f"{path}.{key}")
        elif isinstance(payload, list):
            for index, value in enumerate(payload):
                InputAssembly._validate_public_input(value, f"{path}[{index}]")

    @staticmethod
    def _validate_upstream_refs(upstream_refs: tuple[dict[str, Any], ...]) -> None:
        for ref in upstream_refs:
            for key in ("formal_skill_id", "result_version_id", "output_hash"):
                if not str(ref.get(key) or "").strip():
                    raise BusinessWorkflowError(f"upstream ref missing {key}")


class ExperienceUsageValidator:
    def validate(self, *, experience_context: ExperienceContext, output_payload: dict[str, Any], require_usage: bool) -> None:
        usage = output_payload.get("experience_usage")
        if usage is None:
            if require_usage and experience_context.items:
                raise BusinessWorkflowError("experience_usage is required when experience context is injected")
            return
        if not isinstance(usage, list):
            raise BusinessWorkflowError("experience_usage must be a list")
        frozen_refs = {str(item["experience_ref"]) for item in experience_context.items}
        for item in usage:
            if not isinstance(item, dict):
                raise BusinessWorkflowError("experience_usage item must be an object")
            ref = str(item.get("experience_ref") or "")
            status = str(item.get("usage_status") or "")
            if ref not in frozen_refs:
                raise BusinessWorkflowError("experience_usage references experience not frozen in input")
            if status not in ALLOWED_USAGE_STATUSES:
                raise BusinessWorkflowError("experience_usage has unsupported usage_status")


class BusinessWorkflowMaterializer:
    def __init__(self, store: PersistenceStore, *, id_factory: Callable[[], str] = uuid7):
        self.store = store
        self.conn = store.conn
        self.id_factory = id_factory
        self.usage_validator = ExperienceUsageValidator()

    def record_input_assembly(
        self,
        frozen: FrozenSkillInput,
        *,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowArtifactResult:
        existing = self._existing_result("phase5.input_assembly.record", idempotency_key)
        if existing is not None:
            return existing
        payload = {
            "schema_version": frozen.schema_version,
            "workflow_id": frozen.workflow_id,
            "step_key": frozen.step_key,
            "formal_skill_id": frozen.formal_skill_id,
            "formal_skill_version": frozen.formal_skill_version,
            "input_hash": frozen.input_hash,
            "assembly_hash": frozen.assembly_hash,
            "upstream_refs": list(frozen.upstream_refs),
            "experience_context": frozen.experience_context.as_payload(),
        }
        with self.conn:
            root_id = self.store.create_root("business_workflow_input_assembly")
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version=frozen.schema_version,
                business_payload={
                    "workflow_id": frozen.workflow_id,
                    "step_key": frozen.step_key,
                    "formal_skill_id": frozen.formal_skill_id,
                    "formal_skill_version": frozen.formal_skill_version,
                    "assembly_hash": frozen.assembly_hash,
                },
            )
            self.store.set_current_version(root_id, version_id)
            for ref in frozen.upstream_refs:
                target_version = self._existing_trace_version(str(ref["result_version_id"]))
                self.store.record_object_reference(
                    source_version_id=version_id,
                    relation_role="assembled_from_upstream_skill_result",
                    target_object_kind="formal_business_skill_result",
                    target_stable_id=str(ref["formal_skill_id"]),
                    target_version_id=target_version,
                    target_content_hash=str(ref["output_hash"]),
                    locator={
                        "formal_skill_id": ref["formal_skill_id"],
                        "step_key": frozen.step_key,
                        "result_version_id": ref["result_version_id"],
                    },
                )
            for item in frozen.experience_context.items:
                target_version = self._existing_trace_version(str(item["experience_version"]))
                self.store.record_object_reference(
                    source_version_id=version_id,
                    relation_role="freezes_experience_context",
                    target_object_kind="formal_experience",
                    target_stable_id=str(item["experience_ref"]),
                    target_version_id=target_version,
                    target_content_hash=str(item["content_hash"]),
                    locator={"selection_reason": item["selection_reason"], "experience_version": item["experience_version"]},
                )
            audit_id = self.store.record_audit(
                event_type="phase5.input_assembly.recorded",
                actor=actor,
                object_kind="business_workflow_input_assembly",
                object_id=root_id,
                version_id=version_id,
                payload={"workflow_id": frozen.workflow_id, "step_key": frozen.step_key},
                correlation_id=frozen.workflow_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="phase5.input_assembly.recorded",
                payload={
                    "workflow_id": frozen.workflow_id,
                    "step_key": frozen.step_key,
                    "formal_skill_id": frozen.formal_skill_id,
                    "version_id": version_id,
                    "assembly_hash": frozen.assembly_hash,
                },
                correlation_id=frozen.workflow_id,
                causation_id=audit_id,
            )
            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "outbox_id": outbox_id,
            }
            receipt_id = self.store.record_command(
                command_scope="phase5.input_assembly.record",
                idempotency_key=idempotency_key,
                request_payload=payload,
                result_payload=result_payload,
                correlation_id=frozen.workflow_id,
                causation_id=audit_id,
            )
        return WorkflowArtifactResult(root_id=root_id, version_id=version_id, receipt_id=receipt_id, outbox_id=outbox_id)

    def record_experience_usage(
        self,
        *,
        frozen: FrozenSkillInput,
        output_payload: dict[str, Any],
        result_version_id: str,
        actor: str,
        idempotency_key: str,
        require_usage: bool,
    ) -> WorkflowArtifactResult:
        existing = self._existing_result("phase5.experience_usage.record", idempotency_key)
        if existing is not None:
            return existing
        self.usage_validator.validate(
            experience_context=frozen.experience_context,
            output_payload=output_payload,
            require_usage=require_usage,
        )
        usage = list(output_payload.get("experience_usage") or [])
        payload = {
            "schema_version": "business_workflow.experience_usage.v1",
            "workflow_id": frozen.workflow_id,
            "step_key": frozen.step_key,
            "formal_skill_id": frozen.formal_skill_id,
            "formal_skill_version": frozen.formal_skill_version,
            "result_version_id": result_version_id,
            "experience_context_hash": frozen.experience_context.context_hash,
            "experience_usage": usage,
        }
        with self.conn:
            root_id = self.store.create_root("business_workflow_experience_usage")
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version="business_workflow.experience_usage.v1",
                business_payload={
                    "workflow_id": frozen.workflow_id,
                    "step_key": frozen.step_key,
                    "formal_skill_id": frozen.formal_skill_id,
                    "result_version_id": result_version_id,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self.store.record_object_reference(
                source_version_id=version_id,
                relation_role="records_usage_for_skill_result",
                target_object_kind="formal_business_skill_result",
                target_stable_id=frozen.formal_skill_id,
                target_version_id=self._existing_trace_version(result_version_id),
                target_content_hash=None,
                locator={"workflow_id": frozen.workflow_id, "step_key": frozen.step_key},
            )
            for item in usage:
                target_version = self._existing_trace_version(str(item.get("experience_version") or ""))
                self.store.record_object_reference(
                    source_version_id=version_id,
                    relation_role="uses_frozen_experience",
                    target_object_kind="formal_experience",
                    target_stable_id=str(item["experience_ref"]),
                    target_version_id=target_version,
                    target_content_hash=None,
                    locator={"usage_status": item["usage_status"], "experience_version": item.get("experience_version")},
                )
            audit_id = self.store.record_audit(
                event_type="phase5.experience_usage.recorded",
                actor=actor,
                object_kind="business_workflow_experience_usage",
                object_id=root_id,
                version_id=version_id,
                payload={"workflow_id": frozen.workflow_id, "step_key": frozen.step_key, "usage_count": len(usage)},
                correlation_id=frozen.workflow_id,
                causation_id=result_version_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="phase5.experience_usage.recorded",
                payload={
                    "workflow_id": frozen.workflow_id,
                    "step_key": frozen.step_key,
                    "formal_skill_id": frozen.formal_skill_id,
                    "version_id": version_id,
                    "usage_count": len(usage),
                },
                correlation_id=frozen.workflow_id,
                causation_id=audit_id,
            )
            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "outbox_id": outbox_id,
            }
            receipt_id = self.store.record_command(
                command_scope="phase5.experience_usage.record",
                idempotency_key=idempotency_key,
                request_payload=payload,
                result_payload=result_payload,
                correlation_id=frozen.workflow_id,
                causation_id=audit_id,
            )
        return WorkflowArtifactResult(root_id=root_id, version_id=version_id, receipt_id=receipt_id, outbox_id=outbox_id)

    def _existing_result(self, command_scope: str, idempotency_key: str) -> WorkflowArtifactResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (command_scope, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        return WorkflowArtifactResult(
            root_id=str(payload["root_id"]),
            version_id=str(payload["version_id"]),
            receipt_id=str(row["receipt_id"]),
            outbox_id=str(payload["outbox_id"]),
            replayed=True,
        )

    def _existing_trace_version(self, version_id: str) -> str | None:
        if not version_id:
            return None
        row = self.conn.execute("SELECT version_id FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
        return str(row["version_id"]) if row is not None else None


@dataclass(frozen=True)
class FormalSkillRegistryEntry:
    formal_skill_id: str
    version: str
    contract_path: Path
    allowed_model_nodes: tuple[str, ...]


class FormalSkillRegistry:
    def __init__(self, *, mapping_path: Path = FORMAL_MAPPING_PATH):
        self.mapping_path = Path(mapping_path)
        self.root = self.mapping_path.resolve().parent
        self.entries = self._load_entries()

    def resolve(self, formal_skill_id: str) -> FormalSkillRegistryEntry:
        entry = self.entries.get(formal_skill_id)
        if entry is None:
            raise BusinessWorkflowError(f"unknown formal skill: {formal_skill_id}")
        return entry

    def _load_entries(self) -> dict[str, FormalSkillRegistryEntry]:
        data = yaml.safe_load(self.mapping_path.read_text(encoding="utf-8")) or {}
        if data.get("schema_version") != "formal_skill_route_mapping.v1":
            raise BusinessWorkflowError("unsupported formal Skill registry schema")
        entries: dict[str, FormalSkillRegistryEntry] = {}
        for raw in data.get("formal_skills") or []:
            formal_skill_id = str(raw.get("formal_skill_id") or "")
            status = str(raw.get("status") or "")
            if status != "active_formal_business_skill":
                continue
            if formal_skill_id in entries:
                raise BusinessWorkflowError(f"duplicate formal skill in registry: {formal_skill_id}")
            source_document = str(raw.get("source_document") or "")
            contract_path = (self.root / source_document).resolve()
            entries[formal_skill_id] = FormalSkillRegistryEntry(
                formal_skill_id=formal_skill_id,
                version=str(raw.get("version") or ""),
                contract_path=contract_path,
                allowed_model_nodes=tuple(str(node) for node in raw.get("allowed_model_nodes") or ()),
            )
        missing = sorted(set(FORMAL_BUSINESS_WORKFLOW_SKILLS) - set(entries))
        if missing:
            raise BusinessWorkflowError(f"formal Skill registry missing active entries: {missing}")
        return entries


class FormalSkillDispatcher:
    def __init__(
        self,
        *,
        store: PersistenceStore,
        gateway: ModelGateway,
        registry: FormalSkillRegistry | None = None,
        id_factory: Callable[[], str] = uuid7,
    ):
        self.store = store
        self.gateway = gateway
        self.registry = registry or FormalSkillRegistry()
        self.materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
        self._contracts: dict[str, FormalSkillContract] = {}

    def __call__(self, frozen: FrozenSkillInput, *, job_id: str, attempt_id: str) -> SkillExecutionOutput:
        contract = self._resolve_contract(frozen)
        adapter = FormalBusinessSkillAdapter(contract=contract, gateway=self.gateway)
        try:
            run_result = adapter.run(frozen.input_payload)
            _root_id, version_id, _skill_run_id, _outbox_id = self.materializer.materialize_success(
                job_id=job_id,
                attempt_id=attempt_id,
                frozen_payload={
                    "formal_skill_id": frozen.formal_skill_id,
                    "input": frozen.input_payload,
                    "idempotency_key": f"{frozen.workflow_id}:{frozen.step_key}:{frozen.input_hash}",
                },
                run_result=run_result,
                contract=contract,
                model_port=self._model_port_name(contract),
            )
            return SkillExecutionOutput(result_version_id=version_id, output_payload=run_result.output_payload)
        except (FormalSkillAdapterError, ModelGatewayError, SkillContractError) as exc:
            self.materializer.record_failed_run(
                job_id=job_id,
                attempt_id=attempt_id,
                frozen_payload={"formal_skill_id": frozen.formal_skill_id, "input": frozen.input_payload},
                contract=contract,
                model_port=self._model_port_name(contract),
                error={"code": type(exc).__name__, "message": str(exc)},
            )
            raise

    def _resolve_contract(self, frozen: FrozenSkillInput) -> FormalSkillContract:
        entry = self.registry.resolve(frozen.formal_skill_id)
        if frozen.formal_skill_version != entry.version:
            raise BusinessWorkflowError("formal Skill version mismatch")
        contract = self._contracts.get(frozen.formal_skill_id)
        if contract is None:
            contract = FormalSkillContract.from_yaml(entry.contract_path)
            self._contracts[frozen.formal_skill_id] = contract
        if contract.formal_skill_id != entry.formal_skill_id or contract.version != entry.version:
            raise BusinessWorkflowError("formal Skill contract does not match registry")
        for route_name in entry.allowed_model_nodes:
            if route_name not in self.gateway.routes:
                raise BusinessWorkflowError(f"missing approved model route: {route_name}")
            provider_name = self.gateway.routes[route_name].provider_name
            if provider_name not in self.gateway.providers:
                raise BusinessWorkflowError(f"missing provider for approved model route: {route_name}")
        return contract

    def _model_port_name(self, contract: FormalSkillContract) -> str:
        route = self.gateway.routes.get(contract.route_name)
        if route is None:
            entry = self.registry.resolve(contract.formal_skill_id)
            for route_name in entry.allowed_model_nodes:
                route = self.gateway.routes.get(route_name)
                if route is not None:
                    break
        if route is None:
            return "unconfigured"
        return route.provider_name


class BusinessWorkflowWorker:
    def __init__(
        self,
        *,
        scheduler: Goal03Scheduler,
        materializer: BusinessWorkflowMaterializer,
        skill_executor: SkillExecutionPort,
        worker_id: str,
    ):
        self.scheduler = scheduler
        self.materializer = materializer
        self.skill_executor = skill_executor
        self.worker_id = worker_id

    def run_once(self) -> BusinessWorkflowStepResult:
        try:
            claim = self.scheduler.claim_next(worker_id=self.worker_id, lease_seconds=60)
        except NoClaimableJob:
            return BusinessWorkflowStepResult(status="idle")
        job = self.scheduler.get_job(claim.job_id)
        if job["job_kind"] != FORMAL_SKILL_JOB_KIND:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "unsupported_job_kind", "job_kind": job["job_kind"]},
                retry=False,
            )
            return BusinessWorkflowStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason="unsupported_job_kind",
            )
        try:
            frozen = FrozenSkillInput.from_scheduler_payload(claim.payload)
            self.materializer.record_input_assembly(
                frozen,
                actor=self.worker_id,
                idempotency_key=f"assembly:{frozen.workflow_id}:{frozen.step_key}:{frozen.assembly_hash}",
            )
            execution = self.skill_executor(frozen, job_id=claim.job_id, attempt_id=claim.attempt_id)
            usage = self.materializer.record_experience_usage(
                frozen=frozen,
                output_payload=execution.output_payload,
                result_version_id=execution.result_version_id,
                actor=self.worker_id,
                idempotency_key=f"usage:{frozen.workflow_id}:{frozen.step_key}:{execution.result_version_id}",
                require_usage=frozen.formal_skill_id in EXPERIENCE_REQUIRED_SKILLS,
            )
            self.scheduler.complete(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                result={
                    "workflow_id": frozen.workflow_id,
                    "step_key": frozen.step_key,
                    "formal_skill_id": frozen.formal_skill_id,
                    "result_version_id": execution.result_version_id,
                    "experience_usage_version_id": usage.version_id,
                },
            )
            return BusinessWorkflowStepResult(
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                step_key=frozen.step_key,
                formal_skill_id=frozen.formal_skill_id,
                result_version_id=execution.result_version_id,
            )
        except Exception as exc:  # noqa: BLE001 - workflow worker must record fail-closed errors.
            next_status = self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": type(exc).__name__, "message": str(exc)},
                retry=True,
            )
            return BusinessWorkflowStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason=next_status,
            )


class BusinessWorkflowChainRunner:
    def __init__(
        self,
        *,
        scheduler: Goal03Scheduler,
        worker: BusinessWorkflowWorker,
        input_assembly: InputAssembly | None = None,
    ):
        self.scheduler = scheduler
        self.worker = worker
        self.input_assembly = input_assembly or InputAssembly()
        self.orchestrator = Goal05WorkflowOrchestrator(scheduler)

    def run(
        self,
        *,
        definition: FormalWorkflowDefinition,
        workflow_instance_id: str,
        input_payloads: dict[str, dict[str, Any]],
        domain: str,
        content_form: str,
        conditions: Iterable[str],
        experience_candidates: Iterable[ExperienceVersion],
        idempotency_key: str,
        token_budget: int = 1200,
        max_attempts: int = 1,
    ) -> BusinessWorkflowRunResult:
        self._validate_definition(definition)
        completed_refs: dict[str, dict[str, Any]] = {}
        completed_steps: list[BusinessWorkflowStepResult] = []
        all_upstream_refs: list[dict[str, Any]] = []
        for step in definition.step_graph:
            missing = [dependency for dependency in step.depends_on if dependency not in completed_refs]
            if missing:
                raise BusinessWorkflowError(f"workflow dependencies are not satisfied: {missing}")
            if step.step_key not in input_payloads:
                raise BusinessWorkflowError(f"missing input payload for workflow step: {step.step_key}")
            upstream_refs = tuple(completed_refs[dependency] for dependency in step.depends_on)
            all_upstream_refs.extend(upstream_refs)
            frozen = self.input_assembly.freeze_skill_input(
                workflow_id=workflow_instance_id,
                step_key=step.step_key,
                formal_skill_id=step.formal_skill_id,
                input_payload=input_payloads[step.step_key],
                upstream_refs=upstream_refs,
                domain=domain,
                content_form=content_form,
                conditions=conditions,
                experience_candidates=experience_candidates,
                token_budget=token_budget,
            )
            workflow_steps = build_formal_business_workflow_steps(
                workflow_id=workflow_instance_id,
                frozen_inputs=[frozen],
                max_attempts=max_attempts,
            )
            self.orchestrator.start_workflow(
                workflow_name=definition.workflow_id,
                steps=workflow_steps,
                idempotency_key=f"{idempotency_key}:{step.step_key}",
            )
            result = self.worker.run_once()
            completed_steps.append(result)
            if result.status != "succeeded" or not result.result_version_id:
                raise BusinessWorkflowError(f"workflow step failed closed: {step.step_key}:{result.reason or result.status}")
            completed_refs[step.step_key] = self._result_ref(result.result_version_id)
        return BusinessWorkflowRunResult(
            workflow_id=definition.workflow_id,
            workflow_version=definition.workflow_version,
            workflow_instance_id=workflow_instance_id,
            completed_steps=tuple(completed_steps),
            upstream_refs=tuple(all_upstream_refs),
        )

    def _result_ref(self, result_version_id: str) -> dict[str, Any]:
        row = self.scheduler.conn.execute(
            """
            SELECT formal_skill_id, result_version_id, output_hash
              FROM formal_business_skill_result_index
             WHERE result_version_id=?
            """,
            (result_version_id,),
        ).fetchone()
        if row is None:
            raise BusinessWorkflowError(f"missing formal Skill result for downstream handoff: {result_version_id}")
        return {
            "formal_skill_id": str(row["formal_skill_id"]),
            "result_version_id": str(row["result_version_id"]),
            "output_hash": str(row["output_hash"]),
        }

    @staticmethod
    def _validate_definition(definition: FormalWorkflowDefinition) -> None:
        required = {
            "workflow_id": definition.workflow_id,
            "workflow_version": definition.workflow_version,
            "entry_contract": definition.entry_contract,
            "success_definition": definition.success_definition,
            "failure_definition": definition.failure_definition,
            "cancellation_policy": definition.cancellation_policy,
            "retry_policy": definition.retry_policy,
            "materialization_contract": definition.materialization_contract,
            "outbox_contract": definition.outbox_contract,
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise BusinessWorkflowError(f"workflow definition missing required fields: {missing}")
        if not definition.step_graph:
            raise BusinessWorkflowError("workflow definition must contain at least one step")
        seen: set[str] = set()
        for step in definition.step_graph:
            if step.step_key in seen:
                raise BusinessWorkflowError(f"duplicate workflow step_key: {step.step_key}")
            seen.add(step.step_key)
            if step.formal_skill_id not in FORMAL_BUSINESS_WORKFLOW_SKILLS:
                raise BusinessWorkflowError(f"unknown workflow formal Skill: {step.formal_skill_id}")
            unknown_dependencies = sorted(set(step.depends_on) - seen)
            if unknown_dependencies:
                raise BusinessWorkflowError(f"step depends on unknown or future steps: {unknown_dependencies}")


def build_formal_business_workflow_steps(
    *,
    workflow_id: str,
    frozen_inputs: Iterable[FrozenSkillInput],
    priority: int = 0,
    max_attempts: int = 3,
) -> tuple[WorkflowStepSpec, ...]:
    steps = []
    seen: set[str] = set()
    for frozen in frozen_inputs:
        if frozen.workflow_id != workflow_id:
            raise BusinessWorkflowError("frozen input workflow_id mismatch")
        if frozen.step_key in seen:
            raise BusinessWorkflowError("duplicate workflow step_key")
        seen.add(frozen.step_key)
        steps.append(
            WorkflowStepSpec(
                step_key=frozen.step_key,
                job_kind=FORMAL_SKILL_JOB_KIND,
                payload=frozen.scheduler_payload(),
                priority=priority,
                max_attempts=max_attempts,
            )
        )
    if not steps:
        raise BusinessWorkflowError("formal business workflow requires at least one step")
    return tuple(steps)
