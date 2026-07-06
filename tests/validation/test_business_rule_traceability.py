from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts.core.business_data.run_competitor_registration_full import (
    BASELINE_HARD_MINIMUM_SAMPLES,
    validate_registration_execution_contract,
)

ROOT = Path(__file__).resolve().parents[2]

# BUSINESS_RULE_CATALOG.yaml is a static document -- REQUIREMENT_CODE_TRACEABILITY.yaml
# references it but nothing ever re-ran it against the code that got built, which is
# exactly how a fabricated "10" threshold (no basis in either the legacy system or this
# catalog) went unnoticed for days. This test makes the checkable thresholds for every
# BR-* rule that scripts/core/business_data actually implements into a real assertion.
# When implementing a new BR-* rule (or changing a threshold), add/update its assertion
# here in the same commit -- see AGENTS.md/CLAUDE.md "执行纪律".


def _load_catalog() -> dict:
    return yaml.safe_load((ROOT / "BUSINESS_RULE_CATALOG.yaml").read_text(encoding="utf-8"))


def _rule(catalog: dict, requirement_id: str) -> dict:
    for rule in catalog["rules"]:
        if rule["requirement_id"] == requirement_id:
            return rule
    raise AssertionError(f"{requirement_id} not found in BUSINESS_RULE_CATALOG.yaml")


class BusinessRuleTraceabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _load_catalog()
        settings = yaml.safe_load((ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8"))
        self.hit_cfg = settings["hit_detection"]

    def test_br_collect_001_first_crawl_observe_window(self) -> None:
        rule = _rule(self.catalog, "BR-COLLECT-001")
        self.assertEqual(self.hit_cfg["observe_days"], rule["thresholds"]["observe_days"])

    def test_br_baseline_001_window_and_target_samples(self) -> None:
        rule = _rule(self.catalog, "BR-BASELINE-001")
        self.assertEqual(self.hit_cfg["baseline_window_days"], rule["defaults"]["baseline_window_days"])

    def test_br_baseline_003_minimum_sample_count_is_30_not_invented(self) -> None:
        rule = _rule(self.catalog, "BR-BASELINE-003")
        self.assertEqual(self.hit_cfg["baseline_min_samples"], rule["thresholds"]["minimum_sample_count"])
        self.assertTrue(rule["defaults"]["legacy_supplement"])
        # The only other floor allowed to exist is the hard mathematical minimum for
        # computing a median/P90 at all -- it must stay far below the documented
        # 30-sample target, never a second competing "business" threshold.
        self.assertLess(BASELINE_HARD_MINIMUM_SAMPLES, rule["thresholds"]["minimum_sample_count"])

    def test_br_hit_001_excess_threshold_and_hit_floor(self) -> None:
        rule = _rule(self.catalog, "BR-HIT-001")
        self.assertEqual(self.hit_cfg["excess_threshold"], rule["thresholds"]["excess_threshold"])
        self.assertEqual(
            self.hit_cfg["hit_floor_absolute_like_count"], rule["thresholds"]["hit_floor_absolute_like_count"]
        )
        self.assertEqual(rule["thresholds"]["p90_bounded_by_floor"], True)

    def test_execution_contract_rejects_config_drift_from_the_catalog(self) -> None:
        domain = {
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            }
        }
        # Matches the catalog: passes cleanly.
        validate_registration_execution_contract(domain, self.hit_cfg)

        # A config drifted away from the catalog's documented threshold must be rejected,
        # not silently accepted -- this is the exact failure mode being guarded against.
        drifted = dict(self.hit_cfg, baseline_min_samples=10)
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(domain, drifted)


if __name__ == "__main__":
    unittest.main()
