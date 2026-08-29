from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRequest
from scripts.core.model_gateway.goal07_skill_runner import PortableSkillSpec
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.persistence.goal01_store import content_hash


ROOT = Path(__file__).resolve().parents[3]
SOURCE_TO_TOPIC_CONTRACT_PATH = ROOT / "SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml"
HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH = ROOT / "HOTSPOT_TO_OPPORTUNITY_BUSINESS_CONTRACT.yaml"
COMPETITOR_BREAKDOWN_SKILL_IDS = frozenset({"competitor_breakdown"})
COMPETITOR_BREAKDOWN_ANALYSIS_DELIMITER = "---ANALYSIS---"

_COMPETITOR_BREAKDOWN_BLOCK_PATTERN = re.compile(
    r"^---(QUESTION|SIGNAL|LEAD) ([A-Za-z0-9][A-Za-z0-9_-]*)---$"
)
COMPETITOR_BREAKDOWN_BOUNDARY_MARKER = "---BOUNDARY---"


class FormalSkillValidationError(RuntimeError):
    """Contract failure with optional raw model output for human review."""

    def __init__(
        self,
        message: str,
        *,
        raw_model_output: str | None = None,
        model_run_envelope_version_id: str | None = None,
        model_completion_receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_model_output = raw_model_output
        self.model_run_envelope_version_id = model_run_envelope_version_id
        self.model_completion_receipt = dict(model_completion_receipt or {})


_COMMENT_SOURCE_MARKERS = (
    "评论",
    "评论中",
    "评论里",
    "评论区",
    "评论出现",
    "评论观察",
    "评论事实线索",
    "评论问题",
    "评论补充",
    "评论信号",
    "大量评论",
    "多条评论",
    "这些评论",
    "有评论",
    "部分评论",
    "多位评论",
    "评论者",
    "网友评论",
    "观众评论",
)
_COMMENT_OPINION_MARKERS = (
    "认为",
    "觉得",
    "感觉",
    "喜欢",
    "不喜欢",
    "好听",
    "难听",
    "怀念",
    "共鸣",
    "质疑",
    "争议",
    "反对",
    "支持",
    "赞成",
    "吐槽",
    "抱怨",
    "感慨",
    "希望",
    "建议",
)
_COMMENT_UNCERTAINTY_MARKERS = (
    "待核实",
    "未经证实",
    "尚未核实",
    "有待确认",
    "尚待确认",
    "可能",
    "或许",
    "猜测",
    "传闻",
    "据称",
)
_COMMENT_REPORTING_VERBS = (
    "称",
    "声称",
    "说",
    "爆料",
    "透露",
    "补充",
    "指出",
    "提到",
    "提及",
)
_COMMENT_FACT_PREDICATE_MARKERS = (
    "此前",
    "之前",
    "曾经",
    "已经",
    "已",
    "有过",
    "参加过",
    "合作过",
    "创作过",
    "发布过",
    "收录",
    "获得",
    "担任",
    "来自",
    "发生",
    "存在",
)
_COMMENT_EFFECT_TARGET_MARKERS = (
    "传播",
    "完播",
    "留存",
    "互动",
    "效果",
    "受欢迎",
    "观众认可",
    "粉丝",
    "反馈",
    "情感共鸣",
    "用户认同",
    "内容成功",
    "内容效果",
    "观众",
    "参与",
    "点击",
    "播放",
    "转发",
    "流量",
    "爆款",
    "选题有效",
    "内容有效",
    "结构有效",
    "叙事有效",
    "策略有效",
    "讨论入口",
    "传播潜力",
)
_COMMENT_EFFECT_RELATION_MARKERS = (
    "因此",
    "所以",
    "从而",
    "导致",
    "造成",
    "提高",
    "提升",
    "增强",
    "促进",
    "引发",
    "带来",
    "产生",
    "形成",
    "唤起",
    "影响",
    "说明",
    "表明",
    "显示",
    "体现",
    "反映",
    "证明",
    "印证",
    "佐证",
    "验证",
    "揭示",
)
_AUTHOR_ATTRIBUTION_MARKERS = (
    "原文",
    "作者",
    "文案",
    "口播",
    "稿件",
)


@dataclass(frozen=True)
class FormalSkillRunResult:
    formal_skill_id: str
    output_payload: dict[str, Any]
    model_input_payload: dict[str, Any]
    model_run_envelope_version_id: str
    skill_hash: str
    binding_hash: str
    model_route: str
    raw_model_output: str


@dataclass(frozen=True)
class FormalSkillContract:
    formal_skill_id: str
    version: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_model_nodes: tuple[str, ...]
    route_name: str
    route_id: str
    binding_name: str
    binding_version: str
    input_map: dict[str, Any]
    output_map: dict[str, Any]
    model_input_schema: dict[str, Any]
    model_output_schema: dict[str, Any]
    model_response_format: dict[str, Any] | None
    prompt_template: str
    standalone_boundaries: dict[str, bool]

    @classmethod
    def from_yaml(cls, path: Path) -> "FormalSkillContract":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        binding = data["model_binding"]
        return cls(
            formal_skill_id=str(data.get("formal_skill_id") or data["skill_id"]),
            version=str(data.get("version") or data["skill_version"]),
            input_schema=dict(data["input_schema"]),
            output_schema=dict(data["output_schema"]),
            allowed_model_nodes=tuple(str(item) for item in data["allowed_model_nodes"]),
            route_name=str(binding["route_name"]),
            route_id=str(binding["route_id"]),
            binding_name=str(binding["binding_name"]),
            binding_version=str(binding["binding_version"]),
            input_map=dict(binding["input_map"]),
            output_map=dict(binding["output_map"]),
            model_input_schema=dict(binding["model_input_schema"]),
            model_output_schema=dict(binding["model_output_schema"]),
            model_response_format=dict(binding["model_response_format"]) if binding.get("model_response_format") else None,
            prompt_template=str(binding["prompt_template"]),
            standalone_boundaries={},
        )

    @classmethod
    def from_runtime_skill(cls, formal_skill_id: str) -> "FormalSkillContract":
        directory = ROOT / "runtime_skills" / formal_skill_id
        manifest = yaml.safe_load((directory / "skill.yaml").read_text(encoding="utf-8")) or {}
        binding = yaml.safe_load((directory / "binding.yaml").read_text(encoding="utf-8")) or {}
        input_schema = yaml.safe_load((directory / "input_schema.yaml").read_text(encoding="utf-8")) or {}
        output_schema = yaml.safe_load((directory / "output_schema.yaml").read_text(encoding="utf-8")) or {}
        if manifest.get("formal_skill_id") != formal_skill_id:
            raise FormalSkillValidationError("runtime Skill identity does not match its directory")
        return cls(
            formal_skill_id=formal_skill_id,
            version=str(manifest["skill_version"]),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            allowed_model_nodes=(str(manifest["route_name"]),),
            route_name=str(manifest["route_name"]),
            route_id=str(manifest["route_id"]),
            binding_name=str(binding["binding_name"]),
            binding_version=str(binding["binding_version"]),
            input_map=dict(binding["input_map"]),
            output_map=dict(binding["output_map"]),
            model_input_schema=dict(binding["model_input_schema"]),
            model_output_schema=dict(binding["model_output_schema"]),
            model_response_format=dict(binding["model_response_format"]) if binding.get("model_response_format") else None,
            prompt_template=(directory / "prompt.md").read_text(encoding="utf-8"),
            standalone_boundaries=dict(manifest.get("standalone_boundaries") or {}),
        )

    def validate_contract(self, *, require_model_route: bool = True) -> None:
        if not self.formal_skill_id or not self.version or self.route_name not in self.allowed_model_nodes:
            raise FormalSkillValidationError("invalid Skill contract")
        if require_model_route:
            try:
                ModelRouter.from_file().resolve_bound_route(self.route_id, route_name=self.route_name)
            except ModelRouterError as exc:
                raise FormalSkillValidationError("invalid model route") from exc
        for schema in (self.input_schema, self.output_schema, self.model_input_schema, self.model_output_schema):
            validate_schema_definition(schema)
        if self.model_response_format is not None:
            response_type = self.model_response_format.get("type")
            if response_type == "json_schema":
                schema = self.model_response_format.get("json_schema", {}).get("schema")
                if not isinstance(schema, dict):
                    raise FormalSkillValidationError("model response format needs a JSON schema")
                validate_schema_definition(schema)
            elif response_type != "json_object":
                raise FormalSkillValidationError("model response format is unsupported")
        if self.standalone_boundaries and any(self.standalone_boundaries.get(key) is not False for key in (
            "requires_database", "reads_files_at_runtime", "writes_files_at_runtime",
            "calls_other_skills", "calls_core_api", "accesses_external_url", "uses_chat_memory",
        )):
            raise FormalSkillValidationError("atomic Skill boundary is not standalone")

    @property
    def skill_hash(self) -> str:
        return content_hash({"id": self.formal_skill_id, "version": self.version, "input": self.input_schema, "output": self.output_schema, "route": self.route_id}, "formal_skill.contract.v1")

    @property
    def binding_hash(self) -> str:
        return content_hash({"name": self.binding_name, "version": self.binding_version, "input": self.input_map, "output": self.output_map, "response_format": self.model_response_format}, "formal_skill.binding.v1")

    def portable_skill(self) -> PortableSkillSpec:
        return PortableSkillSpec(self.formal_skill_id, self.version, self.route_name, self.prompt_template, tuple(self.model_input_schema["required"]), self.model_output_schema)


def prepare_external_skill_task(
    contract: FormalSkillContract,
    input_payload: dict[str, Any],
    *,
    constraints: dict[str, Any],
    business_context: dict[str, Any] | None = None,
    task_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare one model-independent handoff for an outside executor.

    The returned input is the already-bound Skill input.  The outside executor
    receives the formal Skill and its current rendered instructions, but no
    route, provider, fallback, or model choice.
    """
    contract.validate_contract(require_model_route=False)
    validate_payload(input_payload, contract.input_schema)
    prepared = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
    model_input = apply_binding(contract.input_map, input_payload, {}, prepared)
    validate_payload(model_input, contract.model_input_schema)
    rendered_prompt = contract.portable_skill().render_prompt(model_input)
    if contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
        rendered_prompt = _bind_competitor_breakdown_source(rendered_prompt, model_input)
    task: dict[str, Any] = {
        "task_type": task_type or contract.formal_skill_id,
        "skill": {
            "formal_skill_id": contract.formal_skill_id,
            "version": contract.version,
            "source_reference": f"runtime_skills/{contract.formal_skill_id}",
            "content": contract.prompt_template,
            "rendered_instructions": rendered_prompt,
            "input_schema": contract.model_input_schema,
            "output_schema": contract.output_schema,
            "skill_hash": contract.skill_hash,
            "binding": {
                "name": contract.binding_name,
                "version": contract.binding_version,
                "hash": contract.binding_hash,
            },
        },
        "input": model_input,
        "constraints": dict(constraints),
        "output_requirements": {
            "submission": "structured_fields",
            "schema": contract.output_schema,
            "formal_output_schema": contract.output_schema,
            "response_format": "structured_fields",
        },
    }
    if business_context:
        task["business_context"] = dict(business_context)
    return task, prepared


def validate_external_skill_output(
    contract: FormalSkillContract,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    *,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate fields submitted by an outside executor without parsing text."""
    contract.validate_contract(require_model_route=False)
    if not isinstance(output_payload, dict):
        raise FormalSkillValidationError("external intelligent result must be structured fields")
    validate_payload(input_payload, contract.input_schema)
    prepared = prepared if prepared is not None else preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
    validate_payload(output_payload, contract.output_schema)
    if contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
        validate_competitor_breakdown_question_expansion_output(
            input_payload, output_payload, validate_optional=False
        )
    if contract.formal_skill_id == "source_to_topic":
        validate_source_to_topic_output_semantics(input_payload, output_payload)
    del prepared
    return dict(output_payload)


class FormalBusinessSkillAdapter:
    def __init__(
        self,
        *,
        contract: FormalSkillContract,
        gateway: ModelGateway,
        data_identity: str | None = None,
    ):
        contract.validate_contract()
        self.contract, self.gateway = contract, gateway
        self.data_identity = data_identity

    def run(
        self,
        input_payload: dict[str, Any],
        *,
        request_metadata: dict[str, Any] | None = None,
    ) -> FormalSkillRunResult:
        from scripts.core.production.business_runtime_guard import (
            enforce_atomic_skill_runtime_guard,
        )

        enforce_atomic_skill_runtime_guard(
            entrypoint="formal_business_skill_adapter",
            operation=self.contract.formal_skill_id,
            data_identity=self.data_identity,
        )
        validate_payload(input_payload, self.contract.input_schema)
        prepared = preprocess_formal_skill_input(self.contract.formal_skill_id, input_payload)
        model_input = apply_binding(self.contract.input_map, input_payload, {}, prepared)
        validate_payload(model_input, self.contract.model_input_schema)
        if self.contract.route_name not in self.gateway.routes:
            raise FormalSkillValidationError("approved model route is unavailable")
        rendered_prompt = self.contract.portable_skill().render_prompt(model_input)
        if self.contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
            rendered_prompt = _bind_competitor_breakdown_source(rendered_prompt, model_input)
        model_run = self.gateway.complete(ModelRequest(
            route_name=self.contract.route_name,
            prompt=rendered_prompt,
            input_payload=model_input,
            correlation_id=input_payload["correlation_id"],
            skill_name=self.contract.formal_skill_id,
            skill_version=self.contract.version,
            skill_hash=self.contract.skill_hash,
            binding_name=self.contract.binding_name,
            binding_version=self.contract.binding_version,
            binding_hash=self.contract.binding_hash,
            response_format=self.contract.model_response_format,
            metadata={"formal_skill_id": self.contract.formal_skill_id, **(request_metadata or {})},
        ))
        try:
            if self.contract.formal_skill_id == "competitor_breakdown":
                model_output = parse_competitor_breakdown_delimited_output(model_run.output_text)
                model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
                model_output = repair_competitor_breakdown_comment_semantics(input_payload, model_output)
                validate_payload(
                    {key: value for key, value in model_output.items() if key != "analysis_text"},
                    self.contract.model_output_schema,
                )
            else:
                model_output = parse_model_json(model_run.output_text)
                model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
                validate_payload(model_output, self.contract.model_output_schema)
            output = apply_binding(self.contract.output_map, input_payload, model_output, prepared)
            if self.contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
                output = resolve_competitor_breakdown_evidence_ids(
                    output,
                    list(prepared.get("transcript_catalog") or []),
                    list(prepared.get("comment_catalog") or []),
                )
                if output.get("schema_version") == "competitor_breakdown.output.raw.v4":
                    output = {
                        key: output[key]
                        for key in (
                            "source_id", "source_content_type",
                            "analysis_text", "question_expansions", "schema_version",
                        )
                    }
            validate_payload(output, self.contract.output_schema)
            if self.contract.formal_skill_id == "competitor_breakdown":
                if output.get("schema_version") == "competitor_breakdown.output.raw.v4":
                    validate_competitor_breakdown_question_expansion_output(input_payload, output, validate_optional=False)
                else:
                    validate_competitor_breakdown_question_expansion_output(input_payload, output)
            if self.contract.formal_skill_id == "source_to_topic":
                validate_source_to_topic_output_semantics(input_payload, output)
        except FormalSkillValidationError as exc:
            raise FormalSkillValidationError(
                str(exc),
                raw_model_output=exc.raw_model_output or model_run.output_text,
                model_run_envelope_version_id=model_run.envelope_version_id,
                model_completion_receipt={
                    "finish_reason": str((model_run.envelope.metadata or {}).get("finish_reason") or "not_available"),
                    "completion_tokens": model_run.envelope.usage.completion_tokens,
                    "duration_ms": model_run.envelope.duration_ms,
                },
            ) from exc
        return FormalSkillRunResult(
            self.contract.formal_skill_id,
            output,
            model_input,
            model_run.envelope_version_id,
            self.contract.skill_hash,
            self.contract.binding_hash,
            self.contract.route_name,
            model_run.output_text,
        )

    def run_test_correction(
        self,
        input_payload: dict[str, Any],
        *,
        rejected_model_output: str,
        validation_errors: list[str],
        request_metadata: dict[str, Any] | None = None,
    ) -> FormalSkillRunResult:
        """Run one explicit, test-only correction of one rejected model answer.

        This method has no loop and is deliberately separate from ``run``: formal
        business execution keeps its one-call, final-outcome boundary.  The
        correction request receives the same source input, the rejected answer,
        and the program's exact validation errors as its complete fixed input.
        """
        if not isinstance(rejected_model_output, str) or not rejected_model_output.strip():
            raise FormalSkillValidationError("test correction needs the rejected model answer")
        errors = [str(item).strip() for item in validation_errors if str(item).strip()]
        if not errors:
            raise FormalSkillValidationError("test correction needs at least one validation error")
        from scripts.core.production.business_runtime_guard import (
            enforce_atomic_skill_runtime_guard,
        )

        enforce_atomic_skill_runtime_guard(
            entrypoint="formal_business_skill_adapter.test_correction",
            operation=self.contract.formal_skill_id,
            data_identity=self.data_identity,
        )
        validate_payload(input_payload, self.contract.input_schema)
        prepared = preprocess_formal_skill_input(self.contract.formal_skill_id, input_payload)
        model_input = apply_binding(self.contract.input_map, input_payload, {}, prepared)
        validate_payload(model_input, self.contract.model_input_schema)
        if self.contract.route_name not in self.gateway.routes:
            raise FormalSkillValidationError("approved model route is unavailable")
        correction_format = (
            "只输出 SOURCE_CONTENT_TYPE 机器头、固定区块标题和完整 analysis 正文。\n"
            if self.contract.formal_skill_id == "competitor_breakdown"
            else "只根据原始编号材料和下面的明确错误，提交一份完整修正后的 JSON。\n"
        )
        correction_prompt = (
            self.contract.portable_skill().render_prompt(model_input)
            + "\n\n【仅用于本次测试的单次修正】\n"
            + "上一份回答没有通过程序核查。不要重新猜测材料，也不要解释错误；"
            + correction_format
            + "核查错误：\n- " + "\n- ".join(errors)
            + "\n\n上一份被拒绝的回答（仅供修正，不是新的材料）：\n"
            + "--- previous_model_output ---\n" + rejected_model_output
            + "\n--- end_previous_model_output ---"
        )
        correction_input = {
            **model_input,
            "previous_model_output": rejected_model_output,
            "validation_errors": errors,
        }
        model_run = self.gateway.complete(ModelRequest(
            route_name=self.contract.route_name,
            prompt=correction_prompt,
            input_payload=correction_input,
            correlation_id=input_payload["correlation_id"],
            skill_name=self.contract.formal_skill_id,
            skill_version=self.contract.version,
            skill_hash=self.contract.skill_hash,
            binding_name=self.contract.binding_name,
            binding_version=self.contract.binding_version,
            binding_hash=self.contract.binding_hash,
            response_format=self.contract.model_response_format,
            metadata={
                "formal_skill_id": self.contract.formal_skill_id,
                "test_only_correction": True,
                "correction_attempt": 1,
                "validation_error_count": len(errors),
                **(request_metadata or {}),
            },
        ))
        try:
            if self.contract.formal_skill_id == "competitor_breakdown":
                model_output = parse_competitor_breakdown_delimited_output(model_run.output_text)
                model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
                model_output = repair_competitor_breakdown_comment_semantics(input_payload, model_output)
                validate_payload(
                    {key: value for key, value in model_output.items() if key != "analysis_text"},
                    self.contract.model_output_schema,
                )
            else:
                model_output = parse_model_json(model_run.output_text)
                model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
                validate_payload(model_output, self.contract.model_output_schema)
            output = apply_binding(self.contract.output_map, input_payload, model_output, prepared)
            if self.contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
                output = resolve_competitor_breakdown_evidence_ids(
                    output,
                    list(prepared.get("transcript_catalog") or []),
                    list(prepared.get("comment_catalog") or []),
                )
                if output.get("schema_version") == "competitor_breakdown.output.raw.v4":
                    output = {
                        key: output[key]
                        for key in (
                            "source_id", "source_content_type",
                            "analysis_text", "question_expansions", "schema_version",
                        )
                    }
            validate_payload(output, self.contract.output_schema)
            if self.contract.formal_skill_id == "competitor_breakdown":
                if output.get("schema_version") == "competitor_breakdown.output.raw.v4":
                    validate_competitor_breakdown_question_expansion_output(input_payload, output)
                else:
                    validate_competitor_breakdown_question_expansion_output(input_payload, output)
            if self.contract.formal_skill_id == "source_to_topic":
                validate_source_to_topic_output_semantics(input_payload, output)
        except FormalSkillValidationError as exc:
            raise FormalSkillValidationError(
                str(exc),
                raw_model_output=exc.raw_model_output or model_run.output_text,
                model_run_envelope_version_id=model_run.envelope_version_id,
                model_completion_receipt={
                    "finish_reason": str((model_run.envelope.metadata or {}).get("finish_reason") or "not_available"),
                    "completion_tokens": model_run.envelope.usage.completion_tokens,
                    "duration_ms": model_run.envelope.duration_ms,
                },
            ) from exc
        return FormalSkillRunResult(
            self.contract.formal_skill_id,
            output,
            correction_input,
            model_run.envelope_version_id,
            self.contract.skill_hash,
            self.contract.binding_hash,
            self.contract.route_name,
            model_run.output_text,
        )


def apply_binding(binding_map: dict[str, Any], input_payload: dict[str, Any], model_output: dict[str, Any], preprocessed: dict[str, Any] | None = None) -> dict[str, Any]:
    prepared, result = preprocessed or {}, {}
    for target, spec in binding_map.items():
        source = spec["source"]
        if source == "input":
            result[target] = input_payload[spec["key"]]
        elif source == "model_output":
            key = spec["key"]
            if key not in model_output and spec.get("required", True) is False:
                continue
            result[target] = model_output[key]
        elif source == "preprocessed":
            result[target] = prepared[spec["key"]]
        elif source == "literal":
            result[target] = spec["value"]
        else:
            raise FormalSkillValidationError("unsupported binding source")
    return result


def preprocess_formal_skill_input(formal_skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
        transcript_catalog = _build_numbered_transcript_catalog(str(payload.get("transcript") or ""))
        comment_catalog = _build_numbered_comment_catalog(list(payload.get("comments") or []))
        supplied_context = payload.get("domain_context")
        supplied_context = supplied_context if isinstance(supplied_context, dict) else {}
        domain_label = str(payload.get("domain_label") or supplied_context.get("label") or "generic").strip() or "generic"
        observed_content_types = supplied_context.get("observed_content_types")
        subject_labels = {
            "person": "人物", "work": "作品", "event": "事件", "concept": "概念",
            "case": "案例", "method": "方法", "collection": "合集",
        }
        expression_labels = {
            "story": "故事", "profile": "经历", "list": "盘点", "analysis": "解读",
            "explanation": "背景说明", "commentary": "观点评论", "event_response": "事件回应",
            "interview": "访谈",
        }
        def readable_type(value: Any) -> str:
            text = str(value or "").strip()
            if "/" not in text:
                return text
            subject, expression = (part.strip() for part in text.split("/", 1))
            if subject in subject_labels and expression in expression_labels:
                return f"{subject_labels[subject]}{expression_labels[expression]}"
            return text
        domain_context = dict(supplied_context)
        domain_context["label"] = domain_label
        domain_context["observed_content_types"] = [
            readable_type(item) for item in observed_content_types if readable_type(item)
        ] if isinstance(observed_content_types, list) else []
        prepared = {
            "transcript_catalog": transcript_catalog,
            "comment_catalog": comment_catalog,
            "numbered_transcript": _render_numbered_catalog(transcript_catalog),
            "numbered_comments": _render_numbered_catalog(comment_catalog),
            "domain_context": json.dumps(domain_context, ensure_ascii=False, separators=(",", ":")),
        }
        return prepared
    if formal_skill_id != "source_to_topic":
        return {}
    source = " ".join(str(payload.get("source_content", "")).split())
    return {"source_text": source, "relation_text": " ".join(str(payload.get("relation_summary", "")).split()), "source_length": len(source)}


def _bind_competitor_breakdown_source(prompt: str, model_input: dict[str, Any]) -> str:
    """Put the supplied transcript and comments into the prompt's input slots.

    The prompt owns the analysis rules.  The formal entry only injects the
    current, numbered source catalogs so the model can select stable evidence
    IDs without inventing or rewriting quotations.
    """
    transcript = str(model_input.get("numbered_transcript") or model_input.get("transcript") or "").strip()
    if not transcript:
        raise FormalSkillValidationError("爆款拆解必须有口播原文")
    comments = str(model_input.get("numbered_comments") or "").strip() or "（本条没有评论材料。）"
    transcript_placeholder = "把文案粘贴在这里。"
    comments_placeholder = "把评论粘贴在这里。"
    if transcript_placeholder not in prompt:
        raise FormalSkillValidationError("爆款拆解提示词缺少原文输入位置")
    if comments_placeholder not in prompt:
        raise FormalSkillValidationError("爆款拆解提示词缺少评论输入位置")
    rendered = prompt.replace(transcript_placeholder, transcript, 1)
    return rendered.replace(comments_placeholder, comments, 1)
def _build_numbered_transcript_catalog(transcript: str) -> list[dict[str, str]]:
    """Split one transcript into stable, complete, numbered paragraphs once."""
    pieces = [piece.strip() for piece in re.split(r"\n+", transcript) if piece.strip()]
    if len(pieces) == 1:
        pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?])", transcript) if piece.strip()]
    catalog: list[dict[str, str]] = []
    for piece in pieces:
        while len(piece) > 360:
            cut = max(piece.rfind(mark, 0, 360) for mark in "，、；：,;:")
            cut = cut + 1 if cut >= 80 else 360
            catalog.append({"id": f"P{len(catalog) + 1:03d}", "text": piece[:cut]})
            piece = piece[cut:].strip()
        if piece:
            catalog.append({"id": f"P{len(catalog) + 1:03d}", "text": piece})
    if not catalog and transcript.strip():
        catalog.append({"id": "P001", "text": transcript.strip()})
    return catalog


def _build_numbered_comment_catalog(comments: list[Any]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for item in comments:
        text = str(item.get("text") or "").strip() if isinstance(item, dict) else str(item).strip()
        if text:
            catalog.append({"id": f"C{len(catalog) + 1:03d}", "text": text})
    return catalog


def _render_numbered_catalog(catalog: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['id']}: {item['text']}" for item in catalog)


def resolve_competitor_breakdown_evidence_ids(
    output: dict[str, Any], transcript_catalog: list[dict[str, Any]], comment_catalog: list[dict[str, Any]]
) -> dict[str, Any]:
    """Restore system-owned quotations after validating model-selected IDs."""
    def lookup(catalog: list[dict[str, Any]]) -> dict[str, str]:
        return {
            str(item.get("id") or ""): str(item.get("text") or "")
            for item in catalog
            if isinstance(item, dict) and str(item.get("id") or "") and str(item.get("text") or "")
        }

    transcript_lookup, comment_lookup = lookup(transcript_catalog), lookup(comment_catalog)

    def restore(value: Any, catalog: dict[str, str]) -> Any:
        if not isinstance(value, list):
            return value
        if all(
            isinstance(item, dict)
            and str(item.get("id") or "") in catalog
            and str(item.get("text") or "") == catalog[str(item.get("id") or "")]
            for item in value
        ):
            return value
        # The model is asked to return source IDs.  Some otherwise valid JSON
        # responses wrap an ID as {"id": "P001"} or {"id": "P001",
        # "text": "P001"}.  This is an unambiguous transport-shape variant:
        # retain only a catalog ID that the model supplied, then restore its
        # system-owned quotation.  Never accept or preserve model-written text.
        source_ids: list[str] = []
        for item in value:
            if isinstance(item, str):
                candidate = item
            elif isinstance(item, dict):
                candidate = item.get("id") if isinstance(item.get("id"), str) else item.get("text")
            else:
                continue
            if isinstance(candidate, str) and candidate in catalog:
                source_ids.append(candidate)
        return [{"id": item, "text": catalog[item]} for item in source_ids]

    resolved = dict(output)
    if resolved.get("schema_version") in {
        "competitor_breakdown.output.raw.v4",
        "competitor_breakdown.output.raw.v5",
    }:
        # Raw question-expansion envelopes do not contain the retired
        # structural evidence fields.  Do not add them during transport
        # restoration; the raw envelope validator owns its own shape.
        return resolved
    question_expansions = resolved.get("question_expansions")
    if isinstance(question_expansions, list):
        resolved["question_expansions"] = [
            {
                "content_type": item.get("content_type"),
                "core_question": item.get("core_question"),
                "reason": item.get("reason"),
            }
            if isinstance(item, dict) else item
            for item in question_expansions
        ]
    resolved["content_type_evidence"] = restore(resolved.get("content_type_evidence"), transcript_lookup)
    if isinstance(resolved.get("structure_assessment"), dict):
        structure_assessment = dict(resolved["structure_assessment"])
        structure_assessment["source_evidence"] = restore(
            structure_assessment.get("source_evidence"), transcript_lookup
        )
        resolved["structure_assessment"] = structure_assessment
    if isinstance(resolved.get("structural_reading"), dict):
        structural_reading = dict(resolved["structural_reading"])
        structural_reading["source_evidence"] = restore(
            structural_reading.get("source_evidence"), transcript_lookup
        )
        resolved["structural_reading"] = structural_reading
    if isinstance(resolved.get("deep_reading"), dict):
        deep_reading = dict(resolved["deep_reading"])
        deep_reading["source_evidence"] = restore(
            deep_reading.get("source_evidence"), transcript_lookup
        )
        selected_lenses = deep_reading.get("selected_lenses")
        if isinstance(selected_lenses, list):
            deep_reading["selected_lenses"] = [
                {**item, "source_evidence": restore(item.get("source_evidence"), transcript_lookup)}
                if isinstance(item, dict) else item
                for item in selected_lenses
            ]
        resolved["deep_reading"] = deep_reading
    if isinstance(resolved.get("content_core"), dict):
        core = dict(resolved["content_core"])
        core["source_evidence"] = restore(core.get("source_evidence"), transcript_lookup)
        resolved["content_core"] = core
    if isinstance(resolved.get("structure_grasp"), dict):
        structure_grasp = dict(resolved["structure_grasp"])
        core = structure_grasp.get("core")
        if isinstance(core, dict):
            structure_grasp["core"] = {
                **core,
                "source_evidence": restore(core.get("source_evidence"), transcript_lookup),
            }
        for key in ("tensions", "highlights", "watching_pulls"):
            values = structure_grasp.get(key)
            if isinstance(values, list):
                structure_grasp[key] = [
                    {**item, "source_evidence": restore(item.get("source_evidence"), transcript_lookup)}
                    if isinstance(item, dict) else item
                    for item in values
                ]
        resolved["structure_grasp"] = structure_grasp
    for collection_key in ("spoken_progression", "recurring_evidence_patterns"):
        values = resolved.get(collection_key)
        if isinstance(values, list):
            resolved[collection_key] = [
                {
                    **item,
                    "source_evidence": restore(item.get("source_evidence"), transcript_lookup),
                }
                if isinstance(item, dict) and "source_evidence" in item
                else item
                for item in values
            ]
    reference_boundary = resolved.get("reference_boundary")
    if isinstance(reference_boundary, dict):
        resolved["reference_boundary"] = {
            key: [
                {**item, "source_evidence": restore(item.get("source_evidence"), transcript_lookup)}
                if isinstance(item, dict) else item
                for item in value
            ]
            if isinstance(value, list) else value
            for key, value in reference_boundary.items()
        }
    reactions = resolved.get("audience_reactions")
    if isinstance(reactions, list):
        resolved["audience_reactions"] = [
            {
                **item,
                "comment_evidence": restore(item.get("comment_evidence"), comment_lookup),
                "related_spoken_evidence": restore(item.get("related_spoken_evidence"), transcript_lookup),
            }
            if isinstance(item, dict) else item
            for item in reactions
        ]
    resolved = _restore_nested_competitor_evidence(resolved, transcript_lookup, comment_lookup)
    return resolved


def _restore_nested_competitor_evidence(
    value: Any, transcript_lookup: dict[str, str], comment_lookup: dict[str, str], *, field_name: str | None = None
) -> Any:
    """Restore evidence IDs inside the full analysis without trusting model quotations."""
    if isinstance(value, dict):
        return {
            key: (
                _restore_evidence_list(value[key], comment_lookup)
                if key == "comment_evidence"
                else _restore_evidence_list(value[key], transcript_lookup)
                if key in {"source_evidence", "related_spoken_evidence"}
                else _restore_nested_competitor_evidence(value[key], transcript_lookup, comment_lookup, field_name=key)
            )
            for key in value
        }
    if isinstance(value, list):
        return [
            _restore_nested_competitor_evidence(item, transcript_lookup, comment_lookup, field_name=field_name)
            for item in value
        ]
    return value


def _restore_evidence_list(value: Any, catalog: dict[str, str]) -> Any:
    if not isinstance(value, list):
        return value
    if all(
        isinstance(item, dict)
        and str(item.get("id") or "") in catalog
        and str(item.get("text") or "") == catalog[str(item.get("id") or "")]
        for item in value
    ):
        return value
    restored: list[dict[str, str]] = []
    for item in value:
        candidate = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
        if isinstance(candidate, str) and candidate in catalog:
            restored.append({"id": candidate, "text": catalog[candidate]})
    return restored


def _normalize_competitor_breakdown_structural_compact_output(model_output: dict[str, Any]) -> dict[str, Any]:
    """Keep the v13 model transport small; final presentation is program-owned."""
    normalized = dict(model_output)
    # These fields were deliberately retired from v13 because they turned a
    # source-bound breakdown into a second interpretation.  Drop only these
    # named legacy fields during the one-way transport cleanup; any other
    # unexpected field still fails the strict contract.
    for retired_key in ("watching_pulls", "usable_for", "not_usable_for"):
        normalized.pop(retired_key, None)

    def wrap_one_record(value: Any, width: int) -> Any:
        """Normalize an unambiguous one-record list delivery variant.

        The model may emit ``[[P001], statement]`` where the compact contract
        expects ``[[[P001], statement]]``.  This only restores the declared
        list container; it never creates wording, evidence, or a new record.
        """
        if not isinstance(value, list) or len(value) != width or not isinstance(value[0], list):
            return value
        if width == 2 and isinstance(value[1], str):
            return [value]
        if width >= 3 and all(isinstance(item, str) for item in value[1:]):
            return [value]
        if width == 3 and isinstance(value[1], list) and isinstance(value[2], str):
            return [value]
        return value

    for key in ("tensions", "highlights", "recurring"):
        normalized[key] = wrap_one_record(normalized.get(key), 2)
    normalized["progression"] = wrap_one_record(normalized.get("progression"), 4)
    normalized["reactions"] = wrap_one_record(normalized.get("reactions"), 3)
    normalized["content_subject_type"] = _canonical_competitor_label(
        normalized.get("content_subject_type"),
        {
            "人物": "person", "歌手": "person", "艺人": "person", "个人": "person", "人物故事": "person",
            "作品": "work", "歌曲": "work", "单曲": "work", "专辑": "work",
            "事件": "event", "历史事件": "event", "概念": "concept", "知识": "concept",
            "案例": "case", "方法": "method", "合集": "collection", "盘点": "collection", "列表": "collection",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear"},
        "unclear",
    )
    normalized["expression_form"] = _canonical_competitor_label(
        normalized.get("expression_form"),
        {
            "故事": "story", "故事讲述": "story", "叙事": "story",
            "人物履历": "profile", "人物介绍": "profile", "人物资料": "profile", "生平": "profile",
            "盘点": "list", "列表": "list", "排行": "list",
            "分析": "analysis", "赏析": "analysis", "解读": "analysis",
            "解释": "explanation", "背景解释": "explanation", "科普": "explanation",
            "评论": "commentary", "观点评论": "commentary",
            "热点回应": "event_response", "事件回应": "event_response",
            "访谈": "interview", "对话": "interview",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear"},
        "unclear",
    )
    assessment = normalized.get("structure_assessment")
    if isinstance(assessment, list) and len(assessment) == 3:
        normalized["structure_assessment"] = [
            assessment[0],
            _canonical_competitor_label(
                assessment[1],
                {
                    "简单": "simple", "普通": "simple", "常规": "simple", "无额外结构": "simple",
                    "有特点": "distinct", "独特": "distinct", "有独特结构": "distinct",
                    "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
                },
                {"simple", "distinct", "unclear"},
                "unclear",
            ),
            assessment[2],
        ]
    return normalized


def materialize_competitor_breakdown_structural_output(model_output: dict[str, Any]) -> dict[str, Any]:
    """Add program-owned sequence numbers to the named-field model transport."""
    progression = model_output.get("progression")
    recurring = model_output.get("recurring")
    assessment = model_output.get("structure_assessment")
    if isinstance(progression, list):
        for stage in progression:
            if not isinstance(stage, dict) or set(stage) != {
                "source_evidence", "spoken_action", "structural_role", "mainline_phase"
            }:
                raise FormalSkillValidationError(
                    "spoken breakdown progression needs evidence, action, structural change, and mainline phase"
                )
            phase = stage["mainline_phase"]
            if phase not in {"setup", "conflict", "response", "turn", "outcome", "closure"}:
                raise FormalSkillValidationError("spoken breakdown progression mainline phase is invalid")
    if not isinstance(assessment, dict) or set(assessment) != {"source_evidence", "level", "statement"}:
        raise FormalSkillValidationError("spoken breakdown structure assessment is invalid")
    return {
        "source_id": model_output.get("source_id"),
        "content_subject_type": model_output.get("content_subject_type"),
        "expression_form": model_output.get("expression_form"),
        "content_type_evidence": model_output.get("content_type_evidence"),
        "structure_assessment": assessment,
        "structure_grasp": {
            "core": model_output.get("core"),
            "tensions": model_output.get("tensions"),
            "highlights": model_output.get("highlights"),
        },
        "spoken_progression": [
            {
                "sequence": index,
                "source_evidence": stage["source_evidence"],
                "spoken_action": stage["spoken_action"],
                "structural_role": stage["structural_role"],
            }
            for index, stage in enumerate(progression or [], start=1)
        ] if isinstance(progression, list) else progression,
        "recurring_evidence_patterns": recurring,
        "audience_reactions": model_output.get("reactions"),
    }


def normalize_formal_skill_model_output(
    formal_skill_id: str, model_output: dict[str, Any]
) -> dict[str, Any]:
    """Apply only deterministic format conversion before a strict Skill check.

    This never creates a new category or changes the supplied source identity.
    It converts common representation differences (for example Chinese labels or
    a list of audience reactions) to the Skill's already-fixed vocabulary.
    """
    if formal_skill_id == "competitor_breakdown" and "analysis_text" in model_output:
        # The current runtime Skill returns one complete prose breakdown plus
        # the bounded expansion list. Do not run the retired structured-label
        # normalizer over this raw envelope; it would add legacy fields and
        # make the strict two-field model contract fail.
        normalized = dict(model_output)
        # Some configured providers still echo the retired structural
        # `content_type_evidence` transport field.  It is not part of the
        # raw v5 contract and is never mapped into formal output; discard only
        # this known compatibility field before the strict model check.
        normalized.pop("content_type_evidence", None)

        def readable_type(value: Any) -> Any:
            text = str(value or "").strip()
            if "/" not in text:
                return value
            subject, expression = (part.strip() for part in text.split("/", 1))
            subjects = {
                "person": "人物", "work": "作品", "event": "事件", "concept": "概念",
                "case": "案例", "method": "方法", "collection": "合集",
            }
            expressions = {
                "story": "故事", "profile": "经历", "list": "盘点", "analysis": "解读",
                "explanation": "背景说明", "commentary": "观点评论", "event_response": "事件回应",
                "interview": "访谈",
            }
            if subject in subjects and expression in expressions:
                return f"{subjects[subject]}{expressions[expression]}"
            return value

        normalized["source_content_type"] = readable_type(normalized.get("source_content_type"))
        expansions = normalized.get("question_expansions")
        if isinstance(expansions, list):
            normalized["question_expansions"] = [
                {
                    "content_type": readable_type(item.get("content_type")),
                    "core_question": item.get("core_question"),
                    "reason": item.get("reason"),
                }
                if isinstance(item, dict) else item
                for item in expansions
            ]
        return normalized
    if formal_skill_id == "competitor_breakdown":
        return _normalize_current_competitor_breakdown_output(model_output)
    if formal_skill_id == "experience_candidate_propose":
        return dict(model_output)
    if formal_skill_id == "research_plan":
        return dict(model_output)
    if formal_skill_id == "source_to_topic":
        # This Skill has its own exact topic-generation output contract.  Do
        # not pass it through the legacy competitor-breakdown normalizer below:
        # that normalizer adds unrelated structural fields and makes a valid
        # source-to-topic response fail the strict schema check.
        return dict(model_output)
    if formal_skill_id == "content_deep_research":
        # The binding adds the fixed node field after model validation.  Some
        # otherwise valid model responses echo that field, so remove only this
        # deterministic transport duplicate before the strict schema check.
        normalized = dict(model_output)
        if normalized.get("node") == "deep_research":
            normalized.pop("node")
        # Some providers follow the research document fields but omit the
        # required outer `document` wrapper.  This is a deterministic transport
        # normalization: it only moves the already returned fixed fields and
        # never creates, edits, or selects research content.
        if "document" not in normalized and "subject" in normalized:
            document_keys = {
                "subject", "timeline", "career_stages", "representative_works",
                "turning_points", "historical_context", "public_memory",
                "current_status", "source_map",
            }
            if document_keys.issubset(normalized):
                normalized = {
                    "document": {key: normalized[key] for key in document_keys},
                    "source_boundaries": normalized.get("source_boundaries", []),
                    "unresolved": normalized.get("unresolved", []),
                }
        return normalized
    normalized = dict(model_output)
    for collection_key in (
        "spoken_progression", "writing_methods", "information_and_argumentation", "audience_reactions",
    ):
        normalized[collection_key] = _normalize_object_list(normalized.get(collection_key))
    # A supplied quotation may be returned as one string or as a single-field
    # object.  These are delivery-shape differences only: convert them without
    # inventing, editing, or selecting any source text.  The semantic check
    # below still requires every resulting item to occur verbatim in transcript.
    normalized["content_type_evidence"] = _normalize_source_excerpt_list(
        normalized.get("content_type_evidence")
    )
    if isinstance(normalized.get("content_core"), dict):
        core = dict(normalized["content_core"])
        core["source_evidence"] = _normalize_source_excerpt_list(core.get("source_evidence"))
        normalized["content_core"] = core
    if isinstance(normalized.get("structural_reading"), dict):
        structural_reading = dict(normalized["structural_reading"])
        structural_reading["source_evidence"] = _normalize_source_excerpt_list(
            structural_reading.get("source_evidence")
        )
        normalized["structural_reading"] = structural_reading
    if isinstance(normalized.get("deep_reading"), dict):
        deep_reading = dict(normalized["deep_reading"])
        deep_reading["source_evidence"] = _normalize_source_excerpt_list(
            deep_reading.get("source_evidence")
        )
        selected_lenses = deep_reading.get("selected_lenses")
        if isinstance(selected_lenses, list):
            deep_reading["selected_lenses"] = [
                {
                    **item,
                    "source_evidence": _normalize_source_excerpt_list(item.get("source_evidence")),
                }
                if isinstance(item, dict) else item
                for item in selected_lenses
            ]
            for item in deep_reading["selected_lenses"]:
                if isinstance(item, dict) and isinstance(item.get("reading"), str):
                    item["reading"] = _strip_internal_source_identifier_notes(item["reading"])
        normalized["deep_reading"] = deep_reading
    for collection_key in (
        "spoken_progression", "writing_methods", "information_and_argumentation",
    ):
        collection = normalized.get(collection_key)
        if isinstance(collection, list):
            normalized[collection_key] = [
                {
                    **item,
                    "source_evidence": _normalize_source_excerpt_list(item.get("source_evidence")),
                }
                if isinstance(item, dict) else item
                for item in collection
            ]
    methods = normalized.get("writing_methods")
    if isinstance(methods, list):
        normalized["writing_methods"] = [
            {
                **item,
                "applicable_conditions": _normalize_text_list(item.get("applicable_conditions")),
                "cannot_infer": _normalize_text_list(item.get("cannot_infer")),
                "position_sequences": _normalize_integer_list(item.get("position_sequences")),
            }
            if isinstance(item, dict) else item
            for item in methods
        ]
    normalized["cannot_infer"] = _normalize_text_list(normalized.get("cannot_infer"))
    normalized["audience_responses"] = _flatten_model_text(
        normalized.get("audience_responses")
    )
    normalized["subject_description"] = _flatten_model_text(
        normalized.get("subject_description")
    )
    normalized["content_subject_type"] = _canonical_competitor_label(
        normalized.get("content_subject_type"),
        {
            "人物": "person", "歌手": "person", "艺人": "person", "个人": "person", "人物故事": "person",
            "作品": "work", "歌曲": "work", "单曲": "work", "专辑": "work",
            "事件": "event", "历史事件": "event", "概念": "concept", "知识": "concept",
            "案例": "case", "方法": "method", "合集": "collection", "盘点": "collection", "列表": "collection",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear"},
        "unclear",
    )
    normalized["expression_form"] = _canonical_competitor_label(
        normalized.get("expression_form"),
        {
            "故事": "story", "故事讲述": "story", "叙事": "story",
            "人物履历": "profile", "人物介绍": "profile", "人物资料": "profile", "生平": "profile",
            "盘点": "list", "列表": "list", "排行": "list",
            "分析": "analysis", "赏析": "analysis", "解读": "analysis",
            "解释": "explanation", "背景解释": "explanation", "科普": "explanation",
            "评论": "commentary", "观点评论": "commentary",
            "热点回应": "event_response", "事件回应": "event_response",
            "访谈": "interview", "对话": "interview",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear"},
        "unclear",
    )
    normalized["secondary_expression_form"] = _canonical_competitor_label(
        normalized.get("secondary_expression_form"),
        {
            "无": "none", "没有": "none", "无次要形式": "none", "不适用": "none",
            "故事": "story", "故事讲述": "story", "叙事": "story",
            "盘点": "list", "列表": "list", "排行": "list",
            "分析": "analysis", "赏析": "analysis", "解读": "analysis",
            "解释": "explanation", "背景解释": "explanation", "科普": "explanation",
            "评论": "commentary", "观点评论": "commentary",
            "热点回应": "event_response", "事件回应": "event_response",
            "访谈": "interview", "对话": "interview",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"story", "list", "analysis", "explanation", "commentary", "event_response", "interview", "none", "unclear"},
        "unclear",
    )
    # The current raw breakdown envelope includes question_expansions.  Only
    # retired legacy fields are removed; the expansion list stays source-bound
    # and is validated after evidence IDs are restored.
    for retired_key in (
        "secondary_expression_form", "audience_responses", "subject_description",
        "writing_methods", "information_and_argumentation",
    ):
        normalized.pop(retired_key, None)
    if formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
        progression = normalized.get("spoken_progression")
        if isinstance(progression, list) and all(isinstance(item, dict) for item in progression):
            field_aliases = {
                "结构性_role": "structural_role",
                "结构角色": "structural_role",
                "段落作用": "structural_role",
            }
            progression = [
                {
                    field_aliases.get(str(key).strip(), str(key).strip()): value
                    for key, value in item.items()
                }
                for item in progression
            ]
            def first_source_position(item: dict[str, Any], original_index: int) -> tuple[int, int]:
                evidence = item.get("source_evidence")
                positions = [
                    int(str(value)[1:])
                    for value in evidence or []
                    if isinstance(value, str) and re.fullmatch(r"P[0-9]{3}", value)
                ]
                return (min(positions), original_index) if positions else (10**9, original_index)

            ordered = sorted(
                enumerate(progression),
                key=lambda pair: first_source_position(pair[1], pair[0]),
            )
            normalized["spoken_progression"] = [
                {**item, "sequence": index}
                for index, (_, item) in enumerate(ordered, start=1)
            ]
        normalized = _strip_internal_source_ids_from_competitor_prose(normalized)
    return normalized


def _normalize_current_competitor_breakdown_output(model_output: dict[str, Any]) -> dict[str, Any]:
    """Normalize delivery shapes for the current full breakdown Skill only."""
    normalized = dict(model_output)

    # Mimo sometimes closes `full_analysis` after the first three sections and
    # emits the remaining named sections at the top level.  Those sections are
    # still unambiguous because their names are reserved by this Skill; fold
    # them back before the strict top-level schema check.
    full_analysis = dict(normalized.get("full_analysis") or {})
    full_analysis_sections = {
        "target_audience", "theme", "structure", "script_formula", "writing_methods",
        "tone_style", "opening_hook", "emotional_arc", "persona", "reusable_parts",
        "adaptation_suggestions", "final_summary",
    }
    for section in full_analysis_sections:
        if section in normalized and section != "full_analysis":
            full_analysis.setdefault(section, normalized.pop(section))
    if "cannot_infer" not in normalized and isinstance(full_analysis.get("cannot_infer"), list):
        normalized["cannot_infer"] = full_analysis.pop("cannot_infer")
    normalized["full_analysis"] = full_analysis

    def evidence_ids(value: Any) -> Any:
        if isinstance(value, str):
            identifiers = re.findall(r"[PC][0-9]{3}", value)
            return identifiers or [value]
        if isinstance(value, dict):
            candidate = value.get("id")
            return [candidate] if isinstance(candidate, str) else value
        if isinstance(value, list):
            result: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and isinstance(item.get("id"), str):
                    result.append(item["id"])
                else:
                    result.append(item)
            return result
        return value

    def nested(value: Any, field_name: str | None = None) -> Any:
        if isinstance(value, dict):
            return {
                key: evidence_ids(item)
                if key in {"source_evidence", "comment_evidence", "related_spoken_evidence"}
                else nested(item, key)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [nested(item, field_name) for item in value]
        return value

    for key in ("content_type_evidence", "cannot_infer"):
        normalized[key] = evidence_ids(normalized.get(key))
    for key in (
        "structure_assessment", "structure_grasp", "spoken_progression",
        "recurring_evidence_patterns", "audience_reactions", "full_analysis",
    ):
        normalized[key] = nested(normalized.get(key), key)

    # Keep the Skill's semantic shape stable when the model uses an object for
    # a single structure-grasp item or a plain string for a recurring pattern.
    grasp = normalized.get("structure_grasp")
    if isinstance(grasp, dict):
        core_value = grasp.get("core")
        if isinstance(core_value, dict):
            # Mimo may nest the peer sections under core; promote them without
            # changing their wording or evidence selections.
            for key in ("tensions", "highlights"):
                if key not in grasp and key in core_value:
                    grasp[key] = core_value.pop(key)
        for key in ("core", "tensions", "highlights"):
            value = grasp.get(key)
            if isinstance(value, dict):
                grasp[key] = [value]
            elif isinstance(value, str) and value.strip():
                grasp[key] = [{"statement": value.strip()}]
            elif value is None:
                grasp[key] = []
    recurring = normalized.get("recurring_evidence_patterns")
    if isinstance(recurring, list):
        normalized["recurring_evidence_patterns"] = [
            (
                {**item, "pattern": item.get("observed_pattern")}
                if isinstance(item, dict)
                and not item.get("pattern")
                and isinstance(item.get("observed_pattern"), str)
                and item.get("observed_pattern", "").strip()
                else {"pattern": item.strip()}
                if isinstance(item, str) and item.strip()
                else item
            )
            for item in recurring
        ]
    if isinstance(normalized.get("spoken_progression"), list):
        normalized["spoken_progression"] = [
            {**item, "sequence": item.get("sequence") or index}
            if isinstance(item, dict) else item
            for index, item in enumerate(normalized["spoken_progression"], start=1)
        ]
    normalized["content_subject_type"] = _canonical_competitor_label(
        normalized.get("content_subject_type"),
        {
            "人物": "person", "歌手": "person", "艺人": "person", "人物故事": "person",
            "作品": "work", "歌曲": "work", "单曲": "work", "专辑": "work",
            "事件": "event", "概念": "concept", "知识": "concept", "案例": "case",
            "方法": "method", "合集": "collection", "盘点": "collection", "列表": "collection",
            "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear"},
        "unclear",
    )
    normalized["expression_form"] = _canonical_competitor_label(
        normalized.get("expression_form"),
        {
            "故事": "story", "故事讲述": "story", "叙事": "story",
            "人物履历": "profile", "人物介绍": "profile", "人物资料": "profile", "生平": "profile",
            "盘点": "list", "列表": "list", "排行": "list", "分析": "analysis", "赏析": "analysis", "解读": "analysis",
            "解释": "explanation", "背景解释": "explanation", "科普": "explanation",
            "评论": "commentary", "观点评论": "commentary", "热点回应": "event_response", "事件回应": "event_response",
            "访谈": "interview", "对话": "interview", "混合": "mixed", "不清楚": "unclear", "未知": "unclear", "无法判断": "unclear",
        },
        {"story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear"},
        "unclear",
    )
    assessment = normalized.get("structure_assessment")
    if isinstance(assessment, list) and len(assessment) == 3:
        normalized["structure_assessment"] = {
            "source_evidence": evidence_ids(assessment[0]),
            "level": _canonical_competitor_label(
                assessment[1],
                {"简单": "simple", "普通": "simple", "有特点": "distinct", "独特": "distinct", "不清楚": "unclear"},
                {"simple", "distinct", "unclear"},
                "unclear",
            ),
            "statement": str(assessment[2] or "").strip(),
        }
    return _strip_internal_source_ids_from_competitor_prose(normalized)


def _normalize_source_excerpt_list(value: Any) -> Any:
    """Normalize an unambiguous quotation container, never quotation content."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in ("excerpt", "quote", "text", "source_evidence"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return [candidate]
            if isinstance(candidate, list) and all(isinstance(item, str) for item in candidate):
                return candidate
    return value


def _strip_internal_source_identifier_notes(value: str) -> str:
    """Remove only parenthetical P/C citation notes from a prose field.

    Formal evidence is kept in the dedicated source_evidence field.  A model
    sometimes repeats those private identifiers in a human-readable reading
    (for example, "...（P001、P008）").  Removing that transport annotation
    does not alter the reading or its source selection, and prevents private
    IDs from leaking into formal business prose.
    """
    without_note = re.sub(
        r"[（(]\s*(?:[PC]\d{3})(?:\s*[,，、]\s*[PC]\d{3})*\s*[）)]",
        "",
        value,
    )
    # Some replies put a source ID after a quotation mark inside the same
    # parenthesis (for example, “..."P016).  It is still only a private
    # transport marker, so remove the ID wherever it appears in this prose.
    return re.sub(r"(?<![A-Za-z0-9_])[PC][0-9]{3}(?![A-Za-z0-9_])", "", without_note).strip()


def _strip_internal_source_ids_from_competitor_prose(value: Any, *, field_name: str | None = None) -> Any:
    """Keep private source IDs in evidence fields, never in explanatory prose."""
    prose_fields = {
        "organizing_thread", "central_reading", "reading", "spoken_action",
        "structural_role", "statement", "setup", "pull_forward", "cannot_infer", "pattern",
    }
    if isinstance(value, dict):
        return {
            key: _strip_internal_source_ids_from_competitor_prose(item, field_name=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _strip_internal_source_ids_from_competitor_prose(item, field_name=field_name)
            for item in value
        ]
    if isinstance(value, str) and field_name in prose_fields:
        return _strip_internal_source_identifier_notes(value)
    return value


def _normalize_text_list(value: Any) -> Any:
    """Wrap one already-written text item without changing its meaning."""
    return [value] if isinstance(value, str) else value


def _normalize_object_list(value: Any) -> Any:
    return [value] if isinstance(value, dict) else value


def _normalize_integer_list(value: Any) -> Any:
    return [value] if isinstance(value, int) else value


def _flatten_model_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "；".join(part for part in (_flatten_model_text(item) for item in value) if part)
    if isinstance(value, dict):
        return "；".join(
            f"{str(key).strip()}：{text}"
            for key, item in value.items()
            if (text := _flatten_model_text(item))
        )
    return str(value).strip()


def _canonical_competitor_label(
    value: Any, aliases: dict[str, str], allowed: set[str], fallback: str
) -> str:
    text = str(value or "").strip().casefold()
    if text in allowed:
        return text
    return aliases.get(text, fallback)


def _first_balanced_json_object(output_text: str) -> str | None:
    """Return the first complete object after transport-only normalization.

    The model's original response is kept by the caller.  This helper changes
    only literal CR/LF characters that occurred inside a JSON string and
    ignores delivery text after the first balanced object.  It never inserts
    fields or repairs JSON structure.
    """
    start = output_text.find("{")
    if start < 0:
        return None
    normalized: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    index = start
    while index < len(output_text):
        character = output_text[index]
        if in_string:
            if escaped:
                normalized.append(character)
                escaped = False
            elif character == "\\":
                normalized.append(character)
                escaped = True
            elif character == '"':
                normalized.append(character)
                in_string = False
            elif character == "\r":
                normalized.append("\\n")
                if index + 1 < len(output_text) and output_text[index + 1] == "\n":
                    index += 1
            elif character == "\n":
                normalized.append("\\n")
            else:
                normalized.append(character)
        else:
            normalized.append(character)
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return "".join(normalized)
                if depth < 0:
                    return None
        index += 1
    return None


def parse_competitor_breakdown_delimited_output(output_text: str) -> dict[str, Any]:
    """Parse the current text-block competitor breakdown transport contract.

    Only short routing/identity values are carried on labelled header lines.
    Analysis, questions, signals, and lead text remain ordinary text blocks,
    so quotes, newlines, and Markdown never participate in JSON encoding. The
    parser accepts this one exact contract and has no legacy JSON or repair
    path.
    """
    if not isinstance(output_text, str):
        raise FormalSkillValidationError("competitor breakdown output must be text")

    lines = output_text.splitlines(keepends=True)
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        raise FormalSkillValidationError(
            "competitor breakdown output must start with SOURCE_CONTENT_TYPE",
            raw_model_output=output_text,
        )
    source_line = lines[index].rstrip("\r\n")
    source_prefix = "SOURCE_CONTENT_TYPE:"
    if not source_line.startswith(source_prefix):
        raise FormalSkillValidationError(
            "competitor breakdown output must start with SOURCE_CONTENT_TYPE",
            raw_model_output=output_text,
        )
    source_content_type = source_line[len(source_prefix):].strip()
    if not source_content_type:
        raise FormalSkillValidationError(
            "competitor breakdown source content type is empty",
            raw_model_output=output_text,
        )

    index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].rstrip("\r\n") != COMPETITOR_BREAKDOWN_ANALYSIS_DELIMITER:
        raise FormalSkillValidationError(
            "competitor breakdown output must contain ---ANALYSIS--- after SOURCE_CONTENT_TYPE",
            raw_model_output=output_text,
        )

    sections: list[tuple[str, str, list[str]]] = []
    current_kind = "analysis"
    current_id = ""
    current_lines: list[str] = []
    index += 1
    for line in lines[index:]:
        marker = line.rstrip("\r\n")
        if marker == COMPETITOR_BREAKDOWN_ANALYSIS_DELIMITER:
            raise FormalSkillValidationError(
                "competitor breakdown output must contain exactly one ---ANALYSIS--- delimiter",
                raw_model_output=output_text,
            )
        match = _COMPETITOR_BREAKDOWN_BLOCK_PATTERN.fullmatch(marker)
        if marker == COMPETITOR_BREAKDOWN_BOUNDARY_MARKER:
            sections.append((current_kind, current_id, current_lines))
            current_kind, current_id = "boundary", ""
            current_lines = []
            continue
        if marker.startswith("---"):
            if match is None:
                raise FormalSkillValidationError(
                    "competitor breakdown output contains an unknown block marker",
                    raw_model_output=output_text,
                )
            sections.append((current_kind, current_id, current_lines))
            current_kind, current_id = match.group(1).lower(), match.group(2)
            current_lines = []
            continue
        current_lines.append(line)
    sections.append((current_kind, current_id, current_lines))

    analysis_sections = [body for kind, _, body in sections if kind == "analysis"]
    if len(analysis_sections) != 1:
        raise FormalSkillValidationError(
            "competitor breakdown output must contain exactly one analysis block",
            raw_model_output=output_text,
        )
    analysis_text = "".join(analysis_sections[0])
    if not analysis_text.strip():
        raise FormalSkillValidationError(
            "competitor breakdown analysis is empty",
            raw_model_output=output_text,
        )

    def line_text(value: str) -> str:
        return value.rstrip("\r\n")

    def header(body: list[str], position: int, label: str) -> tuple[str, int]:
        while position < len(body) and not line_text(body[position]).strip():
            position += 1
        if position >= len(body):
            raise FormalSkillValidationError(
                f"competitor breakdown block is missing {label}",
                raw_model_output=output_text,
            )
        current = line_text(body[position])
        if not current.startswith(label):
            raise FormalSkillValidationError(
                f"competitor breakdown block must start with {label}",
                raw_model_output=output_text,
            )
        return current[len(label):].strip(), position + 1

    def block_text(body: list[str], start: int = 0) -> str:
        return "".join(body[start:]).strip()

    question_expansions: list[dict[str, str]] = []
    expansion_signals: list[dict[str, str]] = []
    typed_expansion_leads: list[dict[str, str]] = []
    boundary_observation: str | None = None
    seen_ids: set[tuple[str, str]] = set()
    for kind, block_id, body in sections:
        if kind == "analysis":
            continue
        if kind == "boundary":
            observation = block_text(body)
            if not observation:
                raise FormalSkillValidationError(
                    "competitor breakdown boundary observation is empty",
                    raw_model_output=output_text,
                )
            if boundary_observation is not None:
                raise FormalSkillValidationError(
                    "competitor breakdown output may contain only one boundary observation",
                    raw_model_output=output_text,
                )
            boundary_observation = observation
            continue
        identity = (kind, block_id)
        if identity in seen_ids:
            raise FormalSkillValidationError(
                f"competitor breakdown block id is duplicated: {block_id}",
                raw_model_output=output_text,
            )
        seen_ids.add(identity)
        if kind == "question":
            question = block_text(body)
            if not question:
                raise FormalSkillValidationError(
                    "competitor breakdown question block is empty",
                    raw_model_output=output_text,
                )
            question_expansions.append({
                # The block header supplies the only type context needed by
                # the legacy object.  No second TYPE/QUESTION/REASON label is
                # required in the model response; the one natural-language
                # body is retained for both legacy text slots.
                "content_type": source_content_type,
                "core_question": question,
                "reason": question,
            })
            continue
        if kind == "signal":
            signal_text = block_text(body)
            if not signal_text:
                raise FormalSkillValidationError(
                    "competitor breakdown signal block is empty",
                    raw_model_output=output_text,
                )
            expansion_signals.append({
                "signal_id": block_id,
                # These are historical object fields, not independent input
                # required from the model in the compact text contract.
                "signal_kind": "observation",
                "signal_text": signal_text,
                "source_anchor": "",
                "reason": "",
            })
            continue
        signal_id, position = header(body, 0, "SIGNAL:")
        canonical_id, position = header(body, position, "TYPE:")
        if not signal_id or not canonical_id:
            raise FormalSkillValidationError(
                "competitor breakdown lead block needs SIGNAL and TYPE values",
                raw_model_output=output_text,
            )
        lead_text = block_text(body, position)
        if not lead_text:
            raise FormalSkillValidationError(
                "competitor breakdown lead block is empty",
                raw_model_output=output_text,
            )
        typed_expansion_leads.append({
            "signal_id": signal_id,
            "canonical_id": canonical_id,
            # The raw contract has one natural-language lead body.  The
            # established downstream object still exposes core_question and
            # reason, so retain that body in both slots without asking the
            # model to repeat it under separate labels.
            "core_question": lead_text,
            "reason": lead_text,
        })

    result: dict[str, Any] = {
        "source_content_type": source_content_type,
        "analysis_text": analysis_text,
    }
    if question_expansions:
        result["question_expansions"] = question_expansions
    if expansion_signals:
        result["expansion_signals"] = expansion_signals
    if typed_expansion_leads:
        result["typed_expansion_leads"] = typed_expansion_leads
    if boundary_observation is not None:
        result["boundary_observation"] = boundary_observation
    return result


_JSON_CODE_FENCE_PATTERN = re.compile(
    r"```(?P<language>json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    flags=re.IGNORECASE | re.DOTALL,
)


def _single_json_code_fence_body(text: str) -> str | None:
    match = _JSON_CODE_FENCE_PATTERN.fullmatch(text)
    if match is None:
        return None
    return match.group("body").strip()


def parse_model_json(
    output_text: str, *, normalize_transport: bool = False
) -> dict[str, Any]:
    text = output_text.strip()
    candidates = [text]
    # Some OpenAI-compatible providers wrap an otherwise valid JSON object in a
    # complete markdown code fence.  This is a delivery-format difference, not
    # a business instruction.  Accept only that exact wrapper; prose before or
    # after JSON remains invalid and cannot enter a formal workflow.
    fenced_body = _single_json_code_fence_body(text)
    if fenced_body is not None:
        candidates.append(fenced_body)
    if normalize_transport:
        normalized_object = _first_balanced_json_object(output_text)
        if normalized_object and normalized_object not in candidates:
            candidates.append(normalized_object)
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(value, dict):
            raise FormalSkillValidationError("model output must be an object")
        return value
    raise FormalSkillValidationError(
        "model output is not JSON "
        f"(output_length={len(text)}, complete_code_fence={text.startswith('```') and text.endswith('```')})",
        raw_model_output=text,
    ) from last_error


def validate_schema_definition(schema: dict[str, Any]) -> None:
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict) or not isinstance(schema.get("required"), list):
        raise FormalSkillValidationError("invalid object schema")


def validate_payload(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise FormalSkillValidationError("payload must be an object")
    properties, required = schema["properties"], set(schema["required"])
    if required - payload.keys():
        raise FormalSkillValidationError("payload is missing required fields")
    if schema.get("additional_properties") is False and payload.keys() - properties.keys():
        unexpected = ", ".join(sorted(str(key) for key in payload.keys() - properties.keys()))
        raise FormalSkillValidationError(f"payload has unexpected fields: {unexpected}")
    for key, value in payload.items():
        if key in properties:
            spec = properties[key]
            spec = {"type": spec} if isinstance(spec, str) else spec
            if value is None and spec.get("nullable") is True:
                continue
            if spec.get("type") == "string" and not isinstance(value, str):
                raise FormalSkillValidationError(f"{key} must be a string")
            if spec.get("type") == "array" and not isinstance(value, list):
                raise FormalSkillValidationError(f"{key} must be an array")
            if spec.get("type") == "object" and not isinstance(value, dict):
                raise FormalSkillValidationError(f"{key} must be an object")
            if "enum" in spec and value not in spec["enum"]:
                raise FormalSkillValidationError(f"{key} is not an allowed value")


_ABSTRACT_DELIVERY_WORDS = (
    "给", "为", "用户", "观众", "带来", "提供", "引发", "满足", "帮助", "让",
    "很有", "有", "新的", "思考", "共鸣", "好奇", "视角", "理解", "讨论", "价值",
    "吸引力", "和", "及", "并", "与",
)
_ABSTRACT_DELIVERY_PATTERNS = (
    re.compile(r"^(?:分析|解读|探讨|研究|帮助理解)(?:一下|相关内容|这个问题|这件事)?$"),
)


def _is_abstract_only_delivery(value: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、；;：:]", "", str(value or ""))
    if not compact:
        return False
    if any(pattern.fullmatch(compact) for pattern in _ABSTRACT_DELIVERY_PATTERNS):
        return True
    residue = compact
    for word in sorted(_ABSTRACT_DELIVERY_WORDS, key=len, reverse=True):
        residue = residue.replace(word, "")
    return not residue


def validate_source_to_topic_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    supplied = [str(item) for item in input_payload["source_evidence_items"]]
    normalized_evidence: list[str] = []
    for item in output_payload["supporting_evidence"]:
        evidence = str(item).strip()
        if evidence in supplied:
            normalized_evidence.append(evidence)
            continue
        containing = [source for source in supplied if len(evidence) >= 8 and evidence in source]
        if len(containing) != 1:
            raise FormalSkillValidationError("candidate evidence must come from supplied source material")
        normalized_evidence.append(containing[0])
    output_payload["supporting_evidence"] = list(dict.fromkeys(normalized_evidence))
    if output_payload["topic_status"] == "no_result":
        return
    if not all(output_payload[key] for key in ("candidate_topic", "topic_angle", "core_question")):
        raise FormalSkillValidationError("candidate requires a topic, angle and core question")
    delivery = output_payload.get("delivery_contract")
    if isinstance(delivery, dict) and _is_abstract_only_delivery(str(delivery.get("user_gets") or "")):
        raise FormalSkillValidationError(
            "candidate delivery_contract.user_gets must contain concrete content, not abstract value language"
        )


def _semantic_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in str(text or "").splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
        if not cleaned:
            continue
        for sentence in re.split(r"(?<=[。！？；])\s*", cleaned):
            sentence = sentence.strip()
            if sentence:
                sentences.append(sentence)
    return sentences


def _comment_section_text(analysis_text: str) -> str:
    comment_section = str(analysis_text or "")
    if "五、评论信号" in comment_section:
        comment_section = comment_section.split("五、评论信号", 1)[1]
        if "六、" in comment_section:
            comment_section = comment_section.split("六、", 1)[0]
    return comment_section


def _has_comment_source(sentence: str) -> bool:
    return any(marker in sentence for marker in _COMMENT_SOURCE_MARKERS)


def _has_uncertainty_marker(sentence: str) -> bool:
    return any(marker in sentence for marker in _COMMENT_UNCERTAINTY_MARKERS)


def _has_comment_effect_claim(sentence: str) -> bool:
    """Detect a source-to-effect inference, not individual sensitive words."""
    if not _has_comment_source(sentence):
        return False
    if not any(marker in sentence for marker in _COMMENT_EFFECT_RELATION_MARKERS):
        return False
    return any(marker in sentence for marker in _COMMENT_EFFECT_TARGET_MARKERS)


def _has_unmarked_comment_fact_claim(sentence: str) -> bool:
    """Detect a reported factual assertion that lacks a pending-verification marker."""
    if not _has_comment_source(sentence) or _has_uncertainty_marker(sentence):
        return False
    if "事实" in sentence and any(marker in sentence for marker in ("确认", "证实", "定论")):
        return True
    reporting_positions = [sentence.find(verb) for verb in _COMMENT_REPORTING_VERBS if verb in sentence]
    if not reporting_positions:
        return False
    tail = sentence[min(position for position in reporting_positions) :]
    if any(marker in sentence for marker in _COMMENT_OPINION_MARKERS) and not any(
        marker in tail for marker in _COMMENT_FACT_PREDICATE_MARKERS
    ):
        return False
    return any(marker in tail for marker in _COMMENT_FACT_PREDICATE_MARKERS) or bool(
        re.search(r"(?:说|称|指出|补充|透露|爆料)[^。！？]{0,30}(?:是|为|有|被|参加|合作|创作|发布|发生)", tail)
    )


def _normalise_claim_text(text: str) -> str:
    return re.sub(r"[\s，。！？、：:；;“”‘’\"'（）()\[\]{}]", "", str(text or "")).casefold()


def _comment_semantic_sentences(comment_section: str) -> list[str]:
    """Keep a source label attached to the sentences in the same comment item."""
    sentences: list[str] = []
    lines = str(comment_section or "").splitlines()
    section_has_source = any(_has_comment_source(line) for line in lines)
    for line in lines:
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
        if not cleaned:
            continue
        line_has_source = section_has_source or _has_comment_source(cleaned)
        for sentence in re.split(r"(?<=[。！？；])\s*", cleaned):
            sentence = sentence.strip()
            if not sentence:
                continue
            if line_has_source and not _has_comment_source(sentence):
                sentence = "评论中" + sentence
            sentences.append(sentence)
    return sentences


def _extract_attributed_author_claims(transcript: str) -> list[str]:
    claims: list[str] = []
    for sentence in _semantic_sentences(transcript):
        if not any(marker in sentence for marker in _AUTHOR_ATTRIBUTION_MARKERS):
            continue
        colon = re.search(r"[:：]", sentence)
        if colon:
            claim = sentence[colon.end() :]
        else:
            match = re.search(r"(?:说|称|认为|评价|判断|主张|表示|写道)", sentence)
            claim = sentence[match.end() :] if match else ""
        normalised = _normalise_claim_text(claim)
        if len(normalised) >= 6:
            claims.append(normalised)
    return claims


def _validate_comment_semantics(input_payload: dict[str, Any], analysis_text: str) -> None:
    """Keep comment observations, fact leads and analysis claims separate."""
    transcript_claims = _extract_attributed_author_claims(str(input_payload.get("transcript") or ""))
    comment_section = _comment_section_text(analysis_text)
    comment_sentences = _comment_semantic_sentences(comment_section)

    for sentence in comment_sentences:
        if _has_comment_effect_claim(sentence):
            raise FormalSkillValidationError(
                "评论观察被升级为内容效果或因果判断；需保留评论来源和实际回应，不得推出传播、互动或选题效果"
            )
        if _has_unmarked_comment_fact_claim(sentence):
            raise FormalSkillValidationError(
                "评论新增事实没有标记为待核实线索，不能把评论说法直接写成已确认事实"
            )

    if transcript_claims:
        comment_boundary = _comment_section_text(analysis_text)
        non_comment_text = str(analysis_text or "").replace(comment_boundary, "", 1)
        attributed_markers = _AUTHOR_ATTRIBUTION_MARKERS + ("原文认为", "作者认为", "文案认为", "口播认为")
        for sentence in _semantic_sentences(non_comment_text):
            normalised = _normalise_claim_text(sentence)
            if not normalised or any(marker in sentence for marker in attributed_markers):
                continue
            if any(claim in normalised for claim in transcript_claims):
                raise FormalSkillValidationError(
                    "作者观点被改写成未标明来源的客观事实；需要保留原文/作者的观点身份"
                )


def repair_competitor_breakdown_comment_semantics(
    input_payload: dict[str, Any], model_output: dict[str, Any]
) -> dict[str, Any]:
    """Repair only isolated comment claims before the strict final validation.

    The raw answer is not accepted as-is when it crosses a semantic boundary.
    A local repair removes only the unsupported inference or marks only the
    reported fact as pending verification, preserving the other observations.
    The strict validator still rejects an unrepairable payload.
    """
    if not isinstance(model_output, dict) or not isinstance(model_output.get("analysis_text"), str):
        return model_output
    analysis_text = str(model_output["analysis_text"])
    comment_section = _comment_section_text(analysis_text)
    if not comment_section.strip():
        return model_output

    def repair_sentence(sentence: str, *, line_has_source: bool) -> str:
        detection_sentence = sentence
        if line_has_source and not _has_comment_source(detection_sentence):
            detection_sentence = "评论中" + detection_sentence
        if _has_comment_effect_claim(detection_sentence):
            relation_positions = [
                (detection_sentence.find(marker), marker)
                for marker in _COMMENT_EFFECT_RELATION_MARKERS
                if detection_sentence.find(marker) >= 0
            ]
            if relation_positions:
                position = min(relation_positions, key=lambda item: item[0])[0]
                if line_has_source and not _has_comment_source(sentence):
                    position = max(0, position - len("评论中"))
                retained = sentence[:position].rstrip("，,：:；; ")
                incomplete_endings = (
                    "这",
                    "这些",
                    "这可能",
                    "可能",
                    "同时",
                    "而",
                    "并且",
                    "以及",
                    "评论信号",
                    "模型结构分析",
                )
                if retained.endswith(incomplete_endings):
                    retained = re.sub(r"(?:这可能|这些|这|可能|同时|而|并且|以及|评论信号|模型结构分析)$", "", retained)
                    retained = retained.rstrip("，,：:；; ")
                sentence = "" if not retained else retained + "。"
                detection_sentence = sentence
        if sentence:
            check_sentence = detection_sentence if detection_sentence else sentence
            if _has_unmarked_comment_fact_claim(check_sentence):
                sentence = sentence.rstrip("。！？； ") + "（该说法待核实）。"
        return sentence

    section_has_source = _has_comment_source(comment_section)

    def repair_text(value: str, *, inherited_source: bool = False) -> str:
        repaired_parts: list[str] = []
        source_context = inherited_source
        for part in re.split(r"(?<=[。！？；])\s*", value):
            if not part.strip():
                continue
            direct_source = _has_comment_source(part)
            contextual_source = direct_source or source_context
            repaired_parts.append(repair_sentence(part, line_has_source=contextual_source))
            source_context = contextual_source
        repaired_value = "".join(part for part in repaired_parts if part).rstrip()
        if repaired_value.endswith(("，", ",")):
            repaired_value = repaired_value[:-1] + "。"
        return repaired_value

    repaired_comment_lines: list[str] = []
    for line in comment_section.splitlines():
        repaired_comment_lines.append(repair_text(line, inherited_source=section_has_source))
    repaired_comment = "\n".join(repaired_comment_lines)
    start = analysis_text.find("五、评论信号")
    end = analysis_text.find("六、", start + len("五、评论信号")) if start >= 0 else -1
    if start >= 0 and end >= 0:
        analysis_text = analysis_text[: start + len("五、评论信号")] + repaired_comment + analysis_text[end:]

    signals = model_output.get("expansion_signals")
    repaired_signals = None
    if isinstance(signals, list):
        repaired_signals = []
        for item in signals:
            if not isinstance(item, dict):
                repaired_signals.append(item)
                continue
            repaired_item = dict(item)
            signal_kind = str(repaired_item.get("signal_kind") or "").strip().lower()
            is_comment_signal = signal_kind.startswith("comment")
            for key in ("signal_text", "reason"):
                value = repaired_item.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                repaired_item[key] = repair_text(value, inherited_source=is_comment_signal)
            if signal_kind in {"comment_fact", "comment_factual"}:
                signal_material = " ".join(
                    str(repaired_item.get(key) or "") for key in ("signal_text", "reason")
                )
                if not _has_uncertainty_marker(signal_material):
                    repaired_item["reason"] = (
                        str(repaired_item.get("reason") or "").rstrip("。！？； ")
                        + "（该评论事实线索待核实）。"
                    )
            repaired_signals.append(repaired_item)

    transcript_claims = _extract_attributed_author_claims(str(input_payload.get("transcript") or ""))
    if transcript_claims:
        comment_boundary = _comment_section_text(analysis_text)
        non_comment_text = analysis_text.replace(comment_boundary, "", 1)
        attributed_markers = _AUTHOR_ATTRIBUTION_MARKERS + ("原文认为", "作者认为", "文案认为", "口播认为")
        for claim in transcript_claims:
            for sentence in _semantic_sentences(non_comment_text):
                if claim in _normalise_claim_text(sentence) and not any(
                    marker in sentence for marker in attributed_markers
                ):
                    replacement = "原文作者认为：" + sentence
                    analysis_text = analysis_text.replace(sentence, replacement, 1)
                    non_comment_text = analysis_text.replace(comment_boundary, "", 1)
                    break

    signals_changed = repaired_signals is not None and repaired_signals != signals
    if analysis_text == model_output["analysis_text"] and not signals_changed:
        return model_output
    repaired = dict(model_output)
    repaired["analysis_text"] = analysis_text
    if signals_changed:
        repaired["expansion_signals"] = repaired_signals
    return repaired


def validate_competitor_breakdown_question_expansion_output(
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    *,
    validate_optional: bool = True,
) -> None:
    """Validate the raw breakdown envelope and keep expansions source-bound."""
    base_fields = {
        "source_id", "source_content_type", "analysis_text", "schema_version",
    }
    optional_fields = {
        "source_content_type_id", "question_expansions",
        "expansion_signals", "typed_expansion_leads", "boundary_observation",
    }
    if not base_fields.issubset(output_payload) or set(output_payload) - base_fields - optional_fields:
        raise FormalSkillValidationError("competitor breakdown output has an invalid field set")
    if output_payload["source_id"] != input_payload["source_id"]:
        raise FormalSkillValidationError("competitor breakdown output does not match its supplied source")
    schema_version = str(output_payload.get("schema_version") or "")
    if schema_version not in {"competitor_breakdown.output.raw.v4", "competitor_breakdown.output.raw.v5"}:
        raise FormalSkillValidationError("competitor breakdown output has an unsupported version")
    if schema_version == "competitor_breakdown.output.raw.v4" and set(output_payload) != base_fields | {"question_expansions"}:
        raise FormalSkillValidationError("legacy competitor breakdown output cannot contain lifecycle fields")
    if not isinstance(output_payload["source_content_type"], str) or not output_payload["source_content_type"].strip():
        raise FormalSkillValidationError("爆款拆解必须先识别本条材料的内容类型")
    if not isinstance(output_payload["analysis_text"], str) or not output_payload["analysis_text"].strip():
        raise FormalSkillValidationError("competitor breakdown analysis is empty")
    if "boundary_observation" in output_payload and (
        not isinstance(output_payload["boundary_observation"], str)
        or not output_payload["boundary_observation"].strip()
    ):
        raise FormalSkillValidationError("competitor breakdown boundary observation must be non-empty text")
    analysis_text = output_payload["analysis_text"]
    _validate_comment_semantics(input_payload, analysis_text)
    shortfall_section = analysis_text
    if "六、候选复用原则与边界" in analysis_text:
        shortfall_section = analysis_text.split("六、候选复用原则与边界", 1)[1]
    if "无明显短板" not in shortfall_section and re.search(
        r"(?:短板|缺点).{0,40}(?:点赞|评论数量|系列|继续|账号能力)",
        shortfall_section,
    ):
        raise FormalSkillValidationError(
            "拆解短板必须相对于本篇承诺和当前内容类型，不能把外部表现或系列延续性当成本篇缺点"
        )
    if not validate_optional:
        return

    expansions = output_payload.get("question_expansions", [])
    if not isinstance(expansions, list) or len(expansions) > 3:
        raise FormalSkillValidationError("competitor breakdown question expansions must contain at most three items")
    context = input_payload.get("domain_context")
    context = context if isinstance(context, dict) else {}
    has_domain_boundary = any(
        str(context.get(key) or "").strip()
        for key in ("description", "allowed_scope")
    ) or any(
        isinstance(context.get(key), list) and any(str(item).strip() for item in context.get(key, []))
        for key in ("excluded_terms", "risk_block_terms")
    )
    signals = output_payload.get("expansion_signals", [])
    typed_leads = output_payload.get("typed_expansion_leads", [])
    lifecycle = str(context.get("content_type_lifecycle") or "discover").strip().casefold()
    registry = context.get("content_type_registry")
    registry = registry if isinstance(registry, dict) else {}
    registry_status = str(registry.get("status") or "").strip().casefold()
    approved_ids = {
        str(item.get("canonical_id") or "").strip()
        for item in (registry.get("types") or [])
        if isinstance(item, dict) and str(item.get("canonical_id") or "").strip()
    }
    if lifecycle == "classify":
        if registry_status != "frozen" or not approved_ids:
            raise FormalSkillValidationError(
                "production classification requires a non-empty FROZEN content type registry"
            )
        source_type = str(output_payload.get("source_content_type") or "").strip()
        source_type_id = str(output_payload.get("source_content_type_id") or "").strip()
        if source_type not in approved_ids and source_type not in {"NO_MATCH", "OUT_OF_SCOPE"}:
            raise FormalSkillValidationError(
                "production source_content_type must be an approved canonical id or NO_MATCH/OUT_OF_SCOPE"
            )
        if source_type_id and source_type_id != source_type:
            raise FormalSkillValidationError(
                "production source_content_type_id must match source_content_type"
            )
        if source_type in {"NO_MATCH", "OUT_OF_SCOPE"} and (signals or typed_leads):
            raise FormalSkillValidationError(
                "an out-of-scope source cannot emit production typed expansion leads"
            )
    if not isinstance(signals, list) or not isinstance(typed_leads, list):
        raise FormalSkillValidationError("expansion signals and typed leads must be arrays")
    if len(typed_leads) > 3:
        raise FormalSkillValidationError("typed expansion leads must contain at most three items")
    signal_ids: set[str] = set()
    for item in signals:
        if not isinstance(item, dict) or set(item) != {"signal_id", "signal_kind", "signal_text", "source_anchor", "reason"}:
            raise FormalSkillValidationError("expansion signal has an invalid shape")
        signal_id = str(item["signal_id"] or "").strip()
        if not signal_id or signal_id in signal_ids or not str(item["signal_text"] or "").strip():
            raise FormalSkillValidationError("expansion signal needs unique identity and text")
        signal_ids.add(signal_id)
    for item in typed_leads:
        if not isinstance(item, dict) or set(item) != {"signal_id", "canonical_id", "core_question", "reason"}:
            raise FormalSkillValidationError("typed expansion lead has an invalid shape")
        if str(item["signal_id"] or "").strip() not in signal_ids:
            raise FormalSkillValidationError("typed expansion lead must point to an expansion signal")
        canonical_id = str(item["canonical_id"] or "").strip()
        if not canonical_id or len(str(item["core_question"] or "").strip()) < 6 or not str(item["reason"] or "").strip():
            raise FormalSkillValidationError("typed expansion lead needs a type id, question and reason")
        if lifecycle == "classify" and canonical_id not in approved_ids:
            raise FormalSkillValidationError(
                "production typed expansion lead must use an approved canonical id"
            )
    if (
        lifecycle == "classify"
        and (expansions or signals or typed_leads)
        and not has_domain_boundary
    ):
        raise FormalSkillValidationError(
            "question expansion requires current domain boundary context"
        )
    expansion_policy = context.get("question_expansion_policy")
    if not isinstance(expansion_policy, dict):
        expansion_policy = {}
    carrier_terms = [
        str(term).strip().casefold()
        for term in (expansion_policy.get("primary_carrier_signal_terms") or [])
        if str(term).strip()
    ]
    if "该样本不具备当前领域正式拆解资格" in output_payload["analysis_text"]:
        raise FormalSkillValidationError(
            "competitor breakdown source is outside the current domain and formal write is blocked"
        )
    seen_questions: set[str] = set()
    for item in expansions:
        if not isinstance(item, dict) or set(item) != {"content_type", "core_question", "reason"}:
            raise FormalSkillValidationError("question expansion has an invalid shape")
        content_type = str(item["content_type"] or "").strip()
        if not content_type:
            raise FormalSkillValidationError("question expansion content type is empty")
        question = str(item["core_question"] or "").strip()
        reason = str(item["reason"] or "").strip()
        if len(question) < 6 or not reason:
            raise FormalSkillValidationError("question expansion needs a concrete question and reason")
        if expansion_policy.get("primary_content_carrier_required") and carrier_terms and not any(
            term in question.casefold() for term in carrier_terms
        ):
            raise FormalSkillValidationError(
                "拓展问题的核心内容没有体现当前领域要求的主要承载方式"
            )
        expansion_text = f"{question} {reason}"
        unsupported_premise_markers = (
            "产生重大影响", "带来重大影响", "具有历史意义", "被大众忽视",
            "不为大众熟知", "被忽略的经典", "经典歌曲", "引发听众共鸣",
            "成为许多听众的", "重要合作", "互相成就", "相互影响",
            "行业影响", "普遍现象", "重大转折", "重要转折", "代表作",
        )
        if any(marker in expansion_text for marker in unsupported_premise_markers) and not any(
            marker in expansion_text for marker in ("是否", "有没有", "是否存在", "能否核实", "待核实", "有待确认")
        ):
            raise FormalSkillValidationError(
                "拓展问题不能把来源尚未支持的影响、意义、经典性或观众效果预先写成事实"
            )
        identity = "".join(question.casefold().split())
        if identity in seen_questions:
            raise FormalSkillValidationError("question expansions must be distinct")
        seen_questions.add(identity)


def validate_current_competitor_breakdown_output_semantics(
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
) -> None:
    """Validate the current full spoken-content breakdown and its adaptation boundary."""
    expected = {
        "source_id", "content_subject_type", "expression_form", "content_type_evidence",
        "structure_assessment", "structure_grasp", "spoken_progression",
        "recurring_evidence_patterns", "audience_reactions", "full_analysis",
        "cannot_infer", "schema_version",
    }
    if set(output_payload) != expected or output_payload["source_id"] != input_payload["source_id"]:
        raise FormalSkillValidationError("爆款拆解结果与输入素材不一致")
    if output_payload["schema_version"] != "competitor_breakdown.output.v1":
        raise FormalSkillValidationError("爆款拆解结果版本不受支持")
    if output_payload["content_subject_type"] not in {
        "person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear",
    }:
        raise FormalSkillValidationError("爆款拆解主题类型无效")
    if output_payload["expression_form"] not in {
        "story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear",
    }:
        raise FormalSkillValidationError("爆款拆解表达形式无效")

    transcript_catalog = _build_numbered_transcript_catalog(str(input_payload.get("transcript") or ""))
    transcript_lookup = {item["id"]: item["text"] for item in transcript_catalog}
    comment_catalog = _build_numbered_comment_catalog(list(input_payload.get("comments") or []))
    comment_lookup = {item["id"]: item["text"] for item in comment_catalog}
    if not transcript_lookup:
        raise FormalSkillValidationError("爆款拆解必须有口播原文")

    def evidence(value: Any, *, label: str, lookup: dict[str, str], minimum: int = 1) -> None:
        if not isinstance(value, list) or len(value) < minimum:
            raise FormalSkillValidationError(f"爆款拆解{label}缺少原文依据")
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "text"}:
                raise FormalSkillValidationError(f"爆款拆解{label}的依据未还原")
            identifier = str(item.get("id") or "")
            text = str(item.get("text") or "")
            if identifier not in lookup or lookup[identifier] != text:
                raise FormalSkillValidationError(f"爆款拆解{label}引用了输入材料之外的依据")

    def text(value: Any, *, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise FormalSkillValidationError(f"爆款拆解{label}不能为空")
        if re.search(r"(?<![A-Za-z0-9_])[PC][0-9]{3}(?![A-Za-z0-9_])", value):
            raise FormalSkillValidationError(f"爆款拆解{label}不能暴露内部依据编号")

    evidence(output_payload["content_type_evidence"], label="内容类型", lookup=transcript_lookup)
    assessment = output_payload["structure_assessment"]
    if not isinstance(assessment, dict) or set(assessment) != {"source_evidence", "level", "statement"}:
        raise FormalSkillValidationError("爆款拆解结构判断格式无效")
    evidence(assessment["source_evidence"], label="结构判断", lookup=transcript_lookup)
    if assessment["level"] not in {"simple", "distinct", "unclear"}:
        raise FormalSkillValidationError("爆款拆解结构判断级别无效")
    text(assessment["statement"], label="结构判断")

    grasp = output_payload["structure_grasp"]
    if not isinstance(grasp, dict) or set(grasp) != {"core", "tensions", "highlights"}:
        raise FormalSkillValidationError("爆款拆解结构理解格式无效")
    for key in ("core", "tensions", "highlights"):
        if not isinstance(grasp[key], list):
            raise FormalSkillValidationError(f"爆款拆解结构理解的{key}格式无效")

    progression = output_payload["spoken_progression"]
    if not isinstance(progression, list):
        raise FormalSkillValidationError("爆款拆解口播推进格式无效")
    if assessment["level"] == "distinct" and not progression:
        raise FormalSkillValidationError("结构被判断为有明显组织方式，但没有记录口播推进")
    for item in progression:
        if not isinstance(item, dict):
            raise FormalSkillValidationError("爆款拆解口播推进条目无效")
        for key in ("source_evidence", "spoken_action", "structural_role", "mainline_phase"):
            if key not in item:
                raise FormalSkillValidationError("爆款拆解口播推进缺少必要字段")
        evidence(item["source_evidence"], label="口播推进", lookup=transcript_lookup)
        text(item["spoken_action"], label="口播动作")
        text(item["structural_role"], label="结构作用")
        if item["mainline_phase"] not in {"setup", "conflict", "response", "turn", "outcome", "closure"}:
            raise FormalSkillValidationError("爆款拆解口播推进阶段无效")

    recurring = output_payload["recurring_evidence_patterns"]
    if not isinstance(recurring, list):
        raise FormalSkillValidationError("爆款拆解重复表达格式无效")
    for item in recurring:
        if not isinstance(item, dict):
            raise FormalSkillValidationError("爆款拆解重复表达条目无效")
        if "source_evidence" in item:
            evidence(item["source_evidence"], label="重复表达", lookup=transcript_lookup)
        statement = item.get("statement") or item.get("pattern")
        text(statement, label="重复表达")

    reactions = output_payload["audience_reactions"]
    if not isinstance(reactions, list):
        raise FormalSkillValidationError("爆款拆解观众反应格式无效")
    for item in reactions:
        if not isinstance(item, dict):
            raise FormalSkillValidationError("爆款拆解观众反应条目无效")
        evidence(item.get("comment_evidence"), label="评论反应", lookup=comment_lookup)
        if item.get("related_spoken_evidence"):
            evidence(item["related_spoken_evidence"], label="评论关联口播", lookup=transcript_lookup)
        text(item.get("observed_reaction"), label="观众反应")
    if not comment_lookup and reactions:
        raise FormalSkillValidationError("评论材料为空时不能生成观众实际反应")

    full_analysis = output_payload["full_analysis"]
    required_sections = {
        "target_audience", "theme", "structure", "script_formula", "writing_methods",
        "tone_style", "opening_hook", "emotional_arc", "persona", "reusable_parts",
        "adaptation_suggestions", "final_summary",
    }
    if not isinstance(full_analysis, dict) or not required_sections.issubset(full_analysis):
        raise FormalSkillValidationError("爆款拆解没有交付完整分析部分")

    def walk_analysis(value: Any, *, field_name: str = "完整分析") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "source_evidence":
                    evidence(item, label=field_name, lookup=transcript_lookup, minimum=0)
                elif key == "related_spoken_evidence":
                    evidence(item, label=field_name, lookup=transcript_lookup, minimum=0)
                elif key == "comment_evidence":
                    evidence(item, label=field_name, lookup=comment_lookup, minimum=0)
                else:
                    walk_analysis(item, field_name=key)
        elif isinstance(value, list):
            for item in value:
                walk_analysis(item, field_name=field_name)

    walk_analysis(full_analysis)
    for section in required_sections:
        if not full_analysis.get(section):
            raise FormalSkillValidationError(f"爆款拆解的{section}部分不能为空")
    suggestions = full_analysis["adaptation_suggestions"]
    if not isinstance(suggestions, dict):
        raise FormalSkillValidationError("爆款拆解原创改编建议格式无效")
    if len(suggestions.get("structural_optimizations") or []) < 3:
        raise FormalSkillValidationError("爆款拆解原创改编建议至少需要三条结构优化")
    if len(suggestions.get("directions") or []) < 2:
        raise FormalSkillValidationError("爆款拆解原创改编建议至少需要两个改编方向")
    reusable = full_analysis["reusable_parts"]
    if not isinstance(reusable, dict) or not reusable.get("learn") or not reusable.get("do_not_copy"):
        raise FormalSkillValidationError("爆款拆解必须同时说明可学习和不可照搬内容")

    limits = output_payload["cannot_infer"]
    if not isinstance(limits, list) or not limits or not all(isinstance(item, str) and item.strip() for item in limits):
        raise FormalSkillValidationError("爆款拆解不能推断边界无效")
    if not any("播放" in item and ("不能" in item or "不可" in item or "无法" in item) for item in limits):
        raise FormalSkillValidationError("爆款拆解必须明确拒绝传播效果因果判断")
    if not comment_lookup and not any("评论" in item and ("空" in item or "无法" in item or "没有" in item) for item in limits):
        raise FormalSkillValidationError("评论材料为空时必须明确说明无法判断实际观众反应")

    prose = json.dumps(full_analysis, ensure_ascii=False)
    forbidden_effect_claims = ("导致播放", "导致点赞", "带来爆款", "因此爆火", "提升完播率", "提高点赞")
    if any(term in prose for term in forbidden_effect_claims):
        raise FormalSkillValidationError("爆款拆解不能把文本方法写成传播效果因果")


def validate_competitor_breakdown_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    """Validate the restored V9 record without asking a model to repair it."""
    # This is the final formal-data boundary.  Evidence identifiers belong only
    # in their dedicated evidence fields, so remove any transport markers that
    # survived a provider-specific output shape before validation and storage.
    sanitized_output = _strip_internal_source_ids_from_competitor_prose(output_payload)
    if sanitized_output != output_payload:
        output_payload.clear()
        output_payload.update(sanitized_output)
    expected = {
        "source_id", "content_subject_type", "expression_form", "content_type_evidence", "structural_reading", "deep_reading",
        "spoken_progression", "recurring_evidence_patterns", "audience_reactions",
        "uncertain_parts", "reference_boundary", "cannot_infer", "schema_version",
    }
    if set(output_payload) != expected or output_payload["source_id"] != input_payload["source_id"]:
        raise FormalSkillValidationError("competitor breakdown output does not match its supplied source")
    if output_payload["schema_version"] not in {"removed_structural_breakdown_template"}:
        raise FormalSkillValidationError("competitor breakdown output has an unsupported version")
    if output_payload["content_subject_type"] not in {"person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear"}:
        raise FormalSkillValidationError("competitor breakdown subject type is invalid")
    if output_payload["expression_form"] not in {"story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear"}:
        raise FormalSkillValidationError("competitor breakdown expression form is invalid")
    transcript = str(input_payload.get("transcript") or "")
    if not transcript.strip():
        raise FormalSkillValidationError("competitor breakdown requires a supplied transcript")

    def concrete(value: Any, *, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FormalSkillValidationError(f"competitor breakdown {label} must be concise and concrete")
        text = value.strip()
        if re.search(r"(?<![A-Za-z0-9_])[PC][0-9]{3}(?![A-Za-z0-9_])", text):
            raise FormalSkillValidationError(f"competitor breakdown {label} must not expose internal source identifiers")
        return text

    def citations(value: Any, *, label: str, prefix: str, source: set[str], minimum: int = 1) -> None:
        if not isinstance(value, list) or len(value) < minimum:
            raise FormalSkillValidationError(f"competitor breakdown {label} lacks numbered evidence")
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "text"}:
                raise FormalSkillValidationError(f"competitor breakdown {label} evidence is not restored")
            evidence_id, text = str(item.get("id") or ""), str(item.get("text") or "")
            if not re.fullmatch(rf"{prefix}[0-9]{{3}}", evidence_id) or not text or text not in source:
                raise FormalSkillValidationError(f"competitor breakdown {label} evidence is outside supplied material")

    common_anchors = {"我们", "你们", "他们", "一个", "这个", "什么", "因为", "所以", "可以", "不是", "然后", "就是", "没有"}

    generic_action_phrases = (
        "\u6309\u65f6\u95f4\u987a\u5e8f", "\u4f9d\u6b21\u8bb2\u8ff0", "\u7a7f\u63d2\u4e86\u4eba\u7269\u5bf9\u8bdd",
        "\u53d9\u8ff0\u4e2d", "\u70b9\u51fa\u89c6\u9891\u4e3b\u9898", "\u5ba2\u89c2\u53d9\u8ff0", "\u4ecb\u7ecd\u4eba\u7269\u80cc\u666f",
        "\u5f15\u51fa\u7b2c\u4e00\u4e2a\u5177\u4f53\u4eba\u7269",
    )
    abstract_judgement_phrases = (
        "\u57cb\u4e0b\u4f0f\u7b14", "\u821e\u53f0\u542b\u91d1\u91cf", "\u7ffb\u5531\u6807\u6746", "\u5236\u9020\u60ac\u5ff5",
        "\u5438\u5f15\u4eba", "\u63d0\u5347\u5b8c\u64ad", "\u5f15\u53d1\u5171\u9e23", "\u51f8\u663e\u4ef7\u503c",
    )

    def require_source_anchor(
        value: str,
        evidence: list[dict[str, str]],
        *,
        label: str,
        profile_timeline_allowed: bool = False,
        list_structure_allowed: bool = False,
    ) -> None:
        compact_value = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", value))
        disallowed_generic_phrases = generic_action_phrases
        if profile_timeline_allowed:
            disallowed_generic_phrases = tuple(
                phrase for phrase in generic_action_phrases if phrase not in {"按时间顺序", "依次讲述"}
            )
        if any(phrase in value for phrase in disallowed_generic_phrases):
            raise FormalSkillValidationError(f"competitor breakdown {label} is a video overview rather than one source action")
        if any(phrase in value for phrase in abstract_judgement_phrases):
            raise FormalSkillValidationError(f"competitor breakdown {label} uses an abstract evaluation rather than a source-bound action")
        if list_structure_allowed and any(marker in value for marker in ("第X首", "逐项", "依次", "清单", "盘点", "报出")):
            return
        anchors = set()
        for item in evidence:
            text = "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", str(item.get("text") or "")))
            anchors.update(text[index:index + 2] for index in range(max(0, len(text) - 1)))
        if not any(anchor not in common_anchors and anchor in compact_value for anchor in anchors):
            raise FormalSkillValidationError(f"competitor breakdown {label} must name an identifiable word or phrase from its cited source")

    transcript_catalog = _build_numbered_transcript_catalog(transcript)
    transcript_segments = {str(item.get("text") or "") for item in transcript_catalog}
    comments = {
        str(item.get("text") or "").strip() if isinstance(item, dict) else str(item).strip()
        for item in input_payload.get("comments") or []
    }
    citations(output_payload["content_type_evidence"], label="content type", prefix="P", source=transcript_segments)
    structural_reading = output_payload["structural_reading"]
    if not isinstance(structural_reading, dict) or set(structural_reading) != {"source_evidence", "organizing_thread"}:
        raise FormalSkillValidationError("competitor breakdown structural reading is invalid")
    citations(structural_reading["source_evidence"], label="structural reading", prefix="P", source=transcript_segments)
    concrete(structural_reading["organizing_thread"], label="structural reading")
    deep_reading = output_payload["deep_reading"]
    if not isinstance(deep_reading, dict) or set(deep_reading) != {"source_evidence", "central_reading", "selected_lenses"}:
        raise FormalSkillValidationError("competitor breakdown deep reading is invalid")
    citations(deep_reading["source_evidence"], label="deep reading", prefix="P", source=transcript_segments)
    concrete(deep_reading["central_reading"], label="deep reading")
    selected_lenses = deep_reading["selected_lenses"]
    if not isinstance(selected_lenses, list):
        raise FormalSkillValidationError("competitor breakdown selected lenses are invalid")
    for lens in selected_lenses:
        if not isinstance(lens, dict) or set(lens) != {"lens_id", "source_evidence", "reading"}:
            raise FormalSkillValidationError("competitor breakdown selected lens is invalid")
        citations(lens["source_evidence"], label="selected lens", prefix="P", source=transcript_segments)
        concrete(lens["reading"], label="selected lens")
    progression = output_payload["spoken_progression"]
    if not isinstance(progression, list) or not progression:
        raise FormalSkillValidationError("competitor breakdown needs a spoken progression")
    sequences: set[int] = set()
    previous_last_position = -1
    for item in progression:
        if not isinstance(item, dict) or set(item) != {"sequence", "source_evidence", "spoken_action", "structural_role"}:
            raise FormalSkillValidationError("competitor breakdown progression stage is invalid")
        sequence = item["sequence"]
        if not isinstance(sequence, int) or sequence < 1 or sequence in sequences:
            raise FormalSkillValidationError("competitor breakdown progression sequence is invalid")
        sequences.add(sequence)
        action = concrete(item["spoken_action"], label="progression spoken action")
        concrete(item["structural_role"], label="progression structural role")
        citations(item["source_evidence"], label="progression stage", prefix="P", source=transcript_segments)
        require_source_anchor(
            action,
            item["source_evidence"],
            label="progression spoken action",
            profile_timeline_allowed=output_payload["expression_form"] == "profile",
        )
        first_position = min(int(str(evidence["id"])[1:]) for evidence in item["source_evidence"])
        last_position = max(int(str(evidence["id"])[1:]) for evidence in item["source_evidence"])
        if last_position < previous_last_position:
            raise FormalSkillValidationError("competitor breakdown progression must follow the source order")
        previous_last_position = last_position
    if sequences != set(range(1, len(progression) + 1)):
        raise FormalSkillValidationError("competitor breakdown progression sequence must be continuous")

    # A label only supports retrieval. It must not force a single legitimate
    # narrative shape onto a source that actually combines chronology, conflict,
    # comparison, or item-by-item development.
    if False:
        chronological_catalog_streak = 0
        catalog_action_terms = ("\u8bb2\u8ff0", "\u4ecb\u7ecd", "\u7ee7\u7eed\u4ecb\u7ecd", "\u5217\u4e3e", "\u56de\u6eaf", "\u53d9\u8ff0")
        catalog_source_pattern = re.compile(r"(?:19|20)\d{2}|\u5355\u66f2|\u4e13\u8f91|\u4f5c\u54c1|\u6f14\u5531\u4f1a|\u699c\u5355|\u64ad\u653e\u91cf")
        for item in progression:
            action = str(item.get("spoken_action") or "")
            source_text = " ".join(str(evidence.get("text") or "") for evidence in item["source_evidence"])
            named_works = set(re.findall(r"《[^》]{1,80}》", action))
            if len(named_works) >= 3 and any(term in action for term in catalog_action_terms):
                raise FormalSkillValidationError(
                    "competitor breakdown story progression turns one stage into a catalog of works"
                )
            if any(term in action for term in catalog_action_terms) and catalog_source_pattern.search(source_text):
                chronological_catalog_streak += 1
            else:
                chronological_catalog_streak = 0
            if chronological_catalog_streak >= 3:
                raise FormalSkillValidationError(
                    "competitor breakdown story progression turns one continuous biography into a chronological catalog"
                )

    if False:
        for item in progression:
            action = str(item.get("spoken_action") or "")
            names_one_item = re.search(r"《[^》]+》", action)
            ordinal = re.search(r"\u7b2c\s*[\u4e00-\u9fff0-9]+\s*(?:\u9996|\u4f4d|\u540d|\u4e2a|\u4ef6|\u90e8|\u8282)", action)
            describes_list_process = any(marker in action for marker in ("逐项", "依次", "清单", "盘点", "报出", "分享"))
            if (
                names_one_item
                or (ordinal and not describes_list_process)
                or re.search(r"[（(][^（）()]{0,120}[、，,][^（）()]{0,120}[）)]", action)
            ):
                raise FormalSkillValidationError("competitor breakdown list items belong in recurring evidence patterns, not the main progression")
    recurring_patterns = output_payload["recurring_evidence_patterns"]
    if not isinstance(recurring_patterns, list):
        raise FormalSkillValidationError("competitor breakdown recurring evidence patterns are invalid")
    for item in recurring_patterns:
        if not isinstance(item, dict) or set(item) != {"source_evidence", "spoken_action"}:
            raise FormalSkillValidationError("competitor breakdown recurring evidence pattern is invalid")
        citations(item["source_evidence"], label="recurring evidence pattern", prefix="P", source=transcript_segments)
        action = concrete(item["spoken_action"], label="recurring evidence pattern")
        require_source_anchor(
            action,
            item["source_evidence"],
            label="recurring evidence pattern",
            list_structure_allowed=output_payload["expression_form"] == "list",
        )
        if False and output_payload["expression_form"] == "list" and len(item["source_evidence"]) < 2:
            raise FormalSkillValidationError("competitor breakdown list recurring pattern needs representative evidence")
        if output_payload["expression_form"] == "list" and len(set(re.findall(r"《[^》]+》", action))) > 3:
            raise FormalSkillValidationError("competitor breakdown list recurring pattern must keep only a few representative examples")
    if False and output_payload["expression_form"] == "list" and not recurring_patterns:
        raise FormalSkillValidationError("competitor breakdown list requires a recurring evidence pattern")
    reactions = output_payload["audience_reactions"]
    if not isinstance(reactions, list):
        raise FormalSkillValidationError("competitor breakdown audience reactions is invalid")
    for item in reactions:
        if not isinstance(item, dict) or set(item) != {"comment_evidence", "related_spoken_evidence", "observed_reaction"}:
            raise FormalSkillValidationError("competitor breakdown audience reaction is invalid")
        citations(item["comment_evidence"], label="audience reaction", prefix="C", source=comments)
        concrete(item["observed_reaction"], label="audience reaction")
        if item["related_spoken_evidence"]:
            citations(item["related_spoken_evidence"], label="audience reaction related speech", prefix="P", source=transcript_segments)

    reference_boundary = output_payload["reference_boundary"]
    if not isinstance(reference_boundary, dict) or set(reference_boundary) != {"usable_for", "not_usable_for"}:
        raise FormalSkillValidationError("competitor breakdown reference boundary is invalid")
    boundary_banned_terms = ("\u7206\u6b3e", "\u6709\u6548", "\u65e0\u6548", "\u597d\u6587\u6848", "\u5dee\u6587\u6848")
    for boundary_key, boundary_label in (("usable_for", "reference usable for"), ("not_usable_for", "reference not usable for")):
        items = reference_boundary[boundary_key]
        if not isinstance(items, list) or len(items) != 1:
            raise FormalSkillValidationError(f"competitor breakdown {boundary_label} needs exactly one source-bound statement")
        item = items[0]
        if not isinstance(item, dict) or set(item) != {"source_evidence", "statement"}:
            raise FormalSkillValidationError(f"competitor breakdown {boundary_label} is invalid")
        citations(item["source_evidence"], label=boundary_label, prefix="P", source=transcript_segments)
        statement = concrete(item["statement"], label=boundary_label)
        if any(term in statement for term in boundary_banned_terms):
            raise FormalSkillValidationError(f"competitor breakdown {boundary_label} must not make an effectiveness or quality claim")

    uncertain_parts = output_payload["uncertain_parts"]
    if not isinstance(uncertain_parts, list):
        raise FormalSkillValidationError("competitor breakdown uncertain parts are invalid")
    wholly_uncertain_ids: set[str] = set()
    for item in uncertain_parts:
        if not isinstance(item, dict) or set(item) != {"source_evidence"}:
            raise FormalSkillValidationError("competitor breakdown uncertain part is invalid")
        citations(item["source_evidence"], label="uncertain part", prefix="P", source=transcript_segments)
        if len(item["source_evidence"]) != 1:
            raise FormalSkillValidationError("competitor breakdown uncertain part must identify one source segment")
        evidence_id = str(item["source_evidence"][0].get("id") or "")
        wholly_uncertain_ids.add(evidence_id)

    def forbid_uncertain_evidence(value: Any, *, label: str) -> None:
        if not isinstance(value, list):
            return
        used_ids = {
            str(evidence.get("id") or "")
            for evidence in value
            if isinstance(evidence, dict)
        }
        conflict_ids = wholly_uncertain_ids & used_ids
        if conflict_ids:
            raise FormalSkillValidationError(
                f"competitor breakdown {label} uses wholly uncertain source {', '.join(sorted(conflict_ids))}"
            )

    forbid_uncertain_evidence(output_payload["content_type_evidence"], label="content type")
    forbid_uncertain_evidence(deep_reading["source_evidence"], label="deep reading")
    for lens in selected_lenses:
        forbid_uncertain_evidence(lens["source_evidence"], label="selected lens")
    for item in progression:
        forbid_uncertain_evidence(item["source_evidence"], label="progression")
    for boundary_key, items in reference_boundary.items():
        for item in items:
            forbid_uncertain_evidence(item["source_evidence"], label=f"reference boundary {boundary_key}")
    for item in reactions:
        forbid_uncertain_evidence(item["related_spoken_evidence"], label="audience reaction related speech")

    limits = output_payload["cannot_infer"]
    if not isinstance(limits, list) or not limits or not all(isinstance(item, str) and item.strip() for item in limits):
        raise FormalSkillValidationError("competitor breakdown inference limits are invalid")
    performance_terms = ("\u64ad\u653e", "\u70b9\u8d5e", "\u8bc4\u8bba", "\u7206\u6b3e")
    denial_terms = ("\u4e0d\u80fd", "\u4e0d\u53ef")
    if not any(any(term in item for term in performance_terms) and any(term in item for term in denial_terms) for item in limits):
        raise FormalSkillValidationError("competitor breakdown must reject performance-causality claims")


def validate_removed_structural_breakdown_output_semantics(
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
) -> None:
    """Validate the source-only structural spoken-breakdown version."""
    sanitized = _strip_internal_source_ids_from_competitor_prose(output_payload)
    if sanitized != output_payload:
        output_payload.clear()
        output_payload.update(sanitized)
    expected = {
        "source_id", "content_subject_type", "expression_form", "content_type_evidence",
        "structure_assessment",
        "spoken_progression", "recurring_evidence_patterns", "audience_reactions",
        "cannot_infer", "schema_version",
    }
    expected.add("structure_grasp")
    if set(output_payload) != expected or output_payload["source_id"] != input_payload["source_id"]:
        raise FormalSkillValidationError("spoken breakdown output does not match its supplied source")
    if output_payload["schema_version"] != "removed_structural_breakdown_template":
        raise FormalSkillValidationError("spoken breakdown output has an unsupported version")
    if output_payload["content_subject_type"] not in {"person", "work", "event", "concept", "case", "method", "collection", "mixed", "unclear"}:
        raise FormalSkillValidationError("spoken breakdown subject type is invalid")
    if output_payload["expression_form"] not in {"story", "profile", "list", "analysis", "explanation", "commentary", "event_response", "interview", "mixed", "unclear"}:
        raise FormalSkillValidationError("spoken breakdown expression form is invalid")

    transcript_catalog = _build_numbered_transcript_catalog(str(input_payload.get("transcript") or ""))
    transcript_segments = {str(item.get("text") or "") for item in transcript_catalog}
    if not transcript_segments:
        raise FormalSkillValidationError("spoken breakdown requires a supplied transcript")
    comment_segments = {
        str(item.get("text") or "")
        for item in _build_numbered_comment_catalog(list(input_payload.get("comments") or []))
    }

    def prose(value: Any, *, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise FormalSkillValidationError(f"spoken breakdown {label} must be present")
        text = value.strip()
        if re.search(r"(?<![A-Za-z0-9_])[PC][0-9]{3}(?![A-Za-z0-9_])", text):
            raise FormalSkillValidationError(f"spoken breakdown {label} must not expose internal source identifiers")
        return text

    def citations(
        value: Any,
        *,
        label: str,
        prefix: str,
        source: set[str],
        minimum: int = 1,
        maximum: int | None = None,
    ) -> set[str]:
        if not isinstance(value, list) or len(value) < minimum:
            raise FormalSkillValidationError(f"spoken breakdown {label} lacks numbered evidence")
        if maximum is not None and len(value) > maximum:
            raise FormalSkillValidationError(
                f"spoken breakdown {label} has too many evidence items; keep only representative evidence"
            )
        ids: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"id", "text"}:
                raise FormalSkillValidationError(f"spoken breakdown {label} evidence is not restored")
            evidence_id, text = str(item.get("id") or ""), str(item.get("text") or "")
            if not re.fullmatch(rf"{prefix}[0-9]{{3}}", evidence_id) or not text or text not in source:
                raise FormalSkillValidationError(f"spoken breakdown {label} evidence is outside supplied material")
            ids.add(evidence_id)
        return ids

    used_source_ids = citations(
        output_payload["content_type_evidence"],
        label="content type",
        prefix="P",
        source=transcript_segments,
        maximum=3,
    )
    structure_assessment = output_payload["structure_assessment"]
    if not isinstance(structure_assessment, dict) or set(structure_assessment) != {"source_evidence", "level", "statement"}:
        raise FormalSkillValidationError("spoken breakdown structure assessment is invalid")
    assessment_level = structure_assessment["level"]
    if assessment_level not in {"simple", "distinct", "unclear"}:
        raise FormalSkillValidationError("spoken breakdown structure assessment level is invalid")
    used_source_ids.update(
        citations(
            structure_assessment["source_evidence"],
            label="structure assessment",
            prefix="P",
            source=transcript_segments,
            maximum=3,
        )
    )
    prose(structure_assessment["statement"], label="structure assessment")
    progression = output_payload["spoken_progression"]
    if not isinstance(progression, list) or (not progression and assessment_level != "simple"):
        raise FormalSkillValidationError("spoken breakdown needs a spoken progression")
    if assessment_level == "simple" and progression:
        raise FormalSkillValidationError(
            "a simple structure must keep repeated delivery in recurring patterns, not spoken progression"
        )
    expected_sequence = 1
    previous_position = -1
    for item in progression:
        if not isinstance(item, dict) or set(item) != {"sequence", "source_evidence", "spoken_action", "structural_role"}:
            raise FormalSkillValidationError("spoken breakdown progression stage is invalid")
        if item["sequence"] != expected_sequence:
            raise FormalSkillValidationError("spoken breakdown progression sequence must be continuous")
        expected_sequence += 1
        ids = citations(
            item["source_evidence"],
            label="progression stage",
            prefix="P",
            source=transcript_segments,
            maximum=4,
        )
        used_source_ids.update(ids)
        prose(item["spoken_action"], label="progression spoken action")
        prose(item["structural_role"], label="progression structural role")
        first_position = min(int(source_id[1:]) for source_id in ids)
        if first_position < previous_position:
            raise FormalSkillValidationError("spoken breakdown progression must follow the source order")
        previous_position = first_position

    # A story may legitimately contain several turns, so there is no fixed
    # stage limit. A generic verb such as "讲述" is not enough to reject a
    # stage: a valid story often uses that verb while its structural_role
    # explains a real conflict, response, turn, or outcome. Reject only a run
    # where both the action and the structural role remain generic event
    # retelling. Do not rewrite the model answer; keep any real failure visible.
    if output_payload["expression_form"] in {"story", "profile"} and len(progression) >= 3:
        generic_biography_action = re.compile(
            r"^(?:\u53e3\u64ad)?(?:\u4ece.*?\u8bb2\u8d77|\u8bb2\u8ff0|\u8be6\u8ff0|\u53d9\u8ff0|\u4ecb\u7ecd|\u56de\u987e)"
        )
        meaningful_change = re.compile(
            r"(?:\u8f6c(?:\u5411|\u6298|\u53d8)|\u6539\u53d8|\u51b2\u7a81|\u56de\u5e94|\u53cd\u5dee|\u56f0\u5883|\u5371\u673a|\u5e94\u5bf9|\u9009\u62e9|\u7a81\u7834|\u7ffb\u76d8|\u6536\u675f|\u56de\u6263|\u5347\u534e|\u5f62\u6210|\u8fdb\u5165|\u63a8\u5411|\u5f3a\u5316|\u5bf9\u7acb|\u843d\u5dee|\u5151\u73b0|\u5b8c\u6210|\u8d77\u70b9|\u7ed3\u679c)"
        )

        def is_generic_recap(stage: dict[str, Any]) -> bool:
            action = str(stage.get("spoken_action") or "").strip()
            role = str(stage.get("structural_role") or "").strip()
            if not generic_biography_action.search(action):
                return False
            if meaningful_change.search(action) or meaningful_change.search(role):
                return False
            return True

        generic_count = sum(
            1
            for item in progression
            if is_generic_recap(item)
        )
        if generic_count * 2 > len(progression):
            raise FormalSkillValidationError(
                "spoken breakdown story progression retells events instead of describing structural spoken actions"
            )

    if output_payload["expression_form"] == "list" and assessment_level != "simple" and len(progression) >= 3:
        # Mentioning item numbers is not itself a failure.  A real list
        # structure may use one item as evidence for a cross-item escalation,
        # contrast, or return to a central line.  Reject only itemized stages
        # whose action and structural role still describe the item itself.
        meaningful_list_structure = re.compile(
            r"(?:标准|筛选|反差|冲突|升级|递进|转向|转折|对比|从.+到|由.+转|回扣|收束|主线|框架|核心|变化|改变|深化|引入|集中|提升|对抗|高潮|闭环|组织|升华|推进|层层|最终)"
        )

        def is_individual_list_item(stage: dict[str, Any]) -> bool:
            source_text = " ".join(
                str(evidence.get("text") or "")
                for evidence in stage["source_evidence"]
                if isinstance(evidence, dict)
            )
            action = str(stage.get("spoken_action") or "").strip()
            role = str(stage.get("structural_role") or "").strip()
            text = f"{action} {source_text}"
            if not re.search(
                r"(?:第\s*[一二三四五六七八九十百千万0-9]+\s*(?:项|步|条|个|例|名|位|种|类|场|段|期|阶段|章|题)|"
                r"[一二三四五六七八九十百千万0-9]+[、.．]|逐(?:项|步|条|例|个))",
                text,
            ):
                return False
            if meaningful_list_structure.search(action) or meaningful_list_structure.search(role):
                return False
            return True

        longest_item_run = 0
        current_item_run = 0
        for stage in progression:
            itemized = is_individual_list_item(stage)
            if itemized:
                current_item_run += 1
                longest_item_run = max(longest_item_run, current_item_run)
            else:
                current_item_run = 0
        if longest_item_run >= 3:
            raise FormalSkillValidationError(
                "spoken breakdown list progression repeats individual delivery items instead of the list's organizing structure"
            )

    recurring = output_payload["recurring_evidence_patterns"]
    if not isinstance(recurring, list):
        raise FormalSkillValidationError("spoken breakdown recurring patterns are invalid")
    for item in recurring:
        if not isinstance(item, dict) or set(item) != {"source_evidence", "spoken_action"}:
            raise FormalSkillValidationError("spoken breakdown recurring pattern is invalid")
        used_source_ids.update(
            citations(
                item["source_evidence"],
                label="recurring pattern",
                prefix="P",
                source=transcript_segments,
                maximum=3,
            )
        )
        prose(item["spoken_action"], label="recurring pattern")

    reactions = output_payload["audience_reactions"]
    if not isinstance(reactions, list):
        raise FormalSkillValidationError("spoken breakdown audience reactions are invalid")
    for item in reactions:
        if not isinstance(item, dict) or set(item) != {"comment_evidence", "related_spoken_evidence", "observed_reaction"}:
            raise FormalSkillValidationError("spoken breakdown audience reaction is invalid")
        citations(
            item["comment_evidence"],
            label="audience reaction",
            prefix="C",
            source=comment_segments,
            maximum=3,
        )
        related = item["related_spoken_evidence"]
        if not isinstance(related, list):
            raise FormalSkillValidationError("spoken breakdown audience reaction related speech is invalid")
        if related:
            used_source_ids.update(
                citations(
                    related,
                    label="audience reaction related speech",
                    prefix="P",
                    source=transcript_segments,
                    maximum=3,
                )
            )
        prose(item["observed_reaction"], label="audience reaction")

    structure_grasp = output_payload["structure_grasp"]
    if not isinstance(structure_grasp, dict) or set(structure_grasp) != {"core", "tensions", "highlights"}:
        raise FormalSkillValidationError("structural breakdown structure grasp is invalid")
    core = structure_grasp["core"]
    if not isinstance(core, dict) or set(core) != {"source_evidence", "statement"}:
        raise FormalSkillValidationError("structural breakdown core is invalid")
    used_source_ids.update(
        citations(
            core["source_evidence"],
            label="structure core",
            prefix="P",
            source=transcript_segments,
            maximum=4,
        )
    )
    prose(core["statement"], label="structure core")
    for key, label in (("tensions", "structure tension"), ("highlights", "structure highlight")):
        items = structure_grasp[key]
        if not isinstance(items, list):
            raise FormalSkillValidationError(f"structural breakdown {label} is invalid")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"source_evidence", "statement"}:
                raise FormalSkillValidationError(f"structural breakdown {label} item is invalid")
            used_source_ids.update(
                citations(
                    item["source_evidence"],
                    label=label,
                    prefix="P",
                    source=transcript_segments,
                    maximum=3,
                )
            )
            prose(item["statement"], label=label)
    limits = output_payload["cannot_infer"]
    if not isinstance(limits, list) or not all(isinstance(item, str) and item.strip() for item in limits):
        raise FormalSkillValidationError("spoken breakdown inference limits are invalid")
    if not any(
        any(term in item for term in ("播放", "点赞", "评论", "爆款"))
        and any(term in item for term in ("不能", "不可"))
        for item in limits
    ):
        raise FormalSkillValidationError("spoken breakdown must reject performance-causality claims")
