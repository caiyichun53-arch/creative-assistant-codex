from __future__ import annotations

import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    TACTIC_EXTRACT_CONTRACT_PATH,
    DeterministicTacticExtractModelPort,
    FormalSkillContract,
    FormalSkillValidationError,
    load_tactic_extract_business_contract,
    load_tactic_extract_fixtures,
    make_tactic_extract_harness,
    sample_tactic_extract_input,
    validate_payload,
    validate_tactic_extract_business_contract,
    validate_tactic_extract_output_semantics,
)


class TacticExtractHarnessMixin:
    def make_tactic_harness(self, **kwargs):
        harness = make_tactic_extract_harness(**kwargs)
        self.addCleanup(harness.close)
        return harness


class TacticExtractBusinessContractTests(unittest.TestCase):
    def test_business_contract_has_required_sections_and_no_missing_requirements(self) -> None:
        result = validate_tactic_extract_business_contract(load_tactic_extract_business_contract())
        self.assertEqual(result["skill_id"], "tactic_extract")
        self.assertEqual(result["skill_version"], "1.0.0")
        self.assertEqual(result["missing_requirement_count"], 0)

    def test_contract_loads_reverse_pattern_reduce_route(self) -> None:
        contract = FormalSkillContract.from_yaml(TACTIC_EXTRACT_CONTRACT_PATH)
        contract.validate_contract()
        self.assertEqual(contract.formal_skill_id, "tactic_extract")
        self.assertEqual(contract.route_name, "business.reverse_pattern_reduce")
        self.assertEqual(contract.allowed_model_nodes, ("business.reverse_pattern_reduce",))

    def test_formal_input_schema_accepts_only_public_binding_shape(self) -> None:
        contract = FormalSkillContract.from_yaml(TACTIC_EXTRACT_CONTRACT_PATH)
        validate_payload(sample_tactic_extract_input(), contract.input_schema)
        for key in (
            "request_id",
            "correlation_id",
            "analysis_batch_id",
            "dna_note_refs",
            "domain_label",
            "schema_version",
        ):
            with self.subTest(missing=key):
                invalid = sample_tactic_extract_input()
                invalid.pop(key)
                with self.assertRaises(FormalSkillValidationError):
                    validate_payload(invalid, contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_tactic_extract_input(database_connection="forbidden"), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_tactic_extract_input(dna_note_refs=["only-one"]), contract.input_schema)

    def test_dna_note_refs_max_is_20_not_12(self) -> None:
        # 2026-07-11: raised 12->20 by explicit user decision (see
        # HANDOFF_STATE.md) so a real 20-hit deviation-ranked batch can be
        # reduced in one call. Locks in both edges of the new boundary.
        contract = FormalSkillContract.from_yaml(TACTIC_EXTRACT_CONTRACT_PATH)
        validate_payload(sample_tactic_extract_input(dna_note_refs=[f"note-{i}" for i in range(20)]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_tactic_extract_input(dna_note_refs=[f"note-{i}" for i in range(21)]), contract.input_schema)

    def test_dna_note_ref_max_length_is_450_not_160(self) -> None:
        # 2026-07-11: raised 160->320->450 (second pass after real data showed
        # a fixed per-field split was silently truncating content -- see
        # run_tactic_extract.py's NOTE_MAX_CHARS comment). Locks in both edges.
        contract = FormalSkillContract.from_yaml(TACTIC_EXTRACT_CONTRACT_PATH)
        validate_payload(sample_tactic_extract_input(dna_note_refs=["字" * 450, "字" * 450]), contract.input_schema)
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(sample_tactic_extract_input(dna_note_refs=["字" * 451, "字" * 2]), contract.input_schema)


class TacticExtractFixtureTests(TacticExtractHarnessMixin, unittest.TestCase):
    def test_required_fixture_matrix_runs_through_adapter(self) -> None:
        fixtures = load_tactic_extract_fixtures()
        fixture_ids = {fixture["fixture_id"] for fixture in fixtures}
        self.assertTrue({"fan_kepu_social_life", "music_entertainment", "third_domain_neutral"}.issubset(fixture_ids))
        for fixture in fixtures:
            with self.subTest(fixture=fixture["fixture_id"]):
                harness = self.make_tactic_harness()
                result = harness.adapter.run(fixture["input"])
                validate_tactic_extract_output_semantics(fixture["input"], result.output_payload)
                self.assertEqual(result.model_route, "business.reverse_pattern_reduce")

    def test_model_output_failures_are_closed(self) -> None:
        for behavior in ("empty", "not_json", "missing_field", "illegal_writeback"):
            with self.subTest(behavior=behavior):
                harness = self.make_tactic_harness(provider=DeterministicTacticExtractModelPort(behavior=behavior))
                with self.assertRaises(FormalSkillValidationError):
                    harness.adapter.run(sample_tactic_extract_input(request_id=f"tactic-case-{behavior}"))


class TacticExtractRuntimeTests(TacticExtractHarnessMixin, unittest.TestCase):
    def test_e2e_success_materializes_result_and_outbox_once(self) -> None:
        harness = self.make_tactic_harness()
        created = harness.api.create_formal_skill_job(sample_tactic_extract_input())
        step = harness.worker.run_once()
        result = harness.api.get_result(created.job_id)
        outbox = harness.api.list_outbox()
        self.assertEqual(step.status, "succeeded")
        self.assertIsNotNone(result)
        self.assertEqual(result["model_route"], "business.reverse_pattern_reduce")
        self.assertEqual(result["output"]["schema_version"], "tactic_extract.output.v1")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(harness.provider.call_count, 1)

    def test_provider_failure_retries_then_succeeds_without_duplicate_outbox(self) -> None:
        harness = self.make_tactic_harness(provider=DeterministicTacticExtractModelPort(behavior="fail_once"))
        created = harness.api.create_formal_skill_job(
            sample_tactic_extract_input(request_id="tactic-retry-once"),
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
        harness = self.make_tactic_harness()
        payload = sample_tactic_extract_input(request_id="tactic-idempotent-case")
        first = harness.api.create_formal_skill_job(payload)
        second = harness.api.create_formal_skill_job(payload)
        self.assertEqual(first.job_id, second.job_id)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(harness.worker.run_once().status, "succeeded")
        self.assertEqual(len(harness.api.list_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
