# Phase 8 External Input Required Report

status: `WAITING_FOR_USER_INPUT`

This report records the current Phase 8 blocker set after the user authorized GPT switching, GPT real regression, a real new-data pilot and one controlled real Feishu test message.

## Completed Without External Calls

- Restored the current Phase 8 state from Git and progress files.
- Confirmed Phase 5, Phase 6 and Phase 7 remain completed.
- Disabled the existing Windows scheduled tasks:
  - `CreationAssistant_Daily`
  - `CreationAssistant_Listener`
- Added `scripts/core/staging/verify_goal_v062_phase8_readiness.py` for repeatable no-secret Phase 8 readiness checks.

## Current Configuration Findings

- `.env.live-gates` still points `MODEL_PROVIDER_MODEL` at a Mimo model, not GPT.
- `MODEL_PROVIDER_API_KEY` and `MODEL_PROVIDER_BASE_URL` are present in `.env.live-gates`, but the model binding is not GPT.
- `.env` contains Feishu app and chat fields for the local push channel.
- `.env.live-gates` Feishu fields are still placeholders, including `FEISHU_EVENT_VERIFICATION_TOKEN`.
- No tracked `PHASE_8_REAL_NEW_DATA_APPROVAL.yaml` exists for newly approved pilot-only data sources.
- Production scheduled tasks are disabled.

## Minimal Required User Input

Provide or update these items, then rerun the readiness verifier:

1. Set `.env.live-gates` `MODEL_PROVIDER_MODEL` to the user-approved GPT model.
2. Confirm `.env.live-gates` `MODEL_PROVIDER_BASE_URL` and `MODEL_PROVIDER_API_KEY` are the intended GPT Provider endpoint and credential.
3. Add `FEISHU_EVENT_VERIFICATION_TOKEN` and live Feishu test fields to `.env.live-gates`, or confirm that the existing `.env` app/chat credentials are the approved live test channel and inbound event validation is not required for this pilot.
4. Create `PHASE_8_REAL_NEW_DATA_APPROVAL.yaml` with newly approved pilot sources/accounts, domain scope, max collection count and max workflow/model-call budget.

## Safety State

- GPT was not called.
- DeepSeek was not called.
- No real platform collection was started.
- No real Feishu message was sent.
- No fallback or automatic downgrade path was added.
- No old data was read.
- No production scheduled task is enabled.

## Validation

- `python scripts\core\staging\verify_goal_v062_phase8_readiness.py` - `WAITING_FOR_USER_INPUT`
- `py_compile scripts\core\staging\verify_goal_v062_phase8_readiness.py` - PASS
- `python scripts\validation\clean_room_empty_db.py --health` - PASS, 20 tables and 0 rows
- `git diff --check` - PASS

## Resume Command

```powershell
cd I:\Creation_assistant-codex
python scripts\core\staging\verify_goal_v062_phase8_readiness.py
```
