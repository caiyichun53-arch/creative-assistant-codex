from __future__ import annotations

import decimal
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
    _cost_cap_result,
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
        live_host_gate: bool = False,
    ) -> LiveGateConfig:
        source = yaml.safe_load((ROOT / "config" / "live_gates.example.yaml").read_text(encoding="utf-8"))
        source["evidence_root"] = str(tmp / "evidence")
        source["status_file"] = str(tmp / "status.yaml")
        if live_model_gate or live_host_gate:
            source["allow_live_calls"] = True
            for gate in source["gates"]:
                if live_model_gate and gate["gate_id"] == "GATE-MODEL-PROVIDER":
                    gate["live_enabled"] = True
                if live_host_gate and gate["gate_id"] == "GATE-HERMES-REAL-HOST":
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
            self.assertEqual(manifest["response"]["billing_mode"], "subscription")
            self.assertEqual(manifest["response"]["monetary_cost_cap"], "not_applicable")

    def test_model_preflight_cost_cap_is_optional_for_subscription_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            values = self.model_env(MODEL_PROVIDER_COST_CAP="__PLACEHOLDER__")
            config = self.make_config(tmp, env_values=values)
            result = run_gate_mode(config=config, gate_id="GATE-MODEL-PROVIDER", mode="preflight", status_path=tmp / "status.yaml")
            self.assertEqual(result.status, "PREFLIGHT_PASSED")
            manifest = yaml.safe_load((self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertNotIn("MODEL_PROVIDER_COST_CAP", manifest["response"]["required_fields"])
            self.assertIn("MODEL_PROVIDER_COST_CAP", manifest["response"]["optional_fields"])

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
        self.assertEqual(result.metadata["billing_mode"], "subscription")
        self.assertEqual(result.metadata["cost_status"], "not_reported")
        self.assertEqual(result.metadata["visible_output_status"], "available")
        self.assertEqual(result.cost["status"], "not_reported")
        self.assertEqual(result.cost["billing_mode"], "subscription")

    def test_hermes_adapter_allows_empty_visible_output_for_minimal_token_gate(self) -> None:
        class FakeCompletions:
            def create(self, **kwargs):
                return SimpleNamespace(
                    id="provider-request-empty",
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=8, total_tokens=9),
                    choices=[SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")],
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
        self.assertEqual(result.output_text, "")
        self.assertEqual(result.metadata["visible_output_status"], "empty")
        self.assertEqual(result.metadata["cost_status"], "not_reported")

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
        call_count = 0

        class FakeHermesAdapter:
            provider_name = "hermes"

            def __init__(self, config):
                self.config = config

            def complete(self, request, route):
                nonlocal call_count
                call_count += 1
                assert route.provider_name == "hermes"
                assert route.parameters["max_completion_tokens"] == 128
                assert route.parameters["reasoning_effort"] == "low"
                return ModelProviderResult(
                    output_text="MODEL_GATE_OK",
                    usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    cost={"status": "not_reported", "billing_mode": "subscription"},
                    provider_request_id="provider-request-1",
                    metadata={
                        "external_io": True,
                        "billing_mode": "subscription",
                        "usage_status": "available",
                        "cost_status": "not_reported",
                        "provider_request_id_status": "available",
                        "finish_reason": "stop",
                        "visible_output_status": "available",
                        "retry_count": 0,
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
            self.assertEqual(call_count, 1)
            manifest = yaml.safe_load((self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["response"]["provider"], "hermes")
            self.assertEqual(manifest["response"]["status"], "succeeded")
            self.assertEqual(manifest["response"]["billing_mode"], "subscription")
            self.assertEqual(manifest["response"]["cost_status"], "not_reported")
            self.assertEqual(manifest["response"]["monetary_cost_cap"], "not_applicable")
            self.assertEqual(manifest["response"]["visible_output_status"], "available")
            self.assertTrue(manifest["response"]["expected_output_match"])
            self.assertEqual(manifest["response"]["actual_call_count"], 1)
            self.assertEqual(manifest["response"]["live_call_limit"], 1)
            self.assertEqual(manifest["response"]["max_retries"], 0)
            self.assertEqual(manifest["response"]["retry_count"], 0)
            self.assertEqual(manifest["response"]["timeout_ms"], 30000)
            self.assertEqual(manifest["response"]["gate_max_output_tokens"], 128)
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

    def test_metered_provider_cost_cap_exceeded_fails_when_enabled(self) -> None:
        class CostlyMeteredAdapter:
            provider_name = "hermes"

            def __init__(self, config):
                self.config = config

            def complete(self, request, route):
                return ModelProviderResult(
                    output_text="MODEL_GATE_OK",
                    usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    cost={"currency": "USD", "amount": "0.02"},
                    provider_request_id="provider-request-1",
                    metadata={
                        "external_io": True,
                        "billing_mode": "metered",
                        "usage_status": "available",
                        "cost_status": "available",
                        "provider_request_id_status": "available",
                        "finish_reason": "stop",
                    },
                )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(tmp, env_values=self.model_env(MODEL_PROVIDER_COST_CAP="0.01"), live_model_gate=True)
            with mock.patch("scripts.validation.live_gates.HermesModelProviderAdapter", CostlyMeteredAdapter):
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

    def test_metered_cost_cap_helper_still_compares_amounts(self) -> None:
        result = _cost_cap_result({"currency": "USD", "amount": "0.005"}, decimal.Decimal("0.01"), billing_mode="metered")
        self.assertEqual(result["cost_cap_status"], "within_cap")
        self.assertEqual(result["cost_cap_comparison"], "actual_usd_amount_lte_configured_usd_cap")

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

    def test_hermes_host_live_path_calls_authenticated_api_server(self) -> None:
        class FakeResponse:
            def __init__(self, status: int, payload: dict):
                self.status = status
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                import json

                return json.dumps(self.payload).encode("utf-8")

        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append((request.full_url, dict(request.header_items()), timeout))
            if request.full_url.endswith("/health"):
                return FakeResponse(200, {"status": "ok"})
            if request.full_url.endswith("/health/detailed"):
                return FakeResponse(200, {"gateway_state": "running"})
            if request.full_url.endswith("/v1/models"):
                return FakeResponse(200, {"data": [{"id": "hermes-agent"}]})
            if request.full_url.endswith("/v1/chat/completions"):
                return FakeResponse(
                    200,
                    {
                        "id": "chatcmpl-test",
                        "choices": [{"message": {"content": "HERMES_HOST_GATE_OK"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = self.make_config(
                tmp,
                live_host_gate=True,
                env_values={
                    "HERMES_TEST_HOST_URL": "http://127.0.0.1:8642",
                    "HERMES_TEST_TOKEN": "test-host-token",
                    "HERMES_TEST_ACTOR_ID": "gate-test-local",
                },
            )
            with mock.patch("scripts.validation.live_gates.urlopen", fake_urlopen):
                result = run_gate_mode(
                    config=config,
                    gate_id="GATE-HERMES-REAL-HOST",
                    mode="run",
                    live_confirm=True,
                    status_path=tmp / "status.yaml",
                )
            self.assertEqual(result.status, "LIVE_PASSED")
            manifest = yaml.safe_load((self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["response"]["interface_type"], "openai-compatible-api-server")
            self.assertEqual(manifest["response"]["host"], "http://127.0.0.1:8642")
            self.assertTrue(manifest["response"]["expected_output_match"])
            self.assertEqual(manifest["response"]["actual_call_count"], 1)
            self.assertFalse(manifest["response"]["dry_run_fallback"])
            self.assertNotIn("test-host-token", (self.manifest_path(result) / "run_manifest.yaml").read_text(encoding="utf-8"))
            self.assertTrue(any(url.endswith("/v1/chat/completions") for url, _, _ in calls))
            auth_calls = [headers for url, headers, _ in calls if "/v1/" in url]
            self.assertTrue(all(headers.get("Authorization") == "Bearer test-host-token" for headers in auth_calls))
            self.assertTrue(all(headers.get("X-hermes-session-key") == "gate-test-local" for headers in auth_calls))

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
