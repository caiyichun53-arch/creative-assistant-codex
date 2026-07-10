from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    CONTENT_PLAN_CONTRACT_PATH,
    DeterministicContentPlanModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_content_plan_business_contract,
    load_content_plan_fixtures,
    make_content_plan_harness,
    sample_content_plan_input,
    validate_content_plan_business_contract,
    validate_content_plan_output_semantics,
    validate_payload,
)


class ContentPlanHarnessMixin:
    def make_plan_harness(self, **kwargs):
        harness = make_content_plan_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class _PromptRecordingContentPlanPort(DeterministicContentPlanModelPort):
    """Wraps the real deterministic port to also capture each subnode's
    actual rendered prompt string -- what a real model would receive."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompts_seen: list[str] = []

    def complete(self, request, route):
        self.prompts_seen.append(request.prompt)
        return super().complete(request, route)


class ContentPlanBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_content_plan_business_contract(load_content_plan_business_contract())
        self.assertEqual(result["skill_id"], "content_plan")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)
        self.assertEqual(result["subnode_count"], 2)

    def test_contract_loads_hook_and_outline_routes(self) -> None:
        contract = FormalSkillContract.from_yaml(CONTENT_PLAN_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "content_plan")
        self.assertEqual(contract.route_name, "business.creation_outline")
        self.assertEqual(contract.allowed_model_nodes, ("business.creation_hook", "business.creation_outline"))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(CONTENT_PLAN_CONTRACT_PATH)
        validate_payload(sample_content_plan_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "candidate_topic",
            "brief",
            "evidence_items",
            "tactic_candidates",
            "style_examples",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_content_plan_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_plan_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_plan_input(style_examples=[]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_content_plan_input(evidence_items=[]), contract.input_schema)


class ContentPlanFixtureTests(ContentPlanHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_content_plan_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_plan_harness()
                result = harness.adapter.run(fixture["input"])
                validate_content_plan_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.creation_outline")
                self.assertEqual(harness.provider.routes_seen, ["business.creation_hook", "business.creation_outline"])

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in (
            "empty",
            "not_json",
            "missing_field",
            "empty_hooks",
            "outline_empty",
            "outline_not_json",
            "outline_missing_field",
            "empty_beats",
        ):
            with self.subTest(behavior=behavior):
                harness = self.make_plan_harness(provider=DeterministicContentPlanModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_content_plan_input(request_id=f"content-plan-case-{behavior}"))


class RenderedPromptsReachTheModelTests(ContentPlanHarnessMixin, unittest.TestCase):
    """Regression for the real 2026-07-11 bug: candidate_topic/evidence_items/
    tactic_candidates were required in the public input_schema (a real
    caller must supply them) but silently dropped before ever reaching
    either subnode's prompt -- content_plan bypasses the generic
    prompt_template/model_input_schema machinery entirely (see
    _run_content_plan()), so this can only be proven by capturing the
    actual rendered prompt strings sent to the model, not by inspecting the
    YAML contract."""

    def test_hook_and_outline_prompts_contain_the_real_required_fields(self) -> None:
        provider = _PromptRecordingContentPlanPort()
        harness = self.make_plan_harness(provider=provider)
        input_payload = sample_content_plan_input()
        created = harness.api.create_formal_skill_job(input_payload)
        harness.worker.run_once()

        self.assertEqual(len(provider.prompts_seen), 2)
        hook_prompt, outline_prompt = provider.prompts_seen

        self.assertIn(input_payload["candidate_topic"], hook_prompt)
        self.assertIn(input_payload["evidence_items"][0]["claim"], hook_prompt)
        self.assertIn(input_payload["tactic_candidates"][0], hook_prompt)

        self.assertIn(input_payload["evidence_items"][0]["claim"], outline_prompt)
        self.assertIn(input_payload["tactic_candidates"][0], outline_prompt)


class ContentPlanRuntimeTests(ContentPlanHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_plan_harness()
        created = harness.api.create_formal_skill_job(sample_content_plan_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.creation_outline")
        self.assertEqual(result["output"]["schema_version"], "content_plan.output.v1")
        self.assertEqual(result["output"]["selected_hook"], result["output"]["hooks"][0])
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 2)
        self.assertEqual(harness.provider.routes_seen, ["business.creation_hook", "business.creation_outline"])

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_plan_harness(provider=DeterministicContentPlanModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_content_plan_input(request_id="content-plan-retry-once"),
            max_attempts=2,
        )
        first = harness.worker.run_once()
        second = harness.worker.run_once()
        self.assertEqual(first.status, "failed")
        self.assertEqual(first.reason, "queued")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)
        self.assertEqual(harness.provider.call_count, 3)
        self.assertEqual(
            harness.provider.routes_seen,
            ["business.creation_hook", "business.creation_hook", "business.creation_outline"],
        )

    def test_job_enqueue_is_idempotent(self) -> None:
        harness = self.make_plan_harness()
        payload = sample_content_plan_input(request_id="content-plan-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
