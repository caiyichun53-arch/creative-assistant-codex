"""Core-side wiring between hit_deep_analysis (sample_deep_analyze's real
output, see run_sample_deep_analyze.py) and the runtime_skills/source_to_topic
atomic Skill (SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml).

2026-07-09: this is the second of three planned bindings for the "选题 ->
大纲 -> 成稿" chain (see ROADMAP.md) -- source_to_topic -> content_plan ->
script_generate. Follows the exact division-of-responsibility pattern
established by run_sample_deep_analyze.py (that file's module docstring
explains the pattern in full; not repeated here).

What counts as "source evidence" here: source_to_topic's own business
contract (non_responsibilities: deep_sample_analysis, tactic_extract) makes
clear the Skill itself must not know or care where its evidence came from --
that decision belongs entirely to this binding layer. The only real,
already-verified evidence this project has today is hit_deep_analysis's
topic_pattern/hook_pattern/structure_pattern (sample_deep_analyze's output on
a real competitor hit) -- so that is what gets assembled into
source_evidence_items here. This is a deliberate, documented choice, not the
only possible one: once tactic_extract or research_evidence_extract are
bound, they could feed source_to_topic too.

Explicitly NOT built here (acknowledged gap, matching the source_to_topic
contract's own non_responsibilities, not an oversight):
  - relation_summary is a plain, honest, deterministic sentence stating that
    no relation judgement has been computed -- content_relation_judge (the
    Skill that would actually produce this) is not bound to real data yet.
    Getting this wrong by inventing a fake relation summary would violate the
    contract's own "must_not_judge_content_relation" -- so the binding layer
    doesn't try to fake what the missing Skill would have said.

Usage:
    python -m scripts.core.experience.run_source_to_topic --limit 1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _load_env_value, _safe_db_path  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    make_source_to_topic_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402

ALLOWED_DOMAIN_LABELS = {"fan_kepu_social_life", "music_entertainment", "third_domain_neutral", "cross_domain", "unknown"}
# SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml input_length_limits.
SOURCE_CONTENT_MAX_CHARS = 1600
EVIDENCE_ITEM_MAX_CHARS = 240
RELATION_SUMMARY_MAX_CHARS = 600
NO_RELATION_JUDGEMENT_YET = (
    "尚未做过来源关联判断(content_relation_judge 这个 Skill 还没有接上真实数据)——"
    "本次候选选题只依据下面列出的证据本身生成,不代表已经和现有内容/选题库比对过是否重复或冲突。"
)[:RELATION_SUMMARY_MAX_CHARS]


def validate_source_to_topic_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-DNA-001"])


def select_analyses_pending_topic(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real hit_deep_analysis rows (sample_deep_analyze's completed output)
    that have never been through source_to_topic (no topic_candidates row
    yet). Oldest-created first, same ordering convention as
    select_hits_pending_analysis()."""
    return conn.execute(
        """
        SELECT hit_deep_analysis.*, hits.title AS hit_title, account.domain_label AS account_domain_label,
               account.account_name AS account_name
          FROM hit_deep_analysis
          JOIN hits ON hits.hit_id = hit_deep_analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
         WHERE hit_deep_analysis.version = (
               SELECT MAX(version) FROM hit_deep_analysis AS d2 WHERE d2.hit_id = hit_deep_analysis.hit_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM topic_candidates WHERE topic_candidates.source_analysis_id = hit_deep_analysis.analysis_id
           )
         ORDER BY hit_deep_analysis.created_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def assemble_source_to_topic_input(analysis_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    """Pure function: a real hit_deep_analysis row in, source_to_topic's exact
    public input contract out. No database handle, no internal id beyond the
    Skill's own request_id/source_id fields, no raw path."""
    domain_label = analysis_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"
    account_name = (analysis_row["account_name"] or "").strip()
    hit_title = (analysis_row["hit_title"] or "").strip()
    source_content = f"账号「{account_name}」的爆款视频「{hit_title}」经深度分析后提炼的选题/开头/结构手法。"[:SOURCE_CONTENT_MAX_CHARS]
    evidence_items = [
        f"选题手法:{analysis_row['topic_pattern']}"[:EVIDENCE_ITEM_MAX_CHARS],
        f"开头手法:{analysis_row['hook_pattern']}"[:EVIDENCE_ITEM_MAX_CHARS],
        f"结构手法:{analysis_row['structure_pattern']}"[:EVIDENCE_ITEM_MAX_CHARS],
    ]
    return {
        "request_id": f"source_to_topic_{analysis_row['analysis_id']}",
        "correlation_id": run_id,
        "source_id": analysis_row["analysis_id"],
        "source_content": source_content,
        "source_evidence_items": evidence_items,
        "domain_label": domain_label,
        "relation_summary": NO_RELATION_JUDGEMENT_YET,
        "schema_version": "source_to_topic.input.v1",
    }


def _persist_topic(conn: sqlite3.Connection, analysis_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM topic_candidates WHERE source_analysis_id=?", (analysis_id,)).fetchone()[0]) + 1
    topic_id = f"{analysis_id}_topic_v{version}"
    conn.execute(
        """
        INSERT INTO topic_candidates(
            topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence,
            source_constraints, no_result_reason, confidence, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            topic_id, analysis_id, version, input_payload["request_id"], input_payload["correlation_id"],
            output["topic_status"], output["candidate_topic"], output["topic_angle"],
            json.dumps(output["supporting_evidence"], ensure_ascii=False),
            json.dumps(output["source_constraints"], ensure_ascii=False),
            output["no_result_reason"], output["confidence"], model_name, run_id,
        ),
    )
    conn.commit()
    return topic_id


def generate_one_topic(conn: sqlite3.Connection, harness: FormalBusinessSkillHarness, analysis_row: sqlite3.Row, *, run_id: str, model_name: str) -> dict[str, Any]:
    input_payload = assemble_source_to_topic_input(analysis_row, run_id=run_id)
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"analysis_id": analysis_row["analysis_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"analysis_id": analysis_row["analysis_id"], "status": "failed", "reason": "no result materialized"}
    topic_id = _persist_topic(conn, analysis_row["analysis_id"], input_payload, result["output"], model_name=model_name, run_id=run_id)
    return {"analysis_id": analysis_row["analysis_id"], "status": "completed", "topic_id": topic_id, "topic_status": result["output"]["topic_status"]}


def run_source_to_topic(conn: sqlite3.Connection, *, limit: int, harness: FormalBusinessSkillHarness, model_name: str) -> dict[str, Any]:
    validate_source_to_topic_execution_contract()
    run_id = "source_to_topic_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_analyses_pending_topic(conn, limit=limit)
    results = [generate_one_topic(conn, harness, row, run_id=run_id, model_name=model_name) for row in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential gap as run_sample_deep_analyze.py's build_real_harness():
    HERMES_BUSINESS_MODEL_* only exists in .env.live-gates, not the real .env,
    as of this writing. Raises RuntimeError via _load_env_value if absent."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.source_to_topic",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="source_to_topic.production.v1",
        config_hash=content_hash({"route": "business.source_to_topic", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0, "max_completion_tokens": 1200, "response_format": {"type": "json_object"}},
        timeout_ms=60000,
    )
    harness = make_source_to_topic_harness(provider=adapter, route=route)  # type: ignore[arg-type]
    return harness, model_name


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Turn pending sample_deep_analyze output into candidate topics via runtime_skills/source_to_topic.")
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
        report = run_source_to_topic(conn, limit=args.limit, harness=harness, model_name=model_name)
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
