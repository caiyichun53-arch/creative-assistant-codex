from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
GRAPH_PATH = ROOT / "REMAINING_FORMAL_SKILL_EXECUTION_GRAPH.yaml"
MAPPING_PATH = ROOT / "FORMAL_SKILL_ROUTE_MAPPING.yaml"


class RemainingFormalSkillExecutionGraphTests(unittest.TestCase):
    def load_graph(self) -> dict:
        return yaml.safe_load(GRAPH_PATH.read_text(encoding="utf-8"))

    def load_mapping(self) -> dict:
        return yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))

    def test_graph_freezes_all_remaining_formal_skills_in_mapping_order(self) -> None:
        graph = self.load_graph()
        mapping = self.load_mapping()
        completed = {item["skill_id"] for item in graph["completed_baseline_skills"]}
        remaining = [item["skill_id"] for item in graph["remaining_skills"]]
        expected = [
            item["formal_skill_id"]
            for item in mapping["formal_skills"]
            if item["formal_skill_id"] not in completed
        ]
        self.assertEqual(remaining, expected)
        self.assertEqual(completed, {"content_classify", "content_relation_judge"})
        self.assertEqual(len(remaining), 10)

    def test_every_remaining_skill_records_required_phase_1_fields(self) -> None:
        required = {
            "skill_id",
            "responsibility",
            "dependencies",
            "required_upstream_artifacts",
            "output_consumers",
            "model_nodes",
            "deterministic_or_llm",
            "schema_status",
            "contract_status",
            "implementation_status",
            "fixture_status",
            "live_validation_status",
            "workflow_phase",
        }
        graph = self.load_graph()
        for skill in graph["remaining_skills"]:
            with self.subTest(skill=skill["skill_id"]):
                self.assertTrue(required.issubset(skill.keys()))
                for field in required - {"model_nodes", "dependencies", "output_consumers"}:
                    self.assertTrue(skill[field])
                self.assertEqual(skill["deterministic_or_llm"], "llm")

    def test_dependencies_are_completed_skills_previous_remaining_skills_or_formal_adapters(self) -> None:
        graph = self.load_graph()
        completed = {item["skill_id"] for item in graph["completed_baseline_skills"]}
        adapters = {item["adapter_id"] for item in graph["external_adapter_dependencies"]}
        seen = set(completed)
        for skill in graph["remaining_skills"]:
            with self.subTest(skill=skill["skill_id"]):
                unknown = set(skill["dependencies"]) - seen - adapters
                self.assertEqual(unknown, set())
                seen.add(skill["skill_id"])

    def test_model_nodes_match_formal_skill_route_mapping(self) -> None:
        graph = self.load_graph()
        mapping = self.load_mapping()
        expected = {
            item["formal_skill_id"]: item.get("allowed_model_nodes", [])
            for item in mapping["formal_skills"]
        }
        for skill in graph["remaining_skills"]:
            with self.subTest(skill=skill["skill_id"]):
                self.assertEqual(skill["model_nodes"], expected[skill["skill_id"]])

    def test_no_completed_or_legacy_skill_is_queued_for_reimplementation(self) -> None:
        graph = self.load_graph()
        remaining = {item["skill_id"] for item in graph["remaining_skills"]}
        self.assertNotIn("content_classify", remaining)
        self.assertNotIn("content_relation_judge", remaining)
        self.assertTrue(graph["graph_policy"]["old_business_logic_must_not_define_dependencies"])
        self.assertFalse(graph["graph_policy"]["production_direct_cli_or_legacy_model_calls_allowed"])


if __name__ == "__main__":
    unittest.main()
