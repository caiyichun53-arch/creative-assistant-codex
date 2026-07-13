"""Registers a real hit_deep_analysis row as a VersionRef evidence
version. This is the first thing written into the
trace_root/trace_version tables that阶段1 (install_versionref_schema_
into_business_db.py) installed into the real business database but left
completely empty.

Why this exists: BR-EXPERIENCE-001 requires experience to have a real
formal-artifact truth source. Before any experience (tactic_state, later)
can cite a real analysis result as its basis, that analysis result has to
first exist as a real trace_version -- this module is that one step, and
nothing more. It does not do any tactic extraction or judgement; it only
makes a real hit_deep_analysis row *citable*.

What "evidence" means here: the three real pattern fields sample_deep_
analyze already produced (topic_pattern/hook_pattern/structure_pattern) on
a specific hit_deep_analysis row, identified by its real analysis_id. Each
registration creates one trace_root (object_kind="hit_deep_analysis_
evidence") + one trace_version whose payload is exactly those three fields,
plus one object_reference from that version back to the real analysis_id
it came from (target_object_kind="hit_deep_analysis", target_stable_id=
analysis_id) -- so the reference is independently traceable to the source
row, not just a copy of its content.

Idempotency is content-addressed, not a caught-duplicate-key trick:
re-registering the same analysis_id looks up whether an object_reference
already points at that analysis_id with the exact same content_hash, and
if so returns the existing root/version/reference untouched -- no second
trace_version row is ever created for identical content. hit_deep_analysis
rows are immutable in this codebase (a re-analysis creates a new analysis_id
suffixed _v2 etc., see run_sample_deep_analyze.py), so "same analysis_id,
different content" is not a real scenario this needs to handle -- it does
not silently paper over it either; the query is content_hash-scoped so an
inconsistency like that would fall through to creating a second, separately
traceable evidence version rather than being confused for a no-op.

Usage (library only, no CLI yet -- 阶段3 will be the first real caller):
    from scripts.core.experience.evidence_registry import register_hit_deep_analysis_evidence
    register_hit_deep_analysis_evidence(conn, "hit_37ae1202dd597fcc3039_v2")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.persistence.goal01_store import PersistenceStore, content_hash  # noqa: E402

EVIDENCE_ROOT_OBJECT_KIND = "hit_deep_analysis_evidence"
EVIDENCE_TARGET_OBJECT_KIND = "hit_deep_analysis"
EVIDENCE_RELATION_ROLE = "sources_from_hit_deep_analysis"
EVIDENCE_PROJECTION_VERSION = "goal02.hit_deep_analysis_evidence.v1"


class EvidenceRegistrationError(RuntimeError):
    """Raised when asked to register evidence for an analysis_id that does
    not exist in the real hit_deep_analysis table -- registering evidence
    for a source that isn't real would create a trace_version with nothing
    genuine behind it, and an object_reference pointing at nothing."""


def _load_hit_deep_analysis_row(conn: sqlite3.Connection, analysis_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT analysis_id, hit_id, topic_pattern, hook_pattern, structure_pattern
          FROM hit_deep_analysis
         WHERE analysis_id = ?
        """,
        (analysis_id,),
    ).fetchone()


def _evidence_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "topic_pattern": row["topic_pattern"],
        "hook_pattern": row["hook_pattern"],
        "structure_pattern": row["structure_pattern"],
    }


def _find_existing_registration(
    conn: sqlite3.Connection, analysis_id: str, target_hash: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT object_reference.reference_id AS reference_id,
               object_reference.source_version_id AS version_id,
               trace_version.root_id AS root_id
          FROM object_reference
          JOIN trace_version ON trace_version.version_id = object_reference.source_version_id
         WHERE object_reference.target_object_kind = ?
           AND object_reference.target_stable_id = ?
           AND object_reference.target_content_hash = ?
        """,
        (EVIDENCE_TARGET_OBJECT_KIND, analysis_id, target_hash),
    ).fetchone()
    if row is None:
        return None
    return {
        "root_id": row["root_id"],
        "version_id": row["version_id"],
        "reference_id": row["reference_id"],
        "target_content_hash": target_hash,
        "replayed": True,
    }


def register_hit_deep_analysis_evidence(conn: sqlite3.Connection, analysis_id: str) -> dict[str, Any]:
    """Registers one real hit_deep_analysis row as a VersionRef evidence
    version. Idempotent: calling this twice for the same analysis_id
    results in exactly one trace_version row, not two (see module docstring)."""
    require_baseline_citations(["9", "18"])

    row = _load_hit_deep_analysis_row(conn, analysis_id)
    if row is None:
        raise EvidenceRegistrationError(
            f"no hit_deep_analysis row exists for analysis_id={analysis_id!r} -- refusing to "
            "register evidence for a source that is not real"
        )

    payload = _evidence_payload(row)
    target_hash = content_hash(payload, EVIDENCE_PROJECTION_VERSION)

    existing = _find_existing_registration(conn, analysis_id, target_hash)
    if existing is not None:
        return existing

    store = PersistenceStore(conn)
    with conn:
        root_id = store.create_root(EVIDENCE_ROOT_OBJECT_KIND)
        version_id = store.append_version(
            root_id,
            payload,
            projection_version=EVIDENCE_PROJECTION_VERSION,
        )
        store.set_current_version(root_id, version_id)
        reference_id = store.record_object_reference(
            source_version_id=version_id,
            relation_role=EVIDENCE_RELATION_ROLE,
            target_object_kind=EVIDENCE_TARGET_OBJECT_KIND,
            target_stable_id=analysis_id,
            target_content_hash=target_hash,
            locator={"table": "hit_deep_analysis", "analysis_id": analysis_id, "hit_id": row["hit_id"]},
        )

    return {
        "root_id": root_id,
        "version_id": version_id,
        "reference_id": reference_id,
        "target_content_hash": target_hash,
        "replayed": False,
    }
