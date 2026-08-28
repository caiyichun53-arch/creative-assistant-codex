from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract,
    FormalSkillValidationError,
    apply_binding,
    preprocess_formal_skill_input,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1b_daily_discovery import (
    ExternalIntelligenceRequired,
    Stage1BDailyDiscoveryService,
)


def _valid_source_to_topic_output(source_ref: str) -> dict:
    angle = {
        "found": True,
        "direction": "解释一个具体问题",
        "reason": "输入材料支持这个方向",
    }
    return {
        "topic_status": "generated",
        "candidate_topic": "一个可以继续核验的具体选题",
        "topic_angle": "从具体机制解释现象",
        "core_question": "这个现象为什么会发生",
        "audience_relation": "帮助目标受众理解一个具体问题",
        "content_increment": "补充机制和核验路径",
        "supporting_evidence": [source_ref],
        "source_constraints": ["来源只用于发现线索"],
        "no_result_reason": "none",
        "confidence": "medium",
        "angle_discovery": {
            "problem_angle": angle,
            "audience_relevance_angle": angle,
            "content_increment_angle": angle,
            "tension_angle": angle,
            "distinct_angle": angle,
            "producible_angle": angle,
            "durable_value_angle": angle,
        },
        "candidate_selection": {
            "selected_direction": "解释一个具体问题",
            "why_selected": "材料足以支撑下一步核验",
            "rejected_directions": [],
        },
        "risks": [],
        "material_gaps": ["正式研究仍需独立核验"],
        "user_review_required": True,
        "user_review_reasons": ["候选仍需人工选择"],
        "execution_review": {
            "used_only_supplied_material": True,
            "did_not_search_by_itself": True,
            "did_not_invent_facts": True,
            "respected_domain_boundary": True,
            "respected_risk_boundary": True,
            "did_not_force_candidate": True,
            "no_score_rank_weight": True,
        },
        "experience_usage": {
            "used_experience_ids": [],
            "unused_experience_ids": [],
            "rationale": "没有适用的经验卡",
        },
        "topic_shape": {
            "core_subject": "一个具体现象",
            "scope_boundary": "只讨论当前来源能支持的范围",
            "one_piece_line": "解释这个现象的机制",
        },
        "delivery_contract": {"user_gets": "一份解释这个具体问题的候选选题"},
        "schema_version": "source_to_topic.output.v2",
    }


class Stage2AExternalIntelligenceBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "stage2a.sqlite3"
        self.core = Stage0ContentProductionCore.open(self.db_path, data_identity="test")
        self.contract = FormalSkillContract.from_runtime_skill("source_to_topic")
        self.guard_patcher = patch(
            "scripts.core.production.stage1b_daily_discovery.enforce_atomic_skill_runtime_guard",
            lambda **_kwargs: None,
        )
        self.guard_patcher.start()

    def tearDown(self) -> None:
        self.core.close()
        self.guard_patcher.stop()
        self.tempdir.cleanup()

    def _prepared_input(self, suffix: str, daily_run_id: str | None = None) -> tuple[dict, dict, dict]:
        run = self.core.create_discovery_run(
            discovery_date="2026-08-28",
            actor="isolated-test",
            execution_mode="test_isolated",
            domains=("music_entertainment",),
            idempotency_key=f"stage2a-run:{suffix}",
            daily_run_id=daily_run_id,
        )
        direction = self.core.register_saved_user_direction_source(
            direction_id=f"direction-{suffix}",
            domain_label="music_entertainment",
            core_question="一个足够长的来源问题",
            submitted_by="isolated-test",
        )
        source = {
            "source_type": "saved_user_direction",
            "source_object_id": direction["direction_id"],
            "source_object_version": direction["source_object_version"],
            "source_time": direction["saved_at"],
            "payload": {
                "title": "一个足够长的来源问题",
                "core_question": "一个足够长的来源问题",
                "url": "",
                "account_name": "用户保存方向",
                "formal_source": {
                    "table": "stage1_saved_user_direction_source",
                    "object_id": direction["direction_id"],
                    "object_version": direction["source_object_version"],
                    "raw_metadata_hash": direction["source_object_version"],
                },
            },
        }
        source_row = self.core.record_discovery_source(
            run_id=run["run_id"],
            domain_label="music_entertainment",
            source_type=source["source_type"],
            source_object_id=source["source_object_id"],
            source_object_version=source["source_object_version"],
            source_time=source["source_time"],
            expires_at=None,
            payload=source["payload"],
            idempotency_key=f"stage2a-source:{suffix}",
        )
        self.core.record_discovery_filter(
            source_version_id=source_row["source_version_id"],
            outcome="eligible",
            reason_code="eligible",
            detail={},
            idempotency_key=f"stage2a-filter:{suffix}",
        )
        assembly_payload = Stage1BDailyDiscoveryService._assembly_payload(
            run_id=run["run_id"],
            domain_label="music_entertainment",
            source={**source, "source_version_id": source_row["source_version_id"]},
            source_version_id=source_row["source_version_id"],
            experience_cards=[],
        )
        assembly = self.core.create_discovery_input_assembly(
            run_id=run["run_id"],
            source_version_id=source_row["source_version_id"],
            payload=assembly_payload,
            prompt_version="source_to_topic.prompt.v2",
            skill_version="source_to_topic.skill.v1.2.0",
            idempotency_key=f"stage2a-assembly:{suffix}",
            external_execution=True,
        )
        preprocessed = preprocess_formal_skill_input("source_to_topic", assembly_payload)
        model_input = apply_binding(
            self.contract.input_map, assembly_payload, {}, preprocessed
        )
        return run, assembly, {"input": assembly_payload, "model_input": model_input}

    def _service(self, executor=None) -> Stage1BDailyDiscoveryService:
        return Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=None,
            external_executor=executor,
        )

    def test_core_task_contains_skill_material_and_limits_without_model_choice(self) -> None:
        captured: dict = {}

        def executor(task):
            captured.update(task)
            return {
                "execution_id": "execution-a",
                "executor_id": "hermes-test-double",
                "model_ref": "mimo-test-double",
                "submitted_at": "2026-08-28T01:00:00+00:00",
                "output": _valid_source_to_topic_output(task["input"]["source_evidence_refs"][0]),
            }

        run, assembly, payloads = self._prepared_input("normal")
        receipt, output = self._service(executor)._run_source_to_topic_skill(
            run_id=run["run_id"],
            source_version_id=assembly["input_integrity_hash"] and self.core.conn.execute(
                "SELECT source_version_id FROM stage1b_input_assembly WHERE assembly_id=?",
                (assembly["assembly_id"],),
            ).fetchone()[0],
            assembly_id=assembly["assembly_id"],
            input_payload=payloads["input"],
        )
        self.assertEqual(receipt.envelope_version_id, receipt.model_run_id)
        self.assertEqual(output["schema_version"], "source_to_topic.output.v2")
        self.assertEqual(captured["task_type"], "source_to_topic")
        self.assertEqual(captured["skill"]["version"], "1.2.0")
        self.assertTrue(captured["constraints"]["cannot_change_business_state"])
        self.assertEqual(captured["output_requirements"]["submission"], "structured_fields")

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        forbidden = {"model", "model_name", "provider", "provider_name", "model_route"}
        self.assertFalse(forbidden.intersection(keys(captured)))

    def test_invalid_external_result_is_rejected_by_core_validation(self) -> None:
        run, assembly, payloads = self._prepared_input("invalid")
        service = self._service(
            lambda _task: {
                "execution_id": "execution-invalid",
                "executor_id": "codex-test-double",
                "model_ref": "codex-current-test",
                "output": {"topic_status": "generated"},
            }
        )
        source_version_id = str(
            self.core.conn.execute(
                "SELECT source_version_id FROM stage1b_input_assembly WHERE assembly_id=?",
                (assembly["assembly_id"],),
            ).fetchone()[0]
        )
        with self.assertRaises(FormalSkillValidationError):
            service._run_source_to_topic_skill(
                run_id=run["run_id"],
                source_version_id=source_version_id,
                assembly_id=assembly["assembly_id"],
                input_payload=payloads["input"],
            )
        row = self.core.conn.execute(
            "SELECT validation_status, via_model_gateway FROM stage1b_model_run WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        self.assertEqual(row["validation_status"], "failed")
        self.assertEqual(row["via_model_gateway"], 0)
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage1b_candidate_version WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0],
            0,
        )

    def test_no_executor_reports_external_need_and_does_not_finish_run(self) -> None:
        run, assembly, payloads = self._prepared_input("waiting")
        source_version_id = str(
            self.core.conn.execute(
                "SELECT source_version_id FROM stage1b_input_assembly WHERE assembly_id=?",
                (assembly["assembly_id"],),
            ).fetchone()[0]
        )
        with self.assertRaises(ExternalIntelligenceRequired) as raised:
            self._service()._run_source_to_topic_skill(
                run_id=run["run_id"],
                source_version_id=source_version_id,
                assembly_id=assembly["assembly_id"],
                input_payload=payloads["input"],
            )
        self.assertEqual(raised.exception.task["task_type"], "source_to_topic")
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage1b_discovery_run WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0],
            "processing",
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage1b_model_run WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()[0],
            0,
        )

    def test_daily_discovery_returns_the_task_without_finishing_the_discovery_run(self) -> None:
        self.core.register_saved_user_direction_source(
            direction_id="daily-direction",
            domain_label="music_entertainment",
            core_question="一个日常来源转选题问题",
            submitted_by="isolated-test",
        )
        result = self._service().run_daily_discovery(
            discovery_date="2026-08-28",
            actor="isolated-test",
            idempotency_key="stage2a-daily-waiting",
            execution_mode="test_isolated",
            domains=("music_entertainment",),
            source_types=("saved_user_direction",),
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(result["status"], "requires_external_intelligence")
        self.assertEqual(result["task"]["task_type"], "source_to_topic")
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage1b_discovery_run WHERE run_id=?",
                (result["run_id"],),
            ).fetchone()[0],
            "processing",
        )

    def test_changing_executor_does_not_create_a_second_business_run(self) -> None:
        business = CreationAssistantFormalBusinessCore(core=self.core)
        started = business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-28",
            actor="isolated-test",
        )
        daily_run_id = str(started["daily_run"]["daily_run_id"])
        business.finish_daily(
            daily_run_id=daily_run_id,
            lifecycle="failed",
            actor="isolated-test",
            reason="external executor was unavailable",
        )
        resumed = business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-28",
            actor="different-executor",
            resume=True,
        )
        self.assertEqual(resumed["daily_run"]["daily_run_id"], daily_run_id)

        first_run, first_assembly, first_payloads = self._prepared_input("executor-a", daily_run_id)
        first_source_id = str(self.core.conn.execute(
            "SELECT source_version_id FROM stage1b_input_assembly WHERE assembly_id=?",
            (first_assembly["assembly_id"],),
        ).fetchone()[0])
        first_service = self._service(lambda task: {
            "execution_id": "execution-a",
            "executor_id": "hermes",
            "model_ref": "mimo",
            "output": _valid_source_to_topic_output(task["input"]["source_evidence_refs"][0]),
        })
        first_receipt, first_output = first_service._run_source_to_topic_skill(
            run_id=first_run["run_id"], source_version_id=first_source_id,
            assembly_id=first_assembly["assembly_id"], input_payload=first_payloads["input"],
        )
        self.core.create_discovery_candidate(
            run_id=first_run["run_id"], source_version_id=first_source_id,
            model_run_id=first_receipt.model_run_id, candidate_id="candidate-a",
            payload={**first_output, "title": first_output["candidate_topic"]},
            idempotency_key="stage2a-candidate-a",
        )

        second_run, second_assembly, second_payloads = self._prepared_input("executor-b", daily_run_id)
        second_source_id = str(self.core.conn.execute(
            "SELECT source_version_id FROM stage1b_input_assembly WHERE assembly_id=?",
            (second_assembly["assembly_id"],),
        ).fetchone()[0])
        second_service = self._service(lambda task: {
            "execution_id": "execution-b",
            "executor_id": "codex",
            "model_ref": "codex-current",
            "output": _valid_source_to_topic_output(task["input"]["source_evidence_refs"][0]),
        })
        second_receipt, _ = second_service._run_source_to_topic_skill(
            run_id=second_run["run_id"], source_version_id=second_source_id,
            assembly_id=second_assembly["assembly_id"], input_payload=second_payloads["input"],
        )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_daily_run").fetchone()[0],
            1,
        )
        daily_contexts = self.core.conn.execute(
            "SELECT daily_run_id FROM stage1b_run_execution_context WHERE run_id IN (?, ?)",
            (first_run["run_id"], second_run["run_id"]),
        ).fetchall()
        self.assertEqual({row["daily_run_id"] for row in daily_contexts}, {daily_run_id})
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage1b_model_run WHERE model_name IN ('mimo', 'codex-current')"
            ).fetchone()[0],
            2,
        )


if __name__ == "__main__":
    unittest.main()
