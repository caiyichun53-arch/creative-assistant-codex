from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


GOAL_ID = "GOAL-V0.6.2-PRODUCTION-COMPLETION-01"
REQUIRED_PHASE_STATUSES = {
    "PHASE_5_BUSINESS_WORKFLOW_FOUNDATION_STATUS.yaml": "COMPLETED_PHASE5_CHECKPOINT",
    "PHASE_6_HERMES_WHITELIST_TOOL_STATUS.yaml": "COMPLETED_PHASE6_CHECKPOINT",
    "PHASE_7_SYNTHETIC_ACCEPTANCE_STATUS.yaml": "COMPLETED_PHASE7_CHECKPOINT",
}
PRODUCTION_TASK_NAMES = ("CreationAssistant_Daily", "CreationAssistant_Listener")
PILOT_APPROVAL_PATH = ROOT / "PHASE_8_REAL_NEW_DATA_APPROVAL.yaml"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.split("#", 1)[0].strip()
    return values


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return "__placeholder__" in lowered or lowered.startswith("your_") or lowered in {"xxx", "placeholder"}


def model_class(model: str | None) -> str:
    lowered = (model or "").lower()
    if "gpt" in lowered or "openai" in lowered:
        return "gpt"
    if "mimo" in lowered or "xiaomi" in lowered:
        return "mimo"
    if "deepseek" in lowered:
        return "deepseek"
    if not lowered:
        return "missing"
    return "other"


def phase_statuses() -> dict[str, Any]:
    statuses: dict[str, str] = {}
    for file_name in REQUIRED_PHASE_STATUSES:
        path = ROOT / file_name
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        statuses[file_name] = str(data.get("status") or "")
    return {
        "statuses": statuses,
        "passed": all(statuses[file_name] == expected for file_name, expected in REQUIRED_PHASE_STATUSES.items()),
    }


def registry_policy() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "BUSINESS_MODEL_ROUTE_REGISTRY.yaml").read_text(encoding="utf-8")) or {}
    defaults = data.get("provider_policy_defaults") or {}
    fallback = defaults.get("fallback_policy") or {}
    return {
        "provider_name": defaults.get("provider_name"),
        "live_model_port": defaults.get("live_model_port"),
        "fallback_enabled": any(
            fallback.get(key) is not False for key in ("dry_run_fallback", "fake_port_fallback", "cli_fallback")
        )
        or fallback.get("on_failure") != "fail_closed",
        "on_failure": fallback.get("on_failure"),
        "node_count": len(data.get("nodes") or []),
    }


def model_config_status() -> dict[str, Any]:
    env = read_env(ROOT / ".env.live-gates")
    model = env.get("MODEL_PROVIDER_MODEL")
    cls = model_class(model)
    return {
        "api_key_present": not is_placeholder(env.get("MODEL_PROVIDER_API_KEY")),
        "base_url_present": not is_placeholder(env.get("MODEL_PROVIDER_BASE_URL")),
        "model_present": not is_placeholder(model),
        "model_class": cls,
        "gpt_configured": cls == "gpt",
        "mimo_configured": cls == "mimo",
        "deepseek_configured": cls == "deepseek",
        "model_value_redacted": bool(model),
    }


def feishu_config_status() -> dict[str, Any]:
    app_env = read_env(ROOT / ".env")
    live_env = read_env(ROOT / ".env.live-gates")
    return {
        "app_id_present": not is_placeholder(app_env.get("FEISHU_APP_ID")),
        "app_secret_present": not is_placeholder(app_env.get("FEISHU_APP_SECRET")),
        "chat_id_present": not is_placeholder(app_env.get("FEISHU_CHAT_ID")),
        "user_open_id_present": not is_placeholder(app_env.get("FEISHU_USER_OPEN_ID")),
        "live_gate_app_id_present": not is_placeholder(live_env.get("FEISHU_APP_ID")),
        "live_gate_app_secret_present": not is_placeholder(live_env.get("FEISHU_APP_SECRET")),
        "live_gate_chat_id_present": not is_placeholder(live_env.get("FEISHU_CHAT_ID")),
        "event_verification_token_present": not is_placeholder(live_env.get("FEISHU_EVENT_VERIFICATION_TOKEN")),
    }


def pilot_source_status() -> dict[str, Any]:
    # Existing local competitor rows are intentionally not counted: Phase 8 requires newly approved data sources.
    if not PILOT_APPROVAL_PATH.exists():
        return {
            "approved_new_source_manifest_present": False,
            "manifest_valid": False,
            "manifest_errors": ["missing PHASE_8_REAL_NEW_DATA_APPROVAL.yaml"],
            "existing_legacy_sources_counted": False,
        }
    try:
        data = yaml.safe_load(PILOT_APPROVAL_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - report manifest parse issue without continuing to live work.
        return {
            "approved_new_source_manifest_present": True,
            "manifest_valid": False,
            "manifest_errors": [f"manifest parse failed: {type(exc).__name__}"],
            "existing_legacy_sources_counted": False,
        }
    errors = validate_pilot_approval(data)
    budgets = data.get("budgets") or {}
    return {
        "approved_new_source_manifest_present": True,
        "manifest_valid": not errors,
        "manifest_errors": errors,
        "approved_source_count": len(data.get("sources") or []),
        "domains": sorted({str(source.get("domain") or "") for source in data.get("sources") or [] if source.get("domain")}),
        "max_collection_accounts": budgets.get("max_collection_accounts"),
        "max_videos": budgets.get("max_videos"),
        "max_workflow_tasks": budgets.get("max_workflow_tasks"),
        "max_gpt_calls": budgets.get("max_gpt_calls"),
        "max_total_tokens": budgets.get("max_total_tokens"),
        "existing_legacy_sources_counted": False,
    }


def validate_pilot_approval(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != "phase8.real_new_data_approval.v1":
        errors.append("schema_version must be phase8.real_new_data_approval.v1")
    for key in ("approval_id", "approved_by", "approved_at", "purpose"):
        if is_placeholder(str(data.get(key) or "")):
            errors.append(f"{key} is required")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must contain at least one newly approved source")
    else:
        if len(sources) > 4:
            errors.append("sources must not exceed 4 entries for the small pilot")
        for index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                errors.append(f"sources[{index}] must be an object")
                continue
            for key in ("source_id", "platform", "domain", "account_ref"):
                if is_placeholder(str(source.get(key) or "")):
                    errors.append(f"sources[{index}].{key} is required")
            if source.get("platform") not in {"douyin", "netease_music", "other_approved_platform"}:
                errors.append(f"sources[{index}].platform must be an approved platform enum")
            if source.get("source_age") != "new_after_phase8_approval":
                errors.append(f"sources[{index}].source_age must be new_after_phase8_approval")
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        errors.append("budgets is required")
    else:
        limits = {
            "max_collection_accounts": 4,
            "max_videos": 20,
            "max_detail_fetches": 20,
            "max_workflow_tasks": 3,
            "max_gpt_calls": 12,
            "max_total_tokens": 60000,
        }
        for key, maximum in limits.items():
            value = budgets.get(key)
            if not isinstance(value, int) or value < 0:
                errors.append(f"budgets.{key} must be a non-negative integer")
            elif value > maximum:
                errors.append(f"budgets.{key} exceeds Phase 8 pilot cap {maximum}")
    safety = data.get("safety") or {}
    required_false = (
        "allow_old_database",
        "allow_old_vault",
        "allow_cold_backup",
        "allow_large_scale_collection",
        "allow_production_timers",
        "allow_fallback",
    )
    for key in required_false:
        if safety.get(key) is not False:
            errors.append(f"safety.{key} must be false")
    if safety.get("fresh_data_only") is not True:
        errors.append("safety.fresh_data_only must be true")
    return errors


def production_task_status() -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"checked": False, "reason": "non_windows_host", "tasks": {}, "all_disabled": False}
    script = (
        "$names=@('CreationAssistant_Daily','CreationAssistant_Listener');"
        "foreach($name in $names){"
        "$task=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;"
        "if($task){Write-Output ($name+'='+$task.State)}else{Write-Output ($name+'=missing')}}"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    tasks: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            tasks[key.strip()] = value.strip()
    return {
        "checked": completed.returncode == 0,
        "tasks": tasks,
        "all_disabled": all(tasks.get(name) in {"Disabled", "missing"} for name in PRODUCTION_TASK_NAMES),
    }


def verify_phase8_readiness() -> dict[str, Any]:
    phases = phase_statuses()
    registry = registry_policy()
    model = model_config_status()
    feishu = feishu_config_status()
    pilot = pilot_source_status()
    tasks = production_task_status()
    missing: list[str] = []
    if not model["gpt_configured"]:
        missing.append("MODEL_PROVIDER_MODEL must be set to the user-approved GPT model in .env.live-gates")
    if not model["api_key_present"]:
        missing.append("MODEL_PROVIDER_API_KEY must be configured in .env.live-gates")
    if not model["base_url_present"]:
        missing.append("MODEL_PROVIDER_BASE_URL must be configured in .env.live-gates")
    if not pilot["approved_new_source_manifest_present"]:
        missing.append("PHASE_8_REAL_NEW_DATA_APPROVAL.yaml with newly approved pilot sources is required")
    elif not pilot["manifest_valid"]:
        missing.append("PHASE_8_REAL_NEW_DATA_APPROVAL.yaml must pass pilot manifest validation")
    if not (feishu["app_id_present"] and feishu["app_secret_present"] and feishu["chat_id_present"]):
        missing.append(".env must contain FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_CHAT_ID")
    if not feishu["event_verification_token_present"]:
        missing.append("FEISHU_EVENT_VERIFICATION_TOKEN is required for real inbound Feishu event validation")
    if not tasks["all_disabled"]:
        missing.append("production scheduled tasks must be disabled")
    if registry["fallback_enabled"]:
        missing.append("business model route registry fallback policy must remain disabled")
    if not phases["passed"]:
        missing.append("Phase 5, 6 and 7 completion statuses must remain completed")

    return {
        "schema_version": "phase8.readiness.v1",
        "goal": GOAL_ID,
        "status": "READY_FOR_LIVE_PHASE8" if not missing else "WAITING_FOR_USER_INPUT",
        "phase_statuses": phases,
        "registry_policy": registry,
        "model_config": model,
        "feishu_config": feishu,
        "pilot_source_config": pilot,
        "production_tasks": tasks,
        "missing_items": missing,
        "safety": {
            "gpt_called": False,
            "deepseek_called": False,
            "real_platform_collection_started": False,
            "real_feishu_message_sent": False,
            "fallback_or_auto_downgrade_added": False,
            "secrets_redacted": True,
        },
    }


def main() -> int:
    result = verify_phase8_readiness()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "READY_FOR_LIVE_PHASE8" else 2


if __name__ == "__main__":
    raise SystemExit(main())
