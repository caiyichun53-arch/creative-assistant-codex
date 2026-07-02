from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import yaml

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.goal07_skill_runner import HostBindingSpec, PortableSkillSpec, SkillContractError
from scripts.core.persistence.goal01_store import PersistenceStore, canonical_json, content_hash, uuid7
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler, NoClaimableJob


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = Path(__file__).with_name("goal_runtime_vertical_slice_schema.sqlite.sql")
RUNTIME_PROBE_JOB_KIND = "runtime_probe.execute"
RUNTIME_PROBE_WORKFLOW = "runtime_probe.vertical_slice"
RUNTIME_PROBE_SCHEMA_VERSION = "runtime_probe.output.v1"
RUNTIME_PROBE_SKILL_PATH = ROOT / "runtime_skills" / "runtime_probe" / "skill.yaml"


class RuntimeVerticalSliceError(RuntimeError):
    pass


class RuntimeProbeValidationError(RuntimeVerticalSliceError):
    pass


@dataclass(frozen=True)
class RuntimeProbeRequest:
    request_id: str
    correlation_id: str
    text: str
    requested_operation: str
    metadata: dict[str, Any]
    idempotency_key: str

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "text": self.text,
            "requested_operation": self.requested_operation,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class RuntimeProbeCreateResult:
    job_id: str
    status: str
    replayed: bool


@dataclass(frozen=True)
class RuntimeProbeStepResult:
    status: str
    job_id: str | None = None
    attempt_id: str | None = None
    result_version_id: str | None = None
    reason: str | None = None


class RuntimeProbeContract:
    allowed_operations = frozenset({"normalize", "summarize"})
    allowed_metadata_keys = frozenset({"case_id", "test_variant", "provider_behavior"})
    required_input_keys = frozenset({"request_id", "correlation_id", "text", "requested_operation", "metadata"})
    required_output_keys = frozenset(
        {"normalized_text", "operation", "result_code", "deterministic_summary", "trace", "schema_version"}
    )

    @classmethod
    def validate_request_dict(cls, payload: dict[str, Any]) -> RuntimeProbeRequest:
        missing = cls.required_input_keys - payload.keys()
        extra = payload.keys() - (cls.required_input_keys | {"idempotency_key"})
        if missing:
            raise RuntimeProbeValidationError(f"missing request fields: {sorted(missing)}")
        if extra:
            raise RuntimeProbeValidationError(f"unexpected request fields: {sorted(extra)}")
        metadata = payload["metadata"]
        if not isinstance(metadata, dict):
            raise RuntimeProbeValidationError("metadata must be an object")
        metadata_extra = metadata.keys() - cls.allowed_metadata_keys
        if metadata_extra:
            raise RuntimeProbeValidationError(f"unexpected metadata fields: {sorted(metadata_extra)}")
        if payload["requested_operation"] not in cls.allowed_operations:
            raise RuntimeProbeValidationError("unsupported requested_operation")
        for key in ("request_id", "correlation_id", "text"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise RuntimeProbeValidationError(f"{key} must be a non-empty string")
        idempotency_key = str(payload.get("idempotency_key") or payload["request_id"])
        return RuntimeProbeRequest(
            request_id=str(payload["request_id"]),
            correlation_id=str(payload["correlation_id"]),
            text=str(payload["text"]),
            requested_operation=str(payload["requested_operation"]),
            metadata=dict(metadata),
            idempotency_key=idempotency_key,
        )

    @classmethod
    def validate_output(cls, payload: dict[str, Any]) -> None:
        missing = cls.required_output_keys - payload.keys()
        extra = payload.keys() - cls.required_output_keys
        if missing:
            raise RuntimeProbeValidationError(f"missing output fields: {sorted(missing)}")
        if extra:
            raise RuntimeProbeValidationError(f"unexpected output fields: {sorted(extra)}")
        if payload["schema_version"] != RUNTIME_PROBE_SCHEMA_VERSION:
            raise RuntimeProbeValidationError("unexpected output schema_version")
        if payload["result_code"] != "ok":
            raise RuntimeProbeValidationError("result_code must be ok for successful output")
        if not isinstance(payload["trace"], dict):
            raise RuntimeProbeValidationError("trace must be an object")


class RuntimeProbeInputBinding:
    binding_name = "runtime_probe_public_input"
    binding_version = "0.1.0"

    def __init__(self) -> None:
        self.spec = HostBindingSpec(
            binding_name=self.binding_name,
            binding_version=self.binding_version,
            input_map={
                "request_id": "request_id",
                "correlation_id": "correlation_id",
                "text": "text",
                "requested_operation": "requested_operation",
                "metadata": "metadata",
            },
            static_inputs={},
            metadata={"goal": "GOAL-RUNTIME-VERTICAL-SLICE-01"},
        )

    @property
    def binding_hash(self) -> str:
        return self.spec.binding_hash

    def bind(self, frozen_payload: dict[str, Any]) -> dict[str, Any]:
        return self.spec.bind(frozen_payload)


def load_runtime_probe_skill(path: Path = RUNTIME_PROBE_SKILL_PATH) -> PortableSkillSpec:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    required = tuple(data["input_schema"]["required"])
    # Keep prompt free of provider and host/runtime implementation details.
    prompt = (
        "Return only JSON for runtime_probe. "
        "request_id={request_id}; correlation_id={correlation_id}; "
        "operation={requested_operation}; text={text}"
    )
    spec = PortableSkillSpec(
        skill_name=data["skill_name"],
        skill_version=str(data["skill_version"]),
        route_name=data["route_name"],
        prompt_template=prompt,
        required_input_keys=required,
        output_contract=data["output_schema"],
        metadata={"schema_version": data["schema_version"]},
    )
    spec.validate_clean_room()
    return spec


class DeterministicRuntimeProbeModelPort:
    provider_name = "runtime_probe_test_port"

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        behavior = (request.input_payload.get("metadata") or {}).get("provider_behavior", "success")
        if behavior == "failure":
            raise RuntimeError("synthetic model port failure")
        if behavior == "timeout":
            raise TimeoutError("synthetic model port timeout")
        if behavior == "empty":
            return ModelProviderResult(output_text="", usage=ModelUsage(), cost={"test": 0}, provider_request_id="fake-empty")
        if behavior == "invalid_structure":
            return ModelProviderResult(
                output_text=json.dumps({"unexpected": True}, ensure_ascii=False),
                usage=ModelUsage(total_tokens=1),
                cost={"test": 0},
                provider_request_id="fake-invalid",
            )
        text = str(request.input_payload["text"])
        operation = str(request.input_payload["requested_operation"])
        normalized = " ".join(text.strip().lower().split())
        output = {
            "normalized_text": normalized,
            "operation": operation,
            "result_code": "ok",
            "deterministic_summary": f"{operation}:{len(normalized)}:{content_hash({'text': normalized})[:12]}",
            "trace": {
                "request_id": request.input_payload["request_id"],
                "correlation_id": request.input_payload["correlation_id"],
                "model_route": route.route_name,
                "model_port": self.provider_name,
            },
            "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        }
        return ModelProviderResult(
            output_text=json.dumps(output, ensure_ascii=False, sort_keys=True),
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost={"test": 0},
            provider_request_id=f"fake-{request.input_payload['request_id']}",
        )


class RuntimeProbeRunner:
    def __init__(self, gateway: ModelGateway):
        self.gateway = gateway
        self.skill = load_runtime_probe_skill()
        self.binding = RuntimeProbeInputBinding()

    def run(self, frozen_payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        public_input = self.binding.bind(frozen_payload)
        RuntimeProbeContract.validate_request_dict({**public_input, "idempotency_key": frozen_payload.get("idempotency_key")})
        prompt = self.skill.render_prompt(public_input)
        model_run = self.gateway.complete(
            ModelRequest(
                route_name=self.skill.route_name,
                prompt=prompt,
                input_payload=public_input,
                correlation_id=public_input["correlation_id"],
                skill_name=self.skill.skill_name,
                skill_version=self.skill.skill_version,
                skill_hash=self.skill.skill_hash,
                binding_name=self.binding.binding_name,
                binding_version=self.binding.binding_version,
                binding_hash=self.binding.binding_hash,
            )
        )
        try:
            output_payload = json.loads(model_run.output_text)
        except json.JSONDecodeError as exc:
            raise RuntimeProbeValidationError("model output is not JSON") from exc
        RuntimeProbeContract.validate_output(output_payload)
        return output_payload, model_run.envelope_version_id


class RuntimeProbeMaterializer:
    def __init__(self, store: PersistenceStore, *, id_factory: Callable[[], str] = uuid7):
        self.store = store
        self.conn = store.conn
        self.id_factory = id_factory
        self.install_schema()

    def install_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def record_failed_run(
        self,
        *,
        job_id: str,
        attempt_id: str,
        frozen_payload: dict[str, Any],
        runner: RuntimeProbeRunner,
        error: dict[str, Any],
    ) -> str:
        skill_run_id = self.id_factory()
        input_hash = content_hash(runner.binding.bind(frozen_payload), "runtime_probe.public_input.v1")
        self.conn.execute(
            """
            INSERT INTO runtime_probe_skill_run(
                skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
                skill_name, skill_version, skill_hash, binding_name, binding_version,
                binding_hash, model_route, model_port, input_hash, error_json
            )
            VALUES(?, ?, ?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_run_id,
                job_id,
                attempt_id,
                frozen_payload["request_id"],
                frozen_payload["correlation_id"],
                runner.skill.skill_name,
                runner.skill.skill_version,
                runner.skill.skill_hash,
                runner.binding.binding_name,
                runner.binding.binding_version,
                runner.binding.binding_hash,
                runner.skill.route_name,
                "runtime_probe_test_port",
                input_hash,
                canonical_json(error),
            ),
        )
        return skill_run_id

    def materialize_success(
        self,
        *,
        job_id: str,
        attempt_id: str,
        frozen_payload: dict[str, Any],
        output_payload: dict[str, Any],
        model_run_envelope_version_id: str,
        runner: RuntimeProbeRunner,
    ) -> tuple[str, str, str, str]:
        public_input = runner.binding.bind(frozen_payload)
        input_hash = content_hash(public_input, "runtime_probe.public_input.v1")
        output_hash = content_hash(output_payload, RUNTIME_PROBE_SCHEMA_VERSION)
        skill_run_id = self.id_factory()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runtime_probe_skill_run(
                    skill_run_id, job_id, attempt_id, status, request_id, correlation_id,
                    skill_name, skill_version, skill_hash, binding_name, binding_version,
                    binding_hash, model_route, model_port, input_hash, output_hash,
                    model_run_envelope_version_id
                )
                VALUES(?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_run_id,
                    job_id,
                    attempt_id,
                    frozen_payload["request_id"],
                    frozen_payload["correlation_id"],
                    runner.skill.skill_name,
                    runner.skill.skill_version,
                    runner.skill.skill_hash,
                    runner.binding.binding_name,
                    runner.binding.binding_version,
                    runner.binding.binding_hash,
                    runner.skill.route_name,
                    "runtime_probe_test_port",
                    input_hash,
                    output_hash,
                    model_run_envelope_version_id,
                ),
            )
            root_id = self.store.create_root("runtime_probe_result")
            payload = {
                "goal": "GOAL-RUNTIME-VERTICAL-SLICE-01",
                "request_id": frozen_payload["request_id"],
                "correlation_id": frozen_payload["correlation_id"],
                "job_id": job_id,
                "skill_run_id": skill_run_id,
                "skill_version": runner.skill.skill_version,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                "model_route": runner.skill.route_name,
                "model_port": "runtime_probe_test_port",
                "output": output_payload,
            }
            version_id = self.store.append_version(
                root_id,
                payload,
                projection_version=RUNTIME_PROBE_SCHEMA_VERSION,
                business_payload={
                    "request_id": frozen_payload["request_id"],
                    "job_id": job_id,
                    "output_hash": output_hash,
                },
            )
            self.store.set_current_version(root_id, version_id)
            self.store.record_audit(
                event_type="runtime_probe.result.materialized",
                actor="runtime_probe_materializer",
                object_kind="runtime_probe_result",
                object_id=root_id,
                version_id=version_id,
                payload={"job_id": job_id, "skill_run_id": skill_run_id},
                correlation_id=frozen_payload["correlation_id"],
                causation_id=attempt_id,
            )
            outbox_id = self.store.enqueue_outbox(
                topic="runtime_probe.result.materialized",
                payload={
                    "request_id": frozen_payload["request_id"],
                    "job_id": job_id,
                    "result_version_id": version_id,
                    "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                },
                correlation_id=frozen_payload["correlation_id"],
                causation_id=skill_run_id,
            )
            self.conn.execute(
                """
                INSERT INTO runtime_probe_result_index(
                    job_id, request_id, correlation_id, result_root_id, result_version_id,
                    skill_run_id, skill_version, input_hash, output_hash, schema_version,
                    model_route, model_port
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    frozen_payload["request_id"],
                    frozen_payload["correlation_id"],
                    root_id,
                    version_id,
                    skill_run_id,
                    runner.skill.skill_version,
                    input_hash,
                    output_hash,
                    RUNTIME_PROBE_SCHEMA_VERSION,
                    runner.skill.route_name,
                    "runtime_probe_test_port",
                ),
            )
        return root_id, version_id, skill_run_id, outbox_id


class RuntimeProbeCoreAPI:
    def __init__(self, scheduler: Goal03Scheduler):
        self.scheduler = scheduler
        self.conn = scheduler.conn

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "component": "runtime_probe_core_api"}

    def readiness(self) -> dict[str, Any]:
        tables = {row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"scheduler_job", "runtime_probe_skill_run", "runtime_probe_result_index", "trace_version"}
        missing = required - tables
        return {"status": "ready" if not missing else "not_ready", "missing": sorted(missing)}

    def create_runtime_probe_job(self, payload: dict[str, Any]) -> RuntimeProbeCreateResult:
        request = RuntimeProbeContract.validate_request_dict(payload)
        result = self.scheduler.enqueue_job(
            job_kind=RUNTIME_PROBE_JOB_KIND,
            payload={**request.payload(), "idempotency_key": request.idempotency_key},
            idempotency_key=request.idempotency_key,
            correlation_id=request.correlation_id,
            max_attempts=3,
        )
        return RuntimeProbeCreateResult(job_id=result.job_id, status=result.status, replayed=result.replayed)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.scheduler.get_job(job_id)
        return {key: row[key] for key in row.keys()}

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT i.*, v.payload_json
              FROM runtime_probe_result_index i
              JOIN trace_version v ON v.version_id=i.result_version_id
             WHERE i.job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "job_id": row["job_id"],
            "request_id": row["request_id"],
            "correlation_id": row["correlation_id"],
            "result_version_id": row["result_version_id"],
            "skill_run_id": row["skill_run_id"],
            "input_hash": row["input_hash"],
            "output_hash": row["output_hash"],
            "schema_version": row["schema_version"],
            "model_route": row["model_route"],
            "model_port": row["model_port"],
            "output": payload["output"],
        }

    def list_outbox(self, *, topic: str = "runtime_probe.result.materialized") -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT outbox_id, topic, payload_json, status, attempts, correlation_id, causation_id
              FROM outbox_message
             WHERE topic=?
             ORDER BY created_at, outbox_id
            """,
            (topic,),
        ).fetchall()
        return [
            {
                "outbox_id": row["outbox_id"],
                "topic": row["topic"],
                "payload": json.loads(row["payload_json"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "correlation_id": row["correlation_id"],
                "causation_id": row["causation_id"],
            }
            for row in rows
        ]


class RuntimeProbeWorker:
    def __init__(
        self,
        *,
        scheduler: Goal03Scheduler,
        runner: RuntimeProbeRunner,
        materializer: RuntimeProbeMaterializer,
        worker_id: str,
    ):
        self.scheduler = scheduler
        self.runner = runner
        self.materializer = materializer
        self.worker_id = worker_id

    def run_once(self) -> RuntimeProbeStepResult:
        try:
            claim = self.scheduler.claim_next(worker_id=self.worker_id, lease_seconds=60)
        except NoClaimableJob:
            return RuntimeProbeStepResult(status="idle")
        job = self.scheduler.get_job(claim.job_id)
        if job["job_kind"] != RUNTIME_PROBE_JOB_KIND:
            self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": "unsupported_job_kind", "job_kind": job["job_kind"]},
                retry=False,
            )
            return RuntimeProbeStepResult(status="failed", job_id=claim.job_id, attempt_id=claim.attempt_id, reason="unsupported_job_kind")
        try:
            output_payload, model_run_version = self.runner.run(claim.payload)
            _root_id, version_id, skill_run_id, outbox_id = self.materializer.materialize_success(
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                frozen_payload=claim.payload,
                output_payload=output_payload,
                model_run_envelope_version_id=model_run_version,
                runner=self.runner,
            )
            self.scheduler.complete(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                result={"result_version_id": version_id, "skill_run_id": skill_run_id, "outbox_id": outbox_id},
            )
            return RuntimeProbeStepResult(
                status="succeeded",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                result_version_id=version_id,
            )
        except (ModelGatewayError, RuntimeProbeValidationError, SkillContractError) as exc:
            self.materializer.record_failed_run(
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                frozen_payload=claim.payload,
                runner=self.runner,
                error={"code": type(exc).__name__, "message": str(exc)},
            )
            next_status = self.scheduler.fail(
                attempt_id=claim.attempt_id,
                worker_id=self.worker_id,
                error={"code": type(exc).__name__, "message": str(exc)},
                retry=True,
            )
            return RuntimeProbeStepResult(
                status="failed",
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                reason=next_status,
            )


@dataclass(frozen=True)
class RuntimeProbeHarness:
    store: PersistenceStore
    scheduler: Goal03Scheduler
    api: RuntimeProbeCoreAPI
    worker: RuntimeProbeWorker
    gateway: ModelGateway
    materializer: RuntimeProbeMaterializer
    runner: RuntimeProbeRunner


def make_runtime_probe_harness(
    *,
    id_factory: Callable[[], str] = uuid7,
    now_ms: Callable[[], int] | None = None,
    monotonic_ms: Callable[[], int] | None = None,
) -> RuntimeProbeHarness:
    store = PersistenceStore.in_memory(id_factory=id_factory)
    scheduler = Goal03Scheduler(store, id_factory=id_factory, now_ms=now_ms)
    materializer = RuntimeProbeMaterializer(store, id_factory=id_factory)
    route = ModelRoute(
        route_name="runtime_probe.test",
        provider_name="runtime_probe_test_port",
        model_name="deterministic-runtime-probe",
        config_version="runtime-probe-test.v1",
        config_hash=content_hash({"route": "runtime_probe.test"}),
        timeout_ms=1000,
    )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={"runtime_probe_test_port": DeterministicRuntimeProbeModelPort()},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=monotonic_ms,
    )
    runner = RuntimeProbeRunner(gateway)
    worker = RuntimeProbeWorker(scheduler=scheduler, runner=runner, materializer=materializer, worker_id="runtime-probe-worker")
    api = RuntimeProbeCoreAPI(scheduler)
    return RuntimeProbeHarness(
        store=store,
        scheduler=scheduler,
        api=api,
        worker=worker,
        gateway=gateway,
        materializer=materializer,
        runner=runner,
    )
