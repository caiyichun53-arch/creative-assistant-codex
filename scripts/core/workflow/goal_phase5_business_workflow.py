from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from scripts.core.model_gateway.formal_skill_adapter import FORMAL_SKILL_JOB_KIND
from scripts.core.persistence.goal01_store import content_hash
from scripts.core.workflow.goal05_workflow import WorkflowStepSpec


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
                "input_hash": self.input_hash,
                "assembly_hash": self.assembly_hash,
                "upstream_refs": list(self.upstream_refs),
                "experience_context": self.experience_context.as_payload(),
            },
        }


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
            "input_hash": input_hash,
            "upstream_refs": upstream,
            "experience_context": context.as_payload(),
        }
        return FrozenSkillInput(
            schema_version=self.schema_version,
            workflow_id=workflow_id,
            step_key=step_key,
            formal_skill_id=formal_skill_id,
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
