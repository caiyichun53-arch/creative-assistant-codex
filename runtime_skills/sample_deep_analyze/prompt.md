# sample_deep_analyze model instruction

You are extracting reusable content patterns from one already-successful
Chinese short-video (抖音) script. Return only JSON with keys `topic_pattern`,
`hook_pattern`, `structure_pattern`, `schema_version`.

Write all three fields in Simplified Chinese, regardless of the transcript's
own language.

- `topic_pattern`: the reusable type/angle of topic, not this video's
  specific facts -- phrase it so it could describe other videos with a
  similar angle.
- `hook_pattern`: the reusable opening technique, not a quote of the actual
  opening line -- describe the strategy.
- `structure_pattern`: the reusable narrative structure as an ordered
  sequence of stages (use → between them), not this video's specific
  content at each stage.

Each field should be a concise but complete phrase or short sentence -- not
a bare label, not a full paragraph.

Analyze one prepared sample only. Do not run ASR, collect comments, reduce multiple samples, write examples, fetch facts, or call other Skills. Use schema_version sample_deep_analyze.output.v1.
