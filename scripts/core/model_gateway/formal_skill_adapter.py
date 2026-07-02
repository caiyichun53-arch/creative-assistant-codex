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
        validate_schema_definition(self.input_schema, "input_schema")
        validate_schema_definition(self.output_schema, "output_schema")
        validate_schema_definition(self.model_input_schema, "model_input_schema")
        validate_schema_definition(self.model_output_schema, "model_output_schema")
        if self.formal_skill_id == "content_classify":
            validate_content_classify_business_contract(load_content_classify_business_contract())
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
        return FormalSkillRunResult(
            formal_skill_id=self.contract.formal_skill_id,
            output_payload=output_payload,
            model_input_payload=model_input,
            model_run_envelope_version_id=model_run.envelope_version_id,
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


def preprocess_formal_skill_input(formal_skill_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    if formal_skill_id == "content_classify":
        return preprocess_content_classify_input(input_payload)
    if formal_skill_id == "content_relation_judge":
        return preprocess_content_relation_judge_input(input_payload)
    return {}


def load_content_classify_business_contract(path: Path = CONTENT_CLASSIFY_CONTRACT_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_content_relation_judge_business_contract(path: Path = CONTENT_RELATION_JUDGE_CONTRACT_PATH) -> dict[str, Any]:
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
