from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

# This test exists because writing "exact_match" in REQUIREMENT_CODE_TRACEABILITY.yaml is
# just a claim in a YAML file -- nothing stops that claim from being false, or from going
# stale the next time someone touches the code it describes. A human has to remember to
# check it, and in this project that has already failed more than once (a fabricated
# baseline_min_samples=10 threshold, a permanently-excluding younger_than_7_days flag that
# contradicted an already-documented rule). This test makes the claim self-verifying:
# every rule marked exact_match must be referenced by its BR-*/GATE-* id somewhere in the
# test suite, or this fails. "missing_in_code" rules are exempt -- that status is already
# an honest admission that no code/test exists yet.

BR_ID_PATTERN = re.compile(r"BR-[A-Z]+-\d+")


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _exact_match_rule_ids(traceability: dict) -> set[str]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            alignment = node.get("requirement_alignment")
            if isinstance(alignment, dict):
                for rule_id, status in alignment.items():
                    if status == "exact_match":
                        found.add(rule_id)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(traceability)
    return found


def _referenced_rule_ids_in_tests() -> set[str]:
    referenced: set[str] = set()
    for test_file in (ROOT / "tests").rglob("test_*.py"):
        text = test_file.read_text(encoding="utf-8", errors="ignore")
        referenced.update(BR_ID_PATTERN.findall(text))
    return referenced


class BusinessRuleTestCoverageTests(unittest.TestCase):
    def test_every_exact_match_rule_has_test_coverage(self) -> None:
        catalog = _load_yaml("BUSINESS_RULE_CATALOG.yaml")
        catalog_rule_ids = {rule["requirement_id"] for rule in catalog["rules"]}

        traceability = _load_yaml("REQUIREMENT_CODE_TRACEABILITY.yaml")
        claimed_exact_match = _exact_match_rule_ids(traceability)

        # A claim about a rule id that doesn't even exist in the catalog is its own bug.
        unknown_ids = claimed_exact_match - catalog_rule_ids
        self.assertEqual(
            unknown_ids, set(), f"REQUIREMENT_CODE_TRACEABILITY.yaml claims exact_match for unknown rule ids: {sorted(unknown_ids)}"
        )

        referenced = _referenced_rule_ids_in_tests()
        uncovered = sorted(claimed_exact_match - referenced)
        self.assertEqual(
            uncovered,
            [],
            "These rules are claimed 'exact_match' in REQUIREMENT_CODE_TRACEABILITY.yaml "
            "but no test file references their id -- either write a test that cites the id, "
            "or downgrade the claim if the rule isn't actually implemented/verified: "
            f"{uncovered}",
        )


if __name__ == "__main__":
    unittest.main()
