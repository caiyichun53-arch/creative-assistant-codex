"""Core-side wiring between topic_candidates (source_to_topic's real output,
see run_source_to_topic.py) and the runtime_skills/content_plan atomic Skill
(CONTENT_PLAN_BUSINESS_CONTRACT.yaml).

2026-07-09: third of three planned bindings for the "选题 -> 大纲 -> 成稿"
chain (see ROADMAP.md) -- source_to_topic -> content_plan -> script_generate.
Follows the division-of-responsibility pattern established by
run_sample_deep_analyze.py/run_source_to_topic.py (see those files' module
docstrings for the pattern in full).

Two inputs content_plan's schema requires (tactic_candidates, style_examples)
do not have a real, properly-produced data source yet -- tactic_extract
(would produce real tactic_candidates by reducing >=2 sample_deep_analyze
results) and a curated style-example library (physical location not even
decided yet, per AGENTS.md "范例:少而厚") are both unbuilt. Rather than
blocking this binding entirely on two separate, larger pieces of unbuilt
work, or silently inventing fake-but-plausible values, this binding uses
honestly-labeled real stand-ins and documents the substitution in the code
and in TECHNICAL_MANUAL.md -- matching the precedent already set by
run_sample_deep_analyze.py's candidate_topic (real video title, not real
topic extraction):
  - tactic_candidates: reuses the same hit_deep_analysis row's topic_pattern/
    hook_pattern/structure_pattern (real analysis output, just not reduced
    across multiple samples the way tactic_extract would).
  - style_examples: real excerpts from the same hit's cleaned transcript
    (real competitor writing, not a curated "what we consider good style"
    library).
2026-07-13 correction: the paragraph above used to say evidence_items and
tactic_candidates were validated for shape but never actually read by either
model call -- that was true when this binding was first written, but
formal_skill_adapter.py's _run_content_plan() was fixed on 2026-07-11 (see
its own comment) to pass candidate_topic/evidence_items/tactic_candidates
into both the hook and outline prompts. Both real model calls now do read
this binding's honest stand-in values, which raises the stakes of the
substitution above slightly (it now visibly steers output, not just passes
a shape check) without changing whether the substitution itself is
appropriate.

Usage:
    python -m scripts.core.experience.run_content_plan --limit 1
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
    make_content_plan_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
# CONTENT_PLAN_BUSINESS_CONTRACT.yaml input_length_limits.
BRIEF_MAX_CHARS = 3000
EVIDENCE_ITEMS_MAX = 12
TACTIC_CANDIDATE_MAX_CHARS = 240
STYLE_EXAMPLE_MAX_CHARS = 400
STYLE_EXAMPLES_MAX = 6
_SENTENCE_SPLIT = re.compile(r"[。！？.!?\n]+")


def validate_content_plan_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-DNA-001"])


def select_topics_pending_plan(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real topic_candidates rows with topic_status='generated' (skip
    needs_review/no_result -- there is no usable topic to plan around),
    human_review_status='approved' (2026-07-10: the human review gate -- a
    generated topic sits at 'pending_review' until someone explicitly
    approves it via review_queue.py; this query will not pick it up before
    that, so nothing auto-flows to a plan/draft unreviewed), that have never
    been through content_plan (no content_plans row yet). Joins back through
    hit_deep_analysis -> hits -> hit_transcripts + account for the
    tactic_candidates/style_examples stand-ins (see module docstring)."""
    return conn.execute(
        """
        SELECT topic_candidates.*, analysis.topic_pattern, analysis.hook_pattern, analysis.structure_pattern,
               hits.hit_id AS hit_id, account.domain_label AS account_domain_label,
               t.cleaned_transcript_text AS transcript_text
          FROM topic_candidates
          JOIN hit_deep_analysis AS analysis ON analysis.analysis_id = topic_candidates.source_analysis_id
          JOIN hits ON hits.hit_id = analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
          LEFT JOIN hit_transcripts AS t ON t.hit_id = hits.hit_id AND t.processing_status = 'completed'
               AND t.version = (
                   SELECT MAX(version) FROM hit_transcripts
                    WHERE hit_transcripts.hit_id = hits.hit_id AND hit_transcripts.processing_status = 'completed'
               )
         WHERE topic_candidates.version = (
               SELECT MAX(version) FROM topic_candidates AS t2
                WHERE t2.source_analysis_id = topic_candidates.source_analysis_id
           )
           AND topic_candidates.topic_status = 'generated'
           AND topic_candidates.human_review_status = 'approved'
           AND NOT EXISTS (
               SELECT 1 FROM content_plans WHERE content_plans.source_topic_id = topic_candidates.topic_id
           )
         ORDER BY topic_candidates.created_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _style_examples_from_transcript(transcript_text: str) -> list[str]:
    """Real excerpts from the source hit's own transcript as an honest
    stand-in for a curated style-example library (see module docstring).
    Falls back to a single placeholder sentence when no transcript is
    available (LEFT JOIN can leave this empty) rather than violating the
    schema's minItems:1."""
    if not transcript_text:
        return ["(无可用转写文字稿,暂无风格参考文本)"]
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(transcript_text) if s.strip()]
    examples = [s[:STYLE_EXAMPLE_MAX_CHARS] for s in sentences[:STYLE_EXAMPLES_MAX]]
    return examples or [transcript_text[:STYLE_EXAMPLE_MAX_CHARS]]


def assemble_content_plan_input(topic_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    """Pure function: a real topic_candidates row (joined with its source
    analysis/hit/account/transcript) in, content_plan's exact public input
    contract out."""
    domain_label = topic_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"

    supporting_evidence = json.loads(topic_row["supporting_evidence"] or "[]")
    source_constraints = json.loads(topic_row["source_constraints"] or "[]")
    evidence_items: list[dict[str, str]] = [
        {"type": "supporting_evidence", "text": item} for item in supporting_evidence
    ] + [
        {"type": "source_constraint", "text": item} for item in source_constraints
    ]
    if not evidence_items:
        # topic_status='generated' should carry real supporting_evidence per
        # the Skill's own output_supporting_evidence_must_reference_input
        # requirement, but fall back rather than violate this schema's own
        # minItems:1 if a real run ever produces an edge case.
        evidence_items = [{"type": "topic_angle", "text": topic_row["topic_angle"]}]
    evidence_items = evidence_items[:EVIDENCE_ITEMS_MAX]

    brief = f"选题:{topic_row['candidate_topic']}。切入角度:{topic_row['topic_angle']}"[:BRIEF_MAX_CHARS]

    tactic_candidates = [
        f"选题手法:{topic_row['topic_pattern']}"[:TACTIC_CANDIDATE_MAX_CHARS],
        f"开头手法:{topic_row['hook_pattern']}"[:TACTIC_CANDIDATE_MAX_CHARS],
        f"结构手法:{topic_row['structure_pattern']}"[:TACTIC_CANDIDATE_MAX_CHARS],
    ]

    return {
        "request_id": f"content_plan_{topic_row['topic_id']}",
        "correlation_id": run_id,
        "candidate_topic": topic_row["candidate_topic"][:120],
        "brief": brief,
        "evidence_items": evidence_items,
        "tactic_candidates": tactic_candidates,
        "style_examples": _style_examples_from_transcript(topic_row["transcript_text"] or ""),
        "domain_label": domain_label,
        "schema_version": "content_plan.input.v1",
    }


def _persist_plan(conn: sqlite3.Connection, topic_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM content_plans WHERE source_topic_id=?", (topic_id,)).fetchone()[0]) + 1
    plan_id = f"{topic_id}_plan_v{version}"
    conn.execute(
        """
        INSERT INTO content_plans(
            plan_id, source_topic_id, version, request_id, correlation_id,
            hooks, selected_hook, beats, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id, topic_id, version, input_payload["request_id"], input_payload["correlation_id"],
            json.dumps(output["hooks"], ensure_ascii=False), output["selected_hook"],
            json.dumps(output["beats"], ensure_ascii=False), model_name, run_id,
        ),
    )
    conn.commit()
    return plan_id


def generate_one_plan(conn: sqlite3.Connection, harness: FormalBusinessSkillHarness, topic_row: sqlite3.Row, *, run_id: str, model_name: str) -> dict[str, Any]:
    input_payload = assemble_content_plan_input(topic_row, run_id=run_id)
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"topic_id": topic_row["topic_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"topic_id": topic_row["topic_id"], "status": "failed", "reason": "no result materialized"}
    plan_id = _persist_plan(conn, topic_row["topic_id"], input_payload, result["output"], model_name=model_name, run_id=run_id)
    return {"topic_id": topic_row["topic_id"], "status": "completed", "plan_id": plan_id}


def run_content_plan(conn: sqlite3.Connection, *, limit: int, harness: FormalBusinessSkillHarness, model_name: str) -> dict[str, Any]:
    validate_content_plan_execution_contract()
    run_id = "content_plan_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_topics_pending_plan(conn, limit=limit)
    results = [generate_one_plan(conn, harness, row, run_id=run_id, model_name=model_name) for row in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential gap as run_sample_deep_analyze.py/run_source_to_topic.py.
    Note: make_content_plan_harness() (unlike the other two harness
    factories) does not accept a route override, only a provider -- the two
    internal ModelRoute objects it builds (business.creation_hook/business.
    creation_outline) keep placeholder model_name labels regardless of the
    real provider passed in. This is cosmetic, not functional: which real
    model actually gets called is determined by the HermesModelProviderAdapter's
    own configured model, not by ModelRoute.model_name."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    harness = make_content_plan_harness(provider=adapter)  # type: ignore[arg-type]
    return harness, model_name


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Turn pending source_to_topic output into hook+outline plans via runtime_skills/content_plan.")
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
        report = run_content_plan(conn, limit=args.limit, harness=harness, model_name=model_name)
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
