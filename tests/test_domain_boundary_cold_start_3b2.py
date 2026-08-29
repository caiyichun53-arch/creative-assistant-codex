"""3B-2 domain production-boundary lifecycle on isolated current-run data."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.core.business_data.domain_boundaries import (
    get_production_boundary_registry,
    require_frozen_production_boundary,
)
from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.cold_start_orchestrator import ColdStartExecutionOrchestrator
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage0_content_core import StateTransitionError
from scripts.core.production.stage1b_daily_discovery import (
    DailyDiscoveryValidationError,
    Stage1BDailyDiscoveryService,
)


class DomainBoundaryColdStart3B2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "domain_packs"
        self.config_dir.mkdir()
        self.domain_label = "isolated_boundary_3b2"
        (self.config_dir / f"{self.domain_label}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "3B-2隔离领域",
                    "formal_domain_label": self.domain_label,
                    "activation_status": "approved",
                    "workflow_mode": "manual_guard",
                    "runtime_requirements": [],
                    "discovery": {
                        "hotspot_match_terms": [],
                        "risk_block_terms": [],
                        "exclude_terms": [],
                        "topic_search": {},
                    },
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection, db_path=Path(":memory:"), data_identity="test"
        )
        self.core.install_schema()
        self._insert_account("current-account", "current", "test")
        self._insert_account("old-account", "old", "test")
        self._insert_account("fixture-account", "fixture", "fixture")
        self._insert_run("cold-current", "current-account", "test", "current-registration")
        self._insert_run("cold-old", "old-account", "test", "old-registration")
        self._insert_run("cold-fixture", "fixture-account", "fixture", "fixture-registration")
        self._insert_breakdown("current-registration", "current-source-1", "Current evidence")
        self._insert_breakdown("current-registration", "current-source-2", "Current second evidence")
        self._insert_breakdown("old-registration", "old-source", "Old evidence")
        self._insert_breakdown(
            "fixture-registration", "fixture-source", "Fixture evidence", data_identity="fixture"
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    def _insert_account(self, account_id: str, name: str, data_identity: str) -> None:
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'competitor', ?, ?, ?, 'active', ?, ?, ?)",
            (
                account_id,
                name,
                self.domain_label,
                f"douyin:{account_id}",
                data_identity,
                "fixture",
                "2026-08-21T00:00:00+08:00",
            ),
        )

    def _insert_run(
        self, cold_start_id: str, account_id: str, data_identity: str, registration_id: str
    ) -> None:
        now = "2026-08-21T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, 'running', ?, 'fixture', ?, NULL)",
            (cold_start_id, account_id, self.domain_label, data_identity, now),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?)",
            (registration_id, cold_start_id, account_id, data_identity, now, now),
        )

    def _insert_breakdown(
        self,
        registration_id: str,
        source_id: str,
        analysis_text: str,
        *,
        data_identity: str = "test",
    ) -> None:
        artifact = {
            "deep_breakdown": {
                "source_id": source_id,
                "analysis_text": analysis_text,
                "question_expansions": [{"question": f"{source_id} question"}],
                "expansion_signals": [{"signal": "relationship"}],
                "typed_expansion_leads": [{"lead": "specific relationship"}],
                "content_type_evidence": ["not used as boundary evidence"],
                "boundary_observation": f"{source_id} 对问题性质边界提供了新的自然语言观察。",
            }
        }
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'breakdown', ?, 'completed', ?, '{}', 1, ?, ?)",
            (
                registration_id,
                source_id,
                json.dumps(artifact, ensure_ascii=False),
                data_identity,
                "2026-08-21T00:00:00+08:00",
            ),
        )

    def _prepare_carrier(self, carrier_id: str = "3b2-carrier") -> ColdStartHumanDecisionAdapter:
        self.core.propose_human_decision_carrier(
            carrier_binding_id=carrier_id,
            carrier_kind="isolated_test",
            entry_ref="3b2-test",
            context_strategy="same_test_session",
            actor="隔离测试",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id=carrier_id,
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "3b2-round-trip",
            },
            actor="隔离测试",
            actor_kind="user",
        )
        return ColdStartHumanDecisionAdapter(core=self.core, config_dir=self.config_dir)

    def _build(self) -> dict[str, object]:
        return self.core.build_cold_start_domain_boundary_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )

    def _freeze(self) -> dict[str, object]:
        candidate = self._build()
        proposal = candidate["proposal"]
        in_id = proposal["in_boundary_principles"][0]["boundary_id"]
        out_id = proposal["out_boundary_principles"][0]["boundary_id"]
        adapter = self._prepare_carrier()
        command = FormalHumanDecisionCommand(
            command_id="3b2-boundary-review-1",
            carrier_binding_id="3b2-carrier",
            session_ref="3b2-session",
            action="review_cold_start_domain_boundary",
            target_ref="cold_start:cold-current",
            payload={
                "cold_start_id": "cold-current",
                "decisions": [
                    {
                        "boundary_id": in_id,
                        "decision": "accepted",
                        "edited_principle": "未来新题材只要其问题性质符合当前样本所体现的对象关系、变化或影响原则，就属于可生产范围。",
                    },
                    {"boundary_id": out_id, "decision": "accepted"},
                    {
                        "boundary_id": "user-added-in",
                        "decision": "accepted",
                        "origin": "user_confirmation",
                        "polarity": "in",
                        "principle": "能让普通用户看清一个具体问题如何发生、变化或产生影响的题目，可以进入生产范围。",
                        "rationale": "用户确认补充的抽象判断原则。",
                    },
                    {
                        "boundary_id": "user-added-out",
                        "decision": "accepted",
                        "origin": "user_confirmation",
                        "polarity": "out",
                        "principle": "只有表面名称关联、没有可说明问题关系或影响的题目，不属于生产范围。",
                        "rationale": "用户确认补充的排除原则。",
                    },
                ],
                "reason": "隔离整版确认",
                "unknown_topic_rule": {
                    "rule": "未出现过的新题材按问题性质原则判断，不按是否出现在样本中判断。",
                    "uncertain_action": "先进入用户确认，不自行放行或拒绝。",
                },
            },
            actor="隔离测试",
        )
        self.review_command = command
        return adapter.review_domain_boundary(command=command)

    @staticmethod
    def _explicit_boundary() -> dict[str, object]:
        return {
            "in_boundary_principles": [{
                "boundary_id": "explicit-in",
                "principle": "围绕音乐作品、音乐人物或音乐事件中的具体经历、关系、变化与影响来生产。",
                "rationale": "这是用户明确提交的生产范围原则。",
            }],
            "out_boundary_principles": [{
                "boundary_id": "explicit-out",
                "principle": "只有名称或标签与音乐相关、但没有可说明的音乐经历、关系、变化或影响，不进入生产范围。",
                "rationale": "这是用户明确提交的排除范围原则。",
            }],
            "unknown_topic_rule": {
                "rule": "未出现过的新题材按问题性质是否符合上述原则判断。",
                "uncertain_action": "无法判断时先交给用户确认，不自行放行或拒绝。",
            },
        }

    def _direct_boundary_command(self) -> FormalHumanDecisionCommand:
        return FormalHumanDecisionCommand(
            command_id="3b2-direct-boundary-review-1",
            carrier_binding_id="3b2-direct-carrier",
            session_ref="3b2-direct-session",
            action="review_cold_start_domain_boundary",
            target_ref="cold_start:cold-current",
            payload={
                "cold_start_id": "cold-current",
                "explicit_boundary": self._explicit_boundary(),
                "reason": "隔离测试的明确生产边界决定",
            },
            actor="隔离测试",
        )

    def test_completed_run_without_candidate_accepts_explicit_human_boundary(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id='cold-current'"
            )
        adapter = self._prepare_carrier("3b2-direct-carrier")
        result = adapter.review_domain_boundary(
            command=self._direct_boundary_command()
        )

        self.assertEqual(result["status"], "frozen")
        self.assertEqual(result["source"], "explicit_human_decision")
        self.assertNotIn("boundary_candidate_id", result)
        registry = require_frozen_production_boundary(self.domain_label)
        self.assertEqual(registry["version"], "1")
        self.assertEqual(
            registry["provenance"]["source"], "explicit_human_decision"
        )
        self.assertEqual(
            registry["provenance"]["cold_start_id"], "cold-current"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            "completed",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_domain_boundary_candidate "
                "WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_human_decision_command "
                "WHERE command_id='3b2-direct-boundary-review-1'"
            ).fetchone()[0],
            "completed",
        )

    def test_direct_boundary_without_actual_content_cannot_freeze(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id='cold-current'"
            )
        adapter = self._prepare_carrier("3b2-direct-empty-carrier")
        command = FormalHumanDecisionCommand(
            command_id="3b2-direct-empty-boundary-1",
            carrier_binding_id="3b2-direct-empty-carrier",
            session_ref="3b2-direct-session",
            action="review_cold_start_domain_boundary",
            target_ref="cold_start:cold-current",
            payload={
                "cold_start_id": "cold-current",
                "explicit_boundary": {},
                "reason": "空内容不得自动冻结",
            },
            actor="隔离测试",
        )
        with self.assertRaisesRegex(
            StateTransitionError,
            "requires in_boundary_principles",
        ):
            adapter.review_domain_boundary(command=command)

        self.assertEqual(
            get_production_boundary_registry(self.domain_label)["status"],
            "NOT_FROZEN",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_domain_boundary_candidate "
                "WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            0,
        )

    def test_scope_uses_only_current_run_successful_breakdowns(self) -> None:
        result = self.core.get_cold_start_domain_boundary_observations(
            cold_start_id="cold-current"
        )
        self.assertEqual(
            {item["source_id"] for item in result["valid_observations"]},
            {"current-source-1", "current-source-2"},
        )
        self.assertNotIn("old-source", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("fixture-source", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("source_content_type", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("content_type_evidence", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("analysis_text", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("expansion_signals", json.dumps(result, ensure_ascii=False))
        self.assertEqual(
            result["valid_observations"][0]["boundary_observation"],
            "current-source-1 对问题性质边界提供了新的自然语言观察。",
        )

    def test_breakdown_without_boundary_observation_is_not_boundary_evidence(self) -> None:
        self._insert_breakdown(
            "current-registration", "current-source-no-boundary", "No boundary observation"
        )
        row = self.connection.execute(
            "SELECT artifact_json FROM stage0_competitor_registration_item "
            "WHERE registration_id='current-registration' AND item_ref='current-source-no-boundary'"
        ).fetchone()
        artifact = json.loads(row[0])
        artifact["deep_breakdown"].pop("boundary_observation", None)
        self.connection.execute(
            "UPDATE stage0_competitor_registration_item SET artifact_json=? "
            "WHERE registration_id='current-registration' AND item_ref='current-source-no-boundary'",
            (json.dumps(artifact, ensure_ascii=False),),
        )
        result = self.core.get_cold_start_domain_boundary_observations(
            cold_start_id="cold-current"
        )
        self.assertNotIn(
            "current-source-no-boundary",
            {item["source_id"] for item in result["valid_observations"]},
        )
        self.assertIn(
            {"source_id": "current-source-no-boundary", "reason": "boundary_observation_is_empty"},
            result["ignored_observations"],
        )

    def test_abstract_evidence_bound_proposal_and_unsupported_model_direction(self) -> None:
        accepted = self.core.build_cold_start_domain_boundary_candidates(
            cold_start_id="cold-current",
            actor="隔离测试",
            proposal_generator=lambda snapshot: {
                "in_boundary_principles": [{
                    "boundary_id": "abstract-in",
                    "principle": "围绕对象之间的具体关系、变化或影响来判断问题是否属于生产范围。",
                    "rationale": "这是对当前拆解事实的抽象，不是题材枚举。",
                    "evidence_refs": [snapshot["valid_observations"][0]["source_id"]],
                }],
                "out_boundary_principles": [{
                    "boundary_id": "abstract-out",
                    "principle": "只有表面名称相关但没有可说明问题关系或影响的题目，不进入生产范围。",
                    "rationale": "防止把对象名称或标签本身当作生产依据。",
                    "evidence_refs": [snapshot["valid_observations"][1]["source_id"]],
                }],
                "unknown_topic_rule": {
                    "rule": "未出现过的新题材按抽象问题性质判断。",
                    "uncertain_action": "进入用户确认。",
                },
            },
        )
        self.assertEqual(accepted["status"], "awaiting_human_decision")
        self.assertTrue(accepted["proposal"]["model_used"])

        self._insert_account("unsupported-account", "unsupported", "test")
        self._insert_run("cold-unsupported", "unsupported-account", "test", "unsupported-registration")
        self._insert_breakdown("unsupported-registration", "unsupported-source", "Unsupported evidence")
        failed = self.core.build_cold_start_domain_boundary_candidates(
            cold_start_id="cold-unsupported",
            actor="隔离测试",
            proposal_generator=lambda snapshot: {
                "in_boundary_principles": [{
                    "boundary_id": "bad-form",
                    "principle": "用人物故事式来覆盖任何新题材并视为有效。",
                    "rationale": "错误把内容形式当边界。",
                    "evidence_refs": [snapshot["valid_observations"][0]["source_id"]],
                }],
                "out_boundary_principles": [{
                    "boundary_id": "bad-out",
                    "principle": "没有证据的内容不进入范围。",
                    "rationale": "错误候选。",
                    "evidence_refs": ["not-current"],
                }],
                "unknown_topic_rule": {"rule": "按样本出现判断。", "uncertain_action": "拒绝。"},
            },
        )
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["failure"]["retry_allowed"])

    def test_user_review_freezes_independent_boundary_and_is_idempotent(self) -> None:
        frozen = self._freeze()
        self.assertEqual(frozen["status"], "frozen")
        self.assertTrue(self.core.cold_start_domain_boundary_is_frozen(cold_start_id="cold-current"))
        registry = require_frozen_production_boundary(self.domain_label)
        self.assertEqual(registry["status"], "FROZEN")
        self.assertEqual(registry["provenance"]["cold_start_id"], "cold-current")
        self.assertEqual(len(registry["in_boundary_principles"]), 2)
        self.assertEqual(len(registry["out_boundary_principles"]), 2)
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            "running",
        )
        adapter = ColdStartHumanDecisionAdapter(core=self.core, config_dir=self.config_dir)
        replay = adapter.review_domain_boundary(command=self.review_command)
        self.assertEqual(replay["status"], "frozen")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_domain_boundary_candidate WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            1,
        )

    def test_missing_boundary_blocks_formal_assembly_and_frozen_boundary_is_used(self) -> None:
        source = {
            "source_type": "historical_high_signal",
            "payload": {"title": "一个可追溯的具体问题", "url": "https://example.invalid/source"},
        }
        with self.assertRaises(DailyDiscoveryValidationError):
            Stage1BDailyDiscoveryService._assembly_payload(
                run_id="daily-run",
                domain_label=self.domain_label,
                source=source,
                source_version_id="source-version",
            )
        self._freeze()
        payload = Stage1BDailyDiscoveryService._assembly_payload(
            run_id="daily-run",
            domain_label=self.domain_label,
            source=source,
            source_version_id="source-version",
        )
        self.assertIn("正式生产边界", payload["domain_rule_summary"])
        in_id = get_production_boundary_registry(self.domain_label)["in_boundary_principles"][0]["boundary_id"]
        out_id = get_production_boundary_registry(self.domain_label)["out_boundary_principles"][0]["boundary_id"]
        unseen = self.core.evaluate_current_production_boundary(
            domain_label=self.domain_label,
            outcome="in_boundary",
            matched_principle_ids=[in_id],
            basis="这是样本未出现过的新题材，但问题性质符合冻结原则。",
        )
        self.assertEqual(unseen["status"], "in_boundary")
        self.assertFalse(unseen["used_keyword_whitelist"])
        out = self.core.evaluate_current_production_boundary(
            domain_label=self.domain_label,
            outcome="out_of_boundary",
            violated_principle_ids=[out_id],
            basis="名称相似，但只有表面关联，没有冻结原则要求的问题关系。",
        )
        self.assertEqual(out["status"], "out_of_boundary")

    def test_orchestrator_exposes_boundary_review_after_breakdown_without_freezing_or_completion(self) -> None:
        now = "2026-08-21T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', ?, ?, ?)",
            ("orchestrator-owned", "owned", self.domain_label, "douyin:owned", "test", "fixture", now),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, 'running', ?, 'fixture', ?, NULL)",
            ("cold-orchestrator", "orchestrator-owned", self.domain_label, "test", now),
        )
        for index in range(20):
            account_id = f"orchestrator-account-{index}"
            registration_id = f"orchestrator-registration-{index}"
            self._insert_account(account_id, f"orchestrator-{index}", "test")
            self.connection.execute(
                "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?)",
                (registration_id, "cold-orchestrator", account_id, "test", now, now),
            )
            for step_name in (
                "historical_material", "high_signal_identification",
                "transcripts_and_comments", "breakdown",
            ):
                self.connection.execute(
                    "INSERT INTO stage0_competitor_registration_step "
                    "(step_record_id, registration_id, step_name, artifact_refs_json, integrity_hash, completed_by, completed_at, data_identity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"step-{index}-{step_name}", registration_id, step_name,
                        json.dumps([{"step": step_name}]), f"hash-{index}-{step_name}",
                        "fixture", now, "test",
                    ),
                )
            self._insert_breakdown(registration_id, f"orchestrator-source-{index}", "当前运行拆解事实")

        class NoopRegistrationService:
            def run_competitor_registration_step(self, **_: object) -> dict[str, object]:
                raise AssertionError("a completed current run must not re-run account steps")

        result = ColdStartExecutionOrchestrator(
            core=self.core,
            registration_service=NoopRegistrationService(),
        ).run(
            cold_start_id="cold-orchestrator",
            actor="隔离测试",
            idempotency_key="3b2-orchestrator",
        )
        self.assertTrue(result["domain_boundary_branch"]["ready"])
        self.assertEqual(
            result["domain_boundary_branch"]["candidate"]["status"],
            "awaiting_human_decision",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-orchestrator'"
            ).fetchone()[0],
            "running",
        )


if __name__ == "__main__":
    unittest.main()
