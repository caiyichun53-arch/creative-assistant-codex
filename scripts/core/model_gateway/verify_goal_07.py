from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.goal07_skill_runner import (
    HostBindingSpec,
    PortableSkillRunner,
    PortableSkillSpec,
    SkillContractError,
    _contains_token,
)
from scripts.core.persistence.goal01_store import PersistenceStore, UUIDv7Generator, content_hash


class FakeModelProvider:
    provider_name = "fake-model-provider"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.calls += 1
        return ModelProviderResult(
            output_text=f"fake:{route.model_name}:{request.input_payload['topic_id']}",
            usage=ModelUsage(prompt_tokens=7, completion_tokens=5, total_tokens=12),
            cost={"currency": "USD", "amount": "0.000012"},
            provider_request_id="fake-request-1",
            metadata={"fixture": True},
        )


class FailingModelProvider:
    provider_name = "failing-provider"

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        raise RuntimeError("fixture provider failure")


class Clock:
    def __init__(self) -> None:
        self.values = iter((1_000, 1_037, 2_000, 2_011))

    def now_ms(self) -> int:
        return next(self.values)


def make_store() -> PersistenceStore:
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    return PersistenceStore.in_memory(id_factory=generator.new)


def make_route(provider_name: str = "fake-model-provider") -> ModelRoute:
    return ModelRoute(
        route_name="research_synthesis",
        provider_name=provider_name,
        model_name="fake-model-v1",
        config_version="goal07.routes.v1",
        config_hash=content_hash({"research_synthesis": provider_name}, "goal07.route_config.v1"),
        parameters={"temperature": 0},
    )


def make_timeout_route() -> ModelRoute:
    return ModelRoute(
        route_name="research_synthesis",
        provider_name="fake-model-provider",
        model_name="fake-model-v1",
        config_version="goal07.routes.v1",
        config_hash=content_hash({"research_synthesis": "fake-model-provider"}, "goal07.route_config.v1"),
        parameters={"temperature": 0},
        timeout_ms=10,
    )


def test_fake_provider_records_traceable_envelope() -> None:
    store = make_store()
    provider = FakeModelProvider()
    route = make_route()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )

    result = gateway.complete(
        ModelRequest(
            route_name="research_synthesis",
            prompt="Summarize fixture evidence.",
            input_payload={"topic_id": "topic-7", "evidence_ids": ["ev-1"]},
            correlation_id="goal07-fixture-correlation",
            skill_name="formal-research-summarizer",
            skill_version="0.1.0",
            skill_hash="skill-hash-fixture",
            binding_name="topic-first-research",
            binding_version="0.1.0",
            binding_hash="binding-hash-fixture",
        )
    )

    assert result.output_text == "fake:fake-model-v1:topic-7"
    assert result.envelope_version_id
    assert result.envelope.status == "succeeded"
    assert result.envelope.correlation_id == "goal07-fixture-correlation"
    assert result.envelope.provider_name == "fake-model-provider"
    assert result.envelope.model_name == "fake-model-v1"
    assert result.envelope.prompt_hash == content_hash({"prompt": "Summarize fixture evidence."}, "goal07.prompt.v1")
    assert result.envelope.input_hash == content_hash(
        {"topic_id": "topic-7", "evidence_ids": ["ev-1"]}, "goal07.model_input.v1"
    )
    assert result.envelope.output_hash == content_hash(
        {"output_text": "fake:fake-model-v1:topic-7"}, "goal07.model_output.v1"
    )
    assert result.envelope.duration_ms == 37
    assert result.envelope.usage.total_tokens == 12
    assert result.envelope.cost == {"currency": "USD", "amount": "0.000012"}
    assert provider.calls == 1

    row = store.conn.execute(
        "SELECT object_kind FROM trace_root WHERE root_id=(SELECT root_id FROM trace_version WHERE version_id=?)",
        (result.envelope_version_id,),
    ).fetchone()
    assert row["object_kind"] == "model_run_envelope"
    payload = json.loads(
        store.conn.execute(
            "SELECT payload_json FROM trace_version WHERE version_id=?",
            (result.envelope_version_id,),
        ).fetchone()["payload_json"]
    )
    assert payload["skill_name"] == "formal-research-summarizer"
    assert payload["binding_name"] == "topic-first-research"
    assert payload["config_version"] == "goal07.routes.v1"
    assert payload["correlation_id"] == "goal07-fixture-correlation"
    try:
        store.conn.execute("UPDATE trace_version SET payload_json='{}' WHERE version_id=?", (result.envelope_version_id,))
    except Exception as exc:  # noqa: BLE001 - database trigger error type is sqlite-specific.
        assert "immutable" in str(exc)
    else:
        raise AssertionError("expected immutable trace_version update rejection")
    audits = store.conn.execute("SELECT event_type FROM audit_event").fetchall()
    assert [row["event_type"] for row in audits] == ["goal07.model_gateway.run_envelope_recorded"]
    print("PASS fake provider records traceable run envelope")


def test_unknown_route_rejected_before_provider_execution() -> None:
    store = make_store()
    provider = FakeModelProvider()
    gateway = ModelGateway(
        routes={},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
    )

    try:
        gateway.complete(ModelRequest(route_name="missing", prompt="prompt", input_payload={}))
    except ModelGatewayError:
        assert provider.calls == 0
        assert store.conn.execute("SELECT count(*) FROM trace_root").fetchone()[0] == 0
        print("PASS unknown route rejected before provider execution")
        return
    raise AssertionError("expected unknown route rejection")


def test_provider_failure_records_failed_envelope() -> None:
    store = make_store()
    route = make_route(provider_name="failing-provider")
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={"failing-provider": FailingModelProvider()},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )

    try:
        gateway.complete(
            ModelRequest(
                route_name="research_synthesis",
                prompt="Summarize fixture evidence.",
                input_payload={"topic_id": "topic-7"},
                skill_name="formal-research-summarizer",
                binding_name="topic-first-research",
            )
        )
    except ModelGatewayError:
        payload = json.loads(store.conn.execute("SELECT payload_json FROM trace_version").fetchone()["payload_json"])
        assert payload["status"] == "failed"
        assert payload["output_hash"] is None
        assert payload["error"]["code"] == "provider_error"
        assert payload["duration_ms"] == 37
        print("PASS provider failure records failed run envelope")
        return
    raise AssertionError("expected provider failure")


def test_provider_timeout_records_failed_envelope() -> None:
    store = make_store()
    route = make_timeout_route()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={"fake-model-provider": FakeModelProvider()},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )

    try:
        gateway.complete(
            ModelRequest(
                route_name="research_synthesis",
                prompt="Summarize fixture evidence.",
                input_payload={"topic_id": "topic-7"},
                correlation_id="goal07-timeout-correlation",
            )
        )
    except ModelGatewayError:
        payload = json.loads(store.conn.execute("SELECT payload_json FROM trace_version").fetchone()["payload_json"])
        assert payload["status"] == "failed"
        assert payload["output_hash"] is None
        assert payload["error"]["code"] == "timeout"
        assert payload["error"]["timeout_ms"] == 10
        assert payload["duration_ms"] == 37
        assert payload["correlation_id"] == "goal07-timeout-correlation"
        print("PASS provider timeout records failed run envelope")
        return
    raise AssertionError("expected provider timeout")


def make_portable_skill() -> PortableSkillSpec:
    return PortableSkillSpec(
        skill_name="formal-research-summarizer",
        skill_version="0.1.0",
        route_name="research_synthesis",
        prompt_template="Summarize evidence for topic {topic_id}: {evidence_text}",
        required_input_keys=("topic_id", "evidence_text"),
        output_contract={"format": "plain_text", "max_chars": 400},
    )


def test_portable_skill_clean_room_rejects_host_database_leaks() -> None:
    skill = make_portable_skill()
    skill.validate_clean_room()
    payload_text = json.dumps(skill.as_payload(), ensure_ascii=False)
    for token in ("database", "table", "orm", "host_uuid", "trace_root", "formal_status"):
        assert not _contains_token(payload_text.lower(), token)

    leaked = PortableSkillSpec(
        skill_name="bad-skill",
        skill_version="0.1.0",
        route_name="research_synthesis",
        prompt_template="Write trace_root {topic_id}",
        required_input_keys=("topic_id",),
        output_contract={"format": "plain_text"},
    )
    try:
        leaked.validate_clean_room()
    except SkillContractError:
        pass
    else:
        raise AssertionError("expected portable skill leak rejection")
    try:
        skill.skill_version = "0.2.0"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("expected portable skill immutability")
    assert not hasattr(skill, "publish")
    print("PASS portable skill clean-room rejects host/database leaks and self-modification")
    return


def test_host_binding_maps_without_leaking_host_identity() -> None:
    binding = HostBindingSpec(
        binding_name="topic-first-research",
        binding_version="0.1.0",
        input_map={"topic_id": "topic_id", "evidence_text": "brief_text"},
        static_inputs={"audience": "fixture"},
    )
    bound = binding.bind(
        {
            "topic_id": "topic-7",
            "brief_text": "fixture evidence",
            "host_uuid": "host-only-value",
        }
    )

    assert bound == {"audience": "fixture", "topic_id": "topic-7", "evidence_text": "fixture evidence"}
    assert "host_uuid" not in json.dumps(bound, ensure_ascii=False).lower()
    assert binding.binding_hash
    print("PASS host binding maps inputs without leaking host identity")


def test_runner_executes_skill_through_model_gateway_contract() -> None:
    store = make_store()
    provider = FakeModelProvider()
    route = make_route()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )
    binding = HostBindingSpec(
        binding_name="topic-first-research",
        binding_version="0.1.0",
        input_map={"topic_id": "topic_id", "evidence_text": "brief_text"},
    )
    skill_input = binding.bind({"topic_id": "topic-7", "brief_text": "fixture evidence"})
    result = PortableSkillRunner(gateway).run(
        skill=make_portable_skill(),
        input_payload=skill_input,
        binding=binding,
    )

    assert result.output_text == "fake:fake-model-v1:topic-7"
    payload = json.loads(
        store.conn.execute(
            "SELECT payload_json FROM trace_version WHERE version_id=?",
            (result.model_run.envelope_version_id,),
        ).fetchone()["payload_json"]
    )
    assert payload["skill_name"] == "formal-research-summarizer"
    assert payload["skill_hash"]
    assert payload["binding_name"] == "topic-first-research"
    assert payload["binding_hash"]
    assert payload["route_name"] == "research_synthesis"
    assert store.conn.execute("SELECT count(*) FROM trace_root WHERE object_kind='model_run_envelope'").fetchone()[0] == 1
    print("PASS runner executes portable skill through ModelGateway contract")


def test_integration_only_writes_run_envelope_through_materializer() -> None:
    store = make_store()
    provider = FakeModelProvider()
    route = make_route()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )
    binding = HostBindingSpec(
        binding_name="topic-first-research",
        binding_version="0.1.0",
        input_map={"topic_id": "topic_id", "evidence_text": "brief_text"},
    )
    portable_input = binding.bind(
        {
            "topic_id": "topic-7",
            "brief_text": "fixture evidence",
            "host_uuid": "host-only-value",
            "formal_status": "host-only-status",
        }
    )

    PortableSkillRunner(gateway).run(
        skill=make_portable_skill(),
        input_payload=portable_input,
        binding=binding,
        correlation_id="goal07-boundary-correlation",
    )

    object_kinds = {
        row["object_kind"]: row["count"]
        for row in store.conn.execute("SELECT object_kind, count(*) AS count FROM trace_root GROUP BY object_kind")
    }
    assert object_kinds == {"model_run_envelope": 1}
    assert store.conn.execute("SELECT count(*) FROM trace_version").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM binding_manifest").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM content_preference_profile").fetchone()[0] == 0
    payload = json.loads(store.conn.execute("SELECT payload_json FROM trace_version").fetchone()["payload_json"])
    assert payload["prompt_hash"]
    assert payload["correlation_id"] == "goal07-boundary-correlation"
    assert "Summarize evidence" not in json.dumps(payload, ensure_ascii=False)
    assert "host_uuid" not in json.dumps(payload, ensure_ascii=False).lower()
    assert "formal_status" not in json.dumps(payload, ensure_ascii=False).lower()
    print("PASS integration only writes run envelope through Materializer")


def test_repeated_execution_appends_envelopes_without_formal_side_effects() -> None:
    store = make_store()
    provider = FakeModelProvider()
    route = make_route()
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={provider.provider_name: provider},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=Clock().now_ms,
    )
    binding = HostBindingSpec(
        binding_name="topic-first-research",
        binding_version="0.1.0",
        input_map={"topic_id": "topic_id", "evidence_text": "brief_text"},
    )
    skill = make_portable_skill()
    portable_input = binding.bind({"topic_id": "topic-7", "brief_text": "fixture evidence"})
    runner = PortableSkillRunner(gateway)

    first = runner.run(skill=skill, input_payload=portable_input, binding=binding, correlation_id="repeat-1")
    second = runner.run(skill=skill, input_payload=portable_input, binding=binding, correlation_id="repeat-2")

    assert first.model_run.envelope_version_id != second.model_run.envelope_version_id
    assert store.conn.execute("SELECT count(*) FROM trace_root WHERE object_kind='model_run_envelope'").fetchone()[0] == 2
    assert store.conn.execute("SELECT count(*) FROM trace_version").fetchone()[0] == 2
    assert store.conn.execute("SELECT count(*) FROM audit_event").fetchone()[0] == 2
    assert store.conn.execute("SELECT count(*) FROM command_receipt").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM outbox_message").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM binding_manifest").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM content_preference_profile").fetchone()[0] == 0
    print("PASS repeated execution appends envelopes without formal side effects")


def main() -> None:
    test_fake_provider_records_traceable_envelope()
    test_unknown_route_rejected_before_provider_execution()
    test_provider_failure_records_failed_envelope()
    test_provider_timeout_records_failed_envelope()
    test_portable_skill_clean_room_rejects_host_database_leaks()
    test_host_binding_maps_without_leaking_host_identity()
    test_runner_executes_skill_through_model_gateway_contract()
    test_integration_only_writes_run_envelope_through_materializer()
    test_repeated_execution_appends_envelopes_without_formal_side_effects()
    print("GOAL-07 ModelGateway verification passed")


if __name__ == "__main__":
    main()
