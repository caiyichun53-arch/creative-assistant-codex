"""Regression checks for the real cold-start closure repair."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillValidationError,
    parse_model_json,
    validate_competitor_breakdown_question_expansion_output,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class BreakdownTransportRepairTest(unittest.TestCase):
    def test_normalizes_only_string_newlines_and_first_balanced_object(self) -> None:
        raw = (
            "delivery prefix\n"
            '{"source_content_type":"case/explanation",'
            '"analysis_text":"第一行\n第二行含有 {括号}",'
            '"expansion_signals":[]}\n'
            "```\n这段解释必须被丢弃"
        )
        with self.assertRaises(FormalSkillValidationError):
            parse_model_json(raw)

        parsed = parse_model_json(raw, normalize_transport=True)

        self.assertEqual(parsed["analysis_text"], "第一行\n第二行含有 {括号}")
        self.assertEqual(set(parsed), {
            "source_content_type", "analysis_text", "expansion_signals"
        })

    def test_incomplete_object_is_not_guessed(self) -> None:
        with self.assertRaises(FormalSkillValidationError):
            parse_model_json(
                '{"source_content_type":"case","analysis_text":"未结束\n',
                normalize_transport=True,
            )

    @staticmethod
    def output() -> dict:
        return {
            "source_id": "source-1",
            "source_content_type": "case/explanation",
            "analysis_text": "材料对象和命题以输入为准；材料中实际推进形成一个可核实观察。",
            "schema_version": "competitor_breakdown.output.raw.v5",
            "matched_source_content_type": "NO_MATCH",
        }

    @staticmethod
    def legacy_expansion_output() -> dict:
        return {
            **BreakdownTransportRepairTest.output(),
            "expansion_signals": [{
                "signal_id": "signal-1",
                "signal_kind": "source_observation",
                "signal_text": "原材料提出一个可继续核实的问题",
                "source_anchor": "原文",
                "reason": "该问题来自原材料",
            }],
            "typed_expansion_leads": [{
                "signal_id": "signal-1",
                "canonical_id": "case/explanation",
                "core_question": "这个现象在什么条件下会发生？",
                "reason": "沿用原材料问题继续核实",
            }],
        }

    def test_v5_analysis_does_not_require_a_domain_boundary(self) -> None:
        validate_competitor_breakdown_question_expansion_output(
            {
                "source_id": "source-1",
                "transcript": "原文只陈述当前现象。",
                "comments": [],
                "domain_context": {"content_type_lifecycle": "discover"},
            },
            self.output(),
        )

    def test_v5_classification_does_not_apply_domain_boundary_rules(self) -> None:
        validate_competitor_breakdown_question_expansion_output(
            {
                "source_id": "source-1",
                "transcript": "原文只陈述当前现象。",
                "comments": [],
                "domain_context": {
                    "content_type_lifecycle": "classify",
                    "content_type_registry": {
                        "status": "frozen",
                        "types": [{"canonical_id": "case/explanation"}],
                    },
                },
            },
            self.output(),
        )


class DiscoverySignalPersistenceBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()

    def tearDown(self) -> None:
        self.core.close()

    def test_discovery_signals_are_observed_without_formal_candidate_write(self) -> None:
        breakdown = BreakdownTransportRepairTest.legacy_expansion_output()
        with patch(
            "scripts.core.production.stage0_content_core.project_content_type",
            return_value={
                "status": "observed",
                "canonical_id": "case/explanation",
            },
        ):
            result = self.core.register_breakdown_question_expansions(
                domain_label="isolated-domain",
                breakdown=breakdown,
                parent_source_ref={
                    "source_type": "competitor_breakdown",
                    "source_object_id": "source-1",
                },
                content_type_lifecycle="discover",
            )

        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["signals"]), 1)
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage1_question_expansion_source"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
