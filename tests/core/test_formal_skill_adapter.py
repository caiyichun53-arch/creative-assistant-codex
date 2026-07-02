from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace

from scripts.core.model_gateway.business_route_registry import load_registry, scan_direct_model_calls
from scripts.core.model_gateway.formal_skill_adapter import (
    DeterministicContentClassifyModelPort,
    FormalBusinessSkillAdapter,
    FormalSkillContract,
    FormalSkillValidationError,
    apply_binding,
    clean_room_status,
    load_formal_mapping,
    make_content_classify_harness,
    run_verification,
    sample_content_classify_input,
    validate_formal_mapping,
    validate_payload,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRunMaterializer
from scripts.core.persistence.goal01_store import PersistenceStore


class HarnessMixin:
    def make_harness(self, **kwargs):
        harness = make_content_classify_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class FormalSkillRouteMappingTests(unittest.TestCase):
    def test_mapping_defines_goal_formal_skill_list_and_first_slice(self) -> None:
        result = validate_formal_mapping(load_formal_mapping())
        self.assertEqual(result["formal_skill_count"], 12)
        self.assertEqual(result["active_first_skill"], "content_classify")
        self.assertEqual(result["unmapped_existing_business_nodes"], [])

    def test_every_business_node_has_one_formal_skill_owner(self) -> None:
        mapping = load_formal_mapping()
        mapped_nodes = [node["node_id"] for node in mapping["business_model_nodes"]]
        registry_routes = sorted(node["logical_route"] for node in load_registry()["nodes"])
        self.assertEqual(sorted(mapped_nodes), registry_routes)
        self.assertEqual(len(mapped_nodes), len(set(mapped_nodes)))
        for node in mapping["business_model_nodes"]:
            self.assertIsInstance(node["mapped_formal_skill"], str)
            self.assertIn(node["action"], {"retain", "rename", "replace", "remove"})

    def test_planned_skills_do_not_silently_reuse_first_slice_node(self) -> None:
        mapping = load_formal_mapping()
        planned = {item["formal_skill_id"]: item for item in mapping["formal_skills"]}
        self.assertEqual(planned["content_relation_judge"]["allowed_model_nodes"], [])
        self.assertEqual(planned["source_to_topic"]["allowed_model_nodes"], [])
        self.assertEqual(planned["content_classify"]["allowed_model_nodes"], ["business.topic_judgement"])


class FormalSkillContractTests(unittest.TestCase):
    def test_first_contract_is_frozen_content_classify(self) -> None:
        contract = FormalSkillContract.from_yaml()
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "content_classify")
        self.assertEqual(contract.route_name, "business.topic_judgement")
        self.assertEqual(contract.allowed_model_nodes, ("business.topic_judgement",))

    def test_input_schema_accepts_only_contract_shape(self) -> None:
        contract = FormalSkillContract.from_yaml()
        validate_payload(sample_content_classify_input(), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_classify_input(candidate_text=""), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            invalid = sample_content_classify_input()
            invalid.pop("source_refs")
            validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            invalid = sample_content_classify_input(extra_field=True)
            validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            invalid = sample_content_classify_input(source_refs=["a", "b", "c", "d", "e", "f"])
            validate_payload(invalid, contract.input_schema)

    def test_output_schema_rejects_invalid_enum_and_const(self) -> None:
        contract = FormalSkillContract.from_yaml()
        validate_payload(
            {
                "classification": "keep",
                "rationale": "synthetic",
                "evidence_used": ["synthetic-source-1"],
                "schema_version": "content_classify.output.v1",
            },
            contract.output_schema,
        )
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(
                {
                    "classification": "maybe",
                    "rationale": "synthetic",
                    "evidence_used": ["synthetic-source-1"],
                    "schema_version": "content_classify.output.v1",
                },
                contract.output_schema,
            )
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(
                {
                    "classification": "keep",
                    "rationale": "synthetic",
                    "evidence_used": ["synthetic-source-1"],
                    "schema_version": "wrong",
                },
                contract.output_schema,
            )

    def test_input_and_output_binding_are_explicit(self) -> None:
        contract = FormalSkillContract.from_yaml()
        input_payload = sample_content_classify_input()
        model_input = apply_binding(contract.input_map, input_payload, {})
        self.assertEqual(
            model_input,
            {
                "fixture_id": "synthetic-content-classify-001",
                "candidate_topic": "A practical synthetic topic worth keeping",
                "evidence_refs": ["synthetic-source-1"],
            },
        )
        output = apply_binding(
            contract.output_map,
            input_payload,
            {
                "decision": "keep",
                "rationale": "synthetic",
                "schema_version": "content_classify.model_output.v1",
            },
        )
        self.assertEqual(output["classification"], "keep")
        self.assertEqual(output["evidence_used"], ["synthetic-source-1"])


class FormalSkillAdapterTests(HarnessMixin, unittest.TestCase):
    def test_adapter_runs_through_approved_model_gateway_route(self) -> None:
        harness = self.make_harness()
        result = harness.adapter.run(sample_content_classify_input())
        self.assertEqual(result.output_payload["classification"], "keep")
        self.assertEqual(result.model_route, "business.topic_judgement")
        self.assertEqual(harness.provider.call_count, 1)

    def test_adapter_core_has_no_runtime_store_or_scheduler_attributes(self) -> None:
        harness = self.make_harness()
        forbidden_attrs = {"store", "conn", "scheduler", "materializer"}
        self.assertTrue(forbidden_attrs.isdisjoint(set(vars(harness.adapter))))

    def test_adapter_rejects_unapproved_route_contract(self) -> None:
        contract = FormalSkillContract.from_yaml()
        bad_contract = replace(contract, route_name="business.creation_draft")
        with self.assertRaises(FormalSkillValidationError):
            bad_contract.validate_contract()

    def test_adapter_rejects_missing_gateway_route(self) -> None:
        store = PersistenceStore.in_memory()
        self.addCleanup(store.conn.close)
        contract = FormalSkillContract.from_yaml()
        gateway = ModelGateway(routes={}, providers={}, materializer=ModelRunMaterializer(store))
        adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
        with self.assertRaises(FormalSkillValidationError):
            adapter.run(sample_content_classify_input())

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "invalid_structure"):
            with self.subTest(behavior=behavior):
                harness = self.make_harness(
                    provider=DeterministicContentClassifyModelPort(behavior=behavior)
                )
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_content_classify_input(request_id=f"case-{behavior}"))


class FormalSkillRuntimeTests(HarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_harness()
        created = harness.api.create_formal_skill_job(sample_content_classify_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["output"]["classification"], "keep")
        self.assertEqual(result["model_route"], "business.topic_judgement")
        self.assertEqual(result["model_port"], "formal_business_skill_test_port")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_failed_schema_run_has_no_result_or_outbox(self) -> None:
        harness = self.make_harness()
        created = harness.api.create_formal_skill_job(
            sample_content_classify_input(request_id="invalid-input", candidate_text=""),
            max_attempts=1,
        )
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])
        count = harness.store.conn.execute("SELECT count(*) FROM formal_business_skill_run").fetchone()[0]
        self.assertEqual(count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_harness(
            provider=DeterministicContentClassifyModelPort(behavior="fail_once")
        )
        created = harness.api.create_formal_skill_job(
            sample_content_classify_input(request_id="retry-once"),
            max_attempts=2,
        )
        first = harness.worker.run_once()
        second = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        self.assertEqual(first.status, "failed")
        self.assertEqual(first.reason, "queued")
        self.assertEqual(second.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(len(harness.api.list_outbox()), 1)
        self.assertEqual(harness.provider.call_count, 2)

    def test_job_enqueue_is_idempotent(self) -> None:
        harness = self.make_harness()
        payload = sample_content_classify_input(request_id="idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)

    def test_result_index_is_immutable(self) -> None:
        harness = self.make_harness()
        created = harness.api.create_formal_skill_job(sample_content_classify_input(request_id="immutable"))
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        with self.assertRaises(sqlite3.DatabaseError):
            harness.store.conn.execute(
                "UPDATE formal_business_skill_result_index SET model_port='changed' WHERE job_id=?",
                (created.job_id,),
            )


class FormalSkillGateTests(unittest.TestCase):
    def test_full_verification_status_is_completed(self) -> None:
        status = run_verification()
        self.assertEqual(status["status"], "COMPLETED")
        self.assertEqual(status["mapping"]["unmapped_existing_business_nodes"], [])
        self.assertEqual(status["fixture_e2e"]["outbox_count"], 1)

    def test_formal_production_roots_have_no_direct_old_model_calls(self) -> None:
        result = scan_direct_model_calls()
        self.assertEqual(result["formal_production_direct_model_call_count"], 0)
        self.assertGreater(result["legacy_direct_model_call_count"], 0)

    def test_clean_room_db_remains_empty(self) -> None:
        result = clean_room_status()
        self.assertEqual(result["table_count"], 20)
        self.assertEqual(result["total_rows"], 0)


if __name__ == "__main__":
    unittest.main()
