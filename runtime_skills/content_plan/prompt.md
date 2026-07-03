Return only JSON through the approved content_plan subnodes.

First call `business.creation_hook` with `fixture_id`, `brief`, and `style_examples`.
It must return `hooks` and `schema_version=content_plan.hook_output.v1`.

Then call `business.creation_outline` with `fixture_id`, the selected hook, and
the same brief. It must return `beats` and
`schema_version=content_plan.outline_output.v1`.

Use only supplied inputs. Do not fetch, verify externally, write draft script
text, send messages, read files, write files, or invoke another Skill.
