import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, formal_domain_labels
from scripts.core.production.high_signal_policy import (
    build_high_signal_artifact,
    build_historical_collection_artifact,
)
from scripts.core.production.stage1_competitor_registration import CompetitorRegistrationService


class _FastExecutor:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        evaluated_at = int(time.time())
        self.history = build_historical_collection_artifact(
            platform="douyin",
            account_source_ref="douyin:fixture",
            items=[
                {
                    "source_id": f"source-{index}",
                    "title": f"source-{index}",
                    "url": f"https://example.test/{index}",
                    "published_at": evaluated_at - 30 * 86400 - index,
                    "metrics": {
                        "likes": 1000,
                        "comments": 100,
                        "shares": 100,
                        "collects": 100,
                        "followers": 1000,
                    },
                }
                for index in range(20)
            ],
            raw_archive_ref="archive",
            raw_archive_refs=["archive"],
            command_hash="command",
            output_hash="output",
            evaluated_at=evaluated_at,
            collection_status="target_reached",
            page_request_count=1,
        )

    def execute(self, *, step_name, registration, completed_artifacts):
        self.calls.append((str(registration["registration_id"]), step_name))
        del completed_artifacts
        artifacts = {
            "historical_material": self.history,
            "high_signal_identification": build_high_signal_artifact(self.history["items"]),
            "transcripts_and_comments": {
                "artifact_kind": "transcripts_and_comments",
                "items": [],
            },
            "breakdown": {
                "artifact_kind": "breakdown",
                "items": [],
            },
        }
        return (artifacts[step_name],)


class IncrementalCompetitorRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.temp.close()
        self.path = Path(self.temp.name)
        self.event_log_path = self.path.with_name("business_runtime_guard_events.jsonl")
        self.event_log_patch = patch(
            "scripts.core.production.business_runtime_guard.EVENT_LOG_PATH",
            self.event_log_path,
        )
        self.event_log_patch.start()
        self.core = Stage0ContentProductionCore.open(self.path, data_identity="test")
        self.domain = next(iter(formal_domain_labels()))
        self.core.register_content_account(
            content_account_id="owned",
            account_role="owned",
            display_name="owned",
            domain_label=self.domain,
            external_account_ref="douyin:owned",
            actor="fixture",
        )
        self.competitors = []
        for index in range(20):
            account_id = f"competitor-{index}"
            self.competitors.append(account_id)
            self.core.register_content_account(
                content_account_id=account_id,
                account_role="competitor",
                display_name=account_id,
                domain_label=self.domain,
                external_account_ref=f"douyin:{account_id}",
                actor="fixture",
            )
        self.run_id = "cold-start-completed"
        self.core.start_cold_start(
            cold_start_id=self.run_id,
            owned_account_id="owned",
            competitor_account_ids=tuple(self.competitors),
            actor="fixture",
            run_contract={
                "domain_label": self.domain,
                "cold_start_contract_version": "test-v1",
                "input_snapshot": {"run_model": {"provider": "fixture", "model": "fixture"}},
            },
        )
        now = "2026-01-01T00:00:00+00:00"
        self.core.conn.execute(
            "INSERT INTO stage0_cold_start_configuration("
            "configuration_id, domain_mode, domain_label, domain_name, domain_boundary, "
            "platform, owned_account_id, competitor_account_ids_json, status, data_identity, confirmed_by, "
            "confirmed_at, cold_start_id) VALUES (?, 'reuse', ?, ?, ?, 'douyin', ?, ?, 'completed', ?, ?, ?, ?)",
            (
                "configuration",
                self.domain,
                self.domain,
                "boundary",
                "owned",
                __import__("json").dumps(self.competitors),
                "test",
                "fixture",
                now,
                self.run_id,
            ),
        )
        self.core.conn.execute(
            "UPDATE stage0_cold_start SET status='completed' WHERE cold_start_id=?",
            (self.run_id,),
        )
        self.core.conn.commit()

    def tearDown(self):
        self.core.close()
        self.event_log_patch.stop()
        self.event_log_path.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)

    def test_one_and_three_incremental_accounts_stay_in_same_run(self):
        before = self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]
        one = self.core.create_incremental_competitor_registrations(
            cold_start_id=self.run_id,
            competitor_accounts=(
                {"display_name": "new-1", "external_account_ref": "douyin:new-1"},
            ),
            actor="fixture",
            idempotency_key="incremental-one",
        )
        three = self.core.create_incremental_competitor_registrations(
            cold_start_id=self.run_id,
            competitor_accounts=tuple(
                {"display_name": f"new-{index}", "external_account_ref": f"douyin:new-{index}"}
                for index in range(2, 5)
            ),
            actor="fixture",
            idempotency_key="incremental-three",
        )
        after = self.core.conn.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]
        rows = self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration WHERE cold_start_id=?",
            (self.run_id,),
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(one["account_count"], 1)
        self.assertEqual(three["account_count"], 3)
        self.assertEqual(rows, 24)
        self.assertFalse(one["created_new_run"])
        self.assertFalse(three["created_new_run"])
        self.assertEqual(
            self.core.get_cold_start_orchestration_status(cold_start_id=self.run_id)["registration_total"],
            20,
        )

    def test_service_reuses_existing_registration_steps_for_one_account(self):
        service = CompetitorRegistrationService(core=self.core, executor=_FastExecutor())
        result = service.run_incremental_competitor_registrations(
            cold_start_id=self.run_id,
            competitor_accounts=(
                {
                    "display_name": "new-service",
                    "external_account_ref": "douyin:new-service",
                },
            ),
            actor="fixture",
            idempotency_key="incremental-service",
        )
        self.assertFalse(result["created_new_run"])
        self.assertEqual(result["account_count"], 1)
        registration_id = result["registration_ids"][0]
        row = self.core.get_competitor_registration(registration_id=registration_id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["current_step"], "completed")
        original_rows = self.core.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration WHERE cold_start_id=? AND competitor_account_id IN (%s)"
            % ",".join("?" for _ in self.competitors),
            (self.run_id, *self.competitors),
        ).fetchone()[0]
        self.assertEqual(original_rows, 20)


    def test_three_incremental_accounts_share_the_same_phase_order(self):
        executor = _FastExecutor()
        service = CompetitorRegistrationService(core=self.core, executor=executor)
        result = service.run_incremental_competitor_registrations(
            cold_start_id=self.run_id,
            competitor_accounts=tuple(
                {
                    "display_name": f"staged-{index}",
                    "external_account_ref": f"douyin:staged-{index}",
                }
                for index in range(1, 4)
            ),
            actor="fixture",
            idempotency_key="incremental-staged-three",
        )
        self.assertFalse(result["created_new_run"])
        self.assertEqual(result["account_count"], 3)
        self.assertTrue(all(item["status"] == "completed" for item in result["results"]))
        self.assertEqual(len(executor.calls), 12)
        steps = [step for _, step in executor.calls]
        self.assertEqual(steps[:3], ["historical_material"] * 3)
        self.assertEqual(steps[3:6], ["high_signal_identification"] * 3)
        self.assertEqual(steps[6:9], ["transcripts_and_comments"] * 3)
        self.assertEqual(steps[9:], ["breakdown"] * 3)

if __name__ == "__main__":
    unittest.main()
