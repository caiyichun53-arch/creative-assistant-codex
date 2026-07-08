from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from scripts.core.business_data.run_competitor_registration_full import (
    validate_registration_execution_contract,
)
from scripts.core.business_data.run_reverse_prep import (
    validate_reverse_prep_execution_contract,
)

ROOT = Path(__file__).resolve().parents[2]

# BUSINESS_RULE_CATALOG.yaml is a static document -- REQUIREMENT_CODE_TRACEABILITY.yaml
# references it but nothing ever re-ran it against the code that got built, which is
# exactly how a fabricated "10" threshold (no basis in either the legacy system or this
# catalog) went unnoticed for days. This test makes the checkable thresholds for every
# BR-* rule that scripts/core/business_data actually implements into a real assertion.
# When implementing a new BR-* rule (or changing a threshold), add/update its assertion
# here in the same commit -- see AGENTS.md/CLAUDE.md "执行纪律".
#
# 2026-07-07: rewritten for the master-doc-realignment design (BR-HIT-001
# amendment_2026_07_07_master_doc_realignment) -- excess_threshold/hit_floor_
# absolute_like_count/baseline_min_samples/baseline_window_days no longer exist.


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
        self.reverse_cfg = settings["reverse_engine"]

    def test_br_collect_001_first_crawl_observe_window(self) -> None:
        rule = _rule(self.catalog, "BR-COLLECT-001")
        self.assertEqual(self.hit_cfg["observe_days"], rule["thresholds"]["observe_days"])

    def test_br_baseline_001_mature_history_window_and_unified_min_samples(self) -> None:
        rule = _rule(self.catalog, "BR-BASELINE-001")
        self.assertEqual(self.hit_cfg["mature_history_window_days"], rule["defaults"]["mature_history_window_days"])
        self.assertEqual(self.hit_cfg["unified_min_samples"], rule["thresholds"]["unified_min_samples"])

    def test_br_baseline_003_minimum_sample_count_is_20_not_invented(self) -> None:
        rule = _rule(self.catalog, "BR-BASELINE-003")
        self.assertEqual(self.hit_cfg["unified_min_samples"], rule["thresholds"]["minimum_sample_count"])
        self.assertFalse(rule["defaults"]["legacy_supplement"])

    def test_br_hit_001_magnitude_channel_thresholds(self) -> None:
        rule = _rule(self.catalog, "BR-HIT-001")
        self.assertEqual(
            self.hit_cfg["single_metric_excess_threshold"], rule["thresholds"]["single_metric_excess_threshold"]
        )
        self.assertEqual(
            self.hit_cfg["multi_indicator_excess_threshold"], rule["thresholds"]["multi_indicator_excess_threshold"]
        )
        self.assertEqual(
            self.hit_cfg["cold_start_d7_single_metric_threshold"],
            rule["thresholds"]["cold_start_d7_single_metric_threshold"],
        )
        self.assertEqual(
            self.hit_cfg["cold_start_d7_multi_indicator_threshold"],
            rule["thresholds"]["cold_start_d7_multi_indicator_threshold"],
        )
        # 2026-07-08: the mature-history/rough channel is deliberately stricter
        # than formal_d_series (3.0x/2.0x vs 2.0x/1.6x) -- noisier evidence needs a
        # higher bar to compensate, not a lower one.
        self.assertGreater(
            self.hit_cfg["cold_start_d7_single_metric_threshold"], self.hit_cfg["single_metric_excess_threshold"]
        )
        self.assertGreater(
            self.hit_cfg["cold_start_d7_multi_indicator_threshold"], self.hit_cfg["multi_indicator_excess_threshold"]
        )

    def test_br_hit_001_mature_history_absolute_floor_and_small_account_p90(self) -> None:
        rule = _rule(self.catalog, "BR-HIT-001")
        self.assertEqual(
            self.hit_cfg["mature_history_absolute_like_floor"], rule["thresholds"]["mature_history_absolute_like_floor"]
        )
        self.assertEqual(
            self.hit_cfg["small_account_p90_percentile"], rule["thresholds"]["small_account_p90_percentile"]
        )

    def test_br_hit_001_baseline_windows_and_activation_gates(self) -> None:
        rule = _rule(self.catalog, "BR-HIT-001")
        self.assertEqual(self.hit_cfg["mature_history_window_days"], rule["thresholds"]["mature_history_window_days"])
        self.assertEqual(self.hit_cfg["mature_history_max_samples"], rule["thresholds"]["mature_history_max_samples"])
        self.assertEqual(
            self.hit_cfg["formal_baseline_activation_min_samples"],
            rule["thresholds"]["formal_baseline_activation_min_samples"],
        )
        self.assertEqual(
            self.hit_cfg["formal_baseline_computation_window"], rule["thresholds"]["formal_baseline_computation_window"]
        )
        self.assertEqual(self.hit_cfg["discovery_delay_hours_max"], rule["thresholds"]["discovery_delay_hours_max"])
        self.assertEqual(self.hit_cfg["unified_min_samples"], rule["thresholds"]["unified_min_samples"])
        # The corrected reading of the document's "20": an activation gate only, not a
        # permanent computation-window cap -- the two numbers must differ on purpose.
        self.assertNotEqual(
            rule["thresholds"]["formal_baseline_activation_min_samples"],
            rule["thresholds"]["formal_baseline_computation_window"],
        )

    def test_br_hit_001_comment_like_ratio_channel(self) -> None:
        rule = _rule(self.catalog, "BR-HIT-001")
        self.assertEqual(
            self.hit_cfg["comment_like_ratio_threshold"], rule["thresholds"]["comment_like_ratio_threshold"]
        )

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
        drifted = dict(self.hit_cfg, unified_min_samples=10)
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(domain, drifted)

    def test_execution_contract_rejects_missing_or_invalid_comment_like_ratio_threshold(self) -> None:
        domain = {
            "collector_policy": {
                "first_crawl": "stock_snapshot_archived",
                "comments": "reverse_prep_only_for_promoted_hits",
            }
        }
        missing = {k: v for k, v in self.hit_cfg.items() if k != "comment_like_ratio_threshold"}
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(domain, missing)

        out_of_range = dict(self.hit_cfg, comment_like_ratio_threshold=1.5)
        with self.assertRaises(ValueError):
            validate_registration_execution_contract(domain, out_of_range)

    def test_br_collect_005_006_top_comments_and_min_comment_len(self) -> None:
        collect_005 = _rule(self.catalog, "BR-COLLECT-005")
        collect_006 = _rule(self.catalog, "BR-COLLECT-006")
        self.assertEqual(self.reverse_cfg["top_comments"], collect_005["thresholds"]["top_comments"])
        self.assertEqual(self.reverse_cfg["top_comments"], collect_006["thresholds"]["top_comments"])
        self.assertEqual(self.reverse_cfg["min_comment_len"], collect_006["defaults"]["min_comment_len"])

    def test_reverse_prep_contract_rejects_config_drift_from_the_catalog(self) -> None:
        # Matches the catalog: passes cleanly.
        validate_reverse_prep_execution_contract(self.reverse_cfg)

        drifted = dict(self.reverse_cfg, top_comments=100)
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(drifted)

        drifted_len = dict(self.reverse_cfg, min_comment_len=1)
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(drifted_len)


if __name__ == "__main__":
    unittest.main()
