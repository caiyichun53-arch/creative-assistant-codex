from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts.core.business_data.run_competitor_registration_full import validate_registration_execution_contract
from scripts.core.business_data.run_reverse_prep import validate_reverse_prep_execution_contract


ROOT = Path(__file__).resolve().parents[2]


class EffectiveBaselineBindingTests(unittest.TestCase):
    """Verify real bindings fail closed on invalid local configuration.

    The effective baseline defines the governing behavior.  It intentionally
    does not duplicate every implementation default, so this test validates
    the executable contract rather than treating an obsolete rule matrix as a
    second design authority.
    """

    def setUp(self) -> None:
        settings = yaml.safe_load((ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8"))
        self.hit_cfg = settings["hit_detection"]
        self.reverse_cfg = settings["reverse_engine"]
        self.domain = {
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            }
        }

    def test_registration_binding_accepts_example_config_and_rejects_invalid_values(self) -> None:
        validate_registration_execution_contract(self.domain, self.hit_cfg)
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(self.domain, dict(self.hit_cfg, unified_min_samples=0))

    def test_reverse_prep_binding_accepts_example_config_and_rejects_invalid_values(self) -> None:
        validate_reverse_prep_execution_contract(self.reverse_cfg)
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(dict(self.reverse_cfg, top_comments=0))
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(dict(self.reverse_cfg, min_comment_len=0))


if __name__ == "__main__":
    unittest.main()
