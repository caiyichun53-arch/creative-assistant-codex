# script_generate Validation Report

status: `COMPLETED_CHECKPOINT`

## Scope

- formal_skill_id: `script_generate`
- implementation_commit: `01ffc35`
- contract: `SCRIPT_GENERATE_BUSINESS_CONTRACT.yaml`
- runtime_skill: `runtime_skills/script_generate/skill.yaml`
- route: `business.creation_draft`
- materializer_contract: `formal_business_skill_result.v1`

## Boundary

- Uses ModelGateway only.
- Does not call another Skill.
- Does not read or write files at runtime.
- Does not fetch external sources.
- Does not review, polish, judge AI flavor, run banned-word checks, publish, or send messages.

## Verification

- `python -m unittest tests.core.test_script_generate_skill`
  - PASS, 8 tests
- `python -m unittest tests.core.test_script_generate_skill tests.core.test_content_plan_skill tests.core.test_production_research_plan_skill tests.core.test_research_evidence_extract_skill tests.core.test_tactic_extract_skill tests.core.test_sample_deep_analyze_skill tests.core.test_source_to_topic_skill tests.core.test_remaining_formal_skill_graph tests.core.test_business_route_registry tests.core.test_formal_skill_adapter`
  - PASS, 113 tests
- `$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codex_pycache_goal_v062_script_generate'; python -m py_compile scripts\core\model_gateway\formal_skill_adapter.py tests\core\test_script_generate_skill.py tests\core\test_content_plan_skill.py tests\core\test_production_research_plan_skill.py tests\core\test_research_evidence_extract_skill.py tests\core\test_tactic_extract_skill.py tests\core\test_sample_deep_analyze_skill.py tests\core\test_source_to_topic_skill.py tests\core\test_remaining_formal_skill_graph.py tests\core\test_business_route_registry.py`
  - PASS
- `python scripts\validation\clean_room_empty_db.py --health`
  - PASS, 20 formal tables, 0 total rows, foreign key check passed
- `git diff --check`
  - PASS

## Fixture Evidence

- Required domain fixtures present:
  - `fan_kepu_social_life`
  - `music_entertainment`
  - `third_domain_neutral`
- Failure fixtures are covered by deterministic provider behaviors:
  - empty model output
  - non-JSON model output
  - missing output field
  - empty draft text
- Runtime tests verify retry without duplicate outbox and idempotent enqueue replay.
