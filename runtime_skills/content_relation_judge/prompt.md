Return only JSON with keys relation_type, relation_direction, confidence, evidence_from_left, evidence_from_right, compared_dimensions, missing_evidence, rationale, schema_version.

Judge only the formal relation between left_content and right_content.

Allowed relation_type values: same_item, equivalent, contains, contained_by, complementary, contradicts, related_distinct, no_relation, insufficient_evidence.

technical_failure is not a relation_type. If confidence for a concrete relation would be low, return insufficient_evidence. Evidence fields must contain only items from the provided evidence refs. Use schema_version content_relation_judge.output.v1.
