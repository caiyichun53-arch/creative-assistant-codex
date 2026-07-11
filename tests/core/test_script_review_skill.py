from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    SCRIPT_REVIEW_CONTRACT_PATH,
    DeterministicScriptReviewModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_script_review_business_contract,
    load_script_review_fixtures,
    make_script_review_harness,
    sample_script_review_input,
    validate_payload,
    validate_script_review_business_contract,
    validate_script_review_output_semantics,
)


class ScriptReviewHarnessMixin:
    def make_review_harness(self, **kwargs):
        harness = make_script_review_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class _PromptRecordingScriptReviewPort(DeterministicScriptReviewModelPort):
    """Wraps the real deterministic port to also capture each subnode's
    actual rendered prompt string -- what a real model would receive."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompts_seen: list[str] = []

    def complete(self, request, route):
        self.prompts_seen.append(request.prompt)
        return super().complete(request, route)


class RenderedPromptsReachTheModelTests(ScriptReviewHarnessMixin, unittest.TestCase):
    """Regression for the real 2026-07-11 bug: all three script_review
    subnode prompts were a bare "Return only JSON with keys X, Y,
    schema_version" string -- none of them contained draft_text, brief,
    evidence_items, edit_notes, or human_reference_refs. A real model call
    would have had no way to know what script it was even reviewing. Proves
    the actual rendered prompts now carry the real content."""

    def test_all_three_subnode_prompts_contain_the_real_required_fields(self) -> None:
        provider = _PromptRecordingScriptReviewPort()
        harness = self.make_review_harness(provider=provider)
        input_payload = sample_script_review_input()
        created = harness.api.create_formal_skill_job(input_payload)
        harness.worker.run_once()

        self.assertEqual(len(provider.prompts_seen), 3)
        # 2026-07-13: polish now runs BEFORE review (置顶规则总表 条目3/30) --
        # order is polish, review, ai_flavor, not review, polish, ai_flavor.
        polish_prompt, review_prompt, ai_flavor_prompt = provider.prompts_seen

        self.assertIn(input_payload["draft_text"], polish_prompt)
        # polish no longer depends on review's findings -- it runs standalone
        # on the raw draft with its own quality criteria, not edit_notes.
        self.assertNotIn("Tighten the opening scene", polish_prompt)

        # review now inspects the POLISHED text (which, per the deterministic
        # fake provider, is the raw draft_text plus an appended suffix), not
        # the raw draft directly.
        self.assertIn(input_payload["draft_text"], review_prompt)
        self.assertIn("Polished pass", review_prompt)
        self.assertIn(input_payload["brief"], review_prompt)
        self.assertIn(input_payload["evidence_items"][0]["claim"], review_prompt)

        self.assertIn(input_payload["human_reference_refs"][0], ai_flavor_prompt)


class ScriptReviewBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_script_review_business_contract(load_script_review_business_contract())
        self.assertEqual(result["skill_id"], "script_review")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)
        self.assertEqual(result["subnode_count"], 3)

    def test_contract_loads_review_polish_ai_routes(self) -> None:
        contract = FormalSkillContract.from_yaml(SCRIPT_REVIEW_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "script_review")
        self.assertEqual(contract.route_name, "business.creation_review")
        self.assertEqual(
            contract.allowed_model_nodes,
            ("business.creation_review", "business.creation_polish", "business.ai_flavor_judge"),
        )

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(SCRIPT_REVIEW_CONTRACT_PATH)
        validate_payload(sample_script_review_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "draft_text",
            "brief",
            "evidence_items",
            "human_reference_refs",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_script_review_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_review_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_review_input(human_reference_refs=[]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_script_review_input(evidence_items=[]), contract.input_schema)


class ScriptReviewFixtureTests(ScriptReviewHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_script_review_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_review_harness()
                result = harness.adapter.run(fixture["input"])
                validate_script_review_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.creation_review")
                self.assertEqual(
                    harness.provider.routes_seen,
                    ["business.creation_polish", "business.creation_review", "business.ai_flavor_judge"],
                )

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in (
            "empty",
            "not_json",
            "missing_field",
            "polish_empty",
            "polish_not_json",
            "polish_missing_field",
            "empty_polished_text",
            "empty_revision_focus",
            "ai_empty",
            "ai_not_json",
            "ai_missing_field",
            "empty_revision_targets",
        ):
            with self.subTest(behavior=behavior):
                harness = self.make_review_harness(provider=DeterministicScriptReviewModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_script_review_input(request_id=f"review-case-{behavior}"))


class ScriptReviewRuntimeTests(ScriptReviewHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_review_harness()
        created = harness.api.create_formal_skill_job(sample_script_review_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.creation_review")
        self.assertEqual(result["output"]["schema_version"], "script_review.output.v1")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 3)
        self.assertEqual(
            harness.provider.routes_seen,
            ["business.creation_polish", "business.creation_review", "business.ai_flavor_judge"],
        )

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_review_harness(provider=DeterministicScriptReviewModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_script_review_input(request_id="review-retry-once"),
            max_attempts=2,
        )
        first = harness.worker.run_once()
        second = harness.worker.run_once()
        self.assertEqual(first.status, "failed")
        self.assertEqual(first.reason, "queued")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)
        self.assertEqual(harness.provider.call_count, 4)

    def test_job_enqueue_is_idempotent(self) -> None:
        harness = self.make_review_harness()
        payload = sample_script_review_input(request_id="review-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
