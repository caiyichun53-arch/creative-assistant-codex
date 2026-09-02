"""2B frozen-registry and production projection regressions."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.core.business_data.domain_labels import get_content_type_registry, project_content_type
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    validate_competitor_breakdown_question_expansion_output,
    validate_external_skill_output,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from scripts.core.production.stage1_competitor_registration import (
    _breakdown_domain_context,
    submit_competitor_breakdown_external_result,
)


FROZEN_REGISTRY = {
    "status": "FROZEN",
    "version": "1",
    "types": [
        {"canonical_id": "music_collection_curation", "name": "音乐主题盘点/合集", "core_subject": "多个音乐对象", "content_promise": "围绕主题组织多个对象", "required_delivery": "主题、筛选逻辑、条目和收束", "scope_boundary": "不做技术分析或无逻辑罗列"},
        {"canonical_id": "person_music_story", "name": "人物音乐故事", "core_subject": "音乐人物", "content_promise": "讲清人物音乐经历和转折", "required_delivery": "人物经历、关键事件和推进", "scope_boundary": "不做泛生活传记或唱功分析"},
        {"canonical_id": "work_context_story", "name": "作品背景故事", "core_subject": "明确音乐作品", "content_promise": "讲清作品如何产生及其背景", "required_delivery": "作品、背景关系和材料支撑", "scope_boundary": "不做赏析、乐理或演唱技术分析"},
        {"canonical_id": "music_event_context", "name": "音乐事件/文化现象", "core_subject": "具体音乐事件或现象", "content_promise": "讲清事件发展及音乐语境", "required_delivery": "事件、参与者、过程和有限解释", "scope_boundary": "不做泛法律、商业、制度或平台分析"},
    ],
}


class ContentTypeLifecycle2BTest(unittest.TestCase):
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
                json.dumps({"source_content_type": "person_music_story"}),
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

    def _breakdown(self, source_type: str, canonical_id: str, *, question: str = "这项音乐经历的事实和过程是什么？", reason: str = "这是一个可以独立核实和成篇的问题") -> dict[str, object]:
        return {
            "source_content_type": source_type,
            "source_content_type_id": source_type,
            "expansion_signals": [{
                "signal_id": "signal_01",
                "signal_kind": "comment_fact",
                "signal_text": "评论补充了一项待核实的音乐事实",
                "source_anchor": "comment",
                "reason": "评论提出了母内容没有展开的新事实",
            }],
            "typed_expansion_leads": [{
                "signal_id": "signal_01",
                "canonical_id": canonical_id,
                "core_question": question,
                "reason": reason,
            }],
        }

    def _run(self, breakdown: dict[str, object], *, external_probe=None) -> dict[str, object]:
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FROZEN_REGISTRY,
        ):
            return self.core.register_breakdown_question_expansions(
                domain_label="music_entertainment",
                breakdown=breakdown,
                parent_source_ref=self.parent,
                content_type_lifecycle="classify",
                external_probe=external_probe,
            )

    @staticmethod
    def _external_input(lifecycle: str) -> dict[str, object]:
        return {
            "correlation_id": "external-receipt-test",
            "source_id": "source-1",
            "transcript": "一段用于外部结果接收边界验证的口播材料。",
            "metrics": {},
            "comments": [],
            "domain_label": "music_entertainment",
            "domain_context": {
                "content_type_lifecycle": lifecycle,
                "content_type_registry": FROZEN_REGISTRY,
            },
            "schema_version": "competitor_breakdown.input.v1",
        }

    @staticmethod
    def _external_output(source_type: str, *, source_id: str = "source-1") -> dict[str, object]:
        return {
            "source_id": source_id,
            "source_content_type": source_type,
            "analysis_text": (
                "WHAT\n核心对象和内容命题以输入材料为准。\n"
                "HOW\n只说明材料中实际出现的推进动作。\n"
                "SO WHAT\n无有效复用参考。"
            ),
            "schema_version": "competitor_breakdown.output.raw.v5",
        }

    def test_formal_music_registry_is_frozen_version_one_and_has_only_four_ids(self) -> None:
        registry = get_content_type_registry("music_entertainment")
        self.assertEqual(registry["status"], "FROZEN")
        self.assertEqual(registry["version"], "1")
        self.assertEqual(
            {item["canonical_id"] for item in registry["types"]},
            {
                "music_collection_curation",
                "person_music_story",
                "work_context_story",
                "music_event_context",
            },
        )
        self.assertTrue(all(item["status"] == "FROZEN" for item in registry["types"]))

    def test_registry_projection_accepts_only_the_four_approved_ids(self) -> None:
        with patch(
            "scripts.core.business_data.domain_labels.get_content_type_registry",
            return_value=FROZEN_REGISTRY,
        ):
            self.assertEqual(project_content_type("music_entertainment", lifecycle="classify", canonical_id="person_music_story")["status"], "matched")
            result = project_content_type("music_entertainment", lifecycle="classify", canonical_id="music_appreciation")
        self.assertEqual(result["canonical_id"], "OUT_OF_SCOPE")

    def test_classify_fails_closed_when_the_supplied_registry_is_not_frozen(self) -> None:
        with self.assertRaises(StateTransitionError):
            _breakdown_domain_context(
                "music_entertainment",
                observed_content_types=["music_technical_analysis"],
                content_type_lifecycle="classify",
                content_type_registry={"status": "NOT_FROZEN", "version": "0", "types": []},
            )

    def test_observed_market_labels_do_not_change_the_production_registry(self) -> None:
        context = _breakdown_domain_context(
            "music_entertainment",
            observed_content_types=["music_appreciation", "vocal_technique_analysis"],
            content_type_lifecycle="classify",
            content_type_registry=FROZEN_REGISTRY,
        )
        self.assertEqual(
            {item["canonical_id"] for item in context["content_type_registry"]["types"]},
            {item["canonical_id"] for item in FROZEN_REGISTRY["types"]},
        )

    def test_core_rejects_a_model_created_fifth_type(self) -> None:
        result = self._run(self._breakdown("music_technical_analysis", "music_technical_analysis"))
        self.assertEqual(result["no_match"][0]["status"], "OUT_OF_SCOPE")
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM stage1_question_expansion_qualification").fetchone()[0], 0)

    def test_cross_domain_legal_or_business_direction_is_out_of_scope(self) -> None:
        result = self._run(self._breakdown("music_event_context", "legal制度分析"))
        self.assertEqual(result["no_match"][0]["status"], "OUT_OF_SCOPE")
        self.assertEqual(result["count"], 0)

    def test_appreciation_vocal_and_theory_analysis_cannot_form_typed_leads(self) -> None:
        for unsupported in ("music_appreciation", "vocal_technique_analysis", "music_theory_technical_analysis"):
            result = self._run(self._breakdown("work_context_story", unsupported, question="这项技术分析的具体问题是什么？"))
            self.assertEqual(result["no_match"][0]["status"], "OUT_OF_SCOPE")
            self.assertEqual(result["count"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM stage1_question_expansion_qualification").fetchone()[0], 0)

    def test_four_approved_types_project_without_person_keyword_override(self) -> None:
        questions = {
            "music_collection_curation": "collection_question_unique",
            "person_music_story": "person_question_unique",
            "work_context_story": "work_question_unique",
            "music_event_context": "event_question_unique",
        }
        for canonical_id, question in questions.items():
            result = self._run(self._breakdown(canonical_id, canonical_id, question=question))
            self.assertEqual(result["count"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM stage1_question_expansion_qualification").fetchone()[0], 4)

    def test_signal_is_kept_and_only_successful_projection_enters_qualification(self) -> None:
        result = self._run(self._breakdown("person_music_story", "person_music_story"))
        self.assertEqual(result["signals"][0]["status"], "observed")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["created"][0]["canonical_id"], "person_music_story")

    def test_qualification_keeps_rejected_and_provider_failure_unresolved(self) -> None:
        def external_probe(question: str) -> dict[str, object]:
            if "拒绝" in question:
                return {"status": "rejected", "rejection_reason": "no_reliable_public_material", "checks": {}}
            raise TimeoutError("provider timeout")

        rejected = self._run(self._breakdown("person_music_story", "person_music_story", question="拒绝这项人物音乐经历是否存在？", reason="评论补充了一项待核实的事实"), external_probe=external_probe)
        unresolved = self._run(self._breakdown("person_music_story", "person_music_story", question="这项人物音乐经历是否存在且如何发展？", reason="评论补充了一项待核实的事实"), external_probe=external_probe)
        self.assertEqual(rejected["rejected_count"], 1)
        self.assertEqual(unresolved["unresolved_count"], 1)

    def test_old_question_expansions_cannot_bypass_frozen_projection(self) -> None:
        result = self._run({
            "source_content_type": "person_music_story",
            "question_expansions": [{
                "content_type": "person_music_story",
                "core_question": "旧自由问题是否可以直接进入资格化？",
                "reason": "这是旧数据格式，不代表已经完成批准类型投影。",
            }],
        })
        self.assertEqual(result["no_match"][0]["status"], "OUT_OF_SCOPE")
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM stage1_question_expansion_qualification").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM stage1_question_expansion_source").fetchone()[0], 0)

    def test_classify_rejects_natural_language_type_name_even_with_an_id(self) -> None:
        result = self._run(self._breakdown("人物音乐故事", "person_music_story"))
        self.assertEqual(result["no_match"][0]["status"], "OUT_OF_SCOPE")
        self.assertEqual(result["count"], 0)

    def test_model_output_cannot_use_a_new_type_or_mismatch_the_id(self) -> None:
        context = {
            "description": "music",
            "content_type_lifecycle": "classify",
            "content_type_registry": FROZEN_REGISTRY,
        }
        base = {
            "source_id": "source_fixture",
            "analysis_text": "WHAT\n核心对象和命题。\nHOW\n实际推进及其关系。\nSO WHAT\n无有效复用参考。",
            "schema_version": "competitor_breakdown.output.raw.v5",
        }
        with self.assertRaises(FormalSkillValidationError):
            validate_competitor_breakdown_question_expansion_output(
                {"source_id": "source_fixture", "domain_context": context},
                {**base, "source_content_type": "fifth_type"},
            )
        with self.assertRaises(FormalSkillValidationError):
            validate_competitor_breakdown_question_expansion_output(
                {"source_id": "source_fixture", "domain_context": context},
                {**base, "source_content_type": "person_music_story", "source_content_type_id": "work_context_story"},
            )

    def test_external_receipt_enforces_classify_type_without_optional_validation(self) -> None:
        contract = FormalSkillContract.from_runtime_skill("competitor_breakdown")
        for source_type in ("music_collection_curation", "NO_MATCH", "OUT_OF_SCOPE"):
            with self.subTest(source_type=source_type):
                validated = validate_external_skill_output(
                    contract,
                    self._external_input("classify"),
                    self._external_output(source_type),
                )
                self.assertEqual(validated["source_content_type"], source_type)

        with self.assertRaises(FormalSkillValidationError):
            validate_external_skill_output(
                contract,
                self._external_input("classify"),
                self._external_output("作品背景说明"),
            )

        validated = validate_external_skill_output(
            contract,
            self._external_input("discover"),
            self._external_output("作品背景说明"),
        )
        self.assertEqual(validated["source_content_type"], "作品背景说明")

        for field in ("question_expansions", "expansion_signals", "typed_expansion_leads"):
            with self.subTest(field=field):
                with self.assertRaises(FormalSkillValidationError):
                    validate_external_skill_output(
                        contract,
                        self._external_input("classify"),
                        {**self._external_output("person_music_story"), field: []},
                    )

    def test_v5_without_expansion_fields_saves_completed_breakdown_and_reports_not_present(self) -> None:
        source_id = "source-without-expansion"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "source.txt"
            transcript.write_text("用于无拓展输出保存测试的口播材料。", encoding="utf-8")
            self.connection.execute(
                "INSERT INTO stage0_competitor_registration_item "
                "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
                "VALUES (?, 'transcripts_and_comments', ?, 'completed', ?, '{}', 1, 'test', ?)",
                (
                    "registration_fixture",
                    source_id,
                    json.dumps({
                        "source_id": source_id,
                        "transcript_ref": str(transcript),
                        "metrics": {},
                        "comments": [],
                    }),
                    "2026-08-18T00:00:00+08:00",
                ),
            )
            self.connection.commit()
            with patch(
                "scripts.core.business_data.domain_labels.get_content_type_registry",
                return_value=FROZEN_REGISTRY,
            ):
                result = submit_competitor_breakdown_external_result(
                    self.core,
                    registration_id="registration_fixture",
                    source_id=source_id,
                    execution_id="execution-without-expansion",
                    executor_id="executor-test",
                    model_ref="test-model",
                    submitted_at="2026-08-18T00:00:00+08:00",
                    output=self._external_output("person_music_story", source_id=source_id),
                )

        self.assertEqual(result["status"], "completed")
        row = self.connection.execute(
            "SELECT status, artifact_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='breakdown' AND item_ref=?",
            ("registration_fixture", source_id),
        ).fetchone()
        self.assertEqual(row[0], "completed")
        artifact = json.loads(row[1])
        deep_breakdown = artifact["deep_breakdown"]
        self.assertNotIn("question_expansions", deep_breakdown)
        self.assertNotIn("expansion_signals", deep_breakdown)
        self.assertNotIn("typed_expansion_leads", deep_breakdown)
        self.assertEqual(
            artifact["optional_enhancements"]["result"],
            {"status": "not_present", "created": [], "count": 0},
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage1_question_expansion_source"
            ).fetchone()[0],
            0,
        )

    def test_invalid_classify_type_is_rejected_before_breakdown_artifact_save(self) -> None:
        source_id = "source-external-receipt"
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "source.txt"
            transcript.write_text("用于外部结果接收测试的口播材料。", encoding="utf-8")
            core = Mock()
            core.data_identity = "test"
            core.conn = Mock()
            core.get_competitor_registration.return_value = {
                "registration_id": "registration-external-receipt",
                "status": "completed",
                "domain_label": "music_entertainment",
            }
            core.list_competitor_registration_items.return_value = [{
                "item_ref": source_id,
                "status": "completed",
                "artifact": {
                    "source_id": source_id,
                    "transcript_ref": str(transcript),
                    "metrics": {},
                    "comments": [],
                },
            }]
            core.observed_breakdown_content_types.return_value = []
            core.record_external_competitor_execution.return_value = "model-run-external-receipt"

            with patch(
                "scripts.core.production.stage1_competitor_registration.require_frozen_content_type_registry",
                return_value=FROZEN_REGISTRY,
            ):
                with self.assertRaises(FormalSkillValidationError):
                    submit_competitor_breakdown_external_result(
                        core,
                        registration_id="registration-external-receipt",
                        source_id=source_id,
                        execution_id="execution-external-receipt",
                        executor_id="executor-test",
                        model_ref="test-model",
                        submitted_at="2026-08-18T00:00:00+08:00",
                        output=self._external_output("作品背景说明", source_id=source_id),
                    )

        core.record_competitor_breakdown_attempt.assert_not_called()
        core.record_competitor_registration_item.assert_not_called()
        core.record_independent_competitor_breakdown.assert_not_called()
        core.register_breakdown_question_expansions.assert_not_called()
        core.record_competitor_breakdown_optional_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
