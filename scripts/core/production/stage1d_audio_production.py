from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import os
from pathlib import Path
import unicodedata
import wave
from typing import Protocol

from scripts.core.external_adapters.goal_phase4_external_adapters import AsrAdapter
from scripts.core.external_adapters.local_sensevoice_executor import LocalSenseVoiceExecutor
from scripts.core.external_adapters.local_voxcpm2_executor import (
    LocalVoxCPM2AudioExecutor,
    VoxCPM2SynthesisRequest,
    VoxCPM2SynthesisResult,
)
from scripts.core.external_adapters.runtime_config import external_runtime_value
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from scripts.core.runtime.runtime_storage import runtime_path


class AudioProductionExecutor(Protocol):
    def synthesize(self, request: VoxCPM2SynthesisRequest) -> VoxCPM2SynthesisResult:
        ...


def _normalized_spoken_text(value: str) -> str:
    return "".join(
        character.casefold() for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _final_delivery_document(core: Stage0ContentProductionCore, version_id: str) -> tuple[str, str]:
    artifact = core.get_artifact_payload(version_id)
    payload = artifact["payload"]
    if artifact["artifact_kind"] != "review" or not isinstance(payload, dict):
        raise StateTransitionError("audio production requires the approved final review artifact")
    document = payload.get("document")
    if not isinstance(document, dict):
        raise StateTransitionError("final review artifact has no structured delivery document")
    title, script_text = document.get("title"), document.get("script_text")
    if not isinstance(title, str) or not title.strip() or not isinstance(script_text, str) or not script_text.strip():
        raise StateTransitionError("final review must contain a title and the exact delivery script_text")
    return title.strip(), script_text.strip()


def _technical_wav_review(path: Path) -> dict[str, object]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            sample_width = audio.getsampwidth()
            frame_count = audio.getnframes()
    except (wave.Error, OSError) as exc:
        raise StateTransitionError("generated audio is not a readable PCM WAV") from exc
    duration = frame_count / sample_rate if sample_rate else 0.0
    checks = {
        "readable_pcm_wav": True,
        "channel_count_supported": channels in {1, 2},
        "sample_rate_supported": sample_rate >= 16000,
        "sample_width_supported": sample_width in {2, 3, 4},
        "nonempty_duration": duration >= 0.5,
    }
    if not all(checks.values()):
        raise StateTransitionError("generated WAV failed controlled technical checks")
    return {
        "technical_status": "passed", "checks": checks, "channels": channels,
        "sample_rate": sample_rate, "sample_width": sample_width,
        "frame_count": frame_count, "duration_seconds": round(duration, 3),
    }


@dataclass
class AudioProductionService:
    core: Stage0ContentProductionCore
    executor: AudioProductionExecutor
    transcriber: AsrAdapter
    output_root: Path

    def produce(
        self,
        *,
        task_id: str,
        approved_content_version_id: str,
        voice_profile_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        title, script_text = _final_delivery_document(self.core, approved_content_version_id)
        profile = self.core.get_voice_profile(voice_profile_id=voice_profile_id)
        started = self.core.begin_audio_production(
            task_id=task_id, approved_content_version_id=approved_content_version_id,
            voice_profile_id=voice_profile_id, title=title, script_text=script_text,
            actor=actor, idempotency_key=idempotency_key,
        )
        production_id = started["audio_production_id"]
        existing = self.core.get_audio_production(audio_production_id=production_id)
        if existing["status"] != "processing":
            return {
                "audio_production_id": production_id,
                "status": existing["status"],
                "quality_review": existing["quality_review"],
                "audio_delivery_id": existing.get("audio_delivery_id"),
            }
        settings = profile["settings"]
        output_dir = self.output_root / production_id
        try:
            synthesis = self.executor.synthesize(VoxCPM2SynthesisRequest(
                text=script_text,
                reference_audio=Path(profile["reference_audio_ref"]),
                output_dir=output_dir,
                mode=profile["mode"],
                prompt_text=profile["prompt_text"],
                style_prompt=str(settings["style_prompt"]),
                emotion_reference_audio=Path(profile["emotion_reference_audio_ref"]) if profile["emotion_reference_audio_ref"] else None,
                emotion_prompt_text=profile["emotion_prompt_text"],
                emotion_preset=str(settings["emotion_preset"]),
                emotion_strength=float(settings["emotion_strength"]),
                segment_strategy=str(settings["segment_strategy"]),
                max_chars=int(settings["max_chars"]),
                silence_ms=int(settings["silence_ms"]),
                cfg_value=float(settings["cfg_value"]),
                inference_timesteps=int(settings["inference_timesteps"]),
            ))
            technical = _technical_wav_review(synthesis.audio_path)
            transcription = self.transcriber.transcribe(
                media_ref=production_id, media_path=str(synthesis.audio_path), max_duration_seconds=600,
            )
            transcript_path = Path(str(transcription.payload["transcript_ref"]))
            if not transcript_path.is_file():
                raise StateTransitionError("audio ASR result did not retain its transcript")
            transcript = transcript_path.read_text(encoding="utf-8").strip()
            target_normalized, transcript_normalized = _normalized_spoken_text(script_text), _normalized_spoken_text(transcript)
            if not target_normalized or not transcript_normalized:
                raise StateTransitionError("audio text-consistency comparison has an empty side")
            similarity = SequenceMatcher(None, target_normalized, transcript_normalized).ratio()
            length_coverage = min(len(target_normalized), len(transcript_normalized)) / max(len(target_normalized), len(transcript_normalized))
            consistency_status = "passed" if similarity >= 0.90 and length_coverage >= 0.90 else "human_attention_required"
            quality_review = {
                **technical,
                "asr_status": "passed",
                "asr_model_ref": transcription.payload["asr_model_ref"],
                "vad_model_ref": transcription.payload["vad_model_ref"],
                "transcript_ref": str(transcript_path),
                "transcript_hash": transcription.payload["transcript_hash"],
                "text_similarity": round(similarity, 4),
                "length_coverage": round(length_coverage, 4),
                "text_consistency_status": consistency_status,
                "human_review_required": True,
            }
            synthesis_record = {
                "engine": "local_voxcpm2",
                "audio_sha256": synthesis.audio_sha256,
                "metadata_sha256": hashlib.sha256(synthesis.metadata_path.read_bytes()).hexdigest(),
                "segment_strategy": synthesis.metadata.get("segment_strategy"),
                "segment_count": synthesis.metadata.get("segment_count"),
                "silence_ms": synthesis.metadata.get("silence_ms"),
                "voice_profile_id": voice_profile_id,
            }
            result = self.core.complete_audio_production(
                audio_production_id=production_id, audio_ref=str(synthesis.audio_path),
                metadata_ref=str(synthesis.metadata_path), synthesis=synthesis_record,
                quality_review=quality_review, actor=actor,
            )
            return {**result, "quality_review": quality_review}
        except Exception as exc:
            self.core.fail_audio_production(
                audio_production_id=production_id,
                error={"error_type": type(exc).__name__, "reason": str(exc)}, actor=actor,
            )
            raise

    def review(
        self,
        *,
        audio_production_id: str,
        decision: str,
        issue_scope: str,
        reason: str,
        actor: str,
    ) -> dict[str, str]:
        return self.core.review_audio_production(
            audio_production_id=audio_production_id, decision=decision, issue_scope=issue_scope,
            reason=reason, actor=actor, actor_kind="user",
        )


def build_production_audio_service(core: Stage0ContentProductionCore) -> AudioProductionService:
    if core.data_identity != "production":
        raise StateTransitionError("real audio connector may only bind to the production Core")
    output_root = Path(
        os.environ.get("VOXCPM2_OUTPUT_DIR")
        or runtime_path("formal", "audio_production_runtime")
    )
    executor = LocalVoxCPM2AudioExecutor(
        python_executable=Path(external_runtime_value("VOXCPM2_PYTHON")),
        generator_script=Path(external_runtime_value("VOXCPM2_GENERATE_SCRIPT")),
        model_dir=Path(external_runtime_value("VOXCPM2_MODEL_DIR")),
        device=os.environ.get("VOXCPM2_DEVICE") or "cuda",
    )
    asr = LocalSenseVoiceExecutor(
        python_executable=Path(external_runtime_value("SENSEVOICE_PYTHON")),
        asr_model=external_runtime_value("SENSEVOICE_ASR_MODEL"),
        vad_model=external_runtime_value("SENSEVOICE_VAD_MODEL"),
        archive_root=output_root / "asr_transcripts",
        worker_path=Path(external_runtime_value("SENSEVOICE_WORKER")),
    )
    return AudioProductionService(
        core=core, executor=executor, transcriber=AsrAdapter(asr), output_root=output_root,
    )
