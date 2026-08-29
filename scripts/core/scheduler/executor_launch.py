"""Minimal launch boundary for the configured external intelligence executor."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence

from scripts.core.external_adapters.windows_process import hidden_process_kwargs


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EXECUTOR_CONFIG_PATH = PROJECT_ROOT / "config" / "external_executor.json"
CONFIG_VERSION = "creation_assistant_external_executor_config_v1"


class ExternalExecutorConfigurationError(ValueError):
    """The configured external executor cannot be used."""


class ExternalExecutorUnavailable(RuntimeError):
    """The configured executor could not be started."""


@dataclass(frozen=True)
class ExecutorLaunchSpec:
    command: str
    args: tuple[str, ...]
    transport: str


class ExternalExecutorConfiguration:
    """Read-only executor selection and launch facts, without model policy."""

    def __init__(self, *, path: Path, payload: dict[str, Any]) -> None:
        self.path = path
        self.default_executor = str(payload["default_executor"])
        self._launch_specs = {
            executor_id: self._parse_launch(executor_id, value)
            for executor_id, value in payload["executors"].items()
        }

    @classmethod
    def load(
        cls, path: Path = DEFAULT_EXECUTOR_CONFIG_PATH
    ) -> "ExternalExecutorConfiguration":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ExternalExecutorConfigurationError(
                f"external executor configuration not found: {path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ExternalExecutorConfigurationError(
                f"invalid external executor configuration: {path}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("config_version") != CONFIG_VERSION:
            raise ExternalExecutorConfigurationError("unsupported external executor config")
        executors = payload.get("executors")
        default_executor = payload.get("default_executor")
        if not isinstance(executors, dict) or not executors:
            raise ExternalExecutorConfigurationError("at least one executor is required")
        if not isinstance(default_executor, str) or default_executor not in executors:
            raise ExternalExecutorConfigurationError("default_executor must be configured")
        return cls(path=path, payload=payload)

    @staticmethod
    def _parse_launch(executor_id: str, value: Any) -> ExecutorLaunchSpec:
        if not isinstance(value, dict):
            raise ExternalExecutorConfigurationError(
                f"executor {executor_id} configuration must be an object"
            )
        launch = value.get("launch")
        if not isinstance(launch, dict):
            raise ExternalExecutorConfigurationError(
                f"executor {executor_id} needs one launch configuration"
            )
        command = launch.get("command")
        args = launch.get("args", [])
        transport = launch.get("transport")
        if not isinstance(command, str) or not command.strip():
            raise ExternalExecutorConfigurationError(
                f"executor {executor_id}.launch.command is required"
            )
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ExternalExecutorConfigurationError(
                f"executor {executor_id}.launch.args must be string arguments"
            )
        if transport != "stdio":
            raise ExternalExecutorConfigurationError(
                f"executor {executor_id} must use the existing stdio boundary"
            )
        return ExecutorLaunchSpec(
            command=command,
            args=tuple(args),
            transport=transport,
        )

    def launch_spec(self, executor_id: str | None = None) -> ExecutorLaunchSpec:
        selected = executor_id or self.default_executor
        try:
            return self._launch_specs[selected]
        except KeyError as exc:
            raise ExternalExecutorConfigurationError(
                f"unknown external executor: {selected}"
            ) from exc

    def executor_ids(self) -> tuple[str, ...]:
        return tuple(self._launch_specs)


class ExecutorLauncher:
    """Start one configured executor exactly once when the caller says it is absent."""

    def __init__(
        self,
        configuration: ExternalExecutorConfiguration | None = None,
    ) -> None:
        self.configuration = configuration or ExternalExecutorConfiguration.load()

    def ensure_started(
        self,
        *,
        executor_id: str | None = None,
        is_running: Callable[[str], bool],
        process_factory: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        selected = executor_id or self.configuration.default_executor
        self.configuration.launch_spec(selected)
        if is_running(selected):
            return {"executor": selected, "started": False, "status": "already_running"}

        spec = self.configuration.launch_spec(selected)
        factory = process_factory or subprocess.Popen
        try:
            process = factory(
                [spec.command, *spec.args],
                cwd=str(PROJECT_ROOT),
                **hidden_process_kwargs(),
            )
        except OSError as exc:
            raise ExternalExecutorUnavailable(
                f"external executor unavailable: {selected}: {exc}"
            ) from exc
        return {
            "executor": selected,
            "started": True,
            "status": "started",
            "pid": getattr(process, "pid", None),
        }


__all__ = [
    "DEFAULT_EXECUTOR_CONFIG_PATH",
    "ExternalExecutorConfiguration",
    "ExternalExecutorConfigurationError",
    "ExternalExecutorUnavailable",
    "ExecutorLaunchSpec",
    "ExecutorLauncher",
]
