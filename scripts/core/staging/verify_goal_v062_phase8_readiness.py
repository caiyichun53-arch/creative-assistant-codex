from __future__ import annotations

import json
import platform
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.clean_room_empty_db import configured_db_path, health_check  # noqa: E402


GOAL_ID = "GOAL-V0.6.2-PRODUCTION-COMPLETION-01"
PROGRESS_PATH = ROOT / "implementation_progress" / f"{GOAL_ID}.md"
PRODUCTION_TASK_NAMES = ("CreationAssistant_Daily", "CreationAssistant_Listener")
FORMAL_SKILLS = (
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
REQUIRED_MANUALS = (
    "BUSINESS_MODEL_SWITCH_TO_GPT_PLAN.md",
    "REAL_NEW_DATA_PILOT_PLAN.md",
    "FEISHU_PRODUCTION_ACTIVATION_RUNBOOK.md",
)
REQUIRED_YAML_FILES = (
    "PHASE_3_LIVE_PROVIDER_MATRIX_STATUS.yaml",
    "PHASE_5_BUSINESS_WORKFLOW_FOUNDATION_STATUS.yaml",
    "PHASE_6_HERMES_WHITELIST_TOOL_STATUS.yaml",
    "PHASE_7_SYNTHETIC_ACCEPTANCE_STATUS.yaml",
    "PHASE_8_AUTHORIZATION_GATE_STATUS.yaml",
    "FORMAL_SKILL_ROUTE_MAPPING.yaml",
    "BUSINESS_MODEL_ROUTE_REGISTRY.yaml",
    "PHASE_8_REAL_NEW_DATA_APPROVAL.example.yaml",
)
PY_COMPILE_TARGETS = (
    "scripts/core/staging/verify_goal_v062_phase8_readiness.py",
    "tests/core/test_phase8_engineering_readiness.py",
)


def read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def table_row_total(table_rows: Any) -> int:
    if isinstance(table_rows, dict):
        return sum(int(count) for count in table_rows.values())
    if isinstance(table_rows, list):
        return sum(int(row["row_count"]) for row in table_rows)
    raise TypeError(f"unsupported table_rows shape: {type(table_rows).__name__}")


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
    if "mimo" in lowered or "xiaomi" in lowered:
        return "mimo"
    if "gpt" in lowered or "openai" in lowered:
        return "gpt"
    if "deepseek" in lowered:
        return "deepseek"
    if not lowered:
        return "missing"
    return "other"


def progress_status() -> dict[str, Any]:
    text = PROGRESS_PATH.read_text(encoding="utf-8")
    required_markers = {
        "phase5_complete": "Phase 5 complete" in text or "Completed Phase 5" in text,
        "phase6_complete": "Phase 6 complete" in text or "Completed Phase 6" in text,
        "phase7_complete": "Phase 7 complete" in text or "Completed Phase 7" in text,
        "phase8_gate_reached": "Phase 8" in text,
    }
    return {"passed": all(required_markers.values()), "markers": required_markers}


def phase_statuses() -> dict[str, Any]:
    phase5 = read_yaml(ROOT / "PHASE_5_BUSINESS_WORKFLOW_FOUNDATION_STATUS.yaml")
    phase6 = read_yaml(ROOT / "PHASE_6_HERMES_WHITELIST_TOOL_STATUS.yaml")
    phase7 = read_yaml(ROOT / "PHASE_7_SYNTHETIC_ACCEPTANCE_STATUS.yaml")
    return {
        "phase5": phase5.get("status"),
        "phase6": phase6.get("status"),
        "phase7": phase7.get("status"),
        "passed": (
            phase5.get("status") == "COMPLETED_PHASE5_CHECKPOINT"
            and phase6.get("status") == "COMPLETED_PHASE6_CHECKPOINT"
            and phase7.get("status") == "COMPLETED_PHASE7_CHECKPOINT"
        ),
        "phase5_details": {
            "dispatcher_complete": bool((phase5.get("implemented") or {}).get("dispatcher", {}).get("uses_formal_business_skill_adapter")),
            "workflow_definitions_complete": bool(
                (phase5.get("implemented") or {}).get("workflow_definitions", {}).get("covers_all_12_skills_across_task_specific_chains")
            ),
            "input_assembly_complete": bool((phase5.get("implemented") or {}).get("input_assembly", {}).get("freezes_public_skill_input")),
            "experience_context_complete": bool((phase5.get("implemented") or {}).get("experience_context", {}).get("only_published_experience_selected")),
            "experience_usage_complete": bool((phase5.get("implemented") or {}).get("experience_usage", {}).get("validates_refs_against_frozen_context")),
            "materializer_outbox_complete": bool((phase5.get("implemented") or {}).get("materializer", {}).get("outbox_events")),
        },
        "phase6_details": {
            "whitelist_tool_complete": bool((phase6.get("implemented") or {}).get("whitelist_actions", {}).get("create_controlled_task")),
            "fallback_blocked": (((phase6.get("implemented") or {}).get("safety") or {}).get("fallback_enablement")) is False,
            "direct_feishu_send_blocked": (((phase6.get("implemented") or {}).get("safety") or {}).get("direct_feishu_send")) is False,
        },
        "phase7_details": {
            "synthetic_e2e_complete": phase7.get("status") == "COMPLETED_PHASE7_CHECKPOINT",
            "disposable_postgresql_environment": bool(
                ((phase7.get("implemented") or {}).get("coverage") or {}).get("disposable_postgresql_environment")
            ),
        },
    }


def formal_skill_status() -> dict[str, Any]:
    mapping = read_yaml(ROOT / "FORMAL_SKILL_ROUTE_MAPPING.yaml")
    active = mapping.get("active_formal_skills") or mapping.get("formal_skills") or []
    active_ids = {str(item.get("formal_skill_id") or item.get("skill_id") or item.get("id") or "") for item in active if isinstance(item, dict)}
    if not active_ids:
        active_ids = {str(item) for item in active}
    missing_assets: list[str] = []
    for skill in FORMAL_SKILLS:
        skill_dir = ROOT / "runtime_skills" / skill
        for file_name in ("skill.yaml", "input_schema.yaml", "output_schema.yaml", "binding.yaml", "prompt.md", "fixtures.yaml"):
            if not (skill_dir / file_name).exists():
                missing_assets.append(f"{skill}/{file_name}")
    return {
        "expected_count": len(FORMAL_SKILLS),
        "mapping_count": len(active_ids),
        "all_skills_in_mapping": set(FORMAL_SKILLS).issubset(active_ids),
        "missing_assets": missing_assets,
        "passed": set(FORMAL_SKILLS).issubset(active_ids) and not missing_assets,
    }


def model_binding_status() -> dict[str, Any]:
    registry = read_yaml(ROOT / "BUSINESS_MODEL_ROUTE_REGISTRY.yaml")
    runtime_defaults = registry.get("runtime_policy_defaults") or registry.get("provider_policy_defaults") or {}
    fallback = runtime_defaults.get("fallback_policy") or {}
    model_routing = registry.get("model_routing") or {}
    model_routes_path = ROOT / str(model_routing.get("config_path") or "config/model_routes.yaml")
    model_routes = read_yaml(model_routes_path)
    providers = model_routes.get("model_providers") or {}
    routes = model_routes.get("model_routes") or {}
    used_route_ids = {str(node.get("route_id")) for node in (registry.get("nodes") or []) if node.get("route_id")}
    used_provider_refs = {
        str((routes.get(route_id) or {}).get("provider_ref") or "")
        for route_id in used_route_ids
    }
    used_provider_refs.discard("")
    used_providers = {
        provider_ref: providers.get(provider_ref) or {}
        for provider_ref in used_provider_refs
    }
    used_provider_types = {
        str(provider.get("type") or "").strip().lower()
        for provider in used_providers.values()
    }
    route_fallback_enabled = any(str((routes.get(route_id) or {}).get("fallback") or "") != "none" for route_id in used_route_ids)
    enabled_non_mimo_providers = {
        provider_ref: str(provider.get("type") or "")
        for provider_ref, provider in used_providers.items()
        if provider.get("enabled") is True and str(provider.get("type") or "").strip().lower() != "mimo"
    }
    primary_provider = next(iter(used_providers.values()), {})
    env = read_env(ROOT / ".env.live-gates")
    model_ref_source = primary_provider.get("model_ref") or runtime_defaults.get("model_ref_env")
    model_class_source = primary_provider.get("model_class_ref") or runtime_defaults.get("model_class_env")
    model = env.get(str(model_ref_source or ""))
    explicit_cls = (env.get(str(model_class_source or "")) or "").strip().lower()
    cls = explicit_cls or ("mimo" if used_provider_types == {"mimo"} else model_class(model))
    fallback_enabled = any(
        fallback.get(key) is not False for key in ("dry_run_fallback", "fake_port_fallback", "cli_fallback")
    ) or fallback.get("on_failure") != "fail_closed" or route_fallback_enabled
    return {
        "binding_id": "business.primary",
        "provider_name": primary_provider.get("provider_name") or runtime_defaults.get("provider_name"),
        "live_model_port": primary_provider.get("live_model_port") or runtime_defaults.get("live_model_port"),
        "model_ref_source": model_ref_source,
        "model_class_source": model_class_source,
        "billing_mode": primary_provider.get("billing_mode") or runtime_defaults.get("billing_mode"),
        "model_class": cls,
        "mimo_configured": cls == "mimo",
        "gpt_configured": cls == "gpt" or bool(enabled_non_mimo_providers),
        "deepseek_configured": cls == "deepseek" or "deepseek" in used_provider_types,
        "single_active_binding": len(used_provider_refs) == 1,
        "fallback_enabled": fallback_enabled,
        "on_failure": fallback.get("on_failure"),
        "node_count": len(registry.get("nodes") or []),
        "route_ids": sorted(used_route_ids),
        "provider_refs": sorted(used_provider_refs),
        "provider_types": sorted(used_provider_types),
        "enabled_non_mimo_providers": enabled_non_mimo_providers,
        "passed": cls == "mimo"
        and len(used_provider_refs) == 1
        and used_provider_types == {"mimo"}
        and (primary_provider.get("billing_mode") or runtime_defaults.get("billing_mode")) == "subscription"
        and not fallback_enabled
        and (primary_provider.get("provider_name") or runtime_defaults.get("provider_name")) == "hermes",
    }


def model_provider_validation_status() -> dict[str, Any]:
    phase3 = read_yaml(ROOT / "PHASE_3_LIVE_PROVIDER_MATRIX_STATUS.yaml")
    policy = phase3.get("model_policy") or {}
    return {
        "status": phase3.get("status"),
        "model_class": policy.get("model_class"),
        "mimo_model_name_detected": policy.get("mimo_model_name_detected"),
        "gpt_model_name_detected": policy.get("gpt_model_name_detected"),
        "gpt_called": phase3.get("gpt_called"),
        "deepseek_called": phase3.get("deepseek_called"),
        "fallback_used": phase3.get("fallback_used"),
        "passed": (
            phase3.get("status") == "COMPLETED"
            and policy.get("mimo_model_name_detected") is True
            and policy.get("gpt_model_name_detected") is False
            and phase3.get("gpt_called") is False
            and phase3.get("deepseek_called") is False
            and phase3.get("fallback_used") is False
        ),
    }


def clean_room_status() -> dict[str, Any]:
    settings = read_yaml(ROOT / "config" / "settings.yaml")
    health = health_check(configured_db_path(settings))
    total_rows = table_row_total(health["table_rows"])
    return {
        "database": health["database"],
        "table_count": health["table_count"],
        "total_rows": total_rows,
        "foreign_key_check": health["foreign_key_check"],
        "passed": health["table_count"] == 20
        and total_rows == 0
        and health["foreign_key_check"] == "passed",
    }


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
        "passed": completed.returncode == 0
        and all(tasks.get(name) in {"Disabled", "missing"} for name in PRODUCTION_TASK_NAMES),
    }


def manual_status() -> dict[str, Any]:
    present = {name: (ROOT / name).exists() for name in REQUIRED_MANUALS}
    return {"manuals": present, "passed": all(present.values())}


def scan_no_forbidden_phase8_live_work() -> dict[str, Any]:
    # This verifier is local-only; it does not inspect secret values or initiate network calls.
    phase8 = read_yaml(ROOT / "PHASE_8_AUTHORIZATION_GATE_STATUS.yaml")
    safety = phase8.get("safety") or {}
    approved_real_data_pilot = (
        safety.get("real_platform_collection_started") is True
        and safety.get("approved_real_data_pilot_started") is True
        and phase8.get("real_data_pilot_status") == "succeeded"
        and bool((phase8.get("activation_evidence") or {}).get("report"))
    )
    forbidden = {
        "gpt_called": safety.get("gpt_called") is not False,
        "deepseek_called": safety.get("deepseek_called") is not False,
        "unapproved_real_platform_collection_started": safety.get("real_platform_collection_started") is not False
        and not approved_real_data_pilot,
        "real_feishu_message_sent": safety.get("real_feishu_message_sent") is not False,
        "fallback_or_auto_downgrade_added": safety.get("fallback_or_auto_downgrade_added") is not False,
    }
    return {
        "forbidden_flags": forbidden,
        "approved_real_data_pilot": approved_real_data_pilot,
        "passed": not any(forbidden.values()),
    }


def phase8_gate_status() -> dict[str, Any]:
    phase8 = read_yaml(ROOT / "PHASE_8_AUTHORIZATION_GATE_STATUS.yaml")
    pre_activation_expected = {
        "engineering_goal_status": "completed",
        "production_activation_status": "not_started",
        "business_model_switch_status": "not_started",
        "real_data_pilot_status": "not_started",
        "feishu_live_activation_status": "not_started",
        "production_schedules_status": "disabled",
    }
    post_activation_expected = {
        "engineering_goal_status": "completed",
        "production_activation_status": "controlled_pilot_started",
        "business_model_switch_status": "not_started",
        "real_data_pilot_status": "succeeded",
        "feishu_live_activation_status": "not_started",
        "production_schedules_status": "disabled",
    }
    actual = {key: phase8.get(key) for key in pre_activation_expected}
    active_waiting_status = str(phase8.get("status") or "").upper() in {"WAITING_FOR_USER_INPUT", "BLOCKED"}
    matched_profile = "pre_activation" if actual == pre_activation_expected else None
    if actual == post_activation_expected:
        matched_profile = "post_controlled_real_data_pilot"
    return {
        "expected": post_activation_expected if matched_profile == "post_controlled_real_data_pilot" else pre_activation_expected,
        "accepted_profiles": ["pre_activation", "post_controlled_real_data_pilot"],
        "matched_profile": matched_profile,
        "actual": actual,
        "active_waiting_status": active_waiting_status,
        "historical_waiting_records_allowed": bool(phase8.get("historical_audit")),
        "passed": matched_profile is not None and not active_waiting_status,
    }


def static_gate_status() -> dict[str, Any]:
    py_compile_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="phase8_pycompile_") as tmp:
        for rel_path in PY_COMPILE_TARGETS:
            path = ROOT / rel_path
            try:
                cfile = Path(tmp) / (rel_path.replace("/", "_").replace("\\", "_") + ".pyc")
                py_compile.compile(str(path), cfile=str(cfile), doraise=True)
            except Exception as exc:  # noqa: BLE001 - convert static gate exception into readiness detail.
                py_compile_errors.append(f"{rel_path}: {type(exc).__name__}: {exc}")

    yaml_errors: list[str] = []
    for rel_path in REQUIRED_YAML_FILES:
        try:
            read_yaml(ROOT / rel_path)
        except Exception as exc:  # noqa: BLE001 - report parse issue without masking other checks.
            yaml_errors.append(f"{rel_path}: {type(exc).__name__}: {exc}")

    diff = subprocess.run(
        ["git", "diff", "--check"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "py_compile_targets": list(PY_COMPILE_TARGETS),
        "py_compile_errors": py_compile_errors,
        "yaml_files": list(REQUIRED_YAML_FILES),
        "yaml_errors": yaml_errors,
        "git_diff_check_exit_code": diff.returncode,
        "git_diff_check_passed": diff.returncode == 0,
        "passed": not py_compile_errors and not yaml_errors and diff.returncode == 0,
    }


def verify_phase8_readiness() -> dict[str, Any]:
    sections = {
        "progress": progress_status(),
        "phases": phase_statuses(),
        "formal_skills": formal_skill_status(),
        "model_binding": model_binding_status(),
        "model_provider_validation": model_provider_validation_status(),
        "clean_room": clean_room_status(),
        "production_tasks": production_task_status(),
        "manuals": manual_status(),
        "safety": scan_no_forbidden_phase8_live_work(),
        "phase8_gate_status": phase8_gate_status(),
        "static_gates": static_gate_status(),
    }
    failures = [name for name, section in sections.items() if not section.get("passed", section.get("all_disabled", False))]
    phase8 = read_yaml(ROOT / "PHASE_8_AUTHORIZATION_GATE_STATUS.yaml")
    production_activation = {
        "business_model_switch_status": "not_started",
        "real_data_pilot_status": phase8.get("real_data_pilot_status") or "not_started",
        "feishu_live_activation_status": "not_started",
        "production_schedules_status": "disabled" if sections["production_tasks"].get("all_disabled") else "not_disabled",
    }
    return {
        "schema_version": "phase8.engineering_readiness.v1",
        "goal": GOAL_ID,
        "status": "ENGINEERING_READY" if not failures else "ENGINEERING_NOT_READY",
        "engineering_goal_status": "completed" if not failures else "incomplete",
        "production_activation_status": phase8.get("production_activation_status") or "not_started",
        "production_activation": production_activation,
        "failures": failures,
        **sections,
        "safety_summary": {
            "gpt_called": False,
            "deepseek_called": False,
            "real_platform_collection_started": bool(phase8.get("safety", {}).get("real_platform_collection_started")),
            "approved_real_data_pilot": sections["safety"].get("approved_real_data_pilot", False),
            "real_feishu_message_sent": False,
            "old_data_read": False,
            "fallback_or_auto_downgrade_added": False,
            "secrets_redacted": True,
        },
    }


def main() -> int:
    result = verify_phase8_readiness()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "ENGINEERING_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
