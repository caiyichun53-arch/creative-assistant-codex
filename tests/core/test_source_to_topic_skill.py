from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    SOURCE_TO_TOPIC_CONTRACT_PATH,
    DeterministicSourceToTopicModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    apply_binding,
    load_source_to_topic_business_contract,
    load_source_to_topic_fixtures,
    make_source_to_topic_harness,
    preprocess_formal_skill_input,
    sample_source_to_topic_input,
    validate_payload,
    validate_source_to_topic_business_contract,
    validate_source_to_topic_output_semantics,
)


class SourceToTopicHarnessMixin:
    def make_source_harness(self, **kwargs):
        harness = make_source_to_topic_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class RenderedPromptReachesModelTests(unittest.TestCase):
    """Regression: applies the same preventive fix real data proved
    necessary for the other 5 rewritten Skills, before this Skill's first
    real call rather than after a real failure -- Simplified Chinese output
    guidance and an explicit schema_version reminder (this Skill's output
    has 8 required keys, the most of any Skill fixed so far)."""

    def test_prompt_requires_chinese_and_reminds_schema_version(self) -> None:
        contract = FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
        input_payload = sample_source_to_topic_input()
        preprocessed = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
        model_input = apply_binding(contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, contract.model_input_schema)
        prompt = contract.portable_skill().render_prompt(model_input)

        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("Do not omit schema_version", prompt)


class SourceToTopicBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_source_to_topic_business_contract(load_source_to_topic_business_contract())
        self.assertEqual(result["skill_id"], "source_to_topic")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_source_to_topic_route(self) -> None:
        contract = FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "source_to_topic")
        self.assertEqual(contract.version, "1.0.0")
        self.assertEqual(contract.route_name, "business.source_to_topic")
        self.assertEqual(contract.allowed_model_nodes, ("business.source_to_topic",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
        validate_payload(sample_source_to_topic_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "source_id",
            "source_content",
            "source_evidence_items",
            "domain_label",
            "relation_summary",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_source_to_topic_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_source_to_topic_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_source_to_topic_input(source_evidence_items=[]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_source_to_topic_input(domain_label="legacy_domain"), contract.input_schema)

    def test_formal_output_schema_and_semantics_reject_bad_results(self) -> None:
        contract = FormalSkillContract.from_yaml(SOURCE_TO_TOPIC_CONTRACT_PATH)
        good = {
            "topic_status": "generated",
            "candidate_topic": "为什么小区电梯总在早高峰堵住",
            "topic_angle": "生活现象解释",
            "supporting_evidence": ["社区电梯早高峰拥堵"],
            "source_constraints": ["must_not_claim_platform_metrics_without_evidence"],
            "no_result_reason": "none",
            "confidence": "high",
            "schema_version": "source_to_topic.output.v1",
        }
        validate_payload(good, contract.output_schema)
        validate_source_to_topic_output_semantics(sample_source_to_topic_input(), good)
        for bad in (
            good | {"supporting_evidence": ["unseen evidence"]},
            good | {"topic_status": "generated", "candidate_topic": ""},
            good | {"topic_status": "needs_review", "source_constraints": []},
            good | {"topic_status": "no_result", "candidate_topic": "still has topic"},
            good | {"topic_status": "no_result", "candidate_topic": "", "topic_angle": "", "supporting_evidence": [], "no_result_reason": "none"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(bad, contract.output_schema)
                    validate_source_to_topic_output_semantics(sample_source_to_topic_input(), bad)


class SourceToTopicFixtureTests(SourceToTopicHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_source_to_topic_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        required = set(load_source_to_topic_business_contract()["fixture_cases"]["required_fixture_ids"])
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral", "needs_review_unknown_domain"}.issubset(fixture_ids))
        self.assertTrue(fixture_ids.issubset(required))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_source_harness()
                result = harness.adapter.run(fixture["input"])
                validate_source_to_topic_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.source_to_topic")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field", "evidence_not_in_input"):
            with self.subTest(behavior=behavior):
                harness = self.make_source_harness(provider=DeterministicSourceToTopicModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_source_to_topic_input(request_id=f"source-case-{behavior}"))


class SourceToTopicRuntimeTests(SourceToTopicHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_source_to_topic_result_and_outbox_once(self) -> None:
        harness = self.make_source_harness()
        created = harness.api.create_formal_skill_job(sample_source_to_topic_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["output"]["topic_status"], "generated")
        self.assertEqual(result["model_route"], "business.source_to_topic")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_failed_schema_run_has_no_result_or_outbox(self) -> None:
        harness = self.make_source_harness()
        invalid = sample_source_to_topic_input(request_id="source-invalid-input")
        invalid["schema_version"] = "legacy"
        created = harness.api.create_formal_skill_job(invalid, max_attempts=1)
        step = harness.worker.run_once()
        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "dead")
        self.assertIsNone(harness.api.get_result(created.job_id))
        self.assertEqual(harness.api.list_outbox(), [])

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_source_harness(provider=DeterministicSourceToTopicModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_source_to_topic_input(request_id="source-retry-once"),
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
        harness = self.make_source_harness()
        payload = sample_source_to_topic_input(request_id="source-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
