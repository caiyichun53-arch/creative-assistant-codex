# External Live Gate Validation Report

status: `NOT_READY`

updated_at: `2026-07-01T20:33:14Z`

## GATE-ASR

- status: `LIVE_PASSED`
- preflight_result: `PREFLIGHT_PASSED`
- live_validation_result: `LIVE_PASSED`
- implementation: `tools/asr/asr_test.py` / `tools/asr/transcribe.py` pattern with FunASR SenseVoice local runtime
- runtime: `tools/asr/.venv/Scripts/python.exe`
- model_root_confirmed: `true`
- model_root_name: `AI_Models`
- asr_model_subdir: `sensevoice/sensevoice-small`
- vad_model_subdir: `modelscope_cache/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`
- audio_extension: `.wav`
- audio_size_bytes: `152322126`
- external_network_call: `false`
- input_media_hash_recorded: `true`
- output_text_hash_recorded: `true`
- input_media_hash: `2018ea52104be4f61d988d76abaaec2fe60c2682d4123203056190b125ddf964`
- output_text_hash: `60d3c415a10eec22d5d4501717995d55548cb5124e2f6ca720f5cea4c675d136`
- output_chars: `4147`
- redacted_text_summary: `1997年6月1...划分为三个阶段。 (4147 chars)`
- correlation_id: `GATE-ASR.live.2026-07-01T20:32:09Z`
- duration_ms: `64463`
- evidence: `validation_evidence/GATE-ASR/20260701T203209`
- code_defect: `false`

## Safety Reset Completed

After this live check, local config was returned to:

- `allow_live_calls=false`
- `shadow_only=true`
- `GATE-ASR live_enabled=false`
- other live gates disabled
