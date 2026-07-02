from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.clean_room_empty_db import configured_db_path, health_check, install_schema


FORBIDDEN_PRODUCTION_PATHS = (
    ROOT / "data" / "creation.db",
    ROOT / "data" / "transcripts",
    ROOT / "data" / "爆款拆解",
    ROOT / "data" / "reverse",
    ROOT / "data" / "topics",
    ROOT / "data" / "drafts",
    ROOT / "data" / "language_fuel",
    ROOT / "data" / "humanize",
    ROOT / "data" / "music",
    ROOT / "data" / "raw",
    ROOT / "vault" / "爆款拆解",
    ROOT / "vault" / "语感燃料",
    ROOT / "vault" / "范例",
    ROOT / "vault" / "方法论",
)


def load_settings() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}


def run_smoke(*, init_if_missing: bool = False, require_legacy_absent: bool = False) -> dict[str, Any]:
    settings = load_settings()
    db_setting = str(settings.get("paths", {}).get("db", ""))
    if db_setting.replace("\\", "/") in {"data/creation.db", "./data/creation.db"}:
        raise RuntimeError("production paths.db still references data/creation.db")
    if settings.get("legacy_runtime", {}).get("enabled") is not False:
        raise RuntimeError("legacy_runtime.enabled must be false for production")
    if os.environ.get("CREATION_ASSISTANT_ALLOW_FIXTURES") == "1" and os.environ.get("CREATION_ASSISTANT_ENV") != "test":
        raise RuntimeError("production mode rejects fixture loading")

    db_path = configured_db_path(settings)
    if init_if_missing and not db_path.exists():
        install_schema(db_path)
    health = health_check(db_path)
    existing_forbidden = [path.relative_to(ROOT).as_posix() for path in FORBIDDEN_PRODUCTION_PATHS if path.exists()]
    if require_legacy_absent and existing_forbidden:
        raise RuntimeError(f"legacy paths still exist in production runtime: {existing_forbidden}")
    return {
        "status": "passed",
        "database": health["database"],
        "table_count": health["table_count"],
        "legacy_paths_present": existing_forbidden,
        "fixtures_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Clean-room production startup smoke check.")
    parser.add_argument("--init-if-missing", action="store_true")
    parser.add_argument("--require-legacy-absent", action="store_true")
    args = parser.parse_args(argv)
    result = run_smoke(init_if_missing=args.init_if_missing, require_legacy_absent=args.require_legacy_absent)
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
