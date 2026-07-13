"""Real tests for scripts/core/experience/tactic_registry.py. Every positive
case asserts real resolvable rows/references, not just "no exception was
raised"; every positive case has a paired reverse case.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.evidence_registry import register_hit_deep_analysis_evidence
from scripts.core.experience.tactic_registry import (
    EVIDENCE_TARGET_OBJECT_KIND,
    SKILL_RUN_TARGET_OBJECT_KIND,
    TacticEvidenceRef,
    register_tactic_candidate,
)
from scripts.core.persistence.goal01_store import IdempotencyConflict
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
        VALUES (?, 'douyin', 'fan_kepu_social_life', 'domain', 'acc', ?, 'https://x', 'cfg', 'active',
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


def _insert_analysis(conn: sqlite3.Connection, analysis_id: str, hit_id: str, *, topic: str) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, 1, 'req1', 'corr1', ?, '开头手法', '结构手法', 'model', 'run1')
        """,
        (analysis_id, hit_id, topic),
    )


def _make_evidence_refs(conn: sqlite3.Connection, count: int) -> tuple[TacticEvidenceRef, ...]:
    _insert_account(conn)
    refs = []
    for i in range(count):
        hit_id = f"h{i}"
        analysis_id = f"a{i}"
        _insert_hit(conn, hit_id)
        _insert_analysis(conn, analysis_id, hit_id, topic=f"选题手法{i}")
        result = register_hit_deep_analysis_evidence(conn, analysis_id)
        refs.append(
            TacticEvidenceRef(
                analysis_id=analysis_id,
                evidence_version_id=result["version_id"],
                evidence_content_hash=result["target_content_hash"],
            )
        )
    return tuple(refs)


SAMPLE_OUTPUT = {
    "common_patterns": ["反常识对比开头", "现象-原因-反转结构"],
    "example_candidates": ["示例一", "示例二"],
    "schema_version": "tactic_extract.output.v1",
}


class RegisterTacticCandidateTests(unittest.TestCase):
    def test_produces_resolvable_tactic_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                evidence_refs = _make_evidence_refs(conn, 2)

                result = register_tactic_candidate(
                    conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                    tactic_extract_output=SAMPLE_OUTPUT, evidence_refs=evidence_refs,
                    actor="test", correlation_id="corr-1",
                )

                self.assertFalse(result.replayed)
                tactic_row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (result.tactic_id,)).fetchone()
                self.assertEqual(tactic_row["state"], "candidate")
                self.assertEqual(tactic_row["basis_version_id"], result.version_id)

                version_row = conn.execute("SELECT * FROM trace_version WHERE version_id=?", (result.version_id,)).fetchone()
                self.assertEqual(version_row["root_id"], result.tactic_id)

                skill_ref = conn.execute(
                    "SELECT * FROM object_reference WHERE reference_id=?", (result.skill_run_reference_id,)
                ).fetchone()
                self.assertEqual(skill_ref["target_object_kind"], SKILL_RUN_TARGET_OBJECT_KIND)
                self.assertEqual(skill_ref["target_stable_id"], "req-1")

                self.assertEqual(len(result.evidence_reference_ids), 2)
                for ref_id, evidence in zip(result.evidence_reference_ids, evidence_refs):
                    row = conn.execute("SELECT * FROM object_reference WHERE reference_id=?", (ref_id,)).fetchone()
                    self.assertEqual(row["target_object_kind"], EVIDENCE_TARGET_OBJECT_KIND)
                    self.assertEqual(row["target_stable_id"], evidence.analysis_id)
                    self.assertEqual(row["target_version_id"], evidence.evidence_version_id)
            finally:
                conn.close()

    def test_same_request_id_and_output_twice_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                evidence_refs = _make_evidence_refs(conn, 2)
                first = register_tactic_candidate(
                    conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                    tactic_extract_output=SAMPLE_OUTPUT, evidence_refs=evidence_refs,
                    actor="test", correlation_id="corr-1",
                )
                second = register_tactic_candidate(
                    conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                    tactic_extract_output=SAMPLE_OUTPUT, evidence_refs=evidence_refs,
                    actor="test", correlation_id="corr-1",
                )
                self.assertFalse(first.replayed)
                self.assertTrue(second.replayed)
                self.assertEqual(first.tactic_id, second.tactic_id)
                self.assertEqual(first.version_id, second.version_id)

                roots = conn.execute("SELECT COUNT(*) FROM trace_root WHERE object_kind='tactic'").fetchone()[0]
                self.assertEqual(roots, 1)
            finally:
                conn.close()

    def test_same_request_id_with_different_output_raises_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                evidence_refs = _make_evidence_refs(conn, 2)
                register_tactic_candidate(
                    conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                    tactic_extract_output=SAMPLE_OUTPUT, evidence_refs=evidence_refs,
                    actor="test", correlation_id="corr-1",
                )
                different_output = {**SAMPLE_OUTPUT, "common_patterns": ["完全不同的归纳结果"]}
                with self.assertRaises(IdempotencyConflict):
                    register_tactic_candidate(
                        conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                        tactic_extract_output=different_output, evidence_refs=evidence_refs,
                        actor="test", correlation_id="corr-1",
                    )
                roots = conn.execute("SELECT COUNT(*) FROM trace_root WHERE object_kind='tactic'").fetchone()[0]
                self.assertEqual(roots, 1)
            finally:
                conn.close()

    def test_fewer_than_two_evidence_refs_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                evidence_refs = _make_evidence_refs(conn, 1)
                with self.assertRaises(ValueError):
                    register_tactic_candidate(
                        conn, request_id="req-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life",
                        tactic_extract_output=SAMPLE_OUTPUT, evidence_refs=evidence_refs,
                        actor="test", correlation_id="corr-1",
                    )
                roots = conn.execute("SELECT COUNT(*) FROM trace_root WHERE object_kind='tactic'").fetchone()[0]
                self.assertEqual(roots, 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
