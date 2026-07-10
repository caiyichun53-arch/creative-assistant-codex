"""Core-side wiring between evidence_registry.py's real registered evidence
(hit_deep_analysis_evidence trace_versions) and the runtime_skills/
tactic_extract atomic Skill (TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml), with
tactic_registry.py doing the real persistence once the Skill returns.

Division of responsibility, same pattern as run_sample_deep_analyze.py/
run_source_to_topic.py's module docstrings (not repeated here):
  - select_evidence_for_tactic_batch()/assemble_tactic_extract_input():
    Core's Input Binding.
  - generate_one_tactic_candidate(): calls the Skill through
    make_tactic_extract_harness(), pulls result["output"] out of the harness
    BEFORE closing it (the harness's own internal trace_version bookkeeping
    is ephemeral -- PersistenceStore.in_memory(), discarded on close()),
    then hands that plain dict to tactic_registry.register_tactic_candidate()
    for real persistence.

What counts as one "dna_note_ref" here: each ref packs a labeled, full-length
(not per-field-truncated) copy of all three real sample_deep_analyze pattern
fields (topic_pattern/hook_pattern/structure_pattern) for one piece of
evidence, not just one field, and not a fixed slice of each -- 2026-07-11 the
Skill's own dna_note_ref_max was raised 160->320->450 in two passes, the
second one after checking real data proved a fixed 90-char-per-field split
was silently cutting real content (structure_pattern in particular is
naturally the longest field, a multi-step narrative breakdown). See
NOTE_MAX_CHARS's comment for the real numbers. Picking only one field per
note, or cutting each field to the same fixed size regardless of its real
content, would both silently hide real signal from the model doing the
reduction -- count_truncated_notes() makes any remaining truncation visible
in run_tactic_extract()'s report instead of leaving it silent.

A single tactic_extract call can only reduce 2-20 pieces of evidence
(dna_note_refs_min/max) and only one domain_label at a time (no mixing) --
select_evidence_for_tactic_batch() takes domain_label as a required filter,
callers choosing to combine multiple domains must make multiple calls.

Usage:
    python -m scripts.core.experience.run_tactic_extract --domain-label fan_kepu_social_life --limit 20
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
from scripts.core.experience.tactic_registry import TacticEvidenceRef, register_tactic_candidate  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    make_tactic_extract_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402

ALLOWED_DOMAIN_LABELS = {"fan_kepu_social_life", "music_entertainment", "third_domain_neutral", "cross_domain", "unknown"}
# TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml's evidence_requirements/input_length_limits.
BATCH_MIN = 2
BATCH_MAX = 20
# 2026-07-11 (second pass): raised 320->450 after checking real data, not
# just token math. The first version of this note-packing split a fixed
# 90-char budget per field (topic/hook/structure) -- checked against all 22
# real hit_deep_analysis rows in production_activation.sqlite3 at the time,
# that silently cut real content on 2 of them (structure_pattern's real max
# was 233 chars -- a 10-step structure breakdown -- getting cut to 90 loses
# steps 5-10 entirely). Fixed by no longer capping each field independently:
# the note is now built from the full, untruncated fields and only the
# WHOLE string is truncated, only if it exceeds NOTE_MAX_CHARS. Real max
# combined (analysis_id + all three fields, no per-field cut) across those
# same 22 rows was 404 chars; 450 covers that with headroom. sample_deep_
# analyze's own output_schema.yaml allows up to 800 chars per field (2400+
# for one row in the theoretical worst case), so truncation is still
# possible for an unusually verbose future row -- assemble_dna_note_refs()
# reports how many notes actually got cut so that is never silent.
NOTE_MAX_CHARS = 450


def validate_run_tactic_extract_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-EXPERIENCE-001"])


def select_evidence_for_tactic_batch(conn: sqlite3.Connection, *, domain_label: str, limit: int) -> list[sqlite3.Row]:
    """Real, already-registered hit_deep_analysis_evidence trace_versions
    (evidence_registry.py's output) for one domain, that no existing tactic
    candidate has referenced yet. Oldest-registered first."""
    return conn.execute(
        """
        SELECT hit_deep_analysis.analysis_id AS analysis_id,
               hit_deep_analysis.topic_pattern AS topic_pattern,
               hit_deep_analysis.hook_pattern AS hook_pattern,
               hit_deep_analysis.structure_pattern AS structure_pattern,
               object_reference.source_version_id AS evidence_version_id,
               object_reference.target_content_hash AS evidence_content_hash,
               trace_version.created_at AS evidence_registered_at
          FROM object_reference
          JOIN trace_version ON trace_version.version_id = object_reference.source_version_id
          JOIN hit_deep_analysis ON hit_deep_analysis.analysis_id = object_reference.target_stable_id
          JOIN hits ON hits.hit_id = hit_deep_analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
         WHERE object_reference.target_object_kind = 'hit_deep_analysis'
           AND account.domain_label = ?
           AND NOT EXISTS (
               SELECT 1 FROM object_reference AS tactic_ref
                WHERE tactic_ref.target_object_kind = 'hit_deep_analysis_evidence'
                  AND tactic_ref.target_stable_id = hit_deep_analysis.analysis_id
           )
         ORDER BY trace_version.created_at
         LIMIT ?
        """,
        (domain_label, limit),
    ).fetchall()


def _note_for_evidence_row(row: sqlite3.Row) -> str:
    """Builds the full, untruncated note (all three real pattern fields, no
    per-field cut) and only truncates the WHOLE string if it exceeds
    NOTE_MAX_CHARS -- see that constant's comment for why a per-field split
    was tried first and abandoned (it silently lost real content)."""
    topic = row["topic_pattern"] or ""
    hook = row["hook_pattern"] or ""
    structure = row["structure_pattern"] or ""
    note = f"{row['analysis_id']}:选题={topic};钩子={hook};结构={structure}"
    return note[:NOTE_MAX_CHARS]


def assemble_dna_note_refs(evidence_rows: list[sqlite3.Row]) -> list[str]:
    return [_note_for_evidence_row(row) for row in evidence_rows]


def count_truncated_notes(evidence_rows: list[sqlite3.Row]) -> int:
    """How many notes in this batch actually got cut by NOTE_MAX_CHARS --
    surfaced in run_tactic_extract()'s report so truncation is never silent,
    even though it is now rare (see NOTE_MAX_CHARS's derivation)."""
    count = 0
    for row in evidence_rows:
        topic = row["topic_pattern"] or ""
        hook = row["hook_pattern"] or ""
        structure = row["structure_pattern"] or ""
        full_length = len(f"{row['analysis_id']}:选题={topic};钩子={hook};结构={structure}")
        if full_length > NOTE_MAX_CHARS:
            count += 1
    return count


def assemble_tactic_extract_input(
    evidence_rows: list[sqlite3.Row], *, request_id: str, correlation_id: str, analysis_batch_id: str, domain_label: str
) -> dict[str, Any]:
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"
    return {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "analysis_batch_id": analysis_batch_id,
        "dna_note_refs": assemble_dna_note_refs(evidence_rows),
        "domain_label": domain_label,
        "schema_version": "tactic_extract.input.v1",
    }


def generate_one_tactic_candidate(
    conn: sqlite3.Connection,
    harness: FormalBusinessSkillHarness,
    evidence_rows: list[sqlite3.Row],
    *,
    request_id: str,
    analysis_batch_id: str,
    domain_label: str,
    run_id: str,
    actor: str = "run_tactic_extract",
) -> dict[str, Any]:
    input_payload = assemble_tactic_extract_input(
        evidence_rows, request_id=request_id, correlation_id=run_id, analysis_batch_id=analysis_batch_id, domain_label=domain_label
    )
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"request_id": request_id, "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"request_id": request_id, "status": "failed", "reason": "no result materialized"}

    evidence_refs = tuple(
        TacticEvidenceRef(
            analysis_id=row["analysis_id"],
            evidence_version_id=row["evidence_version_id"],
            evidence_content_hash=row["evidence_content_hash"],
        )
        for row in evidence_rows
    )
    registration = register_tactic_candidate(
        conn,
        request_id=request_id,
        analysis_batch_id=analysis_batch_id,
        domain_label=domain_label,
        tactic_extract_output=result["output"],
        evidence_refs=evidence_refs,
        actor=actor,
        correlation_id=run_id,
    )
    return {
        "request_id": request_id,
        "status": "completed",
        "tactic_id": registration.tactic_id,
        "version_id": registration.version_id,
        "evidence_count": len(evidence_refs),
        "replayed": registration.replayed,
    }


def run_tactic_extract(
    conn: sqlite3.Connection, *, limit: int, domain_label: str, harness: FormalBusinessSkillHarness, actor: str = "run_tactic_extract"
) -> dict[str, Any]:
    validate_run_tactic_extract_execution_contract()
    run_id = "tactic_extract_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_rows = select_evidence_for_tactic_batch(conn, domain_label=domain_label, limit=limit)
    if len(evidence_rows) < BATCH_MIN:
        return {
            "status": "succeeded",
            "run_id": run_id,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "results": [],
            "note": f"only {len(evidence_rows)} eligible evidence rows for domain_label={domain_label!r}, need at least {BATCH_MIN}",
        }
    batch = evidence_rows[:BATCH_MAX]
    truncated_notes = count_truncated_notes(batch)
    request_id = f"tactic_extract_{run_id}"
    result = generate_one_tactic_candidate(
        conn, harness, batch, request_id=request_id, analysis_batch_id=run_id, domain_label=domain_label, run_id=run_id, actor=actor
    )
    completed = 1 if result["status"] == "completed" else 0
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": 1,
        "completed": completed,
        "failed": 1 - completed,
        "batch_size": len(batch),
        "truncated_notes": truncated_notes,
        "results": [result],
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential shape as run_sample_deep_analyze.py/run_source_to_topic.py's
    build_real_harness(): HERMES_BUSINESS_MODEL_* must be in the real .env
    (2026-07-11: confirmed real, copied in from .env.live-gates by explicit
    user decision -- see HANDOFF_STATE.md)."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.reverse_pattern_reduce",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="tactic_extract.production.v1",
        config_hash=content_hash({"route": "business.reverse_pattern_reduce", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0, "max_completion_tokens": 2800, "response_format": {"type": "json_object"}},
        timeout_ms=90000,
    )
    harness = make_tactic_extract_harness(provider=adapter, route=route)  # type: ignore[arg-type]
    return harness, model_name


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Reduce a batch of registered evidence into a candidate tactic via runtime_skills/tactic_extract.")
    parser.add_argument("--domain-label", required=True, choices=sorted(ALLOWED_DOMAIN_LABELS))
    parser.add_argument("--limit", type=int, default=BATCH_MAX)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Override which .env-style file supplies HERMES_BUSINESS_MODEL_* credentials (defaults to the real .env).",
    )
    args = parser.parse_args(argv)

    db_path = _safe_db_path(Path(args.db))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    env_path = Path(args.env_file) if args.env_file else None
    harness, model_name = build_real_harness(env_path=env_path)
    try:
        install_schema(conn)
        report = run_tactic_extract(conn, limit=args.limit, domain_label=args.domain_label, harness=harness)
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
