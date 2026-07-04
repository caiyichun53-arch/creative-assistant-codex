from __future__ import annotations

import unittest

from scripts.core.model_gateway.business_route_registry import (
    load_registry,
    run_fixture_route_tests,
    run_verification,
    scan_direct_model_calls,
    validate_registry,
    verify_hermes_isolation,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelRequest, ModelRoute
from scripts.core.model_gateway.hermes_model_provider import (
    HermesModelProviderAdapter,
    HermesModelProviderConfig,
    HermesModelProviderError,
)


class BusinessRouteRegistryTests(unittest.TestCase):
    def test_registry_defines_formal_business_nodes(self) -> None:
        registry = load_registry()
        result = validate_registry(registry)
        self.assertEqual(result["node_count"], 15)
        self.assertIn("business.topic_judgement", result["logical_routes"])
        self.assertIn("business.content_relation_judgement", result["logical_routes"])
        self.assertIn("business.source_to_topic", result["logical_routes"])
        self.assertIn("business.research_evidence_extract", result["logical_routes"])
        self.assertIn("business.creation_draft", result["logical_routes"])
        self.assertIn("business.ai_flavor_judge", result["logical_routes"])
        self.assertIn("business.experiment_review", result["logical_routes"])
        self.assertIn("business.experience_revision_propose", result["logical_routes"])

    def test_fixture_routes_execute_through_model_gateway_without_outbox(self) -> None:
        registry = load_registry()
        result = run_fixture_route_tests(registry)
        self.assertEqual(len(result["tested_nodes"]), 15)
        self.assertEqual(result["fixture_provider_call_count"], 15)
        self.assertEqual(result["outbox_count"], 0)
        self.assertTrue(all(item["provider_name"] == "hermes" for item in result["tested_nodes"]))

    def test_formal_production_roots_have_no_direct_cli_model_calls(self) -> None:
        result = scan_direct_model_calls()
        self.assertEqual(result["formal_production_direct_model_call_count"], 0)
        self.assertEqual(result["legacy_direct_model_call_count"], 0)

    def test_hermes_isolation_policy_blocks_recursive_chain(self) -> None:
        result = verify_hermes_isolation(load_registry())
        self.assertTrue(result["passed"])
        self.assertFalse(result["recursive_chain_possible"])
        self.assertTrue(result["tools_disabled"])
        self.assertTrue(result["memory_disabled"])
        self.assertTrue(result["feishu_messaging_disabled"])

    def test_hermes_provider_rejects_tools_and_side_effect_route_parameters(self) -> None:
        adapter = HermesModelProviderAdapter(
            HermesModelProviderConfig(api_key="test-key", base_url="https://example.invalid/v1", model="test-model"),
            client_factory=lambda **kwargs: object(),
        )
        forbidden_routes = [
            ModelRoute(
                route_name="business.creation_draft",
                provider_name="hermes",
                model_name="env:HERMES_BUSINESS_MODEL_NAME",
                config_version="test",
                config_hash="hash",
                parameters={"tools": [{"type": "function"}]},
            ),
            ModelRoute(
                route_name="business.creation_draft",
                provider_name="hermes",
                model_name="env:HERMES_BUSINESS_MODEL_NAME",
                config_version="test",
                config_hash="hash",
                parameters={"nested_jobs": True},
            ),
        ]
        for route in forbidden_routes:
            with self.subTest(route=route.parameters):
                with self.assertRaises(HermesModelProviderError):
                    adapter.complete(
                        ModelRequest(
                            route_name=route.route_name,
                            prompt="fixture",
                            input_payload={"fixture_id": "fixture"},
                        ),
                        route,
                    )

    def test_full_verification_status_is_completed(self) -> None:
        status = run_verification()
        self.assertEqual(status["status"], "COMPLETED")
        self.assertEqual(status["registry"]["node_count"], 15)
        self.assertEqual(status["clean_room_formal_db"]["total_rows"], 0)


if __name__ == "__main__":
    unittest.main()
