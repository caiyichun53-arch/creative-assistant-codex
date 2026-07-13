"""Real tests for scripts/core/experience/evidence_registry.py -- production
activation阶段2. Every positive case asserts an actual stored value (a hash
recomputed independently, a real foreign-key chain followed), not just "no
exception was raised"; every positive case is paired with a reverse case
proving the same code path rejects bad input instead of silently accepting it.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.evidence_registry import (
    EVIDENCE_PROJECTION_VERSION,
    EVIDENCE_ROOT_OBJECT_KIND,
    EVIDENCE_TARGET_OBJECT_KIND,
    EvidenceRegistrationError,
    register_hit_deep_analysis_evidence,
)
from scripts.core.persistence.goal01_store import content_hash
from scripts.core.persistence.install_versionref_schema_into_business_db import SCHEMA_CHAIN


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    for schema_path in SCHEMA_CHAIN:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str = "acc1") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', 'fan_kepu_social_life', 'domain', '半佛仙人', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, account_id + "_sec"),
    )


def _insert_video(conn: sqlite3.Connection, video_id: str, account_id: str) -> None:
    conn.execute(
        """
        INSERT INTO competitor_videos(video_id, account_id, platform, platform_item_id, title, url, raw_json)
        VALUES (?, ?, 'douyin', ?, 't', 'https://x', '{}')
        """,
        (video_id, account_id, video_id + "_item"),
    )


def _insert_hit(conn: sqlite3.Connection, hit_id: str, *, account_id: str = "acc1") -> None:
    video_id = hit_id + "_vid"
    _insert_video(conn, video_id, account_id)
    conn.execute(
        """
        INSERT INTO hits(
            hit_id, video_id, account_id, platform, platform_item_id, title, url,
            like_count, comment_count, share_count, collect_count,
            hit_channel, judgment_confidence, run_id, preparation_status
        ) VALUES (?, ?, ?, 'douyin', ?, '标题', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item"),
    )


def _insert_analysis(
    conn: sqlite3.Connection,
    analysis_id: str,
    hit_id: str,
    *,
    version: int = 1,
    topic_pattern: str = "选题手法示例",
    hook_pattern: str = "开头手法示例",
    structure_pattern: str = "结构手法示例",
) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, ?, 'req1', 'corr1', ?, ?, ?, 'model', 'run1')
        """,
        (analysis_id, hit_id, version, topic_pattern, hook_pattern, structure_pattern),
    )


class RegisterHitDeepAnalysisEvidenceTests(unittest.TestCase):
    def test_registers_real_row_with_independently_recomputed_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(
                    conn, "a1", "h1",
                    topic_pattern="行业反差/悬念留白",
                    hook_pattern="价格反差+情绪代入",
                    structure_pattern="现象-原因-反转",
                )

                result = register_hit_deep_analysis_evidence(conn, "a1")

                expected_hash = content_hash(
                    {
                        "topic_pattern": "行业反差/悬念留白",
                        "hook_pattern": "价格反差+情绪代入",
                        "structure_pattern": "现象-原因-反转",
                    },
                    EVIDENCE_PROJECTION_VERSION,
                )
                self.assertEqual(result["target_content_hash"], expected_hash)
                self.assertFalse(result["replayed"])

                version_row = conn.execute(
                    "SELECT * FROM trace_version WHERE version_id=?", (result["version_id"],)
                ).fetchone()
                self.assertEqual(version_row["content_hash"], expected_hash)
                self.assertEqual(version_row["root_id"], result["root_id"])

                root_row = conn.execute(
                    "SELECT * FROM trace_root WHERE root_id=?", (result["root_id"],)
                ).fetchone()
                self.assertEqual(root_row["object_kind"], EVIDENCE_ROOT_OBJECT_KIND)
                self.assertEqual(root_row["current_version_id"], result["version_id"])

                reference_row = conn.execute(
                    "SELECT * FROM object_reference WHERE reference_id=?", (result["reference_id"],)
                ).fetchone()
                self.assertEqual(reference_row["target_object_kind"], EVIDENCE_TARGET_OBJECT_KIND)
                self.assertEqual(reference_row["target_stable_id"], "a1")
                self.assertEqual(reference_row["target_content_hash"], expected_hash)
                self.assertEqual(reference_row["source_version_id"], result["version_id"])
            finally:
                conn.close()

    def test_registering_the_same_analysis_twice_creates_exactly_one_trace_version_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")

                first = register_hit_deep_analysis_evidence(conn, "a1")
                second = register_hit_deep_analysis_evidence(conn, "a1")

                self.assertFalse(first["replayed"])
                self.assertTrue(second["replayed"])
                self.assertEqual(first["version_id"], second["version_id"])
                self.assertEqual(first["root_id"], second["root_id"])
                self.assertEqual(first["reference_id"], second["reference_id"])

                version_rows = conn.execute(
                    "SELECT * FROM trace_version WHERE root_id=?", (first["root_id"],)
                ).fetchall()
                self.assertEqual(len(version_rows), 1)

                reference_rows = conn.execute(
                    "SELECT * FROM object_reference WHERE target_stable_id='a1'"
                ).fetchall()
                self.assertEqual(len(reference_rows), 1)
            finally:
                conn.close()

    def test_two_different_analyses_get_two_independent_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_hit(conn, "h2")
                _insert_analysis(conn, "a1", "h1", topic_pattern="选题1")
                _insert_analysis(conn, "a2", "h2", topic_pattern="选题2")

                result1 = register_hit_deep_analysis_evidence(conn, "a1")
                result2 = register_hit_deep_analysis_evidence(conn, "a2")

                self.assertNotEqual(result1["root_id"], result2["root_id"])
                self.assertNotEqual(result1["version_id"], result2["version_id"])
                self.assertNotEqual(result1["target_content_hash"], result2["target_content_hash"])
            finally:
                conn.close()

    def test_unknown_analysis_id_raises_and_leaves_no_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h1")
                _insert_analysis(conn, "a1", "h1")

                root_count_before = conn.execute("SELECT COUNT(*) FROM trace_root").fetchone()[0]
                version_count_before = conn.execute("SELECT COUNT(*) FROM trace_version").fetchone()[0]
                reference_count_before = conn.execute("SELECT COUNT(*) FROM object_reference").fetchone()[0]

                with self.assertRaises(EvidenceRegistrationError):
                    register_hit_deep_analysis_evidence(conn, "not_a_real_id")

                root_count_after = conn.execute("SELECT COUNT(*) FROM trace_root").fetchone()[0]
                version_count_after = conn.execute("SELECT COUNT(*) FROM trace_version").fetchone()[0]
                reference_count_after = conn.execute("SELECT COUNT(*) FROM object_reference").fetchone()[0]

                self.assertEqual(root_count_before, root_count_after)
                self.assertEqual(version_count_before, version_count_after)
                self.assertEqual(reference_count_before, reference_count_after)
            finally:
                conn.close()

    def test_empty_database_with_no_analysis_rows_also_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                with self.assertRaises(EvidenceRegistrationError):
                    register_hit_deep_analysis_evidence(conn, "anything")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
