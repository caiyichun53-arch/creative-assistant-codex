"""Small, non-production regression checks for the 2A type boundary."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.domain_labels import project_content_type
from scripts.core.model_gateway.formal_skill_adapter import FormalSkillContract
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import _breakdown_domain_context
from scripts.core.production.stage0_content_core import StateTransitionError


FIXTURE_REGISTRY = {
    "status": "FROZEN",
    "version": "fixture-1",
    "types": [
        {
            "canonical_id": "person_music_story",
            "name": "人物音乐故事",
            "core_subject": "音乐人物的音乐经历",
            "content_promise": "通过材料还原音乐经历和转折",
            "required_delivery": "有证据的叙事解释",
            "scope_boundary": "不承担泛生活或职业百科",
        },
        {
            "canonical_id": "work_context_story",
            "name": "作品背景故事",
            "core_subject": "音乐作品及其创作背景",
            "content_promise": "用背景材料增加对作品的理解",
            "required_delivery": "作品关系和背景的证据化讲述",
            "scope_boundary": "不替代演唱技术或乐理分析",
        },
        {
            "canonical_id": "music_event_context",
            "name": "音乐事件背景",
            "core_subject": "音乐事件或音乐文化现象",
            "content_promise": "解释事件的音乐语境和关系",
            "required_delivery": "事件事实、关系和有限解释",
            "scope_boundary": "不扩展为泛娱乐新闻",
        },
    ],
}


class ContentTypeLifecycle2ATest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        now = "2026-08-18T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("account_fixture", "competitor", "fixture", "music_entertainment", "fixture", "active", "test", "test", now),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cold_fixture", "account_fixture", "music_entertainment", "completed", "test", "test", now, None),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("registration_fixture", "cold_fixture", "account_fixture", "completed", "completed", "test", now, now),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'transcripts_and_comments', ?, 'completed', ?, '{}', 1, 'test', ?)",
            (
                "registration_fixture",
                "source_fixture",
                json.dumps({
                    "source_url": "https://example.test/source",
                    "transcript_ref": "fixture-transcript",
                    "comment_collection_ref": "fixture-comments",
                }),
                now,
            ),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'breakdown', ?, 'completed', ?, '{}', 1, 'test', ?)",
            (
                "registration_fixture",
                "source_fixture",
                json.dumps({"source_content_type": "人物故事"}),
                now,
            ),
        )
        self.parent = {
            "source_type": "competitor_breakdown",
            "source_object_id": "source_fixture",
            "registration_id": "registration_fixture",
        }

    def tearDown(self) -> None:
        self.core.close()

    def test_discover_keeps_raw_observation_without_approval(self) -> None:
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value={"status": "NOT_FROZEN", "version": "0", "types": []},
        ):
            result = project_content_type(
                "music_entertainment",
                lifecycle="discover",
                canonical_id="music_technical_analysis",
            )
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["canonical_id"], "music_technical_analysis")

    def test_music_production_classify_uses_the_frozen_registry(self) -> None:
        context = _breakdown_domain_context(
            "music_entertainment",
            content_type_lifecycle="classify",
        )
        self.assertEqual(context["content_type_registry"]["status"], "FROZEN")

    def test_classify_rejects_type_outside_fixture_registry(self) -> None:
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FIXTURE_REGISTRY,
        ):
            result = project_content_type(
                "music_entertainment",
                lifecycle="classify",
                canonical_id="music_technical_analysis",
            )
        self.assertEqual(result["status"], "out_of_scope")
        self.assertEqual(result["canonical_id"], "OUT_OF_SCOPE")

    def test_signal_is_preserved_but_unmatched_technical_lead_skips_qualification(self) -> None:
        breakdown = {
            "source_content_type": "work_context_story",
            "source_content_type_id": "work_context_story",
            "expansion_signals": [{
                "signal_id": "signal_01",
                "signal_kind": "comment_question",
                "signal_text": "评论追问几首歌为什么难唱，以及音域和技巧难点。",
                "source_anchor": "comment",
                "reason": "评论提出了母内容没有展开的具体问题。",
            }],
            "typed_expansion_leads": [{
                "signal_id": "signal_01",
                "canonical_id": "music_technical_analysis",
                "core_question": "这些作品为什么难唱，音域和技巧难点是什么？",
                "reason": "这是评论新增的独立问题，但需要先通过正式类型投影。",
            }],
        }
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FIXTURE_REGISTRY,
        ):
            result = self.core.register_breakdown_question_expansions(
                domain_label="music_entertainment",
                breakdown=breakdown,
                parent_source_ref=self.parent,
                content_type_lifecycle="classify",
            )
        self.assertEqual(result["signals"][0]["status"], "observed")
        self.assertEqual(result["no_match_count"], 1)
        self.assertEqual(result["count"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage1_question_expansion_qualification"
            ).fetchone()[0],
            0,
        )

    def test_projected_lead_uses_canonical_id_and_enters_qualification(self) -> None:
        breakdown = {
            "source_content_type": "person_music_story",
            "source_content_type_id": "person_music_story",
            "expansion_signals": [{
                "signal_id": "signal_01",
                "signal_kind": "comment_fact",
                "signal_text": "评论补充了一项待核实的音乐合作事实。",
                "source_anchor": "comment",
                "reason": "评论新增了母内容没有展开的关系。",
            }],
            "typed_expansion_leads": [{
                "signal_id": "signal_01",
                "canonical_id": "person_music_story",
                "core_question": "这项音乐合作事实是否存在，过程和关系是什么？",
                "reason": "问题来自评论补充，交付是独立的事实核查和人物音乐故事。",
            }],
        }
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FIXTURE_REGISTRY,
        ):
            result = self.core.register_breakdown_question_expansions(
                domain_label="music_entertainment",
                breakdown=breakdown,
                parent_source_ref=self.parent,
                content_type_lifecycle="classify",
            )
        self.assertEqual(result["count"], 1)
        row = self.connection.execute(
            "SELECT status, checks_json FROM stage1_question_expansion_qualification"
        ).fetchone()
        self.assertEqual(row[0], "qualified")
        self.assertIn("approved_type_projection", row[1])

    def test_projected_lead_qualification_keeps_qualified_rejected_unresolved(self) -> None:
        breakdown = {
            "source_content_type": "person_music_story",
            "source_content_type_id": "person_music_story",
            "expansion_signals": [
                {
                    "signal_id": "signal_ok",
                    "signal_kind": "comment_question",
                    "signal_text": "评论提出一个可独立核查的音乐经历问题。",
                    "source_anchor": "comment",
                    "reason": "评论追问母内容没有展开的事实。",
                },
                {
                    "signal_id": "signal_reject",
                    "signal_kind": "comment_fact",
                    "signal_text": "评论提出一项可能没有公开材料的说法。",
                    "source_anchor": "comment",
                    "reason": "评论新增待核实事实。",
                },
                {
                    "signal_id": "signal_unresolved",
                    "signal_kind": "comment_fact",
                    "signal_text": "评论提出一项暂时无法查询的说法。",
                    "source_anchor": "comment",
                    "reason": "评论新增待核实事实。",
                },
            ],
            "typed_expansion_leads": [
                {
                    "signal_id": "signal_ok",
                    "canonical_id": "person_music_story",
                    "core_question": "这段音乐经历的事实和过程是什么？",
                    "reason": "来源追问带来独立的事实交付。",
                },
                {
                    "signal_id": "signal_reject",
                    "canonical_id": "person_music_story",
                    "core_question": "reject 这项音乐经历说法是否确实存在？",
                    "reason": "评论新增待核实事实。",
                },
                {
                    "signal_id": "signal_unresolved",
                    "canonical_id": "person_music_story",
                    "core_question": "unresolved 这项音乐经历说法是否确实存在？",
                    "reason": "评论新增待核实事实。",
                },
            ],
        }

        def external_probe(question: str) -> dict[str, object]:
            if "reject" in question:
                return {"status": "rejected", "rejection_reason": "lead_no_reliable_public_material", "checks": {}}
            if "unresolved" in question:
                raise TimeoutError("fixture timeout")
            return {"status": "passed", "material_refs": [{"kind": "external_public_source", "ref": "fixture"}], "checks": {}}

        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FIXTURE_REGISTRY,
        ):
            result = self.core.register_breakdown_question_expansions(
                domain_label="music_entertainment",
                breakdown=breakdown,
                parent_source_ref=self.parent,
                content_type_lifecycle="classify",
                external_probe=external_probe,
            )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["unresolved_count"], 1)
        statuses = {
            row[0]
            for row in self.connection.execute(
                "SELECT status FROM stage1_question_expansion_qualification"
            ).fetchall()
        }
        self.assertEqual(statuses, {"qualified", "rejected", "unresolved"})

    def test_person_word_does_not_override_work_type(self) -> None:
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FIXTURE_REGISTRY,
        ):
            result = project_content_type(
                "music_entertainment",
                lifecycle="classify",
                canonical_id="work_context_story",
            )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["canonical_id"], "work_context_story")

    def test_runtime_skill_contract_exposes_only_v5_core_output_fields(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        self.assertEqual(contract.output_schema["properties"]["schema_version"]["enum"][-1], "competitor_breakdown.output.raw.v5")
        self.assertIn("question_expansions", contract.output_schema["properties"])
        for field in ("question_expansions", "expansion_signals", "typed_expansion_leads"):
            self.assertNotIn(field, contract.model_output_schema["properties"])

    def test_legacy_question_expansion_path_remains_readable(self) -> None:
        result = self.core.register_breakdown_question_expansions(
            domain_label="music_entertainment",
            breakdown={
                "source_content_type": "人物故事",
                "question_expansions": [{
                    "content_type": "人物故事",
                    "core_question": "这位音乐人的作品经历如何展开？",
                    "reason": "旧链路仍保留母来源绑定和独立问题。",
                }],
            },
            parent_source_ref=self.parent,
        )
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["created"], [])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage1_question_expansion_qualification"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
