Return exactly one JSON object with keys decision, candidate, source_ids. Use only the supplied frozen breakdowns and content-plan context. Do not fetch material, infer missing facts, claim a writing method is effective, call a skill, access memory, write data, or change state.

First decide whether these specific sources support one concrete reusable spoken-content observation that is relevant to the supplied content-plan context. If not, return {"decision":"no_proposal","candidate":null,"source_ids":[]}.

If they do, return decision "proposal". candidate must contain exactly summary, applicable_when, method, boundary. summary is one concrete observation, not a broad slogan. applicable_when, method and boundary are non-empty arrays of plain Chinese strings. source_ids must contain only supplied source_id values and cite at least two sources. Do not say or imply that the method is verified, popular, universally used, or caused high views. Do not turn several videos into generic domain common sense. The human will decide whether to accept it.

domain_label={domain_label}
content_plan_context={content_plan_context}
frozen_breakdowns={frozen_breakdowns}
