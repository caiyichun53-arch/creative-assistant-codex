from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Callable

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalCommandResult,
)
from scripts.core.external_adapters.windows_process import hidden_process_kwargs
from scripts.core.runtime.liveness import RuntimeLivenessError, run_process_with_liveness


@dataclass(frozen=True)
class LocalSenseVoiceExecutor:
    """Controlled local transcription bridge.

    It keeps local transcription outside the business core while returning only
    a stable transcript reference and hash to the core.
    """

    python_executable: Path
    asr_model: str
    vad_model: str
    archive_root: Path
    worker_path: Path
    timeout_seconds: int = 300
    progress_callback: Callable[[str], None] | None = None

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        if command.adapter_id != "asr.sensevoice" or command.capability != "media.transcription":
            raise ExternalAdapterError("unsupported local SenseVoice adapter command")
        media_path = Path(str(command.input_payload.get("controlled_media_path") or ""))
        if not media_path.is_file():
            return ExternalCommandResult(
                status="failed_media_missing",
                payload={"error": "controlled transcription media is missing"},
                external_side_effect=False,
            )
        if not self.python_executable.is_file() or not self.worker_path.is_file():
            return ExternalCommandResult(
                status="failed_runtime_missing",
                payload={"error": "local transcription runtime is not configured"},
                external_side_effect=False,
            )
        try:
            worker_env = os.environ.copy()
            worker_env["PYTHONUTF8"] = "1"
            worker_env["PYTHONIOENCODING"] = "utf-8"
            completed = run_process_with_liveness(
                [
                    str(self.python_executable),
                    str(self.worker_path),
                    str(media_path),
                    "--asr-model",
                    self.asr_model,
                    "--vad-model",
                    self.vad_model,
                ],
                kind="transcription",
                env=worker_env,
                process_options=hidden_process_kwargs(),
                on_activity=self.progress_callback,
            )
        except RuntimeLivenessError as exc:
            return ExternalCommandResult(
                status="failed_timeout",
                payload={"error": str(exc), "liveness_state": exc.state, "activity_count": exc.activity_count},
                external_side_effect=False,
            )
        if completed.returncode != 0:
            return ExternalCommandResult(
                status=f"failed_exit_{completed.returncode}",
                payload={"error": "local transcription failed"},
                external_side_effect=False,
            )
        transcript = completed.stdout.strip()
        if not transcript:
            return ExternalCommandResult(
                status="failed_empty_transcript",
                payload={"error": "local transcription returned no text"},
                external_side_effect=False,
            )
        digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        self.archive_root.mkdir(parents=True, exist_ok=True)
        transcript_path = self.archive_root / f"{digest}.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        return ExternalCommandResult(
            status="succeeded",
            payload={
                "transcript_ref": str(transcript_path),
                "transcript_hash": digest,
                "asr_model_ref": self.asr_model,
                "vad_model_ref": self.vad_model,
                "duration_seconds": 0,
                "segment_count": 0,
                "quality_status": "completed",
            },
            raw_archive_ref=str(transcript_path),
            external_side_effect=False,
        )
