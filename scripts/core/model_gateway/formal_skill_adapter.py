from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.business_route_registry import load_registry, scan_direct_model_calls
from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProvider,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.model_gateway.goal07_skill_runner import (
    HostBindingSpec,
    PortableSkillSpec,
    SkillContractError,
)
from scripts.core.persistence.goal01_store import (
    PersistenceStore,
    canonical_json,
    content_hash,
    uuid7,
)
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob
from scripts.validation.clean_room_empty_db import health_check


GOAL_ID = "GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01"
FORMAL_MAPPING_PATH = ROOT / "FORMAL_SKILL_ROUTE_MAPPING.yaml"
FIRST_CONTRACT_PATH = ROOT / "FIRST_FORMAL_SKILL_CONTRACT.yaml"
CONTENT_CLASSIFY_CONTRACT_PATH = ROOT / "CONTENT_CLASSIFY_BUSINESS_CONTRACT.yaml"
CONTENT_CLASSIFY_FIXTURES_PATH = ROOT / "runtime_skills" / "content_classify" / "fixtures.yaml"
CONTENT_RELATION_JUDGE_CONTRACT_PATH = ROOT / "CONTENT_RELATION_JUDGE_BUSINESS_CONTRACT.yaml"
CONTENT_RELATION_JUDGE_FIXTURES_PATH = ROOT / "runtime_skills" / "content_relation_judge" / "fixtures.yaml"
SOURCE_TO_TOPIC_CONTRACT_PATH = ROOT / "SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml"
SOURCE_TO_TOPIC_FIXTURES_PATH = ROOT / "runtime_skills" / "source_to_topic" / "fixtures.yaml"
SAMPLE_DEEP_ANALYZE_CONTRACT_PATH = ROOT / "SAMPLE_DEEP_ANALYZE_BUSINESS_CONTRACT.yaml"
SAMPLE_DEEP_ANALYZE_FIXTURES_PATH = ROOT / "runtime_skills" / "sample_deep_analyze" / "fixtures.yaml"
TACTIC_EXTRACT_CONTRACT_PATH = ROOT / "TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml"
TACTIC_EXTRACT_FIXTURES_PATH = ROOT / "runtime_skills" / "tactic_extract" / "fixtures.yaml"
RESEARCH_EVIDENCE_EXTRACT_CONTRACT_PATH = ROOT / "RESEARCH_EVIDENCE_EXTRACT_BUSINESS_CONTRACT.yaml"
RESEARCH_EVIDENCE_EXTRACT_FIXTURES_PATH = ROOT / "runtime_skills" / "research_evidence_extract" / "fixtures.yaml"
PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH = ROOT / "PRODUCTION_RESEARCH_PLAN_BUSINESS_CONTRACT.yaml"
PRODUCTION_RESEARCH_PLAN_FIXTURES_PATH = ROOT / "runtime_skills" / "production_research_plan" / "fixtures.yaml"
CONTENT_PLAN_CONTRACT_PATH = ROOT / "CONTENT_PLAN_BUSINESS_CONTRACT.yaml"
CONTENT_PLAN_FIXTURES_PATH = ROOT / "runtime_skills" / "content_plan" / "fixtures.yaml"
SCRIPT_GENERATE_CONTRACT_PATH = ROOT / "SCRIPT_GENERATE_BUSINESS_CONTRACT.yaml"
SCRIPT_GENERATE_FIXTURES_PATH = ROOT / "runtime_skills" / "script_generate" / "fixtures.yaml"
SCRIPT_REVIEW_CONTRACT_PATH = ROOT / "SCRIPT_REVIEW_BUSINESS_CONTRACT.yaml"
SCRIPT_REVIEW_FIXTURES_PATH = ROOT / "runtime_skills" / "script_review" / "fixtures.yaml"
EXPERIMENT_REVIEW_CONTRACT_PATH = ROOT / "EXPERIMENT_REVIEW_BUSINESS_CONTRACT.yaml"
EXPERIMENT_REVIEW_FIXTURES_PATH = ROOT / "runtime_skills" / "experiment_review" / "fixtures.yaml"
EXPERIENCE_REVISION_PROPOSE_CONTRACT_PATH = ROOT / "EXPERIENCE_REVISION_PROPOSE_BUSINESS_CONTRACT.yaml"
EXPERIENCE_REVISION_PROPOSE_FIXTURES_PATH = (
    ROOT / "runtime_skills" / "experience_revision_propose" / "fixtures.yaml"
)
STATUS_PATH = ROOT / "CONTENT_CLASSIFY_STATUS.yaml"
REPORT_PATH = ROOT / f"{GOAL_ID}_VALIDATION_REPORT.md"
PROGRESS_PATH = ROOT / "implementation_progress" / f"{GOAL_ID}.md"
LIVE_GATE_STATUS_PATH = ROOT / "CONTENT_CLASSIFY_LIVE_GATE_STATUS.yaml"
POSTGRES_EVIDENCE_PATH = ROOT / "validation_evidence" / f"{GOAL_ID}_POSTGRES.md"
SCHEMA_PATH = Path(__file__).with_name("formal_skill_adapter_schema.sqlite.sql")
FORMAL_SKILL_JOB_KIND = "formal_skill.execute"
FORMAL_SKILL_RESULT_SCHEMA_VERSION = "formal_business_skill_result.v1"
CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION = "content_classify.output.v1"
CONTENT_RELATION_JUDGE_GOAL_ID = "GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01"
CONTENT_RELATION_JUDGE_OUTPUT_SCHEMA_VERSION = "content_relation_judge.output.v1"
SOURCE_TO_TOPIC_GOAL_ID = "GOAL-V0.6.2-PRODUCTION-COMPLETION-01"
SOURCE_TO_TOPIC_OUTPUT_SCHEMA_VERSION = "source_to_topic.output.v1"
SAMPLE_DEEP_ANALYZE_OUTPUT_SCHEMA_VERSION = "sample_deep_analyze.output.v1"
TACTIC_EXTRACT_OUTPUT_SCHEMA_VERSION = "tactic_extract.output.v1"
# 2026-07-11: raised 12->200 (effectively uncapped for a diagnostic run) --
# was a third, independent hardcoded copy of the same unsourced "12" also
# present in TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml's schema (all three
# copies now updated together; see that file's evidence_requirements
# comment for the real background). This constant exists so this file and
# the schema file cannot silently drift apart again.
TACTIC_EXTRACT_OUTPUT_ARRAY_MAX = 200
RESEARCH_EVIDENCE_EXTRACT_OUTPUT_SCHEMA_VERSION = "research_evidence_extract.output.v1"
PRODUCTION_RESEARCH_PLAN_OUTPUT_SCHEMA_VERSION = "production_research_plan.output.v1"
CONTENT_PLAN_OUTPUT_SCHEMA_VERSION = "content_plan.output.v1"
SCRIPT_GENERATE_OUTPUT_SCHEMA_VERSION = "script_generate.output.v1"
SCRIPT_REVIEW_OUTPUT_SCHEMA_VERSION = "script_review.output.v1"
EXPERIMENT_REVIEW_OUTPUT_SCHEMA_VERSION = "experiment_review.output.v1"
EXPERIENCE_REVISION_PROPOSE_OUTPUT_SCHEMA_VERSION = "experience_revision_propose.output.v1"

BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "classification_taxonomy",
    "output_enums",
    "no_result_semantics",
    "uncertainty_semantics",
    "evidence_requirements",
    "confidence_policy",
    "domain_scope",
    "cross_domain_behavior",
    "third_domain_behavior",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "test_cases",
    "completion_definition",
}
RELATION_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "source_sections",
    "responsibility",
    "non_responsibilities",
    "relation_subject_definition",
    "allowed_inputs",
    "forbidden_inputs",
    "relation_taxonomy",
    "relation_directionality",
    "symmetric_relations",
    "asymmetric_relations",
    "order_sensitivity",
    "same_item_semantics",
    "insufficient_evidence_semantics",
    "no_relation_semantics",
    "uncertainty_semantics",
    "multiple_relation_policy",
    "primary_relation_policy",
    "evidence_requirements",
    "rationale_requirements",
    "confidence_policy",
    "domain_scope",
    "cross_domain_behavior",
    "third_domain_behavior",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "technical_failure_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
SOURCE_TO_TOPIC_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "topic_status_values",
    "topic_generation_policy",
    "evidence_requirements",
    "confidence_policy",
    "domain_scope",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
SAMPLE_DEEP_ANALYZE_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "analysis_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
TACTIC_EXTRACT_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "reduction_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
RESEARCH_EVIDENCE_EXTRACT_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "extraction_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
PRODUCTION_RESEARCH_PLAN_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "planning_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
CONTENT_PLAN_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "planning_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
SCRIPT_GENERATE_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "generation_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
SCRIPT_REVIEW_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "review_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
EXPERIMENT_REVIEW_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "review_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
EXPERIENCE_REVISION_PROPOSE_BUSINESS_CONTRACT_REQUIRED_KEYS = {
    "skill_id",
    "skill_version",
    "source_documents",
    "responsibility",
    "non_responsibilities",
    "allowed_inputs",
    "forbidden_inputs",
    "proposal_policy",
    "evidence_requirements",
    "input_length_limits",
    "context_budget",
    "token_budget",
    "timeout",
    "retry",
    "idempotency",
    "error_contract",
    "materialization_contract",
    "fixture_cases",
    "completion_definition",
}
CONCRETE_LABELS = {
    "fan_kepu_social_life",
    "music_entertainment",
    "third_domain_neutral",
    "cross_domain",
    "not_classifiable",
}
PROHIBITED_RATIONALE_TERMS = ("quality", "viral", "strategy", "writing suggestion", "爆款", "质量", "创作建议")
RELATION_TYPES = {
    "same_item",
    "equivalent",
    "contains",
    "contained_by",
    "complementary",
    "contradicts",
    "related_distinct",
    "no_relation",
    "insufficient_evidence",
}
SYMMETRIC_RELATIONS = {
    "same_item",
    "equivalent",
    "complementary",
    "contradicts",
    "related_distinct",
    "no_relation",
    "insufficient_evidence",
}
RELATION_DIRECTIONS = {
    "same_item": "symmetric",
    "equivalent": "symmetric",
    "contains": "left_contains_right",
    "contained_by": "left_contained_by_right",
    "complementary": "symmetric",
    "contradicts": "symmetric",
    "related_distinct": "symmetric",
    "no_relation": "symmetric",
    "insufficient_evidence": "not_applicable",
}
RELATION_SWAP = {
    "same_item": "same_item",
    "equivalent": "equivalent",
    "contains": "contained_by",
    "contained_by": "contains",
    "complementary": "complementary",
    "contradicts": "contradicts",
    "related_distinct": "related_distinct",
    "no_relation": "no_relation",
    "insufficient_evidence": "insufficient_evidence",
}


class FormalSkillAdapterError(RuntimeError):
    pass


class FormalSkillValidationError(FormalSkillAdapterError):
    pass


@dataclass(frozen=True)
class FormalSkillRunResult:
    formal_skill_id: str
    output_payload: dict[str, Any]
    model_input_payload: dict[str, Any]
    model_run_envelope_version_id: str
    skill_hash: str
    binding_hash: str
    model_route: str


@dataclass(frozen=True)
class FormalSkillContract:
    formal_skill_id: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_model_nodes: tuple[str, ...]
    model_required: bool
    route_name: str
    route_id: str
    binding_name: str
    binding_version: str
    input_map: dict[str, Any]
    output_map: dict[str, Any]
    model_input_schema: dict[str, Any]
    model_output_schema: dict[str, Any]
    prompt_template: str
    materializer_contract: str

    @classmethod
    def from_yaml(cls, path: Path = CONTENT_CLASSIFY_CONTRACT_PATH) -> "FormalSkillContract":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        model_binding = data["model_binding"]
        return cls(
            formal_skill_id=str(data.get("formal_skill_id") or data["skill_id"]),
            version=str(data.get("version") or data["skill_version"]),
            input_schema=dict(data["input_schema"]),
            output_schema=dict(data["output_schema"]),
            allowed_model_nodes=tuple(str(node) for node in data["allowed_model_nodes"]),
            model_required=bool(data["whether_model_is_required"]),
            route_name=str(model_binding["route_name"]),
            route_id=str(model_binding["route_id"]),
            binding_name=str(model_binding["binding_name"]),
            binding_version=str(model_binding["binding_version"]),
            input_map=dict(model_binding["input_map"]),
            output_map=dict(model_binding["output_map"]),
            model_input_schema=dict(model_binding["model_input_schema"]),
            model_output_schema=dict(model_binding["model_output_schema"]),
            prompt_template=str(model_binding["prompt_template"]),
            materializer_contract=str(data["materializer_contract"]),
        )

    def validate_contract(self) -> None:
        if not self.formal_skill_id:
            raise FormalSkillValidationError("formal_skill_id is required")
        if not self.version:
            raise FormalSkillValidationError("version is required")
        if not self.model_required:
            raise FormalSkillValidationError("first formal Skill must require ModelGateway")
        if self.route_name not in self.allowed_model_nodes:
            raise FormalSkillValidationError("route_name must be in allowed_model_nodes")
        try:
            ModelRouter.from_file().resolve(self.route_id, route_name=self.route_name)
        except ModelRouterError as exc:
            raise FormalSkillValidationError(f"invalid route_id for {self.formal_skill_id}: {exc}") from exc
        validate_schema_definition(self.input_schema, "input_schema")
        validate_schema_definition(self.output_schema, "output_schema")
        validate_schema_definition(self.model_input_schema, "model_input_schema")
        validate_schema_definition(self.model_output_schema, "model_output_schema")
        if self.formal_skill_id == "content_classify":
            validate_content_classify_business_contract(load_content_classify_business_contract())
        if self.formal_skill_id == "source_to_topic":
            validate_source_to_topic_business_contract(load_source_to_topic_business_contract())
        if self.formal_skill_id == "sample_deep_analyze":
            validate_sample_deep_analyze_business_contract(load_sample_deep_analyze_business_contract())
        if self.formal_skill_id == "tactic_extract":
            validate_tactic_extract_business_contract(load_tactic_extract_business_contract())
        if self.formal_skill_id == "research_evidence_extract":
            validate_research_evidence_extract_business_contract(load_research_evidence_extract_business_contract())
        if self.formal_skill_id == "production_research_plan":
            validate_production_research_plan_business_contract(load_production_research_plan_business_contract())
        if self.formal_skill_id == "content_plan":
            validate_content_plan_business_contract(load_content_plan_business_contract())
        if self.formal_skill_id == "script_generate":
            validate_script_generate_business_contract(load_script_generate_business_contract())
        if self.formal_skill_id == "script_review":
            validate_script_review_business_contract(load_script_review_business_contract())
        if self.formal_skill_id == "experiment_review":
            validate_experiment_review_business_contract(load_experiment_review_business_contract())
        if self.formal_skill_id == "experience_revision_propose":
            validate_experience_revision_propose_business_contract(load_experience_revision_propose_business_contract())
        if self.formal_skill_id == "content_relation_judge":
            validate_content_relation_judge_business_contract(load_content_relation_judge_business_contract())

    @property
    def skill_hash(self) -> str:
        return content_hash(
            {
                "formal_skill_id": self.formal_skill_id,
                "version": self.version,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "allowed_model_nodes": list(self.allowed_model_nodes),
                "route_id": self.route_id,
            },
            "formal_business_skill.contract.v1",
        )

    @property
    def binding_hash(self) -> str:
        return content_hash(
            {
                "binding_name": self.binding_name,
                "binding_version": self.binding_version,
                "input_map": self.input_map,
                "output_map": self.output_map,
            },
            "formal_business_skill.binding.v1",
        )

    def portable_skill(self) -> PortableSkillSpec:
        return PortableSkillSpec(
            skill_name=self.formal_skill_id,
            skill_version=self.version,
            route_name=self.route_name,
            prompt_template=self.prompt_template,
            required_input_keys=tuple(self.model_input_schema["required"]),
            output_contract=self.model_output_schema,
            metadata={"schema_version": "formal_business_skill.v1"},
        )

    def host_binding(self) -> HostBindingSpec:
        return HostBindingSpec(
            binding_name=self.binding_name,
            binding_version=self.binding_version,
            input_map={key: value["key"] for key, value in self.input_map.items() if value.get("source") == "input"},
            static_inputs={},
            metadata={"formal_skill_id": self.formal_skill_id},
        )


class FormalBusinessSkillAdapter:
    def __init__(self, *, contract: FormalSkillContract, gateway: ModelGateway):
        contract.validate_contract()
        self.contract = contract
        self.gateway = gateway

    def run(self, input_payload: dict[str, Any]) -> FormalSkillRunResult:
        validate_payload(input_payload, self.contract.input_schema)
        if self.contract.formal_skill_id == "content_plan":
            return self._run_content_plan(input_payload)
        if self.contract.formal_skill_id == "script_review":
            return self._run_script_review(input_payload)
        preprocessed = preprocess_formal_skill_input(self.contract.formal_skill_id, input_payload)
        model_input = apply_binding(self.contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, self.contract.model_input_schema)
        if self.contract.route_name not in self.gateway.routes:
            raise FormalSkillValidationError(f"missing approved model route: {self.contract.route_name}")

        portable = self.contract.portable_skill()
        host_binding = self.contract.host_binding()
        prompt = portable.render_prompt(model_input)
        model_run = self.gateway.complete(
            ModelRequest(
                route_name=self.contract.route_name,
                prompt=prompt,
                input_payload=model_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name=host_binding.binding_name,
                binding_version=host_binding.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id},
            )
        )
        model_output = parse_model_json(model_run.output_text)
        if self.contract.formal_skill_id == "content_classify":
            model_output = normalize_content_classify_model_output(model_output)
        elif self.contract.formal_skill_id == "content_relation_judge":
            model_output = normalize_content_relation_judge_model_output(model_output)
        validate_payload(model_output, self.contract.model_output_schema)
        output_payload = apply_binding(self.contract.output_map, input_payload, model_output, preprocessed)
        validate_payload(output_payload, self.contract.output_schema)
        if self.contract.formal_skill_id == "content_classify":
            validate_content_classify_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "content_relation_judge":
            validate_content_relation_judge_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "source_to_topic":
            validate_source_to_topic_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "sample_deep_analyze":
            validate_sample_deep_analyze_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "tactic_extract":
            validate_tactic_extract_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "research_evidence_extract":
            validate_research_evidence_extract_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "production_research_plan":
            validate_production_research_plan_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "script_generate":
            validate_script_generate_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "script_review":
            validate_script_review_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "experiment_review":
            validate_experiment_review_output_semantics(input_payload, output_payload)
        elif self.contract.formal_skill_id == "experience_revision_propose":
            validate_experience_revision_propose_output_semantics(input_payload, output_payload)
        return FormalSkillRunResult(
            formal_skill_id=self.contract.formal_skill_id,
            output_payload=output_payload,
            model_input_payload=model_input,
            model_run_envelope_version_id=model_run.envelope_version_id,
            skill_hash=self.contract.skill_hash,
            binding_hash=self.contract.binding_hash,
            model_route=self.contract.route_name,
        )

    def _run_content_plan(self, input_payload: dict[str, Any]) -> FormalSkillRunResult:
        # 2026-07-11: both subnode prompts below were rewritten. The hook
        # prompt used to omit candidate_topic/evidence_items/tactic_candidates
        # entirely (all three are required by this Skill's own public
        # input_schema, but never reached the model) -- the outline prompt
        # had the same gap. Both now receive the full real context and real
        # quality guidance (language, hook count, grounding in evidence,
        # drawing on tactic_candidates without copying them verbatim) instead
        # of a bare "return this JSON shape" instruction.
        for route_name in ("business.creation_hook", "business.creation_outline"):
            if route_name not in self.gateway.routes:
                raise FormalSkillValidationError(f"missing approved model route: {route_name}")
        hook_input = {
            "fixture_id": input_payload["request_id"],
            "brief": input_payload["brief"],
            "style_examples": input_payload["style_examples"],
            "candidate_topic": input_payload["candidate_topic"],
            "evidence_items": input_payload["evidence_items"],
            "tactic_candidates": input_payload["tactic_candidates"],
        }
        hook_run = self.gateway.complete(
            ModelRequest(
                route_name="business.creation_hook",
                prompt=(
                    "You are writing opening hooks (开头钩子) for a Chinese "
                    "short-video (抖音) spoken script. Return only JSON with "
                    "keys hooks, schema_version "
                    "(schema_version=content_plan.hook_output.v1).\n\n"
                    "Write every hook in natural, spoken Simplified Chinese, "
                    "not written or formal register. Return 1 to 5 distinct "
                    "hooks, each a real candidate opening line for this "
                    f"exact topic, not a generic template: candidate_topic="
                    f"{hook_input['candidate_topic']}; brief="
                    f"{hook_input['brief']}.\n\n"
                    "Ground each hook in the supplied evidence -- do not "
                    f"invent facts not present here: evidence_items="
                    f"{hook_input['evidence_items']}.\n\n"
                    "You may draw on these known-effective opening "
                    "techniques from prior real hits, but adapt them to this "
                    "specific topic rather than copying their wording "
                    f"verbatim: tactic_candidates={hook_input['tactic_candidates']}."
                    "\n\n"
                    "Match the voice and register of these real style "
                    f"examples: style_examples={hook_input['style_examples']}."
                    "\n\n"
                    "Avoid generic AI-writing tells: no formulaic '你有没有"
                    "想过' openers unless genuinely fitting, no hedging, no "
                    "hook that could apply to any topic interchangeably."
                    "\n\nBefore answering, double check your JSON includes "
                    "both keys -- hooks and schema_version set exactly to "
                    "content_plan.hook_output.v1. Do not omit schema_version."
                ),
                input_payload=hook_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name="content_plan_hook_subnode",
                binding_version=self.contract.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id, "subnode": "hook"},
            )
        )
        hook_output = parse_model_json(hook_run.output_text)
        validate_payload(hook_output, load_content_plan_business_contract()["hook_model_output_schema"])
        hooks = list(hook_output["hooks"])
        selected_hook = str(hooks[0]) if hooks else ""
        outline_input = {
            "fixture_id": input_payload["request_id"],
            "selected_hook": selected_hook,
            "brief": input_payload["brief"],
            "evidence_items": input_payload["evidence_items"],
            "tactic_candidates": input_payload["tactic_candidates"],
        }
        outline_run = self.gateway.complete(
            ModelRequest(
                route_name="business.creation_outline",
                prompt=(
                    "You are structuring the beat outline (节奏骨架) for a "
                    "Chinese short-video (抖音) spoken script. Return only "
                    "JSON with keys beats, schema_version "
                    "(schema_version=content_plan.outline_output.v1).\n\n"
                    "The outline must open from this exact selected hook, "
                    f"not a different opening: selected_hook={selected_hook}."
                    f" Overall direction: brief={outline_input['brief']}.\n\n"
                    "Produce 3 to 8 beats, one short sentence per beat "
                    "describing what that beat covers. Every beat must add "
                    "real new information -- no beat may just restate the "
                    "previous beat in different words. Build the sequence so "
                    "later beats can be grounded in this evidence when the "
                    f"full script is written: evidence_items="
                    f"{outline_input['evidence_items']}.\n\n"
                    "You may draw on these known-effective structure "
                    "patterns from prior real hits, adapted to this specific "
                    f"topic rather than copied verbatim: tactic_candidates="
                    f"{outline_input['tactic_candidates']}."
                    "\n\nBefore answering, double check your JSON includes "
                    "both keys -- beats and schema_version set exactly to "
                    "content_plan.outline_output.v1. Do not omit schema_version."
                ),
                input_payload=outline_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name=self.contract.binding_name,
                binding_version=self.contract.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id, "subnode": "outline"},
            )
        )
        outline_output = parse_model_json(outline_run.output_text)
        validate_payload(outline_output, load_content_plan_business_contract()["outline_model_output_schema"])
        output_payload = {
            "hooks": hooks,
            "selected_hook": selected_hook,
            "beats": outline_output["beats"],
            "schema_version": CONTENT_PLAN_OUTPUT_SCHEMA_VERSION,
        }
        validate_payload(output_payload, self.contract.output_schema)
        validate_content_plan_output_semantics(input_payload, output_payload)
        return FormalSkillRunResult(
            formal_skill_id=self.contract.formal_skill_id,
            output_payload=output_payload,
            model_input_payload={"hook_input": hook_input, "outline_input": outline_input},
            model_run_envelope_version_id=outline_run.envelope_version_id,
            skill_hash=self.contract.skill_hash,
            binding_hash=self.contract.binding_hash,
            model_route=self.contract.route_name,
        )

    def _run_script_review(self, input_payload: dict[str, Any]) -> FormalSkillRunResult:
        # 2026-07-11: all three subnode prompts below used to be a bare
        # "Return only JSON with keys X, Y, schema_version" string -- none of
        # them contained ANY real task content (not draft_text, not brief,
        # not evidence_items, not human_reference_refs). A real model call
        # would have had no way to know what script it was even reviewing.
        # All three now carry the real content plus real quality criteria.
        #
        # 2026-07-13 (置顶规则总表 V0.6.3 卷首核对后, 条目3/30/53/54): the pinned
        # rules doc requires 文案优化 (polish) to happen BEFORE 审核 (the review
        # gate) -- polish is now first here, run directly on the raw draft with
        # its own standalone quality criteria (去工程腔/人感/节奏/表达/情绪/留存/
        # 口播自然度), not "fix what review found" (that dependency is exactly
        # what put it in the wrong order). creation_review now runs on the
        # POLISHED text and, per §54's real scope ("同时检查事实...表达质量...
        # AI腔..."), is the final gate together with ai_flavor_judge -- both
        # inspect the polished output, review handles facts/coherence/register,
        # ai_flavor_judge handles the detailed AI-tell checklist.
        for route_name in ("business.creation_review", "business.creation_polish", "business.ai_flavor_judge"):
            if route_name not in self.gateway.routes:
                raise FormalSkillValidationError(f"missing approved model route: {route_name}")
        contract_data = load_script_review_business_contract()
        polish_input = {
            "fixture_id": input_payload["request_id"],
            "draft_text": input_payload["draft_text"],
        }
        polish_run = self.gateway.complete(
            ModelRequest(
                route_name="business.creation_polish",
                prompt=(
                    "You are polishing a Chinese spoken-narration short-video "
                    "(抖音口播) script draft -- this happens BEFORE the review "
                    "gate, on the raw draft directly, not as a fix-up for "
                    "issues someone else already found. Return only JSON with "
                    "keys polished_text, revision_focus, schema_version "
                    "(schema_version=script_review.polish_output.v1).\n\n"
                    f"draft_text={polish_input['draft_text']}\n\n"
                    "Improve, in this order of priority: reduce 工程腔/模板腔/"
                    "AI腔 (mechanical, corporate-report, or template phrasing); "
                    "strengthen the human feel (人感) and natural speaking "
                    "rhythm (节奏); sharpen the expression so it sounds like "
                    "someone actually talking, not reading a script; improve "
                    "hook/opening retention and information density; keep the "
                    "emotional throughline (情绪) coherent end to end.\n\n"
                    "Do NOT just swap in synonyms, do NOT force the text into "
                    "a generic template, and do NOT mechanically bolt in "
                    "phrases from experience/reference material that don't "
                    "actually fit this specific draft. revision_focus must "
                    "name the 1-3 main directions you actually changed (e.g. "
                    "\"开头留存\" / \"去工程腔\" / \"节奏\"), not a vague "
                    "summary. polished_text must be the complete replacement "
                    "script, not a diff, and must still read as natural "
                    "spoken Chinese."
                    "\n\nBefore answering, double check your JSON includes "
                    "all three keys -- polished_text, revision_focus, and "
                    "schema_version set exactly to "
                    "script_review.polish_output.v1. Do not omit "
                    "schema_version."
                ),
                input_payload=polish_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name="script_review_polish_subnode",
                binding_version=self.contract.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id, "subnode": "polish"},
            )
        )
        polish_output = parse_model_json(polish_run.output_text)
        validate_payload(polish_output, contract_data["polish_model_output_schema"])
        review_input = {
            "fixture_id": input_payload["request_id"],
            "draft_text": polish_output["polished_text"],
            "brief": input_payload["brief"],
            "evidence_items": input_payload["evidence_items"],
        }
        review_run = self.gateway.complete(
            ModelRequest(
                route_name="business.creation_review",
                prompt=(
                    "You are the final review gate for a Chinese "
                    "spoken-narration short-video (抖音口播) script -- this "
                    "runs AFTER polishing, on the polished text, as the last "
                    "check before the draft can be used. Return only JSON "
                    "with keys verdict, issues, schema_version "
                    "(schema_version=script_review.review_output.v1).\n\n"
                    f"draft_text={review_input['draft_text']}\n\n"
                    f"It was meant to follow this brief: brief="
                    f"{review_input['brief']}\n\n"
                    "Check specifically: does the draft stay on topic and "
                    "match the brief; is it coherent and logically ordered; "
                    "does every factual claim trace back to this evidence "
                    "(flag any claim that does not) evidence_items="
                    f"{review_input['evidence_items']}; does it read as "
                    "natural spoken Chinese rather than written/formal "
                    "register; did polishing actually improve expression "
                    "quality or leave it flat.\n\n"
                    "verdict must be pass (no real problems), revise (fixable "
                    "problems exist -- polishing did not fully address them), "
                    "or fail (would need a full rewrite). Each item in issues "
                    "must name a specific, concrete problem tied to a "
                    "specific part of the draft -- not a vague general "
                    "comment."
                    "\n\nBefore answering, double check your JSON includes "
                    "all three keys -- verdict, issues, and schema_version "
                    "set exactly to script_review.review_output.v1. Do not "
                    "omit schema_version."
                ),
                input_payload=review_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name="script_review_review_subnode",
                binding_version=self.contract.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id, "subnode": "review"},
            )
        )
        review_output = parse_model_json(review_run.output_text)
        validate_payload(review_output, contract_data["review_model_output_schema"])
        ai_input = {
            "fixture_id": input_payload["request_id"],
            "draft_text": polish_output["polished_text"],
            "human_reference_refs": input_payload["human_reference_refs"],
        }
        ai_run = self.gateway.complete(
            ModelRequest(
                route_name="business.ai_flavor_judge",
                prompt=(
                    "You are judging whether a Chinese spoken-narration "
                    "short-video (抖音口播) script sounds AI-written. Return "
                    "only JSON with keys ai_flavor_risk, revision_targets, "
                    "schema_version (schema_version="
                    "script_review.ai_flavor_output.v1).\n\n"
                    f"draft_text={ai_input['draft_text']}\n\n"
                    "Compare it against how real people actually write/speak "
                    "in these real reference examples: human_reference_refs="
                    f"{ai_input['human_reference_refs']}\n\n"
                    "Concrete AI-writing tells to check for (each one is a "
                    "real, observed pattern, not a vague notion of "
                    "'unnatural'): AI buzzwords (此外/与...保持一致/至关重要/"
                    "深入探讨/强调/持久的/增强/培养/关键/格局/展示/证明/充满活力"
                    "的); inflated-significance phrasing (标志着...的关键时刻/"
                    "体现了.../彰显了...重要性); forcing ideas into groups of "
                    "exactly three; negative parallelism (不仅...而且.../这不"
                    "仅仅是...而是...); avoiding a plain '是' in favor of "
                    "作为/代表/充当; vague attribution (专家认为/研究显示 with "
                    "no real source); generic upbeat closings (未来可期/值得"
                    "期待/这是重要的一步); overuse of the dash (——) as a "
                    "dramatic pause; every sentence the same length and "
                    "rhythm; over-explaining points a listener would already "
                    "get.\n\n"
                    "ai_flavor_risk is low/medium/high. Each revision_targets "
                    "item must name a specific instance of one of these "
                    "patterns actually present in draft_text, not a generic "
                    "warning."
                    "\n\nBefore answering, double check your JSON includes "
                    "all three keys -- ai_flavor_risk, revision_targets, and "
                    "schema_version set exactly to "
                    "script_review.ai_flavor_output.v1. Do not omit "
                    "schema_version."
                ),
                input_payload=ai_input,
                correlation_id=input_payload["correlation_id"],
                skill_name=self.contract.formal_skill_id,
                skill_version=self.contract.version,
                skill_hash=self.contract.skill_hash,
                binding_name=self.contract.binding_name,
                binding_version=self.contract.binding_version,
                binding_hash=self.contract.binding_hash,
                metadata={"formal_skill_id": self.contract.formal_skill_id, "subnode": "ai_flavor"},
            )
        )
        ai_output = parse_model_json(ai_run.output_text)
        validate_payload(ai_output, contract_data["ai_flavor_model_output_schema"])
        output_payload = {
            "verdict": review_output["verdict"],
            "issues": review_output["issues"],
            "polished_text": polish_output["polished_text"],
            "revision_focus": polish_output["revision_focus"],
            "ai_flavor_risk": ai_output["ai_flavor_risk"],
            "revision_targets": ai_output["revision_targets"],
            "schema_version": SCRIPT_REVIEW_OUTPUT_SCHEMA_VERSION,
        }
        validate_payload(output_payload, self.contract.output_schema)
        validate_script_review_output_semantics(input_payload, output_payload)
        return FormalSkillRunResult(
            formal_skill_id=self.contract.formal_skill_id,
            output_payload=output_payload,
            model_input_payload={"review_input": review_input, "polish_input": polish_input, "ai_input": ai_input},
            model_run_envelope_version_id=ai_run.envelope_version_id,
            skill_hash=self.contract.skill_hash,
            binding_hash=self.contract.binding_hash,
            model_route=self.contract.route_name,
        )


def apply_binding(
    binding_map: dict[str, Any],
    input_payload: dict[str, Any],
    model_output: dict[str, Any],
    preprocessed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preprocessed = preprocessed or {}
    bound: dict[str, Any] = {}
    for target_key, spec in binding_map.items():
        source = spec["source"]
        if source == "input":
            bound[target_key] = input_payload[spec["key"]]
        elif source == "model_output":
            bound[target_key] = model_output[spec["key"]]
        elif source == "preprocessed":
            bound[target_key] = preprocessed[spec["key"]]
        elif source == "literal":
            bound[target_key] = spec["value"]
        else:
            raise FormalSkillValidationError(f"unsupported binding source: {source}")
    return bound


def preprocess_content_classify_input(input_payload: dict[str, Any]) -> dict[str, Any]:
    title = " ".join(str(input_payload.get("title", "")).split())
    body = " ".join(str(input_payload.get("body", "")).split())
    text = f"title: {title}\nbody: {body}".strip()
    return {
        "content_text": text,
        "title_text": title,
        "body_text": body,
        "text_length": len(f"{title}{body}"),
    }


def preprocess_content_relation_judge_input(input_payload: dict[str, Any]) -> dict[str, Any]:
    left = " ".join(str(input_payload.get("left_content", "")).split())
    right = " ".join(str(input_payload.get("right_content", "")).split())
    return {
        "left_text": left,
        "right_text": right,
        "left_length": len(left),
        "right_length": len(right),
    }


def preprocess_source_to_topic_input(input_payload: dict[str, Any]) -> dict[str, Any]:
    source = " ".join(str(input_payload.get("source_content", "")).split())
    relation = " ".join(str(input_payload.get("relation_summary", "")).split())
    return {
        "source_text": source,
        "relation_text": relation,
        "source_length": len(source),
    }


def preprocess_sample_deep_analyze_input(input_payload: dict[str, Any]) -> dict[str, Any]:
    transcript = " ".join(str(input_payload.get("transcript_excerpt", "")).split())
    return {
        "transcript_text": transcript,
        "transcript_length": len(transcript),
    }


def preprocess_formal_skill_input(formal_skill_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    if formal_skill_id == "content_classify":
        return preprocess_content_classify_input(input_payload)
    if formal_skill_id == "content_relation_judge":
        return preprocess_content_relation_judge_input(input_payload)
    if formal_skill_id == "source_to_topic":
        return preprocess_source_to_topic_input(input_payload)
    if formal_skill_id == "sample_deep_analyze":
        return preprocess_sample_deep_analyze_input(input_payload)
    return {}


def load_content_classify_business_contract(path: Path = CONTENT_CLASSIFY_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_content_relation_judge_business_contract(path: Path = CONTENT_RELATION_JUDGE_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_source_to_topic_business_contract(path: Path = SOURCE_TO_TOPIC_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_sample_deep_analyze_business_contract(path: Path = SAMPLE_DEEP_ANALYZE_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_tactic_extract_business_contract(path: Path = TACTIC_EXTRACT_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_research_evidence_extract_business_contract(
    path: Path = RESEARCH_EVIDENCE_EXTRACT_CONTRACT_PATH,
) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_production_research_plan_business_contract(
    path: Path = PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH,
) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_content_plan_business_contract(path: Path = CONTENT_PLAN_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_script_generate_business_contract(path: Path = SCRIPT_GENERATE_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_script_review_business_contract(path: Path = SCRIPT_REVIEW_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_experiment_review_business_contract(path: Path = EXPERIMENT_REVIEW_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_experience_revision_propose_business_contract(
    path: Path = EXPERIENCE_REVISION_PROPOSE_CONTRACT_PATH,
) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_content_classify_fixtures(path: Path = CONTENT_CLASSIFY_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fixtures = data.get("fixtures") or []
    expanded: list[dict[str, Any]] = []
    for fixture in fixtures:
        item = dict(fixture)
        input_payload = dict(item.get("input") or {})
        if input_payload.get("body") == "PLACEHOLDER_OVERLONG_BODY":
            input_payload["body"] = "超长文本" * 320
        item["input"] = input_payload
        expanded.append(item)
    return expanded


def load_content_relation_judge_fixtures(path: Path = CONTENT_RELATION_JUDGE_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fixtures = data.get("fixtures") or []
    expanded: list[dict[str, Any]] = []
    for fixture in fixtures:
        item = dict(fixture)
        input_payload = dict(item.get("input") or {})
        if input_payload.get("left_content") == "PLACEHOLDER_OVERLONG_CONTENT":
            input_payload["left_content"] = "超长关系输入" * 260
        if input_payload.get("right_content") == "PLACEHOLDER_OVERLONG_CONTENT":
            input_payload["right_content"] = "超长关系输入" * 260
        item["input"] = input_payload
        expanded.append(item)
    return expanded


def load_source_to_topic_fixtures(path: Path = SOURCE_TO_TOPIC_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_sample_deep_analyze_fixtures(path: Path = SAMPLE_DEEP_ANALYZE_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_tactic_extract_fixtures(path: Path = TACTIC_EXTRACT_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_research_evidence_extract_fixtures(
    path: Path = RESEARCH_EVIDENCE_EXTRACT_FIXTURES_PATH,
) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_production_research_plan_fixtures(
    path: Path = PRODUCTION_RESEARCH_PLAN_FIXTURES_PATH,
) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_content_plan_fixtures(path: Path = CONTENT_PLAN_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_script_generate_fixtures(path: Path = SCRIPT_GENERATE_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_script_review_fixtures(path: Path = SCRIPT_REVIEW_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_experiment_review_fixtures(path: Path = EXPERIMENT_REVIEW_FIXTURES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def load_experience_revision_propose_fixtures(
    path: Path = EXPERIENCE_REVISION_PROPOSE_FIXTURES_PATH,
) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("fixtures") or [])


def validate_content_classify_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"content_classify business contract missing keys: {missing}")
    if data.get("schema_version") != "content_classify.business_contract.v1":
        raise FormalSkillValidationError("unexpected content_classify business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("content_classify business contract has missing_requirement entries")
    if data.get("skill_id") != "content_classify":
        raise FormalSkillValidationError("content_classify business contract skill_id mismatch")
    output_enums = data.get("output_enums") or {}
    for key in ("classification_status", "primary_label", "no_result_reason", "uncertainty_reason", "confidence"):
        if not output_enums.get(key):
            raise FormalSkillValidationError(f"content_classify output enum missing: {key}")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_content_relation_judge_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(RELATION_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"content_relation_judge business contract missing keys: {missing}")
    if data.get("schema_version") != "content_relation_judge.business_contract.v1":
        raise FormalSkillValidationError("unexpected content_relation_judge business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("content_relation_judge business contract has missing_requirement entries")
    if data.get("skill_id") != "content_relation_judge":
        raise FormalSkillValidationError("content_relation_judge business contract skill_id mismatch")
    relation_types = set((data.get("relation_taxonomy") or {}).get("relation_types") or {})
    if relation_types != RELATION_TYPES:
        raise FormalSkillValidationError("content_relation_judge relation taxonomy mismatch")
    symmetric = set(data.get("symmetric_relations") or [])
    asymmetric = set(data.get("asymmetric_relations") or [])
    if symmetric != SYMMETRIC_RELATIONS:
        raise FormalSkillValidationError("content_relation_judge symmetric relation set mismatch")
    if asymmetric != {"contains", "contained_by"}:
        raise FormalSkillValidationError("content_relation_judge asymmetric relation set mismatch")
    priority = (data.get("primary_relation_policy") or {}).get("priority_order") or []
    if priority != [
        "same_item",
        "equivalent",
        "contradicts",
        "contains_or_contained_by",
        "complementary",
        "related_distinct",
        "no_relation",
    ]:
        raise FormalSkillValidationError("content_relation_judge primary relation policy mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.content_relation_judgement"]:
        raise FormalSkillValidationError("content_relation_judge must use only business.content_relation_judgement")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
        "relation_type_count": len(relation_types),
        "symmetric_relation_count": len(symmetric),
        "asymmetric_relation_count": len(asymmetric),
    }


def validate_source_to_topic_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(SOURCE_TO_TOPIC_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"source_to_topic business contract missing keys: {missing}")
    if data.get("schema_version") != "source_to_topic.business_contract.v1":
        raise FormalSkillValidationError("unexpected source_to_topic business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("source_to_topic business contract has missing_requirement entries")
    if data.get("skill_id") != "source_to_topic":
        raise FormalSkillValidationError("source_to_topic business contract skill_id mismatch")
    statuses = set(data.get("topic_status_values") or [])
    if statuses != {"generated", "needs_review", "no_result"}:
        raise FormalSkillValidationError("source_to_topic topic_status_values mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.source_to_topic"]:
        raise FormalSkillValidationError("source_to_topic must use only business.source_to_topic")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
        "topic_status_count": len(statuses),
    }


def validate_sample_deep_analyze_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(SAMPLE_DEEP_ANALYZE_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"sample_deep_analyze business contract missing keys: {missing}")
    if data.get("schema_version") != "sample_deep_analyze.business_contract.v1":
        raise FormalSkillValidationError("unexpected sample_deep_analyze business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("sample_deep_analyze business contract has missing_requirement entries")
    if data.get("skill_id") != "sample_deep_analyze":
        raise FormalSkillValidationError("sample_deep_analyze business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.reverse_dna_analysis"]:
        raise FormalSkillValidationError("sample_deep_analyze must use only business.reverse_dna_analysis")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_tactic_extract_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(TACTIC_EXTRACT_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"tactic_extract business contract missing keys: {missing}")
    if data.get("schema_version") != "tactic_extract.business_contract.v1":
        raise FormalSkillValidationError("unexpected tactic_extract business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("tactic_extract business contract has missing_requirement entries")
    if data.get("skill_id") != "tactic_extract":
        raise FormalSkillValidationError("tactic_extract business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.reverse_pattern_reduce"]:
        raise FormalSkillValidationError("tactic_extract must use only business.reverse_pattern_reduce")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_research_evidence_extract_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(RESEARCH_EVIDENCE_EXTRACT_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"research_evidence_extract business contract missing keys: {missing}")
    if data.get("schema_version") != "research_evidence_extract.business_contract.v1":
        raise FormalSkillValidationError("unexpected research_evidence_extract business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("research_evidence_extract business contract has missing_requirement entries")
    if data.get("skill_id") != "research_evidence_extract":
        raise FormalSkillValidationError("research_evidence_extract business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.research_evidence_extract"]:
        raise FormalSkillValidationError("research_evidence_extract must use only business.research_evidence_extract")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_production_research_plan_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(PRODUCTION_RESEARCH_PLAN_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"production_research_plan business contract missing keys: {missing}")
    if data.get("schema_version") != "production_research_plan.business_contract.v1":
        raise FormalSkillValidationError("unexpected production_research_plan business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("production_research_plan business contract has missing_requirement entries")
    if data.get("skill_id") != "production_research_plan":
        raise FormalSkillValidationError("production_research_plan business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.research_synthesis"]:
        raise FormalSkillValidationError("production_research_plan must use only business.research_synthesis")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_content_plan_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(CONTENT_PLAN_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"content_plan business contract missing keys: {missing}")
    if data.get("schema_version") != "content_plan.business_contract.v1":
        raise FormalSkillValidationError("unexpected content_plan business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("content_plan business contract has missing_requirement entries")
    if data.get("skill_id") != "content_plan":
        raise FormalSkillValidationError("content_plan business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.creation_hook", "business.creation_outline"]:
        raise FormalSkillValidationError("content_plan must use hook and outline subnodes only")
    hook_schema = data.get("hook_model_output_schema") or {}
    outline_schema = data.get("outline_model_output_schema") or {}
    validate_schema_definition(hook_schema, "content_plan_hook_model_output_schema")
    validate_schema_definition(outline_schema, "content_plan_outline_model_output_schema")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
        "subnode_count": len(allowed_nodes),
    }


def validate_script_generate_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(SCRIPT_GENERATE_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"script_generate business contract missing keys: {missing}")
    if data.get("schema_version") != "script_generate.business_contract.v1":
        raise FormalSkillValidationError("unexpected script_generate business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("script_generate business contract has missing_requirement entries")
    if data.get("skill_id") != "script_generate":
        raise FormalSkillValidationError("script_generate business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.creation_draft"]:
        raise FormalSkillValidationError("script_generate must use only business.creation_draft")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_script_review_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(SCRIPT_REVIEW_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"script_review business contract missing keys: {missing}")
    if data.get("schema_version") != "script_review.business_contract.v1":
        raise FormalSkillValidationError("unexpected script_review business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("script_review business contract has missing_requirement entries")
    if data.get("skill_id") != "script_review":
        raise FormalSkillValidationError("script_review business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.creation_review", "business.creation_polish", "business.ai_flavor_judge"]:
        raise FormalSkillValidationError("script_review must use review, polish and ai_flavor subnodes only")
    for key in ("review_model_output_schema", "polish_model_output_schema", "ai_flavor_model_output_schema"):
        validate_schema_definition(data.get(key) or {}, f"script_review_{key}")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
        "subnode_count": len(allowed_nodes),
    }


def validate_experiment_review_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(EXPERIMENT_REVIEW_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"experiment_review business contract missing keys: {missing}")
    if data.get("schema_version") != "experiment_review.business_contract.v1":
        raise FormalSkillValidationError("unexpected experiment_review business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("experiment_review business contract has missing_requirement entries")
    if data.get("skill_id") != "experiment_review":
        raise FormalSkillValidationError("experiment_review business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.experiment_review"]:
        raise FormalSkillValidationError("experiment_review must use only business.experiment_review")
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_experience_revision_propose_business_contract(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(EXPERIENCE_REVISION_PROPOSE_BUSINESS_CONTRACT_REQUIRED_KEYS - set(data))
    if missing:
        raise FormalSkillValidationError(f"experience_revision_propose business contract missing keys: {missing}")
    if data.get("schema_version") != "experience_revision_propose.business_contract.v1":
        raise FormalSkillValidationError("unexpected experience_revision_propose business contract schema_version")
    if data.get("missing_requirements"):
        raise FormalSkillValidationError("experience_revision_propose business contract has missing_requirement entries")
    if data.get("skill_id") != "experience_revision_propose":
        raise FormalSkillValidationError("experience_revision_propose business contract skill_id mismatch")
    allowed_nodes = data.get("allowed_model_nodes") or []
    if allowed_nodes != ["business.experience_revision_propose"]:
        raise FormalSkillValidationError(
            "experience_revision_propose must use only business.experience_revision_propose"
        )
    return {
        "skill_id": data["skill_id"],
        "skill_version": data["skill_version"],
        "source_document_count": len(data.get("source_documents") or []),
        "missing_requirement_count": len(data.get("missing_requirements") or []),
    }


def validate_content_classify_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    status = output_payload["classification_status"]
    primary_label = output_payload["primary_label"]
    candidate_labels = output_payload["candidate_labels"]
    no_result_reason = output_payload["no_result_reason"]
    uncertainty_reason = output_payload["uncertainty_reason"]
    confidence = output_payload["confidence"]
    evidence_used = output_payload["evidence_used"]
    input_evidence = set(input_payload["evidence_items"])
    if not set(evidence_used).issubset(input_evidence):
        raise FormalSkillValidationError("evidence_used must be selected from input evidence_items")
    rationale = str(output_payload["rationale"]).lower()
    for term in PROHIBITED_RATIONALE_TERMS:
        if term.lower() in rationale:
            raise FormalSkillValidationError(f"rationale contains prohibited non-classification term: {term}")
    if status == "no_result":
        if primary_label != "none" or confidence != "none" or no_result_reason == "none":
            raise FormalSkillValidationError("no_result output must use label none, confidence none and a concrete reason")
        if candidate_labels:
            raise FormalSkillValidationError("no_result output must not include candidate_labels")
        return
    if no_result_reason != "none":
        raise FormalSkillValidationError("classified/uncertain outputs must use no_result_reason none")
    if status == "classified":
        if primary_label not in CONCRETE_LABELS:
            raise FormalSkillValidationError("classified output must use a concrete primary_label")
        if not candidate_labels:
            raise FormalSkillValidationError("classified output must include candidate_labels")
        if uncertainty_reason != "none":
            raise FormalSkillValidationError("classified output must use uncertainty_reason none")
        if confidence == "none":
            raise FormalSkillValidationError("classified output must not use confidence none")
    elif status == "multiple_candidates":
        if primary_label != "cross_domain":
            raise FormalSkillValidationError("multiple_candidates output must use primary_label cross_domain")
        if len(set(candidate_labels)) < 2:
            raise FormalSkillValidationError("multiple_candidates output requires at least two candidate labels")
        if uncertainty_reason != "multiple_supported_domains":
            raise FormalSkillValidationError("multiple_candidates output must preserve multiple_supported_domains")
    elif status == "uncertain":
        if primary_label != "none":
            raise FormalSkillValidationError("uncertain output must use primary_label none")
        if uncertainty_reason == "none":
            raise FormalSkillValidationError("uncertain output must include uncertainty_reason")
        if confidence == "high":
            raise FormalSkillValidationError("uncertain output must not use confidence high")
    else:
        raise FormalSkillValidationError(f"unsupported classification_status: {status}")


def validate_content_relation_judge_output_semantics(
    input_payload: dict[str, Any], output_payload: dict[str, Any]
) -> None:
    relation_type = output_payload["relation_type"]
    direction = output_payload["relation_direction"]
    confidence = output_payload["confidence"]
    left_evidence = output_payload["evidence_from_left"]
    right_evidence = output_payload["evidence_from_right"]
    compared_dimensions = output_payload["compared_dimensions"]
    missing_evidence = output_payload["missing_evidence"]
    if relation_type not in RELATION_TYPES:
        raise FormalSkillValidationError(f"unsupported relation_type: {relation_type}")
    expected_direction = RELATION_DIRECTIONS[relation_type]
    if direction != expected_direction:
        raise FormalSkillValidationError(f"{relation_type} must use relation_direction {expected_direction}")
    if not set(left_evidence).issubset(set(input_payload["left_evidence_items"])):
        raise FormalSkillValidationError("evidence_from_left must be selected from left_evidence_items")
    if not set(right_evidence).issubset(set(input_payload["right_evidence_items"])):
        raise FormalSkillValidationError("evidence_from_right must be selected from right_evidence_items")
    rationale = str(output_payload["rationale"]).lower()
    for term in PROHIBITED_RATIONALE_TERMS:
        if term.lower() in rationale:
            raise FormalSkillValidationError(f"rationale contains prohibited non-relation term: {term}")
    if "technical_failure" in rationale or relation_type == "technical_failure":
        raise FormalSkillValidationError("technical_failure must not be materialized as a relation")
    if relation_type == "insufficient_evidence":
        if confidence not in {"high", "medium"}:
            raise FormalSkillValidationError("insufficient_evidence must use high or medium confidence")
        if not missing_evidence:
            raise FormalSkillValidationError("insufficient_evidence must include missing_evidence")
        return
    if confidence == "low":
        raise FormalSkillValidationError("low confidence must be represented as insufficient_evidence")
    if relation_type == "no_relation":
        if not compared_dimensions:
            raise FormalSkillValidationError("no_relation must include compared_dimensions")
        if missing_evidence:
            raise FormalSkillValidationError("no_relation must not include missing_evidence")
        return
    if missing_evidence:
        raise FormalSkillValidationError("concrete relation output must not include missing_evidence")
    if not left_evidence or not right_evidence:
        raise FormalSkillValidationError("concrete relation output requires evidence from both sides")
    if relation_type == "same_item" and (
        input_payload.get("left_content") != input_payload.get("right_content")
        and input_payload.get("left_content", "").strip() != input_payload.get("right_content", "").strip()
    ):
        if "same public object" not in rationale:
            raise FormalSkillValidationError("same_item requires exact normalized text or explicit same public object rationale")


def validate_source_to_topic_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    status = output_payload["topic_status"]
    topic = output_payload["candidate_topic"]
    angle = output_payload["topic_angle"]
    evidence = output_payload["supporting_evidence"]
    constraints = output_payload["source_constraints"]
    no_result_reason = output_payload["no_result_reason"]
    confidence = output_payload["confidence"]
    input_evidence = set(input_payload["source_evidence_items"])
    if not set(evidence).issubset(input_evidence):
        raise FormalSkillValidationError("supporting_evidence must be selected from source_evidence_items")
    if status == "no_result":
        if topic != "" or angle != "" or evidence:
            raise FormalSkillValidationError("no_result source_to_topic output must not include topic, angle or evidence")
        if no_result_reason == "none" or confidence != "none":
            raise FormalSkillValidationError("no_result source_to_topic output must include concrete reason and confidence none")
        return
    if no_result_reason != "none":
        raise FormalSkillValidationError("generated source_to_topic output must use no_result_reason none")
    if not topic or not angle:
        raise FormalSkillValidationError("generated source_to_topic output requires candidate_topic and topic_angle")
    if confidence not in {"high", "medium", "low"}:
        raise FormalSkillValidationError("generated source_to_topic output requires concrete confidence")
    if not evidence:
        raise FormalSkillValidationError("generated source_to_topic output requires supporting_evidence")
    if status == "needs_review" and not constraints:
        raise FormalSkillValidationError("needs_review source_to_topic output must include source_constraints")
    if status not in {"generated", "needs_review"}:
        raise FormalSkillValidationError(f"unsupported topic_status: {status}")


def validate_sample_deep_analyze_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    transcript = str(input_payload["transcript_excerpt"]).strip()
    for key in ("topic_pattern", "hook_pattern", "structure_pattern"):
        value = str(output_payload[key]).strip()
        if not value:
            raise FormalSkillValidationError(f"sample_deep_analyze {key} must not be empty")
        if len(value) > 800:
            raise FormalSkillValidationError(f"sample_deep_analyze {key} is too long")
    if not transcript:
        raise FormalSkillValidationError("sample_deep_analyze requires transcript_excerpt")
    if output_payload["schema_version"] != SAMPLE_DEEP_ANALYZE_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("sample_deep_analyze output schema_version mismatch")


def validate_tactic_extract_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    common_patterns = output_payload["common_patterns"]
    example_candidates = output_payload["example_candidates"]
    if output_payload["schema_version"] != TACTIC_EXTRACT_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("tactic_extract output schema_version mismatch")
    if not common_patterns:
        raise FormalSkillValidationError("tactic_extract requires common_patterns")
    if not example_candidates:
        raise FormalSkillValidationError("tactic_extract requires example_candidates")
    if len(common_patterns) > TACTIC_EXTRACT_OUTPUT_ARRAY_MAX or len(example_candidates) > TACTIC_EXTRACT_OUTPUT_ARRAY_MAX:
        raise FormalSkillValidationError("tactic_extract output arrays exceed max size")
    for value in common_patterns + example_candidates:
        if not isinstance(value, str) or not value.strip():
            raise FormalSkillValidationError("tactic_extract output arrays must contain non-empty strings")
        lowered = value.lower()
        if "publish" in lowered or "writeback" in lowered or "直接发布" in value or "写入经验库" in value:
            raise FormalSkillValidationError("tactic_extract must not publish or write back experience")
    if len(input_payload["dna_note_refs"]) < 2:
        raise FormalSkillValidationError("tactic_extract requires at least two sample analysis refs")


def validate_research_evidence_extract_output_semantics(
    input_payload: dict[str, Any], output_payload: dict[str, Any]
) -> None:
    evidence_items = output_payload["evidence_items"]
    uncertainty_notes = output_payload["uncertainty_notes"]
    if output_payload["schema_version"] != RESEARCH_EVIDENCE_EXTRACT_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("research_evidence_extract output schema_version mismatch")
    if not evidence_items:
        raise FormalSkillValidationError("research_evidence_extract requires evidence_items")
    if len(evidence_items) > 12 or len(uncertainty_notes) > 8:
        raise FormalSkillValidationError("research_evidence_extract output arrays exceed max size")
    packet = str(input_payload["research_packet"])
    source_refs = set(input_payload["source_refs"])
    for item in evidence_items:
        if not isinstance(item, dict):
            raise FormalSkillValidationError("research_evidence_extract evidence_items must be objects")
        for key in ("claim", "source_ref", "supporting_text"):
            if not str(item.get(key, "")).strip():
                raise FormalSkillValidationError(f"research_evidence_extract evidence item missing {key}")
        if item["source_ref"] not in source_refs:
            raise FormalSkillValidationError("research_evidence_extract source_ref must come from source_refs")
        if str(item["supporting_text"]) not in packet:
            raise FormalSkillValidationError("research_evidence_extract supporting_text must be selected from research_packet")
    for note in uncertainty_notes:
        if not isinstance(note, str) or not note.strip():
            raise FormalSkillValidationError("research_evidence_extract uncertainty_notes must be non-empty strings")


def validate_production_research_plan_output_semantics(
    input_payload: dict[str, Any], output_payload: dict[str, Any]
) -> None:
    if output_payload["schema_version"] != PRODUCTION_RESEARCH_PLAN_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("production_research_plan output schema_version mismatch")
    summary = str(output_payload["summary"]).strip()
    claims = output_payload["claims"]
    plan_steps = output_payload["plan_steps"]
    open_questions = output_payload["open_questions"]
    if not summary:
        raise FormalSkillValidationError("production_research_plan summary must not be empty")
    if not claims or not plan_steps:
        raise FormalSkillValidationError("production_research_plan requires claims and plan_steps")
    if len(claims) > 12 or len(plan_steps) > 10 or len(open_questions) > 8:
        raise FormalSkillValidationError("production_research_plan output arrays exceed max size")
    input_claims = {
        str(item.get("claim", ""))
        for item in input_payload.get("evidence_items", [])
        if isinstance(item, dict)
    }
    tactic_candidates = set(str(item) for item in input_payload.get("tactic_candidates", []))
    for claim in claims:
        if not isinstance(claim, str) or not claim.strip():
            raise FormalSkillValidationError("production_research_plan claims must be non-empty strings")
        if claim not in input_claims:
            raise FormalSkillValidationError("production_research_plan claims must come from evidence_items")
    for step in plan_steps:
        if not isinstance(step, dict):
            raise FormalSkillValidationError("production_research_plan plan_steps must be objects")
        for key in ("step", "purpose", "uses"):
            if not str(step.get(key, "")).strip():
                raise FormalSkillValidationError(f"production_research_plan plan step missing {key}")
        uses = step["uses"]
        if uses not in input_claims and uses not in tactic_candidates:
            raise FormalSkillValidationError("production_research_plan plan step uses must reference evidence claim or tactic candidate")
    for question in open_questions:
        if not isinstance(question, str) or not question.strip():
            raise FormalSkillValidationError("production_research_plan open_questions must be non-empty strings")


def validate_content_plan_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    if output_payload["schema_version"] != CONTENT_PLAN_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("content_plan output schema_version mismatch")
    hooks = output_payload["hooks"]
    selected_hook = str(output_payload["selected_hook"]).strip()
    beats = output_payload["beats"]
    if not hooks:
        raise FormalSkillValidationError("content_plan requires hooks")
    if selected_hook not in hooks:
        raise FormalSkillValidationError("content_plan selected_hook must come from hooks")
    if not beats:
        raise FormalSkillValidationError("content_plan requires beats")
    if len(hooks) > 5 or len(beats) > 8:
        raise FormalSkillValidationError("content_plan output arrays exceed max size")
    for hook in hooks:
        if not isinstance(hook, str) or not hook.strip():
            raise FormalSkillValidationError("content_plan hooks must be non-empty strings")
    for beat in beats:
        if not isinstance(beat, str) or not beat.strip():
            raise FormalSkillValidationError("content_plan beats must be non-empty strings")
    if not str(input_payload.get("brief", "")).strip():
        raise FormalSkillValidationError("content_plan requires brief")


def validate_script_generate_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    if output_payload["schema_version"] != SCRIPT_GENERATE_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("script_generate output schema_version mismatch")
    draft_text = str(output_payload["draft_text"]).strip()
    if len(draft_text) < 50:
        raise FormalSkillValidationError("script_generate draft_text is too short")
    if len(draft_text) > 6000:
        raise FormalSkillValidationError("script_generate draft_text exceeds max length")
    if not input_payload.get("beats"):
        raise FormalSkillValidationError("script_generate requires beats")


def validate_script_review_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    if output_payload["schema_version"] != SCRIPT_REVIEW_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("script_review output schema_version mismatch")
    if output_payload["verdict"] not in {"pass", "revise", "fail"}:
        raise FormalSkillValidationError("script_review verdict is unsupported")
    polished_text = str(output_payload["polished_text"]).strip()
    if len(polished_text) < 50:
        raise FormalSkillValidationError("script_review polished_text is too short")
    if len(polished_text) > 6000:
        raise FormalSkillValidationError("script_review polished_text exceeds max length")
    if not output_payload["revision_focus"]:
        raise FormalSkillValidationError("script_review requires revision_focus (优化稿必须说明主要优化方向)")
    for focus in output_payload["revision_focus"]:
        if not isinstance(focus, str) or not focus.strip():
            raise FormalSkillValidationError("script_review revision_focus must be non-empty strings")
    if output_payload["ai_flavor_risk"] not in {"low", "medium", "high"}:
        raise FormalSkillValidationError("script_review ai_flavor_risk is unsupported")
    if not output_payload["revision_targets"]:
        raise FormalSkillValidationError("script_review requires revision_targets")
    for issue in output_payload["issues"]:
        if not isinstance(issue, str) or not issue.strip():
            raise FormalSkillValidationError("script_review issues must be non-empty strings")
    for target in output_payload["revision_targets"]:
        if not isinstance(target, str) or not target.strip():
            raise FormalSkillValidationError("script_review revision_targets must be non-empty strings")
    if not input_payload.get("human_reference_refs"):
        raise FormalSkillValidationError("script_review requires human_reference_refs")


def validate_experiment_review_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    if output_payload["schema_version"] != EXPERIMENT_REVIEW_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("experiment_review output schema_version mismatch")
    if output_payload["review_status"] not in {
        "supports_hypothesis",
        "refutes_hypothesis",
        "inconclusive",
        "needs_more_data",
    }:
        raise FormalSkillValidationError("experiment_review review_status is unsupported")
    tested_refs = set(input_payload["tested_experience_refs"])
    for key in ("supported_experience_refs", "refuted_experience_refs", "inconclusive_experience_refs"):
        refs = output_payload[key]
        if not set(refs).issubset(tested_refs):
            raise FormalSkillValidationError(f"experiment_review {key} must come from tested_experience_refs")
    evidence_claims = {
        str(item.get("claim", ""))
        for item in input_payload.get("evidence_items", [])
        if isinstance(item, dict)
    }
    if not set(output_payload["evidence_used"]).issubset(evidence_claims):
        raise FormalSkillValidationError("experiment_review evidence_used must come from input evidence claims")
    if not output_payload["key_findings"]:
        raise FormalSkillValidationError("experiment_review requires key_findings")
    forbidden_terms = ("publish", "writeback", "正式发布", "写入经验库", "修改正式经验")
    for value in output_payload["key_findings"] + output_payload["next_actions"]:
        if not isinstance(value, str) or not value.strip():
            raise FormalSkillValidationError("experiment_review findings and actions must be non-empty strings")
        if any(term in value.lower() for term in forbidden_terms):
            raise FormalSkillValidationError("experiment_review must not publish or modify formal experience")


def validate_experience_revision_propose_output_semantics(
    input_payload: dict[str, Any], output_payload: dict[str, Any]
) -> None:
    if output_payload["schema_version"] != EXPERIENCE_REVISION_PROPOSE_OUTPUT_SCHEMA_VERSION:
        raise FormalSkillValidationError("experience_revision_propose output schema_version mismatch")
    status = output_payload["proposal_status"]
    candidate_type = output_payload["candidate_type"]
    if status not in {"candidate_created", "no_change", "needs_human_review"}:
        raise FormalSkillValidationError("experience_revision_propose proposal_status is unsupported")
    if candidate_type not in {"create", "revise", "no_change"}:
        raise FormalSkillValidationError("experience_revision_propose candidate_type is unsupported")
    frozen_refs = {
        str(item.get("experience_ref", ""))
        for item in input_payload.get("frozen_experience_versions", [])
        if isinstance(item, dict)
    }
    target_ref = output_payload["target_experience_ref"]
    if candidate_type == "revise" and target_ref not in frozen_refs:
        raise FormalSkillValidationError("revision candidate target_experience_ref must be frozen in input")
    if candidate_type == "create" and target_ref != "new":
        raise FormalSkillValidationError("create candidate must use target_experience_ref=new")
    if status == "no_change" and candidate_type != "no_change":
        raise FormalSkillValidationError("no_change proposal must use candidate_type no_change")
    if status == "candidate_created" and candidate_type == "no_change":
        raise FormalSkillValidationError("candidate_created requires create or revise candidate_type")
    source_refs = {
        str(item.get("source_ref", ""))
        for item in input_payload.get("new_evidence_items", [])
        if isinstance(item, dict)
    }
    if not set(output_payload["evidence_refs"]).issubset(source_refs):
        raise FormalSkillValidationError("experience_revision_propose evidence_refs must come from new_evidence_items")
    if status == "candidate_created" and not str(output_payload["candidate_summary"]).strip():
        raise FormalSkillValidationError("candidate_created requires candidate_summary")
    forbidden_terms = ("published", "active_formal", "正式发布", "直接覆盖", "写入正式经验")
    text_values = [output_payload["candidate_summary"]] + output_payload["change_rationale"] + output_payload["governance_warnings"]
    for value in text_values:
        if not isinstance(value, str):
            raise FormalSkillValidationError("experience_revision_propose text fields must be strings")
        if any(term in value.lower() for term in forbidden_terms):
            raise FormalSkillValidationError("experience_revision_propose must only propose candidate changes")


def parse_model_json(output_text: str) -> dict[str, Any]:
    stripped = output_text.strip()
    if not stripped:
        raise FormalSkillValidationError("model output is empty")
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise FormalSkillValidationError("model output is not JSON") from exc
    if not isinstance(value, dict):
        raise FormalSkillValidationError("model output JSON must be an object")
    return value


def normalize_content_classify_model_output(model_output: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(model_output)
    none_markers = {None, "", "n/a", "na", "not_applicable", "not applicable", "null", "none"}
    for key in ("no_result_reason", "uncertainty_reason"):
        value = normalized.get(key)
        if value is None or (isinstance(value, str) and value.strip().lower() in none_markers):
            normalized[key] = "none"
    for key in ("candidate_labels", "evidence_used"):
        if normalized.get(key) is None:
            normalized[key] = []
        elif isinstance(normalized.get(key), str):
            normalized[key] = [normalized[key]]
    confidence = normalized.get("confidence")
    if isinstance(confidence, (int, float)):
        if confidence >= 0.75:
            normalized["confidence"] = "high"
        elif confidence >= 0.45:
            normalized["confidence"] = "medium"
        elif confidence > 0:
            normalized["confidence"] = "low"
        else:
            normalized["confidence"] = "none"
    elif isinstance(confidence, str):
        lowered = confidence.strip().lower()
        if lowered in {"not_applicable", "not applicable", "n/a", "na", "null", ""}:
            normalized["confidence"] = "none"
    if (
        normalized.get("classification_status") == "classified"
        and not normalized.get("candidate_labels")
        and normalized.get("primary_label") in CONCRETE_LABELS
    ):
        normalized["candidate_labels"] = [normalized["primary_label"]]
    return normalized


def normalize_content_relation_judge_model_output(model_output: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(model_output)
    for key in ("evidence_from_left", "evidence_from_right", "compared_dimensions", "missing_evidence"):
        if normalized.get(key) is None:
            normalized[key] = []
        elif isinstance(normalized.get(key), str):
            normalized[key] = [normalized[key]]
    confidence = normalized.get("confidence")
    if isinstance(confidence, (int, float)):
        if confidence >= 0.75:
            normalized["confidence"] = "high"
        elif confidence >= 0.45:
            normalized["confidence"] = "medium"
        else:
            normalized["confidence"] = "low"
    elif isinstance(confidence, str):
        normalized["confidence"] = confidence.strip().lower()
    relation_type = normalized.get("relation_type")
    if relation_type in RELATION_DIRECTIONS and not normalized.get("relation_direction"):
        normalized["relation_direction"] = RELATION_DIRECTIONS[str(relation_type)]
    return normalized


def validate_schema_definition(schema: dict[str, Any], label: str) -> None:
    if schema.get("type") != "object":
        raise FormalSkillValidationError(f"{label} must be an object schema")
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not required:
        raise FormalSkillValidationError(f"{label} must define required")
    if not isinstance(properties, dict):
        raise FormalSkillValidationError(f"{label} must define properties")
    missing = set(required) - set(properties)
    if missing:
        raise FormalSkillValidationError(f"{label} missing properties for required fields: {sorted(missing)}")
    if schema.get("additional_properties") is not False:
        raise FormalSkillValidationError(f"{label} must set additional_properties=false")


def validate_payload(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise FormalSkillValidationError("payload must be object")
    required = set(schema["required"])
    properties = schema["properties"]
    missing = required - payload.keys()
    if missing:
        raise FormalSkillValidationError(f"payload missing fields: {sorted(missing)}")
    if schema.get("additional_properties") is False:
        extra = payload.keys() - properties.keys()
        if extra:
            raise FormalSkillValidationError(f"payload has extra fields: {sorted(extra)}")
    for key, raw_spec in properties.items():
        if key not in payload:
            continue
        _validate_value(key, payload[key], _normalize_property(raw_spec))


def _normalize_property(raw_spec: Any) -> dict[str, Any]:
    if isinstance(raw_spec, str):
        return {"type": raw_spec}
    if isinstance(raw_spec, dict):
        return dict(raw_spec)
    raise FormalSkillValidationError(f"unsupported schema property spec: {raw_spec!r}")


def _validate_value(key: str, value: Any, spec: dict[str, Any]) -> None:
    typ = spec.get("type")
    if typ == "string":
        if not isinstance(value, str):
            raise FormalSkillValidationError(f"{key} must be string")
        if int(spec.get("minLength", 0)) and len(value) < int(spec["minLength"]):
            raise FormalSkillValidationError(f"{key} is shorter than minLength")
        if int(spec.get("maxLength", 0)) and len(value) > int(spec["maxLength"]):
            raise FormalSkillValidationError(f"{key} is longer than maxLength")
        if "enum" in spec and value not in spec["enum"]:
            raise FormalSkillValidationError(f"{key} is not an allowed enum value")
        if "const" in spec and value != spec["const"]:
            raise FormalSkillValidationError(f"{key} must equal const value")
        return
    if typ == "array":
        if not isinstance(value, list):
            raise FormalSkillValidationError(f"{key} must be array")
        if int(spec.get("minItems", 0)) and len(value) < int(spec["minItems"]):
            raise FormalSkillValidationError(f"{key} has too few items")
        if int(spec.get("maxItems", 0)) and len(value) > int(spec["maxItems"]):
            raise FormalSkillValidationError(f"{key} has too many items")
        item_spec = _normalize_property(spec.get("items", {"type": "string"}))
        for index, item in enumerate(value):
            _validate_value(f"{key}[{index}]", item, item_spec)
        return
    if typ == "object":
        if not isinstance(value, dict):
            raise FormalSkillValidationError(f"{key} must be object")
        return
    raise FormalSkillValidationError(f"unsupported schema type for {key}: {typ}")


class DeterministicContentClassifyModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "invalid_structure":
            return self._result(json.dumps({"classification_status": "maybe"}), request)
        if behavior == "missing_field":
            return self._result(
                json.dumps(
                    {
                        "classification_status": "classified",
                        "primary_label": "fan_kepu_social_life",
                        "candidate_labels": ["fan_kepu_social_life"],
                        "no_result_reason": "none",
                        "uncertainty_reason": "none",
                        "confidence": "high",
                        "evidence_used": request.input_payload.get("evidence_refs", [])[:1],
                        "schema_version": CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                request,
            )
        if behavior == "empty_result_object":
            return self._result(
                json.dumps(
                    self._payload(
                        status="no_result",
                        primary_label="none",
                        candidates=[],
                        no_result_reason="model_empty_result",
                        uncertainty_reason="none",
                        confidence="none",
                        rationale="Model returned an explicit empty-result classification.",
                        evidence=[],
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                request,
            )
        text = str(request.input_payload["candidate_topic"]).lower()
        evidence = list(request.input_payload.get("evidence_refs") or [])
        used = evidence[:2]
        fan = any(token in text for token in ("社区", "通勤", "电梯", "楼道", "邻里", "社保", "生活", "科普"))
        music = any(token in text for token in ("歌", "音乐", "歌手", "演唱会", "旋律", "专辑", "song"))
        third = any(token in text for token in ("面包", "保存", "食谱", "篮球", "金融"))
        noise = any(token in text for token in ("抽奖", "点击链接", "优惠码"))
        unsupported = "why this song" in text or "mixed language" in text
        title_music_body_social = "title: 老歌" in text and "社区停车位" in text
        short_text = len(text.replace("title:", "").replace("body:", "").strip()) < 16
        if not text.replace("title:", "").replace("body:", "").strip():
            payload = self._payload(
                status="no_result",
                primary_label="none",
                candidates=[],
                no_result_reason="empty_text",
                uncertainty_reason="none",
                confidence="none",
                rationale="Input title and body are empty, so no classification is produced.",
                evidence=[],
            )
        elif unsupported:
            payload = self._payload(
                status="uncertain",
                primary_label="none",
                candidates=["music_entertainment"] if music else [],
                no_result_reason="none",
                uncertainty_reason="mixed_language_boundary",
                confidence="low",
                rationale="The mixed-language evidence is too weak for a stable domain classification.",
                evidence=used,
            )
        elif short_text or "信息不足" in text or "不够" in text:
            payload = self._payload(
                status="no_result",
                primary_label="none",
                candidates=[],
                no_result_reason="insufficient_information",
                uncertainty_reason="none",
                confidence="none",
                rationale="The input evidence is insufficient for a formal classification.",
                evidence=[],
            )
        elif title_music_body_social:
            payload = self._payload(
                status="uncertain",
                primary_label="none",
                candidates=["music_entertainment", "fan_kepu_social_life"],
                no_result_reason="none",
                uncertainty_reason="title_body_conflict",
                confidence="low",
                rationale="Title and body point to different domains, so the classification remains uncertain.",
                evidence=used,
            )
        elif fan and music:
            payload = self._payload(
                status="multiple_candidates",
                primary_label="cross_domain",
                candidates=["fan_kepu_social_life", "music_entertainment"],
                no_result_reason="none",
                uncertainty_reason="multiple_supported_domains",
                confidence="medium",
                rationale="Evidence supports both social-life explanation and music-entertainment domains.",
                evidence=used,
            )
        elif fan:
            payload = self._payload(
                status="classified",
                primary_label="fan_kepu_social_life",
                candidates=["fan_kepu_social_life"],
                no_result_reason="none",
                uncertainty_reason="none",
                confidence="medium" if noise else "high",
                rationale="Evidence centers on social-life explanatory content.",
                evidence=used,
            )
        elif music:
            payload = self._payload(
                status="classified",
                primary_label="music_entertainment",
                candidates=["music_entertainment"],
                no_result_reason="none",
                uncertainty_reason="none",
                confidence="high",
                rationale="Evidence centers on music or entertainment content.",
                evidence=used,
            )
        elif third:
            payload = self._payload(
                status="classified",
                primary_label="third_domain_neutral",
                candidates=["third_domain_neutral"],
                no_result_reason="none",
                uncertainty_reason="none",
                confidence="medium",
                rationale="Evidence belongs outside the two named domains and uses the neutral third-domain label.",
                evidence=used,
            )
        elif noise:
            payload = self._payload(
                status="classified",
                primary_label="not_classifiable",
                candidates=["not_classifiable"],
                no_result_reason="none",
                uncertainty_reason="none",
                confidence="medium",
                rationale="Evidence is present but describes promotional non-content rather than a content domain.",
                evidence=used,
            )
        else:
            payload = self._payload(
                status="uncertain",
                primary_label="none",
                candidates=[],
                no_result_reason="none",
                uncertainty_reason="weak_evidence",
                confidence="low",
                rationale="The evidence is too weak to assign a stable domain label.",
                evidence=used,
            )
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _payload(
        *,
        status: str,
        primary_label: str,
        candidates: list[str],
        no_result_reason: str,
        uncertainty_reason: str,
        confidence: str,
        rationale: str,
        evidence: list[str],
    ) -> dict[str, Any]:
        return {
            "classification_status": status,
            "primary_label": primary_label,
            "candidate_labels": candidates,
            "no_result_reason": no_result_reason,
            "uncertainty_reason": uncertainty_reason,
            "confidence": confidence,
            "rationale": rationale,
            "evidence_used": evidence,
            "schema_version": CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION,
        }

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicContentRelationJudgeModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic relation model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic relation model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "illegal_relation_enum":
            return self._result(json.dumps({"relation_type": "similar"}), request)
        if behavior == "invalid_direction":
            return self._result(
                json.dumps(self._payload("contains", "symmetric", "high", request), ensure_ascii=False, sort_keys=True),
                request,
            )
        if behavior == "evidence_not_in_input":
            payload = self._payload("equivalent", "symmetric", "high", request)
            payload["evidence_from_left"] = ["unseen left evidence"]
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        if behavior == "missing_field":
            payload = self._payload("equivalent", "symmetric", "high", request)
            payload.pop("rationale", None)
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

        left = str(request.input_payload.get("left_content", ""))
        right = str(request.input_payload.get("right_content", ""))
        left_evidence = list(request.input_payload.get("left_evidence_refs") or [])
        right_evidence = list(request.input_payload.get("right_evidence_refs") or [])
        combined = f"{left}\n{right}".lower()
        if not left.strip() or not right.strip() or (len(left.strip()) < 8 and len(right.strip()) < 8):
            payload = self._payload(
                "insufficient_evidence",
                "not_applicable",
                "high",
                request,
                left_evidence=[],
                right_evidence=[],
                compared=[],
                missing=["missing_body_or_comparison_context"],
                rationale="Input lacks enough content or comparison evidence for a reliable relation judgement.",
            )
        elif left.strip() == right.strip():
            payload = self._payload("same_item", "symmetric", "high", request)
        elif "并不是演唱会之后" in right or "不能同时成立" in combined or "之前已经完成" in right:
            payload = self._payload(
                "contradicts",
                "symmetric",
                "high",
                request,
                compared=["specific proposition about timing or factual claim"],
                rationale="Both sides address the same concrete proposition and give incompatible conclusions.",
            )
        elif "维保停梯影响" in left and "维保停梯影响" not in right:
            payload = self._payload("contains", "left_contains_right", "high", request)
        elif "维保停梯影响" in right and "维保停梯影响" not in left:
            payload = self._payload("contained_by", "left_contained_by_right", "high", request)
        elif any(token in combined for token in ("第一波关注", "个人记忆", "临时访客", "观众合唱", "改编")):
            payload = self._payload("complementary", "symmetric", "medium", request)
        elif any(token in combined for token in ("上车位置", "换乘节奏")):
            payload = self._payload("related_distinct", "symmetric", "medium", request)
        elif self._looks_unrelated(left, right):
            payload = self._payload(
                "no_relation",
                "symmetric",
                "high",
                request,
                compared=["topic", "core proposition"],
                rationale="The supplied evidence is sufficient to compare topic and proposition, and no direct content relation is present.",
            )
        else:
            payload = self._payload("equivalent", "symmetric", "high", request)
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _looks_unrelated(left: str, right: str) -> bool:
        unrelated_pairs = (
            ("电梯", "专辑"),
            ("停车位", "老歌"),
            ("砧板", "电梯"),
            ("厨房", "电梯"),
        )
        return any((a in left and b in right) or (b in left and a in right) for a, b in unrelated_pairs)

    @staticmethod
    def _payload(
        relation_type: str,
        direction: str,
        confidence: str,
        request: ModelRequest,
        *,
        left_evidence: list[str] | None = None,
        right_evidence: list[str] | None = None,
        compared: list[str] | None = None,
        missing: list[str] | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        left_refs = list(request.input_payload.get("left_evidence_refs") or [])
        right_refs = list(request.input_payload.get("right_evidence_refs") or [])
        if left_evidence is None:
            left_evidence = left_refs[:1]
        if right_evidence is None:
            right_evidence = right_refs[:1]
        if compared is None:
            compared = ["core facts", "main information"]
        if missing is None:
            missing = []
        if rationale is None:
            rationale = f"Input evidence supports the formal {relation_type} relation."
        return {
            "relation_type": relation_type,
            "relation_direction": direction,
            "confidence": confidence,
            "evidence_from_left": left_evidence,
            "evidence_from_right": right_evidence,
            "compared_dimensions": compared,
            "missing_evidence": missing,
            "rationale": rationale,
            "schema_version": CONTENT_RELATION_JUDGE_OUTPUT_SCHEMA_VERSION,
        }

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicSourceToTopicModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic source_to_topic model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic source_to_topic model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"topic_status": "generated"}), request)
        if behavior == "evidence_not_in_input":
            payload = self._payload(
                "generated",
                "陌生证据为什么突然爆火",
                "from_source_gap",
                ["unseen evidence"],
                [],
                "none",
                "medium",
            )
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

        source = str(request.input_payload.get("source_content", ""))
        evidence = list(request.input_payload.get("source_evidence_refs") or [])
        domain = str(request.input_payload.get("domain_label", "unknown"))
        relation = str(request.input_payload.get("relation_summary", ""))
        if not source.strip() or not evidence:
            payload = self._payload(
                "no_result",
                "",
                "",
                [],
                ["missing_source_content_or_evidence"],
                "insufficient_source_evidence",
                "none",
            )
        elif domain == "unknown" or "insufficient" in relation.lower():
            payload = self._payload(
                "needs_review",
                f"{evidence[0]}背后的信息缺口",
                "source_needs_human_review",
                evidence[:2],
                ["domain_or_relation_boundary_unclear"],
                "none",
                "low",
            )
        elif "电梯" in source or "通勤" in source:
            payload = self._payload(
                "generated",
                "为什么小区电梯总在早高峰堵住",
                "生活现象解释",
                evidence[:2],
                ["must_not_claim_platform_metrics_without_evidence"],
                "none",
                "high",
            )
        elif "歌" in source or "音乐" in source or "演唱会" in source:
            payload = self._payload(
                "generated",
                "一首老歌为什么会重新被年轻人翻出来",
                "音乐记忆与当下情绪",
                evidence[:2],
                ["must_separate_public_evidence_from_fan_speculation"],
                "none",
                "high",
            )
        else:
            payload = self._payload(
                "generated",
                f"{evidence[0]}为什么值得重新讲一遍",
                "source_evidence_reframing",
                evidence[:2],
                ["must_stay_inside_supplied_source_evidence"],
                "none",
                "medium",
            )
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _payload(
        topic_status: str,
        candidate_topic: str,
        topic_angle: str,
        supporting_evidence: list[str],
        source_constraints: list[str],
        no_result_reason: str,
        confidence: str,
    ) -> dict[str, Any]:
        return {
            "topic_status": topic_status,
            "candidate_topic": candidate_topic,
            "topic_angle": topic_angle,
            "supporting_evidence": supporting_evidence,
            "source_constraints": source_constraints,
            "no_result_reason": no_result_reason,
            "confidence": confidence,
            "schema_version": SOURCE_TO_TOPIC_OUTPUT_SCHEMA_VERSION,
        }

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicSampleDeepAnalyzeModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic sample_deep_analyze model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic sample_deep_analyze model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"topic_pattern": "missing fields"}, ensure_ascii=False), request)
        transcript = str(request.input_payload.get("transcript_excerpt", ""))
        metrics = request.input_payload.get("metrics") or {}
        if "演唱会" in transcript or "老歌" in transcript:
            payload = self._payload(
                topic_pattern="old-song-memory_reactivated_by_live_context",
                hook_pattern="start_from_a_familiar_song_returning_in_an_unexpected_crowd",
                structure_pattern="memory_trigger_to_public_scene_to_current_emotion",
            )
        elif "电梯" in transcript or "通勤" in transcript:
            payload = self._payload(
                topic_pattern="ordinary_life_problem_explained_by_hidden_system",
                hook_pattern="name_a_daily_irritation_then_reveal_the_unseen_mechanism",
                structure_pattern="pain_scene_to_mechanism_to_practical_reframe",
            )
        elif int(metrics.get("like_count", 0) or 0) > 100000:
            payload = self._payload(
                topic_pattern="high_metric_source_needs_plain_language_reframe",
                hook_pattern="open_with_the_surprising_metric_then_ground_it_in_one_scene",
                structure_pattern="signal_to_reason_to_reusable_topic_angle",
            )
        else:
            payload = self._payload(
                topic_pattern="source_specific_tension_to_candidate_lesson",
                hook_pattern="surface_the_specific_tension_without_claiming_extra_facts",
                structure_pattern="source_fact_to_tension_to_reusable_pattern",
            )
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _payload(*, topic_pattern: str, hook_pattern: str, structure_pattern: str) -> dict[str, Any]:
        return {
            "topic_pattern": topic_pattern,
            "hook_pattern": hook_pattern,
            "structure_pattern": structure_pattern,
            "schema_version": SAMPLE_DEEP_ANALYZE_OUTPUT_SCHEMA_VERSION,
        }

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicTacticExtractModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic tactic_extract model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic tactic_extract model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"common_patterns": ["missing fields"]}, ensure_ascii=False), request)
        if behavior == "illegal_writeback":
            payload = self._payload(
                common_patterns=["publish this tactic directly"],
                example_candidates=["写入经验库"],
            )
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        refs = [str(item) for item in request.input_payload.get("dna_note_refs", [])]
        joined = " ".join(refs).lower()
        if "music" in joined or "song" in joined:
            payload = self._payload(
                common_patterns=[
                    "music_memory_reactivation",
                    "public_scene_to_private_emotion",
                ],
                example_candidates=[
                    "old_song_returns_after_live_context",
                    "audience_comment_memory_cluster",
                ],
            )
        elif "elevator" in joined or "life" in joined:
            payload = self._payload(
                common_patterns=[
                    "ordinary_life_problem_hidden_system",
                    "daily_pain_scene_to_mechanism",
                ],
                example_candidates=[
                    "elevator_peak_hour_system_explainer",
                    "neighborhood_problem_reframed_as_pattern",
                ],
            )
        else:
            payload = self._payload(
                common_patterns=[
                    "source_tension_to_reusable_angle",
                    "evidence_cluster_to_topic_method",
                ],
                example_candidates=[
                    "neutral_fixture_pattern_candidate",
                    "third_domain_reduction_candidate",
                ],
            )
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _payload(*, common_patterns: list[str], example_candidates: list[str]) -> dict[str, Any]:
        return {
            "common_patterns": common_patterns,
            "example_candidates": example_candidates,
            "schema_version": TACTIC_EXTRACT_OUTPUT_SCHEMA_VERSION,
        }

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicResearchEvidenceExtractModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic research_evidence_extract model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic research_evidence_extract model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"evidence_items": []}, ensure_ascii=False), request)
        packet = str(request.input_payload.get("research_packet", ""))
        refs = list(request.input_payload.get("source_refs") or [])
        source_ref = refs[0] if refs else "missing"
        if behavior == "unseen_source":
            source_ref = "unseen-source"
        if behavior == "unseen_text":
            supporting = "unseen supporting text"
        elif "电梯" in packet:
            supporting = "早高峰电梯拥堵与通勤集中、楼层分布有关"
        elif "老歌" in packet:
            supporting = "老歌传播与演唱会现场和个人记忆评论有关"
        else:
            supporting = packet.split("。")[0].strip() or packet[:40]
        claim = supporting
        payload = {
            "evidence_items": [
                {
                    "claim": claim,
                    "source_ref": source_ref,
                    "supporting_text": supporting,
                }
            ],
            "uncertainty_notes": [] if behavior != "with_uncertainty" else ["source packet has limited scope"],
            "schema_version": RESEARCH_EVIDENCE_EXTRACT_OUTPUT_SCHEMA_VERSION,
        }
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicProductionResearchPlanModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic production_research_plan model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic production_research_plan model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"summary": "missing fields"}, ensure_ascii=False), request)

        evidence_items = [item for item in request.input_payload.get("evidence_refs", []) if isinstance(item, dict)]
        tactic_candidates = [str(item) for item in request.input_payload.get("tactic_candidates", [])]
        first_claim = str(evidence_items[0].get("claim", "")) if evidence_items else ""
        first_tactic = tactic_candidates[0] if tactic_candidates else first_claim
        if behavior == "unseen_claim":
            first_claim = "unseen evidence claim"
        if behavior == "unseen_use":
            first_tactic = "unseen tactic"
        payload = {
            "summary": f"Plan around {request.input_payload.get('candidate_topic', 'candidate topic')}",
            "claims": [first_claim],
            "plan_steps": [
                {
                    "step": "anchor_research_boundary",
                    "purpose": "Keep the production brief grounded in supplied evidence.",
                    "uses": first_claim,
                },
                {
                    "step": "select_tactic_angle",
                    "purpose": "Choose one reusable tactic candidate for downstream planning.",
                    "uses": first_tactic,
                },
            ],
            "open_questions": [] if behavior != "with_open_question" else ["Need one more source before live production."],
            "schema_version": PRODUCTION_RESEARCH_PLAN_OUTPUT_SCHEMA_VERSION,
        }
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicContentPlanModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0
        self.routes_seen: list[str] = []

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        self.routes_seen.append(route.route_name)
        behavior = self.behavior
        is_hook = route.route_name == "business.creation_hook"
        is_outline = route.route_name == "business.creation_outline"
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic content_plan model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic content_plan model port failure")
        if behavior == "outline_failure" and is_outline:
            raise RuntimeError("synthetic content_plan outline failure")
        if behavior in {"empty", "hook_empty"} and is_hook:
            return self._result("", request)
        if behavior == "outline_empty" and is_outline:
            return self._result("", request)
        if behavior in {"not_json", "hook_not_json"} and is_hook:
            return self._result("not-json", request)
        if behavior == "outline_not_json" and is_outline:
            return self._result("not-json", request)
        if behavior in {"missing_field", "hook_missing_field"} and is_hook:
            return self._result(json.dumps({"schema_version": "content_plan.hook_output.v1"}, ensure_ascii=False), request)
        if behavior == "outline_missing_field" and is_outline:
            return self._result(json.dumps({"schema_version": "content_plan.outline_output.v1"}, ensure_ascii=False), request)
        if is_hook:
            hooks = [
                "Open with the familiar wait, then reveal the hidden system.",
                "Start from one small scene and turn it into the central question.",
            ]
            if behavior == "empty_hooks":
                hooks = []
            payload = {"hooks": hooks, "schema_version": "content_plan.hook_output.v1"}
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        if is_outline:
            selected_hook = str(request.input_payload.get("selected_hook", "selected hook"))
            beats = [
                f"Beat 1: Use the selected hook: {selected_hook}",
                "Beat 2: Ground the problem in the supplied brief.",
                "Beat 3: Turn the tactic into a concrete structure for drafting.",
            ]
            if behavior == "empty_beats":
                beats = []
            payload = {"beats": beats, "schema_version": "content_plan.outline_output.v1"}
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        raise RuntimeError(f"unexpected content_plan route: {route.route_name}")

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicScriptGenerateModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic script_generate model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic script_generate model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"schema_version": SCRIPT_GENERATE_OUTPUT_SCHEMA_VERSION}, ensure_ascii=False), request)
        outline = [str(item) for item in request.input_payload.get("outline", [])]
        brief = str(request.input_payload.get("brief", ""))
        if behavior == "empty_draft":
            draft_text = ""
        else:
            outline_text = " ".join(outline)
            draft_text = (
                f"{outline_text} {brief} This draft follows the supplied outline, keeps the explanation concrete, "
                "and stops before review, polish, publication, or deterministic banned-word checks."
            )
        payload = {"draft_text": draft_text, "schema_version": SCRIPT_GENERATE_OUTPUT_SCHEMA_VERSION}
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicScriptReviewModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0
        self.routes_seen: list[str] = []

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        self.routes_seen.append(route.route_name)
        behavior = self.behavior
        is_review = route.route_name == "business.creation_review"
        is_polish = route.route_name == "business.creation_polish"
        is_ai = route.route_name == "business.ai_flavor_judge"
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic script_review model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic script_review model port failure")
        if behavior == "polish_failure" and is_polish:
            raise RuntimeError("synthetic script_review polish failure")
        if behavior == "ai_failure" and is_ai:
            raise RuntimeError("synthetic script_review ai failure")
        if behavior in {"empty", "review_empty"} and is_review:
            return self._result("", request)
        if behavior == "polish_empty" and is_polish:
            return self._result("", request)
        if behavior == "ai_empty" and is_ai:
            return self._result("", request)
        if behavior in {"not_json", "review_not_json"} and is_review:
            return self._result("not-json", request)
        if behavior == "polish_not_json" and is_polish:
            return self._result("not-json", request)
        if behavior == "ai_not_json" and is_ai:
            return self._result("not-json", request)
        if behavior in {"missing_field", "review_missing_field"} and is_review:
            return self._result(json.dumps({"verdict": "revise"}, ensure_ascii=False), request)
        if behavior == "polish_missing_field" and is_polish:
            return self._result(json.dumps({"schema_version": "script_review.polish_output.v1"}, ensure_ascii=False), request)
        if behavior == "ai_missing_field" and is_ai:
            return self._result(json.dumps({"ai_flavor_risk": "low"}, ensure_ascii=False), request)
        if is_review:
            issues = ["Tighten the opening scene and remove broad claims."]
            if behavior == "empty_issues":
                issues = []
            payload = {"verdict": "revise", "issues": issues, "schema_version": "script_review.review_output.v1"}
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        if is_polish:
            text = str(request.input_payload.get("draft_text", ""))
            if behavior == "empty_polished_text":
                polished = ""
            else:
                polished = f"{text} Polished pass: the scene is clearer, the claim stays bounded, and no publishing action is taken."
            revision_focus = ["开头留存", "去工程腔"]
            if behavior == "empty_revision_focus":
                revision_focus = []
            payload = {
                "polished_text": polished,
                "revision_focus": revision_focus,
                "schema_version": "script_review.polish_output.v1",
            }
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        if is_ai:
            targets = ["Keep scene-first wording and avoid generic uplift language."]
            if behavior == "empty_revision_targets":
                targets = []
            payload = {
                "ai_flavor_risk": "low",
                "revision_targets": targets,
                "schema_version": "script_review.ai_flavor_output.v1",
            }
            return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)
        raise RuntimeError(f"unexpected script_review route: {route.route_name}")

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicExperimentReviewModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic experiment_review model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic experiment_review model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"review_status": "supports_hypothesis"}, ensure_ascii=False), request)
        refs = [str(item) for item in request.input_payload.get("tested_experience_refs", [])]
        evidence_items = [item for item in request.input_payload.get("evidence_refs", []) if isinstance(item, dict)]
        evidence_claim = str(evidence_items[0].get("claim", "")) if evidence_items else ""
        supported = refs[:1]
        refuted: list[str] = []
        inconclusive = refs[1:2]
        status = "supports_hypothesis"
        if behavior == "refutes":
            status = "refutes_hypothesis"
            supported = []
            refuted = refs[:1]
        if behavior == "inconclusive":
            status = "inconclusive"
            supported = []
            inconclusive = refs[:1]
        if behavior == "unseen_experience":
            supported = ["experience:unseen"]
        if behavior == "unseen_evidence":
            evidence_claim = "unseen evidence claim"
        finding = f"Supplied experiment evidence supports review status {status}."
        next_actions = ["Keep formal experience unchanged until candidate review."]
        if behavior == "illegal_publish":
            next_actions = ["publish this result directly to the formal experience library"]
        payload = {
            "review_status": status,
            "supported_experience_refs": supported,
            "refuted_experience_refs": refuted,
            "inconclusive_experience_refs": inconclusive,
            "key_findings": [finding],
            "evidence_used": [evidence_claim] if evidence_claim else [],
            "next_actions": next_actions,
            "schema_version": EXPERIMENT_REVIEW_OUTPUT_SCHEMA_VERSION,
        }
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class DeterministicExperienceRevisionProposeModelPort:
    provider_name = "formal_business_skill_test_port"

    def __init__(self, *, behavior: str = "success"):
        self.behavior = behavior
        self.call_count = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.call_count += 1
        behavior = self.behavior
        if behavior == "fail_once" and self.call_count == 1:
            raise RuntimeError("synthetic experience_revision_propose model port failure")
        if behavior == "failure":
            raise RuntimeError("synthetic experience_revision_propose model port failure")
        if behavior == "empty":
            return self._result("", request)
        if behavior == "not_json":
            return self._result("not-json", request)
        if behavior == "missing_field":
            return self._result(json.dumps({"proposal_status": "candidate_created"}, ensure_ascii=False), request)
        frozen = [item for item in request.input_payload.get("frozen_experience_versions", []) if isinstance(item, dict)]
        evidence_items = [item for item in request.input_payload.get("new_evidence_refs", []) if isinstance(item, dict)]
        target = str(frozen[0].get("experience_ref", "experience:missing")) if frozen else "new"
        source_ref = str(evidence_items[0].get("source_ref", "")) if evidence_items else ""
        status = "candidate_created"
        candidate_type = "revise" if frozen else "create"
        if behavior == "create":
            candidate_type = "create"
            target = "new"
        if behavior == "no_change":
            status = "no_change"
            candidate_type = "no_change"
            target = "none"
        if behavior == "unseen_target":
            target = "experience:unseen"
        if behavior == "unseen_evidence_ref":
            source_ref = "unseen-source"
        summary = "Candidate revises experience wording based on frozen review evidence."
        rationale = ["Experiment review supports a candidate-only revision proposal."]
        warnings = ["candidate_only_no_publication"]
        if behavior == "illegal_publish":
            summary = "Published active_formal experience update"
        payload = {
            "proposal_status": status,
            "candidate_type": candidate_type,
            "target_experience_ref": target,
            "candidate_summary": summary,
            "change_rationale": rationale,
            "evidence_refs": [source_ref] if source_ref else [],
            "governance_warnings": warnings,
            "schema_version": EXPERIENCE_REVISION_PROPOSE_OUTPUT_SCHEMA_VERSION,
        }
        return self._result(json.dumps(payload, ensure_ascii=False, sort_keys=True), request)

    @staticmethod
    def _result(output_text: str, request: ModelRequest) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=output_text,
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload.get('fixture_id', 'missing')}",
            metadata={
                "fixture": True,
                "tools_enabled": False,
                "memory_enabled": False,
                "messaging_enabled": False,
                "nested_job_orchestration_enabled": False,
                "file_or_terminal_side_effects_enabled": False,
            },
        )


class FormalBusinessSkillMaterializer:
    def __init__(self, store: PersistenceStore, *, id_factory: Callable[[], str] = uuid7):
        self.store = store
        self.conn = store.conn
        self.id_factory = id_factory
        self.install_schema()

    def install_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def record_failed_run(
        self,
        *,
        job_id: str,
        attempt_id: str,
        frozen_payload: dict[str, Any],
        contract: FormalSkillContract,
        model_port: str,
        error: dict[str, Any],
    ) -> str:
        input_payload = dict(frozen_payload.get("input") or {})
        skill_run_id = self.id_factory()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO formal_business_skill_run(
                    skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
                    request_id, correlation_id, skill_hash, binding_name, binding_version,
                    binding_hash, model_route, model_port, input_hash, error_json
                )
                VALUES(?, ?, ?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_run_id,
                    job_id,
                    attempt_id,
                    contract.formal_skill_id,
                    contract.version,
                    str(input_payload.get("request_id", "unknown")),
                    str(input_payload.get("correlation_id", "unknown")),
                    contract.skill_hash,
                    contract.binding_name,
                    contract.binding_version,
                    contract.binding_hash,
                    contract.route_name,
                    model_port,
                    content_hash(input_payload, "formal_business_skill.input.v1"),
                    canonical_json(error),
                ),
            )
        return skill_run_id

    def materialize_success(
        self,
        *,
        job_id: str,
        attempt_id: str,
        frozen_payload: dict[str, Any],
        run_result: FormalSkillRunResult,
        contract: FormalSkillContract,
        model_port: str,
    ) -> tuple[str, str, str, str]:
        input_payload = frozen_payload["input"]
        input_hash = content_hash(input_payload, f"{contract.formal_skill_id}.input.v1")
        model_input_hash = content_hash(run_result.model_input_payload, f"{contract.formal_skill_id}.model_input.v1")
        output_schema_version = str(run_result.output_payload.get("schema_version") or f"{contract.formal_skill_id}.output.v1")
        output_hash = content_hash(run_result.output_payload, output_schema_version)
        skill_run_id = self.id_factory()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO formal_business_skill_run(
                    skill_run_id, job_id, attempt_id, status, formal_skill_id, skill_version,
                    request_id, correlation_id, skill_hash, binding_name, binding_version,
                    binding_hash, model_route, model_port, input_hash, model_input_hash,
                    output_hash, model_run_envelope_version_id
                )
                VALUES(?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_run_id,
                    job_id,
                    attempt_id,
                    contract.formal_skill_id,
                    contract.version,
                    input_payload["request_id"],
                    input_payload["correlation_id"],
                    contract.skill_hash,
                    contract.binding_name,
                    contract.binding_version,
                    contract.binding_hash,
                    contract.route_name,
                    model_port,
                    input_hash,
                    model_input_hash,
                    output_hash,
                    run_result.model_run_envelope_version_id,
                ),
            )
            root_id = self.store.create_root("formal_business_skill_result")
            payload = {
                "goal": goal_for_formal_skill(contract.formal_skill_id),
                "formal_skill_id": contract.formal_skill_id,
                "request_id": input_payload["request_id"],
                "correlation_id": input_payload["correlation_id"],
                "job_id": job_id,
                "skill_run_id": skill_run_id,
                "skill_version": contract.version,
                "input_hash": input_hash,
                "model_input_hash": model_input_hash,
                "output_hash": output_hash,
                "schema_version": FORMAL_SKILL_RESULT_SCHEMA_VERSION,
                "model_route": contract.route_name,
                "model_port": model_port,
                "output": run_result.output_payload,
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version=FORMAL_SKILL_RESULT_SCHEMA_VERSION,
                business_payload={
                    "formal_skill_id": contract.formal_skill_id,
                    "request_id": input_payload["request_id"],
                    "output_hash": output_hash,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self.store.record_audit(
                event_type="formal_business_skill.result.materialized",
                actor="formal_business_skill_materializer",
                object_kind="formal_business_skill_result",
                object_id=root_id,
                version_id=version_id,
                payload={"job_id": job_id, "skill_run_id": skill_run_id},
                correlation_id=input_payload["correlation_id"],
                causation_id=attempt_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="formal_business_skill.result.materialized",
                payload={
                    "formal_skill_id": contract.formal_skill_id,
                    "request_id": input_payload["request_id"],
                    "job_id": job_id,
                    "result_version_id": version_id,
                    "schema_version": FORMAL_SKILL_RESULT_SCHEMA_VERSION,
                },
                correlation_id=input_payload["correlation_id"],
                causation_id=skill_run_id,
            )
            self.conn.execute(
                """
                INSERT INTO formal_business_skill_result_index(
                    job_id, formal_skill_id, request_id, correlation_id, result_root_id,
                    result_version_id, skill_run_id, skill_version, input_hash,
                    model_input_hash, output_hash, schema_version, model_route, model_port
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    contract.formal_skill_id,
                    input_payload["request_id"],
                    input_payload["correlation_id"],
                    root_id,
                    version_id,
                    skill_run_id,
                    contract.version,
                    input_hash,
                    model_input_hash,
                    output_hash,
                    FORMAL_SKILL_RESULT_SCHEMA_VERSION,
                    contract.route_name,
                    model_port,
                ),
            )
        return root_id, version_id, skill_run_id, outbox_id


@dataclass(frozen=True)
class FormalSkillCreateResult:
    job_id: str
    status: str
    replayed: bool


@dataclass(frozen=True)
class FormalSkillStepResult:
    status: str
    job_id: str | None = None
    attempt_id: str | None = None
    result_version_id: str | None = None
    reason: str | None = None


class FormalBusinessSkillCoreAPI:
    def __init__(self, scheduler: Goal03Scheduler, contract: FormalSkillContract):
        self.scheduler = scheduler
        self.conn = scheduler.conn
        self.contract = contract

    def create_formal_skill_job(self, input_payload: dict[str, Any], *, max_attempts: int = 3) -> FormalSkillCreateResult:
        if input_payload.get("request_id") and not isinstance(input_payload["request_id"], str):
            raise FormalSkillValidationError("request_id must be string when provided")
        request_id = str(input_payload.get("request_id") or self.scheduler.id_factory())
        correlation_id = str(input_payload.get("correlation_id") or request_id)
        idempotency_key = f"{self.contract.formal_skill_id}:{request_id}"
        result = self.scheduler.enqueue_job(
            job_kind=FORMAL_SKILL_JOB_KIND,
            payload={
                "formal_skill_id": self.contract.formal_skill_id,
                "input": input_payload,
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            max_attempts=max_attempts,
        )
        return FormalSkillCreateResult(job_id=result.job_id, status=result.status, replayed=result.replayed)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.scheduler.get_job(job_id)
        return {key: row[key] for key in row.keys()}

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT i.*, v.payload_json
              FROM formal_business_skill_result_index i
              JOIN trace_version v ON v.version_id=i.result_version_id
             WHERE i.job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "job_id": row["job_id"],
            "formal_skill_id": row["formal_skill_id"],
            "request_id": row["request_id"],
            "correlation_id": row["correlation_id"],
            "result_version_id": row["result_version_id"],
            "skill_run_id": row["skill_run_id"],
            "input_hash": row["input_hash"],
            "model_input_hash": row["model_input_hash"],
            "output_hash": row["output_hash"],
            "schema_version": row["schema_version"],
            "model_route": row["model_route"],
            "model_port": row["model_port"],
            "output": payload["output"],
        }

    def list_outbox(self, *, topic: str = "formal_business_skill.result.materialized") -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT outbox_id, topic, payload_json, status, attempts, correlation_id, causation_id
              FROM outbox_message
             WHERE topic=?
             ORDER BY created_at, outbox_id
            """,
            (topic,),
        ).fetchall()
        return [
            {
                "outbox_id": row["outbox_id"],
                "topic": row["topic"],
                "payload": json.loads(row["payload_json"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
            }
            for row in rows
        ]


class FormalBusinessSkillWorker:
    def __init__(
        self,
        *,
        scheduler: Goal03Scheduler,
        adapter: FormalBusinessSkillAdapter,
        materializer: FormalBusinessSkillMaterializer,
        contract: FormalSkillContract,
        worker_id: str,
    ):
        self.scheduler = scheduler
        self.adapter = adapter
        self.materializer = materializer
        self.contract = contract
        self.worker_id = worker_id

    @property
    def model_port_name(self) -> str:
        route = self.adapter.gateway.routes.get(self.contract.route_name)
        return route.provider_name if route is not None else "unconfigured"

    def run_once(self) -> FormalSkillStepResult:
        try:
            claim = self.scheduler.claim_next(worker_id=self.worker_id, lease_seconds=60)
        except NoClaimableJob:
            return FormalSkillStepResult(status="idle")
        job = self.scheduler.get_job(claim.job_id)
        if job["job_kind"] != FORMAL_SKILL_JOB_KIND:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "unsupported_job_kind", "job_kind": job["job_kind"]},
                retry=False,
            )
            return FormalSkillStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason="unsupported_job_kind",
            )
        if claim.payload.get("formal_skill_id") != self.contract.formal_skill_id:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "unsupported_formal_skill_id"},
                retry=False,
            )
            return FormalSkillStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason="unsupported_formal_skill_id",
            )
        try:
            run_result = self.adapter.run(claim.payload["input"])
            _root_id, version_id, skill_run_id, outbox_id = self.materializer.materialize_success(
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                frozen_payload=claim.payload,
                run_result=run_result,
                contract=self.contract,
                model_port=self.model_port_name,
            )
            self.scheduler.complete(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                result={"result_version_id": version_id, "skill_run_id": skill_run_id, "outbox_id": outbox_id},
            )
            return FormalSkillStepResult(
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                result_version_id=version_id,
            )
        except (FormalSkillAdapterError, ModelGatewayError, SkillContractError) as exc:
            self.materializer.record_failed_run(
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                frozen_payload=claim.payload,
                contract=self.contract,
                model_port=self.model_port_name,
                error={"code": type(exc).__name__, "message": str(exc)},
            )
            next_status = self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": type(exc).__name__, "message": str(exc)},
                retry=True,
            )
            return FormalSkillStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason=next_status,
            )


@dataclass(frozen=True)
class FormalBusinessSkillHarness:
    store: PersistenceStore
    scheduler: Goal03Scheduler
    api: FormalBusinessSkillCoreAPI
    worker: FormalBusinessSkillWorker
    gateway: ModelGateway
    materializer: FormalBusinessSkillMaterializer
    adapter: FormalBusinessSkillAdapter
    contract: FormalSkillContract
    provider: ModelProvider

    def close(self) -> None:
        self.store.conn.close()


def goal_for_formal_skill(formal_skill_id: str) -> str:
    if formal_skill_id == "content_relation_judge":
        return CONTENT_RELATION_JUDGE_GOAL_ID
    if formal_skill_id == "source_to_topic":
        return SOURCE_TO_TOPIC_GOAL_ID
    return GOAL_ID


def make_content_classify_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: DeterministicContentClassifyModelPort | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml()
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicContentClassifyModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-content-classify",
            config_version=f"{GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_content_relation_judge_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(CONTENT_RELATION_JUDGE_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicContentRelationJudgeModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-content-relation-judge",
            config_version=f"{CONTENT_RELATION_JUDGE_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_source_to_topic_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicSourceToTopicModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-source-to-topic",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_sample_deep_analyze_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(SAMPLE_DEEP_ANALYZE_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicSampleDeepAnalyzeModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-sample-deep-analyze",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_tactic_extract_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(TACTIC_EXTRACT_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicTacticExtractModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-tactic-extract",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_research_evidence_extract_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(RESEARCH_EVIDENCE_EXTRACT_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicResearchEvidenceExtractModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-research-evidence-extract",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_production_research_plan_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicProductionResearchPlanModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-production-research-plan",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_content_plan_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(CONTENT_PLAN_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicContentPlanModelPort()
    hook_route = ModelRoute(
        route_name="business.creation_hook",
        provider_name=provider.provider_name,
        model_name="deterministic-content-plan-hook",
        config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
        config_hash=content_hash({"route": "business.creation_hook", "formal_skill_id": contract.formal_skill_id}),
        timeout_ms=1000,
    )
    outline_route = ModelRoute(
        route_name="business.creation_outline",
        provider_name=provider.provider_name,
        model_name="deterministic-content-plan-outline",
        config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
        config_hash=content_hash({"route": "business.creation_outline", "formal_skill_id": contract.formal_skill_id}),
        timeout_ms=1000,
    )
    gateway = ModelGateway(
        routes={hook_route.route_name: hook_route, outline_route.route_name: outline_route},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_script_generate_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(SCRIPT_GENERATE_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicScriptGenerateModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-script-generate",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_script_review_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    routes: dict[str, ModelRoute] | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(SCRIPT_REVIEW_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicScriptReviewModelPort()
    # 2026-07-13: real callers (run_script_review.py) need real routes bound
    # to a real provider, not the deterministic test routes this always built
    # before -- same override pattern as make_script_generate_harness's
    # single `route` param, just for all three subnodes at once.
    if routes is None:
        routes = {}
        for route_name in ("business.creation_review", "business.creation_polish", "business.ai_flavor_judge"):
            routes[route_name] = ModelRoute(
                route_name=route_name,
                provider_name=provider.provider_name,
                model_name=f"deterministic-{route_name.replace('.', '-')}",
                config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
                config_hash=content_hash({"route": route_name, "formal_skill_id": contract.formal_skill_id}),
                timeout_ms=1000,
            )
    provider_names = {route.provider_name for route in routes.values()}
    if len(provider_names) != 1:
        raise FormalSkillValidationError("script_review routes must all share the same provider_name")
    gateway = ModelGateway(
        routes=routes,
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_experiment_review_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(EXPERIMENT_REVIEW_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicExperimentReviewModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-experiment-review",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def make_experience_revision_propose_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
    provider: ModelProvider | None = None,
    route: ModelRoute | None = None,
) -> FormalBusinessSkillHarness:
    contract = FormalSkillContract.from_yaml(EXPERIENCE_REVISION_PROPOSE_CONTRACT_PATH)
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = FormalBusinessSkillMaterializer(store, id_factory=id_factory)
    provider = provider or DeterministicExperienceRevisionProposeModelPort()
    if route is None:
        route = ModelRoute(
            route_name=contract.route_name,
            provider_name=provider.provider_name,
            model_name="deterministic-experience-revision-propose",
            config_version=f"{SOURCE_TO_TOPIC_GOAL_ID}.test.v1",
            config_hash=content_hash({"route": contract.route_name, "formal_skill_id": contract.formal_skill_id}),
            timeout_ms=1000,
        )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={route.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
    worker = FormalBusinessSkillWorker(
        scheduler=scheduler,
        adapter=adapter,
        materializer=materializer,
        contract=contract,
        worker_id="formal-business-skill-worker",
    )
    api = FormalBusinessSkillCoreAPI(scheduler, contract)
    return FormalBusinessSkillHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        adapter=adapter,
        contract=contract,
        provider=provider,
    )


def sample_content_classify_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "content-classify-001",
        "correlation_id": "content-classify-correlation-001",
        "content_id": "content-001",
        "title": "社区电梯为什么总在早高峰拥堵",
        "body": "用通勤时间、楼层分布和维护周期解释一个常见生活现象。",
        "evidence_items": ["社区电梯早高峰拥堵", "通勤时间和楼层分布是解释依据"],
        "language_hint": "zh",
        "domain_hint": "fan_kepu_social_life",
    }
    payload.update(overrides)
    return payload


def sample_source_to_topic_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "source-to-topic-001",
        "correlation_id": "source-to-topic-correlation-001",
        "source_id": "source-001",
        "source_content": "社区电梯早高峰拥堵来自通勤集中、楼层分布不均和维保停梯。",
        "source_evidence_items": ["社区电梯早高峰拥堵", "通勤集中", "楼层分布不均", "维保停梯"],
        "domain_label": "fan_kepu_social_life",
        "relation_summary": "source is related_distinct to prior social-life evidence",
        "schema_version": "source_to_topic.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_sample_deep_analyze_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "sample-deep-analyze-001",
        "correlation_id": "sample-deep-analyze-correlation-001",
        "sample_id": "sample-001",
        "candidate_topic": "为什么小区电梯总在早高峰堵住",
        "transcript_excerpt": "每天早高峰电梯都挤不上，其实不是大家运气差，而是通勤时间、楼层分布和维保停梯一起叠加。",
        "metrics": {"like_count": 120000, "comment_count": 2400, "share_count": 900},
        "domain_label": "fan_kepu_social_life",
        "schema_version": "sample_deep_analyze.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_tactic_extract_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "tactic-extract-001",
        "correlation_id": "tactic-extract-correlation-001",
        "analysis_batch_id": "analysis-batch-001",
        "dna_note_refs": [
            "sample_deep_analysis:elevator-life-001",
            "sample_deep_analysis:elevator-life-002",
        ],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "tactic_extract.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_research_evidence_extract_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "research-evidence-001",
        "correlation_id": "research-evidence-correlation-001",
        "research_packet_id": "research-packet-001",
        "research_question": "为什么小区电梯总在早高峰堵住",
        "research_packet": "早高峰电梯拥堵与通勤集中、楼层分布有关。维保停梯会放大等待时间。",
        "source_refs": ["research-src-001", "research-src-002"],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "research_evidence_extract.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_production_research_plan_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "production-research-plan-001",
        "correlation_id": "production-research-plan-correlation-001",
        "candidate_topic": "为什么小区电梯总在早高峰堵住",
        "evidence_items": [
            {
                "claim": "早高峰电梯拥堵与通勤集中、楼层分布有关",
                "source_ref": "research-src-001",
                "supporting_text": "早高峰电梯拥堵与通勤集中、楼层分布有关",
            }
        ],
        "tactic_candidates": ["ordinary_life_problem_hidden_system"],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "production_research_plan.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_content_plan_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "content-plan-001",
        "correlation_id": "content-plan-correlation-001",
        "candidate_topic": "why apartment elevators jam in morning rush",
        "brief": (
            "Explain a familiar morning elevator wait as a system problem using commuting time, "
            "floor distribution, and maintenance windows."
        ),
        "evidence_items": [
            {
                "claim": "morning elevator crowding relates to synchronized commute time and uneven floor distribution",
                "source_ref": "research-src-001",
                "supporting_text": (
                    "morning elevator crowding relates to synchronized commute time and uneven floor distribution"
                ),
            }
        ],
        "tactic_candidates": ["ordinary_life_problem_hidden_system"],
        "style_examples": ["Start from a scene people recognize, then reveal the quiet system behind it."],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "content_plan.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_script_generate_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "script-generate-001",
        "correlation_id": "script-generate-correlation-001",
        "brief": (
            "Explain a familiar morning elevator wait as a system problem using commuting time, "
            "floor distribution, and maintenance windows."
        ),
        "selected_hook": "Open with the familiar wait, then reveal the hidden system.",
        "beats": [
            "Use the selected hook to start from a morning wait.",
            "Explain synchronized commute time and uneven floor distribution.",
            "Close with the practical system insight.",
        ],
        "research_summary": (
            "Morning elevator crowding can be explained through synchronized commute time and floor distribution."
        ),
        "evidence_items": [
            {
                "claim": "morning elevator crowding relates to synchronized commute time and uneven floor distribution",
                "source_ref": "research-src-001",
                "supporting_text": (
                    "morning elevator crowding relates to synchronized commute time and uneven floor distribution"
                ),
            }
        ],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "script_generate.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_script_review_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "script-review-001",
        "correlation_id": "script-review-correlation-001",
        "draft_text": (
            "Morning elevator waits are not just bad luck. The same commute window, uneven floor distribution, "
            "and maintenance timing turn a small building into a queueing system."
        ),
        "brief": (
            "Explain a familiar morning elevator wait as a system problem using commuting time, "
            "floor distribution, and maintenance windows."
        ),
        "evidence_items": [
            {
                "claim": "morning elevator crowding relates to synchronized commute time and uneven floor distribution",
                "source_ref": "research-src-001",
                "supporting_text": (
                    "morning elevator crowding relates to synchronized commute time and uneven floor distribution"
                ),
            }
        ],
        "human_reference_refs": ["human-reference-scene-to-system-001"],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "script_review.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_experiment_review_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "experiment-review-001",
        "correlation_id": "experiment-review-correlation-001",
        "experiment_id": "experiment-001",
        "experiment_design": "Compare two approved hook structures on the same synthetic content brief.",
        "execution_summary": "The scene-first hook had higher completion signal in the synthetic review packet.",
        "result_metrics": {"completion_signal": "higher", "sample_size": 3},
        "evidence_items": [
            {
                "claim": "scene-first hook improved completion signal",
                "source_ref": "experiment-src-001",
                "supporting_text": "scene-first hook had higher completion signal",
            }
        ],
        "tested_experience_refs": ["experience:scene_first_hook", "experience:abstract_hook"],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "experiment_review.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_experience_revision_propose_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "experience-revision-propose-001",
        "correlation_id": "experience-revision-propose-correlation-001",
        "frozen_experience_versions": [
            {
                "experience_ref": "experience:scene_first_hook",
                "experience_version": "1.0.0",
                "status": "published",
                "content_hash": "hash-scene-first-hook-v1",
            }
        ],
        "experiment_review": {
            "review_status": "supports_hypothesis",
            "supported_experience_refs": ["experience:scene_first_hook"],
            "refuted_experience_refs": [],
            "inconclusive_experience_refs": [],
        },
        "new_evidence_items": [
            {
                "claim": "scene-first hook improved completion signal",
                "source_ref": "experiment-src-001",
                "supporting_text": "scene-first hook had higher completion signal",
            }
        ],
        "tactic_candidates": ["ordinary_life_problem_hidden_system"],
        "domain_label": "fan_kepu_social_life",
        "schema_version": "experience_revision_propose.input.v1",
    }
    payload.update(overrides)
    return payload


def sample_content_relation_judge_input(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_id": "content-relation-001",
        "correlation_id": "content-relation-correlation-001",
        "left_content": "社区电梯早高峰拥堵来自通勤集中、楼层分布不均，还受维保停梯影响。",
        "right_content": "社区电梯早高峰拥堵来自通勤集中和楼层分布不均。",
        "left_evidence_items": ["通勤集中", "楼层分布不均", "维保停梯影响"],
        "right_evidence_items": ["通勤集中", "楼层分布不均"],
        "domain_context": "fan_kepu_social_life",
        "relation_scope": "content_object",
        "schema_version": "content_relation_judge.input.v1",
    }
    payload.update(overrides)
    return payload


def load_formal_mapping(path: Path = FORMAL_MAPPING_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def validate_formal_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    if mapping.get("schema_version") != "formal_skill_route_mapping.v1":
        raise FormalSkillValidationError("unexpected mapping schema_version")
    formal_skills = mapping.get("formal_skills") or []
    nodes = mapping.get("business_model_nodes") or []
    if len(formal_skills) != 12:
        raise FormalSkillValidationError("formal Skill list must contain 12 Skill entries from the goal")
    skill_ids = [str(item["formal_skill_id"]) for item in formal_skills]
    if len(skill_ids) != len(set(skill_ids)):
        raise FormalSkillValidationError("duplicate formal_skill_id in mapping")
    node_ids = [str(item["node_id"]) for item in nodes]
    registry_routes = sorted(node["logical_route"] for node in load_registry().get("nodes", []))
    if sorted(node_ids) != registry_routes:
        raise FormalSkillValidationError("business_model_nodes must map every existing business route exactly once")
    if len(node_ids) != len(set(node_ids)):
        raise FormalSkillValidationError("duplicate business node mapping")
    unknown_skills = sorted(set(item["mapped_formal_skill"] for item in nodes) - set(skill_ids))
    if unknown_skills:
        raise FormalSkillValidationError(f"business nodes map to unknown formal Skills: {unknown_skills}")
    first = next(item for item in formal_skills if item["formal_skill_id"] == "content_classify")
    relation = next(item for item in formal_skills if item["formal_skill_id"] == "content_relation_judge")
    if first["status"] != "active_formal_business_skill":
        raise FormalSkillValidationError("content_classify must be the active formal business Skill")
    if relation["status"] != "active_formal_business_skill":
        raise FormalSkillValidationError("content_relation_judge must be an active formal business Skill")
    if relation.get("allowed_model_nodes") != ["business.content_relation_judgement"]:
        raise FormalSkillValidationError("content_relation_judge must map to business.content_relation_judgement")
    return {
        "formal_skill_count": len(formal_skills),
        "business_node_count": len(nodes),
        "planned_skill_count": len([item for item in formal_skills if item["formal_skill_id"] != "content_classify"]),
        "active_formal_business_skill": "content_classify",
        "active_formal_business_skills": [
            item["formal_skill_id"] for item in formal_skills if item["status"] == "active_formal_business_skill"
        ],
        "unmapped_existing_business_nodes": [],
    }


def clean_room_status() -> dict[str, Any]:
    health = health_check(ROOT / "data" / "formal" / "clean_room_v0_6_2.sqlite3")
    return {"table_count": health["table_count"], "total_rows": sum(health["table_rows"].values())}


def load_live_gate_status(path: Path = LIVE_GATE_STATUS_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"status": "not_run", "path": str(path.relative_to(ROOT))}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "status": data.get("status"),
        "formal_skill_id": data.get("formal_skill_id"),
        "logical_route": data.get("logical_route"),
        "provider_type": data.get("provider_type"),
        "actual_model": data.get("actual_model"),
        "actual_call_count": data.get("actual_call_count"),
        "model_gateway_used": data.get("model_gateway_used"),
        "live_model_port_used": data.get("live_model_port_used"),
        "dry_run_fallback": data.get("dry_run_fallback"),
        "fake_port_fallback": data.get("fake_port_fallback"),
        "schema_validation": data.get("schema_validation"),
        "classification_status": data.get("classification_status"),
        "primary_label": data.get("primary_label"),
        "provider_request_id_status": data.get("provider_request_id_status"),
        "usage_status": data.get("usage_status"),
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "total_tokens": data.get("total_tokens"),
        "cost_status": data.get("cost_status"),
        "retry_count": data.get("retry_count"),
        "idempotent_replay": data.get("idempotent_replay"),
        "idempotent_replay_second_live_call": data.get("idempotent_replay_second_live_call"),
        "formal_result_count": data.get("formal_result_count"),
        "outbox_success_event_count": data.get("outbox_success_event_count"),
        "feishu_dispatched": data.get("feishu_dispatched"),
        "clean_room_formal_db": data.get("clean_room_formal_db"),
        "direct_cli_model_call_in_runtime_path": data.get("direct_cli_model_call_in_runtime_path"),
        "path": str(path.relative_to(ROOT)),
    }


def load_postgres_evidence(path: Path = POSTGRES_EVIDENCE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"gate_status": "not_run", "path": str(path.relative_to(ROOT))}
    parsed: dict[str, Any] = {"path": str(path.relative_to(ROOT))}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        value = value.strip()
        if value in {"True", "False"}:
            parsed[key] = value == "True"
        else:
            try:
                parsed[key] = int(value)
            except ValueError:
                parsed[key] = value
    return parsed


def run_verification() -> dict[str, Any]:
    business_contract_result = validate_content_classify_business_contract(load_content_classify_business_contract())
    mapping_result = validate_formal_mapping(load_formal_mapping())
    contract = FormalSkillContract.from_yaml()
    contract.validate_contract()
    fixtures = load_content_classify_fixtures()
    expected_fixture_ids = set(load_content_classify_business_contract()["test_cases"]["required_fixture_ids"])
    fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
    harness = make_content_classify_harness()
    try:
        create = harness.api.create_formal_skill_job(sample_content_classify_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(create.job_id)
        outbox = harness.api.list_outbox()
        provider_call_count = harness.provider.call_count
    finally:
        harness.close()
    direct = scan_direct_model_calls()
    clean_room = clean_room_status()
    live_gate = load_live_gate_status()
    postgres_gate = load_postgres_evidence()
    status = {
        "goal": GOAL_ID,
        "status": "COMPLETED",
        "business_contract": business_contract_result,
        "mapping": mapping_result,
        "first_formal_skill": {
            "formal_skill_id": contract.formal_skill_id,
            "version": contract.version,
            "allowed_model_nodes": list(contract.allowed_model_nodes),
            "route_name": contract.route_name,
            "schema_validation": "passed",
            "standalone_adapter": {
                "uses_model_gateway_only": True,
                "stores_state": False,
                "writes_files": False,
                "calls_other_skills": False,
            },
        },
        "fake_fixture_e2e": {
            "required_fixture_count": len(expected_fixture_ids),
            "implemented_fixture_count": len(fixture_ids),
            "missing_required_fixtures": sorted(expected_fixture_ids - fixture_ids),
            "job_status": step.status,
            "result_schema_version": result["output"]["schema_version"] if result else None,
            "result_primary_label": result["output"]["primary_label"] if result else None,
            "outbox_count": len(outbox),
            "provider_call_count": provider_call_count,
        },
        "direct_model_call_scan": direct,
        "clean_room_formal_db": clean_room,
        "live_provider_gate": live_gate,
        "postgres_e2e_gate": postgres_gate,
    }
    if step.status != "succeeded" or result is None or len(outbox) != 1:
        status["status"] = "FAILED"
    if status["fake_fixture_e2e"]["missing_required_fixtures"]:
        status["status"] = "FAILED"
    if direct["formal_production_direct_model_call_count"] != 0:
        status["status"] = "FAILED"
    if clean_room["total_rows"] != 0:
        status["status"] = "FAILED"
    if live_gate.get("status") not in {"COMPLETED", "not_run"}:
        status["status"] = "FAILED"
    if postgres_gate.get("gate_status") not in {"passed", "not_run"}:
        status["status"] = "FAILED"
    return status


def write_status(status: dict[str, Any], path: Path = STATUS_PATH) -> None:
    path.write_text(yaml.safe_dump(status, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_report(status: dict[str, Any], path: Path = REPORT_PATH) -> None:
    direct = status["direct_model_call_scan"]
    live = status["live_provider_gate"]
    postgres = status["postgres_e2e_gate"]
    lines = [
        f"# {GOAL_ID} Validation Report",
        "",
        f"status: `{status['status']}`",
        "",
        "## Business Contract",
        f"- skill_id: `{status['business_contract']['skill_id']}`",
        f"- skill_version: `{status['business_contract']['skill_version']}`",
        f"- source_document_count: `{status['business_contract']['source_document_count']}`",
        f"- missing_requirement_count: `{status['business_contract']['missing_requirement_count']}`",
        "",
        "## Formal Mapping",
        f"- formal_skill_count: `{status['mapping']['formal_skill_count']}`",
        f"- business_node_count: `{status['mapping']['business_node_count']}`",
        f"- active_formal_business_skill: `{status['mapping']['active_formal_business_skill']}`",
        f"- unmapped_existing_business_nodes: `{len(status['mapping']['unmapped_existing_business_nodes'])}`",
        "",
        "## First Formal Skill",
        f"- formal_skill_id: `{status['first_formal_skill']['formal_skill_id']}`",
        f"- version: `{status['first_formal_skill']['version']}`",
        f"- route_name: `{status['first_formal_skill']['route_name']}`",
        f"- allowed_model_nodes: `{', '.join(status['first_formal_skill']['allowed_model_nodes'])}`",
        f"- schema_validation: `{status['first_formal_skill']['schema_validation']}`",
        "- standalone_adapter: no state store, no file writes, no other Skill calls, ModelGateway only",
        "",
        "## Fake Fixture E2E",
        f"- required_fixture_count: `{status['fake_fixture_e2e']['required_fixture_count']}`",
        f"- implemented_fixture_count: `{status['fake_fixture_e2e']['implemented_fixture_count']}`",
        f"- missing_required_fixtures: `{len(status['fake_fixture_e2e']['missing_required_fixtures'])}`",
        f"- job_status: `{status['fake_fixture_e2e']['job_status']}`",
        f"- result_schema_version: `{status['fake_fixture_e2e']['result_schema_version']}`",
        f"- result_primary_label: `{status['fake_fixture_e2e']['result_primary_label']}`",
        f"- outbox_count: `{status['fake_fixture_e2e']['outbox_count']}`",
        f"- provider_call_count: `{status['fake_fixture_e2e']['provider_call_count']}`",
        "",
        "## Live Provider Gate",
        f"- status: `{live.get('status')}`",
        f"- formal_skill_id: `{live.get('formal_skill_id')}`",
        f"- logical_route: `{live.get('logical_route')}`",
        f"- provider_type: `{live.get('provider_type')}`",
        f"- actual_model: `{live.get('actual_model')}`",
        f"- actual_call_count: `{live.get('actual_call_count')}`",
        f"- model_gateway_used: `{live.get('model_gateway_used')}`",
        f"- live_model_port_used: `{live.get('live_model_port_used')}`",
        f"- dry_run_fallback: `{live.get('dry_run_fallback')}`",
        f"- fake_port_fallback: `{live.get('fake_port_fallback')}`",
        f"- schema_validation: `{live.get('schema_validation')}`",
        f"- classification_status: `{live.get('classification_status')}`",
        f"- primary_label: `{live.get('primary_label')}`",
        f"- usage_status: `{live.get('usage_status')}`",
        f"- prompt_tokens: `{live.get('prompt_tokens')}`",
        f"- completion_tokens: `{live.get('completion_tokens')}`",
        f"- total_tokens: `{live.get('total_tokens')}`",
        f"- cost_status: `{live.get('cost_status')}`",
        f"- retry_count: `{live.get('retry_count')}`",
        f"- idempotent_replay: `{live.get('idempotent_replay')}`",
        f"- idempotent_replay_second_live_call: `{live.get('idempotent_replay_second_live_call')}`",
        f"- formal_result_count: `{live.get('formal_result_count')}`",
        f"- outbox_success_event_count: `{live.get('outbox_success_event_count')}`",
        f"- feishu_dispatched: `{live.get('feishu_dispatched')}`",
        f"- report_path: `{live.get('path')}`",
        "",
        "## PostgreSQL E2E Gate",
        f"- gate_status: `{postgres.get('gate_status')}`",
        f"- postgres_version: `{postgres.get('postgres_version')}`",
        f"- isolation: `{postgres.get('isolation')}`",
        f"- schema_rounds: `{postgres.get('schema_rounds')}`",
        f"- initialized_formal_table_count: `{postgres.get('initialized_formal_table_count')}`",
        f"- initialized_formal_row_count: `{postgres.get('initialized_formal_row_count')}`",
        f"- two_session_concurrent_claim_worker_b_rows: `{postgres.get('two_session_concurrent_claim_worker_b_rows')}`",
        f"- disposable_database_dropped: `{postgres.get('disposable_database_dropped')}`",
        f"- container_removed: `{postgres.get('container_removed')}`",
        f"- secrets_recorded: `{postgres.get('secrets_recorded')}`",
        f"- external_llm_called: `{postgres.get('external_llm_called')}`",
        f"- feishu_called: `{postgres.get('feishu_called')}`",
        f"- legacy_data_imported: `{postgres.get('legacy_data_imported')}`",
        f"- evidence_path: `{postgres.get('path')}`",
        "",
        "## Direct Model Calls",
        f"- formal_production_direct_model_call_count: `{direct['formal_production_direct_model_call_count']}`",
        f"- legacy_direct_model_call_count: `{direct['legacy_direct_model_call_count']}`",
        "",
        "## Clean Room",
        f"- formal_table_count: `{status['clean_room_formal_db']['table_count']}`",
        f"- formal_total_rows: `{status['clean_room_formal_db']['total_rows']}`",
        "",
        "## Source Note",
        "- `target-architecture.md` and `rebuild-direction.md` were not present in the repo or memory folder; this matches earlier memory evidence and was not treated as a blocker.",
        "",
        "## Commands",
        "- `python scripts\\core\\model_gateway\\formal_skill_adapter.py`",
        "- `python scripts\\core\\model_gateway\\run_content_classify_live_gate.py`",
        "- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\core\\model_gateway\\run_content_classify_postgres_gate.ps1`",
        "- `python -m unittest tests.core.test_formal_skill_adapter tests.core.test_business_route_registry tests.validation.test_live_gates tests.core.test_runtime_vertical_slice`",
        "- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codex_pycache_goal_skill_adapter'; python -m py_compile scripts\\core\\model_gateway\\formal_skill_adapter.py scripts\\core\\model_gateway\\run_content_classify_live_gate.py tests\\core\\test_formal_skill_adapter.py`",
        "- `git diff --check`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_progress(status: dict[str, Any], path: Path = PROGRESS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {GOAL_ID} Progress",
        "",
        f"status: {status['status']}",
        "branch: implementation/goal-business-skill-content-classify-01-v0.6.2",
        "",
        "## Checkpoints",
        "- [x] Restore previous content_classify adapter baseline and create isolated goal branch.",
        "- [x] Extract CONTENT_CLASSIFY_BUSINESS_CONTRACT.yaml from current formal sources.",
        "- [x] Replace active content_classify package assets with formal schema, binding, prompt and fixtures.",
        "- [x] Implement formal content_classify semantics using ModelGateway only.",
        "- [x] Verify Runner, Materializer, Outbox, retry and idempotency using isolated fixtures.",
        "- [x] Verify minimal live Provider call through ModelGateway and live Model Port.",
        "- [x] Verify disposable PostgreSQL end-to-end gate.",
        "- [x] Verify no formal production direct model calls.",
        "- [x] Verify clean-room formal DB remains empty.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{GOAL_ID} verifier")
    parser.add_argument("--status-output", default=str(STATUS_PATH))
    parser.add_argument("--report-output", default=str(REPORT_PATH))
    parser.add_argument("--progress-output", default=str(PROGRESS_PATH))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    status = run_verification()
    write_status(status, Path(args.status_output))
    write_report(status, Path(args.report_output))
    write_progress(status, Path(args.progress_output))
    print(yaml.safe_dump(status, allow_unicode=True, sort_keys=False))
    return 0 if status["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
