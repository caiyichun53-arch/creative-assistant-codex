"""Project Runtime entry for deterministic Stage 1 source administration.

This entry never calls an external source or a model.  Daily/validation batches
continue through ``stage1b_daily_discovery``; this module registers the two
user/internal source kinds and performs one explicitly confirmed invalid-run
purge through the Core boundary.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from scripts.core.execution_contract import require_baseline_citations
from scripts.core.production.stage0_content_core import FORMAL_DB_PATH, Stage0ContentProductionCore


def _json(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    purge = subparsers.add_parser("purge-real-daily-validation-run")
    purge.add_argument("--run-id", required=True)
    purge.add_argument("--expected-candidates", required=True, type=int)
    purge.add_argument("--actor", required=True)
    purge.add_argument("--reason", required=True)
    purge.add_argument("--confirm", required=True)

    expansion = subparsers.add_parser("register-question-expansion")
    expansion.add_argument("--expansion-id", required=True)
    expansion.add_argument("--domain", required=True)
    expansion.add_argument("--core-question", required=True)
    expansion.add_argument("--parent-source-ref", required=True, type=_json)
    expansion.add_argument("--actor", required=True)

    direction = subparsers.add_parser("register-user-direction")
    direction.add_argument("--direction-id", required=True)
    direction.add_argument("--domain", required=True)
    direction.add_argument("--core-question", required=True)
    direction.add_argument("--submitted-by", required=True)

    args = parser.parse_args(argv)
    require_baseline_citations(["3", "13", "16", "17"])
    core = Stage0ContentProductionCore.open(FORMAL_DB_PATH, data_identity="production")
    try:
        if args.command == "purge-real-daily-validation-run":
            result = core.purge_real_daily_validation_run(
                run_id=args.run_id,
                actor=args.actor,
                reason=args.reason,
                expected_candidate_count=args.expected_candidates,
                confirmation=args.confirm,
            )
        elif args.command == "register-question-expansion":
            result = core.register_question_expansion_source(
                expansion_id=args.expansion_id,
                domain_label=args.domain,
                core_question=args.core_question,
                parent_source_ref=args.parent_source_ref,
                actor=args.actor,
            )
        else:
            result = core.register_saved_user_direction_source(
                direction_id=args.direction_id,
                domain_label=args.domain,
                core_question=args.core_question,
                submitted_by=args.submitted_by,
            )
    finally:
        core.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
