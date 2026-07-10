Use only the approved `script_review` subnodes on a Chinese spoken-narration
short-video (抖音口播) script.

First call `business.creation_review` with `fixture_id`, `draft_text`,
`brief`, and `evidence_items`. Check topic/brief match, coherence, whether
every claim traces to `evidence_items`, and natural spoken register. `verdict`
is pass/revise/fail; each `issues` item must be concrete, not vague.

Then call `business.creation_polish` with `fixture_id`, `draft_text`, and
review issues as `edit_notes`. Fix only the flagged issues -- do not rewrite
unflagged parts or change structure/voice. `polished_text` must be the full
replacement script, not a diff.

Finally call `business.ai_flavor_judge` with `fixture_id`, the polished text,
and `human_reference_refs`. Compare against the real reference examples and
check for concrete AI-writing tells: AI buzzwords (此外/至关重要/深入探讨/强调/
增强/培养/格局 etc.), inflated-significance phrasing (标志着...关键时刻/体现了
...), forced groups of three, negative parallelism (不仅...而且.../这不仅仅是
...而是...), avoiding 是 in favor of 作为/代表/充当, vague attribution (专家
认为/研究显示 with no source), generic upbeat closings (未来可期/值得期待),
dash (——) overuse, uniform sentence rhythm, over-explaining. `ai_flavor_risk`
is low/medium/high; each `revision_targets` item must cite a real instance in
the text.

Do not fetch sources, publish, write files, write experience, invoke another
Skill, or run deterministic banned-word checks.
