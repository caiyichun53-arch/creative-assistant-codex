# GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01 Validation Report

status: `COMPLETED`

## Business Contract
- skill_id: `content_classify`
- skill_version: `1.0.0`
- source_document_count: `4`
- missing_requirement_count: `0`

## Formal Mapping
- formal_skill_count: `12`
- business_node_count: `10`
- active_formal_business_skill: `content_classify`
- unmapped_existing_business_nodes: `0`

## First Formal Skill
- formal_skill_id: `content_classify`
- version: `1.0.0`
- route_name: `business.topic_judgement`
- allowed_model_nodes: `business.topic_judgement`
- schema_validation: `passed`
- standalone_adapter: no state store, no file writes, no other Skill calls, ModelGateway only

## Fake Fixture E2E
- required_fixture_count: `17`
- implemented_fixture_count: `17`
- missing_required_fixtures: `0`
- job_status: `succeeded`
- result_schema_version: `content_classify.output.v1`
- result_primary_label: `fan_kepu_social_life`
- outbox_count: `1`
- provider_call_count: `1`

## Live Provider Gate
- status: `COMPLETED`
- formal_skill_id: `content_classify`
- logical_route: `business.topic_judgement`
- provider_type: `hermes`
- actual_model: `xiaomi/mimo-v2.5-pro`
- actual_call_count: `1`
- model_gateway_used: `True`
- live_model_port_used: `True`
- dry_run_fallback: `False`
- fake_port_fallback: `False`
- schema_validation: `passed`
- classification_status: `classified`
- primary_label: `fan_kepu_social_life`
- usage_status: `available`
- prompt_tokens: `452`
- completion_tokens: `159`
- total_tokens: `611`
- cost_status: `not_reported`
- retry_count: `0`
- idempotent_replay: `True`
- idempotent_replay_second_live_call: `False`
- formal_result_count: `1`
- outbox_success_event_count: `1`
- feishu_dispatched: `False`
- report_path: `CONTENT_CLASSIFY_LIVE_GATE_STATUS.yaml`

## PostgreSQL E2E Gate
- gate_status: `passed`
- postgres_version: `PostgreSQL 16.14 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit`
- isolation: `disposable Docker container without host port exposure`
- schema_rounds: `2`
- initialized_formal_table_count: `20`
- initialized_formal_row_count: `0`
- two_session_concurrent_claim_worker_b_rows: `0`
- disposable_database_dropped: `True`
- container_removed: `True`
- secrets_recorded: `false`
- external_llm_called: `false`
- feishu_called: `false`
- legacy_data_imported: `false`
- evidence_path: `validation_evidence\GOAL-BUSINESS-SKILL-CONTENT-CLASSIFY-01_POSTGRES.md`

## Direct Model Calls
- formal_production_direct_model_call_count: `0`
- legacy_direct_model_call_count: `21`

## Clean Room
- formal_table_count: `20`
- formal_total_rows: `0`

## Source Note
- `target-architecture.md` and `rebuild-direction.md` were not present in the repo or memory folder; this matches earlier memory evidence and was not treated as a blocker.

## Commands
- `python scripts\core\model_gateway\formal_skill_adapter.py`
- `python scripts\core\model_gateway\run_content_classify_live_gate.py`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\core\model_gateway\run_content_classify_postgres_gate.ps1`
- `python -m unittest tests.core.test_formal_skill_adapter tests.core.test_business_route_registry tests.validation.test_live_gates tests.core.test_runtime_vertical_slice`
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codex_pycache_goal_skill_adapter'; python -m py_compile scripts\core\model_gateway\formal_skill_adapter.py scripts\core\model_gateway\run_content_classify_live_gate.py tests\core\test_formal_skill_adapter.py`
- `git diff --check`
