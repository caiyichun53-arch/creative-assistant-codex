# research_evidence_extract model instruction

Return only JSON with keys evidence_items, uncertainty_notes, schema_version.

Extract evidence only from the supplied research_packet. Each evidence item must include claim, source_ref, supporting_text. source_ref must be one of source_refs and supporting_text must be copied from the research_packet. Do not fetch, verify externally, plan content, or call other Skills. Use schema_version research_evidence_extract.output.v1.
