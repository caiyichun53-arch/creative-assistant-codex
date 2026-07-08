"""One-off ASR provider comparison: local SenseVoice vs MiMo cloud ASR, on the
same real promoted hit's audio.

Governed by BUSINESS_RULE_CATALOG.yaml BR-ASR-003 (see that entry for the full
reasoning) -- this is a bounded, user-initiated benchmark, not a change to the
production reverse-prep path. It exists so the two providers can actually be
compared with a real Chinese 口播 sample before BR-ASR-001's default (local
SenseVoice) is reconsidered, and so this comparison itself is a real, cited,
committed script rather than an ad hoc command run directly against the real
world (see CLAUDE.md/AGENTS.md "执行纪律").

Usage (from repo root, using the project's normal Python -- NOT the SenseVoice
env, that is only used internally via subprocess):
    python -m scripts.tools.compare_asr_providers --video-id <competitor_videos.video_id>
    python -m scripts.tools.compare_asr_providers   # picks the most recent real hit
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.external_adapters import ExternalAdapterCommand  # noqa: E402
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor  # noqa: E402

DB_PATH = ROOT / "data" / "formal" / "production_activation.sqlite3"
FFMPEG = Path("I:/AI_Models/ffmpeg/ffmpeg.exe")
LOCAL_ASR_PYTHON = Path("C:/Users/15891/anaconda3/envs/voxcpm2/python.exe")
LOCAL_ASR_SCRIPT = Path(__file__).with_name("_local_asr_transcribe.py")
# 2026-07-08: the public docs' example endpoint (api.xiaomimimo.com) 401s for a
# Token Plan key -- Token Plan subscriptions have their own dedicated base URL,
# confirmed from the user's real account console screenshot.
MIMO_ENDPOINT = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5-asr"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _load_env_value(key: str) -> str:
    env_path = ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} not found in .env")


def _pick_hit(video_id: str | None) -> tuple[str, str, str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if video_id:
            row = conn.execute(
                "SELECT video_id, title, url FROM hits WHERE video_id=?", (video_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT video_id, title, url FROM hits ORDER BY promoted_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError(f"no matching hit found (video_id={video_id!r})")
        return row["video_id"], row["title"], row["url"]
    finally:
        conn.close()


def _fetch_fresh_music_url(video_url: str, work_dir: Path) -> str:
    executor = LocalMediaCrawlerExecutor(archive_root=work_dir / "mc_detail", timeout_seconds=180)
    command = ExternalAdapterCommand(
        adapter_id="collector.mediacrawler",
        capability="platform.video_snapshot",
        executable="vendor/MediaCrawler/main.py",
        args=("douyin", "detail", "--get_comment", "no"),
        input_payload={"platform": "douyin", "source_url": video_url, "source_kind": "detail", "headless": True},
        max_items=1,
        timeout_seconds=180,
    )
    result = executor.execute(command)
    if result.status != "succeeded":
        raise RuntimeError(f"MediaCrawler detail fetch failed: {result.status}")
    items = result.payload.get("items") or []
    if not items:
        raise RuntimeError("MediaCrawler detail fetch returned no items")
    url = items[0].get("music_download_url")
    if not url:
        raise RuntimeError("no music_download_url in fresh detail response")
    return url


def _download(url: str, dst: Path) -> None:
    response = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}, timeout=60)
    response.raise_for_status()
    dst.write_bytes(response.content)


def _to_wav_16k_mono(src: Path, dst: Path) -> None:
    subprocess.run(
        [str(FFMPEG), "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)],
        capture_output=True,
        check=True,
    )


def _to_mp3_for_mimo(src: Path, dst: Path) -> None:
    # 64kbps mono keeps a several-minute clip well under MiMo's 10MB base64 cap.
    subprocess.run(
        [str(FFMPEG), "-y", "-i", str(src), "-vn", "-ac", "1", "-b:a", "64k", str(dst)],
        capture_output=True,
        check=True,
    )


def _run_local_asr(wav_path: Path, settings: dict) -> str:
    reverse_cfg = settings["reverse_engine"]
    models_root = Path(reverse_cfg["models_root"])
    asr_model = str(models_root / reverse_cfg["asr_model"])
    vad_model = str(models_root / reverse_cfg["vad_model"])
    completed = subprocess.run(
        [str(LOCAL_ASR_PYTHON), str(LOCAL_ASR_SCRIPT), str(wav_path), "--asr-model", asr_model, "--vad-model", vad_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"local ASR subprocess failed: {completed.stderr[-2000:]}")
    return completed.stdout.strip()


def _run_mimo_asr(mp3_path: Path) -> str:
    api_key = _load_env_value("HERMES_BUSINESS_API_KEY")
    audio_b64 = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
    if len(audio_b64) > 10 * 1024 * 1024:
        raise RuntimeError(f"base64 audio too large for MiMo (>10MB): {len(audio_b64)} bytes")
    payload = {
        "model": MIMO_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": f"data:audio/mpeg;base64,{audio_b64}"}}],
            }
        ],
        "asr_options": {"language": "auto"},
    }
    response = requests.post(
        MIMO_ENDPOINT,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"MiMo HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    return data["choices"][0]["message"]["content"]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    require_catalog_citations(["BR-ASR-003"])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", default=None)
    args = parser.parse_args()

    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))

    video_id, title, video_url = _pick_hit(args.video_id)
    print(f"video_id={video_id}")
    print(f"title={title}")
    print(f"url={video_url}")

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        print("fetching fresh download url via MediaCrawler detail...")
        music_url = _fetch_fresh_music_url(video_url, work_dir)

        raw_audio = work_dir / "raw_audio"
        print("downloading audio...")
        _download(music_url, raw_audio)

        wav_path = work_dir / "audio_16k_mono.wav"
        mp3_path = work_dir / "audio_64k.mp3"
        _to_wav_16k_mono(raw_audio, wav_path)
        _to_mp3_for_mimo(raw_audio, mp3_path)

        print("running local SenseVoice ASR...")
        local_text = _run_local_asr(wav_path, settings)
        print(f"local_sensevoice ({len(local_text)} chars): {local_text}")

        print("running MiMo cloud ASR...")
        mimo_text = _run_mimo_asr(mp3_path)

    result = {
        "video_id": video_id,
        "title": title,
        "local_sensevoice": local_text,
        "mimo": mimo_text,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
