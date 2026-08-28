"""3B-3 unified cold-start completion checks on isolated data only."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.core.business_data.domain_boundaries import (
    freeze_production_boundary_registry,
)
from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    freeze_content_type_registry,
    set_domain_pack_config_dir,
)
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)


class ColdStartCompletion3B3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "domain_packs"
        self.config_dir.mkdir()
        self.domain_label = "isolated_completion_3b3"
        (self.config_dir / f"{self.domain_label}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "3B-3隔离领域",
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
        self._seed_current_run()

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    def _seed_current_run(self) -> None:
        now = "2026-08-21T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, 'owned', ?, ?, ?, 'active', ?, ?, ?)",
            ("owned", "owned", self.domain_label, "douyin:owned", "test", "fixture", now),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, 'waiting_human', ?, 'fixture', ?, NULL)",
            ("cold-current", "owned", self.domain_label, "test", now),
        )
        for index in range(20):
            account_id = f"competitor-{index}"
            registration_id = f"registration-{index}"
            self.connection.execute(
                "INSERT INTO stage0_content_account VALUES (?, 'competitor', ?, ?, ?, 'active', ?, ?, ?)",
                (account_id, account_id, self.domain_label, f"douyin:{account_id}", "test", "fixture", now),
            )
            self.connection.execute(
                "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, 'completed', 'completed', ?, ?, ?)",
                (registration_id, "cold-current", account_id, "test", now, now),
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
                    (f"step-{index}-{step_name}", registration_id, step_name, "[]", "hash", "fixture", now, "test"),
                )
            item_ref = f"source-{index}"
            for step_name in ("transcripts_and_comments", "breakdown"):
                self.connection.execute(
                    "INSERT INTO stage0_competitor_registration_item "
                    "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
                    "VALUES (?, ?, ?, 'completed', ?, '{}', 1, ?, ?)",
                    (registration_id, step_name, item_ref, json.dumps({"source_id": item_ref}), "test", now),
                )
        self.connection.execute(
            "INSERT INTO stage0_cold_start_tag_library VALUES "
            "('tag-library', 'cold-current', ?, 'fixture', 20, 1, ?, '[]', 'accepted', 'test', 'fixture', ?, 'fixture', ?, 'fixture')",
            (self.domain_label, json.dumps({"retained": []}), now, now),
        )

    def _freeze_type_and_boundary(self) -> None:
        now = "2026-08-21T00:00:00+08:00"
        freeze_content_type_registry(
            self.domain_label,
            [{
                "canonical_id": "type-story",
                "name": "具体故事",
                "core_subject": "一个具体对象或事件",
                "content_promise": "用户得到一条具体、可追溯的故事线",
                "required_delivery": "事实与事件脉络",
                "scope_boundary": "单篇可以讲清一个核心问题",
            }],
            provenance={
                "cold_start_id": "cold-current",
                "content_type_candidate_id": "type-candidate",
                "frozen_at": now,
            },
        )
        freeze_production_boundary_registry(
            self.domain_label,
            in_boundary_principles=[{
                "boundary_id": "boundary-in",
                "principle": "围绕具体对象、事件或关系形成可交付问题。",
                "rationale": "当前样本支持。",
            }],
            out_boundary_principles=[{
                "boundary_id": "boundary-out",
                "principle": "只有表面名称关联、没有具体问题关系的题目不进入。",
                "rationale": "当前样本支持。",
            }],
            unknown_topic_rule={
                "rule": "新题材按冻结的问题性质判断。",
                "uncertain_action": "待用户确认",
            },
            provenance={
                "cold_start_id": "cold-current",
                "boundary_candidate_id": "boundary-candidate",
                "frozen_at": now,
            },
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start_content_type_candidate "
            "(content_type_candidate_id, cold_start_id, domain_label, candidate_version, status, source_snapshot_json, proposal_json, failure_json, review_json, freeze_provenance_json, data_identity, created_by, created_at, reviewed_by, reviewed_at, review_reason) "
            "VALUES ('type-candidate', 'cold-current', ?, 'v1', 'frozen', '{}', '{}', '{}', '{}', ?, 'test', 'fixture', ?, 'fixture', ?, 'fixture')",
            (self.domain_label, json.dumps({"cold_start_id": "cold-current", "content_type_candidate_id": "type-candidate"}), now, now),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start_domain_boundary_candidate "
            "(boundary_candidate_id, cold_start_id, domain_label, candidate_version, status, source_snapshot_json, proposal_json, failure_json, review_json, freeze_provenance_json, model_run_json, data_identity, created_by, created_at, reviewed_by, reviewed_at, review_reason) "
            "VALUES ('boundary-candidate', 'cold-current', ?, 'v1', 'frozen', '{}', '{}', '{}', '{}', ?, '{}', 'test', 'fixture', ?, 'fixture', ?, 'fixture')",
            (self.domain_label, json.dumps({"cold_start_id": "cold-current", "boundary_candidate_id": "boundary-candidate"}), now, now),
        )

    def test_missing_formal_assets_are_reported_without_completion(self) -> None:
        result = self.core.try_complete_cold_start(
            cold_start_id="cold-current", trigger="isolated_check"
        )
        self.assertFalse(result["completed"])
        self.assertIn("content_types_frozen_for_current_run", result["missing_conditions"])
        self.assertIn("production_boundary_frozen_for_current_run", result["missing_conditions"])
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id='cold-current'"
            ).fetchone()[0],
            "waiting_human",
        )

    def test_all_conditions_complete_automatically_and_repeat_is_idempotent(self) -> None:
        self._freeze_type_and_boundary()
        first = self.core.try_complete_cold_start(
            cold_start_id="cold-current", trigger="boundary_freeze", actor="fixture"
        )
        self.assertTrue(first["completed"])
        self.assertEqual(first["status"], "completed")
        second = self.core.try_complete_cold_start(
            cold_start_id="cold-current", trigger="continue", actor="fixture"
        )
        self.assertTrue(second["completed"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_audit_event WHERE action='cold_start_completed_by_unified_judgment'"
            ).fetchone()[0],
            1,
        )



if __name__ == "__main__":
    unittest.main()
