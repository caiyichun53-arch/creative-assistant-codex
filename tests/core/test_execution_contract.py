from __future__ import annotations

import unittest

from scripts.core.execution_contract import UncitedBaselineError, load_baseline_section, require_baseline_citations


class LoadBaselineSectionTests(unittest.TestCase):
    def test_loads_a_real_baseline_section(self) -> None:
        entry = load_baseline_section("16")
        self.assertEqual(entry["section_id"], "16")
        self.assertIn("path", entry)

    def test_raises_on_a_section_that_does_not_exist(self) -> None:
        with self.assertRaises(UncitedBaselineError):
            load_baseline_section("999")


class RequireBaselineCitationsTests(unittest.TestCase):
    def test_returns_entries_keyed_by_section(self) -> None:
        result = require_baseline_citations(["16", "20"])
        self.assertEqual(set(result.keys()), {"16", "20"})
        self.assertEqual(result["16"]["section_id"], "16")

    def test_raises_on_empty_citation_list(self) -> None:
        with self.assertRaises(UncitedBaselineError):
            require_baseline_citations([])

    def test_raises_if_any_cited_section_does_not_exist(self) -> None:
        with self.assertRaises(UncitedBaselineError):
            require_baseline_citations(["16", "999"])


if __name__ == "__main__":
    unittest.main()
