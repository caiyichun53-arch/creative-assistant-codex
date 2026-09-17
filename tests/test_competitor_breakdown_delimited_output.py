from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.core.model_gateway.formal_skill_adapter import (
    validate_external_skill_output,
    FormalSkillContract,
    FormalSkillValidationError,
    parse_competitor_breakdown_delimited_output,
    prepare_external_skill_task,
    validate_competitor_breakdown_question_expansion_output,
)


def _input(source_id: str) -> dict:
    return {
        "correlation_id": "isolated-correlation",
        "source_id": source_id,
        "transcript": "原文材料。",
        "metrics": {},
        "comments": [],
        "domain_label": "隔离测试领域",
        "domain_context": {"content_type_lifecycle": "discover"},
        "schema_version": "competitor_breakdown.input.v1",
    }


def _response(
    source_type: str,
    analysis_text: str,
    *,
    matched_type: str | None = None,
    include_blocks: bool = False,
    boundary_observation: str | None = None,
) -> str:
    lines = [
        f"SOURCE_CONTENT_TYPE: {source_type}",
    ]
    if matched_type is not None:
        lines.append(f"MATCHED_SOURCE_CONTENT_TYPE: {matched_type}")
    lines.extend(["---ANALYSIS---", analysis_text])
    if include_blocks:
        lines.extend(
            [
                "---QUESTION Q1---",
                "这个问题包含中文引号“观察”和英文双引号 \"quoted\"。",
                "---SIGNAL S1---",
                "评论者提到“某事实”，需要继续核实。\n"
                "这段 signal 也包含 Markdown **强调**。",
                "---LEAD L1---",
                "SIGNAL: S1",
                "TYPE: case/explanation",
                "这个事实背景如何核实？\n可由公开材料独立成篇。",
            ]
        )
    if boundary_observation is not None:
        lines.extend(["---BOUNDARY---", boundary_observation])
    return "\n".join(lines) + "\n"


def _core_analysis() -> str:
    return (
        "这条内容围绕输入材料中的核心对象和命题展开，"
        "只说明材料中实际出现的推进动作及其前后关系；"
        "没有足够材料支持的参考保持克制。"
    )


class CompetitorBreakdownDelimitedOutputTest(unittest.TestCase):
    def test_long_prose_and_text_blocks_are_structurally_safe(self) -> None:
        analysis = (
            "正文包含中文引号“张三”和英文双引号 \"quoted\"，"
            "以及反斜杠 \\.；内容按材料顺序完成交付。\n"
        ) * 85
        self.assertGreaterEqual(len(analysis), 4000)
        self.assertLessEqual(len(analysis), 6000)
        parsed = parse_competitor_breakdown_delimited_output(
            _response("case/explanation", analysis, include_blocks=True)
        )
        self.assertEqual(parsed["analysis_text"].rstrip("\r\n"), analysis.rstrip("\r\n"))
        self.assertEqual(parsed["source_content_type"], "case/explanation")
        self.assertEqual(parsed["question_expansions"], [{
            "content_type": "case/explanation",
            "core_question": "这个问题包含中文引号“观察”和英文双引号 \"quoted\"。",
            "reason": "这个问题包含中文引号“观察”和英文双引号 \"quoted\"。",
        }])
        self.assertEqual(parsed["expansion_signals"], [{
            "signal_id": "S1",
            "signal_kind": "observation",
            "signal_text": "评论者提到“某事实”，需要继续核实。\n这段 signal 也包含 Markdown **强调**。",
            "source_anchor": "",
            "reason": "",
        }])
        self.assertEqual(parsed["typed_expansion_leads"], [{
            "signal_id": "S1",
            "canonical_id": "case/explanation",
            "core_question": "这个事实背景如何核实？\n可由公开材料独立成篇。",
            "reason": "这个事实背景如何核实？\n可由公开材料独立成篇。",
        }])

    def test_old_single_json_envelope_and_trailing_comma_are_not_supported(self) -> None:
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                '{\"source_content_type\":\"case/explanation\",\"analysis_text\":\"正文\"}'
            )
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                'SOURCE_CONTENT_TYPE: case/explanation\n{\"field\": 1,}\n---ANALYSIS---\n正文'
            )

    def test_headers_and_delimiter_are_strict(self) -> None:
        valid = _response("case/explanation", "正文")
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output("说明文字\n" + valid)
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(valid + "---ANALYSIS---\n尾注")
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                valid.replace("---ANALYSIS---", "---ANALYSIS---\n---UNKNOWN X---")
            )
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                "SOURCE_CONTENT_TYPE: case/explanation\n---ANALYSIS---\n"
            )

    def test_observed_and_matched_content_types_are_separate(self) -> None:
        parsed = parse_competitor_breakdown_delimited_output(
            _response("作品背景说明", "内容分析正文", matched_type="work_context_story")
        )
        self.assertEqual(parsed["source_content_type"], "作品背景说明")
        self.assertEqual(parsed["matched_source_content_type"], "work_context_story")

    def test_blocks_need_only_their_real_machine_relationships(self) -> None:
        raw = (
            "SOURCE_CONTENT_TYPE: case/explanation\n"
            "---ANALYSIS---\n分析正文\n"
            "---QUESTION Q1---\n这是直接的问题正文，含有 \"quotes\"。\n"
            "---QUESTION Q2---\n第二个问题。\n"
            "---SIGNAL S1---\nsignal 正文 **不需要标签**。\n"
            "---SIGNAL S2---\n另一个 signal。\n"
            "---LEAD L1---\nSIGNAL: S1\nTYPE: case/explanation\n"
            "这是直接的 lead 正文。\n"
        )
        parsed = parse_competitor_breakdown_delimited_output(raw)
        self.assertEqual(len(parsed["question_expansions"]), 2)
        self.assertEqual(len(parsed["expansion_signals"]), 2)
        self.assertEqual(len(parsed["typed_expansion_leads"]), 1)
        self.assertEqual(parsed["expansion_signals"][0]["signal_text"], "signal 正文 **不需要标签**。")

    def test_optional_boundary_block_is_plain_text(self) -> None:
        observation = "这条样本说明具体生活问题可以进入生产边界。\n允许 Markdown **正文** 和引号“原样保留”。"
        parsed = parse_competitor_breakdown_delimited_output(
            _response("case/explanation", "分析正文", boundary_observation=observation)
        )
        self.assertEqual(parsed["boundary_observation"], observation)
        self.assertNotIn("analysis_text", parsed["boundary_observation"])

    def test_boundary_block_is_optional_but_cannot_be_empty_or_repeated(self) -> None:
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                _response("case/explanation", "正文") + "---BOUNDARY---\n"
            )
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                _response("case/explanation", "正文", boundary_observation="一条观察")
                + "---BOUNDARY---\n另一条观察\n"
            )

    def test_lead_machine_reference_lines_are_required_but_body_labels_are_not(self) -> None:
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                "SOURCE_CONTENT_TYPE: case/explanation\n---ANALYSIS---\n正文\n"
                "---LEAD L1---\nTYPE: case/explanation\nlead 正文"
            )
        with self.assertRaises(FormalSkillValidationError):
            parse_competitor_breakdown_delimited_output(
                "SOURCE_CONTENT_TYPE: case/explanation\n---ANALYSIS---\n正文\n"
                "---LEAD L1---\nSIGNAL: S1\nlead 正文"
            )
        parsed = parse_competitor_breakdown_delimited_output(
            "SOURCE_CONTENT_TYPE: case/explanation\n---ANALYSIS---\n正文\n"
            "---QUESTION Q1---\nQUESTION: 这只是正文中的普通文字，不是必填标签。"
        )
        self.assertIn("QUESTION:", parsed["question_expansions"][0]["core_question"])


    def test_external_result_preserves_prose_and_rejects_retired_fields(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        source = _input("external-result")
        body = "内容围绕一个对象展开，先交付背景，再说明它在本条内容中的意义。"
        result = {
            "source_id": "external-result",
            "source_content_type": "case/explanation",
            "analysis_text": body,
            "schema_version": "competitor_breakdown.output.raw.v5",
        }
        self.assertEqual(validate_external_skill_output(contract, source, result), result)
        for field, value in (("question_expansions", []), ("boundary_observation", "旧边界提案")):
            with self.subTest(field=field), self.assertRaises(FormalSkillValidationError):
                validate_external_skill_output(contract, source, {**result, field: value})

    def test_analysis_does_not_require_named_sections(self) -> None:
        base = {
            "source_id": "free-form-analysis",
            "source_content_type": "作品背景说明",
            "schema_version": "competitor_breakdown.output.raw.v5",
        }
        validate_competitor_breakdown_question_expansion_output(
            _input("free-form-analysis"),
            {
                **base,
                "analysis_text": "内容先交付背景，再把背景与作品本身联系起来；有限参考是先建立关系再解释意义。",
            },
        )

    def test_five_sample_inputs_keep_analysis_as_plain_text(self) -> None:
        sample_ids = [
            "7642904416114330915",
            "7642925833614675251",
            "7643365530891586866",
            "7644519966519086377",
            "7655650817290145067",
        ]
        for source_id in sample_ids:
            with self.subTest(source_id=source_id):
                analysis = (
                    f"样本 {source_id} 的完整分析，包含中文引号“观察”与英文引号\"引用\"，"
                    "并说明承诺、推进、交付和有限参考。"
                )
                parsed = parse_competitor_breakdown_delimited_output(
                    _response("case/explanation", analysis)
                )
                self.assertEqual(parsed["analysis_text"].rstrip("\r\n"), analysis.rstrip("\r\n"))
                self.assertEqual(parsed["source_content_type"], "case/explanation")

    def test_simple_analysis_can_omit_optional_sections_and_use_unknown(self) -> None:
        validate_competitor_breakdown_question_expansion_output(
            _input("simple-core"),
            {
                "source_id": "simple-core",
                "source_content_type": "case/explanation",
                "analysis_text": (
                    "材料只支持识别一个简单对象和命题；只显示一次直接交付，"
                    "没有可可靠拆出的额外结构；有限参考为 unknown。"
                ),
                "schema_version": "competitor_breakdown.output.raw.v5",
            },
        )

    def test_explicit_evidence_reference_must_exist_in_supplied_material(self) -> None:
        valid = _core_analysis() + "\n补充说明：这一观察依据 P001。"
        validate_competitor_breakdown_question_expansion_output(
            _input("evidence-contract"),
            {
                "source_id": "evidence-contract",
                "source_content_type": "case/explanation",
                "analysis_text": valid,
                "schema_version": "competitor_breakdown.output.raw.v5",
            },
        )
        with self.assertRaises(FormalSkillValidationError):
            validate_competitor_breakdown_question_expansion_output(
                _input("evidence-contract"),
                {
                    "source_id": "evidence-contract",
                    "source_content_type": "case/explanation",
                    "analysis_text": _core_analysis() + "\n补充说明：这一观察依据 P999。",
                    "schema_version": "competitor_breakdown.output.raw.v5",
                },
            )

    def test_model_input_exposes_type_reference_without_domain_governance(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        task, _ = prepare_external_skill_task(
            contract,
            {
                **_input("type-reference"),
                "domain_context": {
                    "description": "不得把领域边界交给模型",
                    "allowed_scope": "不得把允许范围交给模型",
                    "excluded_terms": ["excluded"],
                    "content_type_lifecycle": "classify",
                    "content_type_registry": {
                        "status": "FROZEN",
                        "types": [{
                            "canonical_id": "work_context_story",
                            "name": "作品背景故事",
                            "core_subject": "作品及其背景",
                            "content_promise": "增加作品理解",
                            "required_delivery": "背景关系",
                            "scope_boundary": "不作为领域边界输入",
                        }],
                    },
                    "observed_content_types": ["背景说明"],
                },
            },
            constraints={"use_only_supplied_material": True},
        )
        rendered = task["skill"]["rendered_instructions"]
        self.assertIn("work_context_story", rendered)
        self.assertIn("背景说明", rendered)
        self.assertNotIn("numbered_comments", task["input"])
        self.assertNotIn("把评论粘贴在这里", rendered)
        self.assertNotIn("content_type_lifecycle", rendered)
        self.assertNotIn("FROZEN", rendered)
        self.assertNotIn("excluded_terms", rendered)
        self.assertNotIn("不得把领域边界交给模型", rendered)


if __name__ == "__main__":
    unittest.main()
