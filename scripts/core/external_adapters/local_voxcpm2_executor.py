from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from scripts.core.external_adapters.windows_process import hidden_process_kwargs
from scripts.core.runtime.liveness import RuntimeLivenessError, run_process_with_liveness


class VoxCPM2ExecutionError(RuntimeError):
    pass


def _last_json_object(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not output[index + end :].strip():
            result = value
            break
    if result is None:
        raise VoxCPM2ExecutionError("VoxCPM2 did not return its final JSON result")
    return result


@dataclass(frozen=True)
class VoxCPM2SynthesisRequest:
    text: str
    reference_audio: Path
    output_dir: Path
    mode: str
    prompt_text: str
    style_prompt: str
    emotion_reference_audio: Path | None
    emotion_prompt_text: str
    emotion_preset: str
    emotion_strength: float
    segment_strategy: str = "auto"
    max_chars: int = 120
    silence_ms: int = 260
    cfg_value: float = 2.0
    inference_timesteps: int = 10


@dataclass(frozen=True)
class VoxCPM2SynthesisResult:
    audio_path: Path
    metadata_path: Path
    audio_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LocalVoxCPM2AudioExecutor:
    """Real subprocess bridge to the installed local VoxCPM2 one-shot generator."""

    python_executable: Path
    generator_script: Path
    model_dir: Path
    device: str = "cuda"
    timeout_seconds: int = 1800

    def readiness_problems(self) -> list[str]:
        problems: list[str] = []
        if not self.python_executable.is_file():
            problems.append("VoxCPM2 Python runtime is missing")
        if not self.generator_script.is_file():
            problems.append("VoxCPM2 one-shot generator is missing")
        required = ("config.json", "model.safetensors", "audiovae.pth", "tokenizer.json")
        missing = [name for name in required if not (self.model_dir / name).is_file()]
        if missing:
            problems.append("VoxCPM2 model files are missing: " + ", ".join(missing))
        if self.device not in {"cuda", "cpu"}:
            problems.append("VoxCPM2 device must be explicitly cuda or cpu")
        return problems

    def synthesize(self, request: VoxCPM2SynthesisRequest) -> VoxCPM2SynthesisResult:
        problems = self.readiness_problems()
        if problems:
            raise VoxCPM2ExecutionError("; ".join(problems))
        if not request.text.strip() or not request.reference_audio.is_file():
            raise VoxCPM2ExecutionError("audio synthesis requires text and an existing confirmed voice reference")
        if request.mode not in {"basic", "ultimate"}:
            raise VoxCPM2ExecutionError("unsupported VoxCPM2 mode")
        if request.mode == "ultimate" and not (request.emotion_prompt_text or request.prompt_text).strip():
            raise VoxCPM2ExecutionError("ultimate mode requires the matching reference transcript")
        if request.segment_strategy not in {"auto", "segment"}:
            raise VoxCPM2ExecutionError("formal long-form production may not force one unsegmented generation")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        cache_root = request.output_dir / ".runtime_cache"
        temp_root = cache_root / "temp"
        numba_cache = cache_root / "numba"
        model_cache = cache_root / "model"
        for path in (temp_root, numba_cache, model_cache):
            path.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update({
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "TEMP": str(temp_root.resolve()), "TMP": str(temp_root.resolve()),
            "NUMBA_CACHE_DIR": str(numba_cache.resolve()),
            "VOXCPM2_CACHE_DIR": str(model_cache.resolve()),
        })
        command = [
            str(self.python_executable), str(self.generator_script),
            "--text", request.text.strip(),
            "--reference", str(request.reference_audio.resolve()),
            "--output-dir", str(request.output_dir.resolve()),
            "--model-dir", str(self.model_dir.resolve()),
            "--mode", request.mode,
            "--prompt-text", request.prompt_text,
            "--style", request.style_prompt,
            "--emotion-preset", request.emotion_preset,
            "--emotion-strength", str(request.emotion_strength),
            "--segment-strategy", request.segment_strategy,
            "--max-chars", str(request.max_chars),
            "--silence-ms", str(request.silence_ms),
            "--cfg-value", str(request.cfg_value),
            "--inference-timesteps", str(request.inference_timesteps),
            "--device", self.device,
        ]
        if request.emotion_reference_audio is not None:
            if not request.emotion_reference_audio.is_file():
                raise VoxCPM2ExecutionError("configured emotion reference audio is missing")
            command.extend(["--emotion-reference", str(request.emotion_reference_audio.resolve())])
        if request.emotion_prompt_text:
            command.extend(["--emotion-prompt-text", request.emotion_prompt_text])
        try:
            completed = run_process_with_liveness(
                command,
                kind="audio_generation",
                env=environment,
                process_options=hidden_process_kwargs(),
            )
        except RuntimeLivenessError as exc:
            raise VoxCPM2ExecutionError(str(exc)) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown failure"
            raise VoxCPM2ExecutionError(f"VoxCPM2 generation failed: {detail}")
        result = _last_json_object(completed.stdout)
        audio_path = Path(str(result.get("wav_path") or "")).resolve()
        output_root = request.output_dir.resolve()
        if output_root not in audio_path.parents or not audio_path.is_file():
            raise VoxCPM2ExecutionError("VoxCPM2 returned an audio path outside the controlled output directory")
        metadata_path = audio_path.with_suffix(".json")
        if not metadata_path.is_file():
            raise VoxCPM2ExecutionError("VoxCPM2 did not create the required matching metadata")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("text") != request.text.strip():
            raise VoxCPM2ExecutionError("VoxCPM2 metadata does not preserve the requested final script")
        if int(metadata.get("segment_count") or 0) < 1:
            raise VoxCPM2ExecutionError("VoxCPM2 metadata has no generated segments")
        digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        return VoxCPM2SynthesisResult(
            audio_path=audio_path, metadata_path=metadata_path,
            audio_sha256=digest, metadata=metadata,
        )
