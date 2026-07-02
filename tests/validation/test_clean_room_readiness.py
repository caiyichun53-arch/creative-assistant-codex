from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.validation.clean_room_readiness import (
    ALLOWED_DISPOSITIONS,
    run_audit,
    validate_acceptance,
    validate_fixture_manifest,
    validate_formal_storage_config,
    validate_inventory,
    validate_legacy_quarantine,
)
from scripts.validation.clean_room_empty_db import health_check, install_schema
from scripts.validation.fixture_loader import destroy_fixture_db, load_synthetic_fixtures
from scripts.validation.production_startup_smoke import run_smoke


ROOT = Path(__file__).resolve().parents[2]


class CleanRoomReadinessTests(unittest.TestCase):
    def test_manifests_are_structurally_valid(self) -> None:
        errors = []
        errors.extend(validate_inventory(ROOT))
        errors.extend(validate_fixture_manifest(ROOT))
        errors.extend(validate_acceptance(ROOT))
        errors.extend(validate_formal_storage_config(ROOT))
        errors.extend(validate_legacy_quarantine(ROOT))
        self.assertEqual(errors, [])

    def test_inventory_dispositions_are_closed_enum(self) -> None:
        import yaml

        inventory = yaml.safe_load((ROOT / "LEGACY_DATA_INVENTORY.yaml").read_text(encoding="utf-8"))
        dispositions = {item["disposition"] for item in inventory["items"]}
        self.assertLessEqual(dispositions, ALLOWED_DISPOSITIONS)
        self.assertIn("discard_from_target", dispositions)
        self.assertIn("fixture_only", dispositions)
        self.assertIn("cold_archive_only", dispositions)
        self.assertIn("retain_as_current_asset", dispositions)

    def test_current_repository_is_phase_two_ready(self) -> None:
        result = run_audit(ROOT)
        self.assertTrue(result["phase_2_safe_to_execute"])
        statuses = {item["check_id"]: item["status"] for item in result["checks"]}
        self.assertTrue(all(status == "passed" for status in statuses.values()))

    def test_audit_does_not_mutate_legacy_data(self) -> None:
        db = ROOT / "data" / "creation.db"
        before = db.stat().st_mtime_ns if db.exists() else None
        run_audit(ROOT)
        after = db.stat().st_mtime_ns if db.exists() else None
        self.assertEqual(after, before)

    def test_empty_formal_db_has_zero_rows(self) -> None:
        db = ROOT / "validation_evidence" / "clean_room" / "test_empty.sqlite3"
        result = install_schema(db, destroy_existing=True)
        self.assertGreater(result["table_count"], 0)
        self.assertTrue(all(count == 0 for count in result["table_rows"].values()))
        health = health_check(db)
        self.assertEqual(health["foreign_key_check"], "passed")

    def test_fixture_loader_rejects_production_mode(self) -> None:
        db = ROOT / "tests" / "fixtures" / "clean_room" / "reject.sqlite3"
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                load_synthetic_fixtures(db, allow_flag=True, destroy_existing=True)

    def test_fixture_loader_is_test_only_and_destroyable(self) -> None:
        db = ROOT / "tests" / "fixtures" / "clean_room" / "fixture_runtime.sqlite3"
        env = {"CREATION_ASSISTANT_ENV": "test", "CREATION_ASSISTANT_ALLOW_FIXTURES": "1"}
        with patch.dict("os.environ", env, clear=True):
            result = load_synthetic_fixtures(db, allow_flag=True, destroy_existing=True)
            self.assertGreaterEqual(result["fixture_case_count"], 17)
            destroyed = destroy_fixture_db(db, allow_flag=True)
            self.assertTrue(destroyed["destroyed"])

    def test_production_smoke_rejects_fixture_env(self) -> None:
        env = {"CREATION_ASSISTANT_ALLOW_FIXTURES": "1"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(RuntimeError):
                run_smoke(init_if_missing=True)

    def test_production_smoke_uses_clean_room_db(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = run_smoke(init_if_missing=True)
        self.assertEqual(result["status"], "passed")
        self.assertIn("clean_room_v0_6_2.sqlite3", result["database"])


if __name__ == "__main__":
    unittest.main()
