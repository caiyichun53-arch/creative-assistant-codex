from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.external_adapters import ExternalAdapterCommand, LocalTrendRadarExecutor


class LocalTrendRadarExecutorTests(unittest.TestCase):
    @staticmethod
    def _command() -> ExternalAdapterCommand:
        return ExternalAdapterCommand(
            adapter_id="collector.trendradar",
            capability="hotspot.daily_snapshot",
            executable="trendradar",
            args=(),
            input_payload={"provider": "trendradar"},
            max_items=100,
            timeout_seconds=30,
        )

    def test_runs_argument_vector_without_shell_and_reads_normalized_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "python.exe").write_text("test", encoding="utf-8")
            (project / "hotspots.json").write_text(
                json.dumps({"items": [{"id": "h1", "title": "社会热点", "url": "https://example.test/h1"}]}),
                encoding="utf-8",
            )
            executor = LocalTrendRadarExecutor(
                project_dir=project,
                executable=Path("python.exe"),
                command_args=("main.py",),
                normalized_json_path=Path("hotspots.json"),
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with patch("scripts.core.external_adapters.local_trendradar_executor.subprocess.run", return_value=completed) as run:
                result = executor.execute(self._command())
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(len(result.payload["items"]), 1)
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.args[0][0], str(project / "python.exe"))

    def test_missing_normalized_export_is_a_failed_result_not_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "python.exe").write_text("test", encoding="utf-8")
            executor = LocalTrendRadarExecutor(
                project_dir=project,
                executable=Path("python.exe"),
                command_args=("main.py",),
                normalized_json_path=Path("missing.json"),
            )
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            with patch("scripts.core.external_adapters.local_trendradar_executor.subprocess.run", return_value=completed) as run:
                result = executor.execute(self._command())
            self.assertEqual(result.status, "failed_output_missing")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
