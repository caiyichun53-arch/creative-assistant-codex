"""Core-side wiring between scripts/core/business_data's real hits and the
runtime_skills/sample_deep_analyze atomic Skill. This module and its output
are described as "深度分析"/sample_deep_analyze; it is a current analysis
binding rather than an old standalone DNA workflow.

This is exactly the "组装层" that was missing: the Skill itself
(runtime_skills/sample_deep_analyze, contract SAMPLE_DEEP_ANALYZE_BUSINESS_
CONTRACT.yaml) was already built, contract-complete, and validated against a
real model call evidence -- but nothing read a real hit's transcript out of
production_activation.sqlite3, assembled it into the Skill's public input
contract, invoked it, and wrote the result back. That is everything in this
file, and only this file -- the Skill itself is untouched, gains no dependency
on this database, and stays portable (CR-003A): swap production environments,
or call runtime_skills/sample_deep_analyze standalone with a hand-built input
payload, and it still works the same way.

Division of responsibility:
  - select_hits_pending_analysis() / assemble_sample_deep_analyze_input():
    Core's Input Binding -- picks an eligible real hit and converts it into
    ONLY the fields sample_deep_analyze's input_schema.yaml allows. No
    database handle, no internal ids, no raw video path ever crosses into the
    payload (SAMPLE_DEEP_ANALYZE_BUSINESS_CONTRACT.yaml's forbidden_inputs).
  - analyze_one_hit(): calls the Skill through the existing
    make_sample_deep_analyze_harness() (scripts/core/model_gateway/
    formal_skill_adapter.py) -- the same job/worker/materializer machinery
    already built and tested for this Skill, just pointed at a real
    ModelRoute/provider instead of the deterministic fixture port tests use.
  - _persist_analysis(): Core's Output Binding -- takes the Skill's validated,
    generic output_schema (topic_pattern/hook_pattern/structure_pattern) and
    writes it into hit_deep_analysis, a business-specific table the Skill
    itself has no knowledge of.

Explicitly NOT built here (acknowledged gap, not an oversight):
  - candidate_topic is the hit's own video title, not a properly extracted
    topic -- that is runtime_skills/source_to_topic's job, a separate Skill
    this module does not call.
  - tactic_extract (the next stage, reducing >=2 sample_deep_analyze outputs
    into a shared pattern) is a deliberate follow-up, not done here -- it
    needs a real backlog of sample_deep_analyze rows to reduce over, which
    does not exist until this module has actually run.

This module deliberately lives in scripts/core/experience/, not
scripts/core/business_data/ -- BR-COLLECT-007 requires business_data to stay
fully deterministic/LLM-free (tests/validation/test_business_data_no_llm.py
enforces this by scanning for any model_gateway import there), and this
module's entire job is invoking an LLM through ModelGateway. It imports
business_data's schema/DB-path helpers as a dependency; business_data does
not, and must not, import anything from here.

Usage:
    python -m scripts.core.experience.run_sample_deep_analyze --limit 1
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
from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    make_sample_deep_analyze_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402
# 2026-07-13 用户明确拍板:彻底取消 transcript_excerpt/candidate_topic 的字符
# 截断。历史教训(不是删掉就当没发生过):2026-07-09 曾经把截断上限从2200字
# 提到7000字,起因是真实数据证明2200字会切掉约24%真实转写文字稿的正文中段,
# 模型在 structure_pattern 输出里编造了它从没见过的结尾——这正是"偷偷截断"
# 最危险的地方:错误不会报出来,只会安静地污染产出。这次的决定是不再允许这种
# 情况发生,风险(真实调用可能因为内容过长报错、或成本变高)由用户明确接受。


def validate_sample_deep_analyze_execution_contract() -> dict[str, Any]:
    """BR-DNA-001 is the catalog entry for this exact step (single-hit deep
    analysis); the freeze that used to block it (BR-DNA-003) was explicitly
    lifted by the user 2026-07-08 now that the Skill/ModelGateway boundaries
    it was waiting on exist."""
    return require_baseline_citations(["4", "17", "20"])


def select_hits_pending_analysis(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real hits with a completed transcript (reverse-prep already done) that
    have never been through sample_deep_analyze (no hit_deep_analysis row
    yet). Oldest-promoted first -- no priority tiering here, unlike reverse-
    prep, since there is no D0-D7 urgency concept for this step."""
    return conn.execute(
        """
        SELECT hits.*, account.domain_label AS account_domain_label, t.cleaned_transcript_text AS transcript_text
          FROM hits
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
          JOIN hit_transcripts AS t ON t.hit_id = hits.hit_id
         WHERE hits.preparation_status = 'completed'
           AND t.processing_status = 'completed'
           AND t.version = (
               SELECT MAX(version) FROM hit_transcripts
                WHERE hit_transcripts.hit_id = hits.hit_id AND hit_transcripts.processing_status = 'completed'
           )
           AND NOT EXISTS (SELECT 1 FROM hit_deep_analysis WHERE hit_deep_analysis.hit_id = hits.hit_id)
         ORDER BY hits.promoted_at
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


# 2026-07-11: hits.hit_channel carries free-text labels like
# "like_anomaly:3.86x,comment_anomaly:5.99x,...,multi_indicator:like_anomaly=3.86x;..."
# (see BR-HIT-001 section D) -- there is no separate numeric "deviation value"
# column anywhere in the schema. Per explicit user decision, "偏离值" (deviation
# value) for ranking purposes means the single highest baseline-multiplier
# ("Nx") number recorded anywhere in that hit's hit_channel string -- the one
# number in the real data that literally means "how many times this cleared
# the account's baseline median." comment_like_ratio (a plain ratio, not a
# baseline multiple) and p90_small_account (a different, non-baseline
# reference) do not carry this kind of number at all -- a hit whose
# hit_channel contains only those has no defined deviation value under this
# definition and is excluded from ranking, not assigned a fabricated 0.
_ANOMALY_MULTIPLIER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)x")


def deviation_value_from_hit_channel(hit_channel: str | None) -> float | None:
    """Highest "Nx" baseline-multiplier found in a real hit_channel string, or
    None if it contains no such multiplier (comment_like_ratio-only or
    p90_small_account-only hits)."""
    if not hit_channel:
        return None
    matches = _ANOMALY_MULTIPLIER_PATTERN.findall(hit_channel)
    if not matches:
        return None
    return max(float(m) for m in matches)


def select_hits_pending_analysis_by_deviation(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Same eligibility filter as select_hits_pending_analysis() (transcript
    ready, never analyzed) but ordered by deviation_value_from_hit_channel()
    descending instead of promoted_at -- used when a caller explicitly wants
    the most-deviated-from-baseline real hits first (e.g. building a
    tactic_extract evidence batch), not simply the oldest backlog. Hits with
    no defined deviation value (see deviation_value_from_hit_channel) are
    excluded entirely, not sorted to the bottom with a fake low value."""
    rows = conn.execute(
        """
        SELECT hits.*, account.domain_label AS account_domain_label, t.cleaned_transcript_text AS transcript_text
          FROM hits
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
          JOIN hit_transcripts AS t ON t.hit_id = hits.hit_id
         WHERE hits.preparation_status = 'completed'
           AND t.processing_status = 'completed'
           AND t.version = (
               SELECT MAX(version) FROM hit_transcripts
                WHERE hit_transcripts.hit_id = hits.hit_id AND hit_transcripts.processing_status = 'completed'
           )
           AND NOT EXISTS (SELECT 1 FROM hit_deep_analysis WHERE hit_deep_analysis.hit_id = hits.hit_id)
        """
    ).fetchall()
    scored = [(deviation_value_from_hit_channel(row["hit_channel"]), row) for row in rows]
    scored = [(value, row) for value, row in scored if value is not None]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _value, row in scored[:limit]]


def assemble_sample_deep_analyze_input(hit_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    """Pure function: real hit row in, sample_deep_analyze's exact public
    input contract out. Nothing here is a database handle, an internal id, or
    a path -- only fields the Skill's own input_schema.yaml declares."""
    domain_label = hit_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"
    transcript_text = hit_row["transcript_text"] or ""
    candidate_topic = (hit_row["title"] or "").strip() or "未命名选题"
    return {
        "request_id": f"sample_deep_analyze_{hit_row['hit_id']}",
        "correlation_id": run_id,
        "sample_id": hit_row["hit_id"],
        "candidate_topic": candidate_topic,
        "transcript_excerpt": transcript_text,
        "metrics": {
            "like_count": hit_row["like_count"],
            "comment_count": hit_row["comment_count"],
            "share_count": hit_row["share_count"],
            "collect_count": hit_row["collect_count"],
        },
        "domain_label": domain_label,
        "schema_version": "sample_deep_analyze.input.v1",
    }


def _persist_analysis(conn: sqlite3.Connection, hit_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM hit_deep_analysis WHERE hit_id=?", (hit_id,)).fetchone()[0]) + 1
    analysis_id = f"{hit_id}_v{version}"
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(
            analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id, hit_id, version, input_payload["request_id"], input_payload["correlation_id"],
            output["topic_pattern"], output["hook_pattern"], output["structure_pattern"], model_name, run_id,
        ),
    )
    conn.commit()
    return analysis_id


def analyze_one_hit(conn: sqlite3.Connection, harness: FormalBusinessSkillHarness, hit_row: sqlite3.Row, *, run_id: str, model_name: str) -> dict[str, Any]:
    input_payload = assemble_sample_deep_analyze_input(hit_row, run_id=run_id)
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"hit_id": hit_row["hit_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"hit_id": hit_row["hit_id"], "status": "failed", "reason": "no result materialized"}
    analysis_id = _persist_analysis(conn, hit_row["hit_id"], input_payload, result["output"], model_name=model_name, run_id=run_id)
    return {"hit_id": hit_row["hit_id"], "status": "completed", "analysis_id": analysis_id}


def run_sample_deep_analyze(conn: sqlite3.Connection, *, limit: int, harness: FormalBusinessSkillHarness, model_name: str) -> dict[str, Any]:
    validate_sample_deep_analyze_execution_contract()
    run_id = "sample_deep_analyze_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_hits_pending_analysis(conn, limit=limit)
    results = [analyze_one_hit(conn, harness, hit, run_id=run_id, model_name=model_name) for hit in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def _load_env_value(key: str, *, env_path: Path | None = None) -> str:
    path = env_path or (ROOT / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    raise RuntimeError(
        f"{key} not found (or empty) in {path}. Real sample_deep_analyze invocation needs "
        "HERMES_BUSINESS_MODEL_TOKEN/HERMES_BUSINESS_MODEL_BASE_URL/HERMES_BUSINESS_MODEL_NAME/"
        "HERMES_BUSINESS_MODEL_CLASS in the real .env -- these currently only exist in "
        ".env.live-gates (scoped to one-off gated validation runs), not the real .env. "
        "This is a real, unresolved gap: ask the user before copying those values into .env."
    )


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Builds a harness identical in every way to the one
    tests/SAMPLE_DEEP_ANALYZE live-gate validation already proved works,
    except the route/provider point at real production credentials instead of
    a deterministic fixture port. Raises RuntimeError (via _load_env_value) if
    those credentials are not present -- see this function's module docstring
    section on the credential gap found 2026-07-08.

    env_path lets a caller point at .env.live-gates for a single explicitly-
    requested test run without permanently copying those credentials into the
    real .env (a separate, bigger decision the user has not made yet)."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.reverse_dna_analysis",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="sample_deep_analyze.production.v1",
        config_hash=content_hash({"route": "business.reverse_dna_analysis", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0, "response_format": {"type": "json_object"}},
        timeout_ms=90000,
    )
    harness = make_sample_deep_analyze_harness(provider=adapter, route=route)  # type: ignore[arg-type]
    return harness, model_name


def _safe_db_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    allowed = (ROOT / "data" / "formal").resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError(f"sample_deep_analyze DB must stay under data/formal: {resolved}")
    if resolved.name == "creation.db":
        raise ValueError("refusing to write old data/creation.db")
    return resolved


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Analyze pending reverse-prepped hits via runtime_skills/sample_deep_analyze.")
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
        report = run_sample_deep_analyze(conn, limit=args.limit, harness=harness, model_name=model_name)
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
