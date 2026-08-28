"""Explicit credential-free model binding for isolated cold-start tests."""

from __future__ import annotations

from typing import Any


def test_task_model_resolver(
    trusted_context: dict[str, Any] | None,
    explicit_override: str | None,
) -> dict[str, Any]:
    context = trusted_context or {}
    current = str(context.get("task_model_name") or "isolated/model-a").strip()
    override = str(explicit_override or "").strip()
    return {
        "route_id": "business_analysis",
        "provider_ref": "isolated_test_provider",
        "provider_name": "hermes",
        "provider_type": "openai_compatible",
        "model_name": override or current,
        "endpoint": "https://isolated.invalid/v1",
        "source": "user_explicit_override" if override else "hermes_current_session",
        "explicit_override": bool(override),
    }
