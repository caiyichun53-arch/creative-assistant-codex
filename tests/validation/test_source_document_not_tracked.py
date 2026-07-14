from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 2026-07-08: within a single session, reading the raw master design document
# directly (instead of docs/EFFECTIVE_DESIGN_BASELINE.md, the sole current design baseline
# name as "现行系统唯一算数的业务规则依据") caused real drift THREE separate times --
# each time surfacing a stale number/mechanism the catalog had already superseded
# (e.g. the document's literal ">=5" sample floor, already overridden to 20 and
# recorded as such in BR-BASELINE-003). The user's diagnosis: this is not a habit
# problem fixable by trying harder to remember to check the catalog first -- it is
# a mechanism problem, because the document sitting in the repo is itself the
# standing invitation to fall back to it. The fix the user chose: the document does
# not live in this repository at all. Its real path is local-machine-only config,
# same pattern AGENTS.md uses for the other reference libraries ("真实
# 路径写在本机配置,不入库"). It is only ever supplied again, deliberately, by the user,
# for a bounded formal reconciliation session (like the one that produced BR-HIT-001) --
# never as day-to-day fallback reading. This test makes "the file must not be tracked"
# a hard, automatic check instead of relying on anyone remembering not to re-add it.
SOURCE_DOCUMENT_FILENAME_FRAGMENTS = (
    "爆款口播内容经验库系统_最终完整执行总控文档",
    "爆款口播内容经验库系统_旧代码审计启动提示词",
)


def _tracked_files() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return completed.stdout.splitlines()


class SourceDocumentNotTrackedTests(unittest.TestCase):
    def test_master_design_document_is_not_tracked_in_the_repo(self) -> None:
        tracked = _tracked_files()
        violations = [
            path
            for path in tracked
            for fragment in SOURCE_DOCUMENT_FILENAME_FRAGMENTS
            if fragment in path
        ]
        self.assertEqual(
            violations,
            [],
            "The master design source document must not be tracked in this repo -- "
            "it is local-machine-only reference material for deliberate, "
            "user-initiated reconciliation sessions, never a standing fallback for "
            f"day-to-day implementation questions (see docs/EFFECTIVE_DESIGN_BASELINE.md): {violations}",
        )


if __name__ == "__main__":
    unittest.main()
