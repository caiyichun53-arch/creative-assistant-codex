"""Human review gate for the "选题 -> 大纲 -> 成稿 -> 文案优化/审核 -> 最终稿" chain
(source_to_topic -> content_plan -> script_generate -> script_review ->
final_draft).

2026-07-10: added the same day the chain was first wired end to end (topic/
plan/draft stages), after the user pointed out that nothing stopped a
generated topic from auto-flowing all the way to a script draft with no
human ever looking at it -- generated real output does not represent "ready
to use", and production needs a human in the loop.

2026-07-13 (置顶规则总表核对后): extended with two more stages -- review
(script_reviews, BR-CONTENT-004) and final (final_drafts, BR-CONTENT-005) --
following the exact same mechanism, not a new one: each stage table has a
human_review_status column that starts 'pending_review'; the next stage's own
select_*_pending_*() query will not pick up a row until this is 'approved'
(see run_content_plan.py/run_script_generate.py/run_script_review.py/
run_final_draft.py). This script is the only way to change that value --
there is no UI yet, deliberately (get the gate working and used by hand
first; a nicer interface is a separate later decision).

Usage:
    python -m scripts.core.experience.review_queue --list
    python -m scripts.core.experience.review_queue --list topic
    python -m scripts.core.experience.review_queue --approve topic t1_topic_v1
    python -m scripts.core.experience.review_queue --reject plan p1_plan_v1 --note "开头太标题党"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _safe_db_path  # noqa: E402

STAGES: dict[str, dict[str, Any]] = {
    "topic": {
        "table": "topic_candidates",
        "id_col": "topic_id",
        "preview": lambda row: f"选题:{row['candidate_topic']} | 角度:{row['topic_angle']}",
    },
    "plan": {
        "table": "content_plans",
        "id_col": "plan_id",
        "preview": lambda row: f"开头:{row['selected_hook']} | 大纲:{' / '.join(json.loads(row['beats']))}",
    },
    "draft": {
        "table": "script_drafts",
        "id_col": "draft_id",
        "preview": lambda row: (row["draft_text"][:200] + ("…" if len(row["draft_text"]) > 200 else "")),
    },
    # 2026-07-13 (置顶规则总表核对后, BR-CONTENT-004): review's own preview shows
    # the polished text (what a human is actually approving), not the pre-
    # polish draft_text -- plus the verdict/ai_flavor_risk so a reviewer sees
    # the Skill's own judgment at a glance before deciding.
    "review": {
        "table": "script_reviews",
        "id_col": "review_id",
        "preview": lambda row: (
            f"verdict:{row['verdict']} | AI味:{row['ai_flavor_risk']} | "
            + row["polished_text"][:200] + ("…" if len(row["polished_text"]) > 200 else "")
        ),
    },
    # BR-CONTENT-005: a separate confirmation from "review" above -- approving
    # a review does not approve the final draft it could become.
    "final": {
        "table": "final_drafts",
        "id_col": "final_draft_id",
        "preview": lambda row: (row["final_text"][:200] + ("…" if len(row["final_text"]) > 200 else "")),
    },
    # BR-TOPIC-005 (A3, 2026-07-13): a tag discovered from the hit library
    # (source='discovered') starts status=suggested; approving it here is
    # what a real trigger (domain_search_tags_promote_on_approval) uses to
    # flip status to active. Tags seeded from sources.yaml are inserted
    # already human_review_status='approved' and never appear in this queue.
    "tag": {
        "table": "domain_search_tags",
        "id_col": "tag_id",
        "preview": lambda row: f"#{row['tag']} | 领域:{row['domain_label']} | 来自视频:{row['source_video_id']}",
    },
}


def list_pending(conn: sqlite3.Connection, *, stage: str | None = None) -> list[dict[str, Any]]:
    stages = [stage] if stage else list(STAGES)
    items: list[dict[str, Any]] = []
    for name in stages:
        cfg = STAGES[name]
        rows = conn.execute(
            f"SELECT * FROM {cfg['table']} WHERE human_review_status = 'pending_review' ORDER BY created_at"  # noqa: S608 -- cfg['table'] is one of a fixed, hardcoded set (STAGES), never user input
        ).fetchall()
        for row in rows:
            items.append({
                "stage": name,
                "id": row[cfg["id_col"]],
                "preview": cfg["preview"](row),
                "created_at": row["created_at"],
            })
    return items


def set_review_status(conn: sqlite3.Connection, *, stage: str, item_id: str, status: str, note: str | None = None) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}, must be one of {sorted(STAGES)}")
    if status not in ("approved", "rejected"):
        raise ValueError(f"status must be 'approved' or 'rejected', got {status!r}")
    cfg = STAGES[stage]
    existing = conn.execute(
        f"SELECT * FROM {cfg['table']} WHERE {cfg['id_col']} = ?",  # noqa: S608 -- cfg['table']/cfg['id_col'] are from the fixed STAGES dict, never user input
        (item_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"no {stage} row with id {item_id!r}")
    conn.execute(
        f"UPDATE {cfg['table']} SET human_review_status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_note = ? "  # noqa: S608
        f"WHERE {cfg['id_col']} = ?",
        (status, note, item_id),
    )
    conn.commit()
    return {"stage": stage, "id": item_id, "status": status}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", nargs="?", const="__all__", choices=["__all__", *STAGES], metavar="STAGE",
                        help="list pending-review items, optionally filtered to one stage (topic/plan/draft)")
    group.add_argument("--approve", nargs=2, metavar=("STAGE", "ID"))
    group.add_argument("--reject", nargs=2, metavar=("STAGE", "ID"))
    parser.add_argument("--note", default=None, help="optional note recorded with --approve/--reject")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    db_path = _safe_db_path(Path(args.db))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        install_domain_search_schema(conn)
        if args.list is not None:
            stage = None if args.list == "__all__" else args.list
            items = list_pending(conn, stage=stage)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            elif not items:
                print("没有待审核的项目。")
            else:
                for item in items:
                    print(f"[{item['stage']}] {item['id']}  ({item['created_at']})\n    {item['preview']}\n")
            return 0
        stage, item_id = args.approve or args.reject
        status = "approved" if args.approve else "rejected"
        result = set_review_status(conn, stage=stage, item_id=item_id, status=status, note=args.note)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"已标记 [{result['stage']}] {result['id']} 为 {result['status']}。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
