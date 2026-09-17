from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts.agent_platform import hermes_cold_start_management_entry
from scripts.agent_platform import hermes_daily_entry
from scripts.agent_platform import hermes_daily_repair_entry
from scripts.agent_platform import hermes_formal_research_entry
from scripts.agent_platform import hermes_knowledge_convergence_entry
from scripts.agent_platform import run_daily_collection_once


class Stage8HermesRetirementTests(unittest.TestCase):
    def _assert_retired(self, callback, expected_message: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            callback()
        self.assertIn(expected_message, str(raised.exception))

    def test_legacy_daily_routes_refuse_before_forwarding_to_formal_core(self) -> None:
        self._assert_retired(
            hermes_daily_entry.main,
            "legacy Hermes formal daily route is retired",
        )
        self._assert_retired(
            run_daily_collection_once.main,
            "legacy direct formal daily entry is retired",
        )
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "agent_platform"
            / "hermes_daily_entry.sh"
        )
        script_text = script.read_text(encoding="utf-8")
        self.assertIn("legacy Hermes formal daily route is retired", script_text)
        self.assertNotIn("hermes_daily_entry", script_text)

    def test_legacy_formal_management_routes_refuse_state_changing_operations(self) -> None:
        self._assert_retired(
            hermes_cold_start_management_entry.main,
            "legacy Hermes cold-start management route is retired",
        )
        self._assert_retired(
            hermes_formal_research_entry.main,
            "legacy Hermes formal research route is retired",
        )
        with patch.object(
            sys,
            "argv",
            ["hermes_daily_repair_entry", "--action", "apply", "--as-of", "2026-08-27"],
        ):
            self._assert_retired(
                hermes_daily_repair_entry.main,
                "legacy Hermes formal daily repair apply route is retired",
            )
        with patch.object(
            sys,
            "argv",
            ["hermes_knowledge_convergence_entry", "--action", "apply"],
        ):
            self._assert_retired(
                hermes_knowledge_convergence_entry.main,
                "legacy Hermes knowledge convergence apply route is retired",
            )

    def test_direct_formal_research_call_refuses(self) -> None:
        with self.assertRaises(RuntimeError) as research_error:
            hermes_formal_research_entry.run("create_plan", {})
        self.assertIn(
            "legacy Hermes formal research route is retired",
            str(research_error.exception),
        )


if __name__ == "__main__":
    unittest.main()
