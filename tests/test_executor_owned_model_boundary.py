"""Regression checks for the executor-owned model boundary."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.mcp.creation_assistant_mcp_server import SUPPORTED_EXTERNAL_TASK_TYPES
from scripts.core.production.stage0_content_core import StateTransitionError
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
    prepare_test_only_competitor_breakdown_batch,
)
from scripts.core.production.stage1a_research_plan import Stage1AResearchPlanService
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService
from scripts.core.production.stage1c_content_pipeline import Stage1CContentPipelineService
from scripts.core.production.experience_candidate_proposal import (
    ExperienceCandidateProposalService,
)


FORMAL_TASK_TYPES = (
    "source_to_topic",
    "competitor_breakdown",
    "research_plan",
    "content_deep_research",
    "content_plan_generation",
    "formal_draft_generate",
    "copy_optimization",
    "de_ai_revision",
    "final_content_review",
    "experience_candidate_propose",
    "domain_boundary_proposal",
)


def _material() -> dict[str, object]:
    return {
        "source_id": "boundary-test-material",
        "title": "A supplied material",
        "transcript": "This is supplied transcript material for an architecture test.",
        "metrics": {"views": 1},
        "comments": [],
        "domain_label": "music_entertainment",
    }


def _assert_no_model_choice(value: object) -> None:
    forbidden = {
        "model",
        "model_name",
        "provider",
        "provider_name",
        "provider_ref",
        "fallback",
        "route",
        "routing",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            assert str(key).casefold() not in forbidden, key
            _assert_no_model_choice(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_model_choice(child)


class ExecutorOwnedModelBoundaryTests(unittest.TestCase):
    def test_f_deterministic_hashtag_extraction_never_calls_model(self) -> None:
        executor = ConfiguredCompetitorRegistrationExecutor.__new__(
            ConfiguredCompetitorRegistrationExecutor
        )
        executor.progress_callback = None
        artifacts = executor._tag_candidates(
            {},
            (
                {
                    "step_name": "high_signal_identification",
                    "artifact_refs": [
                        {
                            "selected_items": [
                                {"source_id": "one", "title": "Today's #social #life"},
                                {"source_id": "two", "title": "More #social"},
                            ],
                        }
                    ],
                },
            ),
        )
        self.assertEqual(
            [item["tag"] for item in artifacts[0]["candidates"]],
            ["social", "life"],
        )
        self.assertIsNone(artifacts[0]["model_run_id"])

    def test_registered_task_types_use_one_shared_external_protocol(self) -> None:
        self.assertEqual(SUPPORTED_EXTERNAL_TASK_TYPES, FORMAL_TASK_TYPES)
        source_root = Path(__file__).resolve().parents[1] / "scripts" / "core" / "production"
        production_modules = (
            "stage1_competitor_registration.py",
            "stage1a_research_plan.py",
            "stage1b_daily_discovery.py",
            "stage1c_content_pipeline.py",
            "experience_candidate_proposal.py",
            "domain_boundary_lifecycle.py",
        )
        forbidden_imports = (
            "scripts.core.model_gateway.configured_provider",
            "scripts.core.model_gateway.goal07_model_gateway",
            "scripts.core.model_gateway.model_router",
        )
        for filename in production_modules:
            source = (source_root / filename).read_text(encoding="utf-8")
            self.assertFalse(any(marker in source for marker in forbidden_imports), filename)
            self.assertNotIn("FormalBusinessSkillAdapter", source, filename)
            self.assertNotIn("build_configured_model_provider", source, filename)
            self.assertNotIn("ModelGateway(", source, filename)

    def test_old_model_environment_cannot_change_an_external_task(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MODEL_ACTIVE_PROVIDER_REF": "relay_main",
                "GPT_RELAY_MODEL": "legacy-model-a",
                "HERMES_BUSINESS_MODEL_NAME": "old-model-a",
            },
            clear=False,
        ):
            first = prepare_test_only_competitor_breakdown_batch(
                test_id="environment-independent",
                materials=[_material()],
            )
        with patch.dict(
            os.environ,
            {
                "MODEL_ACTIVE_PROVIDER_REF": "mimo_main",
                "GPT_RELAY_MODEL": "legacy-model-b",
                "HERMES_BUSINESS_MODEL_NAME": "old-model-b",
            },
            clear=False,
        ):
            second = prepare_test_only_competitor_breakdown_batch(
                test_id="environment-independent",
                materials=[_material()],
            )
        self.assertEqual(first, second)
        _assert_no_model_choice(first)

    def test_formal_services_reject_direct_model_gateway_inputs(self) -> None:
        core = SimpleNamespace(data_identity="test")
        constructors = (
            lambda: Stage1AResearchPlanService(core=core, gateway=object()),
            lambda: Stage1BDailyDiscoveryService(core=core, gateway=object()),
            lambda: Stage1CContentPipelineService(core=core, gateway=object()),
            lambda: ExperienceCandidateProposalService(core=core, gateway=object()),
        )
        for constructor in constructors:
            with self.subTest(service=constructor):
                with self.assertRaises(StateTransitionError):
                    constructor()

    def test_competitor_executor_rejects_direct_model_gateway(self) -> None:
        with self.assertRaisesRegex(StateTransitionError, "external executor"):
            ConfiguredCompetitorRegistrationExecutor(
                core=SimpleNamespace(data_identity="test"),
                collector=object(),
                transcriber=object(),
                media_materializer=object(),
                gateway=object(),
            )

    def test_formal_runtime_no_longer_accepts_model_binding(self) -> None:
        from scripts.agent_platform.daily_operations_runtime import DailyOperationsCoordinator
        from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
        from scripts.core.production.stage1_daily_operations import ProductionDailyOperationsService

        self.assertNotIn(
            "task_model_binding",
            inspect.signature(DailyOperationsCoordinator._run_production).parameters,
        )
        self.assertNotIn(
            "task_model_binding",
            inspect.signature(CreationAssistantFormalBusinessCore.execute_daily).parameters,
        )
        self.assertNotIn(
            "task_model_binding",
            inspect.signature(ProductionDailyOperationsService).parameters,
        )


if __name__ == "__main__":
    unittest.main()
