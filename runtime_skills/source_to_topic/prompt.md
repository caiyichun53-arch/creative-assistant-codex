# source_to_topic model instruction

Return only JSON with keys topic_status, candidate_topic, topic_angle, supporting_evidence, source_constraints, no_result_reason, confidence, schema_version.

Transform the supplied source evidence into one candidate topic. Do not classify the domain, judge relations, fetch more context, research facts, write scripts, or call other Skills. supporting_evidence must contain only items from source_evidence_refs. Use schema_version source_to_topic.output.v1.
