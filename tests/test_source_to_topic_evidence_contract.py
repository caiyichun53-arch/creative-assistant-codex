import json
import unittest

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillValidationError,
    FormalSkillContract,
    normalize_formal_skill_model_output,
    parse_model_json,
    validate_payload,
    validate_source_to_topic_output_semantics,
)


class SourceToTopicEvidenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input_payload = {
            "source_evidence_items": [
                "一条正式来源标题",
                "https://www.douyin.com/video/7657918628658007348",
                "嗨椰教育-椰子老师",
            ]
        }

    def test_live_prompt_requires_exact_evidence_copy(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("source_to_topic")

        self.assertIn("source_evidence_refs", contract.prompt_template)
        self.assertIn("character-for-character", contract.prompt_template)
        self.assertIn("来源账号：", contract.prompt_template)
        self.assertIn("do not invent or return a source ID", contract.prompt_template)
        self.assertIn("Do not wrap it in a Markdown code fence", contract.prompt_template)

    def test_legal_evidence_passes_and_stays_exact(self) -> None:
        output = {
            "topic_status": "no_result",
            "supporting_evidence": ["嗨椰教育-椰子老师"],
        }

        validate_source_to_topic_output_semantics(self.input_payload, output)

        self.assertEqual(output["supporting_evidence"], ["嗨椰教育-椰子老师"])

    def test_prefixed_evidence_is_rejected(self) -> None:
        output = {
            "topic_status": "no_result",
            "supporting_evidence": ["来源账号：嗨椰教育-椰子老师"],
        }

        with self.assertRaisesRegex(
            FormalSkillValidationError,
            "candidate evidence must come from supplied source material",
        ):
            validate_source_to_topic_output_semantics(self.input_payload, output)

    def test_generated_source_id_is_rejected(self) -> None:
        output = {
            "topic_status": "no_result",
            "supporting_evidence": ["discovery_source_not_supplied"],
        }

        with self.assertRaises(FormalSkillValidationError):
            validate_source_to_topic_output_semantics(self.input_payload, output)

    def test_parser_and_normalizer_do_not_change_legal_identity(self) -> None:
        evidence = "https://www.douyin.com/video/7657918628658007348"
        parsed = parse_model_json(
            json.dumps({"supporting_evidence": [evidence]}, ensure_ascii=False)
        )
        normalized = normalize_formal_skill_model_output("source_to_topic", parsed)

        self.assertEqual(parsed["supporting_evidence"], [evidence])
        self.assertEqual(normalized["supporting_evidence"], [evidence])
        self.assertIsNot(normalized, parsed)

    def test_plain_json_is_accepted(self) -> None:
        self.assertEqual(parse_model_json('{"key":"value"}'), {"key": "value"})

    def test_one_json_code_fence_is_accepted(self) -> None:
        self.assertEqual(
            parse_model_json("```json\n{\"key\":\"value\"}\n```"),
            {"key": "value"},
        )

    def test_one_unlabelled_code_fence_is_accepted(self) -> None:
        self.assertEqual(
            parse_model_json("```\n{\"key\":\"value\"}\n```"),
            {"key": "value"},
        )

    def test_text_outside_code_fence_is_rejected(self) -> None:
        for raw in (
            "这里是结果：\n{\"key\":\"value\"}",
            "```json\n{\"key\":\"value\"}\n```\n补充说明",
        ):
            with self.subTest(raw=raw), self.assertRaises(FormalSkillValidationError):
                parse_model_json(raw)

    def test_invalid_json_body_is_rejected(self) -> None:
        with self.assertRaises(FormalSkillValidationError):
            parse_model_json("```json\n{\"key\":}\n```")

    def test_multiple_code_blocks_are_rejected(self) -> None:
        raw = "```json\n{\"key\":\"one\"}\n```\n```json\n{\"key\":\"two\"}\n```"
        with self.assertRaises(FormalSkillValidationError):
            parse_model_json(raw)

    def test_schema_validation_still_rejects_missing_required_fields(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("source_to_topic")
        parsed = parse_model_json('{"topic_status":"generated"}')
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(parsed, contract.model_output_schema)


if __name__ == "__main__":
    unittest.main()
