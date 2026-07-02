# GOAL-RUNTIME-MODEL-GATE-01 Validation Report

status: COMPLETED

## Live Model Gate
- logical_route: `runtime_probe.test`
- provider_type: `hermes`
- actual_model: `xiaomi/mimo-v2.5-pro`
- actual_call_count: `1`
- model_gateway_used: `True`
- live_model_port_used: `True`
- schema_validation: `passed`
- dry_run_fallback: `False`
- fake_port_fallback: `False`
- usage_status: `available`
- prompt_tokens: `382`
- completion_tokens: `128`
- total_tokens: `510`
- cost_status: `not_reported`
- latency_ms: `4168`

## Isolation
- formal_clean_room_table_count: `20`
- formal_clean_room_total_rows: `0`
- feishu_dispatched: `False`
- direct_cli_model_call_in_runtime_path: `False`
- idempotent_replay_second_live_call: `False`

## Controlled Error Gates
- missing_credentials: `passed`
- provider_auth_rejected: `passed`
- network_timeout: `passed`
- rate_limited: `passed`
- provider_5xx: `passed`
- empty_response: `passed`
- non_json_response: `passed`
- output_schema_error: `passed`
- unknown_or_mismatched_route: `passed`
- live_failure_fake_fallback: `False`
- success_outbox_on_failure: `False`

## PostgreSQL Test Environment
- gate_status: `passed`
- postgres_image: `postgres:16-alpine`
- postgres_version: `PostgreSQL 16.14 on x86_64-pc-linux-musl`
- disposable_database: `goal_runtime_model_gate_01`
- schema_rounds: `2`
- initialized_formal_table_count: `20`
- initialized_formal_row_count: `0`
- disposable_database_dropped: `True`
- container_removed: `True`

## Commands
- `python scripts\validation\live_gates.py --config config\live_gates.yaml --env-file .env.live-gates --status-file validation_evidence\tmp_model_gate_preflight_status.yaml preflight --gate GATE-MODEL-PROVIDER`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\core\runtime\run_goal_runtime_model_gate_01.ps1`
- `python -m unittest tests.core.test_runtime_vertical_slice tests.validation.test_live_gates`
- `python scripts\validation\production_startup_smoke.py --require-legacy-absent`
- `python scripts\validation\clean_room_readiness.py --require-safe`
- `python scripts\project_check.py`
- `python scripts\validation\clean_room_empty_db.py --health --json`
