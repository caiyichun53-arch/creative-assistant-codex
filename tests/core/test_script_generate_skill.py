from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    SCRIPT_GENERATE_CONTRACT_PATH,
    DeterministicScriptGenerateModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    apply_binding,
    load_script_generate_business_contract,
    load_script_generate_fixtures,
    make_script_generate_harness,
    preprocess_formal_skill_input,
    sample_script_generate_input,
    validate_payload,
    validate_script_generate_business_contract,
    validate_script_generate_output_semantics,
)


class RenderedPromptReachesModelTests(unittest.TestCase):
    """Regression for the real 2026-07-11 bug: selected_hook/evidence_items/
    research_summary were required in the public input_schema (a real caller
    must supply them) but silently dropped before ever reaching the model --
    only outline/brief made it into model_input_schema/input_map. Proves the
    actual rendered prompt text (what the real model receives) contains
    these fields now, not just that the schema declares them."""

    def test_selected_hook_evidence_and_research_summary_appear_in_rendered_prompt(self) -> None:
        contract = FormalSkillContract.from_yaml(SCRIPT_GENERATE_CONTRACT_PATH)
        input_payload = sample_script_generate_input()
        preprocessed = preprocess_formal_skill_input(contract.formal_skill_id, input_payload)
        model_input = apply_binding(contract.input_map, input_payload, {}, preprocessed)
        validate_payload(model_input, contract.model_input_schema)
        prompt = contract.portable_skill().render_prompt(model_input)

        self.assertIn(input_payload["selected_hook"], prompt)
        self.assertIn(input_payload["research_summary"], prompt)
        self.assertIn(input_payload["evidence_items"][0]["claim"], prompt)


class ScriptGenerateHarnessMixin:
    def make_script_harness(self, **kwargs):
        harness = make_script_generate_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class ScriptGenerateBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_script_generate_business_contract(load_script_generate_business_contract())
        self.assertEqual(result["skill_id"], "script_generate")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_creation_draft_route(self) -> None:
        contract = FormalSkillContract.from_yaml(SCRIPT_GENERATE_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "script_generate")
        self.assertEqual(contract.route_name, "business.creation_draft")
        self.assertEqual(contract.allowed_model_nodes, ("business.creation_draft",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(SCRIPT_GENERATE_CONTRACT_PATH)
        validate_payload(sample_script_generate_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "brief",
            "selected_hook",
            "beats",
            "research_summary",
            "evidence_items",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_script_generate_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_generate_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_generate_input(beats=[]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_generate_input(evidence_items=[]), contract.input_schema)


class ScriptGenerateFixtureTests(ScriptGenerateHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_script_generate_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_script_harness()
                result = harness.adapter.run(fixture["input"])
                validate_script_generate_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.creation_draft")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field", "empty_draft"):
            with self.subTest(behavior=behavior):
                harness = self.make_script_harness(provider=DeterministicScriptGenerateModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_script_generate_input(request_id=f"script-case-{behavior}"))


class ScriptGenerateRuntimeTests(ScriptGenerateHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_script_harness()
        created = harness.api.create_formal_skill_job(sample_script_generate_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.creation_draft")
        self.assertEqual(result["output"]["schema_version"], "script_generate.output.v1")
        self.assertGreaterEqual(len(result["output"]["draft_text"]), 50)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_script_harness(provider=DeterministicScriptGenerateModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_script_generate_input(request_id="script-retry-once"),
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
        harness = self.make_script_harness()
        payload = sample_script_generate_input(request_id="script-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
