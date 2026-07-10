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

What counts as one "dna_note_ref" here: each ref packs a labeled, complete
copy of all three real sample_deep_analyze pattern fields (topic_pattern/
hook_pattern/structure_pattern) for one piece of evidence. 2026-07-11, in two
passes: first the Skill's own dna_note_ref_max was raised 160->320 (a single
note could not carry all three fields at all at 160); then, after the user
explicitly asked for all character-length caps on this note-packing to be
removed (following a real bug where a fixed 90-char-per-field split silently
cut real content -- structure_pattern's real max observed was 233 chars, a
multi-step narrative breakdown, cut to 90 lost the back half), the binding
code no longer truncates notes at all -- _note_for_evidence_row() returns the
full, untruncated string, unconditionally. dna_note_ref_max was raised again,
320->2500, derived from sample_deep_analyze's own output_schema.yaml ceiling
(3 fields x 800 chars max + ~100 chars overhead for the analysis_id/labels)
so that no real note this pipeline can ever produce is capable of exceeding
it -- this is a provable ceiling, not an empirical one. If a future note
somehow still exceeded 2500 chars, runtime_skills/tactic_extract's own input
validation would reject the job with a clear schema error rather than the
content being silently cut, which is the correct failure mode here.

Residual, accepted risk (not solved by this pass, called out rather than
hidden): context_budget.max_input_tokens is 14000, and 20 notes at the true
2500-char ceiling would be ~33000 estimated tokens -- if every note in a
batch happened to be near that ceiling simultaneously, the real call could
exceed the model's actual context window. Real observed data is nowhere
near this (20 real notes total ~3000 chars, ~2000 tokens) -- this is
flagged as a known, currently-inert edge case, not dynamically guarded
against by shrinking batch size, since that was not asked for here.

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
# 2026-07-11 (third pass): per explicit user instruction, all character
# caps on note packing are removed -- _note_for_evidence_row() no longer
# truncates. This constant now exists only to declare the schema's ceiling
# (dna_note_ref_max in TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml/input_schema.yaml),
# derived from sample_deep_analyze's own output_schema.yaml guarantee (3
# fields x 800 chars max + ~100 chars overhead) so it can never actually bind
# against real content this pipeline produces -- see module docstring for
# the full derivation and the accepted residual risk.
NOTE_MAX_CHARS = 2500


def validate_run_tactic_extract_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-EXPERIENCE-001"])


def select_evidence_for_tactic_batch(conn: sqlite3.Connection, *, domain_label: str, limit: int) -> list[sqlite3.Row]:
    """Real, already-registered hit_deep_analysis_evidence trace_versions
    (evidence_registry.py's output) for one domain, that no existing tactic
    candidate has referenced yet. Oldest-registered first.

    2026-07-11: only the LATEST hit_deep_analysis version per hit_id is
    eligible (matches select_hits_pending_analysis()'s same convention) --
    a hit can have multiple registered evidence versions (e.g. re-analyzed
    after a prompt fix) and a stale earlier version must not be selected
    over a newer one just because it happened to be registered first."""
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
           AND hit_deep_analysis.version = (
               SELECT MAX(version) FROM hit_deep_analysis AS d2 WHERE d2.hit_id = hit_deep_analysis.hit_id
           )
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


class NoteExceedsSchemaCeilingError(ValueError):
    """Raised when a real note would violate dna_note_ref_max
    (NOTE_MAX_CHARS) -- per this constant's derivation, real upstream
    content can never actually reach this, so hitting this error means
    sample_deep_analyze's own output_schema.yaml field caps changed without
    this module being updated to match. Failing loudly here, before a real
    paid API call, is deliberate -- the alternative (silently truncating)
    is exactly what this module stopped doing per user instruction."""


def _note_for_evidence_row(row: sqlite3.Row) -> str:
    """Builds the note from the full, untruncated real pattern fields --
    no character cap is applied here (2026-07-11, per explicit user
    instruction; see module docstring)."""
    topic = row["topic_pattern"] or ""
    hook = row["hook_pattern"] or ""
    structure = row["structure_pattern"] or ""
    return f"{row['analysis_id']}:选题={topic};钩子={hook};结构={structure}"


def assemble_dna_note_refs(evidence_rows: list[sqlite3.Row]) -> list[str]:
    notes = [_note_for_evidence_row(row) for row in evidence_rows]
    for row, note in zip(evidence_rows, notes):
        if len(note) > NOTE_MAX_CHARS:
            raise NoteExceedsSchemaCeilingError(
                f"note for analysis_id={row['analysis_id']!r} is {len(note)} chars, "
                f"exceeding the schema ceiling of {NOTE_MAX_CHARS} -- see NOTE_MAX_CHARS's "
                "derivation comment; this should be mathematically impossible with real data"
            )
    return notes


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
