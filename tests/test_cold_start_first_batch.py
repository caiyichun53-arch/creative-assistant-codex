"""Isolated verification for the first cold-start remediation batch."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests._cold_start_test_model import test_task_model_resolver

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.human_decision_entry import ColdStartHumanDecisionAdapter
from scripts.core.production.stage0_content_core import (
    COLD_START_CONTRACT_VERSION,
    Stage0ContentProductionCore,
    StateTransitionError,
)


class FirstBatchColdStartTest(unittest.TestCase):
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
        self.service = ColdStartOnboardingService(
            core=self.core, config_dir=self.config_dir,
            task_model_resolver=test_task_model_resolver,
        )
        self.adapter = ColdStartHumanDecisionAdapter(
            core=self.core, config_dir=self.config_dir,
            task_model_resolver=test_task_model_resolver,
        )
        self.core.propose_human_decision_carrier(
            carrier_binding_id="isolated-test-carrier",
            carrier_kind="isolated_test",
            entry_ref="first_batch_test",
            context_strategy="same_test_session",
            actor="测试用户",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id="isolated-test-carrier",
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "isolated-test-round-trip",
            },
            actor="测试用户",
            actor_kind="user",
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    @staticmethod
    def payload(*, domain: str = "家庭园艺", count: int = 20, actor: str = "用户") -> dict:
        return {
            "domain_mode": "create",
            "domain_name": domain,
            "platform": "douyin",
            "owned_account": {
                "display_name": "自营账号",
                "external_account_ref": "douyin:owned-account",
            },
            "competitor_accounts": [
                {
                    "display_name": f"对标账号{i}",
                    "external_account_ref": f"douyin:competitor-{i}",
                }
                for i in range(count)
            ],
            "actor": actor,
        }

    def preview(self, payload: dict) -> dict:
        return self.service.preview(payload)

    def test_exact_twenty_and_single_confirmation_create_one_run(self) -> None:
        payload = self.payload()
        preview = self.preview(payload)
        self.assertTrue(preview["ready_to_confirm"])
        before = self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]
        self.assertEqual(before, 0)
        result = self.service.confirm(payload)
        self.assertTrue(result["automatic_start"])
        self.assertEqual(result["cold_start_contract_version"], COLD_START_CONTRACT_VERSION)
        self.assertEqual(len(result["competitor_account_ids"]), 20)
        runs = self.core.conn.execute("SELECT * FROM stage0_cold_start").fetchall()
        self.assertEqual(len(runs), 1)
        registrations = self.core.conn.execute("SELECT COUNT(*) FROM stage0_competitor_registration").fetchone()[0]
        self.assertEqual(registrations, 20)
        contract = self.core.conn.execute("SELECT * FROM stage0_cold_start_run_contract").fetchone()
        self.assertIsNotNone(contract)
        self.assertEqual(contract["cold_start_contract_version"], COLD_START_CONTRACT_VERSION)
        self.assertEqual(len(json.loads(contract["competitor_account_ids_json"])), 20)

        continued = self.service.continue_current_cold_start(
            configuration_id=result["configuration_id"], actor="用户"
        )
        self.assertEqual(continued["cold_start_id"], result["cold_start_id"])
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 1)

    def test_create_and_lifecycle_need_no_operator_identity(self) -> None:
        payload = self.payload(domain="无操作者冷启动")
        payload.pop("actor", None)
        preview = self.preview(payload)
        self.assertTrue(preview["ready_to_confirm"])
        created = self.service.confirm(payload)
        self.assertEqual(
            self.service.current_cold_start_status(
                cold_start_id=created["cold_start_id"]
            )["cold_start_id"],
            created["cold_start_id"],
        )
        stopped = self.service.stop_current_cold_start(
            reason="无操作者停止",
            cold_start_id=created["cold_start_id"],
        )
        self.assertTrue(stopped["stopped"])
        resumed = self.service.resume_current_cold_start(
            cold_start_id=created["cold_start_id"]
        )
        self.assertTrue(resumed["resumed"])
        self.assertFalse(resumed["created_new_run"])

    def test_resume_current_user_run_reuses_existing_run_and_stage(self) -> None:
        payload = self.payload(domain="可恢复运行")
        preview = self.preview(payload)
        created = self.service.confirm(payload)
        self.assertEqual(created["status"], "running")
        self.service.stop_current_cold_start(
            actor="",
            reason="隔离恢复前停止",
            cold_start_id=created["cold_start_id"],
        )
        resumed = self.service.resume_current_cold_start(
            actor="", cold_start_id=created["cold_start_id"]
        )
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["cold_start_id"], created["cold_start_id"])
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_competitor_registration").fetchone()[0],
            20,
        )

    def test_resume_without_exact_run_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "exact cold_start_id"):
            self.service.resume_current_cold_start(actor="用户")
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            0,
        )

    def test_counts_are_exactly_twenty(self) -> None:
        for count in (0, 10, 19, 21):
            report = self.preview(self.payload(domain=f"领域{count}", count=count))
            self.assertFalse(report["ready_to_confirm"])
            self.assertIn("competitor_account_count", {item["code"] for item in report["blockers"]})
        self.assertTrue(self.preview(self.payload(domain="领域20"))["ready_to_confirm"])

    def test_duplicate_accounts_and_owned_overlap_are_rejected(self) -> None:
        payload = self.payload(domain="重复账号")
        payload["competitor_accounts"][1]["external_account_ref"] = payload["competitor_accounts"][0]["external_account_ref"]
        self.assertIn("duplicate_accounts", {item["code"] for item in self.preview(payload)["blockers"]})
        payload = self.payload(domain="自营重叠")
        payload["competitor_accounts"][0]["external_account_ref"] = payload["owned_account"]["external_account_ref"]
        self.assertIn("duplicate_accounts", {item["code"] for item in self.preview(payload)["blockers"]})

    def test_old_boundary_description_is_not_required_and_owned_history_is_not_checked(self) -> None:
        payload = self.payload(domain="无历史自营账号")
        self.assertNotIn("domain_boundary", payload)
        preview = self.preview(payload)
        self.assertNotIn("domain_boundary", preview["normalized"])
        self.assertTrue(all("boundary" not in item for item in preview["overlap_suggestions"]))
        result = self.service.confirm(payload)
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start_configuration WHERE configuration_id=?",
                (result["configuration_id"],),
            ).fetchone()["status"],
            "started",
        )

    def test_new_request_after_existing_run_is_rejected(self) -> None:
        payload = self.payload(domain="已有运行")
        first = self.preview(payload)
        self.service.confirm(payload)
        changed = self.payload(domain="已有运行", actor="另一位用户")
        report = self.preview(changed)
        with self.assertRaisesRegex(StateTransitionError, "unresolved required items"):
            self.service.confirm(changed)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 1)

    def test_cross_domain_account_is_rejected_without_overwrite(self) -> None:
        first_payload = self.payload(domain="账号原领域")
        first = self.preview(first_payload)
        self.service.confirm(first_payload)
        second_payload = self.payload(domain="账号新领域")
        second = self.preview(second_payload)
        with self.assertRaisesRegex(StateTransitionError, "already belongs to domain"):
            self.service.confirm(second_payload)
        row = self.core.conn.execute(
            "SELECT domain_label FROM stage0_content_account WHERE external_account_ref=?",
            ("douyin:owned-account",),
        ).fetchone()
        self.assertEqual(row["domain_label"], "domain_" + __import__("hashlib").sha256("账号原领域".encode()).hexdigest()[:12])
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 1)

    def test_single_account_path_cannot_create_complete_cold_start(self) -> None:
        payload = self.payload(domain="单账号维护", count=20)
        preview = self.preview(payload)
        self.service.confirm(payload)
        with self.assertRaises(StateTransitionError):
            self.core.start_cold_start(
                cold_start_id="single-account-run",
                owned_account_id="missing-owned",
                competitor_account_ids=("one",),
                actor="用户",
            )
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 1)

    def test_failed_start_leaves_no_formal_run_or_domain_pack_and_can_retry(self) -> None:
        payload = self.payload(domain="失败后可重试")
        preview = self.preview(payload)
        with patch.object(
            ColdStartOnboardingService,
            "_automatic_start",
            side_effect=StateTransitionError("模拟启动前检查失败"),
        ):
            with self.assertRaisesRegex(StateTransitionError, "模拟启动前检查失败"):
                self.service.confirm(payload)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0], 0)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start_configuration").fetchone()[0], 0)
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_content_account").fetchone()[0], 0)
        self.assertEqual(list(self.config_dir.glob("*.yaml")), [])
        self.assertEqual(self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start_onboarding_failure").fetchone()[0], 1)
        retry_preview = self.preview(payload)
        retry = self.service.confirm(payload)
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_cold_start_configuration WHERE configuration_id=?",
                (retry["configuration_id"],),
            ).fetchone()["status"],
            "started",
        )

    def test_existing_human_decision_entry_previews_and_confirms_once(self) -> None:
        payload = self.payload(domain="既有人工入口")
        preview = self.adapter.preview_configuration(payload)
        self.assertTrue(preview["ready_to_confirm"])
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            0,
        )
        result = self.adapter.confirm_configuration(
            configuration=preview["normalized"],
            transport_actor="用户",
        )
        self.assertTrue(result["automatic_start"])
        self.assertEqual(result["status"], "running")
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_human_decision_command").fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
