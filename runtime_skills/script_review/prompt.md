Use only the approved `script_review` subnodes.

First call `business.creation_review` with `fixture_id`, `draft_text`, and
`brief`. Then call `business.creation_polish` with `fixture_id`, `draft_text`,
and review issues as `edit_notes`. Finally call `business.ai_flavor_judge` with
`fixture_id`, the polished text, and `human_reference_refs`.

Do not fetch sources, publish, write files, write experience, invoke another
Skill, or run deterministic banned-word checks.
