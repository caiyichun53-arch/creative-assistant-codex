from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.validation.live_gates import (
    GATE_IDS,
    LiveGateConfig,
    command_dry_run,
    command_preflight,
    run_gate_mode,
)


ROOT = Path(__file__).resolve().parents[2]


class LiveGateHarnessTests(unittest.TestCase):
    def make_config(self, tmp: Path) -> LiveGateConfig:
        source = yaml.safe_load((ROOT / "config" / "live_gates.example.yaml").read_text(encoding="utf-8"))
        source["evidence_root"] = str(tmp / "evidence")
        source["status_file"] = str(tmp / "status.yaml")
        config_path = tmp / "live_gates.yaml"
        config_path.write_text(yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")
        env_path = tmp / ".env.live-gates"
        env_path.write_text((ROOT / ".env.live-gates.example").read_text(encoding="utf-8"), encoding="utf-8")
        return LiveGateConfig(config_path, env_path=env_path)

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
