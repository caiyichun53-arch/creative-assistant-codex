from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from scripts.core.persistence.goal01_store import IdempotencyConflict, PersistenceStore, content_hash


class ProductionChainError(RuntimeError):
    pass


ARTIFACT_KINDS = frozenset(
    {
        "research_output",
        "content_plan",
        "script",
        "review",
        "approval",
        "approved_draft",
        "publication_capture",
        "manual_edit",
    }
)

PREFERENCE_ORIGINS = frozenset(
    {
        "user_instruction",
        "manual_edit",
        "approval",
        "rejection",
        "publication_capture",
    }
)


@dataclass(frozen=True)
class VersionRef:
    relation_role: str
    target_object_kind: str
    target_stable_id: str
    target_version_id: str | None
    locator: dict[str, Any]
    target_content_hash: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "relation_role": self.relation_role,
            "target_object_kind": self.target_object_kind,
            "target_stable_id": self.target_stable_id,
            "target_version_id": self.target_version_id,
            "target_content_hash": self.target_content_hash,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class ProductionArtifactCommand:
    artifact_kind: str
    topic_id: str
    actor: str
    idempotency_key: str
    content_payload: dict[str, Any]
    evidence_refs: tuple[VersionRef, ...] = ()
    preference_instruction_refs: tuple[VersionRef, ...] = ()
    model_run_envelope_version_id: str | None = None
    model_run_root_id: str | None = None
    root_id: str | None = None
    based_on_version_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "artifact_kind": self.artifact_kind,
            "topic_id": self.topic_id,
            "actor": self.actor,
            "content_payload": self.content_payload,
            "evidence_refs": [ref.as_payload() for ref in self.evidence_refs],
            "preference_instruction_refs": [ref.as_payload() for ref in self.preference_instruction_refs],
            "model_run_envelope_version_id": self.model_run_envelope_version_id,
            "model_run_root_id": self.model_run_root_id,
            "root_id": self.root_id,
            "based_on_version_id": self.based_on_version_id,
        }


@dataclass(frozen=True)
class ProductionArtifactResult:
    root_id: str
    version_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    replayed: bool = False


@dataclass(frozen=True)
class PreferenceCandidateCommand:
    profile_id: str
    actor: str
    idempotency_key: str
    preference_payload: dict[str, Any]
    evidence_refs: tuple[VersionRef, ...]
    origin: str
    base_revision_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def request_payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "actor": self.actor,
            "preference_payload": self.preference_payload,
            "evidence_refs": [ref.as_payload() for ref in self.evidence_refs],
            "origin": self.origin,
            "base_revision_id": self.base_revision_id,
        }


@dataclass(frozen=True)
class PreferenceCandidateResult:
    revision_id: str
    receipt_id: str
    audit_id: str | None
    outbox_id: str | None
    replayed: bool = False


class ProductionVersionChainMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.conn = store.conn

    def materialize_artifact(self, command: ProductionArtifactCommand) -> ProductionArtifactResult:
        self._validate_artifact_command(command)
        existing = self._existing_artifact_result(command)
        if existing is not None:
            return existing
        with self.conn:
            root_id = command.root_id or self.store.create_root(f"production_{command.artifact_kind}")
            payload = {
                "goal": "GOAL-08",
                "artifact_kind": command.artifact_kind,
                "topic_id": command.topic_id,
                "content_payload": command.content_payload,
                "evidence_refs": [ref.as_payload() for ref in command.evidence_refs],
                "preference_instruction_refs": [
                    ref.as_payload() for ref in command.preference_instruction_refs
                ],
                "model_run_envelope_version_id": command.model_run_envelope_version_id,
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version=f"goal08.production_{command.artifact_kind}.v1",
                based_on_version_id=command.based_on_version_id,
                business_payload={
                    "artifact_kind": command.artifact_kind,
                    "topic_id": command.topic_id,
                    "content_payload": command.content_payload,
                },
            )
            self.store.set_current_version(root_id, version_id)
            for ref in command.evidence_refs:
                self._record_ref(version_id, ref)
            for ref in command.preference_instruction_refs:
                self._record_ref(version_id, ref)
            if command.model_run_envelope_version_id:
                self.store.record_object_reference(
                    source_version_id=version_id,
                    relation_role="generated_by_model_run",
                    target_object_kind="model_run_envelope",
                    target_stable_id=command.model_run_root_id or command.model_run_envelope_version_id,
                    target_version_id=command.model_run_envelope_version_id,
                    target_content_hash=None,
                    locator={"version_id": command.model_run_envelope_version_id},
                )
            result_payload = {"root_id": root_id, "version_id": version_id}
            receipt_id = self.store.record_command(
                command_scope="goal08.production_chain",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal08.production_artifact.materialized",
                actor=command.actor,
                object_kind=f"production_{command.artifact_kind}",
                object_id=root_id,
                version_id=version_id,
                payload={
                    "receipt_id": receipt_id,
                    "artifact_kind": command.artifact_kind,
                    "topic_id": command.topic_id,
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=command.causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal08.production_artifact.materialized",
                payload={
                    "artifact_kind": command.artifact_kind,
                    "topic_id": command.topic_id,
                    "root_id": root_id,
                    "version_id": version_id,
                },
                correlation_id=command.correlation_id or root_id,
                causation_id=audit_id,
            )
            return ProductionArtifactResult(root_id, version_id, receipt_id, audit_id, outbox_id)

    def record_preference_candidate(self, command: PreferenceCandidateCommand) -> PreferenceCandidateResult:
        self._validate_preference_command(command)
        existing = self._existing_preference_result(command)
        if existing is not None:
            return existing
        with self.conn:
            revision_id = self.store.append_preference_revision(
                command.profile_id,
                status="candidate",
                preference_payload=command.preference_payload,
                evidence_refs=[ref.as_payload() for ref in command.evidence_refs],
                origin=command.origin,
                base_revision_id=command.base_revision_id,
            )
            result_payload = {"revision_id": revision_id}
            receipt_id = self.store.record_command(
                command_scope="goal08.preference_candidate",
                idempotency_key=command.idempotency_key,
                request_payload=command.request_payload(),
                result_payload=result_payload,
                correlation_id=command.correlation_id or revision_id,
                causation_id=command.causation_id,
                status="succeeded",
            )
            audit_id = self.store.record_audit(
                event_type="goal08.preference_candidate.recorded",
                actor=command.actor,
                object_kind="content_preference_revision",
                object_id=revision_id,
                payload={"receipt_id": receipt_id, "origin": command.origin},
                correlation_id=command.correlation_id or revision_id,
                causation_id=command.causation_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="goal08.preference_candidate.recorded",
                payload={"profile_id": command.profile_id, "revision_id": revision_id, "origin": command.origin},
                correlation_id=command.correlation_id or revision_id,
                causation_id=audit_id,
            )
            return PreferenceCandidateResult(revision_id, receipt_id, audit_id, outbox_id)

    def _existing_artifact_result(
        self,
        command: ProductionArtifactCommand,
    ) -> ProductionArtifactResult | None:
        payload = self._existing_result(
            "goal08.production_chain",
            command.idempotency_key,
            command.request_payload(),
        )
        if payload is None:
            return None
        return ProductionArtifactResult(
            root_id=str(payload["root_id"]),
            version_id=str(payload["version_id"]),
            receipt_id=str(payload["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            replayed=True,
        )

    def _existing_preference_result(
        self,
        command: PreferenceCandidateCommand,
    ) -> PreferenceCandidateResult | None:
        payload = self._existing_result(
            "goal08.preference_candidate",
            command.idempotency_key,
            command.request_payload(),
        )
        if payload is None:
            return None
        return PreferenceCandidateResult(
            revision_id=str(payload["revision_id"]),
            receipt_id=str(payload["receipt_id"]),
            audit_id=None,
            outbox_id=None,
            replayed=True,
        )

    def _existing_result(
        self,
        command_scope: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT receipt_id, request_hash, result_json
              FROM command_receipt
             WHERE command_scope=? AND idempotency_key=?
            """,
            (command_scope, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != content_hash(request_payload):
            raise IdempotencyConflict("idempotency key reused with different request")
        result = json.loads(row["result_json"])
        result["receipt_id"] = row["receipt_id"]
        return result

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
    def _validate_artifact_command(command: ProductionArtifactCommand) -> None:
        if command.artifact_kind not in ARTIFACT_KINDS:
            raise ProductionChainError(f"unsupported artifact kind: {command.artifact_kind}")
        if not command.topic_id:
            raise ProductionChainError("topic_id is required")
        if not command.actor:
            raise ProductionChainError("actor is required")
        if not command.idempotency_key:
            raise ProductionChainError("idempotency_key is required")
        if not command.content_payload:
            raise ProductionChainError("content_payload is required")
        for ref in (*command.evidence_refs, *command.preference_instruction_refs):
            _validate_version_ref(ref)
        if command.model_run_envelope_version_id and not command.model_run_root_id:
            raise ProductionChainError("model_run_root_id is required with model_run_envelope_version_id")

    @staticmethod
    def _validate_preference_command(command: PreferenceCandidateCommand) -> None:
        if not command.profile_id:
            raise ProductionChainError("profile_id is required")
        if not command.actor:
            raise ProductionChainError("actor is required")
        if not command.idempotency_key:
            raise ProductionChainError("idempotency_key is required")
        if command.origin not in PREFERENCE_ORIGINS:
            raise ProductionChainError(f"unsupported preference origin: {command.origin}")
        if not command.preference_payload:
            raise ProductionChainError("preference_payload is required")
        if not command.evidence_refs:
            raise ProductionChainError("preference evidence_refs are required")
        for ref in command.evidence_refs:
            _validate_version_ref(ref)


def _validate_version_ref(ref: VersionRef) -> None:
    if not ref.relation_role:
        raise ProductionChainError("reference relation_role is required")
    if not ref.target_object_kind:
        raise ProductionChainError("reference target_object_kind is required")
    if not ref.target_stable_id:
        raise ProductionChainError("reference target_stable_id is required")
    if not ref.target_version_id and not ref.target_content_hash:
        raise ProductionChainError("reference must point to a concrete version or content hash")
    if not ref.locator:
        raise ProductionChainError("reference locator is required")
