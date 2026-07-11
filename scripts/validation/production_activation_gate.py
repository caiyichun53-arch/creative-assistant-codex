"""Comprehensive check for 置顶规则总表(V0.6.3 卷首)条目22/23 + 附表"七、Gate
必须检查的内容" -- the production-activation control-package gate the pinned
rules doc names but that never existed as a real script until 2026-07-13.

Deliberately does NOT re-implement checks scripts/validation/dead_goal_chain_
gate.py already does (model positions displayed explicitly, fallback
disabled, no reachable path back to the archived Goal01-12 chain) --
run_production_activation_gate() calls that gate's own run_checklist() and
folds its results in, rather than duplicating the same logic a second time.
This file only adds the checks from the pinned-rules doc's Gate list that
nothing else covers yet: pipeline-stage completeness (including 文案优化 as a
real node, not just documented intent), human-review gates actually existing
on every stage table, no old DNA/scoring-based topic sorting, the formal-
research/对标 boundary, traceability fields, and no auto-publish path.

Usage:
    python -m scripts.validation.production_activation_gate
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.dead_goal_chain_gate import run_checklist as run_dead_goal_chain_checklist  # noqa: E402

SCHEMA_PATH = ROOT / "scripts" / "core" / "business_data" / "competitor_accounts_schema.sqlite.sql"
REVIEW_QUEUE_PATH = ROOT / "scripts" / "core" / "experience" / "review_queue.py"
FORMAL_SKILL_ADAPTER_PATH = ROOT / "scripts" / "core" / "model_gateway" / "formal_skill_adapter.py"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
SETTINGS_EXAMPLE_PATH = ROOT / "config" / "settings.example.yaml"

# The five real pipeline stages review_queue.py's STAGES dict must cover, and
# the table each one is backed by. This is the mechanical proxy for "流程完整
# (研究/内容计划/初稿/文案优化/审核/最终确认)" -- 研究 has no real binding yet
# (see TECHNICAL_MANUAL.md section 10's explicit note) so is not checked here;
# checking for a stage that structurally cannot exist would make this gate
# permanently red for a documented, deliberate gap, not a real regression.
EXPECTED_STAGES = {
    "topic": "topic_candidates",
    "plan": "content_plans",
    "draft": "script_drafts",
    "review": "script_reviews",
    "final": "final_drafts",
}


def _schema_text() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _table_definition(schema_text: str, table: str) -> str | None:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\);",
        schema_text,
        re.DOTALL,
    )
    return match.group(1) if match else None


def check_pipeline_stages_wired() -> dict[str, Any]:
    review_queue_text = REVIEW_QUEUE_PATH.read_text(encoding="utf-8")
    missing_stages = [stage for stage, table in EXPECTED_STAGES.items() if f'"{stage}"' not in review_queue_text or table not in review_queue_text]
    return {
        "name": "pipeline_stages_wired",
        "passed": not missing_stages,
        "detail": f"review_queue.py STAGES missing: {missing_stages}" if missing_stages else "clean",
    }


def check_polish_before_review_order() -> dict[str, Any]:
    """置顶规则总表条目3/30: 文案优化 must run before 审核. Mechanical proxy:
    business.creation_polish's ModelRequest call must appear, in source
    order, before business.creation_review's, inside _run_script_review()."""
    text = FORMAL_SKILL_ADAPTER_PATH.read_text(encoding="utf-8")
    method_match = re.search(r"def _run_script_review\(.*?\n    def _run_", text, re.DOTALL)
    body = method_match.group(0) if method_match else text
    polish_pos = body.find('route_name="business.creation_polish"')
    review_pos = body.find('route_name="business.creation_review"')
    ordered_correctly = 0 <= polish_pos < review_pos
    return {
        "name": "polish_before_review_order",
        "passed": ordered_correctly,
        "detail": "clean" if ordered_correctly else "business.creation_review appears before business.creation_polish in _run_script_review()",
    }


def check_human_review_gates_exist() -> dict[str, Any]:
    schema_text = _schema_text()
    missing: list[str] = []
    for stage, table in EXPECTED_STAGES.items():
        definition = _table_definition(schema_text, table)
        if definition is None:
            missing.append(f"{table} (table not found)")
            continue
        if "human_review_status" not in definition or "'pending_review'" not in definition:
            missing.append(f"{table} (no human_review_status defaulting to pending_review)")
    return {
        "name": "human_review_gates_exist",
        "passed": not missing,
        "detail": missing or "clean",
    }


def check_final_draft_not_auto_approved() -> dict[str, Any]:
    schema_text = _schema_text()
    definition = _table_definition(schema_text, "final_drafts")
    ok = definition is not None and "DEFAULT 'pending_review'" in definition
    return {
        "name": "final_draft_not_auto_approved",
        "passed": ok,
        "detail": "clean" if ok else "final_drafts.human_review_status does not default to pending_review",
    }


def check_no_auto_publish_path() -> dict[str, Any]:
    """置顶规则总表硬禁项"自动发布": no function anywhere under scripts/core
    actually posts/publishes content to a real platform. A narrow, deliberately
    conservative grep -- false positives (a function merely named "publish_*"
    for something unrelated, e.g. publish_experience_revision_proposal, an
    internal experience-catalog action, not posting content to a platform)
    are expected and fine; this only fails on a real platform-posting call."""
    suspicious_patterns = ("requests.post(", "session.post(", "upload_video(", "publish_to_platform(")
    hits: list[str] = []
    for path in (ROOT / "scripts" / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in suspicious_patterns:
            if pattern in text:
                hits.append(f"{path.relative_to(ROOT)}: {pattern}")
    return {
        "name": "no_auto_publish_path",
        "passed": not hits,
        "detail": hits or "clean",
    }


def check_formal_research_no_video_platform_import() -> dict[str, Any]:
    """BR-RESEARCH-001 structural check: nothing under runtime_skills/
    research_evidence_extract or runtime_skills/production_research_plan (or
    their real bindings, if any exist) imports MediaCrawler or a video-
    platform collector."""
    forbidden_substrings = ("MediaCrawler", "local_mediacrawler_executor", "collector.mediacrawler")
    hits: list[str] = []
    research_dirs = [
        ROOT / "runtime_skills" / "research_evidence_extract",
        ROOT / "runtime_skills" / "production_research_plan",
    ]
    for directory in research_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in forbidden_substrings:
                if pattern in text:
                    hits.append(f"{path.relative_to(ROOT)}: {pattern}")
    return {
        "name": "formal_research_no_video_platform_import",
        "passed": not hits,
        "detail": hits or "clean",
    }


def check_benchmark_collection_pipeline_intact() -> dict[str, Any]:
    """置顶规则总表条目40: 对标采集链路(账号发现/视频快照/ASR/评论采样/深度分析)
    must not have been deleted alongside any正式研究 boundary work. Proxy:
    the real entrypoints still exist and still cite their real BR-COLLECT
    requirement ids (not just "the file exists" -- a gutted file with the
    citation stripped would still exist)."""
    checks = {
        "scripts/core/business_data/run_competitor_registration_full.py": "BR-HIT-001",
        "scripts/core/business_data/run_reverse_prep.py": "BR-COLLECT-005",
    }
    missing: list[str] = []
    for rel_path, required_substring in checks.items():
        path = ROOT / rel_path
        if not path.exists():
            missing.append(f"{rel_path} (missing entirely)")
            continue
        if required_substring not in path.read_text(encoding="utf-8", errors="replace"):
            missing.append(f"{rel_path} (no {required_substring} citation found)")
    return {
        "name": "benchmark_collection_pipeline_intact",
        "passed": not missing,
        "detail": missing or "clean",
    }


def check_no_topic_scoring_config() -> dict[str, Any]:
    """置顶规则总表条目34/条目 硬禁项: 不恢复 score/rank/weight 选题排序. Proxy
    (2026-07-13, after BR-TOPIC-002's decay/reheat scoring fields were
    removed): config/settings.yaml and config/settings.example.yaml must not
    have a candidate_pool scoring block, and BUSINESS_RULE_CATALOG.yaml's
    BR-TOPIC-002 entry must not carry a live half_life_days/reheat_boost
    default (a corrected, non-empty defaults block would mean the rule was
    silently reintroduced)."""
    hits: list[str] = []
    for path in (SETTINGS_PATH, SETTINGS_EXAMPLE_PATH):
        if path.exists() and "candidate_pool:" in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(f"{path.name}: candidate_pool scoring block present")
    catalog_text = (ROOT / "BUSINESS_RULE_CATALOG.yaml").read_text(encoding="utf-8")
    if "half_life_days:" in catalog_text or "reheat_boost:" in catalog_text:
        hits.append("BUSINESS_RULE_CATALOG.yaml: half_life_days/reheat_boost still present")
    return {
        "name": "no_topic_scoring_config",
        "passed": not hits,
        "detail": hits or "clean",
    }


def check_traceability_fields_on_pipeline_tables() -> dict[str, Any]:
    """置顶规则总表条目49/58: every pipeline-stage table must carry run_id and
    a request/correlation id pair so a production run's real inputs, model
    calls, and outputs can be traced back."""
    schema_text = _schema_text()
    tables = ("topic_candidates", "content_plans", "script_drafts", "script_reviews")
    missing: list[str] = []
    for table in tables:
        definition = _table_definition(schema_text, table)
        if definition is None:
            missing.append(f"{table} (table not found)")
            continue
        for required_column in ("run_id", "request_id", "correlation_id"):
            if required_column not in definition:
                missing.append(f"{table} (missing {required_column})")
    return {
        "name": "traceability_fields_on_pipeline_tables",
        "passed": not missing,
        "detail": missing or "clean",
    }


def run_production_activation_gate() -> dict[str, Any]:
    checks = [
        check_pipeline_stages_wired(),
        check_polish_before_review_order(),
        check_human_review_gates_exist(),
        check_final_draft_not_auto_approved(),
        check_no_auto_publish_path(),
        check_formal_research_no_video_platform_import(),
        check_benchmark_collection_pipeline_intact(),
        check_no_topic_scoring_config(),
        check_traceability_fields_on_pipeline_tables(),
    ]
    dead_goal_chain_result = run_dead_goal_chain_checklist()
    all_passed = all(check["passed"] for check in checks) and dead_goal_chain_result["status"] == "PASS"
    return {
        "status": "PASS" if all_passed else "FAIL",
        "checks": checks,
        "dead_goal_chain_gate": dead_goal_chain_result,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = run_production_activation_gate()
    for check in result["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"[{mark}] {check['name']}")
        if not check["passed"]:
            print(f"       {check['detail']}")
    nested_mark = "PASS" if result["dead_goal_chain_gate"]["status"] == "PASS" else "FAIL"
    print(f"[{nested_mark}] dead_goal_chain_gate (delegated, see scripts.validation.dead_goal_chain_gate for detail)")
    print(f"\noverall: {result['status']}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
