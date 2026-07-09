from __future__ import annotations

import unittest

from scripts.validation.generate_constitution_mirror import SUBSTITUTIONS, generate_claude_md


class GenerateClaudeMdTests(unittest.TestCase):
    def test_applies_all_known_substitutions(self) -> None:
        source = "\n".join(old for old, _new in SUBSTITUTIONS)
        generated = generate_claude_md(source)
        for old, new in SUBSTITUTIONS:
            self.assertNotIn(old, generated)
            self.assertIn(new, generated)

    def test_raises_clearly_when_a_substitution_source_phrase_is_missing(self) -> None:
        # Simulates AGENTS.md being edited in a way that breaks one of the
        # known phrases (e.g. rewording "Codex 无头") without updating this
        # generator's SUBSTITUTIONS table -- must fail loudly, not silently
        # leave "Codex" sitting in the generated CLAUDE.md.
        source = "some text with no known substitution phrases at all"
        with self.assertRaises(ValueError):
            generate_claude_md(source)

    def test_shared_codex_mentions_outside_the_table_are_left_untouched(self) -> None:
        # Lines that name both tools together (host-adapter lists, handoff
        # prose) are intentionally identical in both files and must survive
        # generation unchanged, not get blanket-replaced. generate_claude_md
        # requires every known phrase to be present (it's only ever called on
        # a whole AGENTS.md), so embed the shared line inside an otherwise
        # complete source rather than passing it alone.
        shared_line = "Hermes、Codex、Claude Code、飞书等只能是适配器"
        source = "\n".join(old for old, _new in SUBSTITUTIONS) + "\n" + shared_line
        generated = generate_claude_md(source)
        self.assertIn(shared_line, generated)


if __name__ == "__main__":
    unittest.main()
