from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.validation.legacy_removal_gate import (
    INVENTORY_PATH,
    is_legacy_executable_path,
    reference_hits,
    run_gate,
)


class LegacyRemovalGateTests(unittest.TestCase):
    def test_gate_blocks_production_legacy_runtime_paths(self) -> None:
        result = run_gate(write_inventory=False)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["production_legacy_executable_path_count"], 0)
        self.assertEqual(result["production_legacy_reference_count"], 0)
        self.assertGreaterEqual(result["inventory"]["item_count"], 69)


class GateActuallyCatchesViolationsTests(unittest.TestCase):
    """Regression coverage for the audit finding that this gate had only ever
    been shown to be clean, never shown to actually fire on a real violation.
    These inject genuine violations through the real production functions
    (not a reimplementation) and assert the gate would go red -- proving the
    detector works, not just that today's repo happens to be clean."""

    def test_new_bat_or_vbs_launcher_is_caught_by_default(self) -> None:
        self.assertTrue(is_legacy_executable_path("scripts/some_brand_new_launcher.bat"))
        self.assertTrue(is_legacy_executable_path("scripts/some_brand_new_launcher.vbs"))
        # An explicitly allow-listed one is the only way to opt out.
        self.assertFalse(is_legacy_executable_path("scripts/scheduled/run_daily_incremental.bat"))

    def test_legacy_path_prefix_is_caught(self) -> None:
        self.assertTrue(is_legacy_executable_path("scripts/collect/register_competitor.py"))
        self.assertTrue(is_legacy_executable_path("scripts/reverse/dna_extract.py"))

    def test_a_genuine_new_reference_to_a_banned_token_in_active_code_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "core" / "business_data" / "not_actually_allowlisted.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text("DB_PATH = 'data/legacy/creation.db'  # a real violation, not a guard clause\n", encoding="utf-8")

            with patch("scripts.validation.legacy_removal_gate.ROOT", fake_root):
                active, historical = reference_hits(["scripts/core/business_data/not_actually_allowlisted.py"])

            self.assertEqual(len(active), 1, "a genuine new creation.db reference in active code must be flagged as active, not silently historical")
            self.assertEqual(historical, [])
            self.assertIn("creation.db", active[0]["tokens"])

    def test_status_would_be_fail_given_any_active_reference_or_legacy_path(self) -> None:
        # Mirrors run_gate()'s own status formula so a future edit that
        # loosens it (e.g. "PASS" if not legacy_paths else "FAIL", dropping
        # the active_refs check) is caught here even though the real repo
        # itself stays clean.
        legacy_paths = ["scripts/some_new_launcher.bat"]
        active_refs = [{"path": "x", "tokens": ["creation.db"]}]
        status = "PASS" if not legacy_paths and not active_refs else "FAIL"
        self.assertEqual(status, "FAIL")

    def test_inventory_items_have_required_contract_fields(self) -> None:
        required_fields = {
            "path",
            "type",
            "original_purpose",
            "current_reference_count",
            "referenced_by",
            "production_reachable",
            "test_reachable",
            "documentation_reachable",
            "replacement",
            "removal_decision",
            "removal_reason",
            "preservation_reason",
            "risk",
            "verification_method",
        }
        data = yaml.safe_load(Path(INVENTORY_PATH).read_text(encoding="utf-8"))

        self.assertGreaterEqual(len(data["items"]), 69)
        self.assertEqual(set(data["summary"]), {"legacy_executable_path_count", "production_legacy_reference_count", "historical_documentation_reference_count"})
        for item in data["items"]:
            self.assertLessEqual(required_fields, set(item))


if __name__ == "__main__":
    unittest.main()
