"""Test backing for scripts/validation/production_activation_gate.py.

Two kinds of tests: (1) run the real gate against the real repo -- it must
pass, since every check it makes was landed for a real reason this session;
(2) prove each check actually catches the regression it claims to catch, by
constructing a deliberately-broken fixture and asserting the check fails on
it -- a gate that only ever prints PASS is not a gate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validation import production_activation_gate as gate


class RealRepoPassesTests(unittest.TestCase):
    def test_full_gate_passes_against_the_real_repo(self) -> None:
        result = gate.run_production_activation_gate()
        failed = [check["name"] for check in result["checks"] if not check["passed"]]
        self.assertEqual(failed, [], f"production_activation_gate failed: {result}")
        self.assertEqual(result["status"], "PASS")


class PipelineStagesWiredTests(unittest.TestCase):
    def test_passes_with_real_review_queue(self) -> None:
        self.assertTrue(gate.check_pipeline_stages_wired()["passed"])

    def test_fails_when_a_stage_is_missing(self) -> None:
        fake_text = 'STAGES = {"topic": {"table": "topic_candidates"}}'
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "review_queue.py"
            fake_path.write_text(fake_text, encoding="utf-8")
            with patch.object(gate, "REVIEW_QUEUE_PATH", fake_path):
                result = gate.check_pipeline_stages_wired()
        self.assertFalse(result["passed"])
        self.assertIn("plan", str(result["detail"]))


class PolishBeforeReviewOrderTests(unittest.TestCase):
    def test_passes_with_real_formal_skill_adapter(self) -> None:
        self.assertTrue(gate.check_polish_before_review_order()["passed"])

    def test_fails_when_review_comes_before_polish(self) -> None:
        fake_text = (
            "    def _run_script_review(self, input_payload):\n"
            '        x = route_name="business.creation_review"\n'
            '        y = route_name="business.creation_polish"\n'
            "\n"
            "    def _run_other(self):\n"
            "        pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "formal_skill_adapter.py"
            fake_path.write_text(fake_text, encoding="utf-8")
            with patch.object(gate, "FORMAL_SKILL_ADAPTER_PATH", fake_path):
                result = gate.check_polish_before_review_order()
        self.assertFalse(result["passed"])


class HumanReviewGatesExistTests(unittest.TestCase):
    def test_passes_with_real_schema(self) -> None:
        self.assertTrue(gate.check_human_review_gates_exist()["passed"])

    def test_fails_when_a_table_has_no_human_review_status(self) -> None:
        fake_schema = (
            "CREATE TABLE IF NOT EXISTS topic_candidates (\n"
            "    topic_id TEXT PRIMARY KEY,\n"
            "    human_review_status TEXT NOT NULL DEFAULT 'pending_review'\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS content_plans (\n"
            "    plan_id TEXT PRIMARY KEY\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS script_drafts (\n"
            "    draft_id TEXT PRIMARY KEY,\n"
            "    human_review_status TEXT NOT NULL DEFAULT 'pending_review'\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS script_reviews (\n"
            "    review_id TEXT PRIMARY KEY,\n"
            "    human_review_status TEXT NOT NULL DEFAULT 'pending_review'\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS final_drafts (\n"
            "    final_draft_id TEXT PRIMARY KEY,\n"
            "    human_review_status TEXT NOT NULL DEFAULT 'pending_review'\n"
            ");\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "schema.sql"
            fake_path.write_text(fake_schema, encoding="utf-8")
            with patch.object(gate, "SCHEMA_PATH", fake_path):
                result = gate.check_human_review_gates_exist()
        self.assertFalse(result["passed"])
        self.assertIn("content_plans", str(result["detail"]))


class FinalDraftNotAutoApprovedTests(unittest.TestCase):
    def test_passes_with_real_schema(self) -> None:
        self.assertTrue(gate.check_final_draft_not_auto_approved()["passed"])

    def test_fails_when_final_drafts_defaults_to_approved(self) -> None:
        fake_schema = (
            "CREATE TABLE IF NOT EXISTS final_drafts (\n"
            "    final_draft_id TEXT PRIMARY KEY,\n"
            "    human_review_status TEXT NOT NULL DEFAULT 'approved'\n"
            ");\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "schema.sql"
            fake_path.write_text(fake_schema, encoding="utf-8")
            with patch.object(gate, "SCHEMA_PATH", fake_path):
                result = gate.check_final_draft_not_auto_approved()
        self.assertFalse(result["passed"])


class NoAutoPublishPathTests(unittest.TestCase):
    def test_passes_with_the_real_repo(self) -> None:
        self.assertTrue(gate.check_no_auto_publish_path()["passed"])

    def test_fails_when_a_real_publish_call_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "scripts" / "core"
            fake_root.mkdir(parents=True)
            (fake_root / "sneaky_publisher.py").write_text(
                "def publish(content):\n    requests.post('https://platform.example/upload', data=content)\n",
                encoding="utf-8",
            )
            with patch.object(gate, "ROOT", Path(tmp)):
                result = gate.check_no_auto_publish_path()
        self.assertFalse(result["passed"])


class FormalResearchNoVideoPlatformImportTests(unittest.TestCase):
    def test_passes_with_the_real_repo(self) -> None:
        self.assertTrue(gate.check_formal_research_no_video_platform_import()["passed"])

    def test_fails_when_research_skill_imports_mediacrawler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            research_dir = Path(tmp) / "runtime_skills" / "research_evidence_extract"
            research_dir.mkdir(parents=True)
            (research_dir / "sneaky.py").write_text(
                "from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor\n",
                encoding="utf-8",
            )
            with patch.object(gate, "ROOT", Path(tmp)):
                result = gate.check_formal_research_no_video_platform_import()
        self.assertFalse(result["passed"])


class BenchmarkCollectionPipelineIntactTests(unittest.TestCase):
    def test_passes_with_the_real_repo(self) -> None:
        self.assertTrue(gate.check_benchmark_collection_pipeline_intact()["passed"])

    def test_fails_when_a_real_entrypoint_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(gate, "ROOT", Path(tmp)):
                result = gate.check_benchmark_collection_pipeline_intact()
        self.assertFalse(result["passed"])
        self.assertTrue(any("missing entirely" in item for item in result["detail"]))


class NoTopicScoringConfigTests(unittest.TestCase):
    def test_passes_with_the_real_repo(self) -> None:
        self.assertTrue(gate.check_no_topic_scoring_config()["passed"])

    def test_fails_when_candidate_pool_scoring_block_reappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_settings = Path(tmp) / "settings.yaml"
            fake_settings.write_text("candidate_pool:\n  decay_half_life_days: 7\n", encoding="utf-8")
            fake_example = Path(tmp) / "settings.example.yaml"
            fake_example.write_text("crawler:\n  daily_max_notes: 20\n", encoding="utf-8")
            with patch.object(gate, "SETTINGS_PATH", fake_settings), patch.object(gate, "SETTINGS_EXAMPLE_PATH", fake_example):
                result = gate.check_no_topic_scoring_config()
        self.assertFalse(result["passed"])


class TraceabilityFieldsOnPipelineTablesTests(unittest.TestCase):
    def test_passes_with_real_schema(self) -> None:
        self.assertTrue(gate.check_traceability_fields_on_pipeline_tables()["passed"])

    def test_fails_when_a_table_is_missing_run_id(self) -> None:
        fake_schema = (
            "CREATE TABLE IF NOT EXISTS topic_candidates (\n"
            "    topic_id TEXT PRIMARY KEY,\n"
            "    request_id TEXT,\n"
            "    correlation_id TEXT\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS content_plans (\n"
            "    plan_id TEXT PRIMARY KEY,\n"
            "    run_id TEXT,\n"
            "    request_id TEXT,\n"
            "    correlation_id TEXT\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS script_drafts (\n"
            "    draft_id TEXT PRIMARY KEY,\n"
            "    run_id TEXT,\n"
            "    request_id TEXT,\n"
            "    correlation_id TEXT\n"
            ");\n"
            "CREATE TABLE IF NOT EXISTS script_reviews (\n"
            "    review_id TEXT PRIMARY KEY,\n"
            "    run_id TEXT,\n"
            "    request_id TEXT,\n"
            "    correlation_id TEXT\n"
            ");\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "schema.sql"
            fake_path.write_text(fake_schema, encoding="utf-8")
            with patch.object(gate, "SCHEMA_PATH", fake_path):
                result = gate.check_traceability_fields_on_pipeline_tables()
        self.assertFalse(result["passed"])
        self.assertTrue(any("topic_candidates" in item and "run_id" in item for item in result["detail"]))


if __name__ == "__main__":
    unittest.main()
