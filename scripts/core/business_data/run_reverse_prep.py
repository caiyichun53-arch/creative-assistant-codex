"""Reverse-prep worker: turns a promoted hit (hits.preparation_status='pending')
into a stored local transcript (BR-ASR-001) plus filtered comments
(BR-COLLECT-005/BR-COLLECT-006), fetched in ONE MediaCrawler detail call per
the project constitution's "合并成一趟 detail 爬 -- 下载链接+评论一次拿"
design, not two separate crawls.

This module implements the source document's section 13 (音频、ASR 与文本清理)
and section 16 (评论采集) requirements as far as the CURRENT data model
supports them:
  - raw_transcript / cleaned_transcript versioning, never overwritten
    ("转写版本不可覆盖") -- hit_transcripts is append-only, one row per attempt.
  - Provenance: asr_model, vad_model, processing_method, and a hash of the
    downloaded audio bytes (audio_sha256) recorded on every attempt.
  - pending/running/completed/failed lifecycle vocabulary (section 13's exact
    wording), not a bespoke one.
  - Deterministic (no LLM) quality-anomaly detection: empty transcript, too
    short, high repetition (the failure mode BR-ASR-003 actually observed with
    a cloud provider looping its output).
  - A 5-tier processing-priority order (section 13.2), best-effort against
    what the schema can currently express -- see _priority_tier()'s docstring
    for exactly which tiers have no real data yet (acknowledged gaps, not
    invented data).
  - hit_comments.sample_rank preserves the crawler's own comment ordering
    (section 16), assigned before filtering so gaps show dropped comments.
  - Audio source fallback (added 2026-07-08 after the historical backfill hit
    this for real): prefers music_download_url, but falls back to
    video_download_url when the fresh detail response has no music track --
    _to_wav_16k_mono strips the video stream either way, so extracting audio
    from the full video file works identically. processing_method records
    which source was actually used (local_sensevoice_funasr vs
    local_sensevoice_funasr_video_audio_fallback) for audit purposes.

Explicitly NOT implemented (documented limitations, not oversights):
  - Priority tier 1 (已选 production_task/用户明确研究对象) -- no "selected
    production task" concept exists in the schema yet.
  - Multi-stage comment re-collection (BR-HIT-002/006/007): hit_comments now
    records which purpose a batch belongs to (2026-07-13), but this worker
    still only fires once, when a hit clears judgement, and always tags that
    single fetch purpose='mature_analysis' -- the early_topic pass (collect
    again as soon as a video FIRST triggers candidacy, before D7) is not
    wired up; it needs a real trigger hook into the D0-D6 candidate-detection
    path (BR-HIT-005), which is a separate, larger piece of work than the
    schema change alone.

scripts/tools/compare_asr_providers.py is a different, narrower thing: a
one-off BR-ASR-003 benchmark comparing local vs cloud ASR, explicitly not a
production path (see its own docstring), and it never touches hit_transcripts/
hit_comments or hits.preparation_status.

2026-07-08 (auto-trigger): run_full_registration()/run_rejudge_only()/
run_daily_incremental() in run_competitor_registration_full.py each call
run_reverse_prep(judgement_run_id=<that judgement's own run_id>) synchronously
right after judge_domain(), by explicit user decision ("爆款文案是要提取经验
的,所以备料这里最后自动执行") -- a promoted hit is useless for DNA/experience
extraction until it has a transcript, so judging a hit without also prepping
it just defers the real work. judgement_run_id scopes this to the hits that
specific judgement run just promoted, not the whole historical pending
backlog -- a judgement run stays bounded by how many hits IT created, not by
however large the backlog has grown. This is a real, synchronous, blocking
call (real network + local ASR inference per hit) -- judge_domain() itself
stays instant; it's the caller (a daily/registration run) that now also waits
for prep to finish before returning.

Usage (from repo root, using the project's normal Python -- NOT the SenseVoice
env or MediaCrawler's own venv, both of which are only invoked internally via
subprocess):
    python -m scripts.core.business_data.run_reverse_prep --limit 1
    python -m scripts.core.business_data.run_reverse_prep --limit 5 --json
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.register_competitor_accounts import (  # noqa: E402
    DEFAULT_DB,
    install_schema,
)
from scripts.core.execution_contract import require_baseline_citations  # noqa: E402
from scripts.core.external_adapters import ExternalAdapterCommand  # noqa: E402
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor  # noqa: E402

DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
FALLBACK_SETTINGS = ROOT / "config" / "settings.example.yaml"

# ffmpeg/local-ASR-python are machine-specific absolute paths -- read from
# reverse_cfg (config/settings.yaml reverse_engine.ffmpeg_path/local_asr_python)
# instead of hardcoding here. Previously hardcoded identically in this file
# and in scripts/tools/compare_asr_providers.py -- the exact "two sources of
# truth" pattern this project has fixed elsewhere (see first_crawl_excluded_reason
# in run_competitor_registration_full.py for precedent).
LOCAL_ASR_SCRIPT = ROOT / "scripts" / "tools" / "_local_asr_transcribe.py"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Sentence-ish boundary for repetition detection -- ASCII/Chinese enders and
# newlines. Not a linguistic sentence splitter, just enough to catch the
# "looped a phrase" failure mode BR-ASR-003 documented for a cloud provider.
_SENTENCE_SPLIT = re.compile(r"[。！？.!?\n]+")

REVERSE_STATUSES = ("pending", "running", "completed", "failed")


def validate_reverse_prep_execution_contract(reverse_cfg: dict[str, Any]) -> dict[str, Any]:
    """Contract gate for this module (see scripts/core/execution_contract.py).
    Cites the four rules this pipeline actually implements: local transcript
    generation (BR-ASR-001), never persisting the signed download URL
    (BR-ASR-002), collecting comments in the same detail crawl (BR-COLLECT-005),
    and deterministic noise-filtered idempotent storage (BR-COLLECT-006)."""
    contract = require_baseline_citations(["6", "16"])
    errors: list[str] = []
    for key in ("models_root", "asr_model", "vad_model"):
        if not str(reverse_cfg.get(key) or "").strip():
            errors.append(f"reverse_engine.{key} must be set")
    if int(reverse_cfg.get("min_transcript_chars", 0)) <= 0:
        errors.append("reverse_engine.min_transcript_chars must be a positive integer")
    if int(reverse_cfg.get("top_comments", 0)) != 60:
        errors.append("reverse_engine.top_comments must be 60 (BR-COLLECT-005/006)")
    if int(reverse_cfg.get("min_comment_len", 0)) != 5:
        errors.append("reverse_engine.min_comment_len must be 5 (BR-COLLECT-006)")
    if errors:
        raise ValueError("reverse prep execution contract mismatch: " + "; ".join(errors))
    return contract


def _priority_tier(row: sqlite3.Row) -> int:
    """Best-effort mapping of source document section 13.2's 5-tier queue onto
    what the CURRENT schema can actually express. Lower number = higher
    priority (processed first).

    Honest gaps, not invented data:
      - Tier 1 (已选 production_task / 用户明确研究对象): no "selected
        production task" concept exists in the schema yet (the experience/
        creation pipeline isn't built) -- no row can ever match tier 1 today.
      - Tier 2's "深度分析合格样本" (deep_eligibility) and tier 4's "普通对照
        和低表现反例" (control/counter-example samples): BR-HIT-006/BR-HIT-002
        are acknowledged gaps -- no such fields exist to match on, so only the
        parts of tiers 2/4 that DO have a real column are used.
    """
    if row["video_baseline_mode"] == "formal_d_series":
        return 2  # formal D/P candidates -- the part of tier 2 that exists
    if row["video_first_contact_category"] == "formal_new":
        return 3  # today's formal newly-tracked video
    if row["video_baseline_mode"] == "mature_history" and row["judgment_confidence"] == "rough":
        return 4  # historical high-signal -- the part of tier 4 that exists
    return 5  # remaining historical/transition samples


def select_pending_hits(conn: sqlite3.Connection, *, limit: int, judgement_run_id: str | None = None) -> list[sqlite3.Row]:
    """judgement_run_id, when given, scopes to hits promoted by that specific
    judgement run only (hits.run_id, distinct from this module's own
    reverse-prep run_id) -- used by the auto-trigger hook in
    run_competitor_registration_full.py so a judgement run only backfills the
    hits it just promoted, not the entire historical pending backlog. Manual/
    CLI usage (judgement_run_id=None) is unscoped, for deliberately working
    through the backlog."""
    query = """
        SELECT hits.*, video.baseline_mode AS video_baseline_mode,
               video.first_contact_category AS video_first_contact_category,
               video.first_trigger_observation AS video_first_trigger_observation
        FROM hits
        JOIN competitor_videos AS video ON video.video_id = hits.video_id
        WHERE hits.preparation_status='pending'
    """
    params: tuple[Any, ...] = ()
    if judgement_run_id is not None:
        query += " AND hits.run_id = ?"
        params = (judgement_run_id,)
    rows = conn.execute(query, params).fetchall()
    ranked = sorted(rows, key=lambda r: (_priority_tier(r), r["promoted_at"]))
    return ranked[:limit]


def _platform_dir_name(platform: str) -> str:
    return "douyin" if platform.lower() in {"douyin", "dy"} else platform.lower()


def _read_jsonl(files: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(files):
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def fetch_detail_with_comments(
    executor: LocalMediaCrawlerExecutor,
    *,
    platform: str,
    video_url: str,
) -> dict[str, Any]:
    """One MediaCrawler detail fetch, get_comment=yes -- resolves both the
    fresh signed download URL (BR-ASR-002: fetched immediately before use,
    never persisted) and raw comments (BR-COLLECT-005: same crawl, not a
    separate pass) in a single network round trip. Reply/second-level
    comments are excluded at the crawler level (LocalMediaCrawlerExecutor
    always passes --get_sub_comment no), matching section 16's "第一阶段关闭
    二级评论"."""
    command = ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.video_snapshot",
        executable="vendor/MediaCrawler/main.py",
        args=(platform, "detail", "--get_comment", "yes"),
        input_payload={
            "platform": platform,
            "source_url": video_url,
            "source_kind": "detail",
            "headless": True,
            "with_comments": True,
        },
        max_items=1,
        timeout_seconds=180,
    )
    result = executor.execute(command)
    if result.status != "succeeded":
        raise RuntimeError(f"MediaCrawler detail fetch failed: {result.status}")
    items = result.payload.get("items") or []
    if not items:
        raise RuntimeError("MediaCrawler detail fetch returned no items")

    jsonl_dir = Path(result.raw_archive_ref) / "raw" / _platform_dir_name(platform) / "jsonl"
    comments = _read_jsonl(jsonl_dir.glob("*_comments_*.jsonl"))
    return {"item": items[0], "comments": comments}


def _download(url: str, dst: Path) -> None:
    response = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}, timeout=60)
    response.raise_for_status()
    dst.write_bytes(response.content)


def _to_wav_16k_mono(src: Path, dst: Path, ffmpeg_path: str) -> None:
    subprocess.run(
        [ffmpeg_path, "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)],
        capture_output=True,
        check=True,
    )


def run_local_asr(wav_path: Path, reverse_cfg: dict[str, Any]) -> str:
    models_root = Path(reverse_cfg["models_root"])
    asr_model = str(models_root / reverse_cfg["asr_model"])
    vad_model = str(models_root / reverse_cfg["vad_model"])
    local_asr_python = reverse_cfg["local_asr_python"]
    completed = subprocess.run(
        [local_asr_python, str(LOCAL_ASR_SCRIPT), str(wav_path), "--asr-model", asr_model, "--vad-model", vad_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"local ASR subprocess failed: {completed.stderr[-2000:]}")
    return completed.stdout.strip()


def clean_transcript(raw_text: str) -> str:
    """Section 13.1's "cleaned_transcript v1" tier: deterministic whitespace
    normalization and exact-consecutive-duplicate-sentence removal. No LLM,
    no semantic rewriting -- "不得改写原意"."""
    collapsed_whitespace = re.sub(r"\s+", " ", raw_text).strip()
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(raw_text) if s.strip()]
    deduped: list[str] = []
    for sentence in sentences:
        if not deduped or deduped[-1] != sentence:
            deduped.append(sentence)
    if len(deduped) == len(sentences) or not deduped:
        # No consecutive duplicates found (or nothing to split) -- the
        # whitespace-normalized text is already the cleaned version.
        return collapsed_whitespace
    return "。".join(deduped)


def detect_quality_flags(raw_text: str, *, min_transcript_chars: int) -> list[str]:
    """Deterministic anomaly checks section 13.1 requires."""
    flags: list[str] = []
    char_count = len(raw_text)
    if char_count == 0:
        flags.append("empty")
        return flags
    if char_count < min_transcript_chars:
        flags.append("too_short")

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(raw_text) if s.strip()]
    if len(sentences) >= 4:
        most_common_text, most_common_count = Counter(sentences).most_common(1)[0]
        if most_common_count >= 3 and (most_common_count / len(sentences)) > 0.3:
            flags.append("high_repetition")
    return flags


def filter_comments(raw_comments: list[dict[str, Any]], *, min_comment_len: int, top_comments: int) -> list[dict[str, Any]]:
    """BR-COLLECT-006: deterministic noise filtering (min length, dedup by
    comment_id) -- no LLM involved. Falls back to a content hash when a raw
    comment lacks comment_id (documented edge case). sample_rank is the
    comment's position in the crawler's own raw ordering (section 16),
    assigned before filtering so dropped comments leave a visible gap."""
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for rank, raw in enumerate(raw_comments):
        text = str(raw.get("text") or raw.get("content") or "").strip()
        if len(text) < min_comment_len:
            continue
        comment_id = str(raw.get("comment_id") or raw.get("id") or "").strip()
        if not comment_id:
            comment_id = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if comment_id in seen:
            continue
        seen.add(comment_id)
        kept.append(
            {
                "comment_id": comment_id,
                "text": text,
                "like_count": int(raw.get("like_count") or 0),
                "parent_comment_id": raw.get("parent_comment_id") or raw.get("reply_to_reply_id") or None,
                "sample_rank": rank,
            }
        )
        if len(kept) >= top_comments:
            break
    return kept


def prep_one_hit(
    conn: sqlite3.Connection,
    executor: LocalMediaCrawlerExecutor,
    hit_row: sqlite3.Row,
    *,
    reverse_cfg: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    hit_id = hit_row["hit_id"]
    conn.execute("UPDATE hits SET preparation_status='running' WHERE hit_id=?", (hit_id,))
    conn.commit()
    try:
        detail = fetch_detail_with_comments(executor, platform=hit_row["platform"], video_url=hit_row["url"])
        download_url = detail["item"].get("music_download_url")
        processing_method = "local_sensevoice_funasr"
        if not download_url:
            # Edge case (observed 2026-07-08 backfill): some fresh detail
            # responses carry no music_download_url at all. Fall back to the
            # video's own download URL -- _to_wav_16k_mono already strips the
            # video stream (-vn), so extracting audio from the full video file
            # works the same way as extracting it from the music track.
            download_url = detail["item"].get("video_download_url")
            processing_method = "local_sensevoice_funasr_video_audio_fallback"
        if not download_url:
            raise RuntimeError("no music_download_url or video_download_url in fresh detail response")

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            raw_audio = work_dir / "raw_audio"
            _download(download_url, raw_audio)
            audio_sha256 = hashlib.sha256(raw_audio.read_bytes()).hexdigest()
            wav_path = work_dir / "audio_16k_mono.wav"
            _to_wav_16k_mono(raw_audio, wav_path, reverse_cfg["ffmpeg_path"])
            raw_transcript_text = run_local_asr(wav_path, reverse_cfg)

        min_transcript_chars = int(reverse_cfg["min_transcript_chars"])
        quality_flags = detect_quality_flags(raw_transcript_text, min_transcript_chars=min_transcript_chars)
        cleaned_transcript_text = clean_transcript(raw_transcript_text)
        processing_status = "failed" if quality_flags else "completed"
        version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM hit_transcripts WHERE hit_id=?", (hit_id,)).fetchone()[0]) + 1

        conn.execute(
            """
            INSERT INTO hit_transcripts(
                transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text,
                char_count, asr_model, vad_model, processing_method, audio_sha256,
                quality_flags, processing_status, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{hit_id}_v{version}", hit_id, version, raw_transcript_text, cleaned_transcript_text,
                len(raw_transcript_text), str(reverse_cfg["asr_model"]), str(reverse_cfg["vad_model"]), processing_method, audio_sha256,
                ",".join(quality_flags), processing_status, run_id,
            ),
        )

        kept_comments = filter_comments(
            detail["comments"],
            min_comment_len=int(reverse_cfg["min_comment_len"]),
            top_comments=int(reverse_cfg["top_comments"]),
        )
        # This worker fires once, when a hit clears judgement -- of BR-HIT-007's
        # four purposes, that is the "D7/P+7d 才首次正式触发 -> 采一次
        # mature_analysis" case (not an early-topic pass; see module docstring
        # for what multi-stage collection still isn't implemented).
        # observation_point comes from the video's own real first-trigger
        # point when known; NULL when the video has no formal D baseline yet
        # (rough/cold-start candidates) rather than guessing one.
        observation_point = (
            hit_row["video_first_trigger_observation"] if "video_first_trigger_observation" in hit_row.keys() else None
        )
        for comment in kept_comments:
            conn.execute(
                """
                INSERT OR IGNORE INTO hit_comments(
                    hit_id, comment_id, text, like_count, parent_comment_id, sample_rank,
                    purpose, observation_point, sampling_strategy, run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hit_id, comment["comment_id"], comment["text"], comment["like_count"],
                    comment["parent_comment_id"], comment["sample_rank"],
                    "mature_analysis", observation_point, "top_n_by_platform_popularity", run_id,
                ),
            )

        new_status = "completed" if processing_status == "completed" else "failed"
        conn.execute("UPDATE hits SET preparation_status=? WHERE hit_id=?", (new_status, hit_id))
        conn.commit()
        return {
            "hit_id": hit_id,
            "status": new_status,
            "char_count": len(raw_transcript_text),
            "quality_flags": quality_flags,
            "comment_count": len(kept_comments),
        }
    except Exception as exc:  # noqa: BLE001 -- one hit's failure must not abort the batch
        conn.execute("UPDATE hits SET preparation_status='failed' WHERE hit_id=?", (hit_id,))
        conn.commit()
        return {"hit_id": hit_id, "status": "failed", "error": str(exc)}


def run_reverse_prep(
    conn: sqlite3.Connection,
    *,
    limit: int,
    reverse_cfg: dict[str, Any],
    executor: LocalMediaCrawlerExecutor | None = None,
    judgement_run_id: str | None = None,
) -> dict[str, Any]:
    validate_reverse_prep_execution_contract(reverse_cfg)
    executor = executor or LocalMediaCrawlerExecutor()
    run_id = "reverse_prep_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    pending = select_pending_hits(conn, limit=limit, judgement_run_id=judgement_run_id)
    results = [prep_one_hit(conn, executor, hit, reverse_cfg=reverse_cfg, run_id=run_id) for hit in pending]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def _safe_db_path(path: Path) -> Path:
    resolved = path if path.is_absolute() else ROOT / path
    resolved = resolved.resolve()
    allowed = (ROOT / "data" / "formal").resolve()
    if not resolved.is_relative_to(allowed):
        raise ValueError(f"reverse prep DB must stay under data/formal: {resolved}")
    if resolved.name == "creation.db":
        raise ValueError("refusing to write old data/creation.db")
    return resolved


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Reverse-prep pending promoted hits: local transcript + comments.")
    parser.add_argument("--limit", type=int, default=1, help="How many pending hits (preparation_status='pending') to prep in this run.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings_path = DEFAULT_SETTINGS if DEFAULT_SETTINGS.exists() else FALLBACK_SETTINGS
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    reverse_cfg = settings["reverse_engine"]
    db_path = _safe_db_path(Path(args.db))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        install_schema(conn)
        report = run_reverse_prep(conn, limit=args.limit, reverse_cfg=reverse_cfg)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0 if report["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
