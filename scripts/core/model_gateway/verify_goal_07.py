from __future__ import annotations

import json
import sys
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


def main() -> None:
    test_fake_provider_records_traceable_envelope()
    test_unknown_route_rejected_before_provider_execution()
    test_provider_failure_records_failed_envelope()
    print("GOAL-07 ModelGateway verification passed")


if __name__ == "__main__":
    main()
