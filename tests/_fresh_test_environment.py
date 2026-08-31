"""Fresh, isolated business environment for full TEST runs.

The production system deliberately stores the frozen registries in the domain
pack itself.  A TEST database alone is therefore not enough to make a new
business run independent.  This helper copies the static domain definition
into a temporary registry and gives the run its own temporary database and
runtime root.  It never edits the checked-in domain pack or the formal runtime.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    get_content_type_registry,
    set_domain_pack_config_dir,
)
from scripts.core.business_data.domain_boundaries import get_production_boundary_registry
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class FreshTestEnvironment:
    """One disposable TEST business environment with no prior run state."""

    def __init__(self, *, domain_label: str = "music_entertainment") -> None:
        self.domain_label = domain_label
        self._temporary = TemporaryDirectory(prefix="creation-assistant-e2e-")
        self.root = Path(self._temporary.name)
        self.database = self.root / "test" / "test_activation.sqlite3"
        self.runtime_root = self.root / "runtime"
        self.config_dir = self.root / "domain_packs"
        self.core: Stage0ContentProductionCore | None = None
        self._runtime_patch = None
        self._previous_config_dir = DOMAIN_CONFIG_DIR
        self._copy_static_domain_pack()

    def _copy_static_domain_pack(self) -> None:
        source = DOMAIN_CONFIG_DIR / f"{self.domain_label}.yaml"
        payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError("the source domain pack must be a mapping")

        # These are formal business results, not static domain definitions.
        # They must be absent at the start of every fresh TEST run.
        payload.pop("content_type_registry", None)
        payload.pop("production_boundary_registry", None)
        discovery = payload.get("discovery")
        if isinstance(discovery, dict):
            topic_search = discovery.get("topic_search")
            if isinstance(topic_search, dict):
                topic_search["active_tags"] = []

        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / source.name).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )

    def __enter__(self) -> "FreshTestEnvironment":
        set_domain_pack_config_dir(self.config_dir)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._runtime_patch = patch(
            "scripts.core.runtime.runtime_storage._test_runtime_root",
            return_value=self.runtime_root,
        )
        self._runtime_patch.start()
        self.core = Stage0ContentProductionCore.open(
            self.database,
            data_identity="test",
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.core is not None:
            self.core.close()
            self.core = None
        if self._runtime_patch is not None:
            self._runtime_patch.stop()
            self._runtime_patch = None
        set_domain_pack_config_dir(self._previous_config_dir)
        self._temporary.cleanup()

    def assert_empty_formal_registries(self) -> None:
        """Verify that no prior freeze crossed into this environment."""

        assert get_content_type_registry(self.domain_label)["status"] == "NOT_FROZEN"
        assert get_production_boundary_registry(self.domain_label)["status"] == "NOT_FROZEN"
