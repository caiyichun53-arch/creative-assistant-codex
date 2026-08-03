from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
FORMAL_GUARDRAIL_PATH = (
    ROOT / "config" / "business_guardrails" / "competitor_registration.json"
)


def _load_formal_guardrail() -> tuple[dict[str, Any], str]:
    try:
        raw = FORMAL_GUARDRAIL_PATH.read_bytes()
        contract = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"formal competitor-registration guardrail is unavailable: {exc}") from exc
    if not isinstance(contract, dict):
        raise RuntimeError("formal competitor-registration guardrail must be an object")
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return contract, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


FORMAL_GUARDRAIL, FORMAL_GUARDRAIL_DIGEST = _load_formal_guardrail()
COLLECTION_GUARDRAIL = FORMAL_GUARDRAIL["collection"]
SELECTION_GUARDRAIL = FORMAL_GUARDRAIL["historical_selection"]
FORMAL_D_GUARDRAIL = FORMAL_GUARDRAIL["formal_d_selection"]


def _policy_scope_digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


COLLECTION_GUARDRAIL_DIGEST = _policy_scope_digest({
    "platform": FORMAL_GUARDRAIL["platform"],
    "collection_policy_version": FORMAL_GUARDRAIL["collection_policy_version"],
    "collection": COLLECTION_GUARDRAIL,
    "first_contact": {
        "historical_mature": FORMAL_GUARDRAIL["first_contact"]["historical_mature"],
        "transition": FORMAL_GUARDRAIL["first_contact"]["transition"],
    },
})
HISTORICAL_SELECTION_GUARDRAIL_DIGEST = _policy_scope_digest({
    "collection_guardrail_digest": COLLECTION_GUARDRAIL_DIGEST,
    "historical_selection_policy_version": FORMAL_GUARDRAIL[
        "historical_selection_policy_version"
    ],
    "historical_selection": SELECTION_GUARDRAIL,
})

HISTORICAL_METRICS = tuple(str(value) for value in SELECTION_GUARDRAIL["metrics"])
HIGH_SIGNAL_POLICY_VERSION = str(FORMAL_GUARDRAIL["historical_selection_policy_version"])
FIRST_REGISTRATION_COLLECTION_POLICY_VERSION = str(
    FORMAL_GUARDRAIL["collection_policy_version"]
)
FIRST_REGISTRATION_MAX_ITEMS = int(COLLECTION_GUARDRAIL["maximum_retained_items"])
FIRST_REGISTRATION_REQUEST_LIMIT = int(COLLECTION_GUARDRAIL["initial_request_limit"])
MATURE_HISTORY_WINDOW_DAYS = int(COLLECTION_GUARDRAIL["recent_window_days"])
HISTORICAL_MATURITY_DAYS = int(COLLECTION_GUARDRAIL["maturity_days"])
MIN_RELIABLE_HISTORY_ITEMS = int(COLLECTION_GUARDRAIL["minimum_reliable_mature_items"])

HISTORICAL_SINGLE_METRIC_MULTIPLIER = float(
    SELECTION_GUARDRAIL["single_metric_multiplier"]
)
HISTORICAL_MULTI_METRIC_MULTIPLIER = float(
    SELECTION_GUARDRAIL["multi_metric_multiplier"]
)
HISTORICAL_MINIMUM_MULTI_METRICS = int(
    SELECTION_GUARDRAIL["minimum_multi_metrics"]
)
MATURE_HISTORY_ABSOLUTE_LIKE_FLOOR = int(
    SELECTION_GUARDRAIL["normal_account_like_floor"]
)
SMALL_ACCOUNT_P90_PERCENTILE = float(
    SELECTION_GUARDRAIL["small_account_p90_percentile"]
)
COMMENT_LIKE_RATIO_THRESHOLD = float(
    SELECTION_GUARDRAIL["comment_like_ratio_threshold"]
)

FORMAL_D_ACTIVATION_COMPLETE_SEQUENCES = int(
    FORMAL_D_GUARDRAIL["activation_complete_sequences"]
)
FORMAL_D_ROLLING_WINDOW = int(
    FORMAL_D_GUARDRAIL["rolling_window_complete_sequences"]
)
FORMAL_D_SINGLE_METRIC_MULTIPLIER = float(
    FORMAL_D_GUARDRAIL["single_metric_multiplier"]
)
FORMAL_D_MULTI_METRIC_MULTIPLIER = float(
    FORMAL_D_GUARDRAIL["multi_metric_multiplier"]
)
FORMAL_D_MINIMUM_MULTI_METRICS = int(
    FORMAL_D_GUARDRAIL["minimum_multi_metrics"]
)


def _metric_value(item: dict[str, Any], metric: str) -> int:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    return max(0, int(metrics.get(metric) or 0))


def _quantile(values: list[int], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower]) * (1.0 - fraction) + float(ordered[upper]) * fraction


def _published_at(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("published_at") or 0))
    except (TypeError, ValueError):
        return 0


def normalize_first_registration_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_ids = [str(item.get("source_id") or "").strip() for item in items]
    if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("historical collection must contain unique non-empty source ids")
    return sorted(
        (dict(item) for item in items),
        key=lambda item: (_published_at(item), str(item.get("source_id") or "")),
        reverse=True,
    )


def select_first_registration_items(
    items: list[dict[str, Any]],
    *,
    evaluated_at: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    ordered = normalize_first_registration_items(items)
    window_start = evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
    mature_cutoff = evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
    recent = [item for item in ordered if _published_at(item) >= window_start]
    recent = recent[:FIRST_REGISTRATION_MAX_ITEMS]
    recent_mature_count = sum(
        window_start <= _published_at(item) <= mature_cutoff for item in recent
    )
    if recent_mature_count >= MIN_RELIABLE_HISTORY_ITEMS:
        return recent, False, recent_mature_count

    older_mature = [
        item
        for item in ordered
        if 0 < _published_at(item) < window_start
        and str(item.get("source_id")) not in {
            str(recent_item.get("source_id")) for recent_item in recent
        }
    ]
    needed = max(0, MIN_RELIABLE_HISTORY_ITEMS - recent_mature_count)
    capacity = max(0, FIRST_REGISTRATION_MAX_ITEMS - len(recent))
    backfill = older_mature[: min(needed, capacity)]
    retained = sorted(
        [*recent, *backfill],
        key=lambda item: (_published_at(item), str(item.get("source_id") or "")),
        reverse=True,
    )
    return retained, bool(backfill), recent_mature_count


def build_historical_collection_artifact(
    *,
    platform: str,
    account_source_ref: str,
    items: list[dict[str, Any]],
    raw_archive_ref: str,
    command_hash: str,
    output_hash: str,
    evaluated_at: int | None = None,
) -> dict[str, Any]:
    evaluated_at = int(evaluated_at or time.time())
    normalized, used_older_history_backfill, recent_mature_count = (
        select_first_registration_items(items, evaluated_at=evaluated_at)
    )
    return {
        "artifact_kind": "historical_material",
        "collection_policy_version": FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
        "formal_guardrail_digest": FORMAL_GUARDRAIL_DIGEST,
        "collection_guardrail_digest": COLLECTION_GUARDRAIL_DIGEST,
        "evaluated_at": evaluated_at,
        "platform": platform,
        "account_source_ref": account_source_ref,
        "items": normalized,
        "item_count": len(normalized),
        "requested_item_limit": FIRST_REGISTRATION_REQUEST_LIMIT,
        "collector_returned_item_count": len(items),
        "recent_mature_item_count": recent_mature_count,
        "used_older_history_backfill": used_older_history_backfill,
        "raw_archive_ref": raw_archive_ref,
        "command_hash": command_hash,
        "output_hash": output_hash,
        "runtime_guard": {
            "status": "passed",
            "maximum_first_registration_items": FIRST_REGISTRATION_MAX_ITEMS,
            "retained_items_are_newest_first": True,
            "older_history_expansion_target": MIN_RELIABLE_HISTORY_ITEMS,
        },
    }


def validate_historical_collection_artifact(artifact: dict[str, Any]) -> None:
    if artifact.get("artifact_kind") != "historical_material":
        raise ValueError("historical collection artifact kind is invalid")
    if artifact.get("collection_policy_version") != FIRST_REGISTRATION_COLLECTION_POLICY_VERSION:
        raise ValueError("historical collection does not use the current formal collection policy")
    artifact_scope_digest = artifact.get("collection_guardrail_digest")
    if (
        artifact_scope_digest is not None
        and artifact_scope_digest != COLLECTION_GUARDRAIL_DIGEST
    ):
        raise ValueError("historical collection does not match the current collection policy")
    legacy_digest = str(artifact.get("formal_guardrail_digest") or "")
    if len(legacy_digest) != 64:
        raise ValueError("historical collection lacks its original formal guardrail receipt")
    if artifact.get("platform") != FORMAL_GUARDRAIL["platform"]:
        raise ValueError("historical collection does not use the configured platform")
    if int(artifact.get("requested_item_limit") or 0) != FIRST_REGISTRATION_REQUEST_LIMIT:
        raise ValueError("first registration must begin with exactly 50 historical items")
    evaluated_at = int(artifact.get("evaluated_at") or 0)
    if evaluated_at <= 0:
        raise ValueError("historical collection lacks its policy evaluation time")
    items = artifact.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("historical collection must contain retained items")
    if len(items) > FIRST_REGISTRATION_MAX_ITEMS:
        raise ValueError("first registration retained more than 50 historical items")
    if int(artifact.get("item_count") or -1) != len(items):
        raise ValueError("historical collection item count is inconsistent")
    normalized = sorted(
        (dict(item) for item in items),
        key=lambda item: (_published_at(item), str(item.get("source_id") or "")),
        reverse=True,
    )
    if [item.get("source_id") for item in items] != [item.get("source_id") for item in normalized]:
        raise ValueError("historical collection is not newest-first")
    window_start = evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
    mature_cutoff = evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
    recent_mature_count = sum(
        window_start <= _published_at(item) <= mature_cutoff for item in items
    )
    older_count = sum(0 < _published_at(item) < window_start for item in items)
    if recent_mature_count >= MIN_RELIABLE_HISTORY_ITEMS and older_count:
        raise ValueError("older history was retained even though recent mature history was sufficient")
    if older_count > max(0, MIN_RELIABLE_HISTORY_ITEMS - recent_mature_count):
        raise ValueError("older-history expansion exceeded the mature-sample target")
    if bool(artifact.get("used_older_history_backfill")) != bool(older_count):
        raise ValueError("historical collection older-history expansion marker is inconsistent")
    guard = artifact.get("runtime_guard")
    if (
        not isinstance(guard, dict)
        or guard.get("status") != "passed"
        or int(guard.get("maximum_first_registration_items") or 0)
        != FIRST_REGISTRATION_MAX_ITEMS
        or guard.get("retained_items_are_newest_first") is not True
        or int(guard.get("older_history_expansion_target") or 0)
        != MIN_RELIABLE_HISTORY_ITEMS
    ):
        raise ValueError("historical collection lacks a passing runtime guard")


def _baseline_pool(
    items: list[dict[str, Any]],
    *,
    evaluated_at: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    mature_cutoff = evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
    window_start = evaluated_at - MATURE_HISTORY_WINDOW_DAYS * 86400
    mature_items = [
        item for item in items if 0 < _published_at(item) <= mature_cutoff
    ]
    in_window = [
        item for item in mature_items if _published_at(item) >= window_start
    ][:FIRST_REGISTRATION_MAX_ITEMS]
    if len(in_window) >= MIN_RELIABLE_HISTORY_ITEMS:
        return in_window, False, len(in_window)
    older_items = [
        item for item in mature_items if _published_at(item) < window_start
    ]
    needed = max(0, MIN_RELIABLE_HISTORY_ITEMS - len(in_window))
    capacity = max(0, FIRST_REGISTRATION_MAX_ITEMS - len(in_window))
    backfill = older_items[: min(needed, capacity)]
    return [*in_window, *backfill], bool(backfill), len(in_window)


def _historical_channels(
    item: dict[str, Any],
    *,
    medians: dict[str, float],
    like_p90: float,
    baseline_reliable: bool,
) -> tuple[list[str], dict[str, float]]:
    ratios = {
        metric: (
            float(_metric_value(item, metric)) / float(medians[metric])
            if medians[metric] > 0
            else 0.0
        )
        for metric in HISTORICAL_METRICS
    }
    channels: list[str] = []
    likes = _metric_value(item, "like_count")
    comments = _metric_value(item, "comment_count")

    if likes > 0 and comments / likes >= COMMENT_LIKE_RATIO_THRESHOLD:
        channels.append(f"comment_like_ratio:{comments / likes:.3f}")

    if not baseline_reliable:
        return channels, ratios

    small_account = like_p90 < MATURE_HISTORY_ABSOLUTE_LIKE_FLOOR
    if small_account:
        if likes >= like_p90 and like_p90 > 0:
            channels.append(f"p90_small_account:like={likes}>=p90:{like_p90:.2f}")
        return channels, ratios

    if likes < MATURE_HISTORY_ABSOLUTE_LIKE_FLOOR:
        return channels, ratios

    single_metrics = [
        metric
        for metric, ratio in ratios.items()
        if ratio >= HISTORICAL_SINGLE_METRIC_MULTIPLIER
    ]
    multi_metrics = [
        metric
        for metric, ratio in ratios.items()
        if ratio >= HISTORICAL_MULTI_METRIC_MULTIPLIER
    ]
    for metric in single_metrics:
        channels.append(f"{metric}_anomaly:{ratios[metric]:.2f}x")
    if len(multi_metrics) >= HISTORICAL_MINIMUM_MULTI_METRICS:
        detail = ";".join(
            f"{metric}={ratios[metric]:.2f}x" for metric in multi_metrics
        )
        channels.append(f"multi_indicator:{detail}")
    return channels, ratios


def judge_against_mature_history(
    *,
    candidate_metrics: dict[str, int],
    baseline_metrics: list[dict[str, int]],
) -> dict[str, Any]:
    medians = {
        metric: float(statistics.median([
            int(item.get(metric) or 0) for item in baseline_metrics
        ])) if baseline_metrics else 0.0
        for metric in HISTORICAL_METRICS
    }
    like_p90 = _quantile(
        [int(item.get("like_count") or 0) for item in baseline_metrics],
        SMALL_ACCOUNT_P90_PERCENTILE,
    )
    channels, ratios = _historical_channels(
        {"metrics": candidate_metrics},
        medians=medians,
        like_p90=like_p90,
        baseline_reliable=len(baseline_metrics) >= MIN_RELIABLE_HISTORY_ITEMS,
    )
    return {
        "baseline_active": len(baseline_metrics) >= MIN_RELIABLE_HISTORY_ITEMS,
        "sample_count": len(baseline_metrics),
        "medians": medians,
        "like_p90": like_p90,
        "channels": channels,
        "metric_ratios": ratios,
    }


def build_high_signal_artifact(
    items: list[dict[str, Any]],
    *,
    evaluated_at: int | None = None,
) -> dict[str, Any]:
    items = normalize_first_registration_items(items)
    if len(items) > FIRST_REGISTRATION_MAX_ITEMS:
        raise ValueError("high-signal screening cannot consume more than 50 retained items")
    evaluated_at = int(evaluated_at or time.time())
    baseline_items, used_older_history_backfill, in_window_count = _baseline_pool(
        items, evaluated_at=evaluated_at
    )
    medians = {
        metric: float(statistics.median(
            [_metric_value(item, metric) for item in baseline_items]
        )) if baseline_items else 0.0
        for metric in HISTORICAL_METRICS
    }
    like_p90 = _quantile(
        [_metric_value(item, "like_count") for item in baseline_items],
        SMALL_ACCOUNT_P90_PERCENTILE,
    )
    reliable = len(baseline_items) >= MIN_RELIABLE_HISTORY_ITEMS
    selected: list[dict[str, Any]] = []
    for item in items:
        channels, ratios = _historical_channels(
            item,
            medians=medians,
            like_p90=like_p90,
            baseline_reliable=reliable,
        )
        if not channels:
            continue
        selected.append({
            **item,
            "signal_channels": channels,
            "metric_ratios": ratios,
            "selection_reason": "independent_or_channels",
            "first_contact_category": (
                "historical_mature"
                if _published_at(item) <= evaluated_at - HISTORICAL_MATURITY_DAYS * 86400
                else "transition"
            ),
        })

    selection_ratio = len(selected) / len(items) if items else 0.0
    return {
        "artifact_kind": "high_signal_identification",
        "selection_policy_version": HIGH_SIGNAL_POLICY_VERSION,
        "collection_policy_version": FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
        "formal_guardrail_digest": FORMAL_GUARDRAIL_DIGEST,
        "historical_selection_guardrail_digest": (
            HISTORICAL_SELECTION_GUARDRAIL_DIGEST
        ),
        "evaluated_at": evaluated_at,
        "baseline_quality": "reliable" if reliable else "insufficient_history",
        "minimum_reliable_history_items": MIN_RELIABLE_HISTORY_ITEMS,
        "baseline_window_days": MATURE_HISTORY_WINDOW_DAYS,
        "baseline_maximum_items": FIRST_REGISTRATION_MAX_ITEMS,
        "baseline_item_count": len(baseline_items),
        "in_window_mature_item_count": in_window_count,
        "used_older_history_backfill": used_older_history_backfill,
        "baseline_medians": medians,
        "baseline_like_p90": like_p90,
        "historical_item_count": len(items),
        "selected_count": len(selected),
        "selection_ratio": selection_ratio,
        "selected_items": selected,
        "runtime_guard": {
            "status": "passed",
            "selected_items_are_historical_subset": True,
            "comment_like_ratio_is_independent": True,
            "maximum_selection_ratio": None,
            "iqr_selection_used": False,
            "first_registration_item_limit": FIRST_REGISTRATION_MAX_ITEMS,
            "older_history_backfill_requires_insufficient_window": True,
        },
    }


def validate_high_signal_artifact(
    *,
    historical_items: list[dict[str, Any]],
    artifact: dict[str, Any],
) -> None:
    evaluated_at = int(artifact.get("evaluated_at") or 0)
    if evaluated_at <= 0:
        raise ValueError("high-signal artifact lacks its baseline evaluation time")
    expected = build_high_signal_artifact(historical_items, evaluated_at=evaluated_at)
    if artifact.get("artifact_kind") != "high_signal_identification":
        raise ValueError("high-signal artifact kind is invalid")
    artifact_scope_digest = artifact.get(
        "historical_selection_guardrail_digest"
    )
    if (
        artifact_scope_digest is not None
        and artifact_scope_digest != HISTORICAL_SELECTION_GUARDRAIL_DIGEST
    ):
        raise ValueError("high-signal artifact does not match the current selection policy")
    for key in (
        "selection_policy_version",
        "collection_policy_version",
        "baseline_quality",
        "minimum_reliable_history_items",
        "baseline_window_days",
        "baseline_maximum_items",
        "historical_item_count",
        "baseline_item_count",
        "in_window_mature_item_count",
        "used_older_history_backfill",
        "baseline_medians",
        "baseline_like_p90",
        "selected_count",
        "selection_ratio",
    ):
        if artifact.get(key) != expected.get(key):
            raise ValueError(f"high-signal artifact {key} does not match the formal policy")
    if artifact.get("runtime_guard", {}).get("status") != "passed":
        raise ValueError("high-signal artifact lacks a passing runtime guard")
    if artifact.get("selected_items") != expected["selected_items"]:
        raise ValueError("high-signal selected videos do not match the formal policy")


def judge_against_formal_d_baseline(
    *,
    candidate_metrics: dict[str, int],
    predecessor_sequences: list[dict[int, dict[str, int]]],
    discovery_batch_index: int,
) -> dict[str, Any]:
    qualifying = [
        sequence
        for sequence in predecessor_sequences
        if set(sequence) >= set(range(8))
        and discovery_batch_index in sequence
    ][-FORMAL_D_ROLLING_WINDOW:]
    if len(qualifying) < FORMAL_D_ACTIVATION_COMPLETE_SEQUENCES:
        return {
            "baseline_active": False,
            "qualifying_sequence_count": len(qualifying),
            "channels": [],
            "medians": {},
        }
    medians = {
        metric: float(statistics.median([
            int(sequence[discovery_batch_index].get(metric) or 0)
            for sequence in qualifying
        ]))
        for metric in HISTORICAL_METRICS
    }
    ratios = {
        metric: (
            float(int(candidate_metrics.get(metric) or 0)) / medians[metric]
            if medians[metric] > 0
            else 0.0
        )
        for metric in HISTORICAL_METRICS
    }
    channels = [
        f"formal_d_{metric}:{ratio:.2f}x"
        for metric, ratio in ratios.items()
        if ratio >= FORMAL_D_SINGLE_METRIC_MULTIPLIER
    ]
    multi = [
        metric for metric, ratio in ratios.items()
        if ratio >= FORMAL_D_MULTI_METRIC_MULTIPLIER
    ]
    if len(multi) >= FORMAL_D_MINIMUM_MULTI_METRICS:
        channels.append(
            "formal_d_multi:" + ";".join(
                f"{metric}={ratios[metric]:.2f}x" for metric in multi
            )
        )
    return {
        "baseline_active": True,
        "qualifying_sequence_count": len(qualifying),
        "channels": channels,
        "medians": medians,
        "metric_ratios": ratios,
    }
