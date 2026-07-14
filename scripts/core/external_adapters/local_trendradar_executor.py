from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalCommandResult,
)


@dataclass(frozen=True)
class LocalTrendRadarExecutor:
    """Run an explicitly configured TrendRadar command and read its normalized JSON export.

    The command is an argument vector, never a shell expression.  The configured export
    must be an object containing an ``items`` list; this adapter does not guess a
    TrendRadar database schema or scrape its rendered reports.
    """

    project_dir: Path
    executable: Path
    command_args: tuple[str, ...]
    normalized_json_path: Path
    timeout_seconds: int = 180

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        if command.adapter_id != "collector.trendradar" or command.capability != "hotspot.daily_snapshot":
            raise ExternalAdapterError("unsupported TrendRadar adapter command")
        if not self.project_dir.is_dir():
            raise ExternalAdapterError("configured TrendRadar project_dir does not exist")
        executable = self.executable if self.executable.is_absolute() else self.project_dir / self.executable
        export_path = self.normalized_json_path if self.normalized_json_path.is_absolute() else self.project_dir / self.normalized_json_path
        if not executable.exists():
            raise ExternalAdapterError("configured TrendRadar executable does not exist")

        try:
            completed = subprocess.run(
                [str(executable), *self.command_args],
                cwd=self.project_dir,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(self.timeout_seconds, command.timeout_seconds),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return ExternalCommandResult(
                status="failed_timeout",
                payload={"error": "TrendRadar timed out; automatic retry is forbidden"},
                external_side_effect=True,
            )
        if completed.returncode != 0:
            return ExternalCommandResult(
                status=f"failed_exit_{completed.returncode}",
                payload={"error": "TrendRadar exited with a non-zero status"},
                external_side_effect=True,
            )
        if not export_path.is_file():
            return ExternalCommandResult(
                status="failed_output_missing",
                payload={"error": "configured normalized TrendRadar export is missing"},
                external_side_effect=True,
            )
        try:
            payload: Any = json.loads(export_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return ExternalCommandResult(
                status="failed_output_invalid",
                payload={"error": f"normalized TrendRadar export is unreadable: {exc}"},
                external_side_effect=True,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return ExternalCommandResult(
                status="failed_output_invalid",
                payload={"error": "normalized TrendRadar export must contain an items list"},
                external_side_effect=True,
            )
        return ExternalCommandResult(
            status="succeeded",
            payload=payload,
            raw_archive_ref=str(export_path),
            external_side_effect=True,
        )
