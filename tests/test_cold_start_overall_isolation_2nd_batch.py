"""Second-batch overall isolation gate.

This is an acceptance test, not a business implementation. It records whether
one isolated user confirmation enters the real cold-start orchestrator without
a second handoff.
"""

from __future__ import annotations

import json
from tests._cold_start_test_model import resolve_task_model
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1_competitor_registration import CompetitorRegistrationService
from tests.test_cold_start_orchestrator_2b1 import FakeContentBranchExecutor


class FailFirstBreakdownExecutor(FakeContentBranchExecutor):
    """Use the real orchestrator while leaving one breakdown unfinished once."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def execute(self, *, step_name, registration, completed_artifacts):
        if step_name == "breakdown" and not self.failed:
            self.failed = True
            self.calls.append((str(registration["registration_id"]), step_name))
            raise RuntimeError("isolated overall breakdown failure")
        return super().execute(
            step_name=step_name,
            registration=registration,
            completed_artifacts=completed_artifacts,
        )


class FailFirstHighSignalExecutor(FakeContentBranchExecutor):
    """Leave selection at nineteen once so twenty-of-twenty cannot be faked."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def execute(self, *, step_name, registration, completed_artifacts):
        if step_name == "high_signal_identification" and not self.failed:
            self.failed = True
            self.calls.append((str(registration["registration_id"]), step_name))
            raise RuntimeError("isolated overall high-signal failure")
        return super().execute(
            step_name=step_name,
            registration=registration,
            completed_artifacts=completed_artifacts,
        )


class ColdStartOverallIsolation2ndBatchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        self.executor = FakeContentBranchExecutor()
        self.registration_service = CompetitorRegistrationService(
            core=self.core,
            executor=self.executor,
        )
        self.adapter = ColdStartHumanDecisionAdapter(
            core=self.core,
            config_dir=self.config_dir,
            execution_registration_service=self.registration_service,
            task_model_resolver=resolve_task_model,
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    @staticmethod
    def payload() -> dict:
        return {
            "domain_mode": "create",
            "domain_name": "第二批整体验收隔离领域",
            "platform": "douyin",
            "owned_account": {
                "display_name": "测试自营账号",
                "external_account_ref": "douyin:isolated-owned",
            },
            "competitor_accounts": [
                {
                    "display_name": f"测试对标账号{i}",
                    "external_account_ref": f"douyin:isolated-competitor-{i}",
                }
                for i in range(20)
            ],
            "actor": "隔离验收用户",
        }

    def _use_executor(self, executor: FakeContentBranchExecutor) -> None:
        self.executor = executor
        self.registration_service = CompetitorRegistrationService(
            core=self.core,
            executor=executor,
        )
        self.adapter = ColdStartHumanDecisionAdapter(
            core=self.core,
            config_dir=self.config_dir,
            execution_registration_service=self.registration_service,
            task_model_resolver=resolve_task_model,
        )

    def _confirm_once(self) -> dict:
        payload = self.payload()
        preview = self.adapter.preview_configuration(payload)
        self.assertTrue(preview["ready_to_confirm"])
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            0,
        )

        self.core.propose_human_decision_carrier(
            carrier_binding_id="isolated-overall-carrier",
            carrier_kind="isolated_test",
            entry_ref="2nd_batch_overall_isolation",
            context_strategy="same_test_session",
            actor="隔离验收用户",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id="isolated-overall-carrier",
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "isolated-overall-round-trip",
            },
            actor="隔离验收用户",
            actor_kind="user",
        )
        with patch("scripts.core.production.stage0_content_core.record_runtime_guard_event"):
            result = self.adapter.confirm_configuration(
                configuration=preview["normalized"],
                transport_actor="隔离验收用户",
            )
        self.cold_start_id = str(result["cold_start_id"])
        self.configuration_id = str(result["configuration_id"])
        self.confirm_command = None
        return result

    def _continue_current_run(self) -> dict:
        with patch("scripts.core.production.stage0_content_core.record_runtime_guard_event"):
            return self.adapter.onboarding.continue_current_cold_start(
                configuration_id=self.configuration_id,
                actor="隔离验收用户",
            )

    def _review_current_library(self) -> dict:
        library = self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)
        self.assertIsNotNone(library)
        command = FormalHumanDecisionCommand(
            command_id="isolated-overall-tag-review",
            carrier_binding_id="isolated-overall-carrier",
            session_ref="isolated-overall-session",
            actor="隔离验收用户",
            action="review_competitor_tag_library",
            target_ref=f"cold_start:{self.cold_start_id}",
            payload={
                "cold_start_id": self.cold_start_id,
                "decisions": [
                    {
                        "tag_id": item["tag_id"],
                        "decision": "accepted",
                        "edited_tag": item["tag"],
                    }
                    for item in library["candidates"]
                ],
                "reason": "第二批整体隔离验收",
            },
        )
        return self.adapter.review_tag_library(command=command)

    def test_one_confirmation_must_enter_orchestrator_in_isolation(self) -> None:
        result = self._confirm_once()
        run_count = self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_cold_start"
        ).fetchone()[0]
        registration_count = self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration"
        ).fetchone()[0]
        evidence = {
            "cold_start_id": result.get("cold_start_id"),
            "automatic_start": result.get("automatic_start"),
            "execution_present": "execution" in result,
            "run_count": int(run_count),
            "registration_count": int(registration_count),
            "data_identity": self.core.data_identity,
            "cold_start_status": self.core.conn.execute(
                "SELECT status FROM stage0_cold_start LIMIT 1"
            ).fetchone()[0],
        }
        print("OVERALL_ISOLATION_GATE_EVIDENCE=" + json.dumps(evidence, ensure_ascii=False, sort_keys=True))

        self.assertIn(
            "execution",
            result,
            "一次隔离确认创建运行后必须自动进入冷启动总编排",
        )

    def test_complete_chain_keeps_one_run_and_does_not_complete_cold_start(self) -> None:
        result = self._confirm_once()
        execution = result["execution"]
        self.assertEqual(execution["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(execution["breakdown_progress"], {"completed": 20, "total": 20})
        self.assertTrue(execution["tag_input_ready"])
        self.assertTrue(execution["tag_branch"]["generation_started"])
        self.assertFalse(execution["tag_branch"]["waits_for_breakdown"])
        library = self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id)
        self.assertIsNotNone(library)
        self.assertEqual(library["status"], "awaiting_human_review")
        self.assertNotIn(
            "拆解不应生成标签",
            [item["tag"] for item in library["candidates"]],
        )
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_cold_start WHERE data_identity=?",
                (self.core.data_identity,),
            ).fetchone()[0]),
            1,
        )
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration WHERE cold_start_id=? AND data_identity=?",
                (self.cold_start_id, self.core.data_identity),
            ).fetchone()[0]),
            20,
        )
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration "
                "WHERE cold_start_id=? AND data_identity=? AND status='completed'",
                (self.cold_start_id, self.core.data_identity),
            ).fetchone()[0]),
            20,
        )
        self.assertNotEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (self.cold_start_id,),
            ).fetchone()["status"],
            "completed",
        )

        calls_before_continue = list(self.executor.calls)
        continued = self._continue_current_run()
        self.assertEqual(continued["execution"]["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(continued["execution"]["breakdown_progress"], {"completed": 20, "total": 20})
        self.assertEqual(self.executor.calls, calls_before_continue)
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
                (self.cold_start_id, self.core.data_identity),
            ).fetchone()[0]),
            1,
        )
        self.assertEqual(
            int(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]),
            1,
        )

    def test_breakdown_failure_still_allows_tags_and_resumes_same_run(self) -> None:
        self._use_executor(FailFirstBreakdownExecutor())
        first = self._confirm_once()
        self.assertTrue(first["execution"]["tag_input_ready"])
        self.assertEqual(first["execution"]["selection_progress"], {"completed": 20, "total": 20})
        self.assertEqual(first["execution"]["breakdown_progress"], {"completed": 19, "total": 20})
        self.assertIsNotNone(self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id))

        reviewed = self._review_current_library()
        self.assertEqual(reviewed["status"], "accepted")
        calls_before_continue = list(self.executor.calls)
        completed_ids = {
            str(row["registration_id"])
            for row in self.core.conn.execute(
                "SELECT registration_id FROM stage0_competitor_registration "
                "WHERE cold_start_id=? AND status='completed' AND data_identity=?",
                (self.cold_start_id, self.core.data_identity),
            ).fetchall()
        }
        continued = self._continue_current_run()
        self.assertEqual(continued["execution"]["breakdown_progress"], {"completed": 20, "total": 20})
        self.assertTrue(continued["execution"]["tag_input_ready"])
        self.assertTrue(all(registration_id not in completed_ids for registration_id, _ in self.executor.calls[len(calls_before_continue):]))
        self.assertEqual(
            int(self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
                (self.cold_start_id, self.core.data_identity),
            ).fetchone()[0]),
            1,
        )
        self.assertNotEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (self.cold_start_id,),
            ).fetchone()["status"],
            "completed",
        )

    def test_pre_twenty_selection_failure_does_not_create_tag_input_and_can_resume(self) -> None:
        self._use_executor(FailFirstHighSignalExecutor())
        first = self._confirm_once()
        self.assertFalse(first["execution"]["tag_input_ready"])
        self.assertEqual(first["execution"]["selection_progress"], {"completed": 19, "total": 20})
        self.assertIsNone(self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id))

        continued = self._continue_current_run()
        self.assertTrue(continued["execution"]["tag_input_ready"])
        self.assertEqual(continued["execution"]["selection_progress"], {"completed": 20, "total": 20})
        self.assertIsNotNone(self.core.get_cold_start_tag_library(cold_start_id=self.cold_start_id))
        self.assertEqual(
            int(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
