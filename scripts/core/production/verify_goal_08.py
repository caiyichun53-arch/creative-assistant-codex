from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.model_gateway.goal07_model_gateway import (  # noqa: E402
    ModelGateway,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.goal07_skill_runner import PortableSkillRunner, PortableSkillSpec  # noqa: E402
from scripts.core.persistence.goal01_store import PersistenceStore, UUIDv7Generator, content_hash  # noqa: E402
from scripts.core.production.goal08_production_chain import (  # noqa: E402
    PreferenceCandidateCommand,
    ProductionArtifactCommand,
    ProductionVersionChainMaterializer,
    VersionRef,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_770_000_000_000

    def now_ms(self) -> int:
        self.value += 1
        return self.value


class FakeModelProvider:
    provider_name = "goal08-fake-provider"

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        return ModelProviderResult(
            output_text=f"script for {request.input_payload['topic_id']}",
            usage=ModelUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
            cost={"currency": "USD", "amount": "0"},
            provider_request_id="goal08-fake-request",
            metadata={"fixture": True},
        )


def make_store() -> PersistenceStore:
    clock = FakeClock()
    generator = UUIDv7Generator(now_ms=clock.now_ms, randbits=lambda bits: 42)
    return PersistenceStore.in_memory(id_factory=generator.new)


def payload_for(store: PersistenceStore, version_id: str) -> dict[str, object]:
    row = store.conn.execute("SELECT payload_json FROM trace_version WHERE version_id=?", (version_id,)).fetchone()
    return json.loads(row["payload_json"])


def root_for(store: PersistenceStore, version_id: str) -> str:
    return str(
        store.conn.execute("SELECT root_id FROM trace_version WHERE version_id=?", (version_id,)).fetchone()["root_id"]
    )


def create_evidence(store: PersistenceStore) -> tuple[str, str, VersionRef]:
    root = store.create_root("research_evidence")
    payload = {"claim": "fixture claim", "quote": "fixture quote"}
    version = store.append_version(root, payload, projection_version="goal06.research_evidence.v1")
    store.set_current_version(root, version)
    return (
        root,
        version,
        VersionRef(
            relation_role="uses_evidence",
            target_object_kind="research_evidence",
            target_stable_id=root,
            target_version_id=version,
            target_content_hash=content_hash(payload, "goal06.research_evidence.v1"),
            locator={"quote": "fixture quote"},
        ),
    )


def create_model_run(store: PersistenceStore) -> tuple[str, str]:
    route = ModelRoute(
        route_name="goal08_script",
        provider_name="goal08-fake-provider",
        model_name="goal08-fake-model",
        config_version="goal08.routes.v1",
        config_hash=content_hash({"route": "goal08_script"}, "goal08.route_config.v1"),
    )
    gateway = ModelGateway(
        routes={route.route_name: route},
        providers={"goal08-fake-provider": FakeModelProvider()},
        materializer=ModelRunMaterializer(store),
        monotonic_ms=FakeClock().now_ms,
    )
    skill = PortableSkillSpec(
        skill_name="goal08-script-writer",
        skill_version="0.1.0",
        route_name="goal08_script",
        prompt_template="Write script for {topic_id} from {evidence_text}",
        required_input_keys=("topic_id", "evidence_text"),
        output_contract={"format": "plain_text"},
    )
    result = PortableSkillRunner(gateway).run(
        skill=skill,
        input_payload={"topic_id": "topic-8", "evidence_text": "fixture quote"},
        correlation_id="goal08-model-correlation",
    )
    return root_for(store, result.model_run.envelope_version_id), result.model_run.envelope_version_id


def test_content_versions_are_immutable_and_replay_safe() -> None:
    store = make_store()
    materializer = ProductionVersionChainMaterializer(store)
    _evidence_root, evidence_version, evidence_ref = create_evidence(store)
    model_root, model_version = create_model_run(store)

    plan = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="content_plan",
            topic_id="topic-8",
            actor="editor",
            idempotency_key="topic-8-plan-v1",
            content_payload={"title": "Plan v1", "beats": ["a", "b"]},
            evidence_refs=(evidence_ref,),
        )
    )
    script_v1 = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="script",
            topic_id="topic-8",
            actor="writer",
            idempotency_key="topic-8-script-v1",
            content_payload={"text": "script v1"},
            evidence_refs=(evidence_ref,),
            model_run_root_id=model_root,
            model_run_envelope_version_id=model_version,
        )
    )
    script_v2 = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="script",
            topic_id="topic-8",
            actor="writer",
            idempotency_key="topic-8-script-v2",
            content_payload={"text": "script v2"},
            root_id=script_v1.root_id,
            based_on_version_id=script_v1.version_id,
            evidence_refs=(evidence_ref,),
        )
    )
    replay = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="script",
            topic_id="topic-8",
            actor="writer",
            idempotency_key="topic-8-script-v2",
            content_payload={"text": "script v2"},
            root_id=script_v1.root_id,
            based_on_version_id=script_v1.version_id,
            evidence_refs=(evidence_ref,),
        )
    )

    assert plan.version_id
    assert script_v1.root_id == script_v2.root_id
    assert script_v1.version_id != script_v2.version_id
    assert replay.replayed
    assert replay.version_id == script_v2.version_id
    assert store.conn.execute(
        "SELECT count(*) FROM trace_version WHERE root_id=?",
        (script_v1.root_id,),
    ).fetchone()[0] == 2
    assert payload_for(store, script_v1.version_id)["content_payload"] == {"text": "script v1"}
    assert payload_for(store, script_v2.version_id)["content_payload"] == {"text": "script v2"}
    assert store.conn.execute(
        "SELECT current_version_id FROM trace_root WHERE root_id=?",
        (script_v1.root_id,),
    ).fetchone()["current_version_id"] == script_v2.version_id
    refs = store.conn.execute(
        "SELECT relation_role, target_version_id FROM object_reference WHERE source_version_id=?",
        (script_v1.version_id,),
    ).fetchall()
    assert {row["relation_role"] for row in refs} >= {"uses_evidence", "generated_by_model_run"}
    assert evidence_version in {row["target_version_id"] for row in refs}
    assert model_version in {row["target_version_id"] for row in refs}
    assert store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope='goal08.production_chain'"
    ).fetchone()[0] == 3
    assert store.conn.execute(
        "SELECT count(*) FROM outbox_message WHERE topic='goal08.production_artifact.materialized'"
    ).fetchone()[0] == 3
    try:
        store.conn.execute("UPDATE trace_version SET payload_json='{}' WHERE version_id=?", (script_v1.version_id,))
    except Exception as exc:  # noqa: BLE001 - sqlite trigger type is implementation detail.
        assert "immutable" in str(exc)
    else:
        raise AssertionError("expected immutable trace_version update rejection")
    print("PASS GOAL-08 content versions are immutable, replay-safe, and model-run-linked")


def test_approval_publication_and_preference_candidate_are_separate() -> None:
    store = make_store()
    materializer = ProductionVersionChainMaterializer(store)
    profile_id = store.create_preference_profile("domain", "domain-general")
    _evidence_root, _evidence_version, evidence_ref = create_evidence(store)

    script = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="script",
            topic_id="topic-9",
            actor="writer",
            idempotency_key="topic-9-script-v1",
            content_payload={"text": "approved wording"},
            evidence_refs=(evidence_ref,),
        )
    )
    approval = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="approval",
            topic_id="topic-9",
            actor="editor",
            idempotency_key="topic-9-approval-v1",
            content_payload={"approved": True, "approved_version_id": script.version_id},
            evidence_refs=(
                VersionRef(
                    relation_role="approves_script_version",
                    target_object_kind="production_script",
                    target_stable_id=script.root_id,
                    target_version_id=script.version_id,
                    locator={"topic_id": "topic-9", "decision": "approval"},
                ),
            ),
        )
    )
    approved_draft = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="approved_draft",
            topic_id="topic-9",
            actor="editor",
            idempotency_key="topic-9-approved-draft-v1",
            content_payload={"text": "approved wording"},
            evidence_refs=(
                VersionRef(
                    relation_role="approved_by",
                    target_object_kind="production_approval",
                    target_stable_id=approval.root_id,
                    target_version_id=approval.version_id,
                    locator={"topic_id": "topic-9"},
                ),
            ),
        )
    )
    publication = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="publication_capture",
            topic_id="topic-9",
            actor="publisher",
            idempotency_key="topic-9-publication-v1",
            content_payload={"text": "published wording changed on platform", "platform": "fixture"},
            evidence_refs=(
                VersionRef(
                    relation_role="published_from_approved_draft",
                    target_object_kind="production_approved_draft",
                    target_stable_id=approved_draft.root_id,
                    target_version_id=approved_draft.version_id,
                    locator={"topic_id": "topic-9", "platform": "fixture"},
                ),
            ),
        )
    )
    manual_edit = materializer.materialize_artifact(
        ProductionArtifactCommand(
            artifact_kind="manual_edit",
            topic_id="topic-9",
            actor="human_editor",
            idempotency_key="topic-9-manual-edit-v1",
            content_payload={"from": "approved wording", "to": "published wording changed on platform"},
            evidence_refs=(
                VersionRef(
                    relation_role="edits_approved_draft",
                    target_object_kind="production_approved_draft",
                    target_stable_id=approved_draft.root_id,
                    target_version_id=approved_draft.version_id,
                    locator={"topic_id": "topic-9"},
                ),
            ),
        )
    )
    preference = materializer.record_preference_candidate(
        PreferenceCandidateCommand(
            profile_id=profile_id,
            actor="editor",
            idempotency_key="topic-9-preference-candidate-v1",
            preference_payload={"voice": "prefer platform wording only as candidate evidence"},
            origin="manual_edit",
            evidence_refs=(
                VersionRef(
                    relation_role="derived_from_manual_edit",
                    target_object_kind="production_manual_edit",
                    target_stable_id=manual_edit.root_id,
                    target_version_id=manual_edit.version_id,
                    locator={"topic_id": "topic-9"},
                ),
            ),
        )
    )

    assert approved_draft.root_id != publication.root_id
    assert approved_draft.version_id != publication.version_id
    assert payload_for(store, approved_draft.version_id)["content_payload"]["text"] == "approved wording"
    assert payload_for(store, publication.version_id)["content_payload"]["text"] == "published wording changed on platform"
    publication_refs = store.conn.execute(
        "SELECT relation_role, target_version_id FROM object_reference WHERE source_version_id=?",
        (publication.version_id,),
    ).fetchall()
    assert [(row["relation_role"], row["target_version_id"]) for row in publication_refs] == [
        ("published_from_approved_draft", approved_draft.version_id)
    ]
    pref_row = store.conn.execute(
        "SELECT status, origin, evidence_refs FROM content_preference_revision WHERE revision_id=?",
        (preference.revision_id,),
    ).fetchone()
    assert pref_row["status"] == "candidate"
    assert pref_row["origin"] == "manual_edit"
    assert "production_manual_edit" in pref_row["evidence_refs"]
    current = store.conn.execute(
        "SELECT current_revision_id FROM content_preference_profile WHERE profile_id=?",
        (profile_id,),
    ).fetchone()["current_revision_id"]
    assert current is None
    assert store.conn.execute(
        "SELECT count(*) FROM command_receipt WHERE command_scope='goal08.preference_candidate'"
    ).fetchone()[0] == 1
    print("PASS GOAL-08 approval/publication separation and non-permanent preference candidate")


def main() -> None:
    test_content_versions_are_immutable_and_replay_safe()
    test_approval_publication_and_preference_candidate_are_separate()
    print("GOAL-08 production chain verification passed")


if __name__ == "__main__":
    main()
