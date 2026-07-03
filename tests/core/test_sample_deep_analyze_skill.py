from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    SAMPLE_DEEP_ANALYZE_CONTRACT_PATH,
    DeterministicSampleDeepAnalyzeModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_sample_deep_analyze_business_contract,
    load_sample_deep_analyze_fixtures,
    make_sample_deep_analyze_harness,
    sample_sample_deep_analyze_input,
    validate_payload,
    validate_sample_deep_analyze_business_contract,
    validate_sample_deep_analyze_output_semantics,
)


class SampleDeepAnalyzeHarnessMixin:
    def make_sample_harness(self, **kwargs):
        harness = make_sample_deep_analyze_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class SampleDeepAnalyzeBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_sample_deep_analyze_business_contract(load_sample_deep_analyze_business_contract())
        self.assertEqual(result["skill_id"], "sample_deep_analyze")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_reverse_dna_route(self) -> None:
        contract = FormalSkillContract.from_yaml(SAMPLE_DEEP_ANALYZE_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "sample_deep_analyze")
        self.assertEqual(contract.route_name, "business.reverse_dna_analysis")
        self.assertEqual(contract.allowed_model_nodes, ("business.reverse_dna_analysis",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(SAMPLE_DEEP_ANALYZE_CONTRACT_PATH)
        validate_payload(sample_sample_deep_analyze_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "sample_id",
            "candidate_topic",
            "transcript_excerpt",
            "metrics",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_sample_deep_analyze_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_sample_deep_analyze_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_sample_deep_analyze_input(transcript_excerpt=""), contract.input_schema)


class SampleDeepAnalyzeFixtureTests(SampleDeepAnalyzeHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_sample_deep_analyze_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "high_metric_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_sample_harness()
                result = harness.adapter.run(fixture["input"])
                validate_sample_deep_analyze_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.reverse_dna_analysis")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field"):
            with self.subTest(behavior=behavior):
                harness = self.make_sample_harness(provider=DeterministicSampleDeepAnalyzeModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_sample_deep_analyze_input(request_id=f"sample-case-{behavior}"))


class SampleDeepAnalyzeRuntimeTests(SampleDeepAnalyzeHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_sample_harness()
        created = harness.api.create_formal_skill_job(sample_sample_deep_analyze_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.reverse_dna_analysis")
        self.assertEqual(result["output"]["schema_version"], "sample_deep_analyze.output.v1")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_sample_harness(provider=DeterministicSampleDeepAnalyzeModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_sample_deep_analyze_input(request_id="sample-retry-once"),
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
        harness = self.make_sample_harness()
        payload = sample_sample_deep_analyze_input(request_id="sample-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
