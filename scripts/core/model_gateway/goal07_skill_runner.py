from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import content_hash


class SkillContractError(RuntimeError):
    pass


FORBIDDEN_PORTABLE_TOKENS = (
    "sqlite",
    "database",
    "table",
    "orm",
    "host_uuid",
    "trace_root",
    "trace_version",
    "command_receipt",
    "audit_event",
    "current_version_id",
    "formal_status",
    "state_transition",
    "db_write",
)


@dataclass(frozen=True)
class PortableSkillSpec:
    skill_name: str
    skill_version: str
    route_name: str
    prompt_template: str
    required_input_keys: tuple[str, ...]
    output_contract: dict[str, Any]
    metadata: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "route_name": self.route_name,
            "prompt_template": self.prompt_template,
            "required_input_keys": list(self.required_input_keys),
            "output_contract": self.output_contract,
            "metadata": self.metadata or {},
        }

    @property
    def skill_hash(self) -> str:
        return content_hash(self.as_payload(), "goal07.portable_skill.v1")

    def validate_runtime_boundary(self) -> None:
        _assert_required("skill_name", self.skill_name)
        _assert_required("skill_version", self.skill_version)
        _assert_required("route_name", self.route_name)
        _assert_required("prompt_template", self.prompt_template)
        if not self.required_input_keys:
            raise SkillContractError("required_input_keys is required")
        _assert_no_forbidden_tokens(self.as_payload())

    def render_prompt(self, input_payload: dict[str, Any]) -> str:
        self.validate_runtime_boundary()
        missing = [key for key in self.required_input_keys if key not in input_payload]
        if missing:
            raise SkillContractError(f"missing skill input keys: {missing}")
        try:
            return self.prompt_template.format(**input_payload)
        except KeyError as exc:
            raise SkillContractError(f"prompt references missing input key: {exc}") from exc


def _assert_required(field_name: str, value: str) -> None:
    if not value:
        raise SkillContractError(f"{field_name} is required")


def _assert_no_forbidden_tokens(value: Any) -> None:
    text = str(value).lower()
    for token in FORBIDDEN_PORTABLE_TOKENS:
        if _contains_token(text, token):
            raise SkillContractError(f"portable contract leaks forbidden token: {token}")


def _contains_token(text: str, token: str) -> bool:
    if "_" in token:
        return token in text
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None
