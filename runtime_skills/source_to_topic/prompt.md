# source_to_topic model instruction

Return only JSON. Transform already supplied and frozen source material into at most one good topic candidate for a Chinese short-video (抖音) content team.

Do not search, fetch URLs, research facts, classify the domain, judge source relation, query databases, read or write files, call other Skills, invent facts, or add evidence outside the input. `supporting_evidence` must contain only items from `source_evidence_refs`. Use only supplied `experience_cards`; do not claim experience that was not supplied. Do not output score, rank, weight, numeric ratings, or automatic final selection.

First discover possible angles from the supplied material. Treat these as topic-finding paths, not after-the-fact checks:
- problem_angle: a concrete question ordinary viewers would actually ask.
- audience_relevance_angle: how the source relates to ordinary life, decisions, emotions, work, family, consumption, or misconceptions.
- content_increment_angle: explanation, warning, decomposition, counter-intuition, or misconception correction beyond repeating the source.
- tension_angle: surface phenomenon versus deeper cause, public intuition versus mechanism, short-term choice versus long-term consequence, or individual feeling versus system rule.
- distinct_angle: a field-specific angle different from obvious hot-list repetition.
- producible_angle: an angle that can become one short video.
- durable_value_angle: value that remains after the hotspot cools down.

Then converge the discovered angles. Remove directions with insufficient material, uncontrolled risk, duplicate/cooling conflict, pure hotspot repetition, weak domain fit, or no short-video path. Keep one selected direction and optionally record rejected directions. If no direction is usable, return `topic_status: "no_result"` and do not force a candidate.

Required output keys: topic_status, candidate_topic, topic_angle, core_question, audience_relation, content_increment, supporting_evidence, source_constraints, no_result_reason, confidence, angle_discovery, candidate_selection, risks, material_gaps, user_review_required, user_review_reasons, execution_review, experience_usage, schema_version.

Write all human-readable values in Simplified Chinese. `topic_status` is exactly one of `"generated"`, `"generated_good_candidate"`, `"valid_but_weak"`, `"needs_review"`, or `"no_result"`. `confidence` is exactly `"high"`, `"medium"`, `"low"`, or `"none"`, never a number. `no_result_reason` is exactly `"none"`, `"empty_source"`, `"insufficient_source_evidence"`, or `"unsupported_source"`. Use schema_version `source_to_topic.output.v1`.

Before answering, double check every required key is present, every enum uses an allowed string, every execution_review item is boolean, and `schema_version` is present.
