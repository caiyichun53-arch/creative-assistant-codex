#!/usr/bin/env python
"""Small build-time guard for the current business contract.

It deliberately checks only the active rules.  Historical stage tests and
retired-summary assertions do not belong in the running project.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "business_guardrails" / "stage_registry.json"
CURRENT_STAGE_POINTER = ROOT / "execution" / "current_implementation_stage.json"
FORBIDDEN_COMMIT_PREFIXES = (
    ".codex/state/",
    ".stage_runtime/",
    "artifacts/validation/",
    "data/",
    "external_responses/",
    "logs/",
    "outputs/",
    "profiles/",
    "raw_external/",
    "sessions/",
    "validation/",
    "vault/",
)
FORBIDDEN_COMMIT_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".key",
    ".log",
    ".pem",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
FORBIDDEN_COMMIT_NAMES = {
    "auth.json",
    "credentials.json",
    "cookies.json",
    "secrets.json",
    "tokens.json",
}
FORBIDDEN_COMMIT_PATHS = {
    "config/live_gates.yaml",
    "config/settings.yaml",
}
SAFE_ENV_FILES = {".env.example", ".env.live-gates.example"}
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("contract must contain one object")
    return value


def _validate_current_cold_start() -> list[str]:
    errors: list[str] = []
    contract = _read_object(ROOT / "config" / "business_guardrails" / "competitor_registration.json")
    required_breakdown = {
        "one_record_per_completed_spoken_transcript",
        "content_type_is_fixed_classification_only",
        "question_expansion_check_required",
        "question_expansion_may_be_empty",
        "question_expansion_requires_source_anchor",
        "question_expansion_requires_unresolved_increment",
        "question_expansion_requires_domain_gate",
        "question_expansion_requires_independent_topic",
        "question_expansion_requires_non_repetition",
        "observed_content_types_are_retrieval_only",
        "new_content_type_does_not_expand_domain",
        "content_type_lifecycle_is_separate_from_candidate_lifecycle",
        "discover_may_record_observed_types_without_approval",
        "classify_requires_domain_registry_frozen",
        "classify_accepts_canonical_ids_only",
        "classify_unknown_type_returns_no_match_or_out_of_scope",
        "frozen_registry_must_pass_config_validation_before_classify",
        "legacy_question_expansion_cannot_bypass_approved_projection",
        "expansion_signal_precedes_typed_lead",
        "unmatched_expansion_signal_skips_qualification",
        "qualified_typed_lead_preserves_existing_candidate_chain",
        "ineligible_source_blocks_formal_breakdown_write",
        "ineligible_source_does_not_pollute_observed_types",
        "every_claim_requires_exact_transcript_evidence",
        "real_progression_is_not_forced_into_four_stages",
        "story_or_profile_progression_rejects_biographical_recap_without_spoken_structure",
        "multiple_concrete_writing_methods_are_retained_per_video",
        "comments_describe_reactions_not_effectiveness",
        "comments_are_passed_to_breakdown_model",
        "comment_evidence_required_when_relevant",
        "performance_causality_claims_are_forbidden",
        "quality_citation_or_structure_failure_is_recorded_and_batch_continues",
        "failed_breakdowns_require_final_summary_before_downstream_progress",
        "formal_backlog_task_freezes_approved_source_snapshot_and_resumes_same_snapshot",
        "provider_declared_incomplete_delivery_gets_one_post_batch_same_input_retry",
        "delivery_retry_never_changes_model_or_input",
        "initial_and_retry_delivery_records_are_both_retained",
        "repeated_delivery_interruption_remains_failed",
        "cold_start_final_confirmation_requires_completed_breakdown_or_explicit_source_exclusion",
    }
    breakdown = contract.get("deep_breakdown")
    if not isinstance(breakdown, dict) or any(breakdown.get(key) is not True for key in required_breakdown):
        errors.append("deep-breakdown rules are incomplete")
    for legacy_key in ("summary_and_knowledge", "first_round_summary_after_all_breakdowns"):
        if legacy_key in contract:
            errors.append("retired cold-start summary rules remain")
    return errors


def _validate_system_governance() -> list[str]:
    errors: list[str] = []
    contract = _read_object(ROOT / "config" / "business_guardrails" / "system_governance.json")
    registered = contract.get("registered_atomic_model_operations") or []
    if not isinstance(registered, list):
        return ["registered atomic Skill operations are incomplete"]
    try:
        from scripts.core.production.business_runtime_guard import _public_setting_binding_errors

        errors.extend(_public_setting_binding_errors())
    except (ImportError, OSError, UnicodeDecodeError, SyntaxError) as exc:
        errors.append("shared setting binding guard cannot run: " + str(exc))
    for skill_id in registered:
        directory = ROOT / "runtime_skills" / str(skill_id)
        required_files = {
            "skill.yaml",
            "binding.yaml",
            "input_schema.yaml",
            "output_schema.yaml",
            "prompt.md",
        }
        missing = sorted(name for name in required_files if not (directory / name).is_file())
        if missing:
            errors.append("atomic Skill implementation material is incomplete: " + str(skill_id))
            continue
        try:
            manifest = yaml.safe_load((directory / "skill.yaml").read_text(encoding="utf-8")) or {}
            binding = yaml.safe_load((directory / "binding.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            errors.append("atomic Skill implementation material cannot be read: " + str(skill_id))
            continue
        required_manifest = {"formal_skill_id", "skill_version", "route_name", "route_id"}
        required_binding = {
            "binding_name",
            "binding_version",
            "input_map",
            "output_map",
            "model_input_schema",
            "model_output_schema",
        }
        if (
            not isinstance(manifest, dict)
            or manifest.get("formal_skill_id") != skill_id
            or not required_manifest.issubset(manifest)
            or not isinstance(binding, dict)
            or not required_binding.issubset(binding)
        ):
            errors.append("atomic Skill implementation material is invalid: " + str(skill_id))
    return errors


def validate(stage: str) -> list[str]:
    errors: list[str] = []
    try:
        registry = _read_object(REGISTRY)
        stages = registry.get("stages")
        if not isinstance(stages, dict) or stage not in stages:
            return ["unknown implementation scope"]
        definition = stages[stage]
        if not isinstance(definition, dict):
            return ["implementation scope has no definition"]
        contract_name = str(definition.get("contract") or "")
        if contract_name and not (ROOT / contract_name).is_file():
            errors.append("current implementation contract is missing")
        for owned_path in definition.get("path_prefixes") or []:
            if not (ROOT / str(owned_path)).exists():
                errors.append(
                    "current implementation material is missing: "
                    + str(owned_path)
                )
        if stage == "cold_start":
            errors.extend(_validate_current_cold_start())
        if stage == "system_governance":
            errors.extend(_validate_system_governance())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read current business contract: {exc}")
    return errors


def _current_stage_scope() -> tuple[str, list[str]]:
    pointer = _read_object(CURRENT_STAGE_POINTER)
    registry = _read_object(REGISTRY)
    stage = str(pointer.get("stage") or "").strip()
    stages = registry.get("stages")
    definition = stages.get(stage) if isinstance(stages, dict) else None
    paths = definition.get("path_prefixes") if isinstance(definition, dict) else None
    if not stage or not isinstance(paths, list) or not paths:
        raise ValueError("current implementation stage has no valid path scope")
    normalized = [str(path).replace("\\", "/").strip("/") for path in paths if str(path).strip()]
    if not normalized:
        raise ValueError("current implementation stage has no path scope")
    return stage, normalized


def _allowed(path: str, paths: list[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in paths)


def _staged_paths() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "cannot read staged paths")
    return sorted({path.replace("\\", "/").strip() for path in result.stdout.splitlines() if path.strip()})


def _forbidden_commit_reason(path: str) -> str | None:
    normalized = path.replace("\\", "/").strip("/").lower()
    name = normalized.rsplit("/", 1)[-1]
    if any(normalized == prefix or normalized.startswith(prefix) for prefix in FORBIDDEN_COMMIT_PREFIXES):
        return "runtime or generated data path"
    if normalized in FORBIDDEN_COMMIT_PATHS:
        return "local configuration or credential path"
    if name.endswith(FORBIDDEN_COMMIT_SUFFIXES):
        return "database, log, or credential file"
    if name in FORBIDDEN_COMMIT_NAMES:
        return "authentication or credential file"
    if normalized == ".env" or (normalized.startswith(".env.") and name not in SAFE_ENV_FILES):
        return "local environment or credential file"
    if normalized.startswith("config/accounts/") and name != "example.yaml":
        return "account credential configuration"
    if normalized.startswith("config/domains/") and name != "example.yaml":
        return "domain credential configuration"
    return None


def _commit_boundary_errors() -> list[str]:
    stage, stage_paths = _current_stage_scope()
    staged = _staged_paths()
    outside_stage = [path for path in staged if not _allowed(path, stage_paths)]
    forbidden = [(path, _forbidden_commit_reason(path)) for path in staged]
    forbidden = [(path, reason) for path, reason in forbidden if reason]
    errors: list[str] = []
    if outside_stage:
        errors.append("current stage " + stage + " does not allow: " + ", ".join(outside_stage))
    if forbidden:
        errors.append(
            "forbidden sensitive or runtime artifacts: "
            + ", ".join(f"{path} ({reason})" for path, reason in forbidden)
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage")
    parser.add_argument("--phase")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--check-commit-boundary", action="store_true")
    args = parser.parse_args()
    if args.check_commit_boundary:
        try:
            errors = _commit_boundary_errors()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors = [f"cannot read commit boundary: {exc}"]
    else:
        if not args.stage or not args.phase:
            parser.error("--stage and --phase are required unless --check-commit-boundary is used")
        del args.phase, args.path
        errors = validate(args.stage)
    if errors:
        print("; ".join(errors))
        return 1
    print("current business contract is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
