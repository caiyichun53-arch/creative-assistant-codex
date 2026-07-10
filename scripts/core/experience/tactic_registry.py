"""Registers a real tactic_extract result as a persisted candidate tactic --
the second half of production activation's阶段3 (see ROADMAP.md and the
approved production-activation plan referenced there). Sibling to
scripts/core/experience/evidence_registry.py (阶段2), which registers a real
hit_deep_analysis row as citable VersionRef evidence; this module is what a
batch of that evidence becomes once tactic_extract has reduced it into
common_patterns/example_candidates.

One tactic candidate = one trace_root (object_kind="tactic"). The tactic's
own trace_version payload IS the tactic_extract output (common_patterns/
example_candidates/domain_label/analysis_batch_id) -- there is no separate
root for "the skill run" and another for "the tactic itself"; duplicating
the same content under two roots would have no addressable benefit, since
the harness's own internal trace_version for this run is ephemeral (see
run_sample_deep_analyze.py's build_real_harness() docstring precedent: the
harness's PersistenceStore.in_memory() job/scheduler bookkeeping is
discarded on harness.close(), so real persistence has always had to happen
here, in the Core-side binding/registry layer, not inside the Skill's own
materializer).

Idempotency is content-addressed, matching evidence_registry.py's pattern,
not a caught-duplicate-key trick: re-registering the same request_id looks
up whether an object_reference already points at that request_id with the
exact same tactic_extract_output content_hash, and if so returns the
existing tactic/version/state untouched. A different output under the same
request_id raises IdempotencyConflict -- this reuses the Skill contract's
own declared idempotency scheme (TACTIC_EXTRACT_BUSINESS_CONTRACT.yaml's
`idempotency.key: tactic_extract:{request_id}`, `duplicate_different_request:
reject`), it does not invent a new one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash  # noqa: E402
from scripts.core.persistence.goal02_store import Goal02StateStore  # noqa: E402

TACTIC_ROOT_OBJECT_KIND = "tactic"
TACTIC_PROJECTION_VERSION = "goal02.tactic_candidate.v1"
SKILL_RUN_TARGET_OBJECT_KIND = "tactic_extract_skill_run"
SKILL_RUN_OUTPUT_PROJECTION_VERSION = "tactic_extract.output.v1"
EVIDENCE_TARGET_OBJECT_KIND = "hit_deep_analysis_evidence"
SKILL_RUN_RELATION_ROLE = "sources_from_tactic_extract_skill_run"
EVIDENCE_RELATION_ROLE = "sources_from_hit_deep_analysis_evidence"


@dataclass(frozen=True)
class TacticEvidenceRef:
    """One piece of already-registered evidence (evidence_registry.py's
    register_hit_deep_analysis_evidence() output) that a tactic candidate is
    being reduced from."""

    analysis_id: str
    evidence_version_id: str
    evidence_content_hash: str


@dataclass(frozen=True)
class TacticRegistrationResult:
    tactic_id: str
    version_id: str
    state_command_id: str
    skill_run_reference_id: str
    evidence_reference_ids: tuple[str, ...]
    replayed: bool = False


def _find_existing_registration(
    conn: sqlite3.Connection, request_id: str, output_hash: str
) -> TacticRegistrationResult | None:
    skill_run_ref = conn.execute(
        """
        SELECT object_reference.reference_id AS reference_id,
               object_reference.source_version_id AS version_id,
               object_reference.target_content_hash AS target_content_hash,
               trace_version.root_id AS root_id
          FROM object_reference
          JOIN trace_version ON trace_version.version_id = object_reference.source_version_id
         WHERE object_reference.target_object_kind = ?
           AND object_reference.target_stable_id = ?
        """,
        (SKILL_RUN_TARGET_OBJECT_KIND, request_id),
    ).fetchone()
    if skill_run_ref is None:
        return None
    if skill_run_ref["target_content_hash"] != output_hash:
        raise IdempotencyConflict(
            f"request_id={request_id!r} was already registered with a different tactic_extract output"
        )

    evidence_refs = conn.execute(
        """
        SELECT reference_id FROM object_reference
         WHERE source_version_id = ? AND target_object_kind = ?
        """,
        (skill_run_ref["version_id"], EVIDENCE_TARGET_OBJECT_KIND),
    ).fetchall()
    tactic_state_row = conn.execute(
        "SELECT * FROM tactic_state WHERE tactic_id=?", (skill_run_ref["root_id"],)
    ).fetchone()
    if tactic_state_row is None:
        raise IdempotencyConflict(
            f"request_id={request_id!r} has a registered trace_version but no tactic_state row -- "
            "a prior registration attempt must have partially failed outside its transaction"
        )
    return TacticRegistrationResult(
        tactic_id=skill_run_ref["root_id"],
        version_id=skill_run_ref["version_id"],
        state_command_id=tactic_state_row["updated_by_command_id"],
        skill_run_reference_id=skill_run_ref["reference_id"],
        evidence_reference_ids=tuple(row["reference_id"] for row in evidence_refs),
        replayed=True,
    )


def register_tactic_candidate(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    analysis_batch_id: str,
    domain_label: str,
    tactic_extract_output: dict[str, Any],
    evidence_refs: tuple[TacticEvidenceRef, ...],
    actor: str,
    correlation_id: str,
) -> TacticRegistrationResult:
    """Persists one real tactic_extract result as a candidate tactic. Must be
    called with the Skill's output already pulled out of the harness (see
    run_tactic_extract.py) -- this function takes a plain dict, not a live
    harness handle, since the harness's own internal bookkeeping is
    ephemeral and gone by the time persistence needs to happen for real."""
    require_catalog_citations(["BR-EXPERIENCE-001"])
    if len(evidence_refs) < 2:
        raise ValueError("register_tactic_candidate requires at least 2 evidence_refs (matches tactic_extract's own dna_note_refs_min)")

    output_hash = content_hash(tactic_extract_output, SKILL_RUN_OUTPUT_PROJECTION_VERSION)
    existing = _find_existing_registration(conn, request_id, output_hash)
    if existing is not None:
        return existing

    idempotency_key = f"tactic_extract:{request_id}"
    payload = {
        "analysis_batch_id": analysis_batch_id,
        "domain_label": domain_label,
        "common_patterns": tactic_extract_output["common_patterns"],
        "example_candidates": tactic_extract_output["example_candidates"],
    }

    store = PersistenceStore(conn)
    goal02 = Goal02StateStore(store)
    with conn:
        root_id = store.create_root(TACTIC_ROOT_OBJECT_KIND)
        version_id = store.append_version(root_id, payload, projection_version=TACTIC_PROJECTION_VERSION)
        store.set_current_version(root_id, version_id)

        skill_run_reference_id = store.record_object_reference(
            source_version_id=version_id,
            relation_role=SKILL_RUN_RELATION_ROLE,
            target_object_kind=SKILL_RUN_TARGET_OBJECT_KIND,
            target_stable_id=request_id,
            target_content_hash=output_hash,
            locator={"request_id": request_id, "analysis_batch_id": analysis_batch_id},
        )
        evidence_reference_ids = tuple(
            store.record_object_reference(
                source_version_id=version_id,
                relation_role=EVIDENCE_RELATION_ROLE,
                target_object_kind=EVIDENCE_TARGET_OBJECT_KIND,
                target_stable_id=ref.analysis_id,
                target_version_id=ref.evidence_version_id,
                target_content_hash=ref.evidence_content_hash,
                locator={"analysis_id": ref.analysis_id},
            )
            for ref in evidence_refs
        )
        state_result = goal02.create_state(
            object_kind=TACTIC_ROOT_OBJECT_KIND,
            object_id=root_id,
            initial_state="candidate",
            basis_version_id=version_id,
            actor=actor,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )

    return TacticRegistrationResult(
        tactic_id=root_id,
        version_id=version_id,
        state_command_id=state_result.command_id,
        skill_run_reference_id=skill_run_reference_id,
        evidence_reference_ids=evidence_reference_ids,
        replayed=False,
    )
