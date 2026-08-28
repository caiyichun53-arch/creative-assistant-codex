"""Run one production daily operation and exit with its real result."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import traceback
import uuid

from scripts.agent_platform.daily_operations_runtime import (
    DailyOperationsCoordinator,
)
from scripts.core.external_adapters.goal_phase4_external_adapters import local_repo_path
from scripts.core.external_adapters.local_mediacrawler_executor import (
    retained_douyin_collector_browser_status,
    start_retained_douyin_collector_browser,
)
from scripts.core.production.stage0_content_core import FORMAL_DB_PATH
from scripts.core.runtime.runtime_storage import runtime_path


CHINA_TIME = timezone(timedelta(hours=8))


def _append_log(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _append_log_safely(path: Path, payload: dict) -> None:
    """Keep the real operation error visible when the runtime log is unavailable."""
    try:
        _append_log(path, payload)
    except Exception as exc:
        payload["log_write_error"] = {
            "error_type": type(exc).__name__,
            "error": str(exc),
            "path": str(path),
        }


def _ensure_runtime_write_access() -> None:
    """Fail early with a clear reason before opening the formal database."""
    probe_root = runtime_path("agent_platform")
    probe = probe_root / f".daily_collection_write_probe_{uuid.uuid4().hex}"
    try:
        probe_root.mkdir(parents=True, exist_ok=True)
        probe.write_text("daily collection write probe\n", encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(
            "formal runtime write access unavailable: "
            f"{probe_root} ({type(exc).__name__}: {exc})"
        ) from exc
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one production daily operation and exit")
    parser.add_argument("--data-identity", choices=("production",), default="production")
    parser.add_argument("--catch-up", action="store_true")
    parser.add_argument("--business-date")
    parser.add_argument("--effective-at")
    parser.add_argument(
        "--resume-domain",
        help="explicitly continue one failed or stopped domain daily run",
    )
    parser.add_argument(
        "--run-log",
        type=Path,
        default=None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now(CHINA_TIME)
    effective_at: datetime | None = None
    if args.catch_up:
        if not args.business_date or not args.effective_at:
            raise SystemExit("catch-up requires both --business-date and --effective-at")
        try:
            business_date = datetime.strptime(args.business_date, "%Y-%m-%d").date()
            effective_at = datetime.fromisoformat(args.effective_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SystemExit(f"invalid catch-up date or time: {exc}") from exc
        if effective_at.tzinfo is None:
            raise SystemExit("catch-up effective time must include a timezone")
        default_log = runtime_path(
            "agent_platform", f"daily_collection_catchup_{business_date.isoformat()}.log"
        )
        trigger = "daily_missed_batch_catch_up"
    else:
        if args.business_date or args.effective_at:
            raise SystemExit("business date overrides are only allowed in explicit catch-up mode")
        business_date = None
        default_log = runtime_path("agent_platform", "daily_collection_once.log")
        trigger = "daily_scheduled"
    run_log = args.run_log or default_log
    browser_dir = local_repo_path("vendor", "MediaCrawler")

    def prepare_collector_browser() -> None:
        try:
            start_retained_douyin_collector_browser(browser_dir, headless=True)
        except Exception:
            # Let the formal collection path record the same readiness failure
            # with its normal daily-operation receipt.
            pass
        retained_douyin_collector_browser_status(browser_dir)

    coordinator = DailyOperationsCoordinator(
        db_path=FORMAL_DB_PATH,
        data_identity=args.data_identity,
        before_runner=prepare_collector_browser,
    )
    try:
        _ensure_runtime_write_access()
        resume = bool(str(args.resume_domain or "").strip())
        requested_domains = (
            (str(args.resume_domain).strip(),)
            if resume
            else None
        )
        if resume:
            trigger = "daily_user_resume_catch_up" if args.catch_up else "daily_user_resume"
        result = coordinator.schedule_all(
            domain_labels=requested_domains,
            trigger=trigger,
            resume=resume,
            attempt_ref=f"daily-once-{uuid.uuid4().hex}",
            business_date=business_date.isoformat() if business_date else None,
            effective_at=effective_at,
        )
        payload = {
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(CHINA_TIME).isoformat(),
            "business_date": result.get("business_date"),
            "effective_at": result.get("effective_at"),
            "run_mode": "catch_up" if args.catch_up else "scheduled",
            "resume": resume,
            "attempt_ref": result.get("attempt_ref"),
            "candidate_count": result.get("candidate_count", 0),
            "domain_results": result.get("domain_results", []),
            "failure_details": result.get("failure_details", []),
        }
        _append_log_safely(run_log, payload)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)
        successful = all(
            str(item.get("daily_run_status") or item.get("status") or "") == "completed"
            for item in result.get("domain_results") or []
        )
        return 0 if successful and "log_write_error" not in payload else 1
    except Exception as exc:
        payload = {
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(CHINA_TIME).isoformat(),
            "status": "failed",
            "run_mode": "catch_up" if args.catch_up else "scheduled",
            "business_date": args.business_date,
            "effective_at": args.effective_at,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        _append_log_safely(run_log, payload)
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
