from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.core.external_adapters import MediaCrawlerCollectorAdapter
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a controlled Phase 8 real-new-data activation.")
    parser.add_argument("--approval", default="PHASE_8_REAL_NEW_DATA_APPROVAL.yaml")
    parser.add_argument("--account-limit", type=int, default=1)
    parser.add_argument("--videos-per-account", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    approval_path = _repo_path(args.approval)
    approval = _read_yaml(approval_path)
    domain_path = _repo_path(approval["sources"][0]["config_ref"])
    domain = _read_yaml(domain_path)

    _validate_approval(approval, domain, args.account_limit, args.videos_per_account)

    run_id = "phase8_activation_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = REPO_ROOT / "data" / "formal" / "phase8_activation" / run_id
    executor = LocalMediaCrawlerExecutor(archive_root=run_dir / "mediacrawler", timeout_seconds=args.timeout_seconds)
    adapter = MediaCrawlerCollectorAdapter(executor)

    results: list[dict[str, Any]] = []
    for seed in domain["competitor_seeds"][: args.account_limit]:
        try:
            result = adapter.collect_video_snapshot(
                platform=domain["platform"],
                source_url=seed["url"],
                max_items=args.videos_per_account,
                with_comments=False,
            )
            results.append(
                {
                    "account": seed["name"],
                    "status": "succeeded",
                    "item_count": result.item_count,
                    "raw_archive_ref": result.raw_archive_ref,
                    "output_hash": result.output_hash,
                }
            )
        except Exception as exc:
            results.append({"account": seed["name"], "status": "failed", "error": str(exc)})
            break

    report = {
        "run_id": run_id,
        "approval_id": approval["approval_id"],
        "domain": domain["name"],
        "self_account": domain["self_account"],
        "policy": {
            "first_crawl": domain["collector_policy"]["first_crawl"],
            "comments": domain["collector_policy"]["comments"],
            "with_comments": False,
            "llm_used": False,
            "feishu_sent": False,
        },
        "requested": {
            "account_limit": args.account_limit,
            "videos_per_account": args.videos_per_account,
        },
        "results": results,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "activation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report | {"report_path": str(report_path)}, ensure_ascii=False, indent=2))

    return 0 if all(item["status"] == "succeeded" for item in results) else 2


def _repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"yaml must be an object: {path}")
    return data


def _validate_approval(approval: dict[str, Any], domain: dict[str, Any], account_limit: int, videos_per_account: int) -> None:
    if approval.get("schema_version") != "phase8.real_new_data_approval.v1":
        raise SystemExit("approval schema_version is not phase8.real_new_data_approval.v1")
    if approval.get("safety", {}).get("allow_old_database") is not False:
        raise SystemExit("approval must forbid old database usage")
    if approval.get("safety", {}).get("allow_fallback") is not False:
        raise SystemExit("approval must forbid fallback")
    if approval.get("domain", {}).get("formal_domain_label") != domain.get("formal_domain_label"):
        raise SystemExit("approval domain does not match domain config")
    if approval.get("sources", [{}])[0].get("source_count") != len(domain.get("competitor_seeds", [])):
        raise SystemExit("approval source_count does not match domain config")
    if account_limit < 1 or videos_per_account < 1:
        raise SystemExit("account-limit and videos-per-account must be positive")
    if videos_per_account > 5:
        raise SystemExit("videos-per-account exceeds adapter validation maximum")
    if account_limit > 1 and approval.get("safety", {}).get("allow_large_scale_collection") is False:
        raise SystemExit("approval forbids large-scale collection; run with account-limit 1")


if __name__ == "__main__":
    sys.exit(main())
