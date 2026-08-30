"""Isolated checks that formal cold-start execution has no model binding."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.model_gateway.model_router import ModelRouter
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
    build_production_competitor_registration_gateway,
)
from tests._cold_start_test_model import resolve_task_model


def _payload(domain: str) -> dict:
    slug = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:12]
    return {
        "domain_mode": "create",
        "domain_name": domain,
        "platform": "douyin",
        "owned_account": {
            "display_name": "自营账号",
            "external_account_ref": f"douyin:owned-{slug}",
        },
        "competitor_accounts": [
            {
                "display_name": f"对标账号{i}",
                "external_account_ref": f"douyin:competitor-{slug}-{i}",
            }
            for i in range(20)
        ],
        "actor": "模型继承测试用户",
    }


def _context(model: str) -> dict:
    return {
        "task_model_name": model,
        "task_model_provider": "custom",
        "task_model_base_url": "https://mimo.example/v1",
    }


def _router() -> tuple[ModelRouter, dict[str, str]]:
    config = {
        "schema_version": "model_routes.v1",
        "model_selection": {
            "active_provider_ref": "MODEL_ACTIVE_PROVIDER_REF",
            "allowed_provider_refs": ["mimo_main", "relay_main"],
        },
        "model_positions": {
            "dialogue_model": "daily_chat",
            "business_model": "business_analysis",
            "writing_model": "writing_generation",
        },
        "model_providers": {
            "mimo_main": {
                "type": "mimo",
                "enabled": True,
                "provider_name": "hermes",
                "endpoint_ref": "MIMO_ENDPOINT",
                "auth_ref": "MIMO_TOKEN",
                "model_ref": "MIMO_DEFAULT_MODEL",
            },
            "relay_main": {
                "type": "openai_compatible",
                "enabled": True,
                "provider_name": "hermes",
                "endpoint_ref": "RELAY_ENDPOINT",
                "auth_ref": "RELAY_TOKEN",
                "model_ref": "RELAY_DEFAULT_MODEL",
            },
        },
        "model_routes": {
            "daily_chat": {
                "provider_ref": "active_provider",
                "fallback": "none",
                "allowed_task_types": ["daily_conversation"],
            },
            "business_analysis": {
                "provider_ref": "active_provider",
                "fallback": "none",
                "allowed_task_types": ["benchmark_analysis"],
            },
            "writing_generation": {
                "provider_ref": "active_provider",
                "fallback": "none",
                "allowed_task_types": ["final_draft"],
            },
        },
    }
    environment = {
        "MODEL_ACTIVE_PROVIDER_REF": "relay_main",
        "MIMO_ENDPOINT": "https://mimo.example/v1",
        "MIMO_TOKEN": "not-used",
        "MIMO_DEFAULT_MODEL": "legacy-mimo-default",
        "RELAY_ENDPOINT": "https://openrouter.ai/api/v1",
        "RELAY_TOKEN": "not-used",
        "RELAY_DEFAULT_MODEL": "stealth/ox-alpha",
    }
    return ModelRouter.from_config(config), environment


class HermesTaskModelInheritanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "domains"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection, db_path=Path(":memory:"), data_identity="test"
        )
        self.core.install_schema()
        self.service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=resolve_task_model,
        )

    def tearDown(self) -> None:
        self.core.close()
        self.temp_dir.cleanup()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)

    def _create(self, domain: str, model: str) -> dict:
        payload = _payload(domain)
        preview = self.service.preview(payload)
        return self.service.confirm(
            payload,
            trusted_internal_context=_context(model),
        )

    def test_a_new_run_and_breakdown_gateway_use_current_hermes_model(self) -> None:
        created = self._create("模型继承场景A", "model-a")
        self.assertEqual(created["run_model"], "")
        router, environment = _router()
        binding = router.resolve_hermes_task_binding(
            route_id="business_analysis",
            current_model="model-a",
            current_provider="custom",
            current_endpoint="https://mimo.example/v1",
            environment=environment,
            env_path=None,
        )

        class Provider:
            provider_name = "hermes"

            def complete(self, *_args, **_kwargs):
                raise AssertionError("isolated routing check must not call a model")

        production_core = Stage0ContentProductionCore(
            sqlite3.connect(":memory:"), db_path=Path(":memory:"), data_identity="production"
        )
        production_core.install_schema()
        try:
            with (
                patch(
                    "scripts.core.production.stage1_competitor_registration.ModelRouter.from_file",
                    return_value=router,
                ),
                patch(
                    "scripts.core.production.stage1_competitor_registration.build_configured_model_provider",
                    return_value=Provider(),
                ),
                patch.dict("os.environ", environment, clear=False),
            ):
                gateway = build_production_competitor_registration_gateway(
                    production_core,
                    task_model_binding=binding.as_payload(),
                )
            self.assertEqual(next(iter(gateway.routes.values())).model_name, "model-a")
        finally:
            production_core.close()

    def test_b_running_execution_keeps_a_and_new_run_inherits_b(self) -> None:
        first = self._create("模型继承场景B旧", "model-a")
        continued = self.service.continue_current_cold_start(
            configuration_id=first["configuration_id"],
            actor="模型继承测试用户",
            trusted_internal_context=_context("model-b"),
        )
        second = self._create("模型继承场景B新", "model-b")
        self.assertEqual(continued["cold_start_id"], first["cold_start_id"])
        self.assertEqual(continued["run_model"], "")
        self.assertEqual(second["run_model"], "")

    def test_c_stop_and_resume_update_execution_model_and_keep_the_run(self) -> None:
        created = self._create("模型继承场景C", "model-a")
        self.service.stop_current_cold_start(
            actor="",
            reason="isolated pause",
            cold_start_id=created["cold_start_id"],
            trusted_internal_context=_context("model-c-stop"),
        )
        resumed = self.service.resume_current_cold_start(
            actor="",
            trusted_internal_context=_context("model-b"),
            cold_start_id=created["cold_start_id"],
        )
        self.assertEqual(resumed["cold_start_id"], created["cold_start_id"])
        self.assertEqual(resumed["run_model"], "")
        self.assertFalse(resumed["created_new_run"])
        snapshot = json.loads(
            self.connection.execute(
                "SELECT input_snapshot_json FROM stage0_cold_start_run_contract "
                "WHERE cold_start_id=?",
                (created["cold_start_id"],),
            ).fetchone()[0]
        )
        self.assertNotIn("run_model", snapshot)
        audit_rows = list(
            self.connection.execute(
                "SELECT action, payload_json FROM stage0_audit_event "
                "WHERE action IN ('configured_cold_start_started', 'configured_cold_start_resumed') "
                "ORDER BY created_at, audit_id"
            )
        )
        self.assertNotIn("run_model", json.loads(audit_rows[0][1]))
        self.assertEqual(json.loads(audit_rows[1][1])["run_model_binding"], {})

    def test_d_resume_requires_current_runtime_model_without_old_binding_fallback(self) -> None:
        def resolver(context: dict[str, object] | None, _override: str | None) -> dict[str, object]:
            if not context or not str(context.get("task_model_name") or "").strip():
                return {}
            return resolve_task_model(context, None)

        service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=resolver,
        )
        payload = _payload("模型恢复缺少当前模型")
        created = service.confirm(
            payload,
            trusted_internal_context=_context("model-a"),
        )
        service.stop_current_cold_start(
            actor="",
            reason="isolated pause",
            cold_start_id=created["cold_start_id"],
        )
        resumed = service.resume_current_cold_start(
            actor="",
            cold_start_id=created["cold_start_id"],
        )
        self.assertEqual(resumed["run_model"], "")
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM stage0_cold_start WHERE cold_start_id=?",
                (created["cold_start_id"],),
            ).fetchone()[0],
            "running",
        )

    def test_e_legacy_stealth_default_cannot_override_hermes_current_model(self) -> None:
        router, environment = _router()
        binding = router.resolve_hermes_task_binding(
            route_id="business_analysis",
            current_model="model-a",
            current_provider="custom",
            current_endpoint="https://mimo.example/v1",
            environment=environment,
            env_path=None,
        )
        route = router.resolve_frozen_task_route(
            binding, environment=environment, env_path=None
        )
        self.assertEqual(environment["RELAY_DEFAULT_MODEL"], "stealth/ox-alpha")
        self.assertEqual(binding.provider_ref, "mimo_main")
        self.assertEqual(route.model_name, "model-a")

    def test_i_new_execution_reads_hermes_current_model_not_active_relay(self) -> None:
        router, environment = _router()
        environment = {
            **environment,
            "HERMES_BUSINESS_MODEL_CLASS": "mimo",
            "HERMES_BUSINESS_MODEL_NAME": "model-from-hermes",
            "HERMES_BUSINESS_MODEL_BASE_URL": "https://mimo.example/v1",
        }
        binding = router.resolve_current_hermes_execution_binding(
            route_id="business_analysis",
            environment=environment,
            env_path=None,
        )
        self.assertEqual(binding.provider_ref, "mimo_main")
        self.assertEqual(binding.model_name, "model-from-hermes")
        self.assertEqual(binding.source, "hermes_current_session")
        self.assertEqual(
            router.resolve_frozen_task_route(
                binding,
                environment={
                    **environment,
                    "HERMES_BUSINESS_MODEL_NAME": "model-switched-mid-execution",
                },
                env_path=None,
            ).model_name,
            "model-from-hermes",
        )

    def test_j_new_resume_execution_can_bind_the_new_hermes_model(self) -> None:
        router, environment = _router()
        first = router.resolve_current_hermes_execution_binding(
            route_id="business_analysis",
            environment={
                **environment,
                "HERMES_BUSINESS_MODEL_CLASS": "mimo",
                "HERMES_BUSINESS_MODEL_NAME": "model-a",
                "HERMES_BUSINESS_MODEL_BASE_URL": "https://mimo.example/v1",
            },
            env_path=None,
        )
        resumed = router.resolve_current_hermes_execution_binding(
            route_id="business_analysis",
            environment={
                **environment,
                "HERMES_BUSINESS_MODEL_CLASS": "mimo",
                "HERMES_BUSINESS_MODEL_NAME": "model-b",
                "HERMES_BUSINESS_MODEL_BASE_URL": "https://mimo.example/v1",
            },
            env_path=None,
        )
        self.assertEqual(first.model_name, "model-a")
        self.assertEqual(resumed.model_name, "model-b")

    def test_g_injected_binding_is_normalized_without_secrets(self) -> None:
        def resolver(context: dict[str, object] | None, override: str | None) -> dict[str, object]:
            binding = resolve_task_model(context, override)
            binding["api_key"] = "must-not-be-persisted"
            binding["access_token"] = "must-not-be-persisted"
            return binding

        service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=resolver,
        )
        payload = _payload("模型绑定脱敏")
        preview = service.preview(payload)
        created = service.confirm(
            payload,
            trusted_internal_context=_context("model-secret-test"),
        )
        snapshot = json.loads(
            self.connection.execute(
                "SELECT input_snapshot_json FROM stage0_cold_start_run_contract WHERE cold_start_id=?",
                (created["cold_start_id"],),
            ).fetchone()[0]
        )
        self.assertNotIn("run_model", snapshot)

    def test_h_invalid_injected_binding_fails_closed(self) -> None:
        def resolver(_context: dict[str, object] | None, _override: str | None) -> dict[str, object]:
            return {"model_name": "incomplete"}

        service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=resolver,
        )
        payload = _payload("模型绑定缺失")
        preview = service.preview(payload)
        result = service.confirm(payload)
        self.assertEqual(result["run_model"], "")
    def test_f_deterministic_hashtag_extraction_never_calls_model(self) -> None:
        executor = ConfiguredCompetitorRegistrationExecutor.__new__(
            ConfiguredCompetitorRegistrationExecutor
        )
        executor.progress_callback = None
        executor.gateway = Mock()
        artifacts = executor._tag_candidates(
            {},
            ({
                "step_name": "high_signal_identification",
                "artifact_refs": [{
                    "selected_items": [
                        {"source_id": "one", "title": "今天聊 #社会观察 #生活"},
                        {"source_id": "two", "title": "继续聊 #社会观察"},
                    ],
                }],
            },),
        )
        self.assertEqual(
            [item["tag"] for item in artifacts[0]["candidates"]],
            ["社会观察", "生活"],
        )
        self.assertEqual(artifacts[0]["model_run_id"], None)
        executor.gateway.run.assert_not_called()
