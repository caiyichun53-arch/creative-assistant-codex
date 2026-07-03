from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH,
    DeterministicProductionResearchPlanModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_production_research_plan_business_contract,
    load_production_research_plan_fixtures,
    make_production_research_plan_harness,
    sample_production_research_plan_input,
    validate_payload,
    validate_production_research_plan_business_contract,
    validate_production_research_plan_output_semantics,
)


class ProductionResearchPlanHarnessMixin:
    def make_plan_harness(self, **kwargs):
        harness = make_production_research_plan_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class ProductionResearchPlanBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_production_research_plan_business_contract(load_production_research_plan_business_contract())
        self.assertEqual(result["skill_id"], "production_research_plan")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_research_synthesis_route(self) -> None:
        contract = FormalSkillContract.from_yaml(PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "production_research_plan")
        self.assertEqual(contract.route_name, "business.research_synthesis")
        self.assertEqual(contract.allowed_model_nodes, ("business.research_synthesis",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(PRODUCTION_RESEARCH_PLAN_CONTRACT_PATH)
        validate_payload(sample_production_research_plan_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "candidate_topic",
            "evidence_items",
            "tactic_candidates",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_production_research_plan_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_production_research_plan_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_production_research_plan_input(evidence_items=[]), contract.input_schema)


class ProductionResearchPlanFixtureTests(ProductionResearchPlanHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_production_research_plan_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_plan_harness()
                result = harness.adapter.run(fixture["input"])
                validate_production_research_plan_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.research_synthesis")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field", "unseen_claim", "unseen_use"):
            with self.subTest(behavior=behavior):
                harness = self.make_plan_harness(
                    provider=DeterministicProductionResearchPlanModelPort(behavior=behavior)
                )
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_production_research_plan_input(request_id=f"plan-case-{behavior}"))


class ProductionResearchPlanRuntimeTests(ProductionResearchPlanHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_plan_harness()
        created = harness.api.create_formal_skill_job(sample_production_research_plan_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.research_synthesis")
        self.assertEqual(result["output"]["schema_version"], "production_research_plan.output.v1")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_plan_harness(provider=DeterministicProductionResearchPlanModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_production_research_plan_input(request_id="plan-retry-once"),
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
        harness = self.make_plan_harness()
        payload = sample_production_research_plan_input(request_id="plan-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
