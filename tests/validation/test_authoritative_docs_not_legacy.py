from __future__ import annotations

import re
import unittest
from pathlib import Path

from scripts.validation.clean_room_readiness import LEGACY_QUARANTINE_ROOTS

ROOT = Path(__file__).resolve().parents[2]

# 2026-07-07: BUILD_PLAN.md was mistakenly cited this session as if it were current
# design authority. It was actually the pre-clean-room MVP's build log -- every one
# of its named code paths (scripts/collect/register_competitor.py,
# scripts/analyze/judge_hits.py, scripts/topics/daily_topics.py, scripts/feishu/push.py)
# was already declared legacy_runtime.quarantined_entrypoints (LEGACY_QUARANTINE_ROOTS)
# at the time it was read. Nobody checked that before trusting it. This test makes
# that check automatic for PROSE design docs -- CLAUDE.md/AGENTS.md/BUSINESS_DECISION_TABLES.md
# describe how the current system works and should never need to name a quarantined
# legacy path; if one shows up, the doc is very likely describing the retired system.
#
# BUSINESS_RULE_CATALOG.yaml and REQUIREMENT_CODE_TRACEABILITY.yaml are deliberately
# NOT in this list: their whole job is migration/traceability bookkeeping, so fields
# like observed_legacy_behavior and code_artifacts legitimately cite legacy paths as
# "here is what the old code did" evidence -- that is not the same mistake as a prose
# doc silently describing legacy code as if it were the current system.
AUTHORITATIVE_DESIGN_DOCS = (
    "CLAUDE.md",
    "AGENTS.md",
    "BUSINESS_DECISION_TABLES.md",
)


def _mentions_quarantined_path(text: str, legacy_path: str) -> bool:
    # Word-boundary-ish match so "scripts/reverse" doesn't false-positive on an
    # unrelated word that happens to contain the substring.
    pattern = re.escape(legacy_path)
    return re.search(pattern, text) is not None


class AuthoritativeDocsNotLegacyTests(unittest.TestCase):
    def test_authoritative_docs_do_not_reference_quarantined_legacy_paths(self) -> None:
        violations: list[str] = []
        for doc_name in AUTHORITATIVE_DESIGN_DOCS:
            doc_path = ROOT / doc_name
            if not doc_path.exists():
                continue
            text = doc_path.read_text(encoding="utf-8", errors="ignore")
            for legacy_path in LEGACY_QUARANTINE_ROOTS:
                if _mentions_quarantined_path(text, legacy_path):
                    violations.append(f"{doc_name} references quarantined legacy path '{legacy_path}'")
        self.assertEqual(
            violations,
            [],
            "An authoritative design doc references a quarantined legacy path -- it is "
            "very likely describing the retired pre-clean-room system, not current "
            f"design, and should not be treated as design authority: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
