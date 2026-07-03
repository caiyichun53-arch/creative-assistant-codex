# production_research_plan model instruction

Return only JSON with keys summary, claims, plan_steps, open_questions, schema_version.

Create a bounded production research plan from the supplied candidate topic, evidence_refs, and tactic_candidates only. Do not fetch, verify externally, write an outline, generate script text, or call other Skills. claims must come from evidence_refs.claim. Each plan_steps item must include step, purpose, uses, and uses must reference an input claim or tactic candidate. Use schema_version production_research_plan.output.v1.
