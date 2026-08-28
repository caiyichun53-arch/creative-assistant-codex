from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalBusinessSkillAdapter,
    FormalSkillContract,
    FormalSkillValidationError,
    parse_competitor_breakdown_delimited_output,
)


class _FakeGateway:
    routes = {"stage0.competitor_registration_analysis": object()}

    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            output_text=self.output_text,
            envelope_version_id="isolated-envelope",
            envelope=SimpleNamespace(
                metadata={},
                usage=SimpleNamespace(completion_tokens=1),
                duration_ms=1,
            ),
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
    include_blocks: bool = False,
    boundary_observation: str | None = None,
) -> str:
    lines = [
        f"SOURCE_CONTENT_TYPE: {source_type}",
        "---ANALYSIS---",
        analysis_text,
    ]
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


class CompetitorBreakdownDelimitedOutputTest(unittest.TestCase):
    def test_long_prose_and_text_blocks_are_structurally_safe(self) -> None:
        analysis = (
            "一、内容类型\n正文包含中文引号“张三”和英文双引号 \"quoted\"，"
            "以及反斜杠 \\.\n"
            "二、推进与兑现\n- 第一段\n- 第二段\n"
            "六、候选复用原则与边界\n无明显短板。\n"
        ) * 55
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

    def test_adapter_preserves_formal_object_and_sends_no_response_format(self) -> None:
        body = "一、内容类型\n二、推进与兑现\n六、候选复用原则与边界\n无明显短板。"
        gateway = _FakeGateway(_response("case/explanation", body))
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        self.assertIsNone(contract.model_response_format)
        self.assertEqual(contract.model_output_schema["required"], ["source_content_type"])
        self.assertNotIn("source_content_type_id", contract.model_output_schema["properties"])
        self.assertNotIn("analysis_text", contract.model_output_schema["properties"])
        adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
        with patch(
            "scripts.core.production.business_runtime_guard.enforce_atomic_skill_runtime_guard"
        ):
            result = adapter.run(_input("real-sample-1"))

        self.assertIsNone(gateway.requests[0].response_format)
        self.assertEqual(
            set(result.output_payload),
            {
                "source_id",
                "source_content_type",
                "analysis_text",
                "schema_version",
            },
        )
        self.assertEqual(result.output_payload["source_id"], "real-sample-1")
        self.assertEqual(result.output_payload["analysis_text"].rstrip("\r\n"), body.rstrip("\r\n"))

    def test_adapter_maps_optional_text_blocks_without_repeated_labels(self) -> None:
        body = "一、内容类型\n二、推进与兑现\n六、候选复用原则与边界\n无明显短板。"
        gateway = _FakeGateway(_response("case/explanation", body, include_blocks=True))
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
        with patch(
            "scripts.core.production.business_runtime_guard.enforce_atomic_skill_runtime_guard"
        ):
            result = adapter.run(_input("real-sample-blocks"))

        self.assertEqual(result.output_payload["question_expansions"][0]["core_question"],
                         "这个问题包含中文引号“观察”和英文双引号 \"quoted\"。")
        self.assertEqual(result.output_payload["expansion_signals"][0]["signal_kind"], "observation")
        self.assertEqual(result.output_payload["typed_expansion_leads"][0]["signal_id"], "S1")

    def test_adapter_maps_boundary_observation_without_changing_formal_object(self) -> None:
        body = "一、内容类型\n二、推进与兑现\n六、候选复用原则与边界\n无明显短板。"
        gateway = _FakeGateway(
            _response(
                "case/explanation",
                body,
                boundary_observation="本条对可生产的问题性质提供了新的具体边界观察。",
            )
        )
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        adapter = FormalBusinessSkillAdapter(contract=contract, gateway=gateway)
        with patch(
            "scripts.core.production.business_runtime_guard.enforce_atomic_skill_runtime_guard"
        ):
            result = adapter.run(_input("real-sample-boundary"))
        self.assertEqual(
            result.output_payload["boundary_observation"],
            "本条对可生产的问题性质提供了新的具体边界观察。",
        )
        self.assertEqual(result.output_payload["analysis_text"].rstrip(), body)

    def test_five_failure_sample_inputs_use_the_same_new_boundary(self) -> None:
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
                    f"样本 {source_id} 的完整分析，包含中文引号“观察”与英文引号\"引用\"。\n"
                    "一、内容类型\n二、推进与兑现\n三、核心交付与增量\n"
                    "四、关键内容动作与类型特有机制\n五、评论信号\n"
                    "六、候选复用原则与边界\n无明显短板。"
                )
                parsed = parse_competitor_breakdown_delimited_output(
                    _response("case/explanation", analysis)
                )
                self.assertEqual(parsed["analysis_text"].rstrip("\r\n"), analysis.rstrip("\r\n"))
                self.assertEqual(parsed["source_content_type"], "case/explanation")


if __name__ == "__main__":
    unittest.main()
