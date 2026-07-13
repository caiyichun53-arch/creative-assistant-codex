"""Core-side wiring between content_plans (content_plan's real output, see
run_content_plan.py) and the runtime_skills/script_generate atomic Skill
(SCRIPT_GENERATE_BUSINESS_CONTRACT.yaml).

The content-production binding chain is source_to_topic -> content_plan ->
script_generate.
Follows the division-of-responsibility pattern established by the prior two
bindings (see run_sample_deep_analyze.py's module docstring for the pattern
in full).

Unlike content_plan, script_generate is a single-model-call Skill (route_name
business.creation_draft, no subnodes) -- simpler wiring, but it needs one
more field neither prior binding produced: research_summary. No research
Skill (research_evidence_extract/production_research_plan) is bound to real
data yet, so this reuses the same honest-real-data-substitution approach
already used for content_plan's tactic_candidates/style_examples: a plain
summary of the supporting_evidence/source_constraints already gathered for
this topic (real data, just not a proper research pass), clearly labeled as
such rather than invented.

Usage:
    python -m scripts.core.experience.run_script_generate --limit 1
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

from scripts.core.business_data.domain_labels import ALLOWED_DOMAIN_LABELS  # noqa: E402
from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _load_env_value, _safe_db_path  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    load_script_generate_execution_configuration,
    make_script_generate_harness,
)
from scripts.core.production.stage0_content_core import reject_legacy_cli_production_write, require_legacy_test_identity  # noqa: E402
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402
NOT_A_REAL_RESEARCH_PASS_PREFIX = "(未经真实研究流程,仅汇总已有证据,不是 research_evidence_extract/production_research_plan 的产出)"


def validate_script_generate_execution_contract() -> dict[str, Any]:
    return require_baseline_citations(["5", "10", "20"])


def select_plans_pending_script(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real content_plans rows with human_review_status='approved' (2026-07-10:
    the human review gate, same mechanism as select_topics_pending_plan() in
    run_content_plan.py -- a plan sits at 'pending_review' until someone
    explicitly approves it via review_queue.py) that have never been through
    script_generate (no script_drafts row yet). Joins back through
    topic_candidates -> hit_deep_analysis -> hits -> account for
    domain_label/evidence."""
    return conn.execute(
        """
        SELECT content_plans.*, topic.candidate_topic, topic.topic_angle,
               topic.supporting_evidence, topic.source_constraints,
               account.domain_label AS account_domain_label
          FROM content_plans
          JOIN topic_candidates AS topic ON topic.topic_id = content_plans.source_topic_id
          JOIN hit_deep_analysis AS analysis ON analysis.analysis_id = topic.source_analysis_id
          JOIN hits ON hits.hit_id = analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
         WHERE content_plans.version = (
               SELECT MAX(version) FROM content_plans AS c2
                WHERE c2.source_topic_id = content_plans.source_topic_id
           )
           AND content_plans.human_review_status = 'approved'
           AND NOT EXISTS (
               SELECT 1 FROM script_drafts WHERE script_drafts.source_plan_id = content_plans.plan_id
           )
         ORDER BY content_plans.created_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def assemble_script_generate_input(
    plan_row: sqlite3.Row,
    *,
    run_id: str,
    execution_configuration: dict[str, Any],
) -> dict[str, Any]:
    """Pure function: a real content_plans row (joined with its source topic/
    hit/account) in, script_generate's exact public input contract out."""
    domain_label = plan_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"

    supporting_evidence = json.loads(plan_row["supporting_evidence"] or "[]")
    source_constraints = json.loads(plan_row["source_constraints"] or "[]")
    evidence_items: list[dict[str, str]] = [
        {"type": "supporting_evidence", "text": item} for item in supporting_evidence
    ] + [
        {"type": "source_constraint", "text": item} for item in source_constraints
    ]
    if not evidence_items:
        evidence_items = [{"type": "topic_angle", "text": plan_row["topic_angle"]}]
    evidence_items = evidence_items[:execution_configuration["evidence_items_max"]]

    brief = f"选题:{plan_row['candidate_topic']}。切入角度:{plan_row['topic_angle']}"

    evidence_summary = "; ".join(item["text"] for item in evidence_items)
    research_summary = f"{NOT_A_REAL_RESEARCH_PASS_PREFIX} 已有证据:{evidence_summary}"

    return {
        "request_id": f"script_generate_{plan_row['plan_id']}",
        "correlation_id": run_id,
        "brief": brief,
        "selected_hook": plan_row["selected_hook"],
        "beats": json.loads(plan_row["beats"] or "[]"),
        "research_summary": research_summary,
        "evidence_items": evidence_items,
        "domain_label": domain_label,
        "schema_version": "script_generate.input.v1",
    }


def _persist_draft(conn: sqlite3.Connection, plan_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM script_drafts WHERE source_plan_id=?", (plan_id,)).fetchone()[0]) + 1
    draft_id = f"{plan_id}_draft_v{version}"
    conn.execute(
        """
        INSERT INTO script_drafts(
            draft_id, source_plan_id, version, request_id, correlation_id,
            draft_text, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (draft_id, plan_id, version, input_payload["request_id"], input_payload["correlation_id"], output["draft_text"], model_name, run_id),
    )
    conn.commit()
    return draft_id


def generate_one_draft(
    conn: sqlite3.Connection,
    harness: FormalBusinessSkillHarness,
    plan_row: sqlite3.Row,
    *,
    run_id: str,
    model_name: str,
    execution_configuration: dict[str, Any],
) -> dict[str, Any]:
    input_payload = assemble_script_generate_input(
        plan_row,
        run_id=run_id,
        execution_configuration=execution_configuration,
    )
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"plan_id": plan_row["plan_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"plan_id": plan_row["plan_id"], "status": "failed", "reason": "no result materialized"}
    draft_id = _persist_draft(conn, plan_row["plan_id"], input_payload, result["output"], model_name=model_name, run_id=run_id)
    return {"plan_id": plan_row["plan_id"], "status": "completed", "draft_id": draft_id}


def run_script_generate(conn: sqlite3.Connection, *, limit: int, harness: FormalBusinessSkillHarness, model_name: str) -> dict[str, Any]:
    validate_script_generate_execution_contract()
    execution_configuration = load_script_generate_execution_configuration()
    if harness.adapter.contract.execution_configuration != execution_configuration:
        raise RuntimeError("script_generate harness configuration does not match the current versioned contract")
    run_id = "script_generate_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_plans_pending_script(conn, limit=limit)
    results = [
        generate_one_draft(
            conn,
            harness,
            row,
            run_id=run_id,
            model_name=model_name,
            execution_configuration=execution_configuration,
        )
        for row in pending
    ]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "execution_configuration": execution_configuration,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential gap as the prior two bindings."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.creation_draft",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="script_generate.production.v1",
        config_hash=content_hash({"route": "business.creation_draft", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0.3, "response_format": {"type": "json_object"}},
        timeout_ms=60000,
    )
    harness = make_script_generate_harness(provider=adapter, route=route)  # type: ignore[arg-type]
    return harness, model_name


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Turn pending content_plan output into script drafts via runtime_skills/script_generate.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-identity", required=True, choices=("test", "fixture", "synthetic", "replay", "mock"))
    parser.add_argument(
        "--env-file",
        default=None,
        help="Override which .env-style file supplies HERMES_BUSINESS_MODEL_* credentials "
        "(defaults to the real .env). Pass .env.live-gates for a one-off test run without "
        "permanently copying those credentials into .env.",
    )
    args = parser.parse_args(argv)
    require_legacy_test_identity(args.data_identity)

    db_path = _safe_db_path(Path(args.db))
    reject_legacy_cli_production_write(db_path, "run_script_generate.py")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    env_path = Path(args.env_file) if args.env_file else None
    harness, model_name = build_real_harness(env_path=env_path)
    try:
        install_schema(conn)
        report = run_script_generate(conn, limit=args.limit, harness=harness, model_name=model_name)
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
