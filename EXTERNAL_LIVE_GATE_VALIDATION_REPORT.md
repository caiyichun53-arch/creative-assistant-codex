# External Live Gate Validation Report

status: `DRY_RUN_PASSED`

updated_at: `2026-07-01T18:46:14Z`

## Gate Summary

### GATE-HERMES-REAL-HOST

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `HermesCoreBridge`
- evidence: `validation_evidence\GATE-HERMES-REAL-HOST\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `HERMES_TEST_HOST_URL, HERMES_TEST_TOKEN, HERMES_TEST_ACTOR_ID`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-FEISHU-THIN-BINDING

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `FeishuThinBinding`
- evidence: `validation_evidence\GATE-FEISHU-THIN-BINDING\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID, FEISHU_EVENT_VERIFICATION_TOKEN`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-MODEL-PROVIDER

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `ModelGateway`
- evidence: `validation_evidence\GATE-MODEL-PROVIDER\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `MODEL_PROVIDER_API_KEY, MODEL_PROVIDER_PROJECT_ID, MODEL_PROVIDER_MODEL, MODEL_PROVIDER_COST_CAP`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-ASR

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `ASR command boundary`
- evidence: `validation_evidence\GATE-ASR\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `ASR_TEST_MEDIA_PATH, ASR_MODELS_ROOT`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-SEARCH-PROVIDER

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `FormalResearchService`
- evidence: `validation_evidence\GATE-SEARCH-PROVIDER\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `SEARCH_PROVIDER_API_KEY, SEARCH_PROVIDER_NAME, SEARCH_PROVIDER_QUERY_FILE`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-EXTERNAL-COLLECTOR-ADAPTER

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `External collector adapter boundary`
- evidence: `validation_evidence\GATE-EXTERNAL-COLLECTOR-ADAPTER\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `COLLECTOR_TEST_PROFILE_DIR, COLLECTOR_TEST_SAMPLE_URL`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-SHADOW-E2E

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `GOAL-12 staging shadow boundary`
- evidence: `validation_evidence\GATE-SHADOW-E2E\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `SHADOW_PUBLICATION_SINK, HERMES_TEST_HOST_URL, FEISHU_CHAT_ID, MODEL_PROVIDER_API_KEY, SEARCH_PROVIDER_API_KEY`
- missing_test_environment: ``
- live_enabled: `False`

### GATE-CONTINUOUS-FAULT-RECOVERY

- status: `DRY_RUN_PASSED`
- mode: `dry-run`
- adapter: `Scheduler and RuntimeHost`
- evidence: `validation_evidence\GATE-CONTINUOUS-FAULT-RECOVERY\20260701T184614`
- external_side_effect: `False`
- failure_reason: ``
- missing_credentials: `CONTINUOUS_RUN_DURATION_MINUTES, CONTINUOUS_RUN_MAX_EXTERNAL_CALLS, HERMES_TEST_HOST_URL`
- missing_test_environment: ``
- live_enabled: `False`

## Safety Statement

This report is generated from local preflight, dry-run or explicitly guarded single-gate runs.
No run-all live mode exists in the harness.
Dry-run mode does not initiate Hermes, Feishu, model, ASR, search or platform network calls.
