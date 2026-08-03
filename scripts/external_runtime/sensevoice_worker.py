from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from pathlib import Path
import sys
from typing import Any


def _text_parts(result: Any) -> list[str]:
    values = result if isinstance(result, list) else [result]
    parts: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)
    return parts


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Local SenseVoice transcription worker")
    parser.add_argument("media_path")
    parser.add_argument("--asr-model", required=True)
    parser.add_argument("--vad-model", required=True)
    args = parser.parse_args()
    media_path, asr_model, vad_model = Path(args.media_path), Path(args.asr_model), Path(args.vad_model)
    if not media_path.is_file() or not asr_model.is_dir() or not vad_model.is_dir():
        print("media or configured local SenseVoice model is missing", file=sys.stderr)
        return 2
    try:
        with redirect_stdout(sys.stderr):
            import torch
            from funasr import AutoModel
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            model = AutoModel(
                model=str(asr_model), vad_model=str(vad_model),
                device="cuda:0" if torch.cuda.is_available() else "cpu",
                disable_update=True,
            )
            result = model.generate(
                input=str(media_path), cache={}, language="auto", use_itn=True,
                batch_size_s=60, merge_vad=True, merge_length_s=15,
            )
        parts = [rich_transcription_postprocess(text).strip() for text in _text_parts(result)]
        transcript = "\n".join(text for text in parts if text)
        if not transcript:
            print("SenseVoice returned no transcript", file=sys.stderr)
            return 3
        print(transcript)
        return 0
    except Exception as exc:
        print(f"SenseVoice failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
