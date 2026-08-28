"""Real saved breakdown output through the formal qualification path."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore


class RealSampleQualification2CTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        now = "2026-08-19T00:00:00+08:00"
        self.connection.execute(
            "INSERT INTO stage0_content_account VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("account_real_sample", "competitor", "real-sample", "music_entertainment", "real-sample", "active", "test", "test", now),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cold_real_sample", "account_real_sample", "music_entertainment", "completed", "test", "test", now, None),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("registration_real_sample", "cold_real_sample", "account_real_sample", "completed", "completed", "test", now, now),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'transcripts_and_comments', ?, 'completed', ?, '{}', 1, 'test', ?)",
            (
                "registration_real_sample",
                "7627790761731378467",
                json.dumps({
                    "source_url": "https://example.test/real-sample",
                    "transcript_ref": "real-sample-transcript",
                    "comment_collection_ref": "real-sample-comments",
                }),
                now,
            ),
        )
        self.connection.execute(
            "INSERT INTO stage0_competitor_registration_item "
            "(registration_id, step_name, item_ref, status, artifact_json, error_json, attempt_count, data_identity, updated_at) "
            "VALUES (?, 'breakdown', ?, 'completed', ?, '{}', 1, 'test', ?)",
            (
                "registration_real_sample",
                "7627790761731378467",
                json.dumps({"source_content_type": "person_music_story"}),
                now,
            ),
        )
        self.parent = {
            "source_type": "competitor_breakdown",
            "source_object_id": "7627790761731378467",
            "registration_id": "registration_real_sample",
        }

    def tearDown(self) -> None:
        self.core.close()

    def test_saved_real_breakdown_enters_formal_qualification(self) -> None:
        root = Path(__file__).resolve().parents[1]
        artifact = json.loads(
            (root / "validation/competitor_breakdown_batch_20260818/3b_7627790761731378467.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(artifact["status"], "completed")
        self.assertFalse(artifact["formal_business_data_written"])

        breakdown = {
            "source_id": artifact["source_id"],
            "source_content_type": artifact["source_content_type"],
            "source_content_type_id": artifact["source_content_type_id"],
            "analysis_text": artifact["analysis_text"],
            "expansion_signals": artifact["expansion_signals"],
            "typed_expansion_leads": [artifact["typed_expansion_leads"][0]],
            "question_expansions": artifact.get("question_expansions", []),
        }

        result = self.core.register_breakdown_question_expansions(
            domain_label="music_entertainment",
            breakdown=breakdown,
            parent_source_ref=self.parent,
            content_type_lifecycle="classify",
            external_probe=lambda question: {
                "status": "passed",
                "material_refs": [
                    {"kind": "external_public_source", "ref": "real-sample-check-1"},
                    {"kind": "external_public_source", "ref": "real-sample-check-2"},
                ],
                "checks": {"decision": "continue"},
            },
        )

        self.assertEqual(result["count"], 1)
        qualification = self.connection.execute(
            "SELECT status, checks_json FROM stage1_question_expansion_qualification"
        ).fetchone()
        self.assertEqual(qualification[0], "qualified")
        self.assertIn("approved_type_projection", qualification[1])
        source_count = self.connection.execute(
            "SELECT COUNT(*) FROM stage1_question_expansion_source"
        ).fetchone()[0]
        self.assertEqual(source_count, 1)


if __name__ == "__main__":
    unittest.main()
