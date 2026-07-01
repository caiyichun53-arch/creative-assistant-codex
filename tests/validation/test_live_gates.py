from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import yaml

from scripts.core.model_gateway.goal07_model_gateway import ModelProviderResult, ModelRoute, ModelUsage
from scripts.core.model_gateway.hermes_model_provider import (
    HermesModelProviderAdapter,
    HermesModelProviderConfig,
    HermesModelProviderError,
)
from scripts.validation.live_gates import (
    GATE_IDS,
    LiveGateConfig,
    command_dry_run,
    command_preflight,
    run_gate_mode,
)


ROOT = Path(__file__).resolve().parents[2]


class LiveGateHarnessTests(unittest.TestCase):
    def make_config(
        self,
        tmp: Path,
        *,
        env_values: dict[str, str] | None = None,
        live_model_gate: bool = False,
    ) -> LiveGateConfig:
        source = yaml.safe_load((ROOT / "config" / "live_gates.example.yaml").read_text(encoding="utf-8"))
        source["evidence_root"] = str(tmp / "evidence")
        source["status_file"] = str(tmp / "status.yaml")
        if live_model_gate:
            source["allow_live_calls"] = True
            for gate in source["gates"]:
                if gate["gate_id"] == "GATE-MODEL-PROVIDER":
                    gate["live_enabled"] = True
        config_path = tmp / "live_gates.yaml"
        config_path.write_text(yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")
        env_path = tmp / ".env.live-gates"
        env_text = (ROOT / ".env.live-gates.example").read_text(encoding="utf-8")
        if env_values:
            env_text += "\n" + "\n".join(f"{key}={value}" for key, value in env_values.items()) + "\n"
        env_path.write_text(env_text, encoding="utf-8")
        return LiveGateConfig(config_path, env_path=env_path)

    def model_env(self, **overrides: str) -> dict[str, str]:
        values = {
            "MODEL_PROVIDER_API_KEY": "test-model-key",
            "MODEL_PROVIDER_BASE_URL": "https://example.invalid/v1",
            "MODEL_PROVIDER_MODEL": "test-hermes-model",
            "MODEL_PROVIDER_COST_CAP": "0.01",
        }
        values.update(overrides)
        return values

    def manifest_path(self, result) -> Path:
        path = Path(result.evidence_location)
        return path if path.is_absolute() else ROOT / path

    def test_config_contains_all_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.assertEqual(tuple(config.gates), GATE_IDS)
            self.assertFalse(config.allow_live_calls)
            self.assertTrue(config.shadow_only)

    def test_preflight_blocks_missing_credentials_without_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp)
            code = command_preflight(config, None, status_path=tmp / "status.yaml")
            self.assertEqual(code, 0)
            status = yaml.safe_load((tmp / "status.yaml").read_text(encoding="utf-8"))
            self.assertEqual(status["gates"]["GATE-FEISHU-THIN-BINDING"]["status"], "BLOCKED_MISSING_CREDENTIAL")

    def test_dry_run_all_gates_passes_without_external_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp)
            code = command_dry_run(config, None, status_path=tmp / "status.yaml")
            self.assertEqual(code, 0)
            status = yaml.safe_load((tmp / "status.yaml").read_text(encoding="utf-8"))
            for gate_id in GATE_IDS:
                latest = status["gates"][gate_id]["latest_result"]
                self.assertEqual(status["gates"][gate_id]["status"], "DRY_RUN_PASSED")
                self.assertFalse(latest["external_side_effect"])

    def test_single_gate_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp)
            result = run_gate_mode(
                config=config,
                gate_id="GATE-MODEL-PROVIDER",
                mode="dry-run",
                status_path=tmp / "status.yaml",
            )
            self.assertEqual(result.status, "DRY_RUN_PASSED")
            status = yaml.safe_load((tmp / "status.yaml").read_text(encoding="utf-8"))
            self.assertEqual(status["gates"]["GATE-MODEL-PROVIDER"]["status"], "DRY_RUN_PASSED")
            self.assertEqual(status["gates"]["GATE-ASR"]["status"], "NOT_READY")

    def test_model_preflight_blocks_missing_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            values = self.model_env(MODEL_PROVIDER_API_KEY="__PLACEHOLDER__")
            config = self.make_config(tmp, env_values=values)
            result = run_gate_mode(config=config, gate_id="GATE-MODEL-PROVIDER", mode="preflight", status_path=tmp / "status.yaml")
            self.assertEqual(result.status, "BLOCKED_MISSING_CREDENTIAL")
            self.assertIn("MODEL_PROVIDER_API_KEY", result.failure_reason)

    def test_model_preflight_blocks_missing_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            values = self.model_env(MODEL_PROVIDER_BASE_URL="__PLACEHOLDER__")
            config = self.make_config(tmp, env_values=values)
            result = run_gate_mode(config=config, gate_id="GATE-MODEL-PROVIDER", mode="preflight", status_path=tmp / "status.yaml")
            self.assertEqual(result.status, "BLOCKED_MISSING_CREDENTIAL")
            self.assertIn("MODEL_PROVIDER_BASE_URL", result.failure_reason)

    def test_model_preflight_blocks_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            values = self.model_env(MODEL_PROVIDER_MODEL="__PLACEHOLDER__")
            config = self.make_config(tmp, env_values=values)
            result = run_gate_mode(config=config, gate_id="GATE-MODEL-PROVIDER", mode="preflight", status_path=tmp / "status.yaml")
            self.assertEqual(result.status, "BLOCKED_MISSING_CREDENTIAL")
            self.assertIn("MODEL_PROVIDER_MODEL", result.failure_reason)

    def test_model_preflight_project_id_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            values = self.model_env(MODEL_PROVIDER_PROJECT_ID="__PLACEHOLDER__")
            config = self.make_config(tmp, env_values=values)
            result = run_gate_mode(config=config, gate_id="GATE-MODEL-PROVIDER", mode="preflight", status_path=tmp / "status.yaml")
            self.assertEqual(result.status, "PREFLIGHT_PASSED")
            manifest = yaml.safe_load((self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["response"]["cost_cap_unit"], "USD")

    def test_hermes_adapter_records_usage_not_available(self) -> None:
        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    id="provider-request-1",
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
                )

        class FakeClient:
            chat = SimpleNamespace(completions=FakeCompletions())

        adapter = HermesModelProviderAdapter(
            HermesModelProviderConfig(api_key="test-model-key", base_url="https://example.invalid/v1", model="test-model"),
            client_factory=lambda **kwargs: FakeClient(),
        )
        result = adapter.complete(
            SimpleNamespace(prompt="hello"),
            ModelRoute(
                route_name="test",
                provider_name="hermes",
                model_name="test-model",
                config_version="test",
                config_hash="hash",
            ),
        )
        self.assertEqual(result.usage.total_tokens, 0)
        self.assertEqual(result.metadata["usage_status"], "not_available")
        self.assertEqual(result.cost["status"], "not_available")

    def test_hermes_adapter_scrubs_api_key_from_errors(self) -> None:
        secret = "test-model-key"

        class FailingCompletions:
            def create(self, **kwargs):
                raise RuntimeError(f"provider rejected Bearer {secret}")

        class FakeClient:
            chat = SimpleNamespace(completions=FailingCompletions())

        adapter = HermesModelProviderAdapter(
            HermesModelProviderConfig(api_key=secret, base_url="https://example.invalid/v1", model="test-model"),
            client_factory=lambda **kwargs: FakeClient(),
        )
        with self.assertRaises(HermesModelProviderError) as raised:
            adapter.complete(
                SimpleNamespace(prompt="hello"),
                ModelRoute(
                    route_name="test",
                    provider_name="hermes",
                    model_name="test-model",
                    config_version="test",
                    config_hash="hash",
                ),
            )
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))

    def test_model_live_path_uses_gateway_and_not_dry_run_provider(self) -> None:
        class FakeHermesAdapter:
            provider_name = "hermes"

            def __init__(self, config):
                self.config = config

            def complete(self, request, route):
                assert route.provider_name == "hermes"
                return ModelProviderResult(
                    output_text="live-gate-hermes-model-ok",
                    usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    cost={"status": "not_available"},
                    provider_request_id="provider-request-1",
                    metadata={
                        "external_io": True,
                        "usage_status": "available",
                        "cost_status": "not_available",
                        "provider_request_id_status": "available",
                        "finish_reason": "stop",
                    },
                )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp, env_values=self.model_env(), live_model_gate=True)
            with mock.patch("scripts.validation.live_gates.HermesModelProviderAdapter", FakeHermesAdapter), mock.patch(
                "scripts.validation.live_gates.DryRunModelProvider",
                side_effect=AssertionError("DryRunModelProvider must not be used in live mode"),
            ):
                result = run_gate_mode(
                    config=config,
                    gate_id="GATE-MODEL-PROVIDER",
                    mode="run",
                    live_confirm=True,
                    environment="validation",
                    status_path=tmp / "status.yaml",
            )
            self.assertEqual(result.status, "LIVE_PASSED")
            manifest = yaml.safe_load((self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["response"]["provider"], "hermes")
            self.assertEqual(manifest["response"]["status"], "succeeded")
            self.assertTrue(manifest["response"]["input_hash"])
            self.assertTrue(manifest["response"]["output_hash"])

    def test_model_live_provider_error_fails_without_key_leak(self) -> None:
        class FailingHermesAdapter:
            provider_name = "hermes"

            def __init__(self, config):
                self.config = config

            def complete(self, request, route):
                raise RuntimeError("provider rejected request")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp, env_values=self.model_env(MODEL_PROVIDER_API_KEY="test-secret-key"), live_model_gate=True)
            with mock.patch("scripts.validation.live_gates.HermesModelProviderAdapter", FailingHermesAdapter):
                result = run_gate_mode(
                    config=config,
                    gate_id="GATE-MODEL-PROVIDER",
                    mode="run",
                    live_confirm=True,
                    environment="validation",
                    status_path=tmp / "status.yaml",
            )
            self.assertEqual(result.status, "LIVE_FAILED")
            self.assertNotIn("test-secret-key", result.failure_reason)
            manifest_text = (self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8")
            self.assertNotIn("test-secret-key", manifest_text)

    def test_model_live_cost_cap_exceeded_fails(self) -> None:
        class CostlyHermesAdapter:
            provider_name = "hermes"

            def __init__(self, config):
                self.config = config

            def complete(self, request, route):
                return ModelProviderResult(
                    output_text="live-gate-hermes-model-ok",
                    usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    cost={"currency": "USD", "amount": "0.02"},
                    provider_request_id="provider-request-1",
                    metadata={
                        "external_io": True,
                        "usage_status": "available",
                        "cost_status": "available",
                        "provider_request_id_status": "available",
                        "finish_reason": "stop",
                    },
                )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp, env_values=self.model_env(MODEL_PROVIDER_COST_CAP="0.01"), live_model_gate=True)
            with mock.patch("scripts.validation.live_gates.HermesModelProviderAdapter", CostlyHermesAdapter):
                result = run_gate_mode(
                    config=config,
                    gate_id="GATE-MODEL-PROVIDER",
                    mode="run",
                    live_confirm=True,
                    environment="validation",
                    status_path=tmp / "status.yaml",
                )
            self.assertEqual(result.status, "LIVE_FAILED")
            self.assertIn("MODEL_PROVIDER_COST_CAP exceeded", result.failure_reason)

    def test_hermes_host_gate_dry_run_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp)
            result = run_gate_mode(
                config=config,
                gate_id="GATE-HERMES-REAL-HOST",
                mode="dry-run",
                status_path=tmp / "status.yaml",
            )
            self.assertEqual(result.status, "DRY_RUN_PASSED")

    def test_live_run_requires_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp)
            result = run_gate_mode(
                config=config,
                gate_id="GATE-SHADOW-E2E",
                mode="run",
                live_confirm=False,
                status_path=tmp / "status.yaml",
            )
            self.assertEqual(result.status, "BLOCKED_MISSING_AUTHORIZATION")
            self.assertFalse(result.external_side_effect)

    def test_secret_leakage_static_check(self) -> None:
        candidate_files = [
            ROOT / ".env.live-gates.example",
            ROOT / "config" / "live_gates.example.yaml",
            ROOT / "EXTERNAL_LIVE_GATE_RUNBOOK.md",
            ROOT / "scripts" / "validation" / "live_gates.py",
        ]
        forbidden_fragments = ("sk-", "xoxb-", "tenant_access_token=", "sessionid=", "cookie=")
        for path in candidate_files:
            text = path.read_text(encoding="utf-8").lower()
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, text, f"{fragment} leaked in {path}")


if __name__ == "__main__":
    unittest.main()
