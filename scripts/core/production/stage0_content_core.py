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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from scripts.core.execution_contract import require_baseline_citations
from scripts.core.model_gateway.goal07_model_gateway import ModelRequest, ModelRunEnvelope
from scripts.core.model_gateway.model_router import ModelRouter


ROOT = Path(__file__).resolve().parents[3]
FORMAL_DB_PATH = ROOT / "data" / "formal" / "production_activation.sqlite3"
DataIdentity = Literal["production", "test", "fixture", "synthetic", "replay", "mock"]
NON_PRODUCTION_IDENTITIES = frozenset({"test", "fixture", "synthetic", "replay", "mock"})

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
            """
        )
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
