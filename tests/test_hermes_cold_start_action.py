"""Isolation checks for the Hermes cold-start transport bridge."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.human_decision_entry import ColdStartHumanDecisionAdapter
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.agent_platform.hermes_cold_start_action import HermesColdStartAction


class HermesColdStartActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection, db_path=Path(":memory:"), data_identity="test"
        )
        self.core.install_schema()
        self.core.propose_human_decision_carrier(
            carrier_binding_id="hermes-isolated-carrier",
            carrier_kind="hermes_isolated_test",
            entry_ref="hermes_cold_start_action_test",
            context_strategy="same_test_session",
            actor="隔离测试用户",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id="hermes-isolated-carrier",
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "hermes-isolated-round-trip",
            },
            actor="隔离测试用户",
            actor_kind="user",
        )
        self.adapter = ColdStartHumanDecisionAdapter(
            core=self.core, config_dir=self.config_dir,
        )
        self.action = HermesColdStartAction(
            adapter=self.adapter,
            carrier_binding_id="hermes-isolated-carrier",
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    @staticmethod
    def configuration(*, domain: str = "Hermes隔离领域") -> dict:
        return {
            "domain_name": domain,
            "owned_account": {
                "display_name": "自营账号",
                "external_account_ref": "douyin:hermes-owned-account",
            },
            "competitor_accounts": [
                {
                    "display_name": f"对标账号{i}",
                    "external_account_ref": f"douyin:hermes-competitor-{i}",
                }
                for i in range(20)
            ],
        }

    def count(self, table: str) -> int:
        return int(self.core.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def preview(self, configuration: dict) -> dict:
        return self.action.invoke(
            operation="preview",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
        )

    def confirm(self, configuration: dict, *args: object) -> dict:
        return self.action.invoke(
            operation="confirm",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            explicit_user_confirmation=True,
        )

    def test_incomplete_preview_asks_for_formal_inputs_before_session_or_onboarding(self) -> None:
        result = self.action.invoke(
            operation="preview",
            configuration={"domain_name": "only-domain"},
            actor="",
            session_ref="",
        )
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["next_action"], "ask_user")
        self.assertEqual(
            [item["field"] for item in result["missing"]],
            ["owned_account", "competitor_accounts"],
        )
        self.assertEqual(self.count("stage0_cold_start"), 0)

    def test_resume_reuses_existing_run_without_new_configuration_input(self) -> None:
        configuration = self.configuration(domain="Hermes可恢复领域")
        preview = self.preview(configuration)
        created = self.confirm(
            configuration, "hermes-resume-create-1"
        )
        self.action.invoke(
            operation="stop",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-stop-before-resume-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-stop-before-resume-1"),
            reason="隔离恢复前停止",
            explicit_user_confirmation=True,
        )
        resumed = self.action.invoke(
            operation="resume",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-resume-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-resume-1"),
        )
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["cold_start_id"], created["cold_start_id"])
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(self.count("stage0_cold_start"), 1)
        self.assertEqual(self.count("stage0_cold_start_configuration"), 1)

    def test_preview_does_not_require_an_operator_field(self) -> None:
        configuration = self.configuration(domain="Hermes单用户预览")
        preview = self.action.invoke(
            operation="preview",
            configuration=configuration,
            actor="current-feishu-session",
            session_ref="hermes-isolated-session",
        )
        self.assertTrue(preview["ready_to_confirm"])
        self.assertNotIn("actor", {item["code"] for item in preview["blockers"]})

    def test_transport_identity_is_not_compared_with_legacy_configuration_actor(self) -> None:
        configuration = self.configuration(domain="Hermes传输上下文")
        configuration["actor"] = "legacy-operator-value"
        preview = self.action.invoke(
            operation="preview",
            configuration=configuration,
            actor="current-feishu-session",
            session_ref="hermes-isolated-session",
        )
        self.assertTrue(preview["ready_to_confirm"])

    def test_preview_is_read_only_and_confirmation_creates_one_run(self) -> None:
        configuration = self.configuration()
        preview = self.preview(configuration)
        self.assertTrue(preview["ready_to_confirm"])
        self.assertEqual(self.count("stage0_cold_start"), 0)
        self.assertEqual(self.count("stage0_human_decision_command"), 0)

        result = self.confirm(
            configuration, "hermes-isolated-confirm-1"
        )
        self.assertTrue(result["automatic_start"])
        self.assertEqual(self.count("stage0_cold_start"), 1)
        self.assertEqual(self.count("stage0_cold_start_configuration"), 1)
        self.assertEqual(self.count("stage0_competitor_registration"), 20)
        run = self.core.conn.execute(
            "SELECT created_by FROM stage0_cold_start WHERE cold_start_id=?",
            (result["cold_start_id"],),
        ).fetchone()
        configuration_row = self.core.conn.execute(
            "SELECT confirmed_by FROM stage0_cold_start_configuration WHERE configuration_id=?",
            (result["configuration_id"],),
        ).fetchone()
        account_rows = self.core.conn.execute(
            "SELECT created_by FROM stage0_content_account ORDER BY content_account_id"
        ).fetchall()
        self.assertEqual(run["created_by"], "")
        self.assertEqual(configuration_row["confirmed_by"], "")
        self.assertTrue(account_rows)
        self.assertTrue(all(row["created_by"] == "" for row in account_rows))
        self.assertEqual(self.count("stage0_human_decision_command"), 0)

    def test_repeated_command_replays_without_second_state(self) -> None:
        configuration = self.configuration(domain="Hermes幂等领域")
        preview = self.preview(configuration)
        first = self.confirm(configuration, "hermes-replay-1")
        second = self.confirm(configuration, "hermes-replay-1")
        self.assertEqual(second["error_type"], "no_pending_preview")
        self.assertEqual(self.count("stage0_cold_start"), 1)
        self.assertEqual(self.count("stage0_cold_start_configuration"), 1)
        self.assertEqual(self.count("stage0_human_decision_command"), 0)

    def test_completed_run_cannot_be_confirmed_again(self) -> None:
        configuration = self.configuration(domain="Hermes已完成不可重启")
        preview = self.preview(configuration)
        created = self.confirm(
            configuration, "hermes-completed-create-1"
        )
        with self.core.conn:
            self.core.conn.execute(
                "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id=?",
                (created["cold_start_id"],),
            )
            self.core.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='completed' WHERE configuration_id=?",
                (created["configuration_id"],),
            )
        retry = self.confirm(configuration, "hermes-completed-retry-1")
        self.assertEqual(retry["error_type"], "no_pending_preview")
        self.assertEqual(self.count("stage0_cold_start"), 1)

    def test_cold_start_confirmation_does_not_require_validated_carrier(self) -> None:
        self.core.propose_human_decision_carrier(
            carrier_binding_id="unvalidated-hermes-carrier",
            carrier_kind="hermes_feishu_gateway",
            entry_ref="hermes://test/unvalidated",
            context_strategy="same_test_session",
            actor="隔离测试用户",
        )
        action = HermesColdStartAction(
            adapter=self.adapter,
            carrier_binding_id="unvalidated-hermes-carrier",
        )
        configuration = self.configuration(domain="Hermes权限领域")
        preview = action.invoke(
            operation="preview",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
        )
        result = action.invoke(
            operation="confirm",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-unvalidated-carrier-1",
            explicit_user_confirmation=True,
        )
        self.assertTrue(result["automatic_start"])
        self.assertEqual(self.count("stage0_cold_start"), 1)

    def test_fixed_internal_hermes_feishu_path_does_not_wait_for_validation(self) -> None:
        self.core.propose_human_decision_carrier(
            carrier_binding_id="hermes-creator-feishu-gateway",
            carrier_kind="hermes_feishu_gateway",
            entry_ref="hermes://creator/feishu-gateway",
            context_strategy="approved_internal_runtime_path",
            actor="隔离测试用户",
        )
        action = HermesColdStartAction(
            adapter=self.adapter,
            carrier_binding_id="hermes-creator-feishu-gateway",
        )
        configuration = self.configuration(domain="Hermes内部路径领域")
        preview = action.invoke(
            operation="preview",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
        )
        result = action.invoke(
            operation="confirm",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-fixed-carrier-1",
            explicit_user_confirmation=True,
        )
        self.assertTrue(result["automatic_start"])
        self.assertEqual(self.count("stage0_cold_start"), 1)
        self.assertEqual(self.count("stage0_human_decision_command"), 0)

    @staticmethod
    def trusted_context(command_id: str) -> dict[str, str]:
        return {
            "marker": "hermes_gateway_internal_v1",
            "platform": "feishu",
            "profile": "creator",
            "carrier_binding_id": "hermes-creator-feishu-gateway",
            "entry_ref": "hermes://creator/feishu-gateway",
            "tool_action": "cold_start_onboarding",
            "user_identity": "feishu-transport-user",
            "session_identity": "hermes-isolated-session",
            "command_identity": command_id,
        }

    def test_trusted_feishu_identity_is_transport_only_for_exact_run(self) -> None:
        configuration = self.configuration(domain="Hermes单用户可信传输")
        preview = self.action.invoke(
            operation="preview",
            configuration=configuration,
            actor="old-feishu-session",
            session_ref="hermes-isolated-session",
        )
        created = self.action.invoke(
            operation="confirm",
            configuration=configuration,
            actor="new-feishu-session",
            session_ref="hermes-isolated-session",
            command_id="hermes-single-user-confirm-1",
            explicit_user_confirmation=True,
            trusted_internal_context=self.trusted_context("hermes-single-user-confirm-1"),
        )
        status = self.action.invoke(
            operation="status",
            configuration={},
            actor="another-feishu-session",
            session_ref="hermes-isolated-session",
            command_id="hermes-single-user-status-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-single-user-status-1"),
        )
        self.assertEqual(status["cold_start_id"], created["cold_start_id"])
        self.assertEqual(self.count("stage0_human_decision_command"), 0)

    def test_trusted_path_registers_missing_fixed_candidate_without_validation(self) -> None:
        action = HermesColdStartAction(
            adapter=self.adapter,
            carrier_binding_id="hermes-creator-feishu-gateway",
        )
        configuration = self.configuration(domain="Hermes自动登记领域")
        preview = action.invoke(
            operation="preview",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
        )
        trusted = {
            "marker": "hermes_gateway_internal_v1",
            "platform": "feishu",
            "profile": "creator",
            "carrier_binding_id": "hermes-creator-feishu-gateway",
            "entry_ref": "hermes://creator/feishu-gateway",
            "tool_action": "cold_start_onboarding",
            "user_identity": "隔离用户",
            "session_identity": "hermes-isolated-session",
            "command_identity": "hermes-auto-propose-1",
        }
        result = action.invoke(
            operation="confirm",
            configuration=configuration,
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-auto-propose-1",
            explicit_user_confirmation=True,
            trusted_internal_context=trusted,
        )
        self.assertTrue(result["automatic_start"])
        self.assertIsNone(
            self.core.conn.execute(
                "SELECT 1 FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=?",
                ("hermes-creator-feishu-gateway",),
            ).fetchone()
        )

    def test_bridge_uses_adapter_and_has_no_direct_core_path(self) -> None:
        calls: list[dict] = []

        class RecordingAdapter:
            def preview_configuration(self, payload: dict) -> dict:
                return {"ready_to_confirm": True, "normalized": payload}

            def confirm_configuration(self, **kwargs: object) -> dict:
                calls.append(dict(kwargs))
                return {"status": "adapter_result"}

        action = HermesColdStartAction(
            adapter=RecordingAdapter(), carrier_binding_id="recording-carrier"  # type: ignore[arg-type]
        )
        action.invoke(
            operation="preview",
            configuration=self.configuration(domain="Hermes适配领域"),
            actor="隔离用户",
            session_ref="hermes-recording-session",
        )
        result = action.invoke(
            operation="confirm",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-recording-session",
            explicit_user_confirmation=True,
        )
        self.assertEqual(result["status"], "adapter_result")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["configuration"]["domain_name"], "Hermes适配领域")
        self.assertEqual(self.count("stage0_cold_start"), 0)

    def test_surrogate_input_is_cleaned_without_changing_chinese_or_emoji(self) -> None:
        configuration = self.configuration(
            domain="泛科普-社会生活" + chr(0xD83D) + chr(0xDE00) + chr(0xDCA7)
        )
        configuration["owned_account"]["display_name"] = "自营😀" + chr(0xDCA7)
        preview = self.preview(configuration)
        self.assertTrue(preview["ready_to_confirm"])
        self.assertEqual(preview["normalized"]["domain_name"], "泛科普-社会生活😀�")
        self.assertEqual(preview["normalized"]["owned_account"]["display_name"], "自营😀�")


    def test_status_stop_and_resume_keep_one_real_run(self) -> None:
        configuration = self.configuration(domain="Hermes停止恢复领域")
        preview = self.preview(configuration)
        created = self.confirm(
            configuration,
            "hermes-stop-resume-create-1",
        )

        running = self.action.invoke(
            operation="status",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-status-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-status-1"),
        )
        self.assertEqual(running["cold_start_id"], created["cold_start_id"])
        self.assertEqual(running["counts"]["owned_accounts"], 1)
        self.assertEqual(running["counts"]["competitor_accounts"], 20)
        self.assertEqual(running["stage_report"][1]["processing_account_count"], 20)
        self.assertEqual(running["counts"]["competitor_accounts"], 20)

        stopped = self.action.invoke(
            operation="stop",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-stop-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-stop-1"),
            reason="隔离验证停止",
            explicit_user_confirmation=True,
        )
        self.assertTrue(stopped["stopped"])
        self.assertEqual(stopped["cold_start_id"], created["cold_start_id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(self.count("stage0_human_decision_command"), 0)
        stopped_status = self.action.invoke(
            operation="status",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-status-2",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-status-2"),
        )
        self.assertEqual(stopped_status["current_stage"], "stopped")
        self.assertEqual(stopped_status["status"], "stopped")

        resumed = self.action.invoke(
            operation="resume",
            configuration={},
            actor="隔离用户",
            session_ref="hermes-isolated-session",
            command_id="hermes-resume-after-stop-1",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=self.trusted_context("hermes-resume-after-stop-1"),
        )
        self.assertTrue(resumed["resumed"])
        self.assertFalse(resumed["created_new_run"])
        self.assertEqual(resumed["cold_start_id"], created["cold_start_id"])
        self.assertEqual(self.count("stage0_cold_start"), 1)
        self.assertEqual(self.count("stage0_cold_start_configuration"), 1)



if __name__ == "__main__":
    unittest.main()
