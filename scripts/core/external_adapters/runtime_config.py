from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ENV_PATH = ROOT / ".env.runtime.local"
MAIN_ENV_PATH = ROOT / ".env"


class ExternalRuntimeConfigurationError(RuntimeError):
    pass


def _dotenv_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def external_runtime_value(key: str) -> str:
    """Resolve one explicit machine binding without silently choosing conflicts."""
    sources = {
        "process": str(os.environ.get(key) or "").strip(),
        "runtime_local": _dotenv_value(RUNTIME_ENV_PATH, key),
        "project_env": _dotenv_value(MAIN_ENV_PATH, key),
    }
    values = {value for value in sources.values() if value}
    if len(values) > 1:
        locations = ", ".join(name for name, value in sources.items() if value)
        raise ExternalRuntimeConfigurationError(f"conflicting explicit runtime values for {key}: {locations}")
    if not values:
        raise ExternalRuntimeConfigurationError(f"external runtime value is unresolved: {key}")
    return next(iter(values))
