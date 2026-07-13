from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.business_data.register_competitor_accounts import install_schema
from scripts.core.business_data.run_reverse_prep import (
    LOCAL_ASR_SCRIPT,
    clean_transcript,
    detect_quality_flags,
    fetch_detail_with_comments,
    filter_comments,
    prep_one_hit,
    run_reverse_prep,
    select_pending_hits,
    validate_reverse_prep_execution_contract,
)
from scripts.core.external_adapters import ExternalAdapterCommand, ExternalCommandResult
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor

# This whole file is the test backing required for the exact_match claims this
# session added to historical traceability material for ASR-related behavior;
# BR-COLLECT-005 and BR-COLLECT-006 -- see tests/validation/test_business_rule_
# test_coverage.py, which fails if an exact_match claim has no test referencing
# its id anywhere in tests/.

REVERSE_CFG = {
    "models_root": "models",
    "asr_model": "sensevoice/sensevoice-small",
    "vad_model": "modelscope_cache/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "ffmpeg_path": "ffmpeg",
    "local_asr_python": "python",
    "min_transcript_chars": 10,
    "top_comments": 60,
    "min_comment_len": 5,
}


def _connect(tmp: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(tmp) / "test.sqlite3")
    conn.row_factory = sqlite3.Row
    install_schema(conn)
    return conn


def _insert_account(conn: sqlite3.Connection, account_id: str = "acc1") -> None:
    conn.execute(
        """
        INSERT INTO competitor_accounts(
            account_id, platform, domain_label, domain_name, account_name, sec_uid,
            homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy
        )
        VALUES (?, 'douyin', 'domain', 'domain', 'account', ?, 'https://x', 'cfg', 'active',
                'stock_snapshot_archived', 'reverse_prep_only_for_promoted_hits')
        """,
        (account_id, account_id + "_sec"),
    )


def _insert_video(
    conn: sqlite3.Connection, video_id: str, account_id: str, *,
    baseline_mode: str | None = None, first_contact_category: str | None = "historical_mature",
) -> None:
    conn.execute(
        """
        INSERT INTO competitor_videos(
            video_id, account_id, platform, platform_item_id, title, url, raw_json,
            baseline_mode, first_contact_category
        ) VALUES (?, ?, 'douyin', ?, 't', 'https://x', '{}', ?, ?)
        """,
        (video_id, account_id, video_id + "_item", baseline_mode, first_contact_category),
    )


def _insert_hit(
    conn: sqlite3.Connection, hit_id: str, *, url: str = "https://www.douyin.com/video/123",
    preparation_status: str = "pending", promoted_at: str | None = None,
    judgment_confidence: str = "formal", baseline_mode: str | None = None,
    first_contact_category: str | None = "historical_mature",
) -> None:
    account_id = hit_id + "_acc"
    video_id = hit_id + "_vid"
    _insert_account(conn, account_id)
    _insert_video(conn, video_id, account_id, baseline_mode=baseline_mode, first_contact_category=first_contact_category)
    columns = "hit_id, video_id, account_id, platform, platform_item_id, title, url, hit_channel, judgment_confidence, run_id, preparation_status"
    values = [hit_id, video_id, account_id, "douyin", hit_id + "_hititem", "t", url, "like_anomaly", judgment_confidence, "run1", preparation_status]
    if promoted_at is not None:
        columns += ", promoted_at"
        values.append(promoted_at)
    placeholders = ", ".join(["?"] * len(values))
    conn.execute(f"INSERT INTO hits({columns}) VALUES ({placeholders})", values)
    conn.commit()


def _write_comments_jsonl(run_dir: Path, comments: list[dict]) -> None:
    jsonl_dir = run_dir / "raw" / "douyin" / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    (jsonl_dir / "1_comments_2026.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in comments), encoding="utf-8"
    )


class _FakeExecutor:
    def __init__(self, result: ExternalCommandResult):
        self.result = result
        self.calls: list[ExternalAdapterCommand] = []

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        self.calls.append(command)
        return self.result


def _fake_detail_result(tmp: str, *, download_url: str = "https://fake.cdn/audio.mp3", comments: list[dict] | None = None) -> ExternalCommandResult:
    run_dir = Path(tmp) / "mc_run"
    _write_comments_jsonl(run_dir, comments or [])
    return ExternalCommandResult(
        status="succeeded",
        payload={"items": [{"music_download_url": download_url}]},
        raw_archive_ref=str(run_dir),
        external_side_effect=True,
    )


def _fake_response() -> object:
    return type("R", (), {"raise_for_status": lambda self: None, "content": b"fake-audio-bytes"})()


def _fake_subprocess_for_prep(transcript_text: str):
    def fake_run(args, **kwargs):
        if str(LOCAL_ASR_SCRIPT) in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout=transcript_text, stderr="")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    return fake_run


class ValidateReversePrepContractTests(unittest.TestCase):
    def test_valid_config_cites_effective_baseline_audio_collection_sections(self) -> None:
        contract = validate_reverse_prep_execution_contract(REVERSE_CFG)
        self.assertTrue({"6", "16"}.issubset(contract))

    def test_missing_asr_model_rejected(self) -> None:
        bad = dict(REVERSE_CFG, asr_model="")
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(bad)

    def test_wrong_top_comments_rejected(self) -> None:
        bad = dict(REVERSE_CFG, top_comments=100)
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(bad)

    def test_wrong_min_comment_len_rejected(self) -> None:
        bad = dict(REVERSE_CFG, min_comment_len=1)
        with self.assertRaises(ValueError):
            validate_reverse_prep_execution_contract(bad)


class SelectPendingHitsTests(unittest.TestCase):
    def test_only_pending_status_returned_respecting_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1", preparation_status="pending")
                _insert_hit(conn, "h2", preparation_status="completed")
                _insert_hit(conn, "h3", preparation_status="pending")
                pending = select_pending_hits(conn, limit=10)
                self.assertEqual({row["hit_id"] for row in pending}, {"h1", "h3"})

                limited = select_pending_hits(conn, limit=1)
                self.assertEqual(len(limited), 1)
            finally:
                conn.close()

    def test_priority_tiers_order_formal_d_series_before_formal_new_before_rough_history_before_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                # Inserted in an order that would be wrong if tiers were ignored.
                _insert_hit(conn, "tier5", promoted_at="2026-01-01T00:00:00", baseline_mode=None, first_contact_category="transition")
                _insert_hit(conn, "tier4", promoted_at="2026-01-02T00:00:00", judgment_confidence="rough", baseline_mode="mature_history", first_contact_category="historical_mature")
                _insert_hit(conn, "tier3", promoted_at="2026-01-03T00:00:00", baseline_mode=None, first_contact_category="formal_new")
                _insert_hit(conn, "tier2", promoted_at="2026-01-04T00:00:00", baseline_mode="formal_d_series", first_contact_category="formal_new")
                ranked = select_pending_hits(conn, limit=10)
                self.assertEqual([row["hit_id"] for row in ranked], ["tier2", "tier3", "tier4", "tier5"])
            finally:
                conn.close()


class CleanTranscriptTests(unittest.TestCase):
    def test_removes_consecutive_duplicate_sentences(self) -> None:
        raw = "今天天气很好。今天天气很好。我们出去玩吧。"
        cleaned = clean_transcript(raw)
        self.assertEqual(cleaned, "今天天气很好。我们出去玩吧")

    def test_collapses_whitespace_when_no_duplicates(self) -> None:
        raw = "这是   一段\n\n正常的文字"
        cleaned = clean_transcript(raw)
        self.assertEqual(cleaned, "这是 一段 正常的文字")


class DetectQualityFlagsTests(unittest.TestCase):
    def test_empty_text_flagged(self) -> None:
        self.assertEqual(detect_quality_flags("", min_transcript_chars=10), ["empty"])

    def test_too_short_flagged(self) -> None:
        self.assertEqual(detect_quality_flags("short", min_transcript_chars=10), ["too_short"])

    def test_high_repetition_flagged(self) -> None:
        raw = "。".join(["卡住了卡住了"] * 5 + ["这是正常的一句话"])
        flags = detect_quality_flags(raw, min_transcript_chars=1)
        self.assertIn("high_repetition", flags)

    def test_normal_text_has_no_flags(self) -> None:
        raw = "第一句话说了一件事。第二句话说了另一件事。第三句话总结一下。"
        self.assertEqual(detect_quality_flags(raw, min_transcript_chars=1), [])


class FilterCommentsTests(unittest.TestCase):
    def test_min_length_filter_and_top_cap(self) -> None:
        raw = [{"comment_id": str(i), "text": "x" * (i + 1), "like_count": i} for i in range(10)]
        kept = filter_comments(raw, min_comment_len=5, top_comments=3)
        self.assertEqual(len(kept), 3)
        self.assertTrue(all(len(c["text"]) >= 5 for c in kept))

    def test_sample_rank_preserves_original_position(self) -> None:
        raw = [
            {"comment_id": "a", "text": "太短"},  # filtered out (len < 5)
            {"comment_id": "b", "text": "这条评论足够长"},
            {"comment_id": "c", "text": "这条也足够长了"},
        ]
        kept = filter_comments(raw, min_comment_len=5, top_comments=60)
        self.assertEqual([c["sample_rank"] for c in kept], [1, 2])

    def test_missing_comment_id_falls_back_to_content_hash(self) -> None:
        raw = [{"text": "这是一条没有id的评论"}]
        kept = filter_comments(raw, min_comment_len=5, top_comments=60)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["comment_id"])

    def test_duplicate_comment_id_deduped(self) -> None:
        raw = [
            {"comment_id": "c1", "text": "重复评论一二三"},
            {"comment_id": "c1", "text": "重复评论一二三"},
        ]
        kept = filter_comments(raw, min_comment_len=5, top_comments=60)
        self.assertEqual(len(kept), 1)


class FetchDetailWithCommentsTests(unittest.TestCase):
    def test_reads_both_video_item_and_comments_from_same_crawl(self) -> None:
        def fake_run(args, **kwargs):
            save_path = Path(args[args.index("--save_data_path") + 1])
            jsonl_dir = save_path / "douyin" / "jsonl"
            jsonl_dir.mkdir(parents=True)
            (jsonl_dir / "1_contents_2026.jsonl").write_text(
                json.dumps({"aweme_id": "v1", "music_download_url": "https://fake.cdn/a.mp3"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (jsonl_dir / "1_comments_2026.jsonl").write_text(
                json.dumps({"comment_id": "c1", "text": "很棒的视频"}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertIn("yes", args)  # --get_comment yes was passed on the SAME detail call
            self.assertIn("--get_sub_comment", args)
            self.assertEqual(args[args.index("--get_sub_comment") + 1], "no")
            return subprocess.CompletedProcess(args, returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            executor = LocalMediaCrawlerExecutor(archive_root=Path(tmp), python_executable=Path("fake-python"))
            with patch("scripts.core.external_adapters.local_mediacrawler_executor.subprocess.run", side_effect=fake_run):
                detail = fetch_detail_with_comments(executor, platform="douyin", video_url="https://www.douyin.com/video/1")

        self.assertEqual(detail["item"]["music_download_url"], "https://fake.cdn/a.mp3")
        self.assertEqual(len(detail["comments"]), 1)
        self.assertEqual(detail["comments"][0]["comment_id"], "c1")


class PrepOneHitTests(unittest.TestCase):
    def test_success_writes_versioned_transcript_with_provenance_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(_fake_detail_result(tmp, comments=[{"comment_id": "c1", "text": "很不错的内容"}]))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    result = prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                self.assertEqual(result["status"], "completed")
                transcript = conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1'").fetchone()
                self.assertEqual(transcript["version"], 1)
                self.assertEqual(transcript["processing_status"], "completed")
                self.assertEqual(transcript["quality_flags"], "")
                self.assertEqual(transcript["asr_model"], REVERSE_CFG["asr_model"])
                self.assertEqual(transcript["vad_model"], REVERSE_CFG["vad_model"])
                self.assertTrue(transcript["audio_sha256"])
                self.assertEqual(transcript["raw_transcript_text"], "这是一段足够长的转写文本用于测试")

                comments = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchall()
                self.assertEqual(len(comments), 1)
                self.assertEqual(comments[0]["sample_rank"], 0)
                hit = conn.execute("SELECT preparation_status FROM hits WHERE hit_id='h1'").fetchone()
                self.assertEqual(hit["preparation_status"], "completed")
            finally:
                conn.close()

    def test_comment_batch_is_tagged_with_purpose_and_sampling_strategy(self) -> None:
        # BR-HIT-007: this worker's single fetch-per-promoted-hit collection
        # is the "D7/P+7d 才首次正式触发" case, so it must record
        # purpose='mature_analysis', not leave the column to a database
        # default (there is none -- NOT NULL with no default, deliberately).
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(_fake_detail_result(tmp, comments=[{"comment_id": "c1", "text": "很不错的内容"}]))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                comment = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchone()
                self.assertEqual(comment["purpose"], "mature_analysis")
                self.assertEqual(comment["sampling_strategy"], "top_n_by_platform_popularity")
            finally:
                conn.close()

    def test_observation_point_comes_from_the_video_first_trigger_when_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                conn.execute(
                    "UPDATE competitor_videos SET first_trigger_observation='D3' WHERE video_id='h1_vid'"
                )
                executor = _FakeExecutor(_fake_detail_result(tmp, comments=[{"comment_id": "c1", "text": "很不错的内容"}]))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    prep_one_hit(
                        conn, executor,
                        conn.execute(
                            """
                            SELECT hits.*, video.first_trigger_observation AS video_first_trigger_observation
                              FROM hits JOIN competitor_videos AS video ON video.video_id = hits.video_id
                             WHERE hits.hit_id='h1'
                            """
                        ).fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                comment = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchone()
                self.assertEqual(comment["observation_point"], "D3")
            finally:
                conn.close()

    def test_observation_point_is_none_when_row_has_no_join_not_a_crash(self) -> None:
        # Reverse case for the fix above: calling prep_one_hit with a plain
        # "SELECT * FROM hits" row (no video_first_trigger_observation
        # column at all) must not raise -- it should record NULL, not crash
        # the whole hit into a spurious "failed" status.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(_fake_detail_result(tmp, comments=[{"comment_id": "c1", "text": "很不错的内容"}]))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    result = prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                self.assertEqual(result["status"], "completed")
                comment = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchone()
                self.assertIsNone(comment["observation_point"])
            finally:
                conn.close()

    def test_same_comment_can_exist_under_two_different_purposes(self) -> None:
        # Schema-level proof of the design goal behind including purpose in
        # the PRIMARY KEY (BR-HIT-007): the same real comment genuinely
        # collected again at a later purpose must get its own row, not be
        # silently dropped by INSERT OR IGNORE the way the old
        # (hit_id, comment_id) primary key would have. run_reverse_prep.py
        # itself only ever writes purpose='mature_analysis' today (early_topic
        # collection is not wired -- see its module docstring); this proves
        # the schema is ready for that once it is.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                conn.execute(
                    """
                    INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
                    VALUES ('h1', 'c1', '很不错的内容', 5, 0, 'early_topic', 'run_early')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
                    VALUES ('h1', 'c1', '很不错的内容', 12, 0, 'mature_analysis', 'run_mature')
                    """
                )
                conn.commit()

                rows = conn.execute(
                    "SELECT purpose, like_count FROM hit_comments WHERE hit_id='h1' AND comment_id='c1' ORDER BY purpose"
                ).fetchall()
                self.assertEqual([r["purpose"] for r in rows], ["early_topic", "mature_analysis"])
                self.assertEqual([r["like_count"] for r in rows], [5, 12])
            finally:
                conn.close()

    def test_retrying_the_same_purpose_stays_deduped_not_a_second_row(self) -> None:
        # Reverse case for the test above: a genuine retry of the SAME
        # purpose (not a different one) must still be idempotent -- the PK
        # change adds purpose as a dimension, it does not turn off dedup
        # within a single purpose.
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                for run_id in ("run_1", "run_2"):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO hit_comments(hit_id, comment_id, text, like_count, sample_rank, purpose, run_id)
                        VALUES ('h1', 'c1', '很不错的内容', 5, 0, 'mature_analysis', ?)
                        """,
                        (run_id,),
                    )
                conn.commit()

                rows = conn.execute(
                    "SELECT * FROM hit_comments WHERE hit_id='h1' AND comment_id='c1' AND purpose='mature_analysis'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()

    def test_ffmpeg_and_asr_python_paths_come_from_reverse_cfg_not_hardcoded(self) -> None:
        # Regression test for the ffmpeg/local-ASR-python absolute paths that
        # used to be hardcoded module constants (duplicated identically in
        # scripts/tools/compare_asr_providers.py) -- proves the values are
        # actually read from reverse_cfg, not just cosmetically no longer
        # module-level, by using distinctive paths and asserting they appear
        # in the real subprocess.run call args.
        distinctive_cfg = dict(REVERSE_CFG, ffmpeg_path="C:/distinctive/ffmpeg.exe", local_asr_python="C:/distinctive/python.exe")
        captured_calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured_calls.append(list(args))
            if str(LOCAL_ASR_SCRIPT) in args:
                return subprocess.CompletedProcess(args, returncode=0, stdout="这是一段足够长的转写文本用于测试", stderr="")
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(_fake_detail_result(tmp))
                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=fake_run):
                    prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=distinctive_cfg, run_id="run_test",
                    )
            finally:
                conn.close()

        ffmpeg_calls = [c for c in captured_calls if "C:/distinctive/ffmpeg.exe" in c]
        asr_calls = [c for c in captured_calls if "C:/distinctive/python.exe" in c]
        self.assertEqual(len(ffmpeg_calls), 1)
        self.assertEqual(len(asr_calls), 1)

    def test_transcript_too_short_marks_hit_failed_but_keeps_transcript_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(_fake_detail_result(tmp))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("short")):
                    result = prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                self.assertEqual(result["status"], "failed")
                transcript = conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1'").fetchone()
                self.assertEqual(transcript["processing_status"], "failed")
                self.assertIn("too_short", transcript["quality_flags"])
            finally:
                conn.close()

    def test_falls_back_to_video_download_url_when_no_music_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                run_dir = Path(tmp) / "mc_run"
                _write_comments_jsonl(run_dir, [])
                executor = _FakeExecutor(
                    ExternalCommandResult(
                        status="succeeded",
                        payload={"items": [{"video_download_url": "https://fake.cdn/video.mp4"}]},
                        raw_archive_ref=str(run_dir),
                        external_side_effect=True,
                    )
                )

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    result = prep_one_hit(
                        conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                        reverse_cfg=REVERSE_CFG, run_id="run_test",
                    )

                self.assertEqual(result["status"], "completed")
                transcript = conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1'").fetchone()
                self.assertEqual(transcript["processing_method"], "local_sensevoice_funasr_video_audio_fallback")
            finally:
                conn.close()

    def test_no_music_or_video_url_marks_failed_without_writing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                run_dir = Path(tmp) / "mc_run"
                _write_comments_jsonl(run_dir, [])
                executor = _FakeExecutor(
                    ExternalCommandResult(
                        status="succeeded", payload={"items": [{}]}, raw_archive_ref=str(run_dir), external_side_effect=True,
                    )
                )

                result = prep_one_hit(
                    conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                    reverse_cfg=REVERSE_CFG, run_id="run_test",
                )

                self.assertEqual(result["status"], "failed")
                self.assertIsNone(conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1'").fetchone())
            finally:
                conn.close()

    def test_mediacrawler_failure_marks_failed_without_writing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                executor = _FakeExecutor(ExternalCommandResult(status="failed_timeout", payload={}, raw_archive_ref=None, external_side_effect=True))

                result = prep_one_hit(
                    conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                    reverse_cfg=REVERSE_CFG, run_id="run_test",
                )

                self.assertEqual(result["status"], "failed")
                self.assertIsNone(conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1'").fetchone())
                hit = conn.execute("SELECT preparation_status FROM hits WHERE hit_id='h1'").fetchone()
                self.assertEqual(hit["preparation_status"], "failed")
            finally:
                conn.close()

    def test_retry_appends_a_new_version_never_overwrites_and_comments_stay_deduped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1")
                comments = [{"comment_id": "c1", "text": "很不错的内容"}]

                for attempt in range(2):
                    executor = _FakeExecutor(_fake_detail_result(tmp, comments=comments))
                    with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                         patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep(f"转写文本第{attempt}次足够长")):
                        prep_one_hit(
                            conn, executor, conn.execute("SELECT * FROM hits WHERE hit_id='h1'").fetchone(),
                            reverse_cfg=REVERSE_CFG, run_id=f"run_{attempt}",
                        )

                transcripts = conn.execute("SELECT * FROM hit_transcripts WHERE hit_id='h1' ORDER BY version").fetchall()
                self.assertEqual(len(transcripts), 2)
                self.assertEqual(transcripts[0]["version"], 1)
                self.assertEqual(transcripts[1]["version"], 2)
                self.assertIn("第0次", transcripts[0]["raw_transcript_text"])
                self.assertIn("第1次", transcripts[1]["raw_transcript_text"])
                comment_rows = conn.execute("SELECT * FROM hit_comments WHERE hit_id='h1'").fetchall()
                self.assertEqual(len(comment_rows), 1)
            finally:
                conn.close()


class RunReversePrepTests(unittest.TestCase):
    def test_processes_only_pending_hits_up_to_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = _connect(tmp)
            try:
                _insert_hit(conn, "h1", preparation_status="pending")
                _insert_hit(conn, "h2", preparation_status="completed")
                executor = _FakeExecutor(_fake_detail_result(tmp))

                with patch("scripts.core.business_data.run_reverse_prep.requests.get", return_value=_fake_response()), \
                     patch("scripts.core.business_data.run_reverse_prep.subprocess.run", side_effect=_fake_subprocess_for_prep("这是一段足够长的转写文本用于测试")):
                    report = run_reverse_prep(conn, limit=10, reverse_cfg=REVERSE_CFG, executor=executor)

                self.assertEqual(report["attempted"], 1)
                self.assertEqual(report["completed"], 1)
                self.assertEqual(len(executor.calls), 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
