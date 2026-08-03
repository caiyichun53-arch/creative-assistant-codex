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
from scripts.core.persistence.goal01_store import blob_hash, content_hash
from scripts.core.runtime.runtime_storage import runtime_path


ROOT = Path(__file__).resolve().parents[3]
SYSTEM_GOVERNANCE_CONTRACT_PATH = ROOT / "config" / "business_guardrails" / "system_governance.json"
QUALITY_CUTOVER_ACCEPTANCE_PATH = runtime_path("agent_platform", "quality_cutover_acceptance.json")
SOURCE_TO_TOPIC_CONTRACT_PATH = ROOT / "SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml"
HOTSPOT_TO_OPPORTUNITY_CONTRACT_PATH = ROOT / "HOTSPOT_TO_OPPORTUNITY_BUSINESS_CONTRACT.yaml"
COMPETITOR_BREAKDOWN_SKILL_IDS = frozenset({
    "competitor_breakdown_structural_v13",
})


class FormalSkillValidationError(RuntimeError):
    """Contract failure with optional raw model output for an isolated quality receipt."""

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

    def validate_contract(self) -> None:
        if not self.formal_skill_id or not self.version or self.route_name not in self.allowed_model_nodes:
            raise FormalSkillValidationError("invalid Skill contract")
        try:
            ModelRouter.from_file().resolve(self.route_id, route_name=self.route_name)
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


class FormalBusinessSkillAdapter:
    def __init__(self, *, contract: FormalSkillContract, gateway: ModelGateway):
        contract.validate_contract()
        self.contract, self.gateway = contract, gateway

    def run(
        self,
        input_payload: dict[str, Any],
        *,
        request_metadata: dict[str, Any] | None = None,
        quality_comparison: bool = False,
    ) -> FormalSkillRunResult:
        from scripts.core.production.business_runtime_guard import (
            enforce_atomic_skill_runtime_guard,
        )

        enforce_atomic_skill_runtime_guard(
            entrypoint="formal_business_skill_adapter",
            operation=self.contract.formal_skill_id,
        )
        if not quality_comparison:
            require_quality_cutover(self.contract)
        validate_payload(input_payload, self.contract.input_schema)
        prepared = preprocess_formal_skill_input(self.contract.formal_skill_id, input_payload)
        model_input = apply_binding(self.contract.input_map, input_payload, {}, prepared)
        validate_payload(model_input, self.contract.model_input_schema)
        if self.contract.route_name not in self.gateway.routes:
            raise FormalSkillValidationError("approved model route is unavailable")
        model_run = self.gateway.complete(ModelRequest(
            route_name=self.contract.route_name,
            prompt=self.contract.portable_skill().render_prompt(model_input),
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
            model_output = parse_model_json(model_run.output_text)
            model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
            validate_payload(model_output, self.contract.model_output_schema)
            if self.contract.formal_skill_id == "competitor_breakdown_structural_v13":
                model_output = materialize_competitor_breakdown_structural_output(model_output)
            output = apply_binding(self.contract.output_map, input_payload, model_output, prepared)
            if self.contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
                output = resolve_competitor_breakdown_evidence_ids(
                    output,
                    list(prepared.get("transcript_catalog") or []),
                    list(prepared.get("comment_catalog") or []),
                )
            validate_payload(output, self.contract.output_schema)
            if self.contract.formal_skill_id == "source_to_topic":
                validate_source_to_topic_output_semantics(input_payload, output)
            if self.contract.formal_skill_id == "competitor_breakdown_structural_v13":
                validate_competitor_breakdown_structural_output_semantics(input_payload, output)
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
        )
        validate_payload(input_payload, self.contract.input_schema)
        prepared = preprocess_formal_skill_input(self.contract.formal_skill_id, input_payload)
        model_input = apply_binding(self.contract.input_map, input_payload, {}, prepared)
        validate_payload(model_input, self.contract.model_input_schema)
        if self.contract.route_name not in self.gateway.routes:
            raise FormalSkillValidationError("approved model route is unavailable")
        correction_prompt = self.contract.portable_skill().render_prompt(model_input) + (
            "\n\n【仅用于本次测试的单次修正】\n"
            "上一份回答没有通过程序核查。不要重新猜测材料，也不要解释错误；"
            "只根据原始编号材料和下面的明确错误，提交一份完整修正后的 JSON。\n"
            "这不是逐项补答：仍按上面的完整模板交付，字段名必须原样保留。\n"
            "核查错误：\n- " + "\n- ".join(errors) +
            "\n\n上一份被拒绝的回答（仅供修正，不是新的材料）：\n"
            "--- previous_model_output ---\n" + rejected_model_output +
            "\n--- end_previous_model_output ---"
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
            model_output = parse_model_json(model_run.output_text)
            model_output = normalize_formal_skill_model_output(self.contract.formal_skill_id, model_output)
            validate_payload(model_output, self.contract.model_output_schema)
            if self.contract.formal_skill_id == "competitor_breakdown_structural_v13":
                model_output = materialize_competitor_breakdown_structural_output(model_output)
            output = apply_binding(self.contract.output_map, input_payload, model_output, prepared)
            if self.contract.formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
                output = resolve_competitor_breakdown_evidence_ids(
                    output,
                    list(prepared.get("transcript_catalog") or []),
                    list(prepared.get("comment_catalog") or []),
                )
            validate_payload(output, self.contract.output_schema)
            if self.contract.formal_skill_id == "source_to_topic":
                validate_source_to_topic_output_semantics(input_payload, output)
            if self.contract.formal_skill_id == "competitor_breakdown_structural_v13":
                validate_competitor_breakdown_structural_output_semantics(input_payload, output)
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
            result[target] = model_output[spec["key"]]
        elif source == "preprocessed":
            result[target] = prepared[spec["key"]]
        elif source == "literal":
            result[target] = spec["value"]
        else:
            raise FormalSkillValidationError("unsupported binding source")
    return result


def require_quality_cutover(contract: FormalSkillContract) -> None:
    governance = json.loads(SYSTEM_GOVERNANCE_CONTRACT_PATH.read_text(encoding="utf-8"))
    status = (governance.get("atomic_skill_quality_cutover") or {}).get(contract.formal_skill_id)
    if status == "per_item_structural_validation_only":
        return
    if status is not None and status != "accepted_same_input_comparison" and not _runtime_quality_cutover_accepted(contract.formal_skill_id):
        raise FormalSkillValidationError(
            f"atomic Skill {contract.formal_skill_id} is blocked until its same-input quality comparison is accepted"
        )


def _runtime_quality_cutover_accepted(formal_skill_id: str) -> bool:
    """Read the user-approved comparison receipt retained outside formal business data."""
    try:
        payload = json.loads(QUALITY_CUTOVER_ACCEPTANCE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    decision = payload.get(formal_skill_id) if isinstance(payload, dict) else None
    if not isinstance(decision, dict) or decision.get("status") != "accepted":
        return False
    receipt_path = Path(str(decision.get("receipt_path") or ""))
    receipt_hash = str(decision.get("receipt_sha256") or "")
    if not receipt_hash or not receipt_path.is_file():
        return False
    return blob_hash(receipt_path.read_bytes()) == receipt_hash


def preprocess_formal_skill_input(formal_skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if formal_skill_id in COMPETITOR_BREAKDOWN_SKILL_IDS:
        transcript_catalog = _build_numbered_transcript_catalog(str(payload.get("transcript") or ""))
        comment_catalog = _build_numbered_comment_catalog(list(payload.get("comments") or []))
        prepared = {
            "transcript_catalog": transcript_catalog,
            "comment_catalog": comment_catalog,
            "numbered_transcript": _render_numbered_catalog(transcript_catalog),
            "numbered_comments": _render_numbered_catalog(comment_catalog),
        }
        return prepared
    if formal_skill_id != "source_to_topic":
        return {}
    source = " ".join(str(payload.get("source_content", "")).split())
    return {"source_text": source, "relation_text": " ".join(str(payload.get("relation_summary", "")).split()), "source_length": len(source)}


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
                {**item, "source_evidence": restore(item.get("source_evidence"), transcript_lookup)}
                if isinstance(item, dict) else item
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
    return resolved


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
    """Turn the compact v13 transport into the fixed formal record shape.

    The model supplies only judgments and source IDs.  Sequence numbers, one-id
    uncertainty records, and every display object are deterministic program work.
    """
    def record(value: Any, keys: tuple[str, ...]) -> Any:
        if not isinstance(value, list) or len(value) != len(keys):
            return value
        return dict(zip(keys, value, strict=True))

    def records(values: Any, keys: tuple[str, ...]) -> Any:
        if not isinstance(values, list):
            return values
        return [record(value, keys) for value in values]

    progression = model_output.get("progression")
    recurring = model_output.get("recurring")
    assessment = model_output.get("structure_assessment")
    is_simple_list = (
        model_output.get("expression_form") == "list"
        and isinstance(assessment, list)
        and len(assessment) == 3
        and assessment[1] == "simple"
    )
    is_simple_profile = (
        model_output.get("expression_form") == "profile"
        and isinstance(assessment, list)
        and len(assessment) == 3
        and assessment[1] == "simple"
    )
    if isinstance(progression, list) and not (is_simple_list or is_simple_profile):
        for stage in progression:
            if not isinstance(stage, list) or len(stage) != 4:
                raise FormalSkillValidationError(
                    "spoken breakdown progression needs evidence, action, structural change, and mainline phase"
                )
            phase = stage[3]
            if phase not in {"setup", "conflict", "response", "turn", "outcome", "closure"}:
                raise FormalSkillValidationError("spoken breakdown progression mainline phase is invalid")
    if is_simple_list or is_simple_profile:
        # A simple list or chronological profile has no source-supported
        # structural change to preserve in its main line.  The model may still
        # mechanically split the delivery into items or dated events; turn
        # those already-selected source IDs into one program-owned recurring
        # delivery record without inventing a new judgment.
        source_ids: list[Any] = []
        if isinstance(progression, list):
            for stage in progression:
                if isinstance(stage, list) and len(stage) >= 3 and isinstance(stage[0], list):
                    source_ids.extend(stage[0])
        if isinstance(recurring, list):
            for pattern in recurring:
                if isinstance(pattern, list) and len(pattern) == 2 and isinstance(pattern[0], list):
                    source_ids.extend(pattern[0])
        if source_ids:
            unique_source_ids = list(dict.fromkeys(source_ids))
            # The final record needs enough source to prove that the delivery
            # is repeated, not every item of a long list.  Keep the first and
            # last already-selected IDs so the final reading stays useful
            # without turning into a transcript-shaped citation block.
            representative_source_ids = (
                [unique_source_ids[0], unique_source_ids[-1]]
                if len(unique_source_ids) > 2
                else unique_source_ids
            )
            recurring = [[
                representative_source_ids,
                (
                    "口播按时间连续交代人物经历，未呈现额外主线转折。"
                    if is_simple_profile
                    else "口播连续逐项交付清单内容，未呈现额外结构变化。"
                ),
            ]]
            progression = []

    return {
        "source_id": model_output.get("source_id"),
        "content_subject_type": model_output.get("content_subject_type"),
        "expression_form": model_output.get("expression_form"),
        "content_type_evidence": model_output.get("content_type_evidence"),
        "structure_assessment": record(
            model_output.get("structure_assessment"), ("source_evidence", "level", "statement")
        ),
        "structure_grasp": {
            "core": record(model_output.get("core"), ("source_evidence", "statement")),
            "tensions": records(model_output.get("tensions"), ("source_evidence", "statement")),
            "highlights": records(model_output.get("highlights"), ("source_evidence", "statement")),
        },
        "spoken_progression": [
            {
                "sequence": index,
                "source_evidence": stage["source_evidence"],
                "spoken_action": stage["spoken_action"],
                "structural_role": stage["structural_role"],
            }
            if isinstance((stage := record(value, ("source_evidence", "spoken_action", "structural_role", "mainline_phase"))), dict)
            else stage
            for index, value in enumerate(progression or [], start=1)
        ] if isinstance(progression, list) else progression,
        "recurring_evidence_patterns": records(recurring, ("source_evidence", "spoken_action")),
        "audience_reactions": records(model_output.get("reactions"), ("comment_evidence", "related_spoken_evidence", "observed_reaction")),
    }


def normalize_formal_skill_model_output(
    formal_skill_id: str, model_output: dict[str, Any]
) -> dict[str, Any]:
    """Apply only deterministic format conversion before a strict Skill check.

    This never creates a new category or changes the supplied source identity.
    It converts common representation differences (for example Chinese labels or
    a list of audience reactions) to the Skill's already-fixed vocabulary.
    """
    if formal_skill_id == "competitor_breakdown_structural_v13":
        return _normalize_competitor_breakdown_structural_compact_output(model_output)
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
    # Version 4 deliberately has no secondary type, generic audience summary,
    # or question-expansion fields.  Removing these retired normalizations keeps
    # the atomic output schema exact rather than silently preserving old routes.
    for retired_key in (
        "secondary_expression_form", "audience_responses", "subject_description", "question_expansions",
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
        "structural_role", "statement", "setup", "pull_forward", "cannot_infer",
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


def parse_model_json(output_text: str) -> dict[str, Any]:
    text = output_text.strip()
    candidates = [text]
    # Some OpenAI-compatible providers wrap an otherwise valid JSON object in a
    # complete markdown code fence.  This is a delivery-format difference, not
    # a business instruction.  Accept only that exact wrapper; prose before or
    # after JSON remains invalid and cannot enter a formal workflow.
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            candidates.append("\n".join(lines[1:-1]).strip())
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
        raise FormalSkillValidationError("payload has unexpected fields")
    for key, value in payload.items():
        if key in properties:
            spec = properties[key]
            spec = {"type": spec} if isinstance(spec, str) else spec
            if spec.get("type") == "string" and not isinstance(value, str):
                raise FormalSkillValidationError(f"{key} must be a string")
            if spec.get("type") == "array" and not isinstance(value, list):
                raise FormalSkillValidationError(f"{key} must be an array")
            if spec.get("type") == "object" and not isinstance(value, dict):
                raise FormalSkillValidationError(f"{key} must be an object")
            if "enum" in spec and value not in spec["enum"]:
                raise FormalSkillValidationError(f"{key} is not an allowed value")


def validate_source_to_topic_output_semantics(input_payload: dict[str, Any], output_payload: dict[str, Any]) -> None:
    if not set(output_payload["supporting_evidence"]).issubset(set(input_payload["source_evidence_items"])):
        raise FormalSkillValidationError("candidate evidence must come from supplied source material")
    if output_payload["topic_status"] == "no_result":
        return
    if not all(output_payload[key] for key in ("candidate_topic", "topic_angle", "core_question")):
        raise FormalSkillValidationError("candidate requires a topic, angle and core question")


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
    if output_payload["schema_version"] not in {"competitor_breakdown.output.v14"}:
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
    domain_pack_path = ROOT / "config" / "domain_packs" / "music_entertainment.yaml"
    try:
        domain_pack = yaml.safe_load(domain_pack_path.read_text(encoding="utf-8")) or {}
        allowed_lens_ids = {str(item.get("id") or "") for item in domain_pack.get("competitor_breakdown_lenses") or []}
    except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise FormalSkillValidationError("competitor breakdown domain lenses are unavailable") from exc
    for lens in selected_lenses:
        if not isinstance(lens, dict) or set(lens) != {"lens_id", "source_evidence", "reading"}:
            raise FormalSkillValidationError("competitor breakdown selected lens is invalid")
        if str(lens["lens_id"] or "") not in allowed_lens_ids:
            raise FormalSkillValidationError("competitor breakdown selected lens is outside the current domain pack")
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


def validate_competitor_breakdown_structural_output_semantics(
    input_payload: dict[str, Any], output_payload: dict[str, Any]
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
    if output_payload["schema_version"] != "competitor_breakdown.output.v13":
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

    def citations(value: Any, *, label: str, prefix: str, source: set[str], minimum: int = 1) -> set[str]:
        if not isinstance(value, list) or len(value) < minimum:
            raise FormalSkillValidationError(f"spoken breakdown {label} lacks numbered evidence")
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
        output_payload["content_type_evidence"], label="content type", prefix="P", source=transcript_segments
    )
    structure_assessment = output_payload["structure_assessment"]
    if not isinstance(structure_assessment, dict) or set(structure_assessment) != {"source_evidence", "level", "statement"}:
        raise FormalSkillValidationError("spoken breakdown structure assessment is invalid")
    assessment_level = structure_assessment["level"]
    if assessment_level not in {"simple", "distinct", "unclear"}:
        raise FormalSkillValidationError("spoken breakdown structure assessment level is invalid")
    used_source_ids.update(
        citations(
            structure_assessment["source_evidence"], label="structure assessment", prefix="P", source=transcript_segments
        )
    )
    prose(structure_assessment["statement"], label="structure assessment")
    progression = output_payload["spoken_progression"]
    if not isinstance(progression, list) or (not progression and assessment_level != "simple"):
        raise FormalSkillValidationError("spoken breakdown needs a spoken progression")
    if output_payload["expression_form"] == "list" and assessment_level == "simple" and progression:
        raise FormalSkillValidationError(
            "a simple list must keep repeated item delivery in recurring patterns, not spoken progression"
        )
    expected_sequence = 1
    previous_position = -1
    for item in progression:
        if not isinstance(item, dict) or set(item) != {"sequence", "source_evidence", "spoken_action", "structural_role"}:
            raise FormalSkillValidationError("spoken breakdown progression stage is invalid")
        if item["sequence"] != expected_sequence:
            raise FormalSkillValidationError("spoken breakdown progression sequence must be continuous")
        expected_sequence += 1
        ids = citations(item["source_evidence"], label="progression stage", prefix="P", source=transcript_segments)
        used_source_ids.update(ids)
        prose(item["spoken_action"], label="progression spoken action")
        prose(item["structural_role"], label="progression structural role")
        first_position = min(int(source_id[1:]) for source_id in ids)
        if first_position < previous_position:
            raise FormalSkillValidationError("spoken breakdown progression must follow the source order")
        previous_position = first_position

    # A person story may legitimately contain several turns, so there is no
    # fixed stage limit.  But a run made mostly of generic "tell the next life
    # event" wording is a biography recap, not a record of spoken structure.
    # This is deliberately checked at the formal boundary rather than left as
    # a prompt-only preference.
    if output_payload["expression_form"] in {"story", "profile"} and len(progression) >= 3:
        generic_biography_action = re.compile(
            r"^(?:\u53e3\u64ad)?(?:\u4ece.*?\u8bb2\u8d77|\u8bb2\u8ff0|\u8be6\u8ff0|\u53d9\u8ff0|\u4ecb\u7ecd|\u56de\u987e)"
        )
        generic_count = sum(
            1
            for item in progression
            if generic_biography_action.search(str(item.get("spoken_action") or "").strip())
        )
        if generic_count * 2 > len(progression):
            raise FormalSkillValidationError(
                "spoken breakdown story progression retells a biography instead of describing structural spoken actions"
            )

    if output_payload["expression_form"] == "list" and assessment_level != "simple" and len(progression) >= 3:
        def is_individual_list_item(stage: dict[str, Any]) -> bool:
            source_text = " ".join(
                str(evidence.get("text") or "")
                for evidence in stage["source_evidence"]
                if isinstance(evidence, dict)
            )
            text = f"{stage['spoken_action']} {source_text}"
            return bool(re.search(r"第\s*[一二三四五六七八九十百千万0-9]+\s*(?:首|个|名|位|场|段|期|名)", text))

        longest_item_run = 0
        current_item_run = 0
        for stage in progression:
            if is_individual_list_item(stage):
                current_item_run += 1
                longest_item_run = max(longest_item_run, current_item_run)
            else:
                current_item_run = 0
        if longest_item_run >= 3:
            raise FormalSkillValidationError(
                "spoken breakdown list progression restates individual entries instead of its spoken structure"
            )

    recurring = output_payload["recurring_evidence_patterns"]
    if not isinstance(recurring, list):
        raise FormalSkillValidationError("spoken breakdown recurring patterns are invalid")
    for item in recurring:
        if not isinstance(item, dict) or set(item) != {"source_evidence", "spoken_action"}:
            raise FormalSkillValidationError("spoken breakdown recurring pattern is invalid")
        used_source_ids.update(citations(item["source_evidence"], label="recurring pattern", prefix="P", source=transcript_segments))
        prose(item["spoken_action"], label="recurring pattern")

    reactions = output_payload["audience_reactions"]
    if not isinstance(reactions, list):
        raise FormalSkillValidationError("spoken breakdown audience reactions are invalid")
    for item in reactions:
        if not isinstance(item, dict) or set(item) != {"comment_evidence", "related_spoken_evidence", "observed_reaction"}:
            raise FormalSkillValidationError("spoken breakdown audience reaction is invalid")
        citations(item["comment_evidence"], label="audience reaction", prefix="C", source=comment_segments)
        related = item["related_spoken_evidence"]
        if not isinstance(related, list):
            raise FormalSkillValidationError("spoken breakdown audience reaction related speech is invalid")
        if related:
            used_source_ids.update(citations(related, label="audience reaction related speech", prefix="P", source=transcript_segments))
        prose(item["observed_reaction"], label="audience reaction")

    structure_grasp = output_payload["structure_grasp"]
    if not isinstance(structure_grasp, dict) or set(structure_grasp) != {"core", "tensions", "highlights"}:
        raise FormalSkillValidationError("structural breakdown structure grasp is invalid")
    core = structure_grasp["core"]
    if not isinstance(core, dict) or set(core) != {"source_evidence", "statement"}:
        raise FormalSkillValidationError("structural breakdown core is invalid")
    used_source_ids.update(citations(core["source_evidence"], label="structure core", prefix="P", source=transcript_segments))
    prose(core["statement"], label="structure core")
    for key, label in (("tensions", "structure tension"), ("highlights", "structure highlight")):
        items = structure_grasp[key]
        if not isinstance(items, list):
            raise FormalSkillValidationError(f"structural breakdown {label} is invalid")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"source_evidence", "statement"}:
                raise FormalSkillValidationError(f"structural breakdown {label} item is invalid")
            used_source_ids.update(citations(item["source_evidence"], label=label, prefix="P", source=transcript_segments))
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
