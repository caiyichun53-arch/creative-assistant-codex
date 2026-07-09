from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validation.unsourced_constant_check import (
    _is_grounded,
    run_checklist,
    scan_file,
)


class IsGroundedTests(unittest.TestCase):
    """Regression coverage for the real bug this check exists to catch:
    TOP_COMMENTS_PER_HIT = 3 was added with a comment explaining *why*
    comments are used as evidence, but never admitting the count itself has
    no source -- indistinguishable, to a human skimming it, from a properly
    sourced number."""

    def test_a_comment_that_explains_purpose_but_not_source_is_not_grounded(self) -> None:
        lines = [
            "# comments are a real evidence source, collected during reverse-prep",
            "TOP_COMMENTS_PER_HIT = 3",
        ]
        self.assertFalse(_is_grounded(lines, 1))

    def test_a_business_rule_citation_grounds_it(self) -> None:
        lines = ["# BR-COLLECT-005 top_comments limit", "TOP_COMMENTS = 60"]
        self.assertTrue(_is_grounded(lines, 1))

    def test_a_schema_file_reference_grounds_it(self) -> None:
        lines = ["# matches input_schema.yaml maxLength", "EVIDENCE_ITEM_MAX_CHARS = 240"]
        self.assertTrue(_is_grounded(lines, 1))

    def test_an_explicit_unsourced_marker_grounds_it(self) -> None:
        lines = ["# UNSOURCED: picked arbitrarily, needs a real decision", "TOP_COMMENTS_PER_HIT = 3"]
        self.assertTrue(_is_grounded(lines, 1))

    def test_no_comment_at_all_is_not_grounded(self) -> None:
        lines = ["TOP_COMMENTS_PER_HIT = 3"]
        self.assertFalse(_is_grounded(lines, 0))

    def test_grounding_marker_more_than_lookback_lines_away_does_not_count(self) -> None:
        lines = ["# BR-COLLECT-005"] + ["# padding"] * 15 + ["TOP_COMMENTS_PER_HIT = 3"]
        self.assertFalse(_is_grounded(lines, len(lines) - 1))


class ScanFileTests(unittest.TestCase):
    def test_flags_an_ungrounded_constant_in_a_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "core" / "experience" / "fake.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text(
                "# comments are a real evidence source\nTOP_COMMENTS_PER_HIT = 3\n",
                encoding="utf-8",
            )
            with patch("scripts.validation.unsourced_constant_check.ROOT", fake_root):
                hits = scan_file(fake_file)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "TOP_COMMENTS_PER_HIT")

    def test_does_not_flag_a_grounded_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "core" / "experience" / "fake.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text(
                "# BR-COLLECT-005: top comments limit\nTOP_COMMENTS = 60\n",
                encoding="utf-8",
            )
            with patch("scripts.validation.unsourced_constant_check.ROOT", fake_root):
                hits = scan_file(fake_file)
        self.assertEqual(hits, [])

    def test_ignores_non_constant_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp)
            fake_file = fake_root / "scripts" / "core" / "experience" / "fake.py"
            fake_file.parent.mkdir(parents=True, exist_ok=True)
            fake_file.write_text("result = compute(3)\nlocal_var = 5\n", encoding="utf-8")
            with patch("scripts.validation.unsourced_constant_check.ROOT", fake_root):
                hits = scan_file(fake_file)
        self.assertEqual(hits, [])


class RunChecklistTests(unittest.TestCase):
    def test_real_repo_currently_passes(self) -> None:
        # As of 2026-07-10 (TOP_COMMENTS_PER_HIT fixed to carry an explicit
        # UNSOURCED marker), the real scanned directories should be clean.
        result = run_checklist()
        self.assertEqual(result["status"], "PASS", result["unsourced_constants"])


if __name__ == "__main__":
    unittest.main()
