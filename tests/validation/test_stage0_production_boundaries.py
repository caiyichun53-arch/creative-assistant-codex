from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from scripts.core.model_gateway.formal_skill_adapter import FormalSkillValidationError, make_content_classify_harness
from scripts.core.production.stage0_content_core import (
    FORMAL_DB_PATH,
    LegacyProductionEntryDisabledError,
    reject_legacy_cli_production_write,
)


ROOT = Path(__file__).resolve().parents[2]
LEGACY_CHAIN_CLI = (
    "scripts/core/experience/run_source_to_topic.py",
    "scripts/core/experience/run_content_plan.py",
    "scripts/core/experience/run_script_generate.py",
    "scripts/core/experience/run_script_review.py",
    "scripts/core/experience/run_final_draft.py",
    "scripts/core/experience/review_queue.py",
    "scripts/core/experience/run_sample_deep_analyze.py",
)


class Stage0ProductionBoundaryTests(unittest.TestCase):
    def test_only_stage0_core_is_the_formal_write_boundary(self) -> None:
        source = (ROOT / "scripts/core/production/stage0_content_core.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("class Stage0ContentProductionCore:"), 1)
        self.assertIn("FORMAL_DB_PATH = ROOT / \"data\" / \"formal\" / \"production_activation.sqlite3\"", source)

    def test_legacy_chain_cli_refuse_the_formal_database_before_connecting(self) -> None:
        with self.assertRaises(LegacyProductionEntryDisabledError):
            reject_legacy_cli_production_write(FORMAL_DB_PATH, "test legacy cli")
        for relative in LEGACY_CHAIN_CLI:
            with self.subTest(entrypoint=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                guard = source.index("reject_legacy_cli_production_write(db_path")
                connect = source.index("sqlite3.connect(db_path)")
                self.assertLess(guard, connect)
                self.assertIn('parser.add_argument("--data-identity", required=True', source)

    def test_in_memory_formal_skill_harness_is_explicitly_test_identity(self) -> None:
        harness = make_content_classify_harness()
        try:
            self.assertEqual(harness.data_identity, "test")
            with self.assertRaises(FormalSkillValidationError):
                replace(harness, data_identity="production")
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
