# tactic_extract model instruction

You are reducing a batch of individual video pattern analyses into a small
set of genuinely reusable patterns (归纳共性) for a Chinese short-video
content team. Return only JSON with keys `common_patterns`,
`example_candidates`, `schema_version`.

- `common_patterns`: only include a pattern if it genuinely recurs across
  at least two different notes -- do not list every distinct phrase seen
  once. Consolidate near-duplicate observations into one pattern. Write in
  Simplified Chinese as a short, genuinely reusable technique description,
  not a copy of one note's exact wording. Aim for a small, memorable set --
  quality and genuine recurrence over exhaustive coverage.
- `example_candidates`: for each common pattern, cite the specific note(s)
  (by id prefix) that best exemplify it and briefly say why. Must not be
  empty when `common_patterns` is non-empty.

Do not publish examples, write to experience memory, fetch files, read DNA notes by path, or call other Skills. Use schema_version tactic_extract.output.v1.
