from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# BR-COLLECT-007: collection, ranking, storage, retrieval, tracking and dedupe are
# deterministic code paths with no LLM calls -- LLM may only run after data is
# materialized as a generation/analysis node. scripts/core/business_data/ is exactly
# the collection/baseline/hit pipeline this rule describes, so it must never import
# anything from the LLM routing layer or a provider SDK.
FORBIDDEN_MARKERS = (
    "model_gateway",
    "ModelRouter",
    "anthropic",
    "openai",
    "ChatCompletion",
)


class BusinessDataNoLlmTests(unittest.TestCase):
    def test_business_data_module_has_no_llm_references(self) -> None:
        business_data_dir = ROOT / "scripts" / "core" / "business_data"
        offenders: list[str] = []
        for source_file in business_data_dir.glob("*.py"):
            text = source_file.read_text(encoding="utf-8", errors="ignore")
            for marker in FORBIDDEN_MARKERS:
                if marker in text:
                    offenders.append(f"{source_file.relative_to(ROOT)}: {marker}")
        self.assertEqual(offenders, [], f"BR-COLLECT-007 violation -- LLM references found: {offenders}")


if __name__ == "__main__":
    unittest.main()
