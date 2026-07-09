from __future__ import annotations

import unittest

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelRunMaterializer,
    ModelUsage,
)
from scripts.core.model_gateway.model_router import (
    DEFAULT_MODEL_ROUTES_PATH,
    ModelRouter,
    ModelRouterError,
    ROOT,
)
from scripts.core.persistence.goal01_store import PersistenceStore


class FailingProvider:
    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        self.calls = 0

    def complete(self, request: ModelRequest, route: ModelRoute) -> ModelProviderResult:
        self.calls += 1
        raise RuntimeError("planned provider failure")


class ModelRouterTests(unittest.TestCase):
    def test_route_id_resolves_to_provider_ref(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        route = router.resolve("business_analysis", route_name="business.topic_judgement")
        self.assertEqual(route.route_id, "business_analysis")
        self.assertEqual(route.provider_name, "mimo_main")
        self.assertEqual(route.provider_ref, "mimo_main")

    def test_routes_can_point_to_different_providers(self) -> None:
        # 2026-07-09: the multi_provider example no longer includes a "codex"
        # provider type. Claude Code/Codex are engineering tools used to
        # write this repo's code, never a valid runtime model provider -- an
        # example config that showed Codex as a business_model provider
        # option was itself a documented instance of the conflation this
        # repo now forbids.
        router = ModelRouter.from_file(ROOT / "config" / "model_routes.example.multi_provider.yaml")
        providers = {router.resolve(route_id).provider_ref for route_id in router.routes}
        self.assertIn("mimo_main", providers)
        self.assertIn("gpt_api_gateway", providers)
        self.assertNotIn("gpt_subscription_codex", providers)

    def test_active_production_readiness_config_is_mimo_only(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        providers = {router.resolve(route_id).provider_ref for route_id in router.routes}
        self.assertEqual(providers, {"mimo_main"})

    def test_all_routes_can_point_to_one_provider(self) -> None:
        router = ModelRouter.from_file(ROOT / "config" / "model_routes.example.single_provider.yaml")
        providers = {router.resolve(route_id).provider_ref for route_id in router.routes}
        self.assertEqual(providers, {"mimo_main"})

    def test_missing_route_id_fails_explicitly(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        with self.assertRaisesRegex(ModelRouterError, "unknown route_id"):
            router.resolve("missing_route")

    def test_missing_provider_ref_fails_explicitly(self) -> None:
        config = _base_config()
        config["model_routes"]["business_analysis"]["provider_ref"] = "missing_provider"
        with self.assertRaisesRegex(ModelRouterError, "provider_ref does not exist"):
            ModelRouter.from_config(config)

    def test_disabled_provider_fails_explicitly(self) -> None:
        config = _base_config()
        config["model_providers"]["gpt_subscription_codex"]["enabled"] = False
        router = ModelRouter.from_config(config)
        with self.assertRaisesRegex(ModelRouterError, "provider is disabled"):
            router.resolve("business_analysis")

    def test_non_none_fallback_fails_explicitly(self) -> None:
        config = _base_config()
        config["model_routes"]["business_analysis"]["fallback"] = "mimo_main"
        with self.assertRaisesRegex(ModelRouterError, "fallback must be none"):
            ModelRouter.from_config(config)

    def test_workflow_node_cannot_bypass_route_id_with_provider_or_model(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        with self.assertRaisesRegex(ModelRouterError, "must not bind provider_name directly"):
            router.validate_workflow_node_bindings(
                [
                    {
                        "node_id": "bad_node",
                        "logical_route": "business.bad",
                        "route_id": "business_analysis",
                        "task_type": "topic_screening",
                        "provider_name": "gpt_subscription_codex",
                    }
                ]
            )

    def test_writing_nodes_bind_writing_generation(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        router.validate_workflow_node_bindings(
            [
                {
                    "node_id": "creation_draft",
                    "logical_route": "business.creation_draft",
                    "route_id": "writing_generation",
                    "task_type": "rough_draft",
                }
            ]
        )
        with self.assertRaisesRegex(ModelRouterError, "business_analysis node"):
            router.validate_workflow_node_bindings(
                [
                    {
                        "node_id": "creation_draft",
                        "logical_route": "business.creation_draft",
                        "route_id": "business_analysis",
                        "task_type": "rough_draft",
                    }
                ]
            )

    def test_business_analysis_cannot_bind_writing_task_type(self) -> None:
        router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
        with self.assertRaisesRegex(ModelRouterError, "business_analysis node"):
            router.validate_workflow_node_bindings(
                [
                    {
                        "node_id": "bad_analysis",
                        "logical_route": "business.bad_analysis",
                        "route_id": "business_analysis",
                        "task_type": "final_draft",
                    }
                ]
            )

    def test_provider_failure_does_not_switch_provider(self) -> None:
        store = PersistenceStore.in_memory()
        try:
            router = ModelRouter.from_file(DEFAULT_MODEL_ROUTES_PATH)
            route = router.resolve("business_analysis", route_name="business.topic_judgement")
            provider = FailingProvider(route.provider_name)
            fallback_provider = FailingProvider("gpt_subscription_codex")
            gateway = ModelGateway(
                routes={route.route_name: route},
                providers={route.provider_name: provider, fallback_provider.provider_name: fallback_provider},
                materializer=ModelRunMaterializer(store),
            )
            with self.assertRaises(ModelGatewayError):
                gateway.complete(ModelRequest(route_name=route.route_name, prompt="prompt", input_payload={}))
            self.assertEqual(provider.calls, 1)
            self.assertEqual(fallback_provider.calls, 0)
        finally:
            store.conn.close()


def _base_config() -> dict:
    return {
        "schema_version": "model_routes.v1",
        "model_providers": {
            "mimo_main": {"type": "mimo", "enabled": True, "auth_ref": "MIMO_AUTH"},
            "gpt_subscription_codex": {
                "type": "codex_cli_subscription",
                "enabled": True,
                "auth_ref": "CODEX_SUBSCRIPTION_AUTH",
            },
        },
        "model_routes": {
            "business_analysis": {
                "provider_ref": "gpt_subscription_codex",
                "fallback": "none",
                "allowed_task_types": ["topic_screening"],
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
