from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.core.execution_contract import load_baseline_section


ROOT = Path(__file__).resolve().parents[2]
CALL_PATTERN = re.compile(r"require_baseline_citations\(\[([^\]]+)\]\)")
SECTION_PATTERN = re.compile(r'[\"\'](\d+)[\"\']')


class EffectiveBaselineCitationCoverageTests(unittest.TestCase):
    def test_every_runtime_citation_points_to_a_real_baseline_section(self) -> None:
        cited_sections: set[str] = set()
        for path in (ROOT / "scripts" / "core").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for call in CALL_PATTERN.findall(text):
                cited_sections.update(SECTION_PATTERN.findall(call))
        self.assertTrue(cited_sections, "no runtime entrypoint cites the effective design baseline")
        for section_id in cited_sections:
            with self.subTest(section_id=section_id):
                self.assertEqual(load_baseline_section(section_id)["section_id"], section_id)


if __name__ == "__main__":
    unittest.main()
