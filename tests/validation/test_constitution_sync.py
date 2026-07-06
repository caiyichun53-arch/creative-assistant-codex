from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The only line allowed to differ between CLAUDE.md and AGENTS.md: which tool
# name is used for the daily model routing / creation conversation reference.
# Everything else describes shared architecture/rules and must stay identical,
# so that neither file can silently drift out of sync with the other again
# (see AGENTS.md/CLAUDE.md "执行纪律": CLAUDE.md/AGENTS.md 从第一次提交后
# 124 次提交都没有互相同步过).
EXEMPT_LINE_PREFIX = "- **模型路由 per-node**"
SHARED_SECTION_MARKER = "## 三根支柱"


def _shared_body(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(SHARED_SECTION_MARKER))
    body_lines = [line for line in lines[start:] if not line.startswith(EXEMPT_LINE_PREFIX)]
    return "\n".join(body_lines)


class ConstitutionSyncTests(unittest.TestCase):
    def test_claude_md_and_agents_md_share_rules_body(self) -> None:
        claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(
            _shared_body(claude_md),
            _shared_body(agents_md),
            "CLAUDE.md and AGENTS.md have drifted apart outside the one exempt "
            "model-routing line. Edit both files together and keep them in sync.",
        )


if __name__ == "__main__":
    unittest.main()
