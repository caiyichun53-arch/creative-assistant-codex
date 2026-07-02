# GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01 Validation Report

status: `COMPLETED`

## Formal Responsibility
- skill_id: `content_relation_judge`
- skill_version: `1.0.0`
- responsibility: judge the formal relation between two already provided public content objects or evidence units
- non_responsibilities: content classification, hit judgement, quality scoring, baseline calculation, search, clustering, dedupe writeback, topic generation, research, deep analysis, experience extraction, creation, review, state transition, database query, file read/write, Feishu messaging, other Skill invocation
- content_classify_modified: `false`
- old_data_read: `false`

## Formal Relation Taxonomy
- relation_types: `same_item`, `equivalent`, `contains`, `contained_by`, `complementary`, `contradicts`, `related_distinct`, `no_relation`, `insufficient_evidence`
- symmetric_relations: `same_item`, `equivalent`, `complementary`, `contradicts`, `related_distinct`, `no_relation`, `insufficient_evidence`
- asymmetric_relations: `contains`, `contained_by`
- A/B swap: `contains -> contained_by`, `contained_by -> contains`; all symmetric relations remain unchanged
- primary_relation_priority: `same_item -> equivalent -> contradicts -> contains/contained_by -> complementary -> related_distinct -> no_relation`
- insufficient_evidence_policy: evidence insufficiency bypasses priority order and returns `insufficient_evidence`

## Business Semantics
- no_relation: information is sufficient, comparison was completed, and there is no meaningful direct content relation
- insufficient_evidence: input is missing, too short, noisy, ambiguous, or lacks a reliable comparison object; normal business result but distinct from no_relation
- technical_failure: credential failure, provider refusal, timeout, rate limit, 5xx, empty response, non-JSON, schema error, unknown route, runner/materializer exception; not a relation_type and creates no formal success result
- confidence_values: `high`, `medium`, `low`
- low_confidence_rule: low confidence for a concrete relation must become `insufficient_evidence`
- missing_requirements: `none`

## Input And Output Contract
- input_schema: `content_relation_judge.input.v1`
- required_input_fields: `request_id`, `correlation_id`, `left_content`, `right_content`, `left_evidence_items`, `right_evidence_items`, `domain_context`, `relation_scope`, `schema_version`
- output_schema: `content_relation_judge.output.v1`
- required_output_fields: `relation_type`, `relation_direction`, `confidence`, `evidence_from_left`, `evidence_from_right`, `compared_dimensions`, `missing_evidence`, `rationale`, `schema_version`
- evidence_policy: output evidence must be selected from supplied left/right evidence only

## Route Mapping
- logical_model_node: `business.content_relation_judgement`
- model_gateway_used: `true`
- route_mapping_adjusted: `true`
- business.topic_judgement_reused: `false`
- reason: relation judgement is independent from content_classify and must not be absorbed by topic judgement

## Fake Fixture And A/B Tests
- fixture_count: `26`
- relation_fixture_tests: `passed`
- A/B_swap_tests: `passed`
- schema_error_tests: `passed`
- non_json_output_tests: `passed`
- illegal_relation_enum_tests: `passed`
- invalid_direction_tests: `passed`
- evidence_not_in_input_tests: `passed`
- idempotency_tests: `passed`
- retry_tests: `passed`
- materializer_failure_tests: `passed`
- outbox_success_once_tests: `passed`

## Live Provider Gate
- status: `COMPLETED`
- actual_call_count: `1`
- logical_route: `business.content_relation_judgement`
- actual_model: `xiaomi/mimo-v2.5-pro`
- provider_type: `hermes`
- dry_run_fallback: `false`
- fake_port_fallback: `false`
- tools_disabled: `true`
- memory_disabled: `true`
- messaging_disabled: `true`
- relation_type: `contains`
- relation_direction: `left_contains_right`
- confidence: `high`
- prompt_tokens: `497`
- completion_tokens: `192`
- total_tokens: `689`
- latency_ms: `4874`
- cost_status: `not_reported`
- retry_count: `0`
- idempotent_replay_second_live_call: `false`
- evidence_path: `CONTENT_RELATION_JUDGE_LIVE_GATE_STATUS.yaml`

## PostgreSQL E2E Gate
- gate_status: `passed`
- isolation: `disposable Docker container without host port exposure`
- initialized_formal_table_count: `20`
- initialized_formal_row_count: `0`
- success_case: `true`
- insufficient_evidence_case: `true`
- no_relation_case: `true`
- provider_failure_no_formal_result: `true`
- schema_error_no_success_result: `true`
- invalid_enum_no_success_result: `true`
- direction_error_no_success_result: `true`
- retry_reclaim: `true`
- idempotency_duplicate_rejected: `true`
- two_session_concurrent_claim_worker_b_rows: `0`
- materializer_failure_preserved_counts: `true`
- outbox_dedupe: `true`
- immutable_result_index: `true`
- ab_swap_semantics: `true`
- disposable_database_dropped: `true`
- container_removed: `true`
- external_llm_called: `false`
- feishu_called: `false`
- legacy_data_imported: `false`
- evidence_path: `validation_evidence\GOAL-BUSINESS-SKILL-CONTENT-RELATION-JUDGE-01_POSTGRES.md`

## Regressions And Static Gates
- content_classify_regression: `passed`
- FormalBusinessSkillAdapter_tests: `passed`
- BusinessRouteRegistry_tests: `passed`
- RuntimeVerticalSlice_tests: `passed`
- clean_room_readiness: `passed`
- production_startup_smoke: `passed`
- project_check: `passed`
- py_compile: `passed`
- git_diff_check: `passed`
- formal_production_direct_model_call_count: `0`
- legacy_direct_model_call_count: `21` isolated, unchanged
- clean_room_formal_db: `20 tables, 0 rows`

## Commands
- `python -m unittest tests.core.test_business_route_registry tests.core.test_formal_skill_adapter`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\core\model_gateway\run_content_relation_judge_postgres_gate.ps1`
- `python scripts\core\model_gateway\run_content_relation_judge_live_gate.py`
- `python -m unittest tests.core.test_formal_skill_adapter tests.core.test_business_route_registry tests.validation.test_live_gates tests.core.test_runtime_vertical_slice`
- `python scripts\validation\clean_room_readiness.py --require-safe`
- `python scripts\validation\production_startup_smoke.py --require-legacy-absent`
- `python scripts\project_check.py`
- `python scripts\core\model_gateway\business_route_registry.py`
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codex_pycache_relation_goal_final'; python -m py_compile scripts\core\model_gateway\formal_skill_adapter.py scripts\core\model_gateway\business_route_registry.py scripts\core\model_gateway\run_content_relation_judge_live_gate.py tests\core\test_formal_skill_adapter.py tests\core\test_business_route_registry.py`
- `python scripts\validation\clean_room_empty_db.py --health`
- `git diff --check`

## Next Goal
- Recommended: `GOAL-BUSINESS-SKILL-SOURCE-TO-TOPIC-01`
- Scope: formalize source_to_topic as a separate Skill that transforms supplied source evidence into a candidate topic without invoking research, creation, Feishu, legacy data, or content_relation_judge.
