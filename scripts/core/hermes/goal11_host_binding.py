from __future__ import annotations

from scripts.core.host.production_host import (
    GOAL11_HOST_MESSAGE_SCOPE,
    GOAL11_RESPONSE_JOB_KIND,
    GOAL11_RESPONSE_SEND_SCOPE,
    GOAL11_RESPONSE_TOPIC,
    CodexBindingEvent,
    CodexHostBinding,
    FeishuBindingEvent,
    FeishuResponseDispatcher,
    FeishuSendResult,
    FeishuThinBinding,
    HostBindingError,
    ProductionHostBridge,
    ProductionHostDispatchResult,
    ProductionHostInboundMessage,
)


HermesHostBindingError = HostBindingError
HermesInboundMessage = ProductionHostInboundMessage
HermesDispatchResult = ProductionHostDispatchResult
HermesCoreBridge = ProductionHostBridge


__all__ = [
    "GOAL11_HOST_MESSAGE_SCOPE",
    "GOAL11_RESPONSE_JOB_KIND",
    "GOAL11_RESPONSE_SEND_SCOPE",
    "GOAL11_RESPONSE_TOPIC",
    "CodexBindingEvent",
    "CodexHostBinding",
    "FeishuBindingEvent",
    "FeishuResponseDispatcher",
    "FeishuSendResult",
    "FeishuThinBinding",
    "HermesCoreBridge",
    "HermesDispatchResult",
    "HermesHostBindingError",
    "HermesInboundMessage",
]
