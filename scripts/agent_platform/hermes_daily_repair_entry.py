"""Read-only daily observation repair planning; the old apply route is retired."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.stage0_content_core import (
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
)


RETIREMENT_MESSAGE = (
    "the legacy Hermes formal daily repair apply route is retired; "
    "use the Creation Assistant Core formal entry"
)


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


def main() -> int:
    args = _parser().parse_args()
    if args.action == "plan":
        payload = {"status": "planned", "plan": _visible_plan(_plan(args.as_of))}
    else:
        raise SystemExit(RETIREMENT_MESSAGE)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
