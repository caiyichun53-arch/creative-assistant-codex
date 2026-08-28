from __future__ import annotations

import ast
import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.core.production.high_signal_policy import (
    COMMENT_LIKE_RATIO_THRESHOLD,
    FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
    FIRST_REGISTRATION_MAX_ITEMS,
    FIRST_REGISTRATION_REQUEST_LIMIT,
    FORMAL_D_ACTIVATION_COMPLETE_SEQUENCES,
    FORMAL_D_MULTI_METRIC_MULTIPLIER,
    FORMAL_D_ROLLING_WINDOW,
    FORMAL_D_SINGLE_METRIC_MULTIPLIER,
    FORMAL_GUARDRAIL,
    FORMAL_GUARDRAIL_DIGEST,
    HIGH_SIGNAL_POLICY_VERSION,
    HISTORICAL_MATURITY_DAYS,
    HISTORICAL_SINGLE_METRIC_MULTIPLIER,
    MATURE_HISTORY_ABSOLUTE_LIKE_FLOOR,
    MATURE_HISTORY_WINDOW_DAYS,
    MIN_RELIABLE_HISTORY_ITEMS,
)
from scripts.core.business_data.domain_labels import get_exploration_policy
from scripts.core.business_data.run_domain_search import TAG_CANDIDATE_LIKE_FLOOR
from scripts.core.runtime.runtime_storage import (
    RuntimeStorageError,
    assert_formal_runtime_storage,
    runtime_path,
)


ROOT = Path(__file__).resolve().parents[3]
EVENT_LOG_PATH = runtime_path("agent_platform", "business_runtime_guard_events.jsonl")
DAILY_OPERATIONS_CONTRACT_PATH = (
    ROOT / "config" / "business_guardrails" / "daily_operations.json"
)
CONTENT_PRODUCTION_CONTRACT_PATH = (
    ROOT / "config" / "business_guardrails" / "content_production.json"
)
SYSTEM_GOVERNANCE_CONTRACT_PATH = (
    ROOT / "config" / "business_guardrails" / "system_governance.json"
)
_RECORDED_PASS_KEYS: set[tuple[str, str, str, str]] = set()


class AtomicSkillRuntimeError(RuntimeError):
    """A formal model step was blocked by the atomic-Skill runtime guard."""


def _public_setting_binding_errors() -> list[str]:
    """Find production modules that use shared guardrails without importing them.

    Shared collection settings are defined once in high_signal_policy.py.  A
    missing import used to remain invisible until the first real daily item
    reached that code path, so this check fails before external collection.
    """
    policy_path = ROOT / "scripts" / "core" / "production" / "high_signal_policy.py"
    try:
        policy_tree = ast.parse(policy_path.read_text(encoding="utf-8"), filename=str(policy_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [f"shared setting source cannot be inspected: {exc}"]

    public_names: set[str] = set()
    for node in policy_tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper() and target.id != "ROOT":
                public_names.add(target.id)

    errors: list[str] = []
    production_dir = ROOT / "scripts" / "core" / "production"
    for path in sorted(production_dir.glob("*.py")):
        if path.name == "high_signal_policy.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append(f"{path.name}: source cannot be inspected: {exc}")
            continue

        loaded = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in public_names
        }
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "scripts.core.production.high_signal_policy":
                bound.update(alias.asname or alias.name for alias in node.names)
        missing = sorted(loaded - bound)
        if missing:
            errors.append(f"{path.name}: shared settings are not bound: {', '.join(missing)}")
    return errors


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_runtime_guard_event(
    *,
    event: str,
    outcome: str,
    details: dict[str, Any] | None = None,
    event_log_path: Path | None = None,
) -> None:
    event_details = details or {}
    if outcome == "passed":
        pass_key = (
            event,
            str(event_details.get("entrypoint") or ""),
            str(event_details.get("contract_version") or ""),
            str(event_details.get("contract_digest") or ""),
        )
        if pass_key in _RECORDED_PASS_KEYS:
            return
        _RECORDED_PASS_KEYS.add(pass_key)
    target_path = event_log_path or EVENT_LOG_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": _now(),
        "event": event,
        "outcome": outcome,
        "details": event_details,
    }
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def enforce_atomic_skill_runtime_guard(
    *, entrypoint: str, operation: str, event_log_path: Path | None = None
) -> dict[str, object]:
    """Fail closed while a business-model operation has not yet been converted to an atomic Skill."""
    contract = json.loads(SYSTEM_GOVERNANCE_CONTRACT_PATH.read_text(encoding="utf-8"))
    boundary = contract.get("skill_boundary") or {}
    blocked = set(contract.get("temporarily_blocked_unconverted_model_operations") or [])
    registered = set(contract.get("registered_atomic_model_operations") or [])
    errors: list[str] = []
    if boundary.get("business_model_judgment_requires_registered_atomic_skill") is not True:
        errors.append("atomic Skill rule is not active")
    if boundary.get("unregistered_business_model_operation_blocks_at_runtime") is not True:
        errors.append("unregistered business-model operation is not configured to block")
    if operation in blocked:
        errors.append("this formal model operation has not yet been converted to a registered atomic Skill")
    if operation not in registered:
        errors.append("this formal model operation is not registered as an atomic Skill")
    if errors:
        record_runtime_guard_event(
            event="atomic_skill_runtime",
            outcome="blocked",
            details={"entrypoint": entrypoint, "operation": operation, "errors": errors},
            event_log_path=event_log_path,
        )
        raise AtomicSkillRuntimeError(
            "atomic Skill runtime guard rejected execution: " + "; ".join(errors)
        )
    record_runtime_guard_event(
        event="atomic_skill_runtime",
        outcome="passed",
        details={"entrypoint": entrypoint, "operation": operation, "contract_version": contract["contract_version"]},
        event_log_path=event_log_path,
    )
    return {"valid": True, "contract_version": contract["contract_version"], "operation": operation}


def enforce_runtime_startup_guard(
    *,
    entrypoint: str,
    event_log_path: Path | None = None,
    scope: str = "full",
) -> dict[str, object]:
    """Validate the formal contract before startup.

    A test-only service may validate the same formal contract, but its guard
    evidence must stay in the test runtime area instead of writing the formal
    runtime log.  The formal database and contract are still checked read-only.

    The full scope remains the cross-stage startup guard used by cold-start
    paths. Formal daily operations use the daily scope so cold-start-only
    settings cannot block an otherwise valid daily run. Daily's own contract
    is checked separately by enforce_daily_operations_runtime_guard.
    """
    if scope not in {"full", "daily"}:
        raise ValueError("runtime startup guard scope must be 'full' or 'daily'")
    guard_event_log_path = event_log_path or EVENT_LOG_PATH
    try:
        from scripts.core.production.stage0_content_core import FORMAL_DB_PATH
        storage_receipt = assert_formal_runtime_storage(database_path=FORMAL_DB_PATH)
    except (RuntimeStorageError, ImportError) as exc:
        record_runtime_guard_event(
            event="formal_runtime_storage",
            outcome="blocked",
            details={"entrypoint": entrypoint, "error": str(exc)},
            event_log_path=guard_event_log_path,
        )
        raise RuntimeError(f"runtime storage guard rejected service startup: {exc}") from exc
    if scope == "daily":
        record_runtime_guard_event(
            event="service_startup",
            outcome="passed",
            details={
                "entrypoint": entrypoint,
                "runtime_scope": "daily",
                "formal_runtime_storage": "validated",
            },
            event_log_path=guard_event_log_path,
        )
        return {
            "valid": True,
            "runtime_scope": "daily",
            "formal_runtime_storage": storage_receipt,
        }
    collection = FORMAL_GUARDRAIL.get("collection", {})
    selection = FORMAL_GUARDRAIL.get("historical_selection", {})
    formal_d = FORMAL_GUARDRAIL.get("formal_d_selection", {})
    deep_breakdown = FORMAL_GUARDRAIL.get("deep_breakdown", {})
    errors = _public_setting_binding_errors()
    current_contract_path = (
        ROOT / "config" / "business_guardrails" / "competitor_registration.json"
    )
    actual = {
        "contract_version": FORMAL_GUARDRAIL.get("contract_version"),
        "collection_policy_version": FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
        "historical_selection_policy_version": HIGH_SIGNAL_POLICY_VERSION,
        "initial_request_limit": FIRST_REGISTRATION_REQUEST_LIMIT,
        "maximum_retained_items": FIRST_REGISTRATION_MAX_ITEMS,
        "recent_window_days": MATURE_HISTORY_WINDOW_DAYS,
        "maturity_days": HISTORICAL_MATURITY_DAYS,
        "minimum_reliable_mature_items": MIN_RELIABLE_HISTORY_ITEMS,
        "historical_single_metric_multiplier": HISTORICAL_SINGLE_METRIC_MULTIPLIER,
        "normal_account_like_floor": MATURE_HISTORY_ABSOLUTE_LIKE_FLOOR,
        "comment_like_ratio_threshold": COMMENT_LIKE_RATIO_THRESHOLD,
        "formal_d_activation": FORMAL_D_ACTIVATION_COMPLETE_SEQUENCES,
        "formal_d_window": FORMAL_D_ROLLING_WINDOW,
        "formal_d_single_metric_multiplier": FORMAL_D_SINGLE_METRIC_MULTIPLIER,
        "formal_d_multi_metric_multiplier": FORMAL_D_MULTI_METRIC_MULTIPLIER,
    }
    required = {
        "contract_version": "cold_start_guard_v14",
        "collection_policy_version": "first_registration_recent_90d_max50_v3",
        "historical_selection_policy_version": "mature_history_ratio_direct_like_floor_single_v13",
        "initial_request_limit": 50,
        "maximum_retained_items": 50,
        "recent_window_days": 90,
        "maturity_days": 7,
        "minimum_reliable_mature_items": 20,
        "historical_single_metric_multiplier": 3.0,
        "normal_account_like_floor": 20000,
        "comment_like_ratio_threshold": 0.2,
        "formal_d_activation": 20,
        "formal_d_window": 50,
        "formal_d_single_metric_multiplier": 2.0,
        "formal_d_multi_metric_multiplier": 1.6,
    }
    errors.extend(
        f"{key}: runtime={actual.get(key)!r}, required={value!r}"
        for key, value in required.items()
        if actual.get(key) != value
    )
    try:
        current_contract = json.loads(current_contract_path.read_text(encoding="utf-8"))
        current_canonical = json.dumps(
            current_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        current_digest = hashlib.sha256(current_canonical.encode("utf-8")).hexdigest()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        current_digest = ""
        errors.append(f"current cold-start contract cannot be read: {exc}")
    if current_digest != FORMAL_GUARDRAIL_DIGEST:
        errors.append("cold-start contract changed after this process loaded it")
    expected_breakdown_policy = {
        "one_record_per_completed_spoken_transcript": True,
        "content_type_is_fixed_classification_only": True,
        "content_type_lifecycle_is_separate_from_candidate_lifecycle": True,
        "discover_may_record_observed_types_without_approval": True,
        "classify_requires_domain_registry_frozen": True,
        "classify_accepts_canonical_ids_only": True,
        "classify_unknown_type_returns_no_match_or_out_of_scope": True,
        "frozen_registry_must_pass_config_validation_before_classify": True,
        "legacy_question_expansion_cannot_bypass_approved_projection": True,
        "expansion_signal_precedes_typed_lead": True,
        "unmatched_expansion_signal_skips_qualification": True,
        "qualified_typed_lead_preserves_existing_candidate_chain": True,
        "every_claim_requires_exact_transcript_evidence": True,
        "real_progression_is_not_forced_into_four_stages": True,
        "multiple_concrete_writing_methods_are_retained_per_video": True,
        "comments_describe_reactions_not_effectiveness": True,
        "performance_causality_claims_are_forbidden": True,
        "quality_citation_or_structure_failure_is_recorded_and_batch_continues": True,
        "failed_breakdowns_require_final_summary_before_downstream_progress": True,
        "provider_declared_incomplete_delivery_gets_one_post_batch_same_input_retry": True,
        "delivery_retry_never_changes_model_or_input": True,
        "initial_and_retry_delivery_records_are_both_retained": True,
        "repeated_delivery_interruption_remains_failed": True,
        "cold_start_final_confirmation_requires_completed_breakdown_or_explicit_source_exclusion": True,
    }
    errors.extend(
        f"deep_breakdown.{key}: runtime={deep_breakdown.get(key)!r}, required={value!r}"
        for key, value in expected_breakdown_policy.items()
        if deep_breakdown.get(key) != value
    )
    if collection.get("older_history_expansion") != "only_when_recent_mature_below_minimum":
        errors.append("older history expansion is not limited to insufficient recent mature history")
    if collection.get("extra_expansion_gates_forbidden") is not True:
        errors.append("unapproved older-history expansion gates are not forbidden")
    if selection.get("like_floor_is_common_gate") is not False:
        errors.append("like floor must not gate the direct comment-like-ratio path")
    if selection.get("like_floor_required_for_multiplier_paths") is not True:
        errors.append("like floor must gate the multiplier paths")
    if selection.get("comment_like_ratio_is_independent_channel") is not True:
        errors.append("comment-like ratio must be one independent qualifying path")
    if selection.get("two_entry_paths_are_or") is not True:
        errors.append("historical hit entries must use OR logic")
    if selection.get("maximum_selection_ratio") is not None:
        errors.append("an unapproved maximum selection ratio is configured")
    if selection.get("iqr_selection_forbidden") is not True:
        errors.append("IQR selection is not forbidden")
    registration = FORMAL_GUARDRAIL.get("registration", {})
    if registration.get("formal_step_receipt_binds_completed_artifact_set") is not True:
        errors.append("formal registration receipts do not bind the completed artifact set")
    if registration.get("resumed_step_may_not_reuse_stale_request_receipt") is not True:
        errors.append("resumed registration steps may reuse a stale request receipt")
    if any(
        registration.get(key) is not True
        for key in (
            "failure_pause_survives_service_restart",
            "restart_resumes_only_work_not_waiting_for_user",
            "explicit_user_resume_clears_only_selected_pause_state",
        )
    ):
        errors.append("cold-start failure pauses may be lost or bypassed on restart")
    runtime_alignment = FORMAL_GUARDRAIL.get("runtime_alignment", {})
    if any(
        runtime_alignment.get(key) is not True
        for key in (
            "current_contract_digest_checked_before_each_formal_operation",
            "contract_change_after_service_start_blocks_formal_operation",
            "automatic_deep_breakdown_runs_are_guarded",
            "human_formal_writes_are_guarded",
            "knowledge_mirror_formal_receipts_are_guarded",
        )
    ):
        errors.append("cold-start formal operations are not fully covered by the runtime guard")
    tag_library = FORMAL_GUARDRAIL.get("tag_library", {})
    if any(
        tag_library.get(key) is not True
        for key in (
            "deterministic_named_entity_registry_is_domain_configured",
            "pending_library_is_refiltered_when_rules_change",
            "filtered_named_entities_are_hard_deleted",
        )
    ):
        errors.append("cold-start named-entity tag filtering differs from the formal contract")
    if formal_d.get("activation_complete_sequences") != 20:
        errors.append("formal D activation does not require twenty complete sequences")
    if errors:
        record_runtime_guard_event(
            event="service_startup",
            outcome="blocked",
            details={"entrypoint": entrypoint, "errors": errors},
            event_log_path=guard_event_log_path,
        )
        raise RuntimeError(
            "runtime alignment guard rejected service startup: "
            + json.dumps(errors, ensure_ascii=False)
        )
    configured_limit = int(
        os.environ.get("COMPETITOR_FIRST_CRAWL_MAX_ITEMS")
        or str(FIRST_REGISTRATION_MAX_ITEMS)
    )
    if configured_limit != FIRST_REGISTRATION_MAX_ITEMS:
        raise RuntimeError(
            "runtime alignment guard rejected service startup: "
            "the configured first collection limit differs from the formal contract"
        )
    record_runtime_guard_event(
        event="service_startup",
        outcome="passed",
        details={
            "entrypoint": entrypoint,
            "contract_version": FORMAL_GUARDRAIL["contract_version"],
            "contract_digest": FORMAL_GUARDRAIL_DIGEST,
            "runtime_identity_receipt": storage_receipt,
        },
        event_log_path=guard_event_log_path,
    )
    return {
        "valid": True,
        "contract_version": FORMAL_GUARDRAIL["contract_version"],
        "contract_digest": FORMAL_GUARDRAIL_DIGEST,
    }


def enforce_daily_operations_runtime_guard(
    *,
    entrypoint: str,
    source_types: tuple[str, ...],
    daily_report_limit: int,
) -> dict[str, object]:
    """Block a real daily run when its executable values drift from the small daily contract."""
    raw = DAILY_OPERATIONS_CONTRACT_PATH.read_bytes()
    contract = json.loads(raw.decode("utf-8"))
    errors: list[str] = []
    if contract.get("contract_version") != "daily_operations_formal_v2":
        errors.append("daily contract version differs from the executable runtime guard")
    if contract.get("status") != "active":
        errors.append("daily contract is not active")
    schedule = contract.get("schedule") or {}
    runtime_alignment = contract.get("runtime_alignment") or {}
    discovery = contract.get("discovery") or {}
    tracking = contract.get("competitor_tracking") or {}
    ranking = contract.get("ranking") or {}
    tag_library = contract.get("tag_library") or {}
    if any(
        runtime_alignment.get(key) is not True
        for key in (
            "automatic_daily_run_is_guarded",
            "daily_human_formal_actions_are_guarded",
            "current_daily_contract_is_checked_at_operation_time",
            "mismatch_blocks_formal_operation",
        )
    ):
        errors.append("daily formal operations are not fully covered by the runtime guard")
    if set(source_types) != set(discovery.get("source_types") or []):
        errors.append("daily discovery source types differ from the formal contract")
    if discovery.get("daily_competitor_content_is_tracking_only") is not True:
        errors.append("daily competitor content is allowed to bypass the formal hit library")
    if int(discovery.get("tag_candidate_like_floor") or 0) != TAG_CANDIDATE_LIKE_FLOOR:
        errors.append("tag candidate like floor differs from the formal contract")
    if (
        discovery.get("question_expansion_requires_formal_parent") is not True
        or discovery.get("question_expansion_cannot_parent_question_expansion") is not True
        or discovery.get("question_expansion_generation_stage") != "competitor_breakdown"
        or discovery.get("question_expansion_primary_material") != "breakdown_transcript"
        or discovery.get("question_expansion_comments_role") != "supporting_audience_signal_only"
        or discovery.get("question_expansion_comments_are_rendered_to_model") is not True
        or discovery.get("question_expansion_comment_evidence_required_when_relevant") is not True
        or discovery.get("standalone_comment_question_expansion_forbidden") is not True
        or int(discovery.get("question_expansion_max_per_breakdown") or 0) != 3
    ):
        errors.append("question expansion must be generated from the breakdown and transcript")
    if (
        discovery.get("all_confirmed_sources_run_in_production") is not True
        or discovery.get("runtime_source_freeze_forbidden") is not True
    ):
        errors.append("a confirmed daily discovery source may be frozen at runtime")
    if (
        schedule.get("all_domains_share_one_schedule") is not True
        or schedule.get("newly_completed_competitor_joins_shared_task") is not True
    ):
        errors.append("daily automatic triggering differs from the formal contract")
    if (
        schedule.get("daily_run_identity") != "domain+business_date"
        or schedule.get("one_daily_run_per_domain_date") is not True
        or schedule.get("daily_runs_share_one_execution_slot") is not True
        or schedule.get("failed_or_stopped_daily_run_requires_user_resume") is not True
        or schedule.get("resume_reuses_daily_run_id") is not True
        or schedule.get("daily_run_completed_only_when_domain_work_completes") is not True
        or schedule.get("global_daily_lifecycle_forbidden") is not True
        or schedule.get("notification_state_does_not_determine_daily_lifecycle") is not True
    ):
        errors.append("daily run identity or lifecycle differs from the formal contract")
    if any(
        tracking.get(key) is not expected
        for key, expected in (
            ("material_preparation_and_breakdown_are_independent_states", True),
        )
    ):
        errors.append("daily hit retry behavior differs from the formal contract")
    if any(
        tracking.get(key) is not expected
        for key, expected in (
            ("all_active_confirmed_accounts", True),
            ("tracking_starts_after_each_formal_competitor_registration", True),
            ("tracking_does_not_wait_for_topic_or_creation", True),
            ("candidate_discovery_waits_for_domain_cold_start_completion", True),
        )
    ):
        errors.append("daily tracking entry boundary differs from the formal contract")
    if int(daily_report_limit) != int(ranking.get("daily_report_limit") or 0):
        errors.append("daily priority report limit differs from the formal contract")
    if ranking.get("daily_report_is_priority_view_only") is not True:
        errors.append("daily top ten is no longer declared as a priority view only")
    if ranking.get("daily_processing_limit_forbidden") is not True:
        errors.append("daily processing cap is not forbidden")
    if ranking.get("daily_selection_limit_forbidden") is not True:
        errors.append("daily selection cap is not forbidden")
    if tag_library.get("extraction_and_maintenance_use_llm") is not False:
        errors.append("tag extraction or maintenance is configured to call a model")
    if tag_library.get("no_automatic_pause_or_delete") is not True:
        errors.append("automatic tag pause or delete is not forbidden")
    digest = hashlib.sha256(raw).hexdigest()
    if errors:
        record_runtime_guard_event(
            event="daily_operations_runtime",
            outcome="blocked",
            details={"entrypoint": entrypoint, "errors": errors, "contract_digest": digest},
        )
        raise RuntimeError(
            "daily operations runtime guard rejected execution: "
            + json.dumps(errors, ensure_ascii=False)
        )
    record_runtime_guard_event(
        event="daily_operations_runtime",
        outcome="passed",
        details={
            "entrypoint": entrypoint,
            "contract_version": contract["contract_version"],
            "contract_digest": digest,
        },
    )
    return {
        "valid": True,
        "contract_version": contract["contract_version"],
        "contract_digest": digest,
    }


def enforce_manual_exploration_runtime_guard(
    *,
    entrypoint: str,
    domain_label: str,
    exploration_kind: str,
    action: str,
    work_count: int | None = None,
) -> dict[str, object]:
    """Block special exploration writes when their executable boundary drifts."""
    raw = DAILY_OPERATIONS_CONTRACT_PATH.read_bytes()
    contract = json.loads(raw.decode("utf-8"))
    human = contract.get("human_boundaries") or {}
    global_exploration = contract.get("special_exploration") or {}
    errors: list[str] = []
    if global_exploration.get("policy_source") != "domain_pack.exploration":
        errors.append("exploration policy source is not the domain pack")
    for key in (
        "global_fixed_item_count_is_forbidden",
        "global_platform_collector_is_forbidden",
        "global_collection_minimum_is_forbidden",
    ):
        if global_exploration.get(key) is not True:
            errors.append(f"global exploration boundary is inactive: {key}")
    kind_flags = {
        "person_exploration": "person_name_routes_to_person_exploration",
        "work_exploration": "single_work_routes_to_work_exploration",
        "playlist_exploration": "work_list_routes_to_inventory_exploration",
    }
    policy_key = {
        "person_exploration": "person",
        "work_exploration": "work",
        "playlist_exploration": "collection",
    }.get(exploration_kind)
    policy = {}
    if policy_key is not None:
        policy = get_exploration_policy(domain_label).get(policy_key) or {}
        if policy.get("enabled") is not True:
            errors.append(f"{domain_label} does not enable {policy_key} exploration")
        if not str(policy.get("material_route") or "").strip():
            errors.append(f"{domain_label} has no material route for {policy_key} exploration")
    if exploration_kind not in kind_flags:
        errors.append("unsupported special exploration kind")
    elif human.get(kind_flags[exploration_kind]) is not True:
        errors.append("special exploration routing differs from the contract")
    for key in (
        "exploration_materials_are_retained_through_core",
        "exploration_has_no_per_material_human_confirmation",
        "exploration_stops_only_for_final_direction",
        "confirmed_exploration_direction_immediately_generates_research_plan",
    ):
        if human.get(key) is not True:
            errors.append(f"special exploration boundary is inactive: {key}")
    if action not in {
        "route",
        "collect_person_materials",
        "record_material",
        "complete_collection",
        "confirm_direction",
    }:
        errors.append("unsupported special exploration action")
    if exploration_kind == "person_exploration" and action == "collect_person_materials":
        if policy.get("material_route") != "music_audience":
            errors.append(
                "this person exploration has no dedicated platform collector; use the generic retained-material route"
            )
        else:
            minimum = int(policy.get("representative_item_minimum") or 0)
            maximum = int(policy.get("representative_item_maximum") or 0)
            if work_count is None or not minimum <= int(work_count) <= maximum:
                errors.append(
                    "person exploration representative item count differs from the domain rule"
                )
            if int(policy.get("audience_material_view_limit") or 0) <= 0:
                errors.append("dedicated audience collector has no positive view limit")
            if policy.get("platform_sessions_are_separate_and_reused_per_scan") is not True:
                errors.append("dedicated audience collector session boundary is inactive")
            if policy.get("platform_failure_is_reported_without_substitution_or_automatic_retry") is not True:
                errors.append("dedicated audience collector failure boundary is inactive")
    if exploration_kind == "playlist_exploration" and action == "route":
        minimum = int(policy.get("minimum_items") or 0)
        if minimum and (work_count is None or int(work_count) < minimum):
            errors.append("collection exploration does not meet the domain item minimum")
    digest = hashlib.sha256(raw).hexdigest()
    if errors:
        record_runtime_guard_event(
            event="manual_exploration_runtime",
            outcome="blocked",
            details={
                "entrypoint": entrypoint,
                "domain_label": domain_label,
                "exploration_kind": exploration_kind,
                "action": action,
                "errors": errors,
                "contract_digest": digest,
            },
        )
        raise RuntimeError(
            "manual exploration runtime guard rejected execution: "
            + json.dumps(errors, ensure_ascii=False)
        )
    record_runtime_guard_event(
        event="manual_exploration_runtime",
        outcome="passed",
        details={
            "entrypoint": entrypoint,
            "domain_label": domain_label,
            "exploration_kind": exploration_kind,
            "action": action,
            "contract_version": contract["contract_version"],
            "contract_digest": digest,
        },
    )
    return {
        "valid": True,
        "contract_version": contract["contract_version"],
        "contract_digest": digest,
    }


def enforce_topic_intake_runtime_guard(
    *,
    entrypoint: str,
    domain_label: str,
    route: str,
    action: str,
) -> dict[str, object]:
    """Keep every topic input on the one declared intake boundary."""
    raw = CONTENT_PRODUCTION_CONTRACT_PATH.read_bytes()
    contract = json.loads(raw.decode("utf-8"))
    intake = contract.get("topic_intake") or {}
    routes = intake.get("routes") or {}
    errors: list[str] = []
    route_policy = routes.get(route)
    if not isinstance(route_policy, dict):
        errors.append(f"unsupported topic intake route: {route}")
        route_policy = {}
    allowed_actions = {
        "system_candidate_selected": {"select_candidate"},
        "direct_formal_topic": {"create_direct_formal_topic"},
        "user_unclear_input": {"record_unclear_input"},
        "person_exploration": {
            "record_exploration_source",
            "record_exploration_material",
            "complete_exploration_material",
            "confirm_exploration_direction",
        },
        "single_object_exploration": {
            "record_exploration_source",
            "record_exploration_material",
            "complete_exploration_material",
            "confirm_exploration_direction",
        },
        "object_collection_exploration": {
            "record_exploration_source",
            "record_exploration_material",
            "complete_exploration_material",
            "confirm_exploration_direction",
        },
    }
    if action not in allowed_actions.get(route, set()):
        errors.append(f"action {action} is not allowed for topic intake route {route}")
    if action in {"select_candidate", "create_direct_formal_topic"}:
        if route_policy.get("formal_topic_created") is not True:
            errors.append("this topic intake route cannot create a formal topic")
    if action == "confirm_exploration_direction":
        if route_policy.get("human_confirmation") != "final_direction_only":
            errors.append("exploration must stop at its final direction confirmation")
        if route_policy.get("research_plan") != "after_final_direction":
            errors.append("exploration direction must lead to its declared research-plan boundary")
    if action == "record_unclear_input" and route_policy.get("formal_topic_created") is not False:
        errors.append("unclear input cannot become a formal topic at intake")
    digest = hashlib.sha256(raw).hexdigest()
    if errors:
        record_runtime_guard_event(
            event="topic_intake_runtime",
            outcome="blocked",
            details={
                "entrypoint": entrypoint,
                "domain_label": domain_label,
                "route": route,
                "action": action,
                "errors": errors,
                "contract_digest": digest,
            },
        )
        raise RuntimeError(
            "topic intake runtime guard rejected execution: "
            + json.dumps(errors, ensure_ascii=False)
        )
    record_runtime_guard_event(
        event="topic_intake_runtime",
        outcome="passed",
        details={
            "entrypoint": entrypoint,
            "domain_label": domain_label,
            "route": route,
            "action": action,
            "contract_version": contract["contract_version"],
            "contract_digest": digest,
        },
    )
    return {
        "valid": True,
        "contract_version": contract["contract_version"],
        "contract_digest": digest,
    }


def enforce_content_production_runtime_guard(
    *,
    entrypoint: str,
    current_node: str,
    current_status: str,
) -> dict[str, object]:
    """Block formal content actions when the executable chain drifts from its small contract."""
    raw = CONTENT_PRODUCTION_CONTRACT_PATH.read_bytes()
    contract = json.loads(raw.decode("utf-8"))
    required_sequence = [
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
        "audio_production",
        "audio_review",
        "text_and_audio_output_only",
    ]
    executable_nodes = {
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
        "user_final_confirmation",
        "audio_production",
        "audio_review",
        "text_and_audio_output_only",
    }
    executable_statuses = {
        "not_started",
        "processing",
        "awaiting_human_review",
        "approved",
        "awaiting_user_action",
        "failed",
        "completed",
    }
    errors: list[str] = []
    if contract.get("status") != "active":
        errors.append("content-production contract is not active")
    if contract.get("formal_sequence") != required_sequence:
        errors.append("formal content sequence differs from the approved contract")
    runtime_alignment = contract.get("runtime_alignment") or {}
    if any(value is not True for value in runtime_alignment.values()):
        errors.append("content formal operations are not fully covered by the runtime guard")
    entry = contract.get("entry") or {}
    if entry.get("extra_topic_confirmation_forbidden") is not True:
        errors.append("an extra topic confirmation is no longer forbidden")
    content = contract.get("content") or {}
    if (
        content.get("new_experience_candidate_appears_only_when_specific_frozen_breakdowns_support_it")
        is not True
        or content.get("confirmed_experience_is_used_only_when_relevant_to_the_current_plan")
        is not True
    ):
        errors.append("experience candidate boundary differs from the contract")
    expected_human_gates = {
        "research_plan": True,
        "deep_research_result": "domain_workflow_mode",
        "content_plan": True,
        "formal_draft": "domain_workflow_mode",
        "final_review": True,
        "audio_review": True,
        "no_per_item_confirmation_inside_automatic_generation": True,
        "return_creates_new_version": True,
        "return_never_overwrites_approved_history": True,
    }
    if contract.get("human_gates") != expected_human_gates:
        errors.append("human confirmation points differ from the approved contract")
    research = contract.get("research") or {}
    if research.get("starts_only_after_research_plan_approval") is not True:
        errors.append("research is no longer blocked before plan approval")
    if (
        research.get(
            "video_audio_transcript_comments_and_video_links_are_not_formal_research_sources"
        )
        is not True
    ):
        errors.append("formal research source boundary differs from the contract")
    audio = contract.get("audio") or {}
    if (
        audio.get("starts_only_after_final_text_approval") is not True
        or audio.get("engine") != "local_voxcpm2"
        or audio.get("user_review_required") is not True
    ):
        errors.append("formal audio boundary differs from the contract")
    workflow_mode = contract.get("workflow_mode") or {}
    if (
        workflow_mode.get("source") != "domain_pack.workflow_mode"
        or set(workflow_mode.get("allowed_values") or [])
        != {"manual_guard", "mature_automatic"}
        or workflow_mode.get("changed_by") != "user_only"
        or workflow_mode.get("automatic_maturity_detection") is not False
        or workflow_mode.get("automatic_mode_switch") is not False
        or workflow_mode.get("automatic_rollback_to_manual_guard") is not False
    ):
        errors.append("domain workflow mode boundary differs from the contract")
    if current_node not in executable_nodes:
        errors.append(f"unsupported formal content node: {current_node}")
    if current_status not in executable_statuses:
        errors.append(f"unsupported formal content status: {current_status}")
    digest = hashlib.sha256(raw).hexdigest()
    if errors:
        record_runtime_guard_event(
            event="content_production_runtime",
            outcome="blocked",
            details={
                "entrypoint": entrypoint,
                "current_node": current_node,
                "current_status": current_status,
                "errors": errors,
                "contract_digest": digest,
            },
        )
        raise RuntimeError(
            "content production runtime guard rejected execution: "
            + json.dumps(errors, ensure_ascii=False)
        )
    record_runtime_guard_event(
        event="content_production_runtime",
        outcome="passed",
        details={
            "entrypoint": entrypoint,
            "current_node": current_node,
            "current_status": current_status,
            "contract_version": contract["contract_version"],
            "contract_digest": digest,
        },
    )
    return {
        "valid": True,
        "contract_version": contract["contract_version"],
        "contract_digest": digest,
    }
