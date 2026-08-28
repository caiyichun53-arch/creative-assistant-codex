"""3B-1 content-type lifecycle checks on isolated cold-start data only."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    get_content_type_registry,
    project_content_type,
    set_domain_pack_config_dir,
)
from scripts.core.production.human_decision_entry import (
    ColdStartHumanDecisionAdapter,
    FormalHumanDecisionCommand,
)
from scripts.core.production.cold_start_orchestrator import ColdStartExecutionOrchestrator
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)


class ContentTypeColdStart3B1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "domain_packs"
        self.config_dir.mkdir()
        self.domain_label = "isolated_content_type_3b1"
        (self.config_dir / f"{self.domain_label}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "3B-1隔离领域",
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
        self._insert_breakdown(
            "current-registration", "current-person-story", "人物故事", "person", "story"
        )
        self._insert_breakdown(
            "current-registration", "current-person-profile", "人物经历", "person", "profile"
        )
        self._insert_breakdown(
            "current-registration", "current-work-background", "作品背景说明", "work", "explanation"
        )
        self._insert_breakdown(
            "current-registration", "current-missing-type", "", "event", "analysis"
        )
        self._insert_breakdown(
            "current-registration", "current-failed", "事件解读", "event", "analysis", status="failed"
        )
        self._insert_breakdown(
            "old-registration", "old-event", "事件解读", "event", "analysis"
        )
        self._insert_breakdown(
            "fixture-registration", "fixture-case", "案例故事", "case", "story", data_identity="fixture"
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
        source_content_type: str,
        subject: str,
        expression: str,
        *,
        data_identity: str = "test",
        status: str = "completed",
    ) -> None:
        artifact = {
            "deep_breakdown": {
                "source_id": source_id,
                "source_content_type": source_content_type,
                "content_subject_type": subject,
                "expression_form": expression,
                "content_type_evidence": [f"{source_id}-evidence"],
            }
        }
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'breakdown', ?, ?, ?, '{}', 1, ?, ?)",
            (
                registration_id,
                source_id,
                status,
                json.dumps(artifact, ensure_ascii=False),
                data_identity,
                "2026-08-21T00:00:00+08:00",
            ),
        )

    def _prepare_carrier(self, carrier_id: str = "3b1-carrier") -> ColdStartHumanDecisionAdapter:
        self.core.propose_human_decision_carrier(
            carrier_binding_id=carrier_id,
            carrier_kind="isolated_test",
            entry_ref="3b1-test",
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
                "evidence_ref": "3b1-round-trip",
            },
            actor="隔离测试",
            actor_kind="user",
        )
        return ColdStartHumanDecisionAdapter(core=self.core, config_dir=self.config_dir)

    def test_only_current_run_successful_breakdowns_form_candidates(self) -> None:
        result = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )
        self.assertEqual(result["status"], "awaiting_human_decision")
        self.assertFalse(result["proposal"]["model_used"])
        self.assertEqual(result["proposal"]["source_rule"], "current_cold_start_successful_breakdown_observations_only")
        self.assertEqual(
            {item["source_id"] for item in result["source_snapshot"]["observations"]},
            {"current-person-story", "current-person-profile", "current-work-background"},
        )
        self.assertNotIn("old-event", json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(
            sorted(item["support_sample_count"] for item in result["candidates"]),
            [1, 2],
        )
        self.assertEqual(len(result["proposal"]["observation_assignments"]), 3)
        self.assertEqual(len(result["proposal"]["ignored_observations"]), 1)

    def test_low_frequency_candidate_is_not_dropped_and_generation_is_idempotent(self) -> None:
        first = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )
        second = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )
        self.assertEqual(first["content_type_candidate_id"], second["content_type_candidate_id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_content_type_candidate WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            1,
        )
        self.assertIn(1, [item["support_sample_count"] for item in first["candidates"]])

    def test_foreign_source_cannot_enter_candidate_proposal(self) -> None:
        result = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )
        proposal = json.loads(json.dumps(result["proposal"], ensure_ascii=False))
        proposal["candidates"][0]["source_refs"][0]["source_id"] = "old-event"
        with self.assertRaises(StateTransitionError):
            self.core._validate_cold_start_content_type_proposal(
                observations=result["source_snapshot"]["observations"], proposal=proposal
            )

    def test_whole_review_freezes_the_same_registry_and_keeps_cold_start_open(self) -> None:
        candidate_set = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-current", actor="隔离测试"
        )
        person = next(item for item in candidate_set["candidates"] if item["support_sample_count"] == 2)
        other = next(item for item in candidate_set["candidates"] if item["candidate_id"] != person["candidate_id"])
        adapter = self._prepare_carrier()
        command = FormalHumanDecisionCommand(
            command_id="3b1-review-1",
            carrier_binding_id="3b1-carrier",
            session_ref="3b1-session",
            action="review_cold_start_content_types",
            target_ref="cold_start:cold-current",
            payload={
                "cold_start_id": "cold-current",
                "decisions": [
                    {
                        "candidate_id": person["candidate_id"],
                        "decision": "accepted",
                        "edited_name": "人物经历故事",
                        "edited_definition": "讲清人物的具体经历、转折和推进过程。",
                    },
                    {"candidate_id": other["candidate_id"], "decision": "rejected"},
                ],
                "reason": "隔离整版确认",
            },
            actor="隔离测试",
        )
        reviewed = adapter.review_content_types(command=command)
        self.assertEqual(reviewed["status"], "frozen")
        self.assertTrue(self.core.cold_start_content_types_are_frozen(cold_start_id="cold-current"))
        registry = get_content_type_registry(self.domain_label)
        self.assertEqual(registry["status"], "FROZEN")
        self.assertEqual([item["name"] for item in registry["types"]], ["人物经历故事"])
        self.assertEqual(registry["provenance"]["cold_start_id"], "cold-current")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            "running",
        )
        self.assertEqual(
            project_content_type(
                self.domain_label,
                lifecycle="classify",
                canonical_id=registry["types"][0]["canonical_id"],
            )["status"],
            "matched",
        )
        replay = adapter.review_content_types(command=command)
        self.assertEqual(replay["status"], "frozen")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_content_type_candidate WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            1,
        )

    def test_no_observation_is_retained_as_failure_and_can_retry_after_new_success(self) -> None:
        self._insert_account("empty-account", "empty", "test")
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, 'running', ?, 'fixture', ?, NULL)",
            ("cold-empty", "empty-account", self.domain_label, "test", "2026-08-21T00:00:00+08:00"),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?)",
            ("empty-registration", "cold-empty", "empty-account", "test", "2026-08-21T00:00:00+08:00", "2026-08-21T00:00:00+08:00"),
        )
        failed = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-empty", actor="隔离测试"
        )
        self.assertEqual(failed["status"], "failed")
        self._insert_breakdown("empty-registration", "new-success", "案例故事", "case", "story")
        retried = self.core.build_cold_start_content_type_candidates(
            cold_start_id="cold-empty", actor="隔离测试"
        )
        self.assertEqual(retried["status"], "awaiting_human_decision")
        self.assertEqual(retried["candidate_version"], "v2")

    def test_orchestrator_creates_one_review_candidate_after_current_run_breakdowns(self) -> None:
        now = "2026-08-21T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', ?, ?, ?)",
            (
                "orchestrator-owned",
                "orchestrator-owned",
                self.domain_label,
                "douyin:orchestrator-owned",
                "test",
                "fixture",
                now,
            ),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, 'running', ?, 'fixture', ?, NULL)",
            ("cold-orchestrator", "orchestrator-owned", self.domain_label, "test", now),
        )
        for index in range(20):
            account_id = f"orchestrator-account-{index}"
            self._insert_account(account_id, f"orchestrator-{index}", "test")
            registration_id = f"orchestrator-registration-{index}"
            self.connection.execute(
                "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?)",
                (registration_id, "cold-orchestrator", account_id, "test", now, now),
            )
            for step_name in (
                "historical_material",
                "high_signal_identification",
                "transcripts_and_comments",
                "breakdown",
            ):
                self.connection.execute(
                    "INSERT INTO stage0_competitor_registration_step "
                    "(step_record_id, registration_id, step_name, artifact_refs_json, integrity_hash, completed_by, completed_at, data_identity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"step-{index}-{step_name}",
                        registration_id,
                        step_name,
                        json.dumps([{"step": step_name}], ensure_ascii=False),
                        f"hash-{index}-{step_name}",
                        "fixture",
                        now,
                        "test",
                    ),
                )
            self._insert_breakdown(
                registration_id,
                f"orchestrator-source-{index}",
                "人物故事" if index % 2 == 0 else "人物经历",
                "person",
                "story" if index % 2 == 0 else "profile",
            )

        class NoopRegistrationService:
            def run_competitor_registration_step(self, **_: object) -> dict[str, object]:
                raise AssertionError("a completed current run must not re-run account steps")

        result = ColdStartExecutionOrchestrator(
            core=self.core,
            registration_service=NoopRegistrationService(),
        ).run(
            cold_start_id="cold-orchestrator",
            actor="隔离测试",
            idempotency_key="3b1-orchestrator",
        )
        self.assertTrue(result["content_type_branch"]["ready"])
        self.assertEqual(
            result["content_type_branch"]["candidate"]["status"],
            "awaiting_human_decision",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_cold_start_content_type_candidate "
                "WHERE cold_start_id='cold-orchestrator'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
