from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.experience.evidence_registry import register_hit_deep_analysis_evidence
from scripts.core.experience.run_tactic_extract import (
    NOTE_MAX_CHARS,
    assemble_dna_note_refs,
    assemble_tactic_extract_input,
    generate_one_tactic_candidate,
    run_tactic_extract,
    select_evidence_for_tactic_batch,
    validate_run_tactic_extract_execution_contract,
)
from scripts.core.model_gateway.formal_skill_adapter import make_tactic_extract_harness
from scripts.core.persistence.install_versionref_schema_into_business_db import SCHEMA_CHAIN


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    for schema_path in SCHEMA_CHAIN:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str = "acc1", domain_label: str = "fan_kepu_social_life") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', ?, 'domain', 'acc', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, domain_label, account_id + "_sec"),
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
            hit_channel, judgment_confidence, run_id, reverse_status
        ) VALUES (?, ?, ?, 'douyin', ?, '标题', 'https://x', 1000, 200, 10, 5, 'like_anomaly', 'formal', 'run1', 'completed')
        """,
        (hit_id, video_id, account_id, hit_id + "_item"),
    )


def _insert_analysis(
    conn: sqlite3.Connection, analysis_id: str, hit_id: str, *,
    topic_pattern: str = "选题手法", hook_pattern: str = "开头手法", structure_pattern: str = "结构手法",
) -> None:
    conn.execute(
        """
        INSERT INTO hit_deep_analysis(analysis_id, hit_id, version, request_id, correlation_id,
            topic_pattern, hook_pattern, structure_pattern, model_name, run_id)
        VALUES (?, ?, 1, 'req1', 'corr1', ?, ?, ?, 'model', 'run1')
        """,
        (analysis_id, hit_id, topic_pattern, hook_pattern, structure_pattern),
    )


def _seed_registered_evidence(conn: sqlite3.Connection, count: int, *, domain_label: str = "fan_kepu_social_life") -> None:
    _insert_account(conn, domain_label=domain_label)
    for i in range(count):
        hit_id = f"h{i}"
        analysis_id = f"a{i}"
        _insert_hit(conn, hit_id)
        _insert_analysis(
            conn, analysis_id, hit_id,
            topic_pattern=f"选题手法{i}", hook_pattern=f"开头手法{i}", structure_pattern=f"结构手法{i}",
        )
        register_hit_deep_analysis_evidence(conn, analysis_id)


class ValidateExecutionContractTests(unittest.TestCase):
    def test_cites_br_experience_001(self) -> None:
        contract = validate_run_tactic_extract_execution_contract()
        self.assertIn("BR-EXPERIENCE-001", contract)


class SelectEvidenceForTacticBatchTests(unittest.TestCase):
    def test_selects_registered_evidence_for_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 3)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual({row["analysis_id"] for row in rows}, {"a0", "a1", "a2"})
            finally:
                conn.close()

    def test_excludes_other_domains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 2, domain_label="fan_kepu_social_life")
                rows = select_evidence_for_tactic_batch(conn, domain_label="music_entertainment", limit=10)
                self.assertEqual(rows, [])
            finally:
                conn.close()

    def test_excludes_evidence_already_used_by_a_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 2)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                harness = make_tactic_extract_harness()
                try:
                    generate_one_tactic_candidate(
                        conn, harness, rows, request_id="req-used", analysis_batch_id="batch-1",
                        domain_label="fan_kepu_social_life", run_id="run-1",
                    )
                finally:
                    harness.close()
                remaining = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                self.assertEqual(remaining, [])
            finally:
                conn.close()


class AssembleDnaNoteRefsTests(unittest.TestCase):
    def test_each_note_carries_all_three_real_pattern_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 1)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                notes = assemble_dna_note_refs(rows)
                self.assertEqual(len(notes), 1)
                self.assertIn("选题手法0", notes[0])
                self.assertIn("开头手法0", notes[0])
                self.assertIn("结构手法0", notes[0])
            finally:
                conn.close()

    def test_notes_respect_the_320_char_schema_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_account(conn)
                _insert_hit(conn, "h0")
                _insert_analysis(conn, "a0", "h0", topic_pattern="字" * 500, hook_pattern="字" * 500, structure_pattern="字" * 500)
                register_hit_deep_analysis_evidence(conn, "a0")
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                notes = assemble_dna_note_refs(rows)
                self.assertLessEqual(len(notes[0]), NOTE_MAX_CHARS)
            finally:
                conn.close()


class AssembleTacticExtractInputTests(unittest.TestCase):
    def test_maps_into_the_exact_public_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 2)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                payload = assemble_tactic_extract_input(
                    rows, request_id="req-1", correlation_id="corr-1", analysis_batch_id="batch-1", domain_label="fan_kepu_social_life"
                )
            finally:
                conn.close()
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["domain_label"], "fan_kepu_social_life")
        self.assertEqual(payload["schema_version"], "tactic_extract.input.v1")
        self.assertEqual(len(payload["dna_note_refs"]), 2)

    def test_unrecognized_domain_falls_back_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _seed_registered_evidence(conn, 2)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)
                payload = assemble_tactic_extract_input(
                    rows, request_id="req-1", correlation_id="corr-1", analysis_batch_id="batch-1", domain_label="some_future_domain"
                )
            finally:
                conn.close()
        self.assertEqual(payload["domain_label"], "unknown")


class GenerateOneTacticCandidateAndRunTests(unittest.TestCase):
    """Uses the real make_tactic_extract_harness() with its default
    deterministic model port -- no real network call, no real credentials."""

    def test_generate_one_tactic_candidate_persists_a_real_tactic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_tactic_extract_harness()
            try:
                _seed_registered_evidence(conn, 3)
                rows = select_evidence_for_tactic_batch(conn, domain_label="fan_kepu_social_life", limit=10)

                result = generate_one_tactic_candidate(
                    conn, harness, rows, request_id="req-1", analysis_batch_id="batch-1",
                    domain_label="fan_kepu_social_life", run_id="run-1",
                )

                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["evidence_count"], 3)
                tactic_row = conn.execute("SELECT * FROM tactic_state WHERE tactic_id=?", (result["tactic_id"],)).fetchone()
                self.assertEqual(tactic_row["state"], "candidate")
            finally:
                conn.close()
                harness.close()

    def test_run_tactic_extract_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_tactic_extract_harness()
            try:
                _seed_registered_evidence(conn, 5)
                report = run_tactic_extract(conn, limit=20, domain_label="fan_kepu_social_life", harness=harness)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["failed"], 0)
                self.assertEqual(report["batch_size"], 5)
            finally:
                conn.close()
                harness.close()

    def test_run_tactic_extract_with_too_few_evidence_rows_reports_zero_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_tactic_extract_harness()
            try:
                _seed_registered_evidence(conn, 1)
                report = run_tactic_extract(conn, limit=20, domain_label="fan_kepu_social_life", harness=harness)
                self.assertEqual(report["attempted"], 0)
                self.assertIn("only 1 eligible", report["note"])
            finally:
                conn.close()
                harness.close()

    def test_run_tactic_extract_caps_batch_at_20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            harness = make_tactic_extract_harness()
            try:
                _seed_registered_evidence(conn, 25)
                report = run_tactic_extract(conn, limit=25, domain_label="fan_kepu_social_life", harness=harness)
                self.assertEqual(report["batch_size"], 20)
            finally:
                conn.close()
                harness.close()


if __name__ == "__main__":
    unittest.main()
