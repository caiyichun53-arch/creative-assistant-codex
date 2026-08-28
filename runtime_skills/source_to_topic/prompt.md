# source_to_topic model instruction

Return exactly one JSON object as plain text. Do not wrap it in a Markdown code fence (```json or ```), and do not prepend or append explanation text. Transform already supplied and frozen source material into at most one good topic candidate for a Chinese short-video (抖音) content team.

Do not search, fetch URLs, research facts, classify the domain, judge source relation, query databases, read or write files, call other Skills, invent facts, or add evidence outside the input. `source_evidence_refs` is the complete allowed evidence vocabulary for this call. Every `supporting_evidence` item must be copied character-for-character from one item in `source_evidence_refs`; return the supplied string itself, not a label, prefix, suffix, punctuation change, paraphrase, translation, or reformatted version. In particular, do not turn an account name or URL into text such as `来源账号：...` or `来源链接：...`, and do not invent or return a source ID unless that exact ID is one of the supplied evidence items. Use `[]` when no supplied evidence can be quoted. Use only supplied `experience_cards`; do not claim experience that was not supplied. Do not output score, rank, weight, numeric ratings, or automatic final selection.

When `source_kind` is `question_expansion`, the input is a qualified expansion lead bound to a formal mother source, not a researched topic or conclusion. The upstream qualification decision is already complete and must not be repeated here. Keep the mother-source relationship, uncertainty and material gaps visible, and convert the qualified lead when it has a usable supplied angle. Do not upgrade the lead into a verified fact, major impact, historical meaning, classic work, or a finished research result. If the supplied lead is internally incomplete or contradicts the frozen contract, return `no_result` and record it as an upstream qualification regression.

Experience cards are not universal rules. Read each card's experience_layer, trigger_signals, applicable_when, use_positions, method, usage_boundary and not_applicable_when before using it. Use a structure card only when the supplied source and the selected angle show a whole-content organization problem or opportunity; use at most one structure card. Use a section_method card only when the named opening, body, transition or ending needs that kind of organization. Use a local_detail card only for a specific wording, example, rhythm or sentence-level issue. If the trigger is not present, leave the card unused. In experience_usage.rationale, explain in plain Chinese which card was used or not used and what supplied condition caused that choice. Never force a card into a topic merely because it is available.

First discover possible angles from the supplied material. Treat these as topic-finding paths, not after-the-fact checks:
- problem_angle: a concrete question ordinary viewers would actually ask.
- audience_relevance_angle: how the source relates to ordinary life, decisions, emotions, work, family, consumption, or misconceptions.
- content_increment_angle: explanation, warning, decomposition, counter-intuition, or misconception correction beyond repeating the source.
- tension_angle: surface phenomenon versus deeper cause, public intuition versus mechanism, short-term choice versus long-term consequence, or individual feeling versus system rule.
- distinct_angle: a field-specific angle different from obvious hot-list repetition.
- producible_angle: an angle that can become one short video.
- durable_value_angle: value that remains after the hotspot cools down.

Then converge the discovered angles. Remove directions with insufficient material, uncontrolled risk, duplicate/cooling conflict, pure hotspot repetition, weak domain fit, or no short-video path. Keep one selected direction and optionally record rejected directions. If no direction is usable, return `topic_status: "no_result"` and do not force a candidate.

Required output keys: topic_status, candidate_topic, topic_angle, core_question, audience_relation, content_increment, supporting_evidence, source_constraints, no_result_reason, confidence, angle_discovery, candidate_selection, risks, material_gaps, user_review_required, user_review_reasons, execution_review, experience_usage, topic_shape, delivery_contract, schema_version.

The output must also contain exactly these two evidence containers:
- topic_shape: core_subject, scope_boundary, one_piece_line.
- delivery_contract: user_gets.
These are factual candidate structure only, not scores, ranks, weights, confidence ratings, recommendations, or model evaluations. Use the supplied material only. If one of these facts cannot be determined stably from the supplied input, return an empty string and keep the uncertainty or gap in the existing source_constraints, material_gaps, risks, or review fields. Do not guess, research, search, or fill a field with generic praise.

topic_shape.core_subject identifies the main person, work, event, relationship, phenomenon, comparison set, or other central object. topic_shape.scope_boundary says what this one piece will cover and what it will not expand into. topic_shape.one_piece_line states the main story, event chain, comparison, relationship, dispute, fact chain, or explanation line that one piece will complete; it must not default to a why/mechanism framing. delivery_contract.user_gets states the concrete content the user will see, know, follow, or obtain after the piece is completed. Abstract phrases such as "引发共鸣", "带来思考", "满足好奇", "提供新视角", "帮助理解", "很有价值", "很有吸引力", or "引发讨论" cannot stand alone as delivery evidence. Keep the four fields content-type neutral and do not repeat the title as a substitute for the facts.

`candidate_selection` must always be an object containing `selected_direction`: use the exact discovered direction chosen for the candidate; use an empty string only when `topic_status` is `no_result`. Do not omit this field.

Write all human-readable values in Simplified Chinese. `topic_status` is exactly one of `"generated"`, `"generated_good_candidate"`, `"valid_but_weak"`, `"needs_review"`, or `"no_result"`. `confidence` is exactly `"high"`, `"medium"`, `"low"`, or `"none"`, never a number. `no_result_reason` is exactly `"none"`, `"empty_source"`, `"insufficient_source_evidence"`, or `"unsupported_source"`. Use schema_version `source_to_topic.output.v2`.

The following keys must always be JSON arrays, even when there is only one item or no item: `supporting_evidence`, `source_constraints`, `risks`, `material_gaps`, `user_review_reasons`, `candidate_selection.rejected_directions`, `experience_usage.used_experience_ids`, and `experience_usage.unused_experience_ids`. Never return these keys as strings, objects, null, or comma-separated text. Use `[]` when empty.

`execution_review` must always be a JSON object, never an array, string, or paragraph. It must contain exactly these boolean keys: `used_only_supplied_material`, `did_not_search_by_itself`, `did_not_invent_facts`, `respected_domain_boundary`, `respected_risk_boundary`, `did_not_force_candidate`, and `no_score_rank_weight`.

`angle_discovery` must always be a JSON object, never an array or list of paragraphs. It must contain exactly these object keys: `problem_angle`, `audience_relevance_angle`, `content_increment_angle`, `tension_angle`, `distinct_angle`, `producible_angle`, and `durable_value_angle`. Each angle object must contain `found` (boolean), `direction` (string), and `reason` (string).

Before answering, double check every required key is present, every enum uses an allowed string, every array field is a JSON array, every execution_review item is boolean, and `schema_version` is present.

The following is the complete input for this call. Treat it as the only source material available to you:

source_kind: {source_kind}
source_content: {source_content}
source_evidence_refs: {source_evidence_refs}
domain_label: {domain_label}
relation_summary: {relation_summary}
event_cluster_summary: {event_cluster_summary}
deterministic_prefilter: {deterministic_prefilter}
material_packet: {material_packet}
duplicate_cooling_status: {duplicate_cooling_status}
domain_rule_summary: {domain_rule_summary}
experience_cards: {experience_cards}
user_direction: {user_direction}
