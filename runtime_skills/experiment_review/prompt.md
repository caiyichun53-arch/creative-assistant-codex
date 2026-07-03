# experiment_review model instruction

Return only JSON with keys review_status, supported_experience_refs, refuted_experience_refs, inconclusive_experience_refs, key_findings, evidence_used, next_actions, schema_version.

Review only the supplied experiment packet. Do not publish, revise, or write formal experience. Do not call another Skill or fetch missing context. Experience refs must come only from tested_experience_refs. evidence_used must contain only claims from evidence_refs. Use schema_version experiment_review.output.v1.
