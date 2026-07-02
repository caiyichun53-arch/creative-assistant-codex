from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from scripts.core.model_gateway.goal07_model_gateway import ModelProviderResult, ModelUsage
import scripts.core.model_gateway.run_content_classify_live_gate as content_live_gate
from scripts.core.model_gateway.business_route_registry import load_registry, scan_direct_model_calls
from scripts.core.model_gateway.formal_skill_adapter import (
    CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION,
    DeterministicContentClassifyModelPort,
    FormalBusinessSkillAdapter,
    FormalBusinessSkillMaterializer,
    FormalBusinessSkillWorker,
    FormalSkillContract,
    FormalSkillValidationError,
    apply_binding,
    clean_room_status,
    load_content_classify_business_contract,
    load_content_classify_fixtures,
    load_formal_mapping,
    make_content_classify_harness,
    preprocess_content_classify_input,
    run_verification,
    sample_content_classify_input,
    validate_content_classify_business_contract,
    validate_content_classify_output_semantics,
    validate_formal_mapping,
    validate_payload,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelGateway, ModelRunMaterializer
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.scheduler.goal03_scheduler import NoClaimableJob


class HarnessMixin:
    def make_harness(self, **kwargs):
        harness = make_content_classify_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class FormalSkillRouteMappingTests(unittest.TestCase):
    def test_mapping_keeps_content_classify_as_first_formal_skill(self) -> None:
        result = validate_formal_mapping(load_formal_mapping())
        self.assertEqual(result["formal_skill_count"], 12)
        self.assertEqual(result["active_formal_business_skill"], "content_classify")
        self.assertEqual(result["unmapped_existing_business_nodes"], [])

    def test_every_existing_business_node_has_exactly_one_owner(self) -> None:
        mapping = load_formal_mapping()
        mapped_nodes = [node["node_id"] for node in mapping["business_model_nodes"]]
        registry_routes = sorted(node["logical_route"] for node in load_registry()["nodes"])
        self.assertEqual(sorted(mapped_nodes), registry_routes)
        self.assertEqual(len(mapped_nodes), len(set(mapped_nodes)))


class ContentClassifyBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_content_classify_business_contract(load_content_classify_business_contract())
        self.assertEqual(result["skill_id"], "content_classify")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_first_contract_loads_active_business_contract(self) -> None:
        contract = FormalSkillContract.from_yaml()
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "content_classify")
        self.assertEqual(contract.version, "1.0.0")
        self.assertEqual(contract.route_name, "business.topic_judgement")
        self.assertEqual(contract.allowed_model_nodes, ("business.topic_judgement",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml()
        validate_payload(sample_content_classify_input(), contract.input_schema)
        for key in ("request_id", "correlation_id", "content_id", "evidence_items", "language_hint", "domain_hint"):
            with self.subTest(missing=key):
                invalid = sample_content_classify_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_classify_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_classify_input(domain_hint="legacy_domain"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_classify_input(evidence_items=[]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_classify_input(body="x" * 1201), contract.input_schema)

    def test_formal_output_schema_and_semantics_reject_bad_results(self) -> None:
        contract = FormalSkillContract.from_yaml()
        good = {
            "classification_status": "classified",
            "primary_label": "fan_kepu_social_life",
            "candidate_labels": ["fan_kepu_social_life"],
            "no_result_reason": "none",
            "uncertainty_reason": "none",
            "confidence": "high",
            "rationale": "Evidence centers on social-life explanatory content.",
            "evidence_used": ["社区电梯早高峰拥堵"],
            "schema_version": CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION,
        }
        validate_payload(good, contract.output_schema)
        validate_content_classify_output_semantics(sample_content_classify_input(), good)
        for bad in (
            good | {"primary_label": "missing_domain"},
            good | {"evidence_used": ["unseen evidence"]},
            good | {"classification_status": "no_result", "primary_label": "fan_kepu_social_life"},
            good | {"classification_status": "multiple_candidates", "primary_label": "fan_kepu_social_life"},
            good | {"rationale": "This has viral quality."},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(bad, contract.output_schema)
                    validate_content_classify_output_semantics(sample_content_classify_input(), bad)

    def test_binding_uses_deterministic_preprocessing_and_no_old_fields(self) -> None:
        contract = FormalSkillContract.from_yaml()
        input_payload = sample_content_classify_input()
        model_input = apply_binding(
            contract.input_map,
            input_payload,
            {},
            preprocess_content_classify_input(input_payload),
        )
        self.assertEqual(model_input["fixture_id"], input_payload["request_id"])
        self.assertIn("title:", model_input["candidate_topic"])
        self.assertEqual(model_input["evidence_refs"], input_payload["evidence_items"])
        self.assertIn("fan_kepu_social_life", model_input["taxonomy"])
        self.assertNotIn("candidate_id", model_input)
        self.assertNotIn("source_refs", model_input)


class ContentClassifyFixtureTests(HarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_matches_expected_business_outcomes(self) -> None:
        harness = self.make_harness()
        fixtures = load_content_classify_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        required = set(load_content_classify_business_contract()["test_cases"]["required_fixture_ids"])
        self.assertTrue(required.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                active_harness = harness
                if fixture.get("provider_behavior"):
                    active_harness = self.make_harness(
                        provider=DeterministicContentClassifyModelPort(behavior=fixture["provider_behavior"])
                    )
                if fixture.get("expected_error"):
                    with self.assertRaises(FormalSkillValidationError):
                        active_harness.adapter.run(fixture["input"])
                    continue
                result = active_harness.adapter.run(fixture["input"])
                expected = fixture["expected"]
                self.assertEqual(result.output_payload["classification_status"], expected["classification_status"])
                self.assertEqual(result.output_payload["primary_label"], expected["primary_label"])
                validate_content_classify_output_semantics(fixture["input"], result.output_payload)

    def test_same_fixture_repeats_without_structural_drift(self) -> None:
        harness = self.make_harness()
        payload = sample_content_classify_input(request_id="repeatable")
        first = harness.adapter.run(payload).output_payload
        second = harness.adapter.run(payload).output_payload
        self.assertEqual(first, second)

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "invalid_structure", "missing_field"):
            with self.subTest(behavior=behavior):
                harness = self.make_harness(provider=DeterministicContentClassifyModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_content_classify_input(request_id=f"case-{behavior}"))

    def test_model_empty_result_can_materialize_formal_no_result(self) -> None:
        harness = self.make_harness(provider=DeterministicContentClassifyModelPort(behavior="empty_result_object"))
        result = harness.adapter.run(sample_content_classify_input(request_id="empty-result-model"))
        self.assertEqual(result.output_payload["classification_status"], "no_result")
        self.assertEqual(result.output_payload["no_result_reason"], "model_empty_result")


class FormalSkillAdapterTests(HarnessMixin, unittest.TestCase):
    def test_adapter_runs_through_approved_model_gateway_route(self) -> None:
        harness = self.make_harness()
        result = harness.adapter.run(sample_content_classify_input())
        self.assertEqual(result.output_payload["primary_label"], "fan_kepu_social_life")
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


class FormalSkillRuntimeTests(HarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_harness()
        created = harness.api.create_formal_skill_job(sample_content_classify_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["output"]["primary_label"], "fan_kepu_social_life")
        self.assertEqual(result["model_route"], "business.topic_judgement")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_failed_schema_run_has_no_result_or_outbox(self) -> None:
        harness = self.make_harness()
        created = harness.api.create_formal_skill_job(
            sample_content_classify_input(request_id="invalid-input", evidence_items=[]),
            max_attempts=1,
        )
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_harness(provider=DeterministicContentClassifyModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_content_classify_input(request_id="retry-once"),
            max_attempts=2,
        )
        first = harness.worker.run_once()
        second = harness.worker.run_once()
        self.assertEqual(first.status, "failed")
        self.assertEqual(first.reason, "queued")
        self.assertEqual(second.status, "succeeded")
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

    def test_two_workers_do_not_claim_same_queued_job(self) -> None:
        harness = self.make_harness()
        harness.api.create_formal_skill_job(sample_content_classify_input(request_id="concurrent-claim"))
        claim = harness.scheduler.claim_next(worker_id="worker-a")
        self.assertEqual(claim.attempt_no, 1)
        with self.assertRaises(NoClaimableJob):
            harness.scheduler.claim_next(worker_id="worker-b")

    def test_materializer_failure_does_not_create_success_result_or_outbox(self) -> None:
        harness = self.make_harness()

        class FailingMaterializer(FormalBusinessSkillMaterializer):
            def materialize_success(self, **kwargs):  # type: ignore[no-untyped-def]
                raise FormalSkillValidationError("injected materializer failure before write")

        materializer = FailingMaterializer(harness.store)
        worker = FormalBusinessSkillWorker(
            scheduler=harness.scheduler,
            adapter=harness.adapter,
            materializer=materializer,
            contract=harness.contract,
            worker_id="failing-materializer-worker",
        )
        created = harness.api.create_formal_skill_job(
            sample_content_classify_input(request_id="materializer-failure"),
            max_attempts=1,
        )
        step = worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])

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
        self.assertEqual(status["business_contract"]["missing_requirement_count"], 0)
        self.assertEqual(status["fake_fixture_e2e"]["outbox_count"], 1)
        self.assertEqual(status["fake_fixture_e2e"]["missing_required_fixtures"], [])

    def test_formal_production_roots_have_no_direct_old_model_calls(self) -> None:
        result = scan_direct_model_calls()
        self.assertEqual(result["formal_production_direct_model_call_count"], 0)
        self.assertGreater(result["legacy_direct_model_call_count"], 0)

    def test_clean_room_db_remains_empty(self) -> None:
        result = clean_room_status()
        self.assertEqual(result["table_count"], 20)
        self.assertEqual(result["total_rows"], 0)

    def test_live_gate_uses_model_gateway_and_no_fake_fallback(self) -> None:
        class FakeHermesAdapter:
            provider_name = "hermes"

            def __init__(self, config):  # type: ignore[no-untyped-def]
                self.config = config

            def complete(self, request, route):  # type: ignore[no-untyped-def]
                output = {
                    "classification_status": "classified",
                    "primary_label": "fan_kepu_social_life",
                    "candidate_labels": ["fan_kepu_social_life"],
                    "no_result_reason": "none",
                    "uncertainty_reason": "none",
                    "confidence": "high",
                    "rationale": "Evidence centers on social-life explanatory content.",
                    "evidence_used": ["社区电梯早高峰拥堵"],
                    "schema_version": CONTENT_CLASSIFY_OUTPUT_SCHEMA_VERSION,
                }
                return ModelProviderResult(
                    output_text=json.dumps(output, ensure_ascii=False, sort_keys=True),
                    usage=ModelUsage(prompt_tokens=11, completion_tokens=13, total_tokens=24),
                    cost={"status": "not_reported", "billing_mode": "subscription"},
                    provider_request_id="fake-live-provider-request",
                    metadata={
                        "cost_status": "not_reported",
                        "usage_status": "available",
                        "provider_request_id_status": "available",
                        "retry_count": 0,
                        "max_retries": 0,
                        "tools_enabled": False,
                        "memory_enabled": False,
                        "messaging_enabled": False,
                    },
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            config_data = yaml.safe_load((Path("config") / "live_gates.example.yaml").read_text(encoding="utf-8"))
            config_data["allow_live_calls"] = True
            config_data["shadow_only"] = True
            config_data["env_file"] = str(tmp / ".env.live-gates")
            config_data["evidence_root"] = str(tmp / "evidence")
            config_data["status_file"] = str(tmp / "status.yaml")
            for gate in config_data["gates"]:
                if gate["gate_id"] == "GATE-MODEL-PROVIDER":
                    gate["live_enabled"] = True
            config_path = tmp / "live_gates.yaml"
            config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
            (tmp / ".env.live-gates").write_text(
                "\n".join(
                    [
                        "MODEL_PROVIDER_API_KEY=test-model-key",
                        "MODEL_PROVIDER_BASE_URL=https://example.invalid/v1",
                        "MODEL_PROVIDER_MODEL=test-live-model",
                    ]
                ),
                encoding="utf-8",
            )
            config = content_live_gate.LiveGateConfig(config_path, env_path=tmp / ".env.live-gates")
            with mock.patch.object(content_live_gate, "HermesModelProviderAdapter", FakeHermesAdapter):
                status = content_live_gate.run_gate(config)
        self.assertEqual(status["status"], "COMPLETED")
        self.assertEqual(status["actual_call_count"], 1)
        self.assertTrue(status["model_gateway_used"])
        self.assertFalse(status["dry_run_fallback"])
        self.assertFalse(status["fake_port_fallback"])
        self.assertEqual(status["classification_status"], "classified")


if __name__ == "__main__":
    unittest.main()
