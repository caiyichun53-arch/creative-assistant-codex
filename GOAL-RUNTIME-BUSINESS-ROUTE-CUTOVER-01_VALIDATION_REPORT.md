# GOAL-RUNTIME-BUSINESS-ROUTE-CUTOVER-01 Validation Report

status: `COMPLETED`

## Registry
- formal_business_llm_node_count: `11`
- logical_routes:
  - `business.ai_flavor_judge`
  - `business.content_relation_judgement`
  - `business.creation_draft`
  - `business.creation_hook`
  - `business.creation_outline`
  - `business.creation_polish`
  - `business.creation_review`
  - `business.research_synthesis`
  - `business.reverse_dna_analysis`
  - `business.reverse_pattern_reduce`
  - `business.topic_judgement`

## Direct Model Calls
- formal_production_direct_model_call_count: `0`
- legacy_direct_model_call_count: `21`
- legacy_direct_model_calls:
  - `scripts/llm/call.py:49` _MODEL_MAP = {"claude-haiku": "haiku", "claude-sonnet": "sonnet", "claude-opus": "opus"}
  - `scripts/llm/call.py:98` def call_llm(node: str, prompt: str, allowed_tools: list[str] | None = None,
  - `scripts/llm/call.py:111` cmd = ["claude", "-p", "--model", model]
  - `scripts/reverse/dna.py:26` from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
  - `scripts/reverse/dna.py:67` body = call_llm("reverse_dna", PROMPT.format(
  - `scripts/reverse/reduce.py:41` from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
  - `scripts/reverse/reduce.py:298` out = call_llm("reverse_reduce", prompt)
  - `scripts/reverse/reduce.py:401` out = call_llm("reverse_reduce", CRITERIA_PROMPT.format(domain=domain, n=n, digest=digest))
  - `scripts/reverse/upgrade_examples.py:31` from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
  - `scripts/reverse/upgrade_examples.py:132` out = call_llm("reverse_reduce", PROMPT.format(dim=dim, body=ben, why=why or "(未写)"))
  - `scripts/research/research.py:15` from call import call_llm, load_prompt  # noqa: E402
  - `scripts/research/research.py:39` out = call_llm("research", prompt, allowed_tools=["WebSearch"], timeout=900)
  - `scripts/humanize/comment_voice.py:34` from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
  - `scripts/humanize/comment_voice.py:120` return call_llm("human_flavor", PROMPT.format(domain=domain, n=n, digest=joined),
  - `scripts/humanize/comment_voice.py:128` notes.append(call_llm("human_flavor",
  - `scripts/humanize/comment_voice.py:135` notes.append(call_llm("human_flavor",
  - `scripts/humanize/comment_voice.py:142` merged.append(call_llm("human_flavor",
  - `scripts/humanize/comment_voice.py:149` merged.append(call_llm("human_flavor",
  - `scripts/humanize/extract.py:33` from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
  - `scripts/humanize/extract.py:163` note = call_llm("human_flavor", MAP_PROMPT.format(
  - `scripts/humanize/extract.py:203` out.append(call_llm("human_flavor", REDUCE_PROMPT.format(notes=joined), timeout=1800))

## Hermes Isolation
- passed: `True`
- recursive_chain_possible: `False`
- tools_disabled: `True`
- memory_disabled: `True`
- feishu_messaging_disabled: `True`
- nested_job_orchestration_disabled: `True`
- file_or_terminal_side_effects_disabled: `True`

## Fixture Route Tests
- tested_node_count: `11`
- fixture_provider_call_count: `11`
- outbox_count: `0`

## Clean Room
- formal_table_count: `20`
- formal_total_rows: `0`

## Commands
- `python scripts\core\model_gateway\business_route_registry.py`
- `python -m unittest tests.core.test_business_route_registry tests.validation.test_live_gates tests.core.test_runtime_vertical_slice`
- `python -m py_compile scripts\core\model_gateway\business_route_registry.py scripts\core\model_gateway\hermes_model_provider.py tests\core\test_business_route_registry.py`

## Next Goal
- Recommended: `GOAL-RUNTIME-BUSINESS-SKILL-ADAPTER-01`, scoped to wiring selected business Skill adapters to these registered routes with synthetic fixtures only.
