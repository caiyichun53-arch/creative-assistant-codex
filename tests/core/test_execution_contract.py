from __future__ import annotations

import unittest

from scripts.core.execution_contract import UncitedRequirementError, load_requirement, require_catalog_citations


class LoadRequirementTests(unittest.TestCase):
    def test_loads_a_real_requirement(self) -> None:
        entry = load_requirement("BR-HIT-001")
        self.assertEqual(entry["requirement_id"], "BR-HIT-001")
        self.assertIn("expected_behavior", entry)

    def test_raises_on_a_requirement_id_that_does_not_exist(self) -> None:
        with self.assertRaises(UncitedRequirementError):
            load_requirement("BR-DOES-NOT-EXIST-999")


class RequireCatalogCitationsTests(unittest.TestCase):
    def test_returns_entries_keyed_by_requirement_id(self) -> None:
        result = require_catalog_citations(["BR-HIT-001", "BR-ASR-001"])
        self.assertEqual(set(result.keys()), {"BR-HIT-001", "BR-ASR-001"})
        self.assertEqual(result["BR-ASR-001"]["requirement_id"], "BR-ASR-001")

    def test_raises_on_empty_citation_list(self) -> None:
        with self.assertRaises(UncitedRequirementError):
            require_catalog_citations([])

    def test_raises_if_any_cited_id_does_not_exist(self) -> None:
        with self.assertRaises(UncitedRequirementError):
            require_catalog_citations(["BR-HIT-001", "BR-FAKE-001"])


if __name__ == "__main__":
    unittest.main()
