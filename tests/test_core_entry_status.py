from __future__ import annotations

from contextlib import redirect_stdout
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

from scripts.core.core_entry import CoreReadOnlySession, CoreStartupError, main
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class CoreEntryStatusTests(unittest.TestCase):
    def test_status_treats_identity_digest_as_historical_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "status.sqlite3"
            core = Stage0ContentProductionCore.open(
                database_path,
                data_identity="test",
            )
            session = CoreReadOnlySession(
                connection=core.conn,
                runtime_root=Path(tempdir),
                database_path=database_path,
                identity_receipt={
                    "formal_database_sha256": "historical-baseline-digest",
                },
            )
            try:
                status = session.basic_status()
            finally:
                session.close()

        self.assertTrue(status["database_open"])
        self.assertTrue(status["runtime_identity_verified"])
        self.assertEqual(
            status["historical_migration_digest"],
            "historical-baseline-digest",
        )
        self.assertTrue(status["historical_migration_digest_recorded"])
        self.assertNotIn(
            "historical_migration_digest_matches_live_database",
            status,
        )
        self.assertIn("historical copy-verification baseline", status["historical_migration_digest_note"])
        self.assertFalse(status["automatic_business_execution"])

    def test_real_core_startup_failure_is_still_reported(self) -> None:
        output = StringIO()
        with patch(
            "scripts.core.core_entry.CoreReadOnlySession.open_formal",
            side_effect=CoreStartupError("formal database could not be opened read-only"),
        ), redirect_stdout(output):
            result = main([])

        self.assertEqual(result, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["database_open"])
        self.assertEqual(
            payload["error"],
            "formal database could not be opened read-only",
        )


if __name__ == "__main__":
    unittest.main()
