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

`candidate_selection` must always be an object containing `selected_direction`: use the exact discovered direction chosen for the candidate; use an empty string only when `topic_status` is `no_result`. Do not omit this field.

Write all human-readable values in Simplified Chinese. `topic_status` is exactly one of `"generated"`, `"generated_good_candidate"`, `"valid_but_weak"`, `"needs_review"`, or `"no_result"`. `confidence` is exactly `"high"`, `"medium"`, `"low"`, or `"none"`, never a number. `no_result_reason` is exactly `"none"`, `"empty_source"`, `"insufficient_source_evidence"`, or `"unsupported_source"`. Use schema_version `source_to_topic.output.v1`.

The following keys must always be JSON arrays, even when there is only one item or no item: `supporting_evidence`, `source_constraints`, `risks`, `material_gaps`, `user_review_reasons`, `candidate_selection.rejected_directions`, `experience_usage.used_experience_ids`, and `experience_usage.unused_experience_ids`. Never return these keys as strings, objects, null, or comma-separated text. Use `[]` when empty.

`execution_review` must always be a JSON object, never an array, string, or paragraph. It must contain exactly these boolean keys: `used_only_supplied_material`, `did_not_search_by_itself`, `did_not_invent_facts`, `respected_domain_boundary`, `respected_risk_boundary`, `did_not_force_candidate`, and `no_score_rank_weight`.

`angle_discovery` must always be a JSON object, never an array or list of paragraphs. It must contain exactly these object keys: `problem_angle`, `audience_relevance_angle`, `content_increment_angle`, `tension_angle`, `distinct_angle`, `producible_angle`, and `durable_value_angle`. Each angle object must contain `found` (boolean), `direction` (string), and `reason` (string).

Before answering, double check every required key is present, every enum uses an allowed string, every array field is a JSON array, every execution_review item is boolean, and `schema_version` is present.
