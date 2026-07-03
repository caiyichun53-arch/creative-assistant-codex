# experience_revision_propose model instruction

Return only JSON with keys proposal_status, candidate_type, target_experience_ref, candidate_summary, change_rationale, evidence_refs, governance_warnings, schema_version.

Propose candidate-only experience creation or revision from supplied frozen experience versions, experiment review and evidence. Do not publish, overwrite, or modify formal experience. Do not call another Skill or read memory. evidence_refs must come only from new_evidence_refs source_ref values. Use schema_version experience_revision_propose.output.v1.
