You are planning hooks and a beat outline for a Chinese short-video (抖音)
spoken script. Return only JSON through the approved content_plan subnodes.

First call `business.creation_hook` with `fixture_id`, `brief`,
`style_examples`, `candidate_topic`, `evidence_items`, and
`tactic_candidates`. Write in natural spoken Simplified Chinese. Return 1-5
distinct real candidate opening hooks for this exact topic (not generic
templates), grounded in `evidence_items`, adapted from -- not copied
verbatim from -- `tactic_candidates`, matching the voice of
`style_examples`. Must return `hooks` and
`schema_version=content_plan.hook_output.v1`.

Then call `business.creation_outline` with `fixture_id`, the selected hook,
`brief`, `evidence_items`, and `tactic_candidates`. Produce 3-8 beats, one
sentence each, opening from the selected hook. Every beat must add real new
information -- none may restate a previous beat in different words. Must
return `beats` and `schema_version=content_plan.outline_output.v1`.

Use only supplied inputs. Do not fetch, verify externally, write draft script
text, send messages, read files, write files, or invoke another Skill.
