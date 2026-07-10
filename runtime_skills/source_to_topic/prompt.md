# source_to_topic model instruction

Return only JSON with keys topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints, no_result_reason, confidence, schema_version.

Transform the supplied source evidence into one candidate topic for a Chinese short-video (抖音) content team. Do not classify the domain, judge relations, fetch more context, research facts, write scripts, or call other Skills. supporting_evidence must contain only items from source_evidence_refs. Use schema_version source_to_topic.output.v1.

Write `candidate_topic`, `topic_angle`, `supporting_evidence`, and `source_constraints` in Simplified Chinese. `topic_angle` must be a genuinely distinct angle on the topic, not a restatement of `candidate_topic`.

Field formats -- follow exactly: `topic_status` is EXACTLY one of `"generated"`/`"needs_review"`/`"no_result"`. `confidence` is EXACTLY one of the strings `"high"`/`"medium"`/`"low"`/`"none"` (never a number). `no_result_reason` is EXACTLY one of `"none"`/`"empty_source"`/`"insufficient_source_evidence"`/`"unsupported_source"` (use `"none"` when there is a real result, never empty string). `source_constraints` is a JSON array of short strings, never one paragraph.

Before answering, double check your JSON includes all eight required keys and every enum field uses one of its exact allowed values -- do not omit `schema_version`.
