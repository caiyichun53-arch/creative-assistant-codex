"""Core-side wiring between script_drafts (script_generate's real output, see
run_script_generate.py) and the runtime_skills/script_review atomic Skill
(SCRIPT_REVIEW_BUSINESS_CONTRACT.yaml).

2026-07-13 (置顶规则总表 V0.6.3 卷首核对后, BR-CONTENT-004): fourth binding for
the "选题 -> 大纲 -> 成稿" chain, extending it past script_generate for the
first time. script_review is a single atomic Skill call with three subnodes
(business.creation_polish, business.creation_review, business.ai_flavor_judge
-- see formal_skill_adapter.py's _run_script_review(), reordered the same day
so polish runs before review) that together realize 文案优化+审核+AI味判定 as
ONE pipeline stage, not three separate ones.

human_reference_refs (script_review's own public input requirement) has the
same "no curated example library exists yet" gap run_content_plan.py already
documented for style_examples -- but NOT the same fallback behavior. content_
plan's _style_examples_from_transcript() falls back to a placeholder sentence
("暂无风格参考文本") when no transcript exists, which is fine for loose style
guidance but wrong here: human_reference_refs exists specifically so
business.ai_flavor_judge can compare the draft against REAL human writing --
feeding it a system-generated placeholder string as if it were a "real
reference example" would corrupt exactly the check it's meant to support.
_human_reference_refs_from_transcript() below reuses the same real-excerpt
extraction logic but returns an empty list instead of a placeholder when no
transcript exists, and assemble_script_review_input() fails loud on that
rather than silently sending an empty list downstream.

Usage:
    python -m scripts.core.experience.run_script_review --limit 1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.domain_labels import ALLOWED_DOMAIN_LABELS  # noqa: E402
from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _load_env_value, _safe_db_path  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    make_script_review_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402

# SCRIPT_REVIEW_BUSINESS_CONTRACT.yaml input_length_limits.
BRIEF_MAX_CHARS = 3000
EVIDENCE_ITEMS_MAX = 12
# runtime_skills/script_review/input_schema.yaml human_reference_refs.
HUMAN_REFERENCE_REFS_MAX = 8
HUMAN_REFERENCE_REF_MAX_CHARS = 240
_SENTENCE_SPLIT = re.compile(r"[。！？.!?\n]+")


def _human_reference_refs_from_transcript(transcript_text: str) -> list[str]:
    """Real excerpts from the source hit's own transcript -- an honest stand-
    in for a curated positive-example library (none exists yet, per AGENTS.md
    "范例:少而厚"). Unlike run_content_plan.py's style_examples helper, this
    does NOT fall back to a placeholder string when no transcript exists --
    see module docstring for why that matters here specifically."""
    if not transcript_text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(transcript_text) if s.strip()]
    refs = [s[:HUMAN_REFERENCE_REF_MAX_CHARS] for s in sentences[:HUMAN_REFERENCE_REFS_MAX]]
    return refs or [transcript_text[:HUMAN_REFERENCE_REF_MAX_CHARS]]


def validate_script_review_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-CONTENT-004"])


def select_drafts_pending_review(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real script_drafts rows with human_review_status='approved' that have
    never been through script_review (no script_reviews row yet). Joins back
    through content_plans -> topic_candidates -> hit_deep_analysis -> hits ->
    account for domain_label, evidence, and the transcript excerpt used for
    human_reference_refs."""
    return conn.execute(
        """
        SELECT script_drafts.*, plan.selected_hook, plan.beats,
               topic.candidate_topic, topic.topic_angle, topic.supporting_evidence,
               topic.source_constraints, account.domain_label AS account_domain_label,
               transcript.cleaned_transcript_text AS transcript_text
          FROM script_drafts
          JOIN content_plans AS plan ON plan.plan_id = script_drafts.source_plan_id
          JOIN topic_candidates AS topic ON topic.topic_id = plan.source_topic_id
          JOIN hit_deep_analysis AS analysis ON analysis.analysis_id = topic.source_analysis_id
          JOIN hits ON hits.hit_id = analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
          LEFT JOIN hit_transcripts AS transcript ON transcript.hit_id = hits.hit_id
              AND transcript.processing_status = 'completed'
              AND transcript.version = (
                  SELECT MAX(version) FROM hit_transcripts AS t2
                   WHERE t2.hit_id = hits.hit_id AND t2.processing_status = 'completed'
              )
         WHERE script_drafts.human_review_status = 'approved'
           AND NOT EXISTS (
               SELECT 1 FROM script_reviews WHERE script_reviews.source_draft_id = script_drafts.draft_id
           )
         ORDER BY script_drafts.created_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def assemble_script_review_input(draft_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    """Pure function: a real script_drafts row (joined with its plan/topic/
    hit/account/transcript) in, script_review's exact public input contract
    out."""
    domain_label = draft_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"

    supporting_evidence = json.loads(draft_row["supporting_evidence"] or "[]")
    source_constraints = json.loads(draft_row["source_constraints"] or "[]")
    evidence_items: list[dict[str, str]] = [
        {"type": "supporting_evidence", "text": item} for item in supporting_evidence
    ] + [
        {"type": "source_constraint", "text": item} for item in source_constraints
    ]
    if not evidence_items:
        evidence_items = [{"type": "topic_angle", "text": draft_row["topic_angle"]}]
    evidence_items = evidence_items[:EVIDENCE_ITEMS_MAX]

    brief = f"选题:{draft_row['candidate_topic']}。切入角度:{draft_row['topic_angle']}"[:BRIEF_MAX_CHARS]

    human_reference_refs = _human_reference_refs_from_transcript(draft_row["transcript_text"] or "")
    if not human_reference_refs:
        # A hit with no completed transcript (should not happen by the time a
        # draft exists, but fail loud rather than send an empty list the
        # Skill's own input_schema already rejects with minItems: 1).
        raise ValueError(f"no transcript excerpts available for draft {draft_row['draft_id']!r} -- cannot assemble human_reference_refs")

    return {
        "request_id": f"script_review_{draft_row['draft_id']}",
        "correlation_id": run_id,
        "draft_text": draft_row["draft_text"],
        "brief": brief,
        "evidence_items": evidence_items,
        "human_reference_refs": human_reference_refs,
        "domain_label": domain_label,
        "schema_version": "script_review.input.v1",
    }


def _persist_review(conn: sqlite3.Connection, draft_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM script_reviews WHERE source_draft_id=?", (draft_id,)).fetchone()[0]) + 1
    review_id = f"{draft_id}_review_v{version}"
    conn.execute(
        """
        INSERT INTO script_reviews(
            review_id, source_draft_id, version, request_id, correlation_id,
            verdict, issues, polished_text, revision_focus, ai_flavor_risk,
            revision_targets, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id, draft_id, version, input_payload["request_id"], input_payload["correlation_id"],
            output["verdict"], json.dumps(output["issues"], ensure_ascii=False), output["polished_text"],
            json.dumps(output["revision_focus"], ensure_ascii=False), output["ai_flavor_risk"],
            json.dumps(output["revision_targets"], ensure_ascii=False), model_name, run_id,
        ),
    )
    conn.commit()
    return review_id


def review_one_draft(conn: sqlite3.Connection, harness: FormalBusinessSkillHarness, draft_row: sqlite3.Row, *, run_id: str, model_name: str) -> dict[str, Any]:
    input_payload = assemble_script_review_input(draft_row, run_id=run_id)
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"draft_id": draft_row["draft_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"draft_id": draft_row["draft_id"], "status": "failed", "reason": "no result materialized"}
    review_id = _persist_review(conn, draft_row["draft_id"], input_payload, result["output"], model_name=model_name, run_id=run_id)
    return {"draft_id": draft_row["draft_id"], "status": "completed", "review_id": review_id}


def run_script_review(conn: sqlite3.Connection, *, limit: int, harness: FormalBusinessSkillHarness, model_name: str) -> dict[str, Any]:
    validate_script_review_execution_contract()
    run_id = "script_review_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_drafts_pending_review(conn, limit=limit)
    results = [review_one_draft(conn, harness, row, run_id=run_id, model_name=model_name) for row in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential gap as the prior three bindings."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    routes = {}
    for route_name in ("business.creation_review", "business.creation_polish", "business.ai_flavor_judge"):
        routes[route_name] = ModelRoute(
            route_name=route_name,
            provider_name=adapter.provider_name,
            model_name=model_name,
            config_version="script_review.production.v1",
            config_hash=content_hash({"route": route_name, "provider": "hermes", "model": model_name}),
            parameters={"temperature": 0.3, "max_completion_tokens": 2500, "response_format": {"type": "json_object"}},
            timeout_ms=60000,
        )
    harness = make_script_review_harness(provider=adapter, routes=routes)  # type: ignore[arg-type]
    return harness, model_name


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Turn pending script_generate output into script_reviews via runtime_skills/script_review.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Override which .env-style file supplies HERMES_BUSINESS_MODEL_* credentials "
        "(defaults to the real .env). Pass .env.live-gates for a one-off test run without "
        "permanently copying those credentials into .env.",
    )
    args = parser.parse_args(argv)

    db_path = _safe_db_path(Path(args.db))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    env_path = Path(args.env_file) if args.env_file else None
    harness, model_name = build_real_harness(env_path=env_path)
    try:
        install_schema(conn)
        report = run_script_review(conn, limit=args.limit, harness=harness, model_name=model_name)
    finally:
        conn.close()
        harness.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        import yaml as _yaml
        print(_yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0 if report["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
