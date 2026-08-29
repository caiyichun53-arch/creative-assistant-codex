from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.core.production.stage0_content_core import (
    DataIdentityError,
    Stage0ContentProductionCore,
)
from scripts.core.runtime.runtime_storage import (
    RuntimeStorageError,
    database_path_for_identity,
    formal_database_path,
    runtime_path,
    runtime_root_for_identity,
)
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor
from scripts.core.scheduler.schedule_registry import ScheduleDisabledError, ScheduleRegistry
from scripts.mcp.creation_assistant_mcp_server import CreationAssistantMcpApplication


FORMAL_DATABASE = formal_database_path().resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RuntimeIdentityIsolationTests(unittest.TestCase):
    def test_formal_and_test_resolve_to_disjoint_roots_and_no_identity_is_rejected(self) -> None:
        formal_root = runtime_root_for_identity("production")
        test_root = runtime_root_for_identity("test")
        self.assertNotEqual(formal_root, test_root)
        self.assertNotEqual(
            database_path_for_identity("production"),
            database_path_for_identity("test"),
        )
        with self.assertRaises(RuntimeStorageError):
            runtime_path("agent_platform", "unscoped.log")

    def test_missing_test_database_is_created_in_test_world_without_formal_fallback(self) -> None:
        with TemporaryDirectory() as tempdir:
            test_database = Path(tempdir) / "new-test.sqlite3"
            self.assertFalse(test_database.exists())
            before = _sha256(FORMAL_DATABASE)
            opened: list[str] = []
            real_connect = sqlite3.connect

            def recording_connect(database, *args, **kwargs):
                opened.append(str(database))
                return real_connect(database, *args, **kwargs)

            with patch("sqlite3.connect", side_effect=recording_connect):
                core = Stage0ContentProductionCore.open(
                    test_database, data_identity="test"
                )
                core.close()

            self.assertTrue(test_database.is_file())
            self.assertNotIn(str(FORMAL_DATABASE), opened)
            self.assertEqual(_sha256(FORMAL_DATABASE), before)

    def test_test_identity_rejects_every_database_inside_formal_runtime(self) -> None:
        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(
                FORMAL_DATABASE, data_identity="test"
            )
        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(
                FORMAL_DATABASE.with_name("other-formal-state.sqlite3"),
                data_identity="test",
            )

    def test_production_identity_rejects_the_test_database(self) -> None:
        with self.assertRaises(DataIdentityError):
            Stage0ContentProductionCore.open(
                database_path_for_identity("test"), data_identity="production"
            )

    def test_test_mcp_reports_test_identity_and_never_exposes_formal_database(self) -> None:
        with TemporaryDirectory() as tempdir:
            test_database = Path(tempdir) / "mcp-test.sqlite3"
            core = Stage0ContentProductionCore.open(
                test_database, data_identity="test"
            )
            app = CreationAssistantMcpApplication(core)
            try:
                status = app.status()
            finally:
                app.close()
            self.assertEqual(status["data_identity"], "test")
            self.assertEqual(Path(status["database_path"]).resolve(), test_database.resolve())
            self.assertNotEqual(Path(status["database_path"]).resolve(), FORMAL_DATABASE)
            self.assertEqual(status["business_state_owner"], "Creation Assistant Core")
            self.assertIsNone(status["mcp_database"])

    def test_scheduler_run_now_is_test_only_during_migration(self) -> None:
        registry = ScheduleRegistry.load()
        observed: list[str] = []
        result = registry.run_now(
            key="daily",
            data_identity="test",
            runner=lambda schedule: observed.append(schedule.key) or "test-only",
        )
        self.assertEqual(result, "test-only")
        self.assertEqual(observed, ["daily"])
        with self.assertRaises(ScheduleDisabledError):
            registry.run_now(
                key="daily",
                data_identity="production",
                runner=lambda _schedule: "must-not-run",
            )

    def test_test_media_archive_resolves_under_test_runtime(self) -> None:
        archive_root = LocalMediaCrawlerExecutor(data_identity="test")._archive_root()
        self.assertEqual(archive_root.parent, runtime_root_for_identity("test"))
        self.assertNotEqual(archive_root, runtime_root_for_identity("production"))


if __name__ == "__main__":
    unittest.main()
