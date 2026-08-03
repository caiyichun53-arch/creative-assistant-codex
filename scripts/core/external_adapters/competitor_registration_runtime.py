from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

from scripts.core.external_adapters.goal_phase4_external_adapters import ExternalAdapterError
from scripts.core.external_adapters.windows_process import hidden_process_kwargs


@dataclass(frozen=True)
class MaterializedAudio:
    media_ref: str
    wav_path: Path
    source_sha256: str


@dataclass(frozen=True)
class LocalCompetitorMediaMaterializer:
    """Bounded signed-media download and local ffmpeg audio extraction."""

    archive_root: Path
    ffmpeg_executable: Path
    timeout_seconds: int = 90
    max_download_bytes: int = 512 * 1024 * 1024

    def materialize(self, *, media_url: str, media_ref: str) -> MaterializedAudio:
        if not media_url.startswith(("https://", "http://")):
            raise ExternalAdapterError("competitor media URL must be HTTP(S)")
        if not self.ffmpeg_executable.is_file():
            raise ExternalAdapterError("configured ffmpeg executable does not exist")
        safe_ref = re.sub(r"[^A-Za-z0-9_.-]+", "_", media_ref).strip("_") or "media"
        work_dir = self.archive_root / safe_ref
        work_dir.mkdir(parents=True, exist_ok=True)
        source_path = work_dir / "source_media.bin"
        wav_path = work_dir / "audio_16k_mono.wav"
        source_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
        request = Request(
            media_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            },
        )
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response, source_path.open("wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > self.max_download_bytes:
                        raise ExternalAdapterError("competitor media exceeds the configured download limit")
                    digest.update(chunk)
                    target.write(chunk)
        except Exception:
            source_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
            raise
        if downloaded == 0:
            source_path.unlink(missing_ok=True)
            raise ExternalAdapterError("competitor media download was empty")
        completed = subprocess.run(
            [str(self.ffmpeg_executable), "-y", "-i", str(source_path), "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
            check=False,
            shell=False,
            **hidden_process_kwargs(),
        )
        if completed.returncode != 0 or not wav_path.is_file():
            stderr_tail = completed.stderr.decode("utf-8", errors="replace")[-500:].strip()
            source_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
            detail = f": {stderr_tail}" if stderr_tail else ""
            raise ExternalAdapterError(f"ffmpeg failed to extract competitor audio{detail}")
        return MaterializedAudio(media_ref=media_ref, wav_path=wav_path, source_sha256=digest.hexdigest())
