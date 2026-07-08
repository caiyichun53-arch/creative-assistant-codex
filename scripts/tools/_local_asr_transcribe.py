"""Standalone local SenseVoice transcription worker.

Runs under a dedicated Python environment with funasr installed (not the
project's main environment) -- invoked as a subprocess by
compare_asr_providers.py, never imported directly. Takes one audio file path,
prints the transcript to stdout as the only output line so the caller can
capture it cleanly.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F300-\U0001F9FF]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_path")
    parser.add_argument("--asr-model", required=True)
    parser.add_argument("--vad-model", required=True)
    args = parser.parse_args()

    # funasr/torchaudio print setup notices ("ffmpeg is not installed...",
    # version banners) directly to stdout -- some at import time (torchaudio's
    # backend detection), some during model construction/generation. Discard
    # all of it here so this subprocess's stdout contains nothing but the
    # transcript, since the caller captures raw stdout as the transcript
    # verbatim (2026-07-08: found polluting a real stored transcript in
    # data/formal/production_activation.sqlite3's hit_transcripts table --
    # the import-time notice specifically survived an earlier fix attempt
    # that only wrapped the AutoModel()/generate() calls, not the imports).
    with contextlib.redirect_stdout(io.StringIO()):
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess

        model = AutoModel(
            model=args.asr_model,
            vad_model=args.vad_model,
            vad_kwargs={"max_single_segment_time": 30000},
            disable_update=True,
        )
        result = model.generate(
            input=args.audio_path, language="auto", use_itn=True, batch_size_s=60, merge_vad=True, merge_length_s=15
        )
    text = "".join(rich_transcription_postprocess(item["text"]) for item in result)
    text = EMOJI.sub("", text).strip()
    # On Windows, a subprocess's stdout defaults to the console's legacy
    # codepage (e.g. GBK) when piped, not UTF-8 -- without this, the caller
    # (which decodes as UTF-8) mangles every non-ASCII character (found
    # corrupting the same real transcript above: ASCII notice text survived,
    # the actual Chinese transcript did not).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
