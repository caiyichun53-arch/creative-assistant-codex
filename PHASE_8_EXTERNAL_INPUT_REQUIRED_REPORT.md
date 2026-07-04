# Phase 8 External Input Required Report

This report records the current model-configuration boundary after the Hermes
runtime model policy was reset to Mimo-only.

## Current Configuration Findings

- `.env.live-gates` uses `HERMES_BUSINESS_MODEL_*` fields for the business model provider.
- `HERMES_BUSINESS_MODEL_CLASS` must be `mimo`.
- `HERMES_BUSINESS_MODEL_NAME` is expected to be `xiaomi/mimo-v2.5-pro` or another approved Mimo model exposed by Hermes.
- Daily LLM configuration in `config/settings.yaml` is also routed to Mimo.
- GPT, OpenAI and Codex are not approved runtime business providers.
- Fallback, backup model lists, provider priority and automatic downgrade remain disabled.

## Required User Inputs

No GPT/OpenAI/Codex business-provider input is required or accepted for the current runtime path.

For live validation, provide only the existing Hermes/Mimo values in ignored local config:

1. `HERMES_BUSINESS_MODEL_TOKEN`
2. `HERMES_BUSINESS_MODEL_BASE_URL`
3. `HERMES_BUSINESS_MODEL_NAME`
4. `HERMES_BUSINESS_MODEL_CLASS=mimo`

## Safety Boundary

- Do not add OpenAI API keys.
- Do not configure ChatGPT Plus, Codex, GPT-5.5 or OpenAI as a Hermes business Provider.
- Do not add fallback, backup, secondary Provider, provider priority or silent downgrade behavior.
