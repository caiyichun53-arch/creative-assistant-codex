"""Stateless Hermes action registration for the existing business bridges.

This module is the project-side description and transport router for Hermes.
It does not own business state, validate cold-start business rules, or call
Core directly.  The only business handler registered here is the existing
``HermesColdStartAction`` bridge.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from scripts.agent_platform.hermes_cold_start_action import (
    HermesColdStartAction,
    collect_cold_start_parameters,
)
from scripts.core.production.stage0_content_core import StateTransitionError


COLD_START_ONBOARDING_TOOL_SPEC: dict[str, Any] = {
    "name": HermesColdStartAction.ACTION_NAME,
    "business_system": "Creation Assistant",
    "description": (
        "所属业务系统为 Creation Assistant。用户要新建冷启动时收集领域、一个自营账号和恰好20个对标账号；"
        "用户可查看、停止或恢复当前冷启动；状态来自真实运行记录。"
        "新建流程先调用preview，只有用户明确确认后才能调用confirm；"
        "停止保留已有结果，恢复复用原运行号，不重新收集账号、不创建新运行。"
        "本工具不自行选择账号、不替换账号、不判断业务资格、不创建平行状态。"
    ),
    "intent": "new_domain_cold_start",
    "trigger_conditions": [
        "用户要求开始、启动或配置一个新的领域冷启动",
        "用户明确提供或准备提供领域、自营账号和20个对标账号",
        "用户要求继续当前冷启动运行、继续上次冷启动或恢复冷启动",
        "用户要求查看当前冷启动状态或进度",
        "用户要求停止当前冷启动运行",
        "用户要确认当前 run 的标签、内容类型或生产边界候选",
    ],
    "do_not_trigger_for": [
        "候选、评分、研究、日常生产或反馈操作",
        "替用户自动寻找、补充或替换账号",
    ],
    "operations": {
        "preview": {
            "description": "收齐输入后调用，只做预检查，不创建正式运行。",
            "requires_explicit_user_confirmation": False,
        },
        "confirm": {
            "description": "仅在用户明确确认当前预检查结果后调用一次；普通聊天中的“确认”即可，不要求额外 Feishu 框架事件。",
            "requires_explicit_user_confirmation": True,
        },
        "status": {
            "description": "只读返回当前真实运行、阶段、更新时间、计数、当前动作和最近失败。",
            "requires_explicit_user_confirmation": False,
        },
        "stop": {
            "description": "停止当前真实运行并保留已有结果，不创建第二个运行。",
            "requires_explicit_user_confirmation": True,
        },
        "resume": {
            "description": "使用明确的 cold_start_id 查找同一个未完成冷启动并从当前阶段继续；没有可恢复运行时只提示创建新的冷启动。",
            "requires_explicit_user_confirmation": False,
        },
        "review_tags": {
            "description": "提交当前 run 的整套标签候选人工决定。",
            "requires_explicit_user_confirmation": True,
        },
        "review_content_types": {
            "description": "提交当前 run 的整套内容类型候选人工决定并冻结。",
            "requires_explicit_user_confirmation": True,
        },
        "review_domain_boundary": {
            "description": "提交当前 run 的整套生产边界候选人工决定，或在没有候选时提交明确的生产边界并冻结。",
            "requires_explicit_user_confirmation": True,
        },
    },
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["preview", "confirm", "status", "stop", "resume", "review_tags", "review_content_types", "review_domain_boundary"]},
            "domain_name": {"type": "string"},
            "owned_account": {"type": "object"},
            "competitor_accounts": {
                "type": "array",
                "description": "对标账号列表；收集阶段不足20个时只询问还缺少的数量。",
            },
            "reason": {"type": "string"},
            "cold_start_id": {"type": "string", "description": "status/stop/resume/review 时必须绑定的同一冷启动 run；不得省略或自动选择最新 run。"},
            "decisions": {"type": "array", "description": "review 操作提交的整套候选决定。"},
            "unknown_topic_rule": {"type": "object"},
            "explicit_boundary": {
                "type": "object",
                "description": "没有 boundary candidate 时，用户明确提交的生产边界；必须包含 in_boundary_principles、out_boundary_principles 和 unknown_topic_rule 的实际内容。",
            },
        },
        "required": ["operation"],
    },
    "context_schema": {
        "required": [
            "user_identity",
            "hermes_carrier_binding_id",
            "session_identity",
            "command_identity",
        ],
        "description": (
            "由 Hermes 运行环境提供，不由用户或模型填写。user_identity 仅是本轮传输上下文；当前固定 creator + Feishu "
            "路径属于单用户内部可信运行路径；它不构成 Creation Assistant 的业务操作者、角色或权限概念。"
        ),
    },
    "handler": "existing_cold_start_human_decision_adapter",
}

HERMES_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    HermesColdStartAction.ACTION_NAME: COLD_START_ONBOARDING_TOOL_SPEC,
}


TRUSTED_INTERNAL_GATEWAY_MARKER = "hermes_gateway_internal_v1"
TRUSTED_INTERNAL_GATEWAY_PLATFORM = "feishu"
TRUSTED_INTERNAL_GATEWAY_PROFILE = "creator"
TRUSTED_INTERNAL_GATEWAY_CARRIER = "hermes-creator-feishu-gateway"
TRUSTED_INTERNAL_GATEWAY_ENTRY_REF = "hermes://creator/feishu-gateway"


def registered_tools() -> dict[str, dict[str, Any]]:
    """Return the machine-readable project-side Hermes tool registration."""
    return deepcopy(HERMES_TOOL_REGISTRY)


def get_tool_spec(name: str) -> dict[str, Any]:
    tool_name = str(name or "").strip()
    try:
        return deepcopy(HERMES_TOOL_REGISTRY[tool_name])
    except KeyError as exc:
        raise StateTransitionError(f"Hermes tool is not registered: {tool_name}") from exc


def matches_new_cold_start_intent(user_message: str) -> bool:
    """Provide a narrow routing hint; business validation remains in Core."""
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    cold_start = "冷启动" in text or "cold start" in text
    if any(word in text for word in ("继续", "恢复", "continue", "resume")):
        return False
    start_request = any(word in text for word in ("开始", "启动", "新建", "配置", "start", "create"))
    return cold_start and start_request


def matches_resume_cold_start_intent(user_message: str) -> bool:
    """Provide a narrow routing hint for continuing an existing run."""
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    cold_start = "冷启动" in text or "cold start" in text
    resume_request = any(word in text for word in ("继续", "恢复", "continue", "resume"))
    return cold_start and resume_request


def matches_status_cold_start_intent(user_message: str) -> bool:
    """Route an explicit request to inspect the current cold-start run."""
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    cold_start = "冷启动" in text or "cold start" in text
    status_request = any(
        word in text
        for word in ("查看", "状态", "进度", "怎么样", "status", "progress")
    )
    return cold_start and status_request


def matches_stop_cold_start_intent(user_message: str) -> bool:
    """Route an explicit request to stop the current cold-start run."""
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    cold_start = "冷启动" in text or "cold start" in text
    stop_request = any(
        word in text
        for word in ("停止", "暂停", "终止", "stop", "pause", "terminate")
    )
    return cold_start and stop_request


@dataclass(frozen=True)
class HermesInvocationContext:
    """The real Hermes transport context carried into the existing bridge."""

    user_identity: str
    hermes_carrier_binding_id: str
    session_identity: str
    command_identity: str
    chat_id: str = ""
    thread_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HermesInvocationContext":
        if not isinstance(value, Mapping):
            raise StateTransitionError("Hermes invocation context must be an object")
        names = {
            "user_identity": "the transport session identity",
            "hermes_carrier_binding_id": "the Hermes carrier binding",
            "session_identity": "the session identity",
            "command_identity": "the command identity",
        }
        values: dict[str, str] = {}
        for key, label in names.items():
            text = str(value.get(key) or "").strip()
            if not text:
                raise StateTransitionError(f"Hermes invocation context requires {label}")
            values[key] = text
        target = value.get("notification_target")
        target_mapping = target if isinstance(target, Mapping) else {}
        chat_id = str(
            value.get("chat_id")
            or value.get("hermes_session_chat_id")
            or target_mapping.get("chat_id")
            or ""
        ).strip()
        thread_id = str(
            value.get("thread_id")
            or value.get("hermes_session_thread_id")
            or target_mapping.get("thread_id")
            or ""
        ).strip()
        return cls(**values, chat_id=chat_id, thread_id=thread_id)

    def notification_target(self) -> dict[str, str] | None:
        """Return only the dynamic Feishu target captured for this turn."""
        if not self.chat_id:
            return None
        target = {"platform": "feishu", "chat_id": self.chat_id}
        if self.thread_id:
            target["thread_id"] = self.thread_id
        return target

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "user_identity": self.user_identity,
            "hermes_carrier_binding_id": self.hermes_carrier_binding_id,
            "session_identity": self.session_identity,
            "command_identity": self.command_identity,
        }
        target = self.notification_target()
        if target is not None:
            result["notification_target"] = target
        return result


class HermesToolRouter:
    """Route one Hermes action to an already-created transport bridge."""

    def __init__(self, *, cold_start_action: HermesColdStartAction) -> None:
        self.cold_start_action = cold_start_action

    def dispatch(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        explicit_user_confirmation: bool = False,
        trusted_internal_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if str(tool_name or "").strip() != HermesColdStartAction.ACTION_NAME:
            raise StateTransitionError(f"Hermes tool is not registered: {tool_name}")
        if not isinstance(arguments, Mapping):
            raise StateTransitionError("Hermes tool arguments must be an object")

        operation = str(arguments.get("operation") or "").strip()
        configuration = {
            key: arguments.get(key)
            for key in ("domain_name", "owned_account", "competitor_accounts")
            if key in arguments
        }
        early_cold_start_id = str(arguments.get("cold_start_id") or "").strip() or None
        if operation in {"preview", "confirm"} and early_cold_start_id:
            raise StateTransitionError(
                f"Hermes {operation} cannot target an existing cold_start_id"
            )

        # Confirmation consumes the normalized preview kept by the current
        # transport session.  It must not rebuild business input from the
        # model's second tool call.
        intake_operation = operation in {"", "preview"}
        if intake_operation:
            collection = collect_cold_start_parameters(configuration)
            if not collection["complete_for_preview"]:
                response = {
                    "status": "needs_input",
                    "next_action": "ask_user",
                    "message": collection["message"],
                    "missing": collection["missing"],
                    "provided": collection["provided"],
                }
                try:
                    response["context"] = HermesInvocationContext.from_mapping(context).as_dict()
                except StateTransitionError:
                    # Incomplete intake is a user-facing question, not a business
                    # action; it must not require Hermes internal fields.
                    pass
                return response

        if operation not in {
            "preview", "confirm", "status", "stop", "resume",
            "review_tags", "review_content_types", "review_domain_boundary",
        }:
            raise StateTransitionError(
                "cold-start action requires preview, confirm, status, stop, resume or a review operation"
            )

        call_context = HermesInvocationContext.from_mapping(context)
        if call_context.hermes_carrier_binding_id != self.cold_start_action.carrier_binding_id:
            raise StateTransitionError("Hermes carrier binding does not match the registered action")

        trusted = parse_trusted_internal_gateway_context(trusted_internal_context)
        if trusted is not None and (
            trusted["user_identity"] != call_context.user_identity
            or trusted["session_identity"] != call_context.session_identity
            or trusted["command_identity"] != call_context.command_identity
            or trusted["carrier_binding_id"] != call_context.hermes_carrier_binding_id
        ):
            raise StateTransitionError(
                "trusted Hermes gateway context does not match the call context"
            )
        cold_start_id = str(arguments.get("cold_start_id") or "").strip() or None
        run_bound_operations = {
            "status", "stop", "resume",
            "review_tags", "review_content_types", "review_domain_boundary",
        }
        if operation in run_bound_operations and not cold_start_id:
            raise StateTransitionError(
                f"Hermes {operation} requires the exact cold_start_id"
            )
        if operation in {"preview", "confirm"} and cold_start_id:
            raise StateTransitionError(
                f"Hermes {operation} cannot target an existing cold_start_id"
            )
        if operation in {"confirm", "stop", "review_tags", "review_content_types", "review_domain_boundary"} and not explicit_user_confirmation:
            raise StateTransitionError(
                f"Hermes {operation} requires explicit user confirmation"
            )
        if operation in {
            "status", "stop", "resume",
            "review_tags", "review_content_types", "review_domain_boundary",
        }:
            result = self.cold_start_action.invoke(
                operation=operation,
                configuration={},
                actor=call_context.user_identity,
                session_ref=call_context.session_identity,
                command_id=call_context.command_identity,
                reason=str(arguments.get("reason") or "") or None,
                trusted_internal_context=trusted,
                notification_target=call_context.notification_target(),
                cold_start_id=cold_start_id,
                decisions=(
                    list(arguments.get("decisions"))
                    if isinstance(arguments.get("decisions"), list)
                    else None
                ),
                unknown_topic_rule=(
                    dict(arguments.get("unknown_topic_rule"))
                    if isinstance(arguments.get("unknown_topic_rule"), Mapping)
                    else None
                ),
                explicit_boundary=(
                    dict(arguments.get("explicit_boundary"))
                    if isinstance(arguments.get("explicit_boundary"), Mapping)
                    else None
                ),
                explicit_user_confirmation=explicit_user_confirmation,
            )
            return {
                "status": "completed",
                "tool": HermesColdStartAction.ACTION_NAME,
                "operation": operation,
                "result": result,
                "context": call_context.as_dict(),
            }

        # The chat turn that reaches confirm is the user's confirmation.  The
        # action retrieves the normalized preview from the same session.
        result = self.cold_start_action.invoke(
            operation=operation,
            configuration=configuration,
            actor=call_context.user_identity,
            session_ref=call_context.session_identity,
            command_id=call_context.command_identity,
            trusted_internal_context=trusted,
            notification_target=call_context.notification_target(),
            explicit_user_confirmation=explicit_user_confirmation,
        )
        return {
            "status": "completed",
            "tool": HermesColdStartAction.ACTION_NAME,
            "operation": operation,
            "result": result,
            "context": call_context.as_dict(),
        }


def parse_trusted_internal_gateway_context(
    value: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Validate the gateway-only context for the fixed Hermes + Feishu path.

    The gateway creates this from its runtime source; user text and tool
    arguments never supply it.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StateTransitionError("trusted Hermes gateway context must be an object")
    expected = {
        "marker": TRUSTED_INTERNAL_GATEWAY_MARKER,
        "platform": TRUSTED_INTERNAL_GATEWAY_PLATFORM,
        "profile": TRUSTED_INTERNAL_GATEWAY_PROFILE,
        "carrier_binding_id": TRUSTED_INTERNAL_GATEWAY_CARRIER,
        "entry_ref": TRUSTED_INTERNAL_GATEWAY_ENTRY_REF,
        "tool_action": HermesColdStartAction.ACTION_NAME,
    }
    normalized = {
        key: str(value.get(key) or "").strip()
        for key in (
            "marker",
            "platform",
            "profile",
            "carrier_binding_id",
            "entry_ref",
            "tool_action",
            "user_identity",
            "session_identity",
            "command_identity",
            "task_model_name",
            "task_model_provider",
            "task_model_base_url",
        )
    }
    for key, expected_value in expected.items():
        if normalized[key] != expected_value:
            raise StateTransitionError(
                f"trusted Hermes gateway context has invalid {key}"
            )
    if any(
        not normalized[key]
        for key in ("user_identity", "session_identity", "command_identity")
    ):
        raise StateTransitionError(
            "trusted Hermes gateway context requires user, session and command identities"
        )
    return normalized
