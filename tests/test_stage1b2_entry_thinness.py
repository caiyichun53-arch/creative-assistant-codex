from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Stage1B2EntryThinnessTests(unittest.TestCase):
    def test_core_has_no_import_back_to_old_transport_entries(self) -> None:
        core_root = PROJECT_ROOT / "scripts" / "core"
        for source in core_root.rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("from scripts.agent_platform", text, str(source))
            self.assertNotIn("import scripts.agent_platform", text, str(source))

    def test_active_old_entries_do_not_own_formal_lifecycle_calls(self) -> None:
        active_entries = (
            "scripts/agent_platform/daily_operations_runtime.py",
            "scripts/agent_platform/run_daily_collection_once.py",
            "scripts/agent_platform/hermes_formal_research_entry.py",
            "scripts/agent_platform/hermes_daily_repair_entry.py",
            "scripts/agent_platform/hermes_knowledge_convergence_entry.py",
            "scripts/agent_platform/hermes_cold_start_action.py",
            "scripts/agent_platform/cold_start_background_entry.py",
        )
        forbidden_direct_calls = (
            "get_or_create_daily_run",
            "start_daily_run",
            "finish_daily_run",
            "resume_stopped_cold_start",
            "start_configured_cold_start",
            "stop_configured_cold_start",
            "fail_configured_cold_start",
            "reconcile_daily_observation_history",
            "converge_knowledge_data",
            "create_discovery_run",
            "complete_discovery_run",
            "record_discovery_decision",
            "select_discovery_candidate",
        )
        for relative in active_entries:
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for call in forbidden_direct_calls:
                self.assertNotIn(f"self.core.{call}", text, f"{relative}: {call}")

    def test_daily_repair_date_eligibility_is_core_owned(self) -> None:
        with TemporaryDirectory() as temp_dir:
            core = Stage0ContentProductionCore.open(
                Path(temp_dir) / "repair.sqlite3", data_identity="test"
            )
            try:
                business = CreationAssistantFormalBusinessCore(core=core)
                self.assertEqual(
                    business.validate_daily_repair_request(
                        as_of_business_date="2026-08-27",
                        now=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
                    ),
                    "2026-08-27",
                )
            finally:
                core.close()

    def test_each_migrated_write_path_calls_the_shared_core_boundary(self) -> None:
        expected_calls = {
            "scripts/agent_platform/daily_operations_runtime.py": (
                "prepare_daily_execution",
                "execute_daily",
                "finalize_daily_execution",
            ),
            "scripts/core/production/stage1b_daily_discovery.py": (
                "execute_daily_discovery",
                "handoff_daily_discovery_candidate",
            ),
            "scripts/agent_platform/hermes_daily_repair_entry.py": (
                "plan_daily_repair",
            ),
            "scripts/agent_platform/cold_start_background_entry.py": (
                "execute_cold_start_background",
                "fail_cold_start_background_if_running",
            ),
        }
        for relative, calls in expected_calls.items():
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            for call in calls:
                self.assertIn(call, text, f"{relative}: {call}")


if __name__ == "__main__":
    unittest.main()
