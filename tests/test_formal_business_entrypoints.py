from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.core.formal_business_entrypoints import (
    CreationAssistantFormalBusinessCore,
)
from scripts.core.production.human_decision_entry import FormalHumanDecisionCommand
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)


class FormalBusinessEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "formal-business.sqlite3"
        self.core = Stage0ContentProductionCore.open(
            self.db_path,
            data_identity="test",
        )
        self.business = CreationAssistantFormalBusinessCore(core=self.core)

    def tearDown(self) -> None:
        self.core.close()
        self.tempdir.cleanup()

    def test_same_day_daily_requests_share_one_business_run(self) -> None:
        first = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry-1",
        )
        second = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry-2",
        )

        self.assertEqual(
            first["daily_run"]["daily_run_id"],
            second["daily_run"]["daily_run_id"],
        )
        self.assertEqual(
            self.core.conn.execute(
                "SELECT COUNT(*) FROM stage0_daily_run "
                "WHERE domain_label=? AND business_date=?",
                ("music_entertainment", "2026-08-27"),
            ).fetchone()[0],
            1,
        )

    def test_resume_reactivates_the_same_failed_business_run(self) -> None:
        started = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry",
        )
        daily_run_id = str(started["daily_run"]["daily_run_id"])
        self.business.finish_daily(
            daily_run_id=daily_run_id,
            lifecycle="failed",
            actor="test-entry",
            reason="isolated failure",
        )

        resumed = self.business.resume_daily(
            daily_run_id=daily_run_id,
            actor="test-resume-entry",
        )

        self.assertEqual(resumed["action"], "resumed")
        self.assertEqual(resumed["daily_run"]["daily_run_id"], daily_run_id)
        self.assertEqual(resumed["daily_run"]["lifecycle"], "running")
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_daily_run").fetchone()[0],
            1,
        )

    def test_resume_cannot_create_a_new_business_run(self) -> None:
        with self.assertRaises(StateTransitionError):
            self.business.request_daily(
                domain_label="music_entertainment",
                business_date="2026-08-27",
                actor="test-resume-entry",
                resume=True,
            )

        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_daily_run").fetchone()[0],
            0,
        )

    def test_completed_business_run_is_reused_without_new_run(self) -> None:
        started = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry",
        )
        daily_run_id = str(started["daily_run"]["daily_run_id"])
        self.business.finish_daily(
            daily_run_id=daily_run_id,
            lifecycle="completed",
            actor="test-entry",
        )

        repeated = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry-again",
        )

        self.assertEqual(repeated["action"], "skipped_completed")
        self.assertEqual(repeated["daily_run"]["daily_run_id"], daily_run_id)
        self.assertEqual(
            self.core.conn.execute("SELECT COUNT(*) FROM stage0_daily_run").fetchone()[0],
            1,
        )

    def test_human_decision_is_validated_and_finished_by_core(self) -> None:
        self.core.propose_human_decision_carrier(
            carrier_binding_id="isolated-carrier",
            carrier_kind="test",
            entry_ref="test-entry",
            context_strategy="same-test-session",
            actor="test-user",
        )
        self.core.validate_human_decision_carrier(
            carrier_binding_id="isolated-carrier",
            validation_evidence={
                "inbound_round_trip": True,
                "outbound_round_trip": True,
                "same_context_verified": True,
                "decision_identity_verified": True,
                "evidence_ref": "isolated-round-trip",
            },
            actor="test-user",
            actor_kind="user",
        )
        result = self.business.submit_human_decision(
            command=FormalHumanDecisionCommand(
                command_id="decision-1",
                carrier_binding_id="isolated-carrier",
                session_ref="session-1",
                action="confirm_formal_topic",
                target_ref="topic-1",
                payload={"confirmed": True},
                actor="test-user",
            ),
            apply_formal_decision=lambda _command: {"accepted": True},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            self.core.conn.execute(
                "SELECT status FROM stage0_human_decision_command WHERE command_id=?",
                ("decision-1",),
            ).fetchone()[0],
            "completed",
        )

    def test_core_rejects_an_illegal_completed_to_running_transition(self) -> None:
        started = self.business.request_daily(
            domain_label="music_entertainment",
            business_date="2026-08-27",
            actor="test-entry",
        )
        daily_run_id = str(started["daily_run"]["daily_run_id"])
        self.business.finish_daily(
            daily_run_id=daily_run_id,
            lifecycle="completed",
            actor="test-entry",
        )

        with self.assertRaises(StateTransitionError):
            self.business.finish_daily(
                daily_run_id=daily_run_id,
                lifecycle="running",
                actor="test-entry",
            )

    def test_boundary_does_not_import_external_transport(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath(
            "scripts", "core", "formal_business_entrypoints.py"
        )
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("scripts.agent_platform", text)
        self.assertNotIn("scripts.mcp", text)
        self.assertNotIn("scripts.web", text)


if __name__ == "__main__":
    unittest.main()
