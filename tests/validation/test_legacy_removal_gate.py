from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts.validation.legacy_removal_gate import INVENTORY_PATH, run_gate


class LegacyRemovalGateTests(unittest.TestCase):
    def test_gate_blocks_production_legacy_runtime_paths(self) -> None:
        result = run_gate(write_inventory=False)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["production_legacy_executable_path_count"], 0)
        self.assertEqual(result["production_legacy_reference_count"], 0)
        self.assertGreaterEqual(result["inventory"]["item_count"], 69)

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
