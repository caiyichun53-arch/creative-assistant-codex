from __future__ import annotations

import unittest
from pathlib import Path

from scripts.validation.generate_constitution_mirror import generate_claude_md

ROOT = Path(__file__).resolve().parents[2]


class ConstitutionSyncTests(unittest.TestCase):
    def test_claude_md_matches_what_the_generator_would_produce_from_agents_md(self) -> None:
        # Replaces the old "shared body diff, minus one exempt line" check,
        # which only compared text from '## 三根支柱' onward and so never
        # actually verified the "这是什么" section above it -- exactly the
        # section that drifted for 124 commits (see AGENTS.md "开工纪律").
        # This compares the WHOLE file against a real regeneration, so
        # there's no unguarded section left, and a failure's fix is
        # mechanical: run scripts/validation/generate_constitution_mirror.py,
        # not hand-copy prose between two files again.
        agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        generated = generate_claude_md(agents_md)

        self.assertEqual(
            claude_md,
            generated,
            "CLAUDE.md does not match what generate_constitution_mirror.py would "
            "produce from the current AGENTS.md. AGENTS.md is the only file to "
            "hand-edit -- run `python -m scripts.validation.generate_constitution_mirror` "
            "to refresh CLAUDE.md, then commit both.",
        )


if __name__ == "__main__":
    unittest.main()
