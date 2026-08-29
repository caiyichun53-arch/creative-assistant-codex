"""Explicit maintenance entrypoint for the retained Douyin collector browser."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterError,
    local_repo_path,
)
from scripts.core.external_adapters.local_mediacrawler_executor import (
    retained_douyin_collector_browser_status,
    start_retained_douyin_collector_browser,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check or explicitly start the retained Douyin collector browser"
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="start the dedicated collector browser if it is not already ready",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="start a visible browser for explicit login maintenance",
    )
    args = parser.parse_args()

    mediacrawler_dir = local_repo_path("vendor", "MediaCrawler")
    if not (mediacrawler_dir / "main.py").is_file():
        print(json.dumps({"status": "blocked", "reason": "collector program is missing"}, ensure_ascii=False))
        return 1

    try:
        if args.start:
            start_retained_douyin_collector_browser(
                mediacrawler_dir,
                headless=not args.visible,
                data_identity="production",
            )
        status = retained_douyin_collector_browser_status(
            mediacrawler_dir, data_identity="production"
        )
    except (ExternalAdapterError, OSError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
