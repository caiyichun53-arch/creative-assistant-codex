"""Hermes-owned one-time reconciliation for formal daily observation history."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    MediaCrawlerCollectorAdapter,
    local_repo_path,
)
from scripts.core.external_adapters.local_mediacrawler_executor import (
    LocalMediaCrawlerExecutor,
    start_retained_douyin_collector_browser,
)
from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.business_runtime_guard import enforce_runtime_startup_guard
from scripts.core.production.stage0_content_core import (
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
)
from scripts.core.runtime.runtime_storage import runtime_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or apply one formal daily-observation reconciliation"
    )
    parser.add_argument("--action", choices=("plan", "apply"), required=True)
    parser.add_argument("--as-of", required=True)
    return parser


def _plan(as_of_business_date: str) -> dict:
    core = Stage0ContentProductionCore.open_read_only(
        FORMAL_DB_PATH, data_identity="production"
    )
    try:
        return CreationAssistantFormalBusinessCore(core=core).plan_daily_repair(
            as_of_business_date=as_of_business_date
        )
    finally:
        core.close()


def _visible_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key != "recovery_targets"}


def _source_backed_observation(target: dict) -> dict:
    return {
        "video_id": target["video_id"],
        "platform_item_id": target["platform_item_id"],
        "business_date": target["business_date"],
        "expected_d": target["expected_d"],
        "metrics": target["metrics"],
        "raw_archive_ref": target["raw_archive_ref"],
        "raw_json": target["raw_json"],
        "run_id": target["source_run_id"],
        "recovery_source": "retained_target_date_creator_snapshot",
    }


def _collect_missing_details(
    *, targets: list[dict], as_of_business_date: str, repair_ref: str
) -> tuple[list[dict], list[dict]]:
    if not targets:
        return [], []
    if len(targets) > 20:
        raise RuntimeError("targeted daily repair exceeds one controlled detail batch")
    browser_dir = local_repo_path("vendor", "MediaCrawler")
    start_retained_douyin_collector_browser(browser_dir, headless=True)
    collector = MediaCrawlerCollectorAdapter(LocalMediaCrawlerExecutor(
        archive_root=runtime_path(
            "formal", "daily_repairs", as_of_business_date, repair_ref, "mediacrawler"
        )
    ))
    result = collector.collect_video_snapshots(
        platform="douyin",
        source_urls=tuple(str(item["url"]) for item in targets),
        with_comments=False,
    )
    returned = {
        str(item.get("source_id") or ""): {
            "item": item,
            "raw_archive_ref": result.raw_archive_ref,
        }
        for item in result.payload.get("items", [])
        if isinstance(item, dict)
    }
    expected = {str(item["platform_item_id"]) for item in targets}
    target_by_source = {
        str(item["platform_item_id"]): item for item in targets
    }
    individual_failures: dict[str, str] = {}
    for missing_source_id in sorted(expected - set(returned)):
        target = target_by_source[missing_source_id]
        try:
            individual = collector.collect_video_snapshot(
                platform="douyin",
                source_url=str(target["url"]),
                max_items=1,
                with_comments=False,
            )
        except Exception as exc:
            individual_failures[missing_source_id] = str(exc)
            continue
        matched = [
            item
            for item in individual.payload.get("items", [])
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == missing_source_id
        ]
        if len(matched) == 1:
            returned[missing_source_id] = {
                "item": matched[0],
                "raw_archive_ref": individual.raw_archive_ref,
            }
    unexpected = sorted(set(returned) - expected)
    if unexpected:
        raise RuntimeError(
            "targeted daily detail recovery returned unexpected videos; "
            f"unexpected={unexpected}"
        )
    run_id = (
        f"daily_competitor:music_entertainment:{as_of_business_date}:"
        f"targeted-gap-repair-{repair_ref}"
    )
    observations: list[dict] = []
    unrecovered: list[dict] = []
    for target in targets:
        source_id = str(target["platform_item_id"])
        if source_id not in returned:
            unrecovered.append({
                "platform_item_id": source_id,
                "video_id": target["video_id"],
                "error": individual_failures.get(
                    source_id, "targeted detail recovery returned no item"
                ),
            })
            continue
        returned_item = returned[source_id]
        item = returned_item["item"]
        observations.append({
            "video_id": target["video_id"],
            "platform_item_id": target["platform_item_id"],
            "business_date": target["business_date"],
            "expected_d": target["expected_d"],
            "metrics": item["metrics"],
            "raw_archive_ref": returned_item["raw_archive_ref"],
            "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            "run_id": run_id,
            "recovery_source": "user_authorized_post_midnight_targeted_detail",
        })
    return observations, unrecovered


def _apply(as_of_business_date: str) -> dict:
    validation_core = Stage0ContentProductionCore.open_read_only(
        FORMAL_DB_PATH, data_identity="production"
    )
    try:
        as_of_business_date = CreationAssistantFormalBusinessCore(
            core=validation_core
        ).validate_daily_repair_request(as_of_business_date=as_of_business_date)
    finally:
        validation_core.close()
    enforce_runtime_startup_guard(entrypoint="hermes_daily_observation_repair")
    plan = _plan(as_of_business_date)
    repair_ref = uuid.uuid4().hex
    source_backed = [
        _source_backed_observation(item)
        for item in plan["recovery_targets"]
        if item["source_seen"]
    ]
    live_targets = [
        item for item in plan["recovery_targets"] if not item["source_seen"]
    ]
    live_observations, unrecovered_targets = _collect_missing_details(
        targets=live_targets,
        as_of_business_date=as_of_business_date,
        repair_ref=repair_ref,
    )
    observations = tuple([*source_backed, *live_observations])

    backup_path = runtime_path(
        "formal",
        "backups",
        f"pre_daily_observation_reconciliation_{as_of_business_date}_{repair_ref}.sqlite3",
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FORMAL_DB_PATH, backup_path)

    connection = sqlite3.connect(FORMAL_DB_PATH)
    core = Stage0ContentProductionCore(
        connection,
        db_path=FORMAL_DB_PATH,
        data_identity="production",
    )
    try:
        result = CreationAssistantFormalBusinessCore(core=core).apply_daily_repair(
            as_of_business_date=as_of_business_date,
            recovery_observations=observations,
            repair_run_id=f"daily-observation-reconciliation:{as_of_business_date}:{repair_ref}",
            actor="codex_user_authorized_daily_repair",
            reason=(
                "repair missed target-date observations, restore scheduler-owned business dates, "
                "deduplicate one-video-one-day checks, and exclude unrecoverable gaps from formal D baselines"
            ),
        )
    finally:
        core.close()

    installed = Stage0ContentProductionCore.open(
        FORMAL_DB_PATH, data_identity="production"
    )
    installed.close()
    verification = _plan(as_of_business_date)
    if any(
        int(verification[key]) != 0
        for key in (
            "first_seen_dates_to_change",
            "business_dates_to_change",
            "d_points_to_change",
            "duplicate_rows_to_archive_remove",
        )
    ) or int(verification["target_date_gaps"]) != len(unrecovered_targets):
        raise RuntimeError(f"post-repair verification failed: {_visible_plan(verification)}")
    return {
        "status": "completed_with_gaps" if unrecovered_targets else "completed",
        "backup_path": str(backup_path),
        "source_backed_recoveries": len(source_backed),
        "targeted_detail_recoveries": len(live_observations),
        "unrecovered_target_count": len(unrecovered_targets),
        "unrecovered_targets": unrecovered_targets,
        "result": result,
        "verification": _visible_plan(verification),
    }


def main() -> int:
    args = _parser().parse_args()
    if args.action == "plan":
        payload = {"status": "planned", "plan": _visible_plan(_plan(args.as_of))}
    else:
        payload = _apply(args.as_of)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
