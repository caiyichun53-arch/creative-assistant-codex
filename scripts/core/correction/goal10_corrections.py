from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash
from scripts.core.production.goal08_production_chain import VersionRef


class CorrectionError(RuntimeError):
    pass


ACTION_KINDS = frozenset(
    {
        "no_action",
        "deterministic_recompute",
        "semantic_rerun",
        "replace_current_candidate",
        "require_user_reconfirmation",
        "historical_annotation",
        "human_attention_required",
    }
)

RECONFIRMATION_KINDS = frozenset({"production_approved_draft", "production_approval"})
HISTORICAL_ANNOTATION_KINDS = frozenset({"production_publication_capture"})
SEMANTIC_RERUN_ROLES = frozenset({"input_assembly_included_ref", "binding_local_ref", "generated_by_model_run"})


@dataclass(frozen=True)
class CorrectionRegistrationCommand:
    actor: str
    idempotency_key: str
    target_ref: VersionRef
    corrected_payload: dict[str, Any]
    change_scope: str
    reason: str
    evidence_refs: tuple[VersionRef, ...] = ()
    safety_limits: dict[str, int] | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "target_ref": self.target_ref.as_payload(),
            "corrected_payload_hash": content_hash(self.corrected_payload, "goal10.corrected_payload.v1"),
            "change_scope": self.change_scope,
            "reason": self.reason,
            "evidence_refs": [ref.as_payload() for ref in self.evidence_refs],
            "safety_limits": self.safety_limits or {},
        }


@dataclass(frozen=True)
class DependencyEdge:
    source_version_id: str
    source_object_kind: str
    relation_role: str
    target_ref: VersionRef
    edge_source: str
    local_ref: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_version_id": self.source_version_id,
            "source_object_kind": self.source_object_kind,
            "relation_role": self.relation_role,
            "target_ref": self.target_ref.as_payload(),
            "edge_source": self.edge_source,
            "local_ref": self.local_ref,
        }


@dataclass(frozen=True)
class CorrectionImpact:
    root_id: str
    version_id: str
    target_version_id: str
    action_kind: str
    expected_basis_hash: str
    replayed: bool = False


@dataclass(frozen=True)
class CorrectionRegistrationResult:
    root_id: str
    version_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    dependency_index_version_ids: tuple[str, ...]
    impacts: tuple[CorrectionImpact, ...]
    replayed: bool = False


class CorrectionMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.conn = store.conn

    def register_correction(self, command: CorrectionRegistrationCommand) -> CorrectionRegistrationResult:
        self._validate_command(command)
        existing = self._existing_result(command)
        if existing is not None:
            return existing
        old_hash = self._version_content_hash(command.target_ref.target_version_id)
        corrected_hash = content_hash(command.corrected_payload, "goal10.corrected_payload.v1")
        with self.conn:
            root_id = self.store.create_root("goal10_correction_record")
            payload = {
                "goal": "GOAL-10",
                "target_ref": command.target_ref.as_payload(),
                "old_content_hash": old_hash,
                "corrected_payload_hash": corrected_hash,
                "change_scope": command.change_scope,
                "reason": command.reason,
                "evidence_refs": [ref.as_payload() for ref in command.evidence_refs],
                "safety_limits": command.safety_limits or {},
                "status": "registered",
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version="goal10.correction_record.v1",
                business_payload={
                    "target_stable_id": command.target_ref.target_stable_id,
                    "target_version_id": command.target_ref.target_version_id,
                    "old_content_hash": old_hash,
                    "corrected_payload_hash": corrected_hash,
                    "change_scope": command.change_scope,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self._record_ref(version_id, command.target_ref)
            for ref in command.evidence_refs:
                self._record_ref(version_id, ref)

            dependency_versions: list[str] = []
            impacts: list[CorrectionImpact] = []
            for edge in self._direct_dependency_edges(command.target_ref):
                dependency_versions.append(self._record_dependency_index(version_id, edge))
                impacts.append(self._record_impact(version_id, edge))

            result_payload = {
                "root_id": root_id,
                "version_id": version_id,
                "dependency_index_version_ids": dependency_versions,
                "impact_version_ids": [impact.version_id for impact in impacts],
            }
            receipt_id = self.store.record_command(
                command_scope="goal10.correction.register",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal10.correction.registered",
                actor=command.actor,
                object_kind="goal10_correction_record",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "target_ref": command.target_ref.as_payload(),
                    "dependency_count": len(dependency_versions),
                    "impact_count": len(impacts),
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal10.correction.registered",
                payload={
                    "root_id": root_id,
                    "version_id": version_id,
                    "target_ref": command.target_ref.as_payload(),
                    "impact_count": len(impacts),
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=audit_id,
            )
            return CorrectionRegistrationResult(
                root_id=root_id,
                version_id=version_id,
                receipt_id=receipt_id,
                audit_id=audit_id,
                outbox_id=outbox_id,
                dependency_index_version_ids=tuple(dependency_versions),
                impacts=tuple(impacts),
            )

    def _record_dependency_index(self, correction_version_id: str, edge: DependencyEdge) -> str:
        root_id = self.store.create_root("goal10_dependency_index")
        payload = {
            "goal": "GOAL-10",
            "correction_version_id": correction_version_id,
            "edge": edge.as_payload(),
        }
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal10.dependency_index.v1",
            business_payload={
                "correction_version_id": correction_version_id,
                "source_version_id": edge.source_version_id,
                "target_version_id": edge.target_ref.target_version_id,
                "relation_role": edge.relation_role,
            },
        )
        self.store.set_current_version(root_id, version_id)
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="indexed_correction_record",
            target_object_kind="goal10_correction_record",
            target_stable_id=correction_version_id,
            target_version_id=correction_version_id,
            target_content_hash=None,
            locator={"version_id": correction_version_id},
        )
        self._record_ref(version_id, edge.target_ref)
        return version_id

    def _record_impact(self, correction_version_id: str, edge: DependencyEdge) -> CorrectionImpact:
        action_kind = _action_kind_for(edge)
        expected_basis_hash = _impact_basis_hash(correction_version_id, edge, action_kind)
        root_id = self.store.create_root("goal10_correction_impact")
        payload = {
            "goal": "GOAL-10",
            "correction_version_id": correction_version_id,
            "target_version_id": edge.source_version_id,
            "dependency": edge.as_payload(),
            "action_kind": action_kind,
            "expected_basis_hash": expected_basis_hash,
            "processing_status": "planned",
            "result": None,
            "stop_reason": None,
            "human_attention": None,
        }
        version_id = self.store.append_version(
            root_id,
            payload,
            projection_version="goal10.correction_impact.v1",
            business_payload={
                "correction_version_id": correction_version_id,
                "target_version_id": edge.source_version_id,
                "action_kind": action_kind,
                "expected_basis_hash": expected_basis_hash,
                "processing_status": "planned",
            },
        )
        self.store.set_current_version(root_id, version_id)
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="impact_for_correction_record",
            target_object_kind="goal10_correction_record",
            target_stable_id=correction_version_id,
            target_version_id=correction_version_id,
            target_content_hash=None,
            locator={"version_id": correction_version_id},
        )
        self.store.record_object_reference(
            source_version_id=version_id,
            relation_role="impact_target_version",
            target_object_kind=edge.source_object_kind,
            target_stable_id=edge.source_version_id,
            target_version_id=edge.source_version_id,
            target_content_hash=None,
            locator={"version_id": edge.source_version_id},
        )
        return CorrectionImpact(root_id, version_id, edge.source_version_id, action_kind, expected_basis_hash)

    def _direct_dependency_edges(self, target_ref: VersionRef) -> tuple[DependencyEdge, ...]:
        edges: list[DependencyEdge] = []
        rows = self.conn.execute(
            """
            SELECT r.source_version_id,
                   tr.object_kind AS source_object_kind,
                   r.relation_role,
                   r.target_object_kind,
                   r.target_stable_id,
                   r.target_version_id,
                   r.target_content_hash,
                   r.locator_json
              FROM object_reference r
              JOIN trace_version tv ON tv.version_id=r.source_version_id
              JOIN trace_root tr ON tr.root_id=tv.root_id
             WHERE (
                    r.target_version_id=?
                    OR (
                    r.target_version_id IS NULL
                    AND r.target_stable_id=?
                    AND r.target_content_hash=?
                    )
                )
               AND tr.object_kind NOT LIKE 'goal10_%'
             ORDER BY r.source_version_id, r.relation_role
            """,
            (
                target_ref.target_version_id,
                target_ref.target_stable_id,
                target_ref.target_content_hash,
            ),
        ).fetchall()
        for row in rows:
            edges.append(
                DependencyEdge(
                    source_version_id=row["source_version_id"],
                    source_object_kind=row["source_object_kind"],
                    relation_role=row["relation_role"],
                    target_ref=VersionRef(
                        relation_role=row["relation_role"],
                        target_object_kind=row["target_object_kind"],
                        target_stable_id=row["target_stable_id"],
                        target_version_id=row["target_version_id"],
                        target_content_hash=row["target_content_hash"],
                        locator=json.loads(row["locator_json"]),
                    ),
                    edge_source="object_reference",
                )
            )
        for row in self._binding_manifest_edges(target_ref):
            edges.append(row)
        return tuple(edges)

    def _binding_manifest_edges(self, target_ref: VersionRef) -> tuple[DependencyEdge, ...]:
        rows = self.conn.execute(
            """
            SELECT b.source_version_id,
                   tr.object_kind AS source_object_kind,
                   b.local_ref,
                   b.object_ref_json
              FROM binding_manifest b
              JOIN trace_version tv ON tv.version_id=b.source_version_id
              JOIN trace_root tr ON tr.root_id=tv.root_id
             ORDER BY b.source_version_id, b.local_ref
            """
        ).fetchall()
        edges: list[DependencyEdge] = []
        for row in rows:
            payload = json.loads(row["object_ref_json"])
            ref = VersionRef(
                relation_role=payload.get("relation_role") or "binding_local_ref",
                target_object_kind=payload.get("target_object_kind") or "",
                target_stable_id=payload.get("target_stable_id") or "",
                target_version_id=payload.get("target_version_id"),
                target_content_hash=payload.get("target_content_hash"),
                locator=payload.get("locator") or {"local_ref": row["local_ref"]},
            )
            if _same_target(ref, target_ref):
                edges.append(
                    DependencyEdge(
                        source_version_id=row["source_version_id"],
                        source_object_kind=row["source_object_kind"],
                        relation_role=ref.relation_role,
                        target_ref=ref,
                        edge_source="binding_manifest",
                        local_ref=row["local_ref"],
                    )
                )
        return tuple(edges)

    def _version_content_hash(self, version_id: str | None) -> str:
        if not version_id:
            raise CorrectionError("target_ref must include target_version_id")
        row = self.conn.execute("SELECT content_hash FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise CorrectionError("target version does not exist")
        return str(row["content_hash"])

    def _existing_result(self, command: CorrectionRegistrationCommand) -> CorrectionRegistrationResult | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            ("goal10.correction.register", command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(command.request_payload()):
            raise IdempotencyConflict("idempotency key reused with different request")
        payload = json.loads(row["result_json"])
        impact_rows = self.conn.execute(
            """
            SELECT v.root_id, v.version_id, v.payload_json
              FROM trace_version v
              JOIN trace_root r ON r.root_id=v.root_id
             WHERE r.object_kind='goal10_correction_impact'
               AND v.version_id IN ({})
             ORDER BY v.version_id
            """.format(",".join("?" for _ in payload["impact_version_ids"])),
            tuple(payload["impact_version_ids"]),
        ).fetchall() if payload["impact_version_ids"] else []
        impacts = []
        for impact_row in impact_rows:
            impact_payload = json.loads(impact_row["payload_json"])
            impacts.append(
                CorrectionImpact(
                    root_id=impact_row["root_id"],
                    version_id=impact_row["version_id"],
                    target_version_id=impact_payload["target_version_id"],
                    action_kind=impact_payload["action_kind"],
                    expected_basis_hash=impact_payload["expected_basis_hash"],
                    replayed=True,
                )
            )
        return CorrectionRegistrationResult(
            root_id=str(payload["root_id"]),
            version_id=str(payload["version_id"]),
            receipt_id=str(row["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            dependency_index_version_ids=tuple(payload["dependency_index_version_ids"]),
            impacts=tuple(impacts),
            replayed=True,
        )

    def _record_ref(self, source_version_id: str, ref: VersionRef) -> str:
        return self.store.record_object_reference(
            source_version_id=source_version_id,
            relation_role=ref.relation_role,
            target_object_kind=ref.target_object_kind,
            target_stable_id=ref.target_stable_id,
            target_version_id=ref.target_version_id,
            target_content_hash=ref.target_content_hash,
            locator=ref.locator,
        )

    @staticmethod
    def _validate_command(command: CorrectionRegistrationCommand) -> None:
        if not command.actor:
            raise CorrectionError("actor is required")
        if not command.idempotency_key:
            raise CorrectionError("idempotency_key is required")
        if not command.corrected_payload:
            raise CorrectionError("corrected_payload is required")
        if not command.change_scope:
            raise CorrectionError("change_scope is required")
        if not command.reason:
            raise CorrectionError("reason is required")
        _validate_version_ref(command.target_ref)
        for ref in command.evidence_refs:
            _validate_version_ref(ref)
        if command.safety_limits:
            for key, value in command.safety_limits.items():
                if not isinstance(key, str) or not isinstance(value, int) or value <= 0:
                    raise CorrectionError("safety_limits must map string keys to positive integers")


def _same_target(ref: VersionRef, target_ref: VersionRef) -> bool:
    if ref.target_version_id and target_ref.target_version_id:
        return ref.target_version_id == target_ref.target_version_id
    return (
        ref.target_stable_id == target_ref.target_stable_id
        and ref.target_content_hash == target_ref.target_content_hash
        and ref.target_content_hash is not None
    )


def _action_kind_for(edge: DependencyEdge) -> str:
    if edge.relation_role.startswith("discarded"):
        return "no_action"
    if edge.source_object_kind in RECONFIRMATION_KINDS:
        return "require_user_reconfirmation"
    if edge.source_object_kind in HISTORICAL_ANNOTATION_KINDS:
        return "historical_annotation"
    if edge.relation_role in SEMANTIC_RERUN_ROLES or edge.edge_source == "binding_manifest":
        return "semantic_rerun"
    return "deterministic_recompute"


def _impact_basis_hash(correction_version_id: str, edge: DependencyEdge, action_kind: str) -> str:
    return content_hash(
        {
            "correction_version_id": correction_version_id,
            "target_ref": edge.target_ref.as_payload(),
            "source_version_id": edge.source_version_id,
            "action_kind": action_kind,
        },
        "goal10.impact_basis.v1",
    )


def _validate_version_ref(ref: VersionRef) -> None:
    if not ref.relation_role:
        raise CorrectionError("reference relation_role is required")
    if not ref.target_object_kind:
        raise CorrectionError("reference target_object_kind is required")
    if not ref.target_stable_id:
        raise CorrectionError("reference target_stable_id is required")
    if not ref.target_version_id and not ref.target_content_hash:
        raise CorrectionError("reference must point to a concrete version or content hash")
    if not ref.locator:
        raise CorrectionError("reference locator is required")
