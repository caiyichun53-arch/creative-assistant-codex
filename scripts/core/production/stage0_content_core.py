"""Stage 0's only formal write boundary for the first content-production chain.

This module deliberately implements state, version, audit, input-assembly, and
data-identity controls only.  It does not generate research or content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from scripts.core.execution_contract import require_baseline_citations
from scripts.core.business_data.domain_labels import FORMAL_DOMAIN_LABELS, get_discovery_policy
from scripts.core.model_gateway.goal07_model_gateway import ModelRequest, ModelRunEnvelope
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError


ROOT = Path(__file__).resolve().parents[3]
FORMAL_DB_PATH = ROOT / "data" / "formal" / "production_activation.sqlite3"
DataIdentity = Literal["production", "test", "fixture", "synthetic", "replay", "mock"]
NON_PRODUCTION_IDENTITIES = frozenset({"test", "fixture", "synthetic", "replay", "mock"})
DISCOVERY_EXECUTION_MODES = frozenset({"test_isolated", "validation_live", "production_daily"})
DISCOVERY_RUN_OUTCOMES = frozenset(
    {"processing", "completed", "completed_with_failures", "timed_out", "interrupted", "failed", "cancelled"}
)

CHAIN_NODES = (
    "formal_topic",
    "research_plan",
    "research_plan_confirmation",
    "deep_research",
    "research_result_confirmation",
    "content_plan",
    "content_plan_confirmation",
    "formal_draft",
    "initial_draft_confirmation",
    "copy_optimization",
    "de_ai_revision",
    "review",
    "user_final_confirmation",
)
ARTIFACT_NODES = frozenset(
    {
        "formal_topic",
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
    }
)
UPSTREAM_NODE = {
    "research_plan": "formal_topic",
    "deep_research": "research_plan",
    "content_plan": "deep_research",
    "formal_draft": "content_plan",
    "copy_optimization": "formal_draft",
    "de_ai_revision": "copy_optimization",
    "review": "de_ai_revision",
}
CONFIRMATION_NODE = {
    "research_plan": "research_plan_confirmation",
    "deep_research": "research_result_confirmation",
    "content_plan": "content_plan_confirmation",
    "formal_draft": "initial_draft_confirmation",
    "review": "user_final_confirmation",
}
NEXT_ARTIFACT_NODE = {
    "formal_topic": "research_plan",
    "research_plan": "deep_research",
    "deep_research": "content_plan",
    "content_plan": "formal_draft",
    "formal_draft": "copy_optimization",
    "copy_optimization": "de_ai_revision",
    "de_ai_revision": "review",
}
MODEL_BINDINGS = {
    "research_plan": ("business_analysis", "business_planning"),
    "deep_research": ("business_analysis", "material_summary"),
    "content_plan": ("business_analysis", "business_planning"),
    "formal_draft": ("writing_generation", "rough_draft"),
    "copy_optimization": ("writing_generation", "polishing"),
    "de_ai_revision": ("writing_generation", "de_ai_style"),
    "review": ("writing_generation", "final_copy_review"),
}


class Stage0CoreError(RuntimeError):
    pass


class DataIdentityError(Stage0CoreError):
    pass


class StateTransitionError(Stage0CoreError):
    pass


class StaleResultError(Stage0CoreError):
    pass


class ModelGatewayRequiredError(Stage0CoreError):
    pass


class ModelBindingUnavailableError(Stage0CoreError):
    pass


class LegacyProductionEntryDisabledError(Stage0CoreError):
    pass


@dataclass(frozen=True)
class InputAssembly:
    task_id: str
    node: str
    upstream_version_id: str | None
    user_requirements: str
    material_refs: tuple[dict[str, Any], ...]
    research_refs: tuple[dict[str, Any], ...]
    content_plan_ref: dict[str, Any] | None
    considered_experience: tuple[dict[str, Any], ...]
    adopted_experience: tuple[dict[str, Any], ...]
    rejected_experience: tuple[dict[str, Any], ...]
    omitted_materials: tuple[dict[str, Any], ...]
    prompt_version: str
    skill_version: str
    model_config_version: str

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "node": self.node,
            "upstream_version_id": self.upstream_version_id,
            "user_requirements": self.user_requirements,
            "material_refs": list(self.material_refs),
            "research_refs": list(self.research_refs),
            "content_plan_ref": self.content_plan_ref,
            "considered_experience": list(self.considered_experience),
            "adopted_experience": list(self.adopted_experience),
            "rejected_experience": list(self.rejected_experience),
            "omitted_materials": list(self.omitted_materials),
            "prompt_version": self.prompt_version,
            "skill_version": self.skill_version,
            "model_config_version": self.model_config_version,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def is_formal_database(path: Path | str) -> bool:
    return Path(path).resolve() == FORMAL_DB_PATH.resolve()


def reject_legacy_cli_production_write(path: Path | str, entrypoint: str) -> None:
    """Fail closed before a legacy CLI can open the formal Stage 0 database."""
    if is_formal_database(path):
        raise LegacyProductionEntryDisabledError(
            f"{entrypoint} is not a formal production entrypoint; use Stage0ContentProductionCore"
        )


def require_legacy_test_identity(data_identity: str) -> str:
    if data_identity not in NON_PRODUCTION_IDENTITIES:
        raise DataIdentityError("legacy CLI may only run with an explicit non-production data identity")
    return data_identity


class CoreModelRunMaterializer:
    """ModelGateway materializer that persists envelopes through Stage 0 Core."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        metadata = dict(envelope.metadata or {})
        binding = metadata.get("stage0_core")
        if not isinstance(binding, dict):
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 0 Core binding")
        return self._core._persist_gateway_envelope(envelope, binding)


class CoreDiscoveryModelRunMaterializer:
    """ModelGateway materializer for Stage 1B candidates; never creates a production task."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("stage1b_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 1B Core binding")
        return self._core.persist_discovery_model_envelope(envelope, binding)


class Stage0ContentProductionCore:
    """The sole formal Core API for Stage 0's first vertical production chain."""

    def __init__(self, connection: sqlite3.Connection, *, db_path: Path, data_identity: DataIdentity) -> None:
        self.conn = connection
        self.db_path = db_path.resolve()
        self.data_identity = data_identity
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, db_path: Path | str, *, data_identity: DataIdentity) -> "Stage0ContentProductionCore":
        resolved = Path(db_path).resolve()
        if data_identity == "production":
            require_baseline_citations(["5", "7", "10", "12", "18", "19", "22"])
            if resolved != FORMAL_DB_PATH.resolve():
                raise DataIdentityError("production identity may only use data/formal/production_activation.sqlite3")
        elif data_identity in NON_PRODUCTION_IDENTITIES:
            if resolved == FORMAL_DB_PATH.resolve():
                raise DataIdentityError("non-production identity must never open the formal production database")
        else:
            raise DataIdentityError(f"unsupported data identity: {data_identity}")
        connection = sqlite3.connect(resolved)
        core = cls(connection, db_path=resolved, data_identity=data_identity)
        core.install_schema()
        return core

    def close(self) -> None:
        self.conn.close()

    def install_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stage0_content_task (
                task_id TEXT PRIMARY KEY,
                topic_version_id TEXT NOT NULL,
                current_node TEXT NOT NULL,
                current_version_id TEXT,
                current_status TEXT NOT NULL,
                task_revision INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cancelled_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_content_node_version (
                version_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                parent_version_id TEXT,
                upstream_version_id TEXT,
                input_assembly_id TEXT,
                status TEXT NOT NULL,
                output_ref TEXT,
                validation_status TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                task_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_decision (
                decision_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                decision TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                upstream_version_id TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_model_run (
                model_run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                node_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage0_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT NOT NULL,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                output_version_id TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway = 1)
            );
            CREATE TABLE IF NOT EXISTS stage0_command_receipt (
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(command_scope, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS stage0_audit_event (
                audit_id TEXT PRIMARY KEY,
                task_id TEXT,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1a_artifact_payload (
                version_id TEXT PRIMARY KEY REFERENCES stage0_content_node_version(version_id),
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('formal_topic', 'research_plan')),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_update
            BEFORE UPDATE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_delete
            BEFORE DELETE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_update
            BEFORE UPDATE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_delete
            BEFORE DELETE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TABLE IF NOT EXISTS stage1b_discovery_run (
                run_id TEXT PRIMARY KEY,
                discovery_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                failure_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage1b_run_execution_context (
                run_id TEXT PRIMARY KEY REFERENCES stage1b_discovery_run(run_id),
                execution_mode TEXT NOT NULL CHECK(execution_mode IN ('test_isolated', 'validation_live', 'production_daily')),
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('processing', 'completed', 'completed_with_failures', 'timed_out', 'interrupted', 'failed', 'cancelled')),
                classification_reason TEXT NOT NULL,
                classified_by TEXT NOT NULL,
                classified_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1_question_expansion_source (
                expansion_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                parent_source_ref_json TEXT NOT NULL,
                validation_outcome TEXT NOT NULL CHECK(validation_outcome = 'supported'),
                validated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1_saved_user_direction_source (
                direction_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_source_version (
                source_version_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('daily_competitor_content', 'historical_high_signal', 'hotspot', 'tag_discovery', 'question_expansion', 'saved_user_direction')),
                source_object_id TEXT NOT NULL,
                source_object_version TEXT NOT NULL,
                source_time TEXT NOT NULL,
                expires_at TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_type, source_object_id, source_object_version)
            );
            CREATE TABLE IF NOT EXISTS stage1b_filter_result (
                filter_result_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                outcome TEXT NOT NULL CHECK(outcome IN ('eligible', 'excluded')),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                model_config_version TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_model_run (
                model_run_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage1b_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT NOT NULL,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway IN (0, 1)),
                UNIQUE(run_id, source_version_id, input_assembly_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_version (
                candidate_version_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                parent_candidate_version_id TEXT,
                model_run_id TEXT NOT NULL REFERENCES stage1b_model_run(model_run_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_user_decision', 'selected', 'deferred', 'rejected', 'evergreen', 'angle_change_requested')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_daily_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                candidate_version_id TEXT REFERENCES stage1b_candidate_version(candidate_version_id),
                display_position INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, domain_label, display_position)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_decision (
                decision_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                decision TEXT NOT NULL CHECK(decision IN ('selected', 'deferred', 'rejected', 'angle_change_requested', 'evergreen')),
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                formal_topic_task_id TEXT,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_absence (
                absence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                model_run_id TEXT REFERENCES stage1b_model_run(model_run_id),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_cooldown (
                cooldown_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL UNIQUE REFERENCES stage1b_candidate_version(candidate_version_id),
                snapshot_id TEXT NOT NULL REFERENCES stage1b_daily_snapshot(snapshot_id),
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                started_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_update
            BEFORE UPDATE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_delete
            BEFORE DELETE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_update
            BEFORE UPDATE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_delete
            BEFORE DELETE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_execution_context_mode_immutable
            BEFORE UPDATE OF execution_mode ON stage1b_run_execution_context
            BEGIN SELECT RAISE(ABORT, 'stage1b execution mode is immutable; create a new run instead'); END;
            """
        )
        discovery_schema = Path(__file__).resolve().parents[1] / "business_data" / "domain_search_schema.sqlite.sql"
        self.conn.executescript(discovery_schema.read_text(encoding="utf-8"))
        self.conn.commit()

    def create_task(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if self.data_identity == "production":
            raise StateTransitionError("production formal topics must be submitted and explicitly confirmed separately")
        if actor_kind != "user":
            raise StateTransitionError("formal topic creation requires a user confirmation")
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("create_task", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, NULL, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "research_plan", "not_started", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)",
                (topic_version_id, task_id, "formal_topic", "approved", "topic_confirmed", self.data_identity, actor, now, 0),
            )
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            result = {"task_id": task_id, "topic_version_id": topic_version_id}
            self._receipt("create_task", idempotency_key, request, result)
            self._audit(task_id, "task_created", result)
        return result

    def submit_formal_topic(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Create a user-supplied formal-topic version, awaiting confirmation."""
        if actor_kind != "user":
            raise StateTransitionError("formal topic submission requires a user actor")
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("submit_formal_topic", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "formal_topic", topic_version_id, "awaiting_human_review", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    topic_version_id,
                    task_id,
                    "formal_topic",
                    "awaiting_human_review",
                    "formal_topic_payload",
                    "topic_submitted",
                    self.data_identity,
                    actor,
                    now,
                    0,
                ),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "task_revision": "0"}
            self._receipt("submit_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_submitted", result)
        return result

    def confirm_formal_topic(
        self,
        *,
        task_id: str,
        topic_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if actor_kind != "user":
            raise StateTransitionError("only a user may confirm a formal topic")
        task, version = self._task(task_id), self._version(topic_version_id)
        self._assert_current_node(task, "formal_topic", "awaiting_human_review", topic_version_id)
        if version["node"] != "formal_topic":
            raise StateTransitionError("version is not a formal topic")
        request = {"task_id": task_id, "topic_version_id": topic_version_id, "actor": actor, "reason": reason}
        replay = self._replay("confirm_formal_topic", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            self._set_task(task_id, node="research_plan", version_id=topic_version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": "research_plan", "task_revision": str(revision)}
            self._receipt("confirm_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_confirmed", result)
        return result

    def create_input_assembly(self, assembly: InputAssembly, *, idempotency_key: str) -> dict[str, str]:
        if assembly.node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{assembly.node} has no model-input assembly in Stage 0")
        task = self._task(assembly.task_id)
        if task["current_node"] != assembly.node or task["current_status"] not in {"not_started", "awaiting_human_review"}:
            raise StateTransitionError("input assembly may only be prepared for the current new or returned node")
        upstream = self._approved_upstream(task, assembly.node, assembly.upstream_version_id)
        request = assembly.payload()
        replay = self._replay("create_input_assembly", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("assembly"), _hash(request)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, assembly.task_id, assembly.node, upstream, _canonical(request), integrity_hash, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash}
            self._receipt("create_input_assembly", idempotency_key, request, result)
            self._audit(assembly.task_id, "input_assembly_created", result)
        return result

    def create_node_request(
        self,
        *,
        task_id: str,
        node: str,
        input_assembly_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{node} is not a model-backed Stage 0 node")
        task = self._task(task_id)
        self._assert_current_node(task, node, "not_started")
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != node or assembly["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not belong to this task/node/identity")
        upstream = self._approved_upstream(task, node, assembly["upstream_version_id"])
        request = {"task_id": task_id, "node": node, "input_assembly_id": input_assembly_id, "actor": actor}
        replay = self._replay("create_node_request", idempotency_key, request)
        if replay:
            return replay
        version_id = _id("version")
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (version_id, task_id, node, upstream, input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=node, version_id=version_id, status="processing", revision=revision)
            result = {"node_version_id": version_id, "task_revision": str(revision)}
            self._receipt("create_node_request", idempotency_key, request, result)
            self._audit(task_id, "node_request_created", {**result, "node": node})
        return result

    def prepare_model_request(self, *, task_id: str, node_version_id: str, prompt: str) -> ModelRequest:
        """Build a request from the only routing configuration; no provider is chosen here."""
        version = self._version(node_version_id)
        task = self._task(task_id)
        if version["task_id"] != task_id or version["node"] not in MODEL_BINDINGS:
            raise StateTransitionError("node version is not a model-backed version of this task")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        route_id, task_type = MODEL_BINDINGS[version["node"]]
        router = ModelRouter.from_file()
        definition = router.routes.get(route_id)
        if definition is None or task_type not in definition.allowed_task_types:
            raise ModelBindingUnavailableError(f"{node_version_id} lacks an explicit route binding")
        try:
            route = router.resolve_bound_route(route_id, route_name=f"stage0.{version['node']}")
        except Exception as exc:
            raise ModelBindingUnavailableError(
                "config/model_routes.yaml has no explicit resolved model binding; formal model execution remains disabled"
            ) from exc
        assembly = self._assembly(version["input_assembly_id"])
        payload = json.loads(assembly["payload_json"])
        return ModelRequest(
            route_name=route.route_name,
            prompt=prompt,
            input_payload=payload,
            correlation_id=task_id,
            skill_name=f"stage0.{version['node']}",
            skill_version=payload["skill_version"],
            binding_name=route_id,
            binding_version=payload["model_config_version"],
            metadata={
                "stage0_core": {
                    "task_id": task_id,
                    "node": version["node"],
                    "node_version_id": node_version_id,
                    "input_assembly_id": version["input_assembly_id"],
                    "task_revision": int(task["task_revision"]),
                    "prompt_version": payload["prompt_version"],
                    "skill_version": payload["skill_version"],
                    "data_identity": self.data_identity,
                }
            },
        )

    def complete_node_from_model(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        output_ref: str,
        validation_status: str,
        actor: str,
        expected_task_revision: int,
        idempotency_key: str,
        artifact_payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if validation_status != "passed":
            raise StateTransitionError("only schema-validated model output may await human review")
        task, request_version = self._task(task_id), self._version(node_version_id)
        if int(task["task_revision"]) != expected_task_revision:
            raise StaleResultError("model result is stale because the task revision changed")
        run = self._model_run(model_run_id)
        self._assert_current_node(task, request_version["node"], "processing", node_version_id)
        if run["task_id"] != task_id or run["node_version_id"] != node_version_id or run["status"] != "succeeded":
            raise ModelGatewayRequiredError("model result is not the successful current Gateway run")
        if run["via_model_gateway"] != 1 or run["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("formal output requires a matching ModelGateway record")
        request = {"task_id": task_id, "node_version_id": node_version_id, "model_run_id": model_run_id, "output_ref": output_ref}
        replay = self._replay("complete_node_from_model", idempotency_key, request)
        if replay:
            return replay
        output_version_id, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (output_version_id, task_id, request_version["node"], node_version_id, request_version["upstream_version_id"], request_version["input_assembly_id"], "awaiting_human_review", output_ref, "passed", self.data_identity, actor, _now(), revision),
            )
            if artifact_payload is not None:
                if request_version["node"] not in {"research_plan"}:
                    raise StateTransitionError("artifact payload persistence is only enabled for Stage 1A research plans")
                self._insert_artifact_payload(output_version_id, request_version["node"], artifact_payload)
            self.conn.execute("UPDATE stage0_model_run SET output_version_id=? WHERE model_run_id=?", (output_version_id, model_run_id))
            self._set_task(task_id, node=request_version["node"], version_id=output_version_id, status="awaiting_human_review", revision=revision)
            result = {"node_version_id": output_version_id, "task_revision": str(revision)}
            self._receipt("complete_node_from_model", idempotency_key, request, result)
            self._audit(task_id, "model_output_awaiting_human_review", {**result, "model_run_id": model_run_id})
        return result

    def approve_current_node(self, *, task_id: str, version_id: str, actor: str, actor_kind: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, version = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, version["node"], "awaiting_human_review", version_id)
        if version["node"] == "review" and actor_kind != "user":
            raise StateTransitionError("only a user may perform final confirmation")
        request = {"task_id": task_id, "version_id": version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("approve_current_node", idempotency_key, request)
        if replay:
            return replay
        decision_node = CONFIRMATION_NODE.get(version["node"], version["node"])
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, decision_node, version_id, "approved", actor, actor_kind, reason)
            next_node = NEXT_ARTIFACT_NODE.get(version["node"])
            if next_node is None:
                self._set_task(task_id, node="user_final_confirmation", version_id=version_id, status="approved", revision=revision)
            else:
                self._set_task(task_id, node=next_node, version_id=version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": next_node or "user_final_confirmation", "task_revision": str(revision)}
            self._receipt("approve_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_approved", {**result, "decision_node": decision_node})
        return result

    def return_current_node(self, *, task_id: str, version_id: str, input_assembly_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, previous = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, previous["node"], "awaiting_human_review", version_id)
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != previous["node"] or assembly["upstream_version_id"] != previous["upstream_version_id"]:
            raise StateTransitionError("returned node needs a new matching input assembly")
        request = {"task_id": task_id, "version_id": version_id, "input_assembly_id": input_assembly_id, "actor": actor, "reason": reason}
        replay = self._replay("return_current_node", idempotency_key, request)
        if replay:
            return replay
        replacement, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, previous["node"], version_id, "returned", actor, "user", reason)
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (replacement, task_id, previous["node"], version_id, previous["upstream_version_id"], input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=previous["node"], version_id=replacement, status="processing", revision=revision)
            result = {"node_version_id": replacement, "task_revision": str(revision)}
            self._receipt("return_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_returned_new_version", {**result, "returned_version_id": version_id})
        return result

    def cancel_current_task(self, *, task_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task = self._task(task_id)
        if task["current_status"] in {"approved", "cancelled"}:
            raise StateTransitionError("task is already terminal")
        request = {"task_id": task_id, "actor": actor, "reason": reason}
        replay = self._replay("cancel_current_task", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute("UPDATE stage0_content_task SET current_status='cancelled', task_revision=?, cancelled_reason=? WHERE task_id=?", (revision, reason, task_id))
            result = {"task_id": task_id, "task_revision": str(revision)}
            self._receipt("cancel_current_task", idempotency_key, request, result)
            self._audit(task_id, "task_cancelled", result)
        return result

    def _persist_gateway_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway data identity does not match Core identity")
        task_id, node_version_id = str(binding.get("task_id") or ""), str(binding.get("node_version_id") or "")
        task, version = self._task(task_id), self._version(node_version_id)
        if int(binding.get("task_revision", -1)) != int(task["task_revision"]):
            raise StaleResultError("ModelGateway envelope belongs to a stale task revision")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if version["node"] != binding.get("node") or version["input_assembly_id"] != binding.get("input_assembly_id"):
            raise ModelGatewayRequiredError("ModelGateway envelope does not match the active node input")
        assembly = self._assembly(version["input_assembly_id"])
        model_run_id = _id("model_run")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_model_run(
                    model_run_id, task_id, node, node_version_id, input_assembly_id, status, request_id,
                    prompt_version, skill_version, route_id, route_version, provider_ref, provider_name,
                    model_name, input_integrity_hash, output_hash, output_version_id, validation_status,
                    error_json, retry_status, usage_json, cost_json, duration_ms, data_identity, created_at,
                    via_model_gateway
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_run_id, task_id, version["node"], node_version_id, version["input_assembly_id"],
                    envelope.status, envelope.correlation_id, binding["prompt_version"], binding["skill_version"],
                    envelope.route_id or envelope.route_name, envelope.config_version, envelope.provider_ref,
                    envelope.provider_name, envelope.model_name, assembly["integrity_hash"], envelope.output_hash,
                    None, "not_validated", _canonical(envelope.error or {}), "not_retried",
                    _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                    _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1,
                ),
            )
            self._audit(task_id, "model_gateway_envelope_recorded", {"model_run_id": model_run_id, "node": version["node"], "status": envelope.status})
        return model_run_id

    def _task(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_task WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("task does not exist in this data identity")
        return row

    def _version(self, version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_node_version WHERE version_id=?", (version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("node version does not exist in this data identity")
        return row

    def _assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not exist in this data identity")
        return row

    def _model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None:
            raise ModelGatewayRequiredError("model run does not exist")
        return row

    def _discovery_run(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_discovery_run WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery run does not exist in this data identity")
        return row

    def _validate_discovery_execution_mode(self, execution_mode: str) -> None:
        if execution_mode not in DISCOVERY_EXECUTION_MODES:
            raise StateTransitionError("discovery execution mode is invalid")
        if self.data_identity in NON_PRODUCTION_IDENTITIES and execution_mode != "test_isolated":
            raise DataIdentityError("non-production discovery data must use test_isolated mode")
        if self.data_identity == "production" and execution_mode == "test_isolated":
            raise DataIdentityError("production discovery data cannot use test_isolated mode")

    def _discovery_context_optional(self, run_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM stage1b_run_execution_context WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery execution context does not exist in this data identity")
        return row

    def _discovery_context(self, run_id: str) -> sqlite3.Row:
        row = self._discovery_context_optional(run_id)
        if row is None:
            raise StateTransitionError(
                "discovery run has no execution classification; classify it as validation_live before it can be viewed or used"
            )
        return row

    def _discovery_source(self, source_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_source_version WHERE source_version_id=?", (source_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery source does not exist in this data identity")
        return row

    def _discovery_assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery input assembly does not exist in this data identity")
        return row

    def _discovery_model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("discovery model run does not exist in this data identity")
        return row

    def _discovery_candidate(self, candidate_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_candidate_version WHERE candidate_version_id=?", (candidate_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery candidate does not exist in this data identity")
        return row

    def _resolve_discovery_model_route(self):
        router = ModelRouter.from_file()
        definition = router.routes.get("business_analysis")
        if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
            raise ModelBindingUnavailableError("daily discovery requires the explicit business topic_screening route")
        try:
            return router.resolve_bound_route("business_analysis", route_name="stage1b.daily_discovery")
        except Exception as exc:
            raise ModelBindingUnavailableError("daily discovery has no explicit resolved model binding") from exc

    def _assert_discovery_source_provenance(
        self,
        *,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        payload: dict[str, Any],
    ) -> None:
        """Accept only a source whose precise formal origin still matches the Core facts."""
        origin = payload.get("formal_source")
        if not isinstance(origin, dict):
            raise StateTransitionError("daily discovery source requires a formal_source mapping")
        if source_type == "daily_competitor_content":
            row = self.conn.execute(
                "SELECT video.video_id, video.last_checked_at, video.publish_time, video.title, video.url, video.raw_json, "
                "video.raw_archive_ref, video.excluded_reason, account.domain_label, account.registration_status, "
                "account.source_config_ref FROM competitor_videos video JOIN competitor_accounts account "
                "ON account.account_id=video.account_id WHERE video.video_id=?",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "competitor_videos", "last_checked_at"
        elif source_type == "historical_high_signal":
            row = self.conn.execute(
                "SELECT hit.hit_id, hit.promoted_at, hit.publish_time, hit.title, hit.url, hit.judgment_confidence, "
                "video.raw_json, video.raw_archive_ref, video.excluded_reason, account.domain_label, "
                "account.registration_status, account.source_config_ref FROM hits hit "
                "JOIN competitor_videos video ON video.video_id=hit.video_id "
                "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE hit.hit_id=?",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "hits", "promoted_at"
        elif source_type == "hotspot":
            row = self.conn.execute(
                "SELECT observation.*, run.status FROM trendradar_hotspot_observation observation "
                "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
                "WHERE observation.observation_id=? AND run.status IN ('completed', 'completed_with_failures')",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "trendradar_hotspot_observation", "observed_at"
        elif source_type == "tag_discovery":
            row = self.conn.execute(
                "SELECT video.*, tag.tag FROM discovered_external_videos video "
                "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
                "WHERE video.discovered_video_id=? AND video.domain_label=? AND tag.status='active'",
                (source_object_id, domain_label),
            ).fetchone()
            expected_table, expected_version = "discovered_external_videos", "discovered_at"
        elif source_type == "question_expansion":
            row = self.conn.execute(
                "SELECT * FROM stage1_question_expansion_source WHERE expansion_id=? "
                "AND domain_label=? AND validation_outcome='supported' AND data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_question_expansion_source", "integrity_hash"
        elif source_type == "saved_user_direction":
            row = self.conn.execute(
                "SELECT * FROM stage1_saved_user_direction_source WHERE direction_id=? "
                "AND domain_label=? AND status='active' AND data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_saved_user_direction_source", "integrity_hash"
        else:
            raise StateTransitionError("daily discovery source type is not enabled in this Stage 1B slice")
        if row is None:
            raise StateTransitionError("daily discovery source is not registered in the formal source facts")
        if source_type == "hotspot":
            match_terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
            folded_title = str(row["title"]).casefold()
            if not match_terms or not any(term.casefold() in folded_title for term in match_terms):
                raise StateTransitionError("hotspot does not match the versioned domain policy")
        if origin.get("table") != expected_table or origin.get("object_id") != source_object_id:
            raise StateTransitionError("daily discovery source mapping does not identify the formal origin")
        formal_version = str(row[expected_version])
        if origin.get("object_version") != formal_version or source_object_version != formal_version:
            raise StaleResultError("daily discovery source version does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            expected_time_field = "publish_time"
        elif source_type == "question_expansion":
            expected_time_field = "validated_at"
        elif source_type == "saved_user_direction":
            expected_time_field = "saved_at"
        else:
            expected_time_field = expected_version
        if source_time != str(row[expected_time_field]):
            raise StateTransitionError("daily discovery source time does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            if domain_label != str(row["domain_label"]):
                raise StateTransitionError("daily discovery source domain does not match the formal origin")
            if row["registration_status"] != "active" or not str(row["source_config_ref"] or "").strip():
                raise StateTransitionError("daily discovery source account is not qualified")
            if not str(row["raw_archive_ref"] or "").strip() or row["excluded_reason"] is not None:
                raise StateTransitionError("daily discovery source lacks qualified formal material")
            if source_type == "historical_high_signal" and row["judgment_confidence"] != "formal":
                raise StateTransitionError("historical source lacks formal high-signal qualification")
        if source_type in {"question_expansion", "saved_user_direction"}:
            if not str(row["core_question"] or "").strip():
                raise StateTransitionError("daily discovery source lacks a concrete core question")
            raw_payload = row["payload_json"]
        else:
            if not str(row["title"] or "").strip() or not str(row["url"] or "").strip():
                raise StateTransitionError("daily discovery source lacks required formal material")
            raw_payload = row["raw_json"]
        try:
            raw_hash = _hash(json.loads(str(raw_payload)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("daily discovery source has unreadable formal raw payload") from exc
        if origin.get("raw_metadata_hash") != raw_hash:
            raise StaleResultError("daily discovery source raw payload does not match the formal origin")

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._task(task_id)
        return {key: row[key] for key in row.keys()}

    def get_node_version(self, version_id: str) -> dict[str, Any]:
        row = self._version(version_id)
        return {key: row[key] for key in row.keys()}

    def get_input_assembly_payload(self, assembly_id: str) -> dict[str, Any]:
        row = self._assembly(assembly_id)
        return json.loads(row["payload_json"])

    def find_command_replay(self, command: str, idempotency_key: str, request: dict[str, Any]) -> dict[str, str] | None:
        return self._replay(command, idempotency_key, request)

    def record_completed_command(
        self,
        *,
        command: str,
        idempotency_key: str,
        request: dict[str, Any],
        task_id: str,
        event: str,
        result: dict[str, str],
    ) -> None:
        """Persist an orchestration receipt and audit event through the Core boundary."""
        with self.conn:
            self._receipt(command, idempotency_key, request, result)
            self._audit(task_id, event, result)

    def record_model_validation_failure(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        reason: str,
    ) -> None:
        """Record a rejected structured output without creating a business artifact."""
        task, version, model_run = self._task(task_id), self._version(node_version_id), self._model_run(model_run_id)
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if model_run["data_identity"] != self.data_identity or model_run["task_id"] != task_id or model_run["node_version_id"] != node_version_id:
            raise ModelGatewayRequiredError("model run does not belong to the active node request")
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?",
                (_canonical({"validation_error": reason}), model_run_id),
            )
            self._audit(task_id, "model_output_validation_failed", {"model_run_id": model_run_id, "reason": reason})

    def create_discovery_run(
        self, *, discovery_date: str, actor: str, execution_mode: str, idempotency_key: str
    ) -> dict[str, str]:
        self._validate_discovery_execution_mode(execution_mode)
        request = {
            "discovery_date": discovery_date,
            "actor": actor,
            "execution_mode": execution_mode,
            "data_identity": self.data_identity,
        }
        replay = self._replay("stage1b_create_discovery_run", idempotency_key, request)
        if replay:
            return replay
        run_id = _id("discovery_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_discovery_run VALUES (?, ?, 'processing', ?, ?, ?, NULL, NULL)",
                (run_id, discovery_date, self.data_identity, actor, _now()),
            )
            self.conn.execute(
                "INSERT INTO stage1b_run_execution_context VALUES (?, ?, 'processing', ?, ?, ?, ?)",
                (run_id, execution_mode, "run created with explicit execution mode", actor, _now(), self.data_identity),
            )
            result = {"run_id": run_id, "discovery_date": discovery_date}
            self._receipt("stage1b_create_discovery_run", idempotency_key, request, result)
            self._audit(run_id, "stage1b_discovery_run_started", {**result, "execution_mode": execution_mode})
        return result

    def classify_existing_discovery_run_as_validation_live(
        self, *, run_id: str, actor: str, reason: str, idempotency_key: str
    ) -> dict[str, str]:
        """One-way classification for an already-recorded live validation run.

        This preserves immutable source/model/candidate records while explicitly
        preventing them from entering the formal daily candidate path.
        """
        if self.data_identity != "production":
            raise DataIdentityError("live validation classification requires production data identity")
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("live validation classification requires an audited actor and reason")
        run = self._discovery_run(run_id)
        if self._discovery_context_optional(run_id) is not None:
            raise StateTransitionError("discovery run already has an immutable execution classification")
        decision = self.conn.execute(
            "SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id IN "
            "(SELECT candidate_version_id FROM stage1b_candidate_version WHERE run_id=?) LIMIT 1",
            (run_id,),
        ).fetchone()
        if decision is not None:
            raise StateTransitionError("a run with candidate decisions cannot be retroactively classified")
        request = {"run_id": run_id, "execution_mode": "validation_live", "actor": actor, "reason": reason}
        replay = self._replay("stage1b_classify_existing_validation_live", idempotency_key, request)
        if replay:
            return replay
        lifecycle_status = "completed" if run["status"] == "completed" else "failed"
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_run_execution_context VALUES (?, 'validation_live', ?, ?, ?, ?, ?)",
                (run_id, lifecycle_status, reason, actor, _now(), self.data_identity),
            )
            result = {"run_id": run_id, "execution_mode": "validation_live", "lifecycle_status": lifecycle_status}
            self._receipt("stage1b_classify_existing_validation_live", idempotency_key, request, result)
            self._audit(run_id, "stage1b_run_classified_validation_live", {**result, "reason": reason})
        return result

    def register_question_expansion_source(
        self,
        *,
        expansion_id: str,
        domain_label: str,
        core_question: str,
        parent_source_ref: dict[str, Any],
        actor: str,
    ) -> dict[str, str]:
        """Register only an already-supported bounded expansion as a source."""
        if domain_label not in FORMAL_DOMAIN_LABELS:
            raise StateTransitionError("question expansion requires a configured formal domain")
        if len(core_question.strip()) < 6 or not parent_source_ref:
            raise StateTransitionError("question expansion requires a concrete question and parent source")
        payload = {
            "title": core_question.strip(),
            "core_question": core_question.strip(),
            "parent_source_ref": parent_source_ref,
            "validation_outcome": "supported",
        }
        integrity_hash = _hash(payload)
        validated_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage1_question_expansion_source(
                    expansion_id, domain_label, core_question, parent_source_ref_json,
                    validation_outcome, validated_at, payload_json, integrity_hash,
                    data_identity, created_by
                ) VALUES (?, ?, ?, ?, 'supported', ?, ?, ?, ?, ?)
                """,
                (expansion_id, domain_label, core_question.strip(), _canonical(parent_source_ref),
                 validated_at, _canonical(payload), integrity_hash, self.data_identity, actor),
            )
        return {"expansion_id": expansion_id, "source_object_version": integrity_hash, "validated_at": validated_at}

    def register_saved_user_direction_source(
        self,
        *,
        direction_id: str,
        domain_label: str,
        core_question: str,
        submitted_by: str,
    ) -> dict[str, str]:
        """Register a user-supplied concrete question without inventing an angle."""
        if domain_label not in FORMAL_DOMAIN_LABELS:
            raise StateTransitionError("saved user direction requires a configured formal domain")
        if len(core_question.strip()) < 6 or not submitted_by.strip():
            raise StateTransitionError("saved user direction requires a concrete question and user identity")
        payload = {"title": core_question.strip(), "core_question": core_question.strip(), "submitted_by": submitted_by.strip()}
        integrity_hash = _hash(payload)
        saved_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage1_saved_user_direction_source(
                    direction_id, domain_label, core_question, submitted_by, saved_at,
                    payload_json, integrity_hash, status, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (direction_id, domain_label, core_question.strip(), submitted_by.strip(), saved_at,
                 _canonical(payload), integrity_hash, self.data_identity),
            )
        return {"direction_id": direction_id, "source_object_version": integrity_hash, "saved_at": saved_at}

    def purge_validation_live_run(
        self,
        *,
        run_id: str,
        actor: str,
        reason: str,
        expected_candidate_count: int,
        confirmation: str,
    ) -> dict[str, Any]:
        """Physically remove one explicitly approved erroneous validation run.

        This exceptional lifecycle action intentionally leaves no business-data
        audit copy of the removed run.  It returns exact deletion counts to the
        invoking controlled Runtime process.
        """
        require_baseline_citations(["3", "13", "16", "17"])
        if self.data_identity != "production":
            raise DataIdentityError("validation result purge requires production data identity")
        if confirmation != f"DELETE_VALIDATION_LIVE_RUN:{run_id}":
            raise StateTransitionError("validation result purge requires the exact confirmation token")
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("validation result purge requires actor and reason")
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        if context["execution_mode"] != "validation_live":
            raise StateTransitionError("only validation_live results can be physically purged here")
        candidate_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage1b_candidate_version WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        if candidate_count != int(expected_candidate_count):
            raise StateTransitionError(
                f"candidate count mismatch: expected {expected_candidate_count}, found {candidate_count}"
            )
        decision_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage1b_candidate_decision WHERE candidate_version_id IN "
            "(SELECT candidate_version_id FROM stage1b_candidate_version WHERE run_id=?)",
            (run_id,),
        ).fetchone()[0])
        if decision_count:
            raise StateTransitionError("a validation run with user decisions cannot be purged")

        deleted: dict[str, int] = {}
        derived_tokens = {run_id}
        for table, column in (
            ("stage1b_source_version", "source_version_id"),
            ("stage1b_input_assembly", "assembly_id"),
            ("stage1b_model_run", "model_run_id"),
            ("stage1b_candidate_version", "candidate_version_id"),
            ("stage1b_candidate_absence", "absence_id"),
            ("stage1b_daily_snapshot", "snapshot_id"),
        ):
            derived_tokens.update(
                str(row[0]) for row in self.conn.execute(
                    f"SELECT {column} FROM {table} WHERE run_id=?", (run_id,)
                ).fetchall()
            )
        derived_tokens.update(
            str(row[0]) for row in self.conn.execute(
                "SELECT filter_result_id FROM stage1b_filter_result WHERE source_version_id IN "
                "(SELECT source_version_id FROM stage1b_source_version WHERE run_id=?)",
                (run_id,),
            ).fetchall()
        )

        def delete(table: str, where: str, params: tuple[Any, ...]) -> None:
            cursor = self.conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            deleted[table] = int(cursor.rowcount)

        with self.conn:
            self.conn.execute("DROP TRIGGER IF EXISTS stage1b_candidate_version_immutable_delete")
            self.conn.execute("DROP TRIGGER IF EXISTS stage1b_source_version_immutable_delete")
            delete(
                "stage1b_candidate_decision",
                "candidate_version_id IN (SELECT candidate_version_id FROM stage1b_candidate_version WHERE run_id=?)",
                (run_id,),
            )
            delete(
                "stage1b_candidate_cooldown",
                "candidate_version_id IN (SELECT candidate_version_id FROM stage1b_candidate_version WHERE run_id=?)",
                (run_id,),
            )
            delete("stage1b_daily_snapshot", "run_id=?", (run_id,))
            delete("stage1b_candidate_version", "run_id=?", (run_id,))
            delete("stage1b_candidate_absence", "run_id=?", (run_id,))
            delete("stage1b_model_run", "run_id=?", (run_id,))
            delete("stage1b_input_assembly", "run_id=?", (run_id,))
            delete(
                "stage1b_filter_result",
                "source_version_id IN (SELECT source_version_id FROM stage1b_source_version WHERE run_id=?)",
                (run_id,),
            )
            delete("stage1b_source_version", "run_id=?", (run_id,))
            delete("domain_search_page_observation", "run_id=?", (run_id,))
            delete("discovered_external_videos", "run_id=?", (run_id,))
            self.conn.execute(
                "UPDATE domain_search_cursor SET last_searched_at=NULL, run_id=NULL WHERE run_id=?", (run_id,)
            )
            delete(
                "trendradar_hotspot_observation",
                "collection_run_id IN (SELECT collection_run_id FROM trendradar_collection_run WHERE discovery_run_id=?)",
                (run_id,),
            )
            delete("trendradar_collection_run", "discovery_run_id=?", (run_id,))
            delete("stage1b_run_execution_context", "run_id=?", (run_id,))
            delete("stage1b_discovery_run", "run_id=?", (run_id,))
            token_where = " OR ".join("payload_json LIKE ?" for _ in derived_tokens)
            delete(
                "stage0_audit_event",
                f"task_id=? OR {token_where}",
                (run_id, *(f"%{token}%" for token in sorted(derived_tokens))),
            )
            receipt_where = " OR ".join("result_json LIKE ?" for _ in derived_tokens)
            delete(
                "stage0_command_receipt",
                receipt_where,
                tuple(f"%{token}%" for token in sorted(derived_tokens)),
            )
            self.conn.execute(
                "CREATE TRIGGER stage1b_source_version_immutable_delete "
                "BEFORE DELETE ON stage1b_source_version "
                "BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END"
            )
            self.conn.execute(
                "CREATE TRIGGER stage1b_candidate_version_immutable_delete "
                "BEFORE DELETE ON stage1b_candidate_version "
                "BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END"
            )
        return {
            "run_id": run_id,
            "deleted": deleted,
            "expected_candidate_count": expected_candidate_count,
            "run_status_before_delete": str(run["status"]),
            "result": "physically_deleted",
        }

    def record_discovery_source(
        self,
        *,
        run_id: str,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        expires_at: str | None = None,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        if run["status"] != "processing":
            raise StateTransitionError("discovery sources may only be recorded while a run is processing")
        self._assert_discovery_source_provenance(
            domain_label=domain_label,
            source_type=source_type,
            source_object_id=source_object_id,
            source_object_version=source_object_version,
            source_time=source_time,
            payload=payload,
        )
        request = {
            "run_id": run_id, "domain_label": domain_label, "source_type": source_type,
            "source_object_id": source_object_id, "source_object_version": source_object_version,
            "source_time": source_time, "expires_at": expires_at, "payload": payload,
        }
        replay = self._replay("stage1b_record_discovery_source", idempotency_key, request)
        if replay:
            return replay
        source_version_id = _id("discovery_source")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_source_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_version_id, run_id, domain_label, source_type, source_object_id, source_object_version,
                 source_time, expires_at, _canonical(payload), _hash(payload), self.data_identity, _now()),
            )
            result = {"source_version_id": source_version_id, "input_integrity_hash": _hash(payload)}
            self._receipt("stage1b_record_discovery_source", idempotency_key, request, result)
            self._audit(run_id, "stage1b_source_recorded", {**result, "source_type": source_type})
        return result

    def record_discovery_filter(
        self,
        *,
        source_version_id: str,
        outcome: str,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        source = self._discovery_source(source_version_id)
        if outcome not in {"eligible", "excluded"}:
            raise StateTransitionError("discovery filter outcome is invalid")
        request = {"source_version_id": source_version_id, "outcome": outcome, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_discovery_filter", idempotency_key, request)
        if replay:
            return replay
        result_id = _id("discovery_filter")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_filter_result VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, source_version_id, outcome, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            result = {"filter_result_id": result_id, "source_version_id": source_version_id, "outcome": outcome}
            self._receipt("stage1b_record_discovery_filter", idempotency_key, request, result)
            self._audit(source["run_id"], "stage1b_source_filtered", {**result, "reason_code": reason_code})
        return result

    def create_discovery_input_assembly(
        self,
        *,
        run_id: str,
        source_version_id: str,
        payload: dict[str, Any],
        prompt_version: str,
        skill_version: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("discovery input must belong to an active run source")
        filter_row = self.conn.execute("SELECT outcome FROM stage1b_filter_result WHERE source_version_id=?", (source_version_id,)).fetchone()
        if filter_row is None or filter_row["outcome"] != "eligible":
            raise StateTransitionError("LLM input may only be assembled for deterministically eligible sources")
        route = self._resolve_discovery_model_route()
        stored_payload = dict(payload)
        stored_payload["model_binding"] = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        request = {"run_id": run_id, "source_version_id": source_version_id, "payload": stored_payload, "prompt_version": prompt_version, "skill_version": skill_version, "model_config_version": route.config_version}
        replay = self._replay("stage1b_create_discovery_input", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("discovery_assembly"), _hash(stored_payload)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, run_id, source_version_id, _canonical(stored_payload), integrity_hash, prompt_version, skill_version, route.config_version, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash, "model_config_hash": route.config_hash}
            self._receipt("stage1b_create_discovery_input", idempotency_key, request, result)
            self._audit(run_id, "stage1b_input_assembly_created", result)
        return result

    def prepare_discovery_model_request(self, *, run_id: str, source_version_id: str, assembly_id: str, prompt: str) -> ModelRequest:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        assembly = self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StateTransitionError("discovery model request has stale or mismatched input")
        route = self._resolve_discovery_model_route()
        payload = json.loads(assembly["payload_json"])
        expected_binding = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        if payload.get("model_binding") != expected_binding or assembly["model_config_version"] != route.config_version:
            raise StaleResultError("discovery input assembly no longer matches the explicit model binding")
        return ModelRequest(
            route_name=route.route_name,
            prompt=prompt,
            input_payload=payload,
            correlation_id=run_id,
            skill_name="stage1b.source_to_topic",
            skill_version=assembly["skill_version"],
            binding_name="business_analysis",
            binding_version=route.config_version,
            binding_hash=route.config_hash,
            metadata={"stage1b_core": {"run_id": run_id, "source_version_id": source_version_id, "input_assembly_id": assembly_id, "data_identity": self.data_identity, "prompt_version": assembly["prompt_version"], "skill_version": assembly["skill_version"], "expected_binding": expected_binding}},
        )

    def persist_discovery_model_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway discovery identity does not match Core identity")
        run_id = str(binding.get("run_id") or "")
        source_version_id = str(binding.get("source_version_id") or "")
        assembly_id = str(binding.get("input_assembly_id") or "")
        run, source, assembly = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StaleResultError("discovery ModelGateway envelope belongs to stale input")
        route = self._resolve_discovery_model_route()
        expected_binding = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        if binding.get("expected_binding") != expected_binding:
            raise ModelGatewayRequiredError("discovery ModelGateway request lacks the current explicit binding")
        if (
            envelope.route_name != route.route_name
            or envelope.route_id != route.route_id
            or envelope.provider_name != route.provider_name
            or envelope.provider_ref != route.provider_ref
            or envelope.model_name != route.model_name
            or envelope.config_version != route.config_version
            or envelope.config_hash != route.config_hash
            or envelope.binding_name != "business_analysis"
            or envelope.binding_version != route.config_version
            or envelope.binding_hash != route.config_hash
        ):
            raise ModelGatewayRequiredError("discovery ModelGateway envelope does not match business_analysis binding")
        model_run_id = _id("discovery_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model_run_id, run_id, source_version_id, assembly_id, envelope.status, envelope.correlation_id,
                 binding["prompt_version"], binding["skill_version"], envelope.route_id or envelope.route_name,
                 envelope.config_version, envelope.provider_ref, envelope.provider_name, envelope.model_name,
                 assembly["integrity_hash"], envelope.output_hash, "not_validated", _canonical(envelope.error or {}),
                 "not_retried", _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                 _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1),
            )
            self._audit(run_id, "stage1b_model_gateway_envelope_recorded", {"model_run_id": model_run_id, "status": envelope.status})
        return model_run_id

    def record_discovery_model_validation_failure(self, *, model_run_id: str, reason: str) -> None:
        model_run = self._discovery_model_run(model_run_id)
        with self.conn:
            self.conn.execute("UPDATE stage1b_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?", (_canonical({"validation_error": reason}), model_run_id))
            self._audit(model_run["run_id"], "stage1b_model_output_validation_failed", {"model_run_id": model_run_id, "reason": reason})

    def record_discovery_no_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str | None,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("candidate absence must belong to an active source run")
        if model_run_id is not None:
            model_run = self._discovery_model_run(model_run_id)
            if model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
                raise ModelGatewayRequiredError("candidate absence model run does not belong to the source")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_candidate_absence", idempotency_key, request)
        if replay:
            return replay
        absence_id = _id("candidate_absence")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_absence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (absence_id, run_id, source_version_id, model_run_id, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            if model_run_id is not None:
                valid_business_absences = {
                    "model_returned_no_candidate",
                    "candidate_outside_domain_policy",
                    "candidate_material_insufficient",
                }
                validation_status = "passed" if reason_code in valid_business_absences else "failed"
                self.conn.execute("UPDATE stage1b_model_run SET validation_status=? WHERE model_run_id=?", (validation_status, model_run_id))
            result = {"absence_id": absence_id, "reason_code": reason_code}
            self._receipt("stage1b_record_candidate_absence", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_absent", result)
        return result

    def create_discovery_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str,
        candidate_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source, model_run = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_model_run(model_run_id)
        if run["status"] != "processing" or source["run_id"] != run_id or model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
            raise StateTransitionError("candidate does not belong to the active source run")
        if model_run["status"] != "succeeded" or model_run["via_model_gateway"] != 1 or model_run["validation_status"] != "not_validated":
            raise ModelGatewayRequiredError("candidate requires one successful unconsumed ModelGateway run")
        forbidden_fields = {"score", "rank", "weight", "recommendation_score", "quality_rank"}
        if forbidden_fields & set(payload):
            raise StateTransitionError("discovery candidates must not contain business-ranking fields")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_absence WHERE run_id=? AND source_version_id=?", (run_id, source_version_id)).fetchone():
            raise StateTransitionError("a source recorded as zero-candidate cannot create a candidate")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "candidate_id": candidate_id, "payload": payload}
        replay = self._replay("stage1b_create_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        candidate_version_id = _id("candidate_version")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_version VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'awaiting_user_decision', ?, ?)",
                (candidate_version_id, candidate_id, run_id, source["domain_label"], source_version_id, model_run_id, _canonical(payload), _hash(payload), self.data_identity, _now()),
            )
            self.conn.execute("UPDATE stage1b_model_run SET validation_status='passed' WHERE model_run_id=?", (model_run_id,))
            result = {"candidate_version_id": candidate_version_id, "candidate_id": candidate_id}
            self._receipt("stage1b_create_discovery_candidate", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_created", result)
        return result

    def complete_discovery_run(
        self,
        *,
        run_id: str,
        domains: tuple[str, ...],
        lifecycle_status: str = "completed",
        failure_reason: str | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        if lifecycle_status not in DISCOVERY_RUN_OUTCOMES - {"processing"}:
            raise StateTransitionError("discovery lifecycle status is invalid")
        if run["status"] == "completed":
            return {"run_id": run_id, "status": context["lifecycle_status"]}
        if run["status"] != "processing":
            raise StateTransitionError("only a processing discovery run can complete")
        request = {
            "run_id": run_id,
            "domains": list(domains),
            "lifecycle_status": lifecycle_status,
            "failure_reason": failure_reason,
        }
        replay = self._replay("stage1b_complete_discovery_run", idempotency_key, request)
        if replay:
            return replay
        with self.conn:
            for domain_label in domains:
                rows = self.conn.execute("SELECT candidate_version_id FROM stage1b_candidate_version WHERE run_id=? AND domain_label=? ORDER BY created_at, candidate_version_id", (run_id, domain_label)).fetchall()
                if rows:
                    for position, row in enumerate(rows, start=1):
                        snapshot_id = _id("snapshot")
                        self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)", (snapshot_id, run_id, domain_label, row["candidate_version_id"], position, self.data_identity, _now()))
                        source = self.conn.execute("SELECT expires_at FROM stage1b_source_version WHERE source_version_id=(SELECT source_version_id FROM stage1b_candidate_version WHERE candidate_version_id=?)", (row["candidate_version_id"],)).fetchone()
                        if source is not None and source["expires_at"] is None:
                            started_at = _now()
                            expires_at = (datetime.fromisoformat(started_at) + timedelta(days=3)).isoformat()
                            self.conn.execute(
                                "INSERT INTO stage1b_candidate_cooldown VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (_id("candidate_cooldown"), row["candidate_version_id"], snapshot_id, "system", "system", "ordinary_unselected_candidate_displayed", started_at, expires_at, self.data_identity, _now()),
                            )
                else:
                    self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, NULL, 0, ?, ?)", (_id("snapshot"), run_id, domain_label, self.data_identity, _now()))
            core_status = "completed" if lifecycle_status in {"completed", "completed_with_failures"} else "failed"
            completed_at = _now()
            self.conn.execute(
                "UPDATE stage1b_discovery_run SET status=?, completed_at=?, failure_reason=? WHERE run_id=?",
                (core_status, completed_at, failure_reason, run_id),
            )
            self.conn.execute(
                "UPDATE stage1b_run_execution_context SET lifecycle_status=? WHERE run_id=?",
                (lifecycle_status, run_id),
            )
            result = {"run_id": run_id, "status": lifecycle_status, "execution_mode": context["execution_mode"]}
            self._receipt("stage1b_complete_discovery_run", idempotency_key, request, result)
            self._audit(run_id, "stage1b_daily_snapshot_finalized", {**result, "failure_reason": failure_reason})
        return result

    def get_discovery_snapshot(self, *, run_id: str, domain_label: str) -> list[dict[str, Any]]:
        self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        rows = self.conn.execute(
            "SELECT snapshot.display_position, candidate.candidate_version_id, candidate.candidate_id, candidate.payload_json, source.source_type, source.source_time, source.expires_at, source.payload_json AS source_payload_json FROM stage1b_daily_snapshot snapshot LEFT JOIN stage1b_candidate_version candidate ON candidate.candidate_version_id=snapshot.candidate_version_id LEFT JOIN stage1b_source_version source ON source.source_version_id=candidate.source_version_id WHERE snapshot.run_id=? AND snapshot.domain_label=? ORDER BY snapshot.display_position, snapshot.snapshot_id",
            (run_id, domain_label),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["candidate_version_id"] is None:
                continue
            candidate_payload = json.loads(row["payload_json"])
            source_payload = json.loads(row["source_payload_json"])
            source_reference = candidate_payload.get("source_reference")
            if not isinstance(source_reference, dict):
                source_reference = {
                    "source_version_id": None,
                    "source_type": row["source_type"],
                    "source_time": row["source_time"],
                }
            stage1a_handoff_packet = {
                "candidate_version_id": row["candidate_version_id"],
                "candidate_id": row["candidate_id"],
                "source_type": row["source_type"],
                "core_question": candidate_payload.get("core_question"),
                "domain_label": domain_label,
                "recommendation_reason": candidate_payload.get("why_attention") or candidate_payload.get("new_angle"),
                "source_evidence_refs": [
                    source_reference,
                    source_payload.get("formal_source", {}),
                ],
                "risk_limits": candidate_payload.get("risk_limits"),
                "material_gap": candidate_payload.get("material_readiness"),
                "timeliness_limits": row["expires_at"] or "not_time_limited",
                "capacity_consumption": {
                    "formal_daily_content_slots": 1,
                    "domain_daily_limit": 1,
                },
                "user_confirmation_required": True,
            }
            result.append({"display_position": row["display_position"], "candidate_version_id": row["candidate_version_id"], "candidate_id": row["candidate_id"], "candidate": candidate_payload, "source_type": row["source_type"], "source_time": row["source_time"], "expires_at": row["expires_at"], "source": source_payload, "stage1a_handoff_packet": stage1a_handoff_packet, "execution_mode": context["execution_mode"], "lifecycle_status": context["lifecycle_status"], "formal_candidate_pool": context["execution_mode"] == "production_daily" and context["lifecycle_status"] == "completed"})
        return result

    def record_discovery_decision(
        self,
        *,
        candidate_version_id: str,
        decision: str,
        actor: str,
        reason: str,
        formal_topic_task_id: str | None,
        idempotency_key: str,
    ) -> dict[str, str]:
        candidate = self._discovery_candidate(candidate_version_id)
        if decision not in {"selected", "deferred", "rejected", "angle_change_requested", "evergreen"}:
            raise StateTransitionError("unsupported discovery decision")
        if decision == "selected":
            raise StateTransitionError("selected candidates must use select_discovery_candidate")
        if not reason.strip():
            raise StateTransitionError("a user decision requires a reason")
        if formal_topic_task_id is not None:
            raise StateTransitionError("only the atomic selection action may create a formal-topic link")
        if candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("candidate is not awaiting a user decision")
        request = {"candidate_version_id": candidate_version_id, "decision": decision, "actor": actor, "reason": reason, "formal_topic_task_id": formal_topic_task_id}
        replay = self._replay("stage1b_record_discovery_decision", idempotency_key, request)
        if replay:
            return replay
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        with self.conn:
            decision_id = _id("candidate_decision")
            self.conn.execute("INSERT INTO stage1b_candidate_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (decision_id, candidate_version_id, decision, actor, reason, formal_topic_task_id, self.data_identity, _now()))
            result = {"decision_id": decision_id, "candidate_version_id": candidate_version_id, "decision": decision}
            self._receipt("stage1b_record_discovery_decision", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_user_decision", result)
        return result

    def select_discovery_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Atomically hand one exact Stage 1B candidate to Stage 1A awaiting confirmation."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("candidate selection requires an explicit user and reason")
        candidate = self._discovery_candidate(candidate_version_id)
        run = self._discovery_run(candidate["run_id"])
        context = self._discovery_context(candidate["run_id"])
        if context["execution_mode"] != "production_daily" or context["lifecycle_status"] != "completed":
            raise StateTransitionError("only a completed production_daily candidate may enter Stage 1A")
        if run["status"] != "completed" or candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("only a completed awaiting-user-decision candidate may be selected")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        payload = json.loads(candidate["payload_json"])
        domain_label = str(candidate["domain_label"])
        now = _now()
        if self.domain_formal_topic_count(domain_label=domain_label, current_date=now[:10]) >= 1:
            raise StateTransitionError("the formal-domain daily capacity has already been used")
        if self.formal_topic_title_seen(domain_label=domain_label, normalized_title=str(payload.get("normalized_title") or "")):
            raise StateTransitionError("a matching formal topic already exists in this domain")
        source_ref = payload.get("source_reference")
        if not isinstance(source_ref, dict) or source_ref.get("source_version_id") != candidate["source_version_id"]:
            raise StateTransitionError("candidate source reference is not the exact candidate source version")
        request = {"candidate_version_id": candidate_version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("stage1b_select_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id, decision_id = _id("task"), _id("version"), _id("candidate_decision")
        topic_payload = {
            "title": payload["title"],
            "core_question": payload["core_question"],
            "domain": domain_label,
            "source_refs": [source_ref, {"kind": "daily_candidate", "candidate_version_id": candidate_version_id}],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "formal_topic", topic_version_id, "awaiting_human_review", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (topic_version_id, task_id, "formal_topic", "awaiting_human_review", "formal_topic_payload", "topic_submitted", self.data_identity, actor, now, 0),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            self.conn.execute(
                "INSERT INTO stage1b_candidate_decision VALUES (?, ?, 'selected', ?, ?, ?, ?, ?)",
                (decision_id, candidate_version_id, actor, reason, task_id, self.data_identity, now),
            )
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "decision_id": decision_id, "candidate_version_id": candidate_version_id}
            self._receipt("stage1b_select_discovery_candidate", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_selected_for_stage1a_confirmation", result)
            self._audit(task_id, "formal_topic_submitted_from_stage1b_candidate", result)
        return result

    def discovery_source_readiness(self, *, domain_label: str, daily_since: str) -> dict[str, Any]:
        if domain_label not in FORMAL_DOMAIN_LABELS:
            raise StateTransitionError("daily discovery requires a configured formal domain")
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        daily_sources = self.conn.execute(
            "SELECT COUNT(*) FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (domain_label, daily_since),
        ).fetchone()[0] if competitor_ready else 0
        historical_sources = self.conn.execute(
            "SELECT COUNT(*) FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence='formal' AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (domain_label,),
        ).fetchone()[0] if competitor_ready else 0
        hotspot_rows = self.conn.execute(
            "SELECT observation.title FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures')",
            (daily_since,),
        ).fetchall()
        hotspot_terms = tuple(str(term).casefold() for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
        hotspot_sources = sum(
            1 for row in hotspot_rows
            if hotspot_terms and any(term in str(row["title"]).casefold() for term in hotspot_terms)
        )
        tag_sources = self.conn.execute(
            "SELECT COUNT(*) FROM discovered_external_videos video JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
            "WHERE video.domain_label=? AND tag.status='active' AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>''",
            (domain_label,),
        ).fetchone()[0]
        question_expansion_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_question_expansion_source WHERE domain_label=? "
            "AND validation_outcome='supported' AND data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        saved_user_direction_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        counts = {
            "hotspot_sources": hotspot_sources,
            "daily_sources": daily_sources,
            "historical_sources": historical_sources,
            "tag_sources": tag_sources,
            "question_expansion_sources": question_expansion_sources,
            "saved_user_direction_sources": saved_user_direction_sources,
        }
        if not any(counts.values()):
            return {"status": "blocked", "reason": "no_qualified_formal_source", **counts}
        return {"status": "ready", "reason": "qualified_formal_source_available", **counts}

    def load_real_discovery_sources(self, *, domain_label: str, daily_since: str, per_source_limit: int) -> list[dict[str, Any]]:
        """Read only qualified, already-recorded formal source facts; never collect or invent content."""
        if self.discovery_source_readiness(domain_label=domain_label, daily_since=daily_since)["status"] != "ready":
            return []
        result: list[dict[str, Any]] = []
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        all_hotspot_rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures') "
            "ORDER BY observation.observed_at DESC, observation.observation_id ASC",
            (daily_since,),
        ).fetchall()
        hotspot_terms = tuple(str(term).casefold() for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
        hotspot_rows = [
            row for row in all_hotspot_rows
            if hotspot_terms and any(term in str(row["title"]).casefold() for term in hotspot_terms)
        ][:per_source_limit]
        for row in hotspot_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "hotspot", "source_object_id": row["observation_id"], "source_object_version": row["observed_at"], "source_time": row["observed_at"], "payload": {"source_id": row["observation_id"], "title": row["title"], "url": row["url"], "account_name": f"TrendRadar/{row['source_channel']}", "source_time": row["observed_at"], "formal_source": {"table": "trendradar_hotspot_observation", "object_id": row["observation_id"], "object_version": row["observed_at"], "raw_metadata_hash": raw_hash}}})
        daily_rows = self.conn.execute(
            "SELECT video.video_id, video.title, video.url, video.publish_time, video.last_checked_at, video.raw_json, account.account_name "
            "FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            "ORDER BY video.publish_time DESC, video.video_id ASC LIMIT ?",
            (domain_label, daily_since, per_source_limit),
        ).fetchall() if competitor_ready else []
        for row in daily_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "daily_competitor_content", "source_object_id": row["video_id"], "source_object_version": row["last_checked_at"], "source_time": row["publish_time"], "payload": {"source_id": row["video_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "formal_source": {"table": "competitor_videos", "object_id": row["video_id"], "object_version": row["last_checked_at"], "raw_metadata_hash": raw_hash}}})
        historical_rows = self.conn.execute(
            "SELECT hit.hit_id, hit.title, hit.url, hit.publish_time, hit.promoted_at, hit.hit_channel, hit.judgment_confidence, "
            "video.raw_json, account.account_name FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence='formal' AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            "ORDER BY hit.promoted_at DESC, hit.hit_id ASC LIMIT ?",
            (domain_label, per_source_limit),
        ).fetchall() if competitor_ready else []
        for row in historical_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "historical_high_signal", "source_object_id": row["hit_id"], "source_object_version": row["promoted_at"], "source_time": row["publish_time"], "payload": {"source_id": row["hit_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "signal_basis": row["hit_channel"], "signal_confidence": row["judgment_confidence"], "formal_source": {"table": "hits", "object_id": row["hit_id"], "object_version": row["promoted_at"], "raw_metadata_hash": raw_hash}}})
        tag_rows = self.conn.execute(
            "SELECT video.*, tag.tag FROM discovered_external_videos video "
            "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id WHERE video.domain_label=? "
            "AND tag.status='active' AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "ORDER BY video.discovered_at DESC, video.discovered_video_id ASC LIMIT ?",
            (domain_label, per_source_limit),
        ).fetchall()
        for row in tag_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "tag_discovery", "source_object_id": row["discovered_video_id"], "source_object_version": row["discovered_at"], "source_time": row["discovered_at"], "payload": {"source_id": row["discovered_video_id"], "title": row["title"], "url": row["url"], "account_name": f"标签搜索/{row['tag']}", "source_time": row["discovered_at"], "formal_source": {"table": "discovered_external_videos", "object_id": row["discovered_video_id"], "object_version": row["discovered_at"], "raw_metadata_hash": raw_hash}}})
        expansion_rows = self.conn.execute(
            "SELECT * FROM stage1_question_expansion_source WHERE domain_label=? "
            "AND validation_outcome='supported' AND data_identity=? ORDER BY validated_at DESC, expansion_id ASC LIMIT ?",
            (domain_label, self.data_identity, per_source_limit),
        ).fetchall()
        for row in expansion_rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "source_type": "question_expansion",
                "source_object_id": row["expansion_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["validated_at"],
                "payload": {
                    **payload,
                    "source_id": row["expansion_id"],
                    "url": "",
                    "account_name": "已完成拓展验证",
                    "source_time": row["validated_at"],
                    "formal_source": {
                        "table": "stage1_question_expansion_source",
                        "object_id": row["expansion_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        direction_rows = self.conn.execute(
            "SELECT * FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=? ORDER BY saved_at DESC, direction_id ASC LIMIT ?",
            (domain_label, self.data_identity, per_source_limit),
        ).fetchall()
        for row in direction_rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "source_type": "saved_user_direction",
                "source_object_id": row["direction_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["saved_at"],
                "payload": {
                    **payload,
                    "source_id": row["direction_id"],
                    "url": "",
                    "account_name": "用户保存方向",
                    "source_time": row["saved_at"],
                    "formal_source": {
                        "table": "stage1_saved_user_direction_source",
                        "object_id": row["direction_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        return result

    def discovery_source_seen(self, *, source_type: str, source_object_id: str, source_object_version: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM stage1b_source_version WHERE source_type=? AND source_object_id=? AND source_object_version=? AND data_identity=?", (source_type, source_object_id, source_object_version, self.data_identity)).fetchone()
        return row is not None

    def discovery_candidate_in_cooldown(self, *, domain_label: str, normalized_title: str, now: str) -> bool:
        rows = self.conn.execute(
            "SELECT candidate.payload_json FROM stage1b_candidate_cooldown cooldown JOIN stage1b_candidate_version candidate "
            "ON candidate.candidate_version_id=cooldown.candidate_version_id WHERE candidate.domain_label=? "
            "AND candidate.data_identity=? AND cooldown.data_identity=? AND cooldown.expires_at>? "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision decision WHERE decision.candidate_version_id=candidate.candidate_version_id)",
            (domain_label, self.data_identity, self.data_identity, now),
        ).fetchall()
        return any(
            normalized_title in {
                str(json.loads(row["payload_json"]).get("normalized_title", "")),
                str(json.loads(row["payload_json"]).get("normalized_source_title", "")),
            }
            for row in rows
        )

    def formal_topic_title_seen(self, *, domain_label: str, normalized_title: str) -> bool:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return False
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=?",
            (self.data_identity,),
        ).fetchall()
        return any(json.loads(row["payload_json"]).get("domain") == domain_label and str(json.loads(row["payload_json"]).get("title", "")).casefold() == normalized_title for row in rows)

    def domain_formal_topic_count(self, *, domain_label: str, current_date: str) -> int:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return 0
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=? AND substr(version.created_at, 1, 10)=?",
            (self.data_identity, current_date),
        ).fetchall()
        return sum(1 for row in rows if json.loads(row["payload_json"]).get("domain") == domain_label)

    def get_discovery_candidate(self, candidate_version_id: str) -> dict[str, Any]:
        row = self._discovery_candidate(candidate_version_id)
        return {key: row[key] for key in row.keys()} | {"payload": json.loads(row["payload_json"])}

    def get_artifact_payload(self, version_id: str) -> dict[str, Any]:
        version = self._version(version_id)
        row = self.conn.execute(
            "SELECT artifact_kind, payload_json, integrity_hash, created_at FROM stage1a_artifact_payload WHERE version_id=? AND data_identity=?",
            (version_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("artifact payload does not exist for this version")
        return {
            "version_id": version_id,
            "task_id": version["task_id"],
            "node": version["node"],
            "artifact_kind": row["artifact_kind"],
            "payload": json.loads(row["payload_json"]),
            "integrity_hash": row["integrity_hash"],
            "created_at": row["created_at"],
        }

    def _insert_artifact_payload(self, version_id: str, artifact_kind: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO stage1a_artifact_payload VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, artifact_kind, _canonical(payload), _hash(payload), self.data_identity, _now()),
        )

    def _approved_upstream(self, task: sqlite3.Row, node: str, upstream_version_id: str | None) -> str:
        expected_node = UPSTREAM_NODE[node]
        if not upstream_version_id:
            raise StateTransitionError("exact approved upstream version is required")
        upstream = self._version(upstream_version_id)
        if upstream["task_id"] != task["task_id"] or upstream["node"] != expected_node:
            raise StateTransitionError("upstream version is not the adjacent required node")
        approved = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE version_id=? AND decision='approved'", (upstream_version_id,)
        ).fetchone()
        if approved is None:
            raise StateTransitionError("upstream version is not approved")
        return upstream_version_id

    @staticmethod
    def _assert_current_node(task: sqlite3.Row, node: str, status: str, version_id: str | None = None) -> None:
        if task["current_node"] != node or task["current_status"] != status:
            raise StateTransitionError(f"task is at {task['current_node']}/{task['current_status']}, not {node}/{status}")
        if version_id is not None and task["current_version_id"] != version_id:
            raise StaleResultError("node version is no longer current")

    def _set_task(self, task_id: str, *, node: str, version_id: str, status: str, revision: int) -> None:
        self.conn.execute(
            "UPDATE stage0_content_task SET current_node=?, current_version_id=?, current_status=?, task_revision=? WHERE task_id=?",
            (node, version_id, status, revision, task_id),
        )

    def _decision(self, task_id: str, node: str, version_id: str, decision: str, actor: str, actor_kind: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO stage0_content_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("decision"), task_id, node, version_id, decision, actor, actor_kind, reason, _now(), self.data_identity),
        )

    def _replay(self, scope: str, key: str, request: dict[str, Any]) -> dict[str, str] | None:
        if not key:
            raise StateTransitionError("idempotency_key is required")
        row = self.conn.execute("SELECT request_hash, result_json FROM stage0_command_receipt WHERE command_scope=? AND idempotency_key=?", (scope, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != _hash(request):
            raise StateTransitionError("idempotency key was reused with a different request")
        return {str(key): str(value) for key, value in json.loads(row["result_json"]).items()}

    def _receipt(self, scope: str, key: str, request: dict[str, Any], result: dict[str, str]) -> None:
        self.conn.execute("INSERT INTO stage0_command_receipt VALUES (?, ?, ?, ?, ?)", (scope, key, _hash(request), _canonical(result), _now()))

    def _audit(self, task_id: str | None, action: str, payload: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO stage0_audit_event VALUES (?, ?, ?, ?, ?, ?)", (_id("audit"), task_id, action, _canonical(payload), self.data_identity, _now()))
