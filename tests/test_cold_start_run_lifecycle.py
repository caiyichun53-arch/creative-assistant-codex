"""Focused isolation checks for the single cold-start run lifecycle."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
)


class ColdStartRunLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        self.now = "2026-08-23T00:00:00+08:00"
        self.competitor_ids = tuple(f"competitor-{index}" for index in range(20))
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', ?, ?, ?)",
            ("owned", "owned", "domain-lifecycle", "douyin:owned", "test", "fixture", self.now),
        )
        for account_id in self.competitor_ids:
            self.connection.execute(
                "INSERT INTO stage0_content_account VALUES (?, 'competitor', ?, ?, ?, 'active', ?, ?, ?)",
                (
                    account_id,
                    account_id,
                    "domain-lifecycle",
                    f"douyin:{account_id}",
                    "test",
                    "fixture",
                    self.now,
                ),
            )
        self.created = self.core.start_cold_start(
            cold_start_id="cold-lifecycle",
            owned_account_id="owned",
            competitor_account_ids=self.competitor_ids,
            actor="fixture",
        )
        self.run_model_binding = {
            "route_id": "business_analysis",
            "provider_ref": "isolated_test_provider",
            "provider_name": "hermes",
            "provider_type": "openai_compatible",
            "model_name": "isolated/model-a",
            "endpoint": "https://isolated.invalid/v1",
            "source": "hermes_current_session",
            "explicit_override": False,
        }
        self.connection.execute(
            "INSERT INTO stage0_cold_start_configuration("
            "configuration_id, domain_mode, domain_label, domain_name, "
            "domain_boundary, platform, owned_account_id, competitor_account_ids_json, "
            "status, data_identity, confirmed_by, confirmed_at, cold_start_id"
            ") VALUES (?, 'create', ?, ?, '', 'douyin', ?, ?, 'started', ?, ?, ?, ?)",
            (
                "config-lifecycle",
                "domain-lifecycle",
                "lifecycle",
                "owned",
                json.dumps(list(self.competitor_ids)),
                "test",
                "fixture",
                self.now,
                "cold-lifecycle",
            ),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start_run_contract("
            "cold_start_id, domain_label, owned_account_id, competitor_account_ids_json, "
            "input_snapshot_json, cold_start_contract_version, created_at, data_identity"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cold-lifecycle",
                "domain-lifecycle",
                "owned",
                json.dumps(list(self.competitor_ids)),
                json.dumps({"run_model": self.run_model_binding}),
                "cold_start_guard_v14",
                self.now,
                "test",
            ),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.core.close()

    def run_status(self) -> str:
        return str(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-lifecycle'"
            ).fetchone()[0]
        )

    def test_new_run_starts_running(self) -> None:
        self.assertEqual(self.created["status"], "running")
        self.assertEqual(self.run_status(), "running")

    def test_running_transitions_to_stopped(self) -> None:
        result = self.core.stop_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="isolated stop",
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.run_status(), "stopped")

    def test_stopped_stop_returns_current_state_without_command_receipt(self) -> None:
        self.core.stop_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="isolated stop",
        )
        repeated = self.core.stop_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="repeated stop",
        )
        self.assertEqual(repeated["status"], "stopped")
        self.assertEqual(repeated["message"], "当前冷启动已经停止。")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_command_receipt "
                "WHERE command_scope IN ('stop_configured_cold_start', 'resume_stopped_cold_start')"
            ).fetchone()[0],
            0,
        )

    def test_running_resume_returns_current_state_without_second_executor(self) -> None:
        result = self.core.resume_stopped_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            task_model_binding=self.run_model_binding,
        )
        self.assertEqual(result["status"], "running")
        self.assertFalse(result["resumed"])
        self.assertFalse(result["created_new_run"])
        self.assertEqual(self.run_status(), "running")

    def test_stopped_transitions_back_to_running(self) -> None:
        self.core.stop_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="isolated stop",
        )
        result = self.core.resume_stopped_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            task_model_binding=self.run_model_binding,
        )
        self.assertEqual(result["prior_status"], "stopped")
        self.assertEqual(self.run_status(), "running")

    def test_failed_transitions_back_to_running(self) -> None:
        failed = self.core.fail_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="isolated failure",
        )
        self.assertEqual(failed["status"], "failed")
        resumed = self.core.resume_stopped_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            task_model_binding=self.run_model_binding,
        )
        self.assertEqual(resumed["prior_status"], "failed")
        self.assertEqual(self.run_status(), "running")

    def test_same_run_can_record_a_second_failure_with_new_reason(self) -> None:
        first = self.core.fail_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="failure A",
        )
        self.assertEqual(first["status"], "failed")
        self.core.resume_stopped_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            task_model_binding=self.run_model_binding,
        )
        second = self.core.fail_configured_cold_start(
            configuration_id="config-lifecycle",
            actor="fixture",
            reason="failure B",
        )
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["reason"], "failure B")
        self.assertEqual(self.run_status(), "failed")
        reasons = [
            row[0]
            for row in self.connection.execute(
                "SELECT json_extract(payload_json, '$.reason') "
                "FROM stage0_audit_event "
                "WHERE action='configured_cold_start_failed' ORDER BY created_at"
            )
        ]
        self.assertEqual(reasons, ["failure A", "failure B"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_command_receipt "
                "WHERE command_scope='fail_configured_cold_start'"
            ).fetchone()[0],
            0,
        )

    def test_human_wait_does_not_override_remaining_automatic_work(self) -> None:
        self.connection.execute(
            "UPDATE stage0_competitor_registration "
            "SET current_step='historical_material', status='awaiting_human_review' "
            "WHERE cold_start_id='cold-lifecycle'"
        )
        first_registration = str(self.created["registration_ids"][0])
        self.connection.execute(
            "UPDATE stage0_competitor_registration SET status='processing' "
            "WHERE registration_id=?",
            (first_registration,),
        )
        result = self.core.refresh_cold_start_run_lifecycle(
            cold_start_id="cold-lifecycle"
        )
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.run_status(), "running")

    def test_only_human_decisions_transitions_to_waiting_human(self) -> None:
        self.connection.execute(
            "UPDATE stage0_competitor_registration "
            "SET current_step='historical_material', status='awaiting_human_review' "
            "WHERE cold_start_id='cold-lifecycle'"
        )
        result = self.core.refresh_cold_start_run_lifecycle(
            cold_start_id="cold-lifecycle"
        )
        self.assertEqual(result["status"], "waiting_human")
        self.assertEqual(self.run_status(), "waiting_human")

    def test_business_stage_changes_do_not_replace_run_lifecycle(self) -> None:
        first_registration = str(self.created["registration_ids"][0])
        self.connection.execute(
            "UPDATE stage0_competitor_registration "
            "SET current_step='breakdown', status='processing' WHERE registration_id=?",
            (first_registration,),
        )
        self.assertEqual(self.run_status(), "running")
        schema = str(
            self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_cold_start'"
            ).fetchone()[0]
        )
        self.assertEqual(
            schema.count("status TEXT NOT NULL CHECK"),
            1,
        )
        for status in ("running", "stopped", "failed", "waiting_human", "completed"):
            self.assertIn(f"'{status}'", schema)


if __name__ == "__main__":
    unittest.main()
