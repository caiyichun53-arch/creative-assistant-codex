import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService


class SourceToTopicStructuredOutputBindingTests(unittest.TestCase):
    def test_active_source_to_topic_consumer_uses_external_task_boundary(self) -> None:
        captured: dict = {}

        class Contract:
            formal_skill_id = "source_to_topic"
            version = "1.2.0"
            input_schema = {}
            output_schema = {}
            model_input_schema = {}
            model_output_schema = {}
            input_map = {}
            output_map = {}
            binding_name = "source_to_topic_public_input_to_topic_generation"
            binding_version = "1.1.0"
            skill_hash = "skill-hash"
            binding_hash = "binding-hash"
            model_response_format = {"type": "json_object"}
            prompt_template = "formal source-to-topic Skill"

            def validate_contract(self, **_kwargs) -> None:
                return None

            def portable_skill(self):
                return SimpleNamespace(render_prompt=lambda _payload: "prompt")

        class Core:
            def prepare_discovery_external_task(self, **kwargs):
                captured.update(kwargs)
                return {
                    "task_type": "source_to_topic",
                    "skill": {"formal_skill_id": "source_to_topic"},
                    "input": {},
                    "constraints": {},
                    "output_requirements": {},
                }

            def record_discovery_external_execution(self, **_kwargs):
                return "external-execution-1"

        service = SimpleNamespace(
            core=Core(),
            source_to_topic_contract=Contract(),
            external_executor=lambda _task: {
                "execution_id": "execution-1",
                "executor_id": "isolated-executor",
                "model_ref": "isolated-model",
                "output": {"ignored": True},
            },
        )

        def fake_binding(_binding_map, _input_payload, model_output, _preprocessed):
            if model_output:
                return {"execution_review": {"respected_domain_boundary": True}}
            return {}

        with (
            patch("scripts.core.production.stage1b_daily_discovery.enforce_atomic_skill_runtime_guard"),
            patch("scripts.core.production.stage1b_daily_discovery.validate_payload"),
            patch("scripts.core.production.stage1b_daily_discovery.preprocess_formal_skill_input", return_value={}),
            patch("scripts.core.production.stage1b_daily_discovery.apply_binding", side_effect=fake_binding),
            patch("scripts.core.production.stage1b_daily_discovery.validate_source_to_topic_output_semantics"),
        ):
            Stage1BDailyDiscoveryService._run_source_to_topic_skill(
                service,
                run_id="run-1",
                source_version_id="source-1",
                assembly_id="assembly-1",
                input_payload={},
            )

        self.assertEqual(captured["skill"]["formal_skill_id"], "source_to_topic")
        self.assertNotIn("model_route", captured)
        self.assertNotIn("provider", captured)


if __name__ == "__main__":
    unittest.main()
