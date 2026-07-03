from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    EXPERIMENT_REVIEW_CONTRACT_PATH,
    DeterministicExperimentReviewModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_experiment_review_business_contract,
    load_experiment_review_fixtures,
    make_experiment_review_harness,
    sample_experiment_review_input,
    validate_experiment_review_business_contract,
    validate_experiment_review_output_semantics,
    validate_payload,
)


class ExperimentReviewHarnessMixin:
    def make_experiment_harness(self, **kwargs):
        harness = make_experiment_review_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class ExperimentReviewBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_experiment_review_business_contract(load_experiment_review_business_contract())
        self.assertEqual(result["skill_id"], "experiment_review")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_experiment_review_route(self) -> None:
        contract = FormalSkillContract.from_yaml(EXPERIMENT_REVIEW_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "experiment_review")
        self.assertEqual(contract.version, "1.0.0")
        self.assertEqual(contract.route_name, "business.experiment_review")
        self.assertEqual(contract.allowed_model_nodes, ("business.experiment_review",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(EXPERIMENT_REVIEW_CONTRACT_PATH)
        validate_payload(sample_experiment_review_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "experiment_id",
            "experiment_design",
            "execution_summary",
            "result_metrics",
            "evidence_items",
            "tested_experience_refs",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_experiment_review_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_experiment_review_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_experiment_review_input(evidence_items=[]), contract.input_schema)


class ExperimentReviewFixtureTests(ExperimentReviewHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_experiment_review_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                provider = DeterministicExperimentReviewModelPort(
                    behavior="inconclusive" if fixture["fixture_id"] == "inconclusive" else "success"
                )
                harness = self.make_experiment_harness(provider=provider)
                result = harness.adapter.run(fixture["input"])
                validate_experiment_review_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.experiment_review")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field", "unseen_experience", "unseen_evidence", "illegal_publish"):
            with self.subTest(behavior=behavior):
                harness = self.make_experiment_harness(provider=DeterministicExperimentReviewModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_experiment_review_input(request_id=f"experiment-case-{behavior}"))


class ExperimentReviewRuntimeTests(ExperimentReviewHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_experiment_harness()
        created = harness.api.create_formal_skill_job(sample_experiment_review_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.experiment_review")
        self.assertEqual(result["output"]["schema_version"], "experiment_review.output.v1")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_experiment_harness(provider=DeterministicExperimentReviewModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_experiment_review_input(request_id="experiment-retry-once"),
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
        harness = self.make_experiment_harness()
        payload = sample_experiment_review_input(request_id="experiment-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)

    def test_failed_schema_run_has_no_result_or_outbox(self) -> None:
        harness = self.make_experiment_harness()
        invalid = sample_experiment_review_input(request_id="experiment-invalid-input")
        invalid["schema_version"] = "legacy"
        created = harness.api.create_formal_skill_job(invalid, max_attempts=1)
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])


if __name__ == "__main__":
    unittest.main()
